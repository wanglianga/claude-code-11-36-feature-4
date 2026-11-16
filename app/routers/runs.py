"""Cutoff batches, screening runs and equivalence verification."""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Center,
    CutoffBatch,
    ProtocolRevision,
    Reconciliation,
    ScreeningDecision,
    ScreeningRun,
    Subject,
    Upload,
    User,
)
from ..schemas import CutoffIn, RunIn
from ..security import audit, require_roles
from ..services.screening import InjectedCrash, execute_run, rules_hash

router = APIRouter(tags=["screening"])

STRATEGY_LABELS = {
    "full": "全量重算：补齐后重新扫描冻结范围内所有中心",
    "incremental": "增量重算：只重算受影响的病例，其余复用基线结果",
}


@router.post("/cutoffs", status_code=201)
def create_cutoff(
    body: CutoffIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer", "analysis_lead")),
):
    if not body.upload_ids:
        raise HTTPException(422, "a cutoff batch needs at least one upload")
    uploads = db.scalars(select(Upload).where(Upload.id.in_(body.upload_ids))).all()
    if len(uploads) != len(set(body.upload_ids)):
        raise HTTPException(422, "unknown upload id in cutoff")
    for u in uploads:
        if u.status != "completed":
            raise HTTPException(
                409, f"upload {u.id} is {u.status}; finish it before freezing a cutoff batch"
            )
    if db.scalar(select(CutoffBatch).where(CutoffBatch.label == body.label)):
        raise HTTPException(409, "cutoff label already exists")
    batch = CutoffBatch(
        label=body.label,
        upload_ids=sorted({u.id for u in uploads}),
        created_by=user.username,
    )
    db.add(batch)
    audit(db, user.username, "cutoff.create", "cutoff", body.label, {"uploads": batch.upload_ids})
    db.commit()
    return {"id": batch.id, "label": batch.label, "upload_ids": batch.upload_ids}


@router.get("/cutoffs")
def list_cutoffs(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    rows = db.scalars(select(CutoffBatch).order_by(CutoffBatch.id)).all()
    return [
        {
            "id": b.id,
            "label": b.label,
            "upload_ids": b.upload_ids,
            "created_by": b.created_by,
            "created_at": b.created_at.isoformat(),
        }
        for b in rows
    ]


def _run_summary(db: Session, run: ScreeningRun) -> dict:
    revision = db.get(ProtocolRevision, run.protocol_revision_id)
    batch = db.get(CutoffBatch, run.cutoff_batch_id)
    return {
        "id": run.id,
        "strategy": run.strategy,
        "strategy_label": STRATEGY_LABELS.get(run.strategy),
        "status": run.status,
        "protocol": {"revision_id": revision.id, "version": revision.version},
        "rules_hash": rules_hash(revision.rules or {}),
        "cutoff": {"id": batch.id, "label": batch.label},
        "base_run_id": run.base_run_id,
        "verification_of_run_id": run.verification_of_run_id,
        "equivalence_result": run.equivalence_result,
        "counts": run.counts,
        "reused_count": run.reused_count,
        "recomputed_count": run.recomputed_count,
        "affected_subject_count": len(run.affected_subjects or []),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.post("/screening-runs", status_code=201)
def create_run(
    body: RunIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer", "analysis_lead")),
):
    revision = db.get(ProtocolRevision, body.protocol_revision_id)
    if not revision:
        raise HTTPException(404, "protocol revision not found")
    if revision.status != "approved":
        raise HTTPException(409, "screening requires an approved protocol revision")
    batch = db.get(CutoffBatch, body.cutoff_batch_id)
    if not batch:
        raise HTTPException(404, "cutoff batch not found")

    base_run = None
    if body.strategy == "incremental":
        if not body.base_run_id:
            raise HTTPException(422, "incremental runs need a base_run_id")
        base_run = db.get(ScreeningRun, body.base_run_id)
        if not base_run or base_run.status not in ("completed", "verified"):
            raise HTTPException(422, "base run must be completed")
        base_rev = db.get(ProtocolRevision, base_run.protocol_revision_id)
        if rules_hash(base_rev.rules or {}) != rules_hash(revision.rules or {}):
            raise HTTPException(
                409,
                "incremental runs cannot cross a protocol rule change; run full recomputation",
            )
        base_batch = db.get(CutoffBatch, base_run.cutoff_batch_id)
        if not set(base_batch.upload_ids or []).issubset(set(batch.upload_ids or [])):
            raise HTTPException(
                409,
                "the new cutoff must contain every upload of the base cutoff; "
                "otherwise run full recomputation",
            )

    if body.max_centers is not None or body.crash_after_centers is not None:
        from ..config import settings

        if not settings.allow_fault_injection:
            raise HTTPException(
                403, "fault injection is disabled in this deployment"
            )

    run = ScreeningRun(
        protocol_revision_id=revision.id,
        cutoff_batch_id=batch.id,
        strategy=body.strategy,
        status="queued",
        base_run_id=base_run.id if base_run else None,
        trigger_type=body.trigger_type,
        trigger_ref=body.trigger_ref,
        executed_by=user.username,
    )
    db.add(run)
    db.flush()
    audit(
        db,
        user.username,
        "screening.start",
        "screening_run",
        run.id,
        {"strategy": run.strategy, "cutoff": batch.label, "revision": revision.version},
    )
    db.commit()

    return _execute(db, run, body)


def _execute(db: Session, run: ScreeningRun, body) -> dict:
    from datetime import datetime, timezone

    progress: list[dict] = []

    def record(center_code, n, reused, recomputed):
        progress.append(
            {"center": center_code, "subjects": n, "reused": reused, "recomputed": recomputed}
        )

    try:
        execute_run(
            db,
            run,
            max_centers=body.max_centers,
            crash_after_centers=body.crash_after_centers,
            progress=record,
        )
    except InjectedCrash as exc:
        db.refresh(run)
        run.status = "partial"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        audit(db, run.executed_by, "screening.interrupted", "screening_run", run.id, {"error": str(exc)})
        db.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "message": "run interrupted; resume the same run to continue from the unfinished centre",
                "run": _run_summary(db, run),
                "progress": progress,
            },
        )
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    audit(
        db,
        run.executed_by,
        "screening.complete",
        "screening_run",
        run.id,
        {"counts": run.counts, "reused": run.reused_count, "recomputed": run.recomputed_count},
    )
    db.commit()
    return {**_run_summary(db, run), "progress": progress}


