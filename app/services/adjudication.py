"""Usability adjudicator, certification versions and the analysis reference book.

The adjudicator marks every lab report as one of

* ``analysis_ready``    — may enter cohort analysis;
* ``exploratory_only``  — usable for exploration, not for confirmatory analysis;
* ``lineage_pending``   — lineage/custody still has to be verified;
* ``unusable``          — must not be used.

and freezes the verdict together with the exact container chain, temperature
evidence, constraint version and reasons it depends on (the *certification
version*). Re-adjudication after new evidence, corrections or rule changes
creates a new version; superseded versions stay readable so historical cohort
runs keep the sample basis they were computed from.

Downgrade rules implemented here (see the task statement):

* a temperature gap bounded by both handover sign-offs downgrades only the
  assays covered by the gap to exploratory — never the whole subject;
* a device clock skew beyond the protocol tolerance means the series proves
  continuity only, so temperature compliance cannot be certified;
* interval (inventory-only) centres get interval-class certifications that
  are capped for high-sensitivity endpoints;
* a mislabelled container anywhere on the chain makes the assay unusable.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    AnalysisReference,
    CohortRunRef,
    Container,
    ContainerRelation,
    ProtocolConstraint,
    SampleEvidence,
    UsabilityCertification,
)
from .lineage import CONFIRMED, FROZEN, get_container, parse_ts

ANALYSIS_READY = "analysis_ready"
EXPLORATORY_ONLY = "exploratory_only"
LINEAGE_PENDING = "lineage_pending"
UNUSABLE = "unusable"

VERDICT_ORDER = {
    ANALYSIS_READY: 0,
    EXPLORATORY_ONLY: 1,
    LINEAGE_PENDING: 2,
    UNUSABLE: 3,
}

REASON_SEVERITY = {
    "container_mislabeled": UNUSABLE,
    "temperature_excursion": UNUSABLE,
    "storage_duration_exceeded": UNUSABLE,
    "aliquot_count_exceeded": UNUSABLE,
    "min_remaining_volume_violated": UNUSABLE,
    "container_not_registered": LINEAGE_PENDING,
    "no_approved_constraint": LINEAGE_PENDING,
    "lineage_frozen": LINEAGE_PENDING,
    "lineage_root_missing": LINEAGE_PENDING,
    "temperature_gap_unbounded": LINEAGE_PENDING,
    "temperature_evidence_missing": LINEAGE_PENDING,
    "remaining_volume_unproven": LINEAGE_PENDING,
    "temperature_gap_bounded": EXPLORATORY_ONLY,
    "clock_skew_exceeded": EXPLORATORY_ONLY,
    "interval_mode_insufficient": EXPLORATORY_ONLY,
}

# Micro-gaps at segment boundaries up to this size are sensor cadence, not
# custody gaps.
GAP_GRACE_SECONDS = 60


class AdjudicationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------


def _build_chain(db: Session, container: Container, at: datetime):
    """Walk from the assayed container up to the root sample(s) as of ``at``.

    Returns ``(segments, edges)`` where each segment carries the container,
    the chain edges touching it and its custody interval ``[start, end]``.
    """
    segments: dict[int, dict] = {}
    edges: list[ContainerRelation] = []
    queue: list[tuple[Container, ContainerRelation | None]] = [(container, None)]
    while queue:
        node, outgoing = queue.pop(0)
        if node.id in segments:
            continue
        incoming = db.scalars(
            select(ContainerRelation)
            .where(
                ContainerRelation.child_container_id == node.id,
                ContainerRelation.valid_from <= at,
                or_(
                    ContainerRelation.valid_to.is_(None),
                    ContainerRelation.valid_to > at,
                ),
                ContainerRelation.state.in_((CONFIRMED, FROZEN)),
            )
            .order_by(ContainerRelation.id)
        ).all()
        start = min((r.valid_from for r in incoming), default=None)
        end = outgoing.valid_from if outgoing is not None else at
        segments[node.id] = {
            "container": node,
            "incoming_all": incoming,
            "outgoing": outgoing,
            "start": start,
            "end": end,
        }
        edges.extend(incoming)
        for rel in incoming:
            parent = db.get(Container, rel.parent_container_id)
            if parent is not None:
                queue.append((parent, rel))
    # Root segments start at their sampling registration.
    for seg in segments.values():
        if not seg["incoming_all"]:
            node = seg["container"]
            registration = (
                db.get(SampleEvidence, node.registered_evidence_id)
                if node.registered_evidence_id
                else None
            )
            seg["start"] = (
                registration.occurred_at if registration is not None else node.created_at
            )
    return list(segments.values()), edges


def _max_aliquot_depth(segments: list[dict], container_id: int, _seen=None) -> int:
    _seen = _seen or set()
    if container_id in _seen:
        return 0
    _seen = _seen | {container_id}
    seg = next(s for s in segments if s["container"].id == container_id)
    best = 0
    for rel in seg["incoming_all"]:
        depth = _max_aliquot_depth(segments, rel.parent_container_id, _seen) + (
            1 if rel.relation_type == "aliquot" else 0
        )
        best = max(best, depth)
    return best


# ---------------------------------------------------------------------------
# Temperature evidence evaluation
# ---------------------------------------------------------------------------


def _excursion_tolerated(points: list[tuple[datetime, float]], tmin, tmax, exception_rules: dict) -> bool:
    """An excursion is tolerated when it fits one of the exception rules."""
    allowed = (exception_rules or {}).get("allowed_excursions") or []
    if not allowed or not points:
        return False
    duration_min = (points[-1][0] - points[0][0]).total_seconds() / 60.0
    deviation = 0.0
    for _, temp in points:
        if tmin is not None and temp < tmin:
            deviation = max(deviation, tmin - temp)
        if tmax is not None and temp > tmax:
            deviation = max(deviation, temp - tmax)
    for rule in allowed:
        if (
            deviation <= float(rule.get("max_deviation_c", 0))
            and duration_min <= float(rule.get("max_minutes", 0))
        ):
            return True
    return False


def _temperature_check(
    db: Session,
    center_id: int,
    seg: dict,
    constraint: ProtocolConstraint,
) -> tuple[list[dict], dict]:
    """Evaluate temperature evidence for one chain segment.

    Returns ``(findings, basis_fragment)``; each finding is a dict with a
    reason ``code`` and a human-readable ``detail``.
    """
    container = seg["container"]
    start, end = seg["start"], seg["end"]
    tmin = float(constraint.temp_min_c) if constraint.temp_min_c is not None else None
    tmax = float(constraint.temp_max_c) if constraint.temp_max_c is not None else None
    tolerance = constraint.clock_skew_tolerance_seconds
    findings: list[dict] = []
    basis: dict = {
        "container_id": container.id,
        "barcode": container.barcode,
        "interval": [
            start.isoformat() if start else None,
            end.isoformat() if end else None,
        ],
        "allowed_band_c": [tmin, tmax],
        "series": [],
        "inventory_points": [],
        "gaps": [],
        "excursions": [],
    }
    if start is None or end is None or end <= start:
        return findings, basis

    if container.collection_mode == "interval":
        # Interval custody: daily inventory points plus signed handovers.
        # Intra-day order and brief excursions stay unverifiable by design;
        # that residual uncertainty is recorded, not hidden.
        inventories = db.scalars(
            select(SampleEvidence).where(
                SampleEvidence.center_id == center_id,
                SampleEvidence.evidence_type == "inventory",
                SampleEvidence.active.is_(True),
            )
        ).all()
        points = []
        for inv in inventories:
            payload = inv.payload or {}
            if payload.get("container_barcode") != container.barcode:
                continue
            if start <= inv.occurred_at <= end:
                points.append(inv)
        basis["inventory_points"] = [
            {
                "evidence_id": inv.id,
                "observed_at": inv.occurred_at.isoformat(),
                "temp_c": (inv.payload or {}).get("temp_c"),
                "location": (inv.payload or {}).get("location"),
            }
            for inv in sorted(points, key=lambda e: e.occurred_at)
        ]
        basis["intraday_unverified"] = True
        if not points:
            findings.append(
                {
                    "code": "temperature_evidence_missing",
                    "detail": f"no inventory observation covers {container.barcode}",
                }
            )
        else:
            for inv in points:
                temp = (inv.payload or {}).get("temp_c")
                if temp is None:
                    continue
                temp = float(temp)
                if (tmin is not None and temp < tmin) or (tmax is not None and temp > tmax):
                    findings.append(
                        {
                            "code": "temperature_excursion",
                            "detail": (
                                f"inventory {inv.id} observed {temp}°C outside "
                                f"[{tmin}, {tmax}] for {container.barcode}"
                            ),
                        }
                    )
                    basis["excursions"].append(
                        {"evidence_id": inv.id, "temp_c": temp, "observed_at": inv.occurred_at.isoformat()}
                    )
        return findings, basis

    # Event mode: device temperature series.
    all_series = db.scalars(
        select(SampleEvidence).where(
            SampleEvidence.center_id == center_id,
            SampleEvidence.evidence_type == "temperature_series",
            SampleEvidence.active.is_(True),
        )
    ).all()
    windowed = []  # (evidence, skew, device, [(ts, temp)])
    for series in all_series:
        payload = series.payload or {}
        if payload.get("container_barcode") != container.barcode:
            continue
        skew = int(payload.get("clock_skew_seconds", 0))
        device = payload.get("device_id")
        points = []
        for reading in payload.get("readings", []):
            ts = parse_ts(reading["ts"])
            temp = float(reading["temp_c"])
            if start <= ts <= end:
                points.append((ts, temp))
        points.sort()
        if points:
            windowed.append((series, skew, device, points))
        basis["series"].append(
            {
                "evidence_id": series.id,
                "device_id": device,
                "clock_skew_seconds": skew,
                "clock_skew_within_tolerance": abs(skew) <= tolerance,
                "readings_in_window": len(points),
                "first_reading": points[0][0].isoformat() if points else None,
                "last_reading": points[-1][0].isoformat() if points else None,
            }
        )
    if not windowed:
        findings.append(
            {
                "code": "temperature_evidence_missing",
                "detail": (
                    f"no temperature series covers {container.barcode} between "
                    f"{start.isoformat()} and {end.isoformat()}"
                ),
            }
        )
        return findings, basis

    # Coverage gaps inside the custody interval.
    coverage = sorted(
        (points[0][0], points[-1][0]) for _, _, _, points in windowed
    )
    gaps: list[tuple[datetime, datetime]] = []
    cursor = start
    for seg_start, seg_end in coverage:
        if (seg_start - cursor).total_seconds() > GAP_GRACE_SECONDS:
            gaps.append((cursor, seg_start))
        cursor = max(cursor, seg_end)
    if (end - cursor).total_seconds() > GAP_GRACE_SECONDS:
        gaps.append((cursor, end))
    for gap_start, gap_end in gaps:
        receipt = _handover_covering(db, center_id, container.barcode, gap_start, gap_end)
        gap_basis = {
            "start": gap_start.isoformat(),
            "end": gap_end.isoformat(),
            "bounded_by_evidence_id": receipt.id if receipt else None,
        }
        basis["gaps"].append(gap_basis)
        if receipt is not None:
            findings.append(
                {
                    "code": "temperature_gap_bounded",
                    "detail": (
                        f"temperature coverage of {container.barcode} breaks "
                        f"{gap_start.isoformat()}…{gap_end.isoformat()}, bounded by "
                        f"handover receipt {receipt.evidence_uid}"
                    ),
                }
            )
        else:
            findings.append(
                {
                    "code": "temperature_gap_unbounded",
                    "detail": (
                        f"temperature coverage of {container.barcode} breaks "
                        f"{gap_start.isoformat()}…{gap_end.isoformat()} with no "
                        "signed handover bounding the gap"
                    ),
                }
            )

    # Excursions and clock skew.
    skew_exceeded = False
    for series, skew, device, points in windowed:
        if abs(skew) > tolerance:
            skew_exceeded = True
        if tmin is None and tmax is None:
            continue
        excursion = [
            (ts, temp)
            for ts, temp in points
            if (tmin is not None and temp < tmin) or (tmax is not None and temp > tmax)
        ]
        if not excursion:
            continue
        basis["excursions"].append(
            {
                "evidence_id": series.id,
                "device_id": device,
                "points": [
                    {"ts": ts.isoformat(), "temp_c": temp} for ts, temp in excursion
                ],
            }
        )
        if _excursion_tolerated(excursion, tmin, tmax, constraint.exception_rules):
            continue
        if abs(skew) > tolerance:
            # With an untrusted clock the readings prove sequence continuity
            # only — not that the excursion fell outside an allowed window.
            findings.append(
                {
                    "code": "clock_skew_exceeded",
                    "detail": (
                        f"device {device} clock skew {skew}s exceeds tolerance "
                        f"{tolerance}s; excursion timing cannot be proven"
                    ),
                }
            )
        else:
            findings.append(
                {
                    "code": "temperature_excursion",
                    "detail": (
                        f"{len(excursion)} reading(s) of {container.barcode} outside "
                        f"[{tmin}, {tmax}]°C beyond any exception rule"
                    ),
                }
            )
    if skew_exceeded:
        basis["continuity_only"] = True
        findings.append(
            {
                "code": "clock_skew_exceeded",
                "detail": (
                    f"device clock skew exceeds tolerance {tolerance}s; the "
                    "series proves sequence continuity only, not that "
                    "excursions stayed outside allowed windows"
                ),
            }
        )
    return findings, basis


def _handover_covering(
    db: Session, center_id: int, barcode: str, gap_start: datetime, gap_end: datetime
) -> SampleEvidence | None:
    """A signed handover whose departure/arrival sign-offs bracket the gap."""
    handovers = db.scalars(
        select(SampleEvidence).where(
            SampleEvidence.center_id == center_id,
            SampleEvidence.evidence_type == "handover",
            SampleEvidence.active.is_(True),
        )
    ).all()
    for handover in handovers:
        payload = handover.payload or {}
        if payload.get("container_barcode") != barcode:
            continue
        departure = payload.get("departure_signoff_at")
        arrival = payload.get("arrival_signoff_at")
        if not departure or not arrival:
            continue
        if parse_ts(departure) <= gap_start and parse_ts(arrival) >= gap_end:
            return handover
    return None


# ---------------------------------------------------------------------------
# Adjudication
# ---------------------------------------------------------------------------


def _active_constraint(db: Session, assay_purpose: str) -> ProtocolConstraint | None:
    return db.scalar(
        select(ProtocolConstraint)
        .where(
            ProtocolConstraint.assay_purpose == assay_purpose,
            ProtocolConstraint.status == "approved",
        )
        .order_by(ProtocolConstraint.version.desc())
    )


def adjudicate_report(db: Session, report_evidence: SampleEvidence, actor: str) -> UsabilityCertification:
    """(Re-)adjudicate one lab report and freeze a certification version."""
    payload = report_evidence.payload or {}
    report_uid = payload.get("report_uid") or report_evidence.evidence_uid
    barcode = payload.get("container_barcode")
    purpose = payload.get("assay_purpose") or "unspecified"
    at = report_evidence.occurred_at
    center_id = report_evidence.center_id

    container = get_container(db, center_id, barcode) if barcode else None
    constraint = _active_constraint(db, purpose)

    reasons: list[dict] = []
    segments: list[dict] = []
    edges: list[ContainerRelation] = []
    subject_refs: list[str] = []
    temperature_basis: list[dict] = []
    collection_mode = "event"

    if container is None:
        reasons.append(
            {
                "code": "container_not_registered",
                "detail": f"container {barcode!r} is not registered at this centre",
            }
        )
    else:
        collection_mode = container.collection_mode
        segments, edges = _build_chain(db, container, at)
        if any(edge.state == FROZEN for edge in edges):
            reasons.append(
                {
                    "code": "lineage_frozen",
                    "detail": "the container chain crosses a frozen, unresolved branch",
                }
            )
        for seg in segments:
            node = seg["container"]
            if node.state == "mislabeled":
                reasons.append(
                    {
                        "code": "container_mislabeled",
                        "detail": f"container {node.barcode} was judged mislabeled",
                    }
                )
            if not seg["incoming_all"]:
                # Root of the chain: must be a registered sample.
                if node.kind != "sample":
                    reasons.append(
                        {
                            "code": "lineage_root_missing",
                            "detail": (
                                f"chain root {node.barcode} is a {node.kind}, "
                                "not a registered sample"
                            ),
                        }
                    )
                elif node.subject_ref:
                    subject_refs.append(node.subject_ref)
        if not any(not seg["incoming_all"] for seg in segments):
            reasons.append(
                {
                    "code": "lineage_root_missing",
                    "detail": "the chain never reaches a registered sample",
                }
            )

        if constraint is not None:
            # Storage duration from each root sample to the assay.
            if constraint.max_duration_hours is not None:
                limit = float(constraint.max_duration_hours)
                for seg in segments:
                    if seg["incoming_all"] or seg["start"] is None:
                        continue
                    hours = (at - seg["start"]).total_seconds() / 3600.0
                    if hours > limit:
                        reasons.append(
                            {
                                "code": "storage_duration_exceeded",
                                "detail": (
                                    f"{hours:.1f}h from sampling to assay exceeds "
                                    f"{limit}h for {purpose}"
                                ),
                            }
                        )
            # Aliquot depth along the deepest path.
            if constraint.max_aliquot_count is not None:
                depth = _max_aliquot_depth(segments, container.id)
                if depth > constraint.max_aliquot_count:
                    reasons.append(
                        {
                            "code": "aliquot_count_exceeded",
                            "detail": (
                                f"{depth} aliquot steps exceed the allowed "
                                f"{constraint.max_aliquot_count}"
                            ),
                        }
                    )
            # Minimum remaining volume on every aliquot step.
            if constraint.min_remaining_volume_ul is not None:
                minimum = float(constraint.min_remaining_volume_ul)
                for edge in edges:
                    if edge.relation_type != "aliquot":
                        continue
                    if edge.parent_remaining_after_ul is None:
                        reasons.append(
                            {
                                "code": "remaining_volume_unproven",
                                "detail": (
                                    f"relation {edge.id} carries no remaining-volume "
                                    "evidence for its parent"
                                ),
                            }
                        )
                    elif float(edge.parent_remaining_after_ul) < minimum:
                        reasons.append(
                            {
                                "code": "min_remaining_volume_violated",
                                "detail": (
                                    f"parent remaining {float(edge.parent_remaining_after_ul)}µL "
                                    f"below the required {minimum}µL"
                                ),
                            }
                        )
            # Temperature evidence per chain segment.
            for seg in segments:
                findings, frag = _temperature_check(db, center_id, seg, constraint)
                reasons.extend(findings)
                temperature_basis.append(frag)

    if constraint is None:
        reasons.append(
            {
                "code": "no_approved_constraint",
                "detail": f"no approved constraint book entry for purpose {purpose!r}",
            }
        )

    # Interval-class evidence is capped for high-sensitivity endpoints.
    if (
        constraint is not None
        and constraint.requires_event_graph
        and collection_mode == "interval"
    ):
        reasons.append(
            {
                "code": "interval_mode_insufficient",
                "detail": (
                    "centre supplies inventory-interval evidence only; "
                    f"purpose {purpose!r} requires a full event graph"
                ),
            }
        )

    verdict = ANALYSIS_READY
    for reason in reasons:
        severity = REASON_SEVERITY[reason["code"]]
        if VERDICT_ORDER[severity] > VERDICT_ORDER[verdict]:
            verdict = severity
    codes = list(dict.fromkeys(r["code"] for r in reasons))

    usage_restrictions: list[str] = []
    if collection_mode == "interval":
        usage_restrictions.append("not_for_high_sensitivity_endpoint")
    if verdict == EXPLORATORY_ONLY:
        usage_restrictions.append("exploratory_use_only")
    elif verdict == LINEAGE_PENDING:
        usage_restrictions.extend(["awaiting_lineage_verification", "do_not_use_for_analysis"])
    elif verdict == UNUSABLE:
        usage_restrictions.append("do_not_use")

    frozen_basis = {
        "report": {
            "evidence_id": report_evidence.id,
            "evidence_uid": report_evidence.evidence_uid,
            "revision": report_evidence.revision,
            "report_uid": report_uid,
            "occurred_at": at.isoformat(),
            "received_at": report_evidence.received_at.isoformat(),
            "issued_by": report_evidence.issued_by,
        },
        "container_chain": [
            {
                "container_id": seg["container"].id,
                "barcode": seg["container"].barcode,
                "kind": seg["container"].kind,
                "state": seg["container"].state,
                "collection_mode": seg["container"].collection_mode,
                "custody": [
                    seg["start"].isoformat() if seg["start"] else None,
                    seg["end"].isoformat() if seg["end"] else None,
                ],
            }
            for seg in sorted(segments, key=lambda s: s["container"].id)
        ],
        "relations": [
            {
                "relation_id": edge.id,
                "type": edge.relation_type,
                "parent_container_id": edge.parent_container_id,
                "child_container_id": edge.child_container_id,
                "valid_from": edge.valid_from.isoformat(),
                "valid_to": edge.valid_to.isoformat() if edge.valid_to else None,
                "quantity_ul": float(edge.quantity_ul) if edge.quantity_ul is not None else None,
                "state": edge.state,
                "evidence_id": edge.evidence_id,
            }
            for edge in sorted(edges, key=lambda e: e.id)
        ],
        "temperature": temperature_basis,
        "constraint": (
            {
                "id": constraint.id,
                "assay_purpose": constraint.assay_purpose,
                "version": constraint.version,
                "protocol_revision_id": constraint.protocol_revision_id,
            }
            if constraint
            else None
        ),
        "collection_mode": collection_mode,
        "cert_class": "interval" if collection_mode == "interval" else "event",
        "usage_restrictions": usage_restrictions,
        "reasons": reasons,
    }
    basis_hash = hashlib.sha256(
        json.dumps(frozen_basis, sort_keys=True, default=str).encode()
    ).hexdigest()

    active = db.scalar(
        select(UsabilityCertification).where(
            UsabilityCertification.center_id == center_id,
            UsabilityCertification.report_uid == report_uid,
            UsabilityCertification.status == "active",
        )
    )
    if (
        active is not None
        and active.verdict == verdict
        and active.reason_codes == codes
        and active.basis_hash == basis_hash
    ):
        return active

    version = 1
    if active is not None:
        version = active.cert_version + 1
        active.status = "superseded"
    certification = UsabilityCertification(
        report_uid=report_uid,
        lab_report_evidence_id=report_evidence.id,
        center_id=center_id,
        container_id=container.id if container else None,
        subject_refs=sorted(set(subject_refs)),
        assay_purpose=purpose,
        verdict=verdict,
        reason_codes=codes,
        constraint_id=constraint.id if constraint else None,
        cert_version=version,
        status="active",
        frozen_basis=frozen_basis,
        basis_hash=basis_hash,
        chain_container_ids=sorted(seg["container"].id for seg in segments),
        usage_restrictions=usage_restrictions,
        created_by=actor,
    )
    db.add(certification)
    db.flush()
    _sync_references_after_adjudication(db, center_id, report_uid, certification)
    return certification


def readjudicate_for_containers(
    db: Session, container_ids: list[int], actor: str
) -> list[dict]:
    """Re-adjudicate every active certification whose chain touches the
    containers, e.g. after new temperature evidence, a freeze or a mislabel.

    Also picks up *orphan* reports: a lab report that arrived before its
    container was registered has an empty chain and would otherwise never be
    re-triggered when the registration finally lands.
    """
    if not container_ids:
        return []
    conditions = [
        UsabilityCertification.chain_container_ids.contains([cid]) for cid in container_ids
    ]
    certs = list(
        db.scalars(
            select(UsabilityCertification).where(
                UsabilityCertification.status == "active", or_(*conditions)
            )
        ).all()
    )
    containers = db.scalars(
        select(Container).where(Container.id.in_(container_ids))
    ).all()
    by_center: dict[int, set[str]] = {}
    for container in containers:
        by_center.setdefault(container.center_id, set()).add(container.barcode)
    for center_id, barcodes in by_center.items():
        orphans = db.scalars(
            select(UsabilityCertification).where(
                UsabilityCertification.status == "active",
                UsabilityCertification.center_id == center_id,
                UsabilityCertification.container_id.is_(None),
            )
        ).all()
        for cert in orphans:
            report = db.get(SampleEvidence, cert.lab_report_evidence_id)
            if report and (report.payload or {}).get("container_barcode") in barcodes:
                certs.append(cert)
    impacted = []
    seen = set()
    for cert in certs:
        if cert.id in seen:
            continue
        seen.add(cert.id)
        report = db.get(SampleEvidence, cert.lab_report_evidence_id)
        if report is None or not report.active:
            continue
        new = adjudicate_report(db, report, actor)
        impacted.append(
            {
                "report_uid": new.report_uid,
                "certification_id": new.id,
                "cert_version": new.cert_version,
                "verdict": new.verdict,
                "reason_codes": new.reason_codes,
            }
        )
    return impacted


# ---------------------------------------------------------------------------
# Analysis reference book
# ---------------------------------------------------------------------------


def select_references(
    db: Session, run_ref: CohortRunRef, certification_ids: list[int], actor: str
) -> list[AnalysisReference]:
    """Select certifications into an open cohort run.

    Only ``analysis_ready`` active certifications may be selected; the
    reference pins the exact certification version chosen now.
    """
    if run_ref.status != "open":
        raise AdjudicationError(f"cohort run {run_ref.id} is {run_ref.status}; references are closed")
    selected: list[AnalysisReference] = []
    for cert_id in dict.fromkeys(certification_ids):
        cert = db.get(UsabilityCertification, cert_id)
        if cert is None:
            raise AdjudicationError(f"certification {cert_id} not found")
        if cert.status != "active":
            raise AdjudicationError(
                f"certification {cert_id} is superseded; select the active version"
            )
        if cert.verdict != ANALYSIS_READY:
            raise AdjudicationError(
                f"certification {cert_id} verdict is {cert.verdict}; "
                "only analysis_ready certifications may enter a cohort run"
            )
        existing = db.scalar(
            select(AnalysisReference).where(
                AnalysisReference.cohort_run_ref_id == run_ref.id,
                AnalysisReference.certification_id == cert.id,
            )
        )
        if existing is not None:
            selected.append(existing)
            continue
        ref = AnalysisReference(
            cohort_run_ref_id=run_ref.id,
            certification_id=cert.id,
            center_id=cert.center_id,
            report_uid=cert.report_uid,
            verdict_at_selection=cert.verdict,
            status="active",
            selected_by=actor,
            history=[
                {
                    "event": "selected",
                    "actor": actor,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "certification_id": cert.id,
                    "cert_version": cert.cert_version,
                    "verdict": cert.verdict,
                }
            ],
        )
        db.add(ref)
        selected.append(ref)
    db.flush()
    return selected


def _sync_references_after_adjudication(
    db: Session, center_id: int, report_uid: str, active_cert: UsabilityCertification
) -> None:
    """Pause open-run references when the active verdict drops below
    ``analysis_ready``. Locked runs are historical and never touched."""
    if active_cert.verdict == ANALYSIS_READY:
        return
    refs = db.scalars(
        select(AnalysisReference)
        .join(CohortRunRef, CohortRunRef.id == AnalysisReference.cohort_run_ref_id)
        .where(
            AnalysisReference.report_uid == report_uid,
            AnalysisReference.status == "active",
            CohortRunRef.status == "open",
        )
    ).all()
    now = datetime.now(timezone.utc)
    for ref in refs:
        ref.status = "paused"
        ref.pause_reason = (
            f"active verdict for {report_uid} is now {active_cert.verdict} "
            f"(certification v{active_cert.cert_version})"
        )
        ref.paused_at = now
        ref.history = [
            *(ref.history or []),
            {
                "event": "paused",
                "at": now.isoformat(),
                "reason": ref.pause_reason,
            },
        ]
    db.flush()


def resume_reference(db: Session, run_ref: CohortRunRef, ref: AnalysisReference, actor: str) -> AnalysisReference:
    """Resume a paused reference once the report is analysis_ready again.

    The pin moves to the current active certification version; the change is
    appended to the reference history. Locked runs cannot be modified.
    """
    if run_ref.status != "open":
        raise AdjudicationError(f"cohort run {run_ref.id} is locked")
    if ref.status != "paused":
        raise AdjudicationError(f"reference {ref.id} is {ref.status}, not paused")
    current = db.scalar(
        select(UsabilityCertification).where(
            UsabilityCertification.center_id == ref.center_id,
            UsabilityCertification.report_uid == ref.report_uid,
            UsabilityCertification.status == "active",
        )
    )
    if current is None or current.verdict != ANALYSIS_READY:
        raise AdjudicationError(
            f"report {ref.report_uid} is not analysis_ready; cannot resume"
        )
    now = datetime.now(timezone.utc)
    ref.certification_id = current.id
    ref.verdict_at_selection = current.verdict
    ref.status = "active"
    ref.pause_reason = None
    ref.paused_at = None
    ref.history = [
        *(ref.history or []),
        {
            "event": "resumed",
            "actor": actor,
            "at": now.isoformat(),
            "certification_id": current.id,
            "cert_version": current.cert_version,
        },
    ]
    db.flush()
    return ref
