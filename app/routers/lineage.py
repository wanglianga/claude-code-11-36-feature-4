"""Sample lineage integrity and analysis usability certification API.

Mounted under ``/lineage``. The sampling evidence store, container graph,
constraint book, usability certifications and the analysis reference book
live here; the cohort screening service is untouched and consumes
certifications only through the reference book.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    AnalysisReference,
    Center,
    CohortRunRef,
    Container,
    ContainerRelation,
    GraphIssue,
    ProtocolConstraint,
    ProtocolRevision,
    SampleEvidence,
    ScreeningRun,
    UsabilityCertification,
    User,
)
from ..schemas import (
    ConstraintIn,
    CohortRunRefIn,
    EvidenceIn,
    MislabelIn,
    PoolSplitIn,
    ReadjudicateIn,
    ResolveIssueIn,
    SelectReferencesIn,
)
from ..security import audit, require_roles
from ..services import adjudication, lineage
from ..services.adjudication import AdjudicationError
from ..services.lineage import LineageError

router = APIRouter(prefix="/lineage", tags=["lineage"])


def _center_for(user: User, db: Session, center_code: str) -> Center:
    center = db.scalar(select(Center).where(Center.code == center_code))
    if not center:
        raise HTTPException(404, "center not found")
    if user.role == "center_uploader" and user.center_id != center.id:
        raise HTTPException(403, "center uploaders may only act for their own centre")
    return center


def _check_container_access(user: User, container: Container) -> None:
    if user.role == "center_uploader" and user.center_id != container.center_id:
        raise HTTPException(403, "not your centre")


def _evidence_out(e: SampleEvidence) -> dict:
    return {
        "id": e.id,
        "center_id": e.center_id,
        "evidence_uid": e.evidence_uid,
        "revision": e.revision,
        "evidence_type": e.evidence_type,
        "payload": e.payload,
        "occurred_at": e.occurred_at.isoformat(),
        "received_at": e.received_at.isoformat(),
        "issued_by": e.issued_by,
        "active": e.active,
        "supersedes_id": e.supersedes_id,
    }


def _container_out(c: Container) -> dict:
    return {
        "id": c.id,
        "center_id": c.center_id,
        "barcode": c.barcode,
        "kind": c.kind,
        "subject_ref": c.subject_ref,
        "volume_ul": float(c.volume_ul) if c.volume_ul is not None else None,
        "state": c.state,
        "collection_mode": c.collection_mode,
        "registered_evidence_id": c.registered_evidence_id,
    }


def _relation_out(r: ContainerRelation) -> dict:
    return {
        "id": r.id,
        "parent_container_id": r.parent_container_id,
        "child_container_id": r.child_container_id,
        "relation_type": r.relation_type,
        "quantity_ul": float(r.quantity_ul) if r.quantity_ul is not None else None,
        "parent_remaining_after_ul": (
            float(r.parent_remaining_after_ul) if r.parent_remaining_after_ul is not None else None
        ),
        "valid_from": r.valid_from.isoformat(),
        "valid_to": r.valid_to.isoformat() if r.valid_to else None,
        "evidence_id": r.evidence_id,
        "state": r.state,
    }


def _certification_out(c: UsabilityCertification, with_basis: bool = False) -> dict:
    out = {
        "id": c.id,
        "center_id": c.center_id,
        "report_uid": c.report_uid,
        "lab_report_evidence_id": c.lab_report_evidence_id,
        "container_id": c.container_id,
        "subject_refs": c.subject_refs,
        "assay_purpose": c.assay_purpose,
        "verdict": c.verdict,
        "reason_codes": c.reason_codes,
        "constraint_id": c.constraint_id,
        "cert_version": c.cert_version,
        "status": c.status,
        "usage_restrictions": c.usage_restrictions,
        "basis_hash": c.basis_hash,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat(),
    }
    if with_basis:
        out["frozen_basis"] = c.frozen_basis
    return out


# ---------------------------------------------------------------------------
# Evidence store
# ---------------------------------------------------------------------------


@router.post("/evidence", status_code=201)
def submit_evidence(
    body: EvidenceIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("center_uploader", "sample_curator")),
):
    center = _center_for(user, db, body.center_code)
    try:
        evidence, duplicate = lineage.submit_evidence(
            db,
            center,
            evidence_type=body.evidence_type,
            evidence_uid=body.evidence_uid,
            occurred_at=body.occurred_at,
            issued_by=body.issued_by,
            payload=body.payload,
            actor=user.username,
        )
    except LineageError as exc:
        raise HTTPException(422, str(exc))
    audit(
        db,
        user.username,
        "lineage.evidence.submit",
        "sample_evidence",
        evidence.id,
        {
            "center": center.code,
            "type": evidence.evidence_type,
            "uid": evidence.evidence_uid,
            "revision": evidence.revision,
            "duplicate": duplicate,
        },
    )
    db.commit()
    return {**_evidence_out(evidence), "duplicate": duplicate}


@router.get("/evidence")
def list_evidence(
    center_code: str | None = None,
    evidence_type: str | None = None,
    container_barcode: str | None = None,
    active_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    stmt = select(SampleEvidence).order_by(SampleEvidence.id)
    if user.role == "center_uploader":
        stmt = stmt.where(SampleEvidence.center_id == user.center_id)
    elif center_code:
        center = db.scalar(select(Center).where(Center.code == center_code))
        if not center:
            raise HTTPException(404, "center not found")
        stmt = stmt.where(SampleEvidence.center_id == center.id)
    if evidence_type:
        stmt = stmt.where(SampleEvidence.evidence_type == evidence_type)
    if active_only:
        stmt = stmt.where(SampleEvidence.active.is_(True))
    rows = db.scalars(stmt).all()
    if container_barcode:
        rows = [
            e
            for e in rows
            if container_barcode
            in {
                (e.payload or {}).get("container_barcode"),
                (e.payload or {}).get("sample_barcode"),
                (e.payload or {}).get("parent_barcode"),
                (e.payload or {}).get("pool_barcode"),
                (e.payload or {}).get("barcode"),
            }
            or container_barcode
            in [c.get("barcode") for c in (e.payload or {}).get("children", [])]
            or container_barcode
            in [s.get("barcode") for s in (e.payload or {}).get("sources", [])]
        ]
    return [_evidence_out(e) for e in rows]


@router.get("/evidence/{evidence_id}")
def get_evidence(
    evidence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    evidence = db.get(SampleEvidence, evidence_id)
    if not evidence:
        raise HTTPException(404, "evidence not found")
    if user.role == "center_uploader" and user.center_id != evidence.center_id:
        raise HTTPException(403, "not your centre")
    revisions = db.scalars(
        select(SampleEvidence)
        .where(
            SampleEvidence.center_id == evidence.center_id,
            SampleEvidence.evidence_uid == evidence.evidence_uid,
        )
        .order_by(SampleEvidence.revision)
    ).all()
    return {
        **_evidence_out(evidence),
        "revision_chain": [
            {"id": r.id, "revision": r.revision, "active": r.active} for r in revisions
        ],
    }


# ---------------------------------------------------------------------------
# Containers and the relationship graph
# ---------------------------------------------------------------------------


@router.get("/containers")
def list_containers(
    center_code: str | None = None,
    kind: str | None = None,
    state: str | None = None,
    subject_ref: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    stmt = select(Container).order_by(Container.id)
    if user.role == "center_uploader":
        stmt = stmt.where(Container.center_id == user.center_id)
    elif center_code:
        center = db.scalar(select(Center).where(Center.code == center_code))
        if not center:
            raise HTTPException(404, "center not found")
        stmt = stmt.where(Container.center_id == center.id)
    if kind:
        stmt = stmt.where(Container.kind == kind)
    if state:
        stmt = stmt.where(Container.state == state)
    if subject_ref:
        stmt = stmt.where(Container.subject_ref == subject_ref)
    return [_container_out(c) for c in db.scalars(stmt).all()]


@router.get("/containers/{container_id}")
def get_container(
    container_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    container = db.get(Container, container_id)
    if not container:
        raise HTTPException(404, "container not found")
    _check_container_access(user, container)
    relations = db.scalars(
        select(ContainerRelation)
        .where(
            (ContainerRelation.parent_container_id == container.id)
            | (ContainerRelation.child_container_id == container.id)
        )
        .order_by(ContainerRelation.id)
    ).all()
    return {
        **_container_out(container),
        "relations": [_relation_out(r) for r in relations],
    }


@router.get("/containers/{container_id}/lineage")
def container_lineage(
    container_id: int,
    at: str | None = Query(default=None, description="ISO-8601 moment; default now"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    """Which sample(s) did this container belong to at time ``at``?"""
    container = db.get(Container, container_id)
    if not container:
        raise HTTPException(404, "container not found")
    _check_container_access(user, container)
    moment = lineage.parse_ts(at) if at else datetime.now(timezone.utc)
    nodes, edges = lineage.lineage_at(db, container, moment)
    roots = [n for n in nodes if n.kind == "sample"]
    return {
        "container_id": container.id,
        "barcode": container.barcode,
        "at": moment.isoformat(),
        "root_samples": [
            {"container_id": r.id, "barcode": r.barcode, "subject_ref": r.subject_ref}
            for r in roots
        ],
        "containers": [_container_out(n) for n in nodes],
        "relations": [_relation_out(e) for e in edges],
    }


@router.post("/containers/{container_id}/mislabel")
def judge_mislabel(
    container_id: int,
    body: MislabelIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("sample_curator")),
):
    """Judge a container mislabelled; downstream certifications are
    re-adjudicated and open cohort-run references are paused, while locked
    runs keep their original certification."""
    container = db.get(Container, container_id)
    if not container:
        raise HTTPException(404, "container not found")
    try:
        result = lineage.judge_mislabel(db, container, body.reason, user.username)
    except LineageError as exc:
        raise HTTPException(409, str(exc))
    audit(
        db,
        user.username,
        "lineage.container.mislabel",
        "container",
        container.id,
        {"reason": body.reason, "impacted": result["impacted"]},
    )
    db.commit()
    return result


@router.post("/containers/{container_id}/split")
def split_pool(
    container_id: int,
    body: PoolSplitIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("sample_curator")),
):
    """Attempt to split a pool back into sources. Without measured remaining
    volumes and aliquot-quality evidence the pool stays irreversible."""
    pool = db.get(Container, container_id)
    if not pool:
        raise HTTPException(404, "container not found")
    center = db.get(Center, pool.center_id)
    try:
        relations = lineage.split_pool(
            db, center, pool, body.children, body.volume_evidence, user.username
        )
    except LineageError as exc:
        db.commit()  # persist the recorded rejection issue
        raise HTTPException(409, str(exc))
    audit(
        db,
        user.username,
        "lineage.pool.split",
        "container",
        pool.id,
        {"children": body.children, "relation_ids": [r.id for r in relations]},
    )
    db.commit()
    return {"pool_id": pool.id, "relations": [_relation_out(r) for r in relations]}


# ---------------------------------------------------------------------------
# Graph issues
# ---------------------------------------------------------------------------


def _issue_out(issue: GraphIssue) -> dict:
    return {
        "id": issue.id,
        "issue_type": issue.issue_type,
        "status": issue.status,
        "container_ids": issue.container_ids,
        "relation_ids": issue.relation_ids,
        "evidence_ids": issue.evidence_ids,
        "detail": issue.detail,
        "created_by": issue.created_by,
        "created_at": issue.created_at.isoformat(),
        "resolved_by": issue.resolved_by,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "resolution": issue.resolution,
    }


@router.get("/graph-issues")
def list_graph_issues(
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    stmt = select(GraphIssue).order_by(GraphIssue.id)
    if status:
        stmt = stmt.where(GraphIssue.status == status)
    return [_issue_out(i) for i in db.scalars(stmt).all()]


@router.post("/graph-issues/{issue_id}/resolve")
def resolve_graph_issue(
    issue_id: int,
    body: ResolveIssueIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("sample_curator")),
):
    """Resolve a frozen branch: confirm the relations to keep, reject the
    rest. The confirmed chain is never silently overwritten by receipt order."""
    issue = db.get(GraphIssue, issue_id)
    if not issue:
        raise HTTPException(404, "issue not found")
    try:
        lineage.resolve_issue(db, issue, body.keep_relation_ids, body.note, user.username)
    except LineageError as exc:
        raise HTTPException(409, str(exc))
    audit(
        db,
        user.username,
        "lineage.issue.resolve",
        "graph_issue",
        issue.id,
        {"keep_relation_ids": body.keep_relation_ids, "note": body.note},
    )
    db.commit()
    return _issue_out(issue)


# ---------------------------------------------------------------------------
# Constraint book
# ---------------------------------------------------------------------------


def _constraint_out(c: ProtocolConstraint) -> dict:
    return {
        "id": c.id,
        "assay_purpose": c.assay_purpose,
        "version": c.version,
        "status": c.status,
        "protocol_revision_id": c.protocol_revision_id,
        "temp_min_c": float(c.temp_min_c) if c.temp_min_c is not None else None,
        "temp_max_c": float(c.temp_max_c) if c.temp_max_c is not None else None,
        "max_duration_hours": (
            float(c.max_duration_hours) if c.max_duration_hours is not None else None
        ),
        "max_aliquot_count": c.max_aliquot_count,
        "min_remaining_volume_ul": (
            float(c.min_remaining_volume_ul) if c.min_remaining_volume_ul is not None else None
        ),
        "clock_skew_tolerance_seconds": c.clock_skew_tolerance_seconds,
        "requires_event_graph": c.requires_event_graph,
        "exception_rules": c.exception_rules,
        "created_by": c.created_by,
        "approved_by": c.approved_by,
    }


@router.post("/constraints", status_code=201)
def create_constraint(
    body: ConstraintIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    if body.protocol_revision_id is not None and not db.get(
        ProtocolRevision, body.protocol_revision_id
    ):
        raise HTTPException(404, "protocol revision not found")
    prior = db.scalar(
        select(ProtocolConstraint)
        .where(ProtocolConstraint.assay_purpose == body.assay_purpose)
        .order_by(ProtocolConstraint.version.desc())
    )
    constraint = ProtocolConstraint(
        assay_purpose=body.assay_purpose,
        version=(prior.version + 1) if prior else 1,
        protocol_revision_id=body.protocol_revision_id,
        temp_min_c=body.temp_min_c,
        temp_max_c=body.temp_max_c,
        max_duration_hours=body.max_duration_hours,
        max_aliquot_count=body.max_aliquot_count,
        min_remaining_volume_ul=body.min_remaining_volume_ul,
        clock_skew_tolerance_seconds=body.clock_skew_tolerance_seconds,
        requires_event_graph=body.requires_event_graph,
        exception_rules=body.exception_rules,
        created_by=user.username,
    )
    db.add(constraint)
    audit(
        db,
        user.username,
        "lineage.constraint.create",
        "protocol_constraint",
        f"{body.assay_purpose}:v{constraint.version}",
        {"temp": [body.temp_min_c, body.temp_max_c]},
    )
    db.commit()
    return _constraint_out(constraint)


@router.get("/constraints")
def list_constraints(
    assay_purpose: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    stmt = select(ProtocolConstraint).order_by(ProtocolConstraint.id)
    if assay_purpose:
        stmt = stmt.where(ProtocolConstraint.assay_purpose == assay_purpose)
    return [_constraint_out(c) for c in db.scalars(stmt).all()]


@router.post("/constraints/{constraint_id}/approve")
def approve_constraint(
    constraint_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    constraint = db.get(ProtocolConstraint, constraint_id)
    if not constraint:
        raise HTTPException(404, "constraint not found")
    if constraint.status != "draft":
        raise HTTPException(409, f"constraint is {constraint.status}")
    # A new approved version retires the previously approved one. Existing
    # certifications keep the constraint version frozen in their basis.
    current = db.scalars(
        select(ProtocolConstraint).where(
            ProtocolConstraint.assay_purpose == constraint.assay_purpose,
            ProtocolConstraint.status == "approved",
        )
    ).all()
    for old in current:
        old.status = "retired"
    constraint.status = "approved"
    constraint.approved_by = user.username
    constraint.approved_at = datetime.now(timezone.utc)
    audit(
        db,
        user.username,
        "lineage.constraint.approve",
        "protocol_constraint",
        constraint.id,
        {"assay_purpose": constraint.assay_purpose, "version": constraint.version},
    )
    db.commit()
    return _constraint_out(constraint)


# ---------------------------------------------------------------------------
# Usability certifications
# ---------------------------------------------------------------------------


@router.get("/certifications")
def list_certifications(
    verdict: str | None = None,
    status: str | None = None,
    report_uid: str | None = None,
    subject_ref: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    stmt = select(UsabilityCertification).order_by(UsabilityCertification.id)
    if verdict:
        stmt = stmt.where(UsabilityCertification.verdict == verdict)
    if status:
        stmt = stmt.where(UsabilityCertification.status == status)
    if report_uid:
        stmt = stmt.where(UsabilityCertification.report_uid == report_uid)
    if subject_ref:
        stmt = stmt.where(UsabilityCertification.subject_refs.contains([subject_ref]))
    return [_certification_out(c) for c in db.scalars(stmt).all()]


@router.get("/certifications/{certification_id}")
def get_certification(
    certification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    """Full traceability: original evidence, container relations, temperature
    intervals, constraint version, usage restrictions and reference impact."""
    cert = db.get(UsabilityCertification, certification_id)
    if not cert:
        raise HTTPException(404, "certification not found")
    evidence_ids = {cert.lab_report_evidence_id}
    basis = cert.frozen_basis or {}
    for frag in basis.get("temperature", []):
        evidence_ids.update(s.get("evidence_id") for s in frag.get("series", []) if s.get("evidence_id"))
        evidence_ids.update(
            p.get("evidence_id") for p in frag.get("inventory_points", []) if p.get("evidence_id")
        )
        evidence_ids.update(
            g.get("bounded_by_evidence_id") for g in frag.get("gaps", []) if g.get("bounded_by_evidence_id")
        )
    evidence_ids.update(
        r.get("evidence_id") for r in basis.get("relations", []) if r.get("evidence_id")
    )
    # The sampling registration hangs off the chain containers, not a relation.
    chain_container_ids = [
        c.get("container_id") for c in basis.get("container_chain", []) if c.get("container_id")
    ]
    if chain_container_ids:
        evidence_ids.update(
            cid
            for cid in db.scalars(
                select(Container.registered_evidence_id).where(
                    Container.id.in_(chain_container_ids),
                    Container.registered_evidence_id.is_not(None),
                )
            ).all()
        )
    evidence_ids.discard(None)
    evidence_rows = {
        e.id: e
        for e in db.scalars(
            select(SampleEvidence).where(SampleEvidence.id.in_(sorted(evidence_ids)))
        ).all()
    } if evidence_ids else {}
    references = db.scalars(
        select(AnalysisReference).where(AnalysisReference.certification_id == cert.id)
    ).all()
    other_versions = db.scalars(
        select(UsabilityCertification)
        .where(
            UsabilityCertification.center_id == cert.center_id,
            UsabilityCertification.report_uid == cert.report_uid,
        )
        .order_by(UsabilityCertification.cert_version)
    ).all()
    return {
        **_certification_out(cert, with_basis=True),
        "evidence": [_evidence_out(evidence_rows[eid]) for eid in sorted(evidence_rows)],
        "reference_impact": [
            {
                "reference_id": r.id,
                "cohort_run_ref_id": r.cohort_run_ref_id,
                "status": r.status,
                "selected_by": r.selected_by,
                "selected_at": r.selected_at.isoformat(),
            }
            for r in references
        ],
        "versions": [
            {"id": v.id, "cert_version": v.cert_version, "verdict": v.verdict, "status": v.status}
            for v in other_versions
        ],
    }


@router.post("/lab-reports/{evidence_id}/adjudicate")
def adjudicate_lab_report(
    evidence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("sample_curator")),
):
    evidence = db.get(SampleEvidence, evidence_id)
    if not evidence or evidence.evidence_type != "lab_report":
        raise HTTPException(404, "lab report evidence not found")
    if not evidence.active:
        raise HTTPException(409, "only the active revision of a report can be adjudicated")
    cert = adjudication.adjudicate_report(db, evidence, user.username)
    audit(
        db,
        user.username,
        "lineage.certification.adjudicate",
        "usability_certification",
        cert.id,
        {"report_uid": cert.report_uid, "verdict": cert.verdict},
    )
    db.commit()
    return _certification_out(cert, with_basis=True)


@router.post("/certifications/readjudicate")
def readjudicate(
    body: ReadjudicateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("sample_curator")),
):
    """Bulk re-adjudication, e.g. after a constraint-book rule change. Old
    certification versions are preserved; cohort runs keep their pinned basis."""
    stmt = select(UsabilityCertification).where(UsabilityCertification.status == "active")
    if body.assay_purpose:
        stmt = stmt.where(UsabilityCertification.assay_purpose == body.assay_purpose)
    if body.report_uid:
        stmt = stmt.where(UsabilityCertification.report_uid == body.report_uid)
    certs = db.scalars(stmt).all()
    results = []
    for cert in certs:
        report = db.get(SampleEvidence, cert.lab_report_evidence_id)
        if report is None or not report.active:
            continue
        new = adjudication.adjudicate_report(db, report, user.username)
        results.append(
            {
                "report_uid": new.report_uid,
                "certification_id": new.id,
                "cert_version": new.cert_version,
                "verdict": new.verdict,
                "reason_codes": new.reason_codes,
            }
        )
    audit(
        db,
        user.username,
        "lineage.certification.readjudicate",
        "usability_certification",
        None,
        {"count": len(results), "assay_purpose": body.assay_purpose},
    )
    db.commit()
    return {"readjudicated": results}


# ---------------------------------------------------------------------------
# Analysis reference book
# ---------------------------------------------------------------------------


def _run_ref_out(run: CohortRunRef, db: Session) -> dict:
    refs = db.scalars(
        select(AnalysisReference)
        .where(AnalysisReference.cohort_run_ref_id == run.id)
        .order_by(AnalysisReference.id)
    ).all()
    return {
        "id": run.id,
        "label": run.label,
        "screening_run_id": run.screening_run_id,
        "status": run.status,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat(),
        "locked_by": run.locked_by,
        "locked_at": run.locked_at.isoformat() if run.locked_at else None,
        "references": [
            {
                "id": r.id,
                "certification_id": r.certification_id,
                "report_uid": r.report_uid,
                "verdict_at_selection": r.verdict_at_selection,
                "status": r.status,
                "pause_reason": r.pause_reason,
                "selected_by": r.selected_by,
                "selected_at": r.selected_at.isoformat(),
                "history": r.history,
            }
            for r in refs
        ],
    }


@router.post("/cohort-runs", status_code=201)
def create_cohort_run_ref(
    body: CohortRunRefIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    if db.scalar(select(CohortRunRef).where(CohortRunRef.label == body.label)):
        raise HTTPException(409, "label already exists")
    if body.screening_run_id is not None and not db.get(ScreeningRun, body.screening_run_id):
        raise HTTPException(404, "screening run not found")
    run_ref = CohortRunRef(
        label=body.label, screening_run_id=body.screening_run_id, created_by=user.username
    )
    db.add(run_ref)
    audit(db, user.username, "lineage.cohort_run.create", "cohort_run_ref", body.label)
    db.commit()
    return _run_ref_out(run_ref, db)


@router.get("/cohort-runs")
def list_cohort_run_refs(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    runs = db.scalars(select(CohortRunRef).order_by(CohortRunRef.id)).all()
    return [_run_ref_out(r, db) for r in runs]


@router.get("/cohort-runs/{run_ref_id}")
def get_cohort_run_ref(
    run_ref_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    run_ref = db.get(CohortRunRef, run_ref_id)
    if not run_ref:
        raise HTTPException(404, "cohort run ref not found")
    return _run_ref_out(run_ref, db)


@router.post("/cohort-runs/{run_ref_id}/references", status_code=201)
def select_references(
    run_ref_id: int,
    body: SelectReferencesIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    """Select certifications into an open cohort run. Only ``analysis_ready``
    active certifications are accepted."""
    run_ref = db.get(CohortRunRef, run_ref_id)
    if not run_ref:
        raise HTTPException(404, "cohort run ref not found")
    try:
        adjudication.select_references(db, run_ref, body.certification_ids, user.username)
    except AdjudicationError as exc:
        raise HTTPException(409, str(exc))
    audit(
        db,
        user.username,
        "lineage.cohort_run.select",
        "cohort_run_ref",
        run_ref.id,
        {"certification_ids": body.certification_ids},
    )
    db.commit()
    return _run_ref_out(run_ref, db)


@router.post("/cohort-runs/{run_ref_id}/references/{reference_id}/resume")
def resume_reference(
    run_ref_id: int,
    reference_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    run_ref = db.get(CohortRunRef, run_ref_id)
    if not run_ref:
        raise HTTPException(404, "cohort run ref not found")
    ref = db.get(AnalysisReference, reference_id)
    if not ref or ref.cohort_run_ref_id != run_ref.id:
        raise HTTPException(404, "reference not found")
    try:
        adjudication.resume_reference(db, run_ref, ref, user.username)
    except AdjudicationError as exc:
        raise HTTPException(409, str(exc))
    audit(
        db,
        user.username,
        "lineage.cohort_run.resume_reference",
        "analysis_reference",
        ref.id,
    )
    db.commit()
    return _run_ref_out(run_ref, db)


@router.post("/cohort-runs/{run_ref_id}/lock")
def lock_cohort_run_ref(
    run_ref_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    """Lock the run's references. Locked runs are historical: later
    re-adjudications and mislabel judgements never modify them."""
    run_ref = db.get(CohortRunRef, run_ref_id)
    if not run_ref:
        raise HTTPException(404, "cohort run ref not found")
    if run_ref.status != "open":
        raise HTTPException(409, f"cohort run is {run_ref.status}")
    run_ref.status = "locked"
    run_ref.locked_by = user.username
    run_ref.locked_at = datetime.now(timezone.utc)
    audit(db, user.username, "lineage.cohort_run.lock", "cohort_run_ref", run_ref.id)
    db.commit()
    return _run_ref_out(run_ref, db)


# ---------------------------------------------------------------------------
# Subject-level usability view
# ---------------------------------------------------------------------------


@router.get("/subjects/{subject_ref}/usability")
def subject_usability(
    subject_ref: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    """Every lab report certification for one subject. A downgrade lands on
    the affected assay only — the subject as a whole is never excluded."""
    certs = db.scalars(
        select(UsabilityCertification)
        .where(
            UsabilityCertification.subject_refs.contains([subject_ref]),
            UsabilityCertification.status == "active",
        )
        .order_by(UsabilityCertification.id)
    ).all()
    return {
        "subject_ref": subject_ref,
        "certifications": [_certification_out(c) for c in certs],
    }
