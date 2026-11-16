"""Identity candidates and privacy controls.

Ordinary centre staff only ever see the randomly assigned study id. Only a
privacy admin can see hospital-local references, and only a privacy admin can
confirm or reject an identity merge. Nothing is merged automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Center,
    IdentityCandidate,
    NormalizedRecord,
    SourceRecord,
    Subject,
    User,
)
from ..schemas import IdentityDecisionIn
from ..security import audit, require_roles

router = APIRouter(tags=["privacy"])


@router.get("/identity-candidates")
def list_candidates(
    status: str = "pending",
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("privacy_admin")),
):
    stmt = select(IdentityCandidate).order_by(IdentityCandidate.id)
    if status != "all":
        stmt = stmt.where(IdentityCandidate.status == status)
    rows = db.scalars(stmt).all()
    out = []
    for cand in rows:
        a = db.get(Subject, cand.subject_a_id)
        b = db.get(Subject, cand.subject_b_id)
        out.append(
            {
                "id": cand.id,
                "status": cand.status,
                "evidence": cand.evidence,
                "subject_a": {"study_id": a.study_id, "center_id": a.center_id},
                "subject_b": {"study_id": b.study_id, "center_id": b.center_id},
                "decided_by": cand.decided_by,
            }
        )
    return out


@router.post("/identity-candidates/{candidate_id}/decide")
def decide_candidate(
    candidate_id: int,
    body: IdentityDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("privacy_admin")),
):
    cand = db.get(IdentityCandidate, candidate_id)
    if not cand:
        raise HTTPException(404, "candidate not found")
    if cand.status != "pending":
        raise HTTPException(409, f"candidate already {cand.status}")

    a = db.get(Subject, cand.subject_a_id)
    b = db.get(Subject, cand.subject_b_id)
    cand.status = body.status
    cand.decided_by = user.username
    cand.decided_at = datetime.now(timezone.utc)
    if body.note:
        cand.evidence = {**(cand.evidence or {}), "note": body.note}

    if body.status == "confirmed":
        # Keep the lower study id as the survivor so the merge is deterministic.
        survivor, absorbed = (a, b) if a.id < b.id else (b, a)
        absorbed.merged_into_subject_id = survivor.id
        # Re-point normalized rows; source records themselves are untouched.
        for rec in db.scalars(
            select(NormalizedRecord).where(NormalizedRecord.subject_id == absorbed.id)
        ).all():
            rec.subject_id = survivor.id
        # Any further pending candidates involving the absorbed subject are moot.
        others = db.scalars(
            select(IdentityCandidate).where(
                IdentityCandidate.status == "pending",
                (
                    (IdentityCandidate.subject_a_id == absorbed.id)
                    | (IdentityCandidate.subject_b_id == absorbed.id)
                ),
            )
        ).all()
        for other in others:
            other.status = "rejected"
            other.evidence = {**(other.evidence or {}), "auto_rejected": "subject merged"}
            other.decided_by = user.username
            other.decided_at = datetime.now(timezone.utc)

    audit(
        db,
        user.username,
        "identity.decide",
        "identity_candidate",
        cand.id,
        {"status": body.status, "study_ids": [a.study_id, b.study_id]},
    )
    db.commit()
    return {"id": cand.id, "status": cand.status}


@router.get("/subjects/{study_id}/trace")
def trace_subject(
    study_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("privacy_admin", "rule_reviewer", "analysis_lead")),
):
    """Full traceability from a study id back to hospital source rows."""
    subject = db.scalar(select(Subject).where(Subject.study_id == study_id))
    if not subject:
        raise HTTPException(404, "subject not found")
    include_source = user.role == "privacy_admin"
    rows = db.execute(
        select(NormalizedRecord, SourceRecord, Center)
        .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
        .join(Center, Center.id == SourceRecord.center_id)
        .where(NormalizedRecord.subject_id == subject.id)
        .order_by(SourceRecord.id)
    ).all()
    return {
        "study_id": subject.study_id,
        "center": db.get(Center, subject.center_id).code,
        "merged_into": (
            db.get(Subject, subject.merged_into_subject_id).study_id
            if subject.merged_into_subject_id
            else None
        ),
        "records": [
            {
                "source_record_id": s.id,
                "source_row_no": s.source_row_no,
                "source_uid": s.source_uid,
                "revision": s.revision,
                "active": s.active,
                "record_type": s.record_type,
                "normalized_status": n.status,
                "normalized_issues": n.issues,
                "study_code": n.study_code,
                "original_value": n.original_value,
                "original_unit": n.original_unit,
                "study_value": float(n.converted_value) if n.converted_value is not None else None,
                "study_unit": n.study_unit,
                "conversion": n.conversion,
                "effective_date": n.effective_date.isoformat() if n.effective_date else None,
                # Hospital patient reference is personally identifying, so it is
                # only returned to the privacy administrator.
                "patient_ref": (s.payload or {}).get("patient_ref") if include_source else None,
            }
            for n, s, _ in rows
        ],
    }