@router.post("/screening-runs/{run_id}/resume")
def resume_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer", "analysis_lead")),
):
    run = db.get(ScreeningRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.status not in ("partial", "running", "queued", "failed"):
        raise HTTPException(409, f"run is {run.status}; nothing to resume")

    class _Body:
        max_centers = None
        crash_after_centers = None

    audit(db, user.username, "screening.resume", "screening_run", run.id)
    db.commit()
    return _execute(db, run, _Body)


@router.get("/screening-runs")
def list_runs(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    runs = db.scalars(select(ScreeningRun).order_by(ScreeningRun.id)).all()
    return [_run_summary(db, r) for r in runs]


@router.get("/screening-runs/{run_id}")
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    run = db.get(ScreeningRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    reconciliation = db.execute(
        select(Reconciliation, Center)
        .join(Center, Center.id == Reconciliation.center_id)
        .where(Reconciliation.run_id == run_id)
        .order_by(Center.code)
    ).all()
    return {
        **_run_summary(db, run),
        "reconciliation": [
            {
                "center_code": c.code,
                "raw_rows": r.raw_rows,
                "normalized_ok": r.normalized_ok,
                "pending": r.pending,
                "failed": r.failed,
                "subjects_total": r.subjects_total,
                "included": r.included,
                "missing_data": r.missing_data,
                # identity_review is a *sub-count* of pending_subjects, broken
                # out because identity links need the privacy admin.
                "pending_subjects": r.pending_subjects,
                "identity_review": r.identity_review,
                "excluded_ineligible": r.excluded_ineligible,
                "consent_withdrawn": r.consent_withdrawn,
                "merged_visits": r.merged_visits,
                "balanced": r.balanced,
                "checks": r.checks,
            }
            for r, c in reconciliation
        ],
    }


@router.post("/screening-runs/{run_id}/verify-equivalence")
def verify_equivalence(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer", "analysis_lead")),
):
    """Re-run the same protocol and cutoff with the other strategy and compare
    every subject decision. The two strategies must agree exactly."""
    run = db.get(ScreeningRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.status == "partial":
        raise HTTPException(409, "resume the interrupted run before verifying equivalence")

    if run.strategy == "incremental":
        # Meaningful check: incremental result == full recomputation at the
        # identical protocol revision and cutoff.
        counterpart_strategy = "full"
        counterpart_base = None
    else:
        # For a full run, verify with an independent incremental path whose
        # baseline is the most recent earlier cutoff of the same rules.
        prior = db.scalars(
            select(ScreeningRun)
            .where(
                ScreeningRun.id != run.id,
                ScreeningRun.status.in_(("completed", "verified")),
                ScreeningRun.protocol_revision_id == run.protocol_revision_id,
                ScreeningRun.cutoff_batch_id != run.cutoff_batch_id,
            )
            .order_by(ScreeningRun.id.desc())
        ).all()
        batch_ids = set(db.get(CutoffBatch, run.cutoff_batch_id).upload_ids or [])
        counterpart_base = None
        for candidate in prior:
            cand_uploads = set(db.get(CutoffBatch, candidate.cutoff_batch_id).upload_ids or [])
            if cand_uploads.issubset(batch_ids):
                counterpart_base = candidate
                break
        if counterpart_base is None:
            raise HTTPException(
                409,
                "no earlier completed run of the same rules covers a subset of this cutoff; "
                "an incremental counterpart cannot be constructed",
            )
        counterpart_strategy = "incremental"

    other = ScreeningRun(
        protocol_revision_id=run.protocol_revision_id,
        cutoff_batch_id=run.cutoff_batch_id,
        strategy=counterpart_strategy,
        status="queued",
        base_run_id=counterpart_base.id if counterpart_base else None,
        trigger_type="manual",
        trigger_ref=f"verify:{run.id}",
        verification_of_run_id=run.id,
        executed_by=user.username,
    )
    db.add(other)
    db.flush()
    db.commit()

    class _Body:
        max_centers = None
        crash_after_centers = None

    _execute(db, other, _Body)

    left = {
        d.subject_id: (d.outcome, tuple(sorted(d.reason_codes or [])))
        for d in db.scalars(
            select(ScreeningDecision).where(ScreeningDecision.run_id == run.id)
        ).all()
    }
    right = {
        d.subject_id: (d.outcome, tuple(sorted(d.reason_codes or [])))
        for d in db.scalars(
            select(ScreeningDecision).where(ScreeningDecision.run_id == other.id)
        ).all()
    }
    mismatches = []
    for sid in sorted(set(left) | set(right)):
        if left.get(sid) != right.get(sid):
            mismatches.append(
                {
                    "subject_id": sid,
                    "left": left.get(sid),
                    "right": right.get(sid),
                }
            )
    # Snapshot membership must also agree.
    result = {
        "run_id": run.id,
        "comparison_run_id": other.id,
        "left_strategy": run.strategy,
        "right_strategy": other.strategy,
        "subjects_compared": len(set(left) | set(right)),
        "equal": not mismatches,
        "mismatches": mismatches[:50],
        "mismatch_count": len(mismatches),
        "cohort_checksum": {
            "left": _cohort_checksum(left),
            "right": _cohort_checksum(right),
        },
    }
    run.equivalence_result = result
    other.equivalence_result = result
    for target in (run, other):
        target.status = "verified" if result["equal"] else "completed"
    db.commit()
    audit(db, user.username, "screening.verify", "screening_run", run.id, result)
    db.commit()
    return result


def _cohort_checksum(decisions: dict) -> str:
    included = sorted(
        [sid, outcome] for sid, (outcome, _) in decisions.items()
        if outcome == "included"
    )
    return hashlib.sha256(json.dumps(included).encode()).hexdigest()


@router.get("/screening-runs/{run_id}/decisions")
def run_decisions(
    run_id: int,
    outcome: str | None = None,
    center_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    stmt = (
        select(ScreeningDecision, Subject, Center)
        .join(Subject, Subject.id == ScreeningDecision.subject_id)
        .join(Center, Center.id == ScreeningDecision.center_id)
        .where(ScreeningDecision.run_id == run_id)
        .order_by(Center.code, Subject.study_id)
    )
    if outcome:
        stmt = stmt.where(ScreeningDecision.outcome == outcome)
    if center_code:
        stmt = stmt.where(Center.code == center_code)
    rows = db.execute(stmt).all()
    return [
        {
            "study_id": s.study_id,
            "subject_id": s.id,
            "center_code": c.code,
            "outcome": d.outcome,
            "reason_codes": d.reason_codes,
            "evidence": d.evidence,
            "index_date": d.index_date.isoformat() if d.index_date else None,
            "visits_merged": d.visits_merged,
            "safety_retain": d.safety_retain,
            "reused": d.reused,
            "reused_from_run_id": d.reused_from_run_id,
            "input_fingerprint": d.input_fingerprint,
        }
        for d, s, c in rows
    ]
