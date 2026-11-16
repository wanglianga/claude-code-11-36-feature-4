"""Cohort snapshots, follow-up plans, analysis runs and versioned reports.

Rules enforced here:

* A snapshot can only be created from a completed, fully reconciled run.
* A locked snapshot is immutable: late data creates a new snapshot that
  supersedes it; old reports keep pointing at the old snapshot.
* Snapshots, follow-up plans and analysis runs all carry the same
  ``protocol_revision_id``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AnalysisRun,
    CohortSnapshot,
    FollowUpPlan,
    FollowUpTask,
    ProtocolRevision,
    Report,
    ScreeningDecision,
    ScreeningRun,
    SnapshotCase,
)
from .screening import INCLUDED, WITHDRAWN

SNAPSHOT_OUTCOMES = (INCLUDED, WITHDRAWN)


class SnapshotError(RuntimeError):
    pass


def create_snapshot(db: Session, run: ScreeningRun, actor: str) -> CohortSnapshot:
    if run.status not in ("completed", "verified"):
        raise SnapshotError(f"Run {run.id} is {run.status}; a snapshot needs a completed run")
    if not (run.counts or {}).get("balanced", False):
        raise SnapshotError(
            "Reconciliation failed; the analysis lead must resolve the centre discrepancies first"
        )

    previous = db.scalar(
        select(CohortSnapshot)
        .where(
            CohortSnapshot.protocol_revision_id == run.protocol_revision_id,
            CohortSnapshot.status.in_(("locked", "draft")),
        )
        .order_by(CohortSnapshot.id.desc())
    )

    decisions = db.scalars(
        select(ScreeningDecision).where(
            ScreeningDecision.run_id == run.id,
            ScreeningDecision.outcome.in_(SNAPSHOT_OUTCOMES),
        )
    ).all()

    snapshot = CohortSnapshot(
        run_id=run.id,
        protocol_revision_id=run.protocol_revision_id,
        cutoff_batch_id=run.cutoff_batch_id,
        status="draft",
        supersedes_snapshot_id=previous.id if previous else None,
        case_count=sum(1 for d in decisions if d.outcome == INCLUDED),
        safety_count=sum(
            1 for d in decisions if d.outcome == WITHDRAWN and d.safety_retain
        ),
        created_by=actor,
    )
    db.add(snapshot)
    db.flush()

    rows = []
    for d in sorted(decisions, key=lambda x: (x.center_id, x.subject_id)):
        rows.append(
            SnapshotCase(
                snapshot_id=snapshot.id,
                subject_id=d.subject_id,
                center_id=d.center_id,
                outcome=d.outcome,
                reason_codes=list(d.reason_codes or []),
                evidence=list(d.evidence or []),
                index_date=d.index_date,
                safety_retain=d.safety_retain,
                treatment_code=_freeze_treatment(db, d),
                decision_id=d.id,
            )
        )
    db.add_all(rows)
    snapshot.checksum = snapshot_checksum(snapshot, rows)
    db.commit()
    return snapshot


def snapshot_checksum(snapshot: CohortSnapshot, rows) -> str:
    material = {
        "run_id": snapshot.run_id,
        "protocol_revision_id": snapshot.protocol_revision_id,
        "cutoff_batch_id": snapshot.cutoff_batch_id,
        "cases": [
            {
                "subject_id": r.subject_id,
                "center_id": r.center_id,
                "outcome": r.outcome,
                "index_date": r.index_date.isoformat() if r.index_date else None,
                "safety_retain": r.safety_retain,
                "decision_id": r.decision_id,
            }
            for r in rows
        ],
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()
    ).hexdigest()


def lock_snapshot(db: Session, snapshot: CohortSnapshot, actor: str) -> CohortSnapshot:
    if snapshot.status == "locked":
        return snapshot
    if snapshot.status != "draft":
        raise SnapshotError("Only a draft snapshot can be locked")
    snapshot.status = "locked"
    snapshot.locked_by = actor
    snapshot.locked_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    # The predecessor stays readable (old reports point at it) but is marked as
    # superseded so nobody mistakes it for current.
    if snapshot.supersedes_snapshot_id:
        previous = db.get(CohortSnapshot, snapshot.supersedes_snapshot_id)
        if previous and previous.status == "locked":
            previous.status = "superseded"
    db.commit()
    return snapshot


def build_followup_plan(db: Session, snapshot: CohortSnapshot, actor: str) -> FollowUpPlan:
    if snapshot.status not in ("locked", "superseded"):
        raise SnapshotError(
            "Follow-up plans are built only from a locked snapshot"
        )
    revision = db.get(ProtocolRevision, snapshot.protocol_revision_id)
    schedule = (revision.rules or {}).get("follow_up") or {}
    visits = schedule.get("visits") or [
        {"day": 30, "kind": "safety"},
        {"day": 90, "kind": "efficacy"},
        {"day": 180, "kind": "efficacy"},
    ]

    existing = db.scalar(
        select(FollowUpPlan).where(
            FollowUpPlan.snapshot_id == snapshot.id,
            FollowUpPlan.protocol_revision_id == snapshot.protocol_revision_id,
        )
    )
    if existing:
        return existing

    plan = FollowUpPlan(
        snapshot_id=snapshot.id,
        protocol_revision_id=snapshot.protocol_revision_id,
        created_by=actor,
    )
    db.add(plan)
    db.flush()

    active_withdrawals = set(
        r[0]
        for r in db.execute(
            select(SnapshotCase.subject_id).where(
                SnapshotCase.snapshot_id == snapshot.id,
                SnapshotCase.outcome == WITHDRAWN,
            )
        ).all()
    )
    cases = db.scalars(
        select(SnapshotCase).where(
            SnapshotCase.snapshot_id == snapshot.id,
            SnapshotCase.outcome == INCLUDED,
        )
    ).all()
    tasks = []
    for case in cases:
        for visit in visits:
            base = case.index_date or snapshot.created_at.date()
            tasks.append(
                FollowUpTask(
                    plan_id=plan.id,
                    subject_id=case.subject_id,
                    center_id=case.center_id,
                    protocol_revision_id=snapshot.protocol_revision_id,
                    visit_day=int(visit.get("day", 0)),
                    kind=str(visit.get("kind", "safety")),
                    scheduled_on=base + timedelta(days=int(visit.get("day", 0))),
                    status="scheduled",
                )
            )
    # Safety-retained withdrawals are never scheduled; the plan simply omits
    # them and the safety analysis reads their frozen historical records.
    assert not active_withdrawals.intersection({t.subject_id for t in tasks})
    db.add_all(tasks)
    db.commit()
    return plan


def apply_withdrawal_to_followups(db: Session, subject_id: int, actor: str) -> int:
    """Terminate future contact. Completed tasks are left intact."""
    tasks = db.scalars(
        select(FollowUpTask).where(
            FollowUpTask.subject_id == subject_id,
            FollowUpTask.status == "scheduled",
        )
    ).all()
    for task in tasks:
        task.status = "cancelled_consent_withdrawn"
        task.blocked_reason = "consent_withdrawn"
    db.commit()
    return len(tasks)


def run_analysis(db: Session, snapshot: CohortSnapshot, kind: str, actor: str) -> AnalysisRun:
    if snapshot.status not in ("locked", "superseded"):
        raise SnapshotError("Analysis reads only locked snapshots")
    cases = db.scalars(
        select(SnapshotCase).where(
            SnapshotCase.snapshot_id == snapshot.id,
            SnapshotCase.outcome == INCLUDED,
        )
    ).all()
    by_center: dict[int, int] = {}
    for c in cases:
        by_center[c.center_id] = by_center.get(c.center_id, 0) + 1

    # Treatment comparison across centres, resolved from the frozen snapshot's
    # own decisions so a later upload cannot change this run.
    treatments: dict[str, dict[str, int]] = {}
    for case in cases:
        code = case.treatment_code or "unspecified"
        bucket = treatments.setdefault(str(case.center_id), {})
        bucket[code] = bucket.get(code, 0) + 1

    results = {
        "snapshot_id": snapshot.id,
        "protocol_revision_id": snapshot.protocol_revision_id,
        "cutoff_batch_id": snapshot.cutoff_batch_id,
        "kind": kind,
        "case_count": len(cases),
        "by_center": {str(k): v for k, v in sorted(by_center.items())},
        "treatment_by_center": {
            center: dict(sorted(v.items())) for center, v in sorted(treatments.items())
        },
        "snapshot_checksum": snapshot.checksum,
    }
    run = AnalysisRun(
        snapshot_id=snapshot.id,
        protocol_revision_id=snapshot.protocol_revision_id,
        kind=kind,
        status="completed",
        results=results,
        executed_by=actor,
    )
    db.add(run)
    db.commit()
    return run


def _freeze_treatment(db: Session, decision: ScreeningDecision) -> str:
    """Read the treatment arm now, so the snapshot stores today's value.

    Captured at snapshot time (not analysis time) so a later amendment cannot
    retroactively change a frozen analysis.
    """
    from ..models import NormalizedRecord, SourceRecord

    record = db.scalar(
        select(NormalizedRecord)
        .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
        .where(
            NormalizedRecord.kind == "case",
            SourceRecord.active.is_(True),
            NormalizedRecord.subject_id == decision.subject_id,
        )
        .order_by(NormalizedRecord.id.desc())
    )
    if record is None:
        return "unspecified"
    return str((record.normalized or {}).get("treatment_code") or "unspecified")


def publish_report(
    db: Session,
    analysis_run: AnalysisRun,
    snapshot: CohortSnapshot,
    actor: str,
    note: str | None = None,
) -> Report:
    """Reports are versioned and immutable. A late upload never edits one."""
    previous = db.scalar(
        select(Report)
        .where(Report.protocol_revision_id == analysis_run.protocol_revision_id)
        .order_by(Report.report_version.desc())
    )
    version = (previous.report_version + 1) if previous else 1
    revision = db.get(ProtocolRevision, analysis_run.protocol_revision_id)
    content = {
        "report_version": version,
        "note": note,
        "protocol": {
            "id": revision.protocol_id,
            "revision_id": revision.id,
            "version": revision.version,
        },
        "snapshot": {
            "id": snapshot.id,
            "status": snapshot.status,
            "case_count": snapshot.case_count,
            "safety_count": snapshot.safety_count,
            "checksum": snapshot.checksum,
            "created_at": snapshot.created_at.isoformat(),
        },
        "analysis": analysis_run.results,
        "supersedes_report_id": previous.id if previous else None,
        "supersedes_snapshot_id": snapshot.supersedes_snapshot_id,
    }
    content_hash = hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str).encode()
    ).hexdigest()
    report = Report(
        analysis_run_id=analysis_run.id,
        snapshot_id=snapshot.id,
        protocol_revision_id=analysis_run.protocol_revision_id,
        report_version=version,
        content=content,
        content_hash=content_hash,
        supersedes_report_id=previous.id if previous else None,
        frozen=True,
        created_by=actor,
    )
    db.add(report)
    db.commit()
    return report
