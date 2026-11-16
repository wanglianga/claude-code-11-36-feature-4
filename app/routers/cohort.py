"""Cohort snapshots, follow-up plans, analysis runs and frozen reports."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    AnalysisRun,
    CohortSnapshot,
    Center,
    CutoffBatch,
    FollowUpPlan,
    FollowUpTask,
    ProtocolRevision,
    Report,
    ScreeningRun,
    SnapshotCase,
    Subject,
    User,
)
from ..security import audit, require_roles
from ..schemas import AnalysisIn, ConsentWithdrawalIn, ReportIn
from ..services.cohort import (
    SnapshotError,
    build_followup_plan,
    create_snapshot,
    lock_snapshot,
    publish_report,
    run_analysis,
)

router = APIRouter(tags=["cohort"])


def _snapshot_summary(db: Session, snap: CohortSnapshot) -> dict:
    revision = db.get(ProtocolRevision, snap.protocol_revision_id)
    batch = db.get(CutoffBatch, snap.cutoff_batch_id)
    return {
        "id": snap.id,
        "status": snap.status,
        "run_id": snap.run_id,
        "protocol": {
            "revision_id": revision.id,
            "version": revision.version,
            "protocol_id": revision.protocol_id,
        },
        "cutoff": {"id": batch.id, "label": batch.label},
        "case_count": snap.case_count,
        "safety_count": snap.safety_count,
        "checksum": snap.checksum,
        "created_by": snap.created_by,
        "locked_by": snap.locked_by,
        "supersedes_snapshot_id": snap.supersedes_snapshot_id,
        "created_at": snap.created_at.isoformat(),
        "locked_at": snap.locked_at.isoformat() if snap.locked_at else None,
    }


@router.post("/screening-runs/{run_id}/snapshots", status_code=201)
def make_snapshot(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    run = db.get(ScreeningRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    try:
        snap = create_snapshot(db, run, user.username)
    except SnapshotError as exc:
        raise HTTPException(409, str(exc))
    # Creating a snapshot from a run is the analysis lead's acknowledgement of
    # the per-centre reconciliation; reconciliation cannot pass with an
    # unexplained discrepancy, so this is not a rubber stamp.
    audit(
        db,
        user.username,
        "snapshot.create",
        "snapshot",
        snap.id,
        {
            "run_id": run_id,
            "checksum": snap.checksum,
            "cases": snap.case_count,
            "centres_balanced": [
                {"center_id": k, "balanced": v}
                for k, v in sorted((run.counts or {}).get("per_center", {}).items())
            ],
        },
    )
    db.commit()
    return _snapshot_summary(db, snap)


@router.post("/snapshots/{snapshot_id}/lock")
def lock(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    snap = db.get(CohortSnapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, "snapshot not found")
    if snap.status == "locked":
        raise HTTPException(409, "snapshot already locked")
    # Locking requires the analysis lead to have acknowledged per-centre
    # reconciliation: that is recorded because create_snapshot refuses a
    # non-balanced run, so the lock itself only needs the frozen artefact.
    lock_snapshot(db, snap, user.username)
    audit(db, user.username, "snapshot.lock", "snapshot", snap.id, {"checksum": snap.checksum})
    db.commit()
    return _snapshot_summary(db, snap)


@router.get("/snapshots")
def list_snapshots(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    snaps = db.scalars(select(CohortSnapshot).order_by(CohortSnapshot.id)).all()
    return [_snapshot_summary(db, s) for s in snaps]


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    snap = db.get(CohortSnapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, "snapshot not found")
    cases = db.execute(
        select(SnapshotCase, Subject, Center)
        .join(Subject, Subject.id == SnapshotCase.subject_id)
        .join(Center, Center.id == SnapshotCase.center_id)
        .where(SnapshotCase.snapshot_id == snapshot_id)
        .order_by(Center.code, Subject.study_id)
    ).all()
    return {
        **_snapshot_summary(db, snap),
        "cases": [
            {
                "study_id": s.study_id,
                "center_code": c.code,
                "outcome": sc.outcome,
                "reason_codes": sc.reason_codes,
                "index_date": sc.index_date.isoformat() if sc.index_date else None,
                "safety_retain": sc.safety_retain,
                "evidence": sc.evidence,
            }
            for sc, s, c in cases
        ],
    }


@router.post("/snapshots/{snapshot_id}/follow-up-plans", status_code=201)
def make_followup_plan(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    snap = db.get(CohortSnapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, "snapshot not found")
    try:
        plan = build_followup_plan(db, snap, user.username)
    except SnapshotError as exc:
        raise HTTPException(409, str(exc))
    audit(db, user.username, "followup.plan", "follow_up_plan", plan.id, {"snapshot_id": snapshot_id})
    db.commit()
    return {"id": plan.id, "snapshot_id": plan.snapshot_id, "protocol_revision_id": plan.protocol_revision_id}


@router.get("/follow-up-plans/{plan_id}")
def get_followup_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead", "rule_reviewer", "center_uploader")),
):
    plan = db.get(FollowUpPlan, plan_id)
    if not plan:
        raise HTTPException(404, "plan not found")
    stmt = (
        select(FollowUpTask, Subject, Center)
        .join(Subject, Subject.id == FollowUpTask.subject_id)
        .join(Center, Center.id == FollowUpTask.center_id)
        .where(FollowUpTask.plan_id == plan_id)
        .order_by(Center.code, Subject.study_id, FollowUpTask.visit_day)
    )
    if user.role == "center_uploader":
        stmt = stmt.where(FollowUpTask.center_id == user.center_id)
    rows = db.execute(stmt).all()
    return {
        "id": plan.id,
        "snapshot_id": plan.snapshot_id,
        "protocol_revision_id": plan.protocol_revision_id,
        "tasks": [
            {
                "task_id": t.id,
                "study_id": s.study_id,
                "center_code": c.code,
                "visit_day": t.visit_day,
                "kind": t.kind,
                "scheduled_on": t.scheduled_on.isoformat() if t.scheduled_on else None,
                "status": t.status,
                "blocked_reason": t.blocked_reason,
            }
            for t, s, c in rows
        ],
    }


@router.post("/snapshots/{snapshot_id}/analysis-runs", status_code=201)
def make_analysis(
    snapshot_id: int,
    body: AnalysisIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    snap = db.get(CohortSnapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, "snapshot not found")
    try:
        run = run_analysis(db, snap, body.kind, user.username)
    except SnapshotError as exc:
        raise HTTPException(409, str(exc))
    audit(db, user.username, "analysis.run", "analysis_run", run.id, {"snapshot_id": snapshot_id})
    db.commit()
    return {
        "id": run.id,
        "snapshot_id": run.snapshot_id,
        "protocol_revision_id": run.protocol_revision_id,
        "kind": run.kind,
        "results": run.results,
    }


@router.post("/reports", status_code=201)
def create_report(
    body: ReportIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    run = db.get(AnalysisRun, body.analysis_run_id)
    if not run:
        raise HTTPException(404, "analysis run not found")
    snap = db.get(CohortSnapshot, run.snapshot_id)
    if not snap or snap.status not in ("locked", "superseded"):
        raise HTTPException(409, "reports require a locked snapshot")
    report = publish_report(db, run, snap, user.username, body.note)
    audit(
        db,
        user.username,
        "report.publish",
        "report",
        report.id,
        {"version": report.report_version, "snapshot_id": snap.id, "hash": report.content_hash},
    )
    db.commit()
    return {
        "id": report.id,
        "report_version": report.report_version,
        "snapshot_id": report.snapshot_id,
        "protocol_revision_id": report.protocol_revision_id,
        "content_hash": report.content_hash,
        "frozen": report.frozen,
        "content": report.content,
    }


@router.get("/reports")
def list_reports(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    rows = db.scalars(select(Report).order_by(Report.id)).all()
    return [
        {
            "id": r.id,
            "report_version": r.report_version,
            "snapshot_id": r.snapshot_id,
            "protocol_revision_id": r.protocol_revision_id,
            "protocol_version": r.content["protocol"]["version"],
            "case_count": r.content["snapshot"]["case_count"],
            "snapshot_checksum": r.content["snapshot"]["checksum"],
            "content_hash": r.content_hash,
            "frozen": r.frozen,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/reports/{report_id}")
def get_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(404, "report not found")
    return {
        "id": report.id,
        "report_version": report.report_version,
        "frozen": report.frozen,
        "content_hash": report.content_hash,
        "content": report.content,
    }


@router.post("/subjects/{study_id}/withdraw")
def withdraw_consent(
    study_id: str,
    body: ConsentWithdrawalIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead", "privacy_admin")),
):
    """Terminate future contact immediately.

    Data already collected and permitted for retention still flows into the
    safety analysis through the next screening run; this endpoint only stops
    outreach, it does not delete history.
    """
    from ..models import Subject
    from ..services.cohort import apply_withdrawal_to_followups

    subject = db.scalar(select(Subject).where(Subject.study_id == study_id))
    if not subject:
        raise HTTPException(404, "subject not found")
    cancelled = apply_withdrawal_to_followups(db, subject.id, user.username)
    audit(
        db,
        user.username,
        "consent.withdraw",
        "subject",
        study_id,
        {"cancelled_tasks": cancelled, "allow_retention": body.allow_retention},
    )
    db.commit()
    return {
        "study_id": study_id,
        "cancelled_follow_up_tasks": cancelled,
        "note": "the next screening run will record the withdrawal; a new snapshot supersedes the old one",
    }
