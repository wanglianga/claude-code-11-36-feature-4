"""Sampling evidence store and the container relationship graph.

Two evidence-collection modes share this module:

* ``event`` centres register every physical operation (aliquot, merge,
  handover, device temperature series) as it happens — the full event graph.
* ``interval`` centres register daily inventory snapshots and signed
  handovers only — the custody chain is interval-shaped, and the adjudicator
  (services/adjudication.py) caps what such evidence can prove.

Invariants enforced here:

* evidence is append-only; corrections supersede, never rewrite;
* an aliquot never belongs to two mutually exclusive containers over the
  same period — a late conflicting record freezes the affected branch
  instead of overwriting the confirmed chain in receipt order;
* a pool is irreversible: splitting it back into sources without measured
  volume / aliquot-quality evidence is refused and recorded;
* a mislabelled container poisons every certification whose chain crosses
  it, while locked cohort runs keep their original basis.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    Center,
    Container,
    ContainerRelation,
    GraphIssue,
    SampleEvidence,
)

EVIDENCE_TYPES = {
    "sampling_registration",
    "container_barcode",
    "aliquot",
    "merge",
    "handover",
    "inventory",
    "lab_report",
    "temperature_series",
}

CONFIRMED = "confirmed"
FROZEN = "frozen"
REJECTED = "rejected"
SUPERSEDED = "superseded"


class LineageError(RuntimeError):
    pass


def parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        import re

        text = str(value).strip().replace("Z", "+00:00")
        # Query strings decode "+" as a space; restore a trailing offset.
        text = re.sub(r" (?=\d{2}:\d{2}$)", "+", text)
        ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def center_mode(center: Center) -> str:
    mode = (center.config or {}).get("evidence_mode", "event")
    return "interval" if mode == "interval" else "event"


def get_container(db: Session, center_id: int, barcode: str) -> Container | None:
    return db.scalar(
        select(Container).where(
            Container.center_id == center_id, Container.barcode == barcode
        )
    )


# ---------------------------------------------------------------------------
# Evidence ingestion
# ---------------------------------------------------------------------------


def submit_evidence(
    db: Session,
    center: Center,
    *,
    evidence_type: str,
    evidence_uid: str,
    occurred_at,
    issued_by: str,
    payload: dict,
    actor: str,
) -> tuple[SampleEvidence, bool]:
    """Store one evidence record and apply its graph effects.

    Returns ``(evidence, duplicate)``. A byte-identical re-send is
    acknowledged without activating a new revision; a changed payload
    supersedes the previous revision and re-derives its graph effects.
    """
    if evidence_type not in EVIDENCE_TYPES:
        raise LineageError(f"unknown evidence_type {evidence_type!r}")
    occurred = parse_ts(occurred_at)
    digest = payload_hash(payload)
    priors = db.scalars(
        select(SampleEvidence)
        .where(
            SampleEvidence.center_id == center.id,
            SampleEvidence.evidence_uid == evidence_uid,
        )
        .order_by(SampleEvidence.revision.desc())
    ).all()
    active = next((e for e in priors if e.active), None)
    if active and active.payload_hash == digest:
        return active, True

    evidence = SampleEvidence(
        center_id=center.id,
        evidence_uid=evidence_uid,
        revision=(priors[0].revision + 1) if priors else 1,
        evidence_type=evidence_type,
        payload=payload,
        payload_hash=digest,
        occurred_at=occurred,
        issued_by=issued_by,
        active=True,
        supersedes_id=active.id if active else None,
    )
    db.add(evidence)
    db.flush()

    if active is not None:
        active.active = False
        _supersede_graph_effects(db, active)

    _apply_graph_effects(db, center, evidence, actor)
    db.flush()

    # Adjudication triggers (lazy import: adjudication imports helpers here).
    from . import adjudication

    if evidence_type == "lab_report":
        adjudication.adjudicate_report(db, evidence, actor)
    elif evidence_type in (
        "temperature_series",
        "handover",
        "inventory",
        "aliquot",
        "merge",
        "sampling_registration",
    ):
        touched = _containers_named_in(db, center, evidence)
        if touched:
            adjudication.readjudicate_for_containers(
                db, [c.id for c in touched], actor
            )
    return evidence, False


def _supersede_graph_effects(db: Session, old: SampleEvidence) -> None:
    """Undo the graph effects of a superseded evidence revision."""
    relations = db.scalars(
        select(ContainerRelation).where(
            ContainerRelation.evidence_id == old.id,
            ContainerRelation.state.in_((CONFIRMED, FROZEN)),
        )
    ).all()
    for rel in relations:
        if rel.state == CONFIRMED:
            _restore_parent_volume(db, rel)
        rel.state = SUPERSEDED


def _apply_graph_effects(db: Session, center: Center, evidence: SampleEvidence, actor: str) -> None:
    payload = evidence.payload
    if evidence.evidence_type == "sampling_registration":
        barcode = payload.get("sample_barcode")
        if not barcode:
            raise LineageError("sampling_registration needs payload.sample_barcode")
        container = get_container(db, center.id, barcode)
        if container is None:
            container = Container(
                center_id=center.id,
                barcode=barcode,
                kind="sample",
                collection_mode=center_mode(center),
            )
            db.add(container)
        container.subject_ref = payload.get("subject_ref")
        container.volume_ul = payload.get("volume_ul")
        container.registered_evidence_id = evidence.id
        db.flush()
    elif evidence.evidence_type == "container_barcode":
        barcode = payload.get("barcode")
        if not barcode:
            raise LineageError("container_barcode evidence needs payload.barcode")
        container = get_container(db, center.id, barcode)
        if container is None:
            container = Container(
                center_id=center.id,
                barcode=barcode,
                kind=payload.get("kind", "aliquot"),
                collection_mode=center_mode(center),
            )
            db.add(container)
        container.registered_evidence_id = evidence.id
        if payload.get("volume_ul") is not None:
            container.volume_ul = payload["volume_ul"]
        db.flush()
    elif evidence.evidence_type == "aliquot":
        _apply_aliquot(db, center, evidence, actor)
    elif evidence.evidence_type == "merge":
        _apply_merge(db, center, evidence, actor)
    # handover / inventory / temperature_series / lab_report carry no graph
    # structure of their own; the adjudicator consumes them.


def _containers_named_in(db: Session, center: Center, evidence: SampleEvidence) -> list[Container]:
    payload = evidence.payload
    barcodes = set()
    if evidence.evidence_type in ("temperature_series", "handover", "inventory"):
        if payload.get("container_barcode"):
            barcodes.add(payload["container_barcode"])
    elif evidence.evidence_type == "aliquot":
        if payload.get("parent_barcode"):
            barcodes.add(payload["parent_barcode"])
        barcodes.update(
            c.get("barcode") for c in payload.get("children", []) if c.get("barcode")
        )
    elif evidence.evidence_type == "merge":
        if payload.get("pool_barcode"):
            barcodes.add(payload["pool_barcode"])
        barcodes.update(
            s.get("barcode") for s in payload.get("sources", []) if s.get("barcode")
        )
    elif evidence.evidence_type == "sampling_registration":
        if payload.get("sample_barcode"):
            barcodes.add(payload["sample_barcode"])
    if not barcodes:
        return []
    return db.scalars(
        select(Container).where(
            Container.center_id == center.id, Container.barcode.in_(sorted(barcodes))
        )
    ).all()


# ---------------------------------------------------------------------------
# Graph writes: aliquot / merge, conflict detection, freezing
# ---------------------------------------------------------------------------


def _decrement_parent(db: Session, parent: Container, rel: ContainerRelation) -> None:
    if parent.volume_ul is None or rel.quantity_ul is None:
        return
    remaining = float(parent.volume_ul) - float(rel.quantity_ul)
    parent.volume_ul = max(remaining, 0.0)
    if parent.volume_ul <= 0:
        parent.state = "consumed"


def _restore_parent_volume(db: Session, rel: ContainerRelation) -> None:
    parent = db.get(Container, rel.parent_container_id)
    if parent is None or rel.quantity_ul is None or parent.volume_ul is None:
        return
    parent.volume_ul = float(parent.volume_ul) + float(rel.quantity_ul)
    if parent.state == "consumed" and parent.volume_ul > 0:
        parent.state = "active"


def _apply_aliquot(db: Session, center: Center, evidence: SampleEvidence, actor: str) -> list[ContainerRelation]:
    payload = evidence.payload
    parent_barcode = payload.get("parent_barcode")
    children = payload.get("children") or []
    if not parent_barcode or not children:
        raise LineageError("aliquot evidence needs parent_barcode and children[]")
    parent = get_container(db, center.id, parent_barcode)
    if parent is None:
        raise LineageError(f"parent container {parent_barcode!r} is not registered")
    declared_remaining = payload.get("parent_remaining_ul")
    when = evidence.occurred_at

    # Evidence-level checks: the declared remaining volume is the state after
    # ALL children of this record left the parent.
    total_qty = sum(
        float(c["volume_ul"]) for c in children if c.get("volume_ul") is not None
    )
    current_remaining = float(parent.volume_ul) if parent.volume_ul is not None else None
    time_conflict = parent.state == "consumed"
    quantity_conflict = False
    if current_remaining is not None:
        if current_remaining - total_qty < -0.01:
            quantity_conflict = True
        elif declared_remaining is not None and abs(
            (current_remaining - total_qty) - float(declared_remaining)
        ) > 0.01:
            # The record's claimed remaining volume does not match the
            # confirmed chain — a late, overlapping aliquot record.
            quantity_conflict = True

    relations: list[ContainerRelation] = []
    conflicts: list[dict] = []
    for child_spec in children:
        barcode = child_spec.get("barcode")
        qty = child_spec.get("volume_ul")
        if not barcode:
            raise LineageError("aliquot child is missing its barcode")
        child = get_container(db, center.id, barcode)
        if child is None:
            child = Container(
                center_id=center.id,
                barcode=barcode,
                kind=child_spec.get("kind", "aliquot"),
                volume_ul=qty,
                collection_mode=center_mode(center),
                registered_evidence_id=evidence.id,
            )
            db.add(child)
            db.flush()
        # Origin conflict: this child is already claimed by another record.
        incoming = db.scalars(
            select(ContainerRelation).where(
                ContainerRelation.child_container_id == child.id,
                ContainerRelation.state.in_((CONFIRMED, FROZEN)),
                ContainerRelation.evidence_id != evidence.id,
            )
        ).all()
        relation = ContainerRelation(
            parent_container_id=parent.id,
            child_container_id=child.id,
            relation_type="aliquot",
            quantity_ul=qty,
            parent_remaining_after_ul=declared_remaining,
            valid_from=when,
            evidence_id=evidence.id,
            state=CONFIRMED,
        )
        child_conflict = bool(incoming) or time_conflict or quantity_conflict
        if child_conflict:
            relation.state = FROZEN
            prior_ids = [r.id for r in incoming]
            if quantity_conflict or time_conflict:
                # The confirmed consumption of the same parent is the chain
                # this late record contradicts — that branch freezes too.
                prior_ids += [
                    r.id
                    for r in db.scalars(
                        select(ContainerRelation).where(
                            ContainerRelation.parent_container_id == parent.id,
                            ContainerRelation.state == CONFIRMED,
                            ContainerRelation.evidence_id != evidence.id,
                        )
                    ).all()
                ]
            conflicts.append(
                {
                    "child_barcode": barcode,
                    "origin_conflict": bool(incoming),
                    "time_conflict": time_conflict,
                    "quantity_conflict": quantity_conflict,
                    "prior_relation_ids": sorted(set(prior_ids)),
                }
            )
        db.add(relation)
        db.flush()
        relations.append(relation)
    for relation in relations:
        if relation.state == CONFIRMED:
            _decrement_parent(db, parent, relation)
    if conflicts:
        _freeze_branch(db, relations, conflicts, evidence, actor, issue_type="late_aliquot_conflict")
    return relations


def _apply_merge(db: Session, center: Center, evidence: SampleEvidence, actor: str) -> list[ContainerRelation]:
    payload = evidence.payload
    pool_barcode = payload.get("pool_barcode")
    sources = payload.get("sources") or []
    if not pool_barcode or len(sources) < 2:
        raise LineageError("merge evidence needs pool_barcode and at least two sources")
    if get_container(db, center.id, pool_barcode):
        raise LineageError(f"pool barcode {pool_barcode!r} is already registered")
    pool = Container(
        center_id=center.id,
        barcode=pool_barcode,
        kind="pool",
        volume_ul=payload.get("pool_volume_ul"),
        collection_mode=center_mode(center),
        registered_evidence_id=evidence.id,
    )
    db.add(pool)
    db.flush()
    when = evidence.occurred_at

    relations: list[ContainerRelation] = []
    conflicts: list[dict] = []
    for src in sources:
        barcode = src.get("barcode")
        qty = src.get("volume_ul")
        source = get_container(db, center.id, barcode)
        if source is None:
            raise LineageError(f"merge source {barcode!r} is not registered")
        relation = ContainerRelation(
            parent_container_id=source.id,
            child_container_id=pool.id,
            relation_type="merge",
            quantity_ul=qty,
            valid_from=when,
            evidence_id=evidence.id,
            state=CONFIRMED,
        )
        quantity_conflict = (
            qty is not None
            and source.volume_ul is not None
            and float(source.volume_ul) - float(qty) < -0.01
        )
        time_conflict = source.state == "consumed"
        if quantity_conflict or time_conflict:
            relation.state = FROZEN
            prior_ids = [
                r.id
                for r in db.scalars(
                    select(ContainerRelation).where(
                        ContainerRelation.parent_container_id == source.id,
                        ContainerRelation.state == CONFIRMED,
                        ContainerRelation.evidence_id != evidence.id,
                    )
                ).all()
            ]
            conflicts.append(
                {
                    "source_barcode": barcode,
                    "quantity_conflict": quantity_conflict,
                    "time_conflict": time_conflict,
                    "prior_relation_ids": sorted(set(prior_ids)),
                }
            )
        db.add(relation)
        db.flush()
        relations.append(relation)
        if relation.state == CONFIRMED:
            _decrement_parent(db, source, relation)
    if conflicts:
        _freeze_branch(db, relations, conflicts, evidence, actor, issue_type="quantity_conflict")
    return relations


def _freeze_branch(
    db: Session,
    new_relations: list[ContainerRelation],
    conflicts: list[dict],
    evidence: SampleEvidence,
    actor: str,
    *,
    issue_type: str,
) -> GraphIssue:
    """Freeze the affected branch.

    Both the late record's relations and the previously confirmed relations
    they clash with are frozen. The confirmed chain is *not* overwritten in
    receipt order; a curator resolution decides which side stands.
    """
    prior_ids = {
        rid for conflict in conflicts for rid in conflict.get("prior_relation_ids", [])
    }
    priors = (
        db.scalars(select(ContainerRelation).where(ContainerRelation.id.in_(prior_ids))).all()
        if prior_ids
        else []
    )
    for rel in priors:
        if rel.state == CONFIRMED:
            rel.state = FROZEN
            _restore_parent_volume(db, rel)
    container_ids = {r.parent_container_id for r in new_relations + priors}
    container_ids.update(r.child_container_id for r in new_relations + priors)
    issue = GraphIssue(
        issue_type=issue_type,
        status="open",
        container_ids=sorted(container_ids),
        relation_ids=sorted({r.id for r in new_relations + priors}),
        evidence_ids=sorted({evidence.id, *(r.evidence_id for r in priors)}),
        detail={
            "conflicts": conflicts,
            "note": (
                "late record conflicts with the confirmed chain (quantity or "
                "time overlap); branch frozen, confirmed chain not overwritten"
            ),
        },
        created_by=actor,
    )
    db.add(issue)
    db.flush()
    from . import adjudication

    adjudication.readjudicate_for_containers(db, issue.container_ids, actor)
    return issue


def resolve_issue(
    db: Session, issue: GraphIssue, keep_relation_ids: list[int], note: str | None, actor: str
) -> GraphIssue:
    """Curator resolution of a frozen branch: confirm one side, reject the rest."""
    if issue.status != "open":
        raise LineageError(f"issue {issue.id} is already {issue.status}")
    keep = set(keep_relation_ids)
    unknown = keep - set(issue.relation_ids)
    if unknown:
        raise LineageError(f"relations {sorted(unknown)} are not part of issue {issue.id}")
    for rel_id in issue.relation_ids:
        rel = db.get(ContainerRelation, rel_id)
        if rel is None or rel.state != FROZEN:
            continue
        if rel_id in keep:
            rel.state = CONFIRMED
            parent = db.get(Container, rel.parent_container_id)
            if parent is not None:
                _decrement_parent(db, parent, rel)
        else:
            rel.state = REJECTED
    issue.status = "resolved"
    issue.resolved_by = actor
    issue.resolved_at = datetime.now(timezone.utc)
    issue.resolution = {"keep_relation_ids": sorted(keep), "note": note}
    db.flush()
    from . import adjudication

    adjudication.readjudicate_for_containers(db, issue.container_ids, actor)
    return issue


# ---------------------------------------------------------------------------
# Corrections: mislabel judgement and pool split attempts
# ---------------------------------------------------------------------------


def judge_mislabel(db: Session, container: Container, reason: str, actor: str) -> dict:
    """Mark a container as mislabelled and re-adjudicate everything downstream.

    Historical (locked) cohort runs keep their original certification; open
    runs get their references paused by the adjudication sync.
    """
    if container.state == "mislabeled":
        raise LineageError("container is already judged mislabeled")
    container.state = "mislabeled"
    issue = GraphIssue(
        issue_type="mislabel",
        status="resolved",
        container_ids=[container.id],
        relation_ids=[],
        evidence_ids=[],
        detail={"barcode": container.barcode, "reason": reason},
        created_by=actor,
        resolved_by=actor,
        resolved_at=datetime.now(timezone.utc),
        resolution={"judgment": "mislabeled", "reason": reason},
    )
    db.add(issue)
    db.flush()
    from . import adjudication

    impacted = adjudication.readjudicate_for_containers(db, [container.id], actor)
    return {"issue_id": issue.id, "impacted": impacted}


def split_pool(
    db: Session,
    center: Center,
    pool: Container,
    children: list[dict],
    volume_evidence: dict | None,
    actor: str,
) -> list[ContainerRelation]:
    """Attempt to split a pool back into sources.

    Without measured remaining volumes and aliquot-quality evidence the pool
    stays irreversible and no separable lineage is fabricated. With that
    evidence the split is recorded as ordinary aliquots *of the pool* — the
    merge history is never rewritten.
    """
    if pool.kind != "pool":
        raise LineageError("only a pool container can be split")
    measured = (volume_evidence or {}).get("measured_volumes_ul") or {}
    quality_ref = (volume_evidence or {}).get("quality_check_ref")
    barcodes = [c.get("barcode") for c in children]
    sufficient = (
        bool(measured)
        and bool(quality_ref)
        and all(b in measured for b in barcodes)
    )
    if not sufficient:
        issue = GraphIssue(
            issue_type="pool_split_rejected",
            status="resolved",
            container_ids=[pool.id],
            relation_ids=[],
            evidence_ids=[],
            detail={
                "pool_barcode": pool.barcode,
                "attempted_children": children,
                "reason": (
                    "split requested without aliquot-quality / remaining-volume "
                    "evidence; pool stays irreversible"
                ),
            },
            created_by=actor,
            resolved_by=actor,
            resolved_at=datetime.now(timezone.utc),
            resolution={"outcome": "rejected", "pool_state": "irreversible"},
        )
        db.add(issue)
        db.flush()
        raise LineageError(
            "pool is irreversible: splitting it back into sources requires "
            "measured remaining volumes and an aliquot-quality reference; "
            "no separable lineage was fabricated"
        )
    payload = {
        "parent_barcode": pool.barcode,
        "children": [
            {"barcode": b, "volume_ul": measured[b], "kind": "aliquot"} for b in barcodes
        ],
        "parent_remaining_ul": (volume_evidence or {}).get("pool_remaining_ul"),
        "quality_check_ref": quality_ref,
        "split_of_pool": True,
    }
    # Each split is its own evidence record; reusing one uid would supersede
    # the previous split's relations.
    prior_splits = db.scalars(
        select(SampleEvidence.evidence_uid).where(
            SampleEvidence.center_id == center.id,
            SampleEvidence.evidence_uid.like(f"split-{pool.barcode}-%"),
        )
    ).all()
    evidence, _ = submit_evidence(
        db,
        center,
        evidence_type="aliquot",
        evidence_uid=f"split-{pool.barcode}-{len(prior_splits) + 1}",
        occurred_at=datetime.now(timezone.utc),
        issued_by=actor,
        payload=payload,
        actor=actor,
    )
    return db.scalars(
        select(ContainerRelation).where(ContainerRelation.evidence_id == evidence.id)
    ).all()


# ---------------------------------------------------------------------------
# Lineage queries
# ---------------------------------------------------------------------------


def lineage_at(db: Session, container: Container, at) -> tuple[list[Container], list[ContainerRelation]]:
    """The container's ancestry as of ``at`` — which sample(s) it came from."""
    moment = parse_ts(at)
    nodes: dict[int, Container] = {}
    edges: dict[int, ContainerRelation] = {}
    queue = [container]
    while queue:
        node = queue.pop(0)
        if node.id in nodes:
            continue
        nodes[node.id] = node
        incoming = db.scalars(
            select(ContainerRelation)
            .where(
                ContainerRelation.child_container_id == node.id,
                ContainerRelation.valid_from <= moment,
                or_(
                    ContainerRelation.valid_to.is_(None),
                    ContainerRelation.valid_to > moment,
                ),
                ContainerRelation.state.in_((CONFIRMED, FROZEN)),
            )
            .order_by(ContainerRelation.id)
        ).all()
        for rel in incoming:
            edges[rel.id] = rel
            parent = db.get(Container, rel.parent_container_id)
            if parent is not None:
                queue.append(parent)
    return list(nodes.values()), list(edges.values())
