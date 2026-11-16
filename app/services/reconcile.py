"""Per-center data reconciliation, executed before a snapshot may be locked.

The analysis lead must confirm, for every center, that:

    raw source rows == normalized ok + pending + failed        (row conservation)
    subjects         == included + missing + ineligible
                        + withdrawn + pending                 (subject conservation)
    included subjects have no unresolved source rows          (no silent pollution)

Only when all checks pass is the snapshot lockable.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Center,
    CutoffBatch,
    NormalizedRecord,
    Reconciliation,
    ScreeningDecision,
    ScreeningRun,
    SourceRecord,
)
from .screening import (
    INELIGIBLE,
    INCLUDED,
    INSUFFICIENT,
    PENDING,
    WITHDRAWN,
)

BLOCKING_KINDS = ("case", "diagnosis", "lab", "medication", "followup", "consent")


def reconcile_run(db: Session, run: ScreeningRun) -> dict:
    batch = db.get(CutoffBatch, run.cutoff_batch_id)
    upload_ids = [int(u) for u in (batch.upload_ids or [])]
    centers = db.scalars(select(Center).order_by(Center.id)).all()
    total = {
        "raw_rows": 0,
        "normalized_ok": 0,
        "pending": 0,
        "failed": 0,
        "subjects_total": 0,
        "included": 0,
        "missing_data": 0,
        "identity_review": 0,
        "excluded_ineligible": 0,
        "consent_withdrawn": 0,
        "pending_subjects": 0,
        "merged_visits": 0,
        "unlinked_rows": 0,
        "safety_retained": 0,
    }
    all_balanced = True

    for center in centers:
        existing = db.scalar(
            select(Reconciliation).where(
                Reconciliation.run_id == run.id, Reconciliation.center_id == center.id
            )
        )
        identity_review = sum(
            1
            for reasons in db.scalars(
                select(ScreeningDecision.reason_codes).where(
                    ScreeningDecision.run_id == run.id,
                    ScreeningDecision.center_id == center.id,
                    ScreeningDecision.outcome == PENDING,
                )
            ).all()
            if "identity_candidate_unadjudicated" in (reasons or [])
        )
        raw_rows = db.scalar(
            select(func.count(SourceRecord.id)).where(
                SourceRecord.center_id == center.id,
                SourceRecord.upload_id.in_(upload_ids) if upload_ids else False,
            )
        ) or 0
        status_rows = db.execute(
            select(NormalizedRecord.status, func.count())
            .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
            .where(SourceRecord.center_id == center.id, SourceRecord.upload_id.in_(upload_ids))
            .group_by(NormalizedRecord.status)
        ).all()
        by_status = {s: int(n) for s, n in status_rows}
        ok = by_status.get("ok", 0)
        pend = by_status.get("pending", 0)
        fail = by_status.get("failed", 0)

        linked = db.scalar(
            select(func.count(NormalizedRecord.id))
            .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
            .where(
                SourceRecord.center_id == center.id,
                SourceRecord.upload_id.in_(upload_ids),
                NormalizedRecord.subject_id.is_not(None),
            )
        ) or 0
        unlinked = raw_rows - linked
        # A row that normalized cleanly but has no subject would be data we can
        # read but cannot attribute to anyone: that must not pass silently.
        unlinked_ok = db.scalar(
            select(func.count(NormalizedRecord.id))
            .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
            .where(
                SourceRecord.center_id == center.id,
                SourceRecord.upload_id.in_(upload_ids),
                NormalizedRecord.subject_id.is_(None),
                NormalizedRecord.status == "ok",
            )
        ) or 0

        outcome_rows = db.execute(
            select(ScreeningDecision.outcome, func.count())
            .where(ScreeningDecision.run_id == run.id, ScreeningDecision.center_id == center.id)
            .group_by(ScreeningDecision.outcome)
        ).all()
        outcomes = {o: int(n) for o, n in outcome_rows}
        subjects_total = sum(outcomes.values())

        merged = db.scalar(
            select(func.coalesce(func.sum(ScreeningDecision.visits_merged), 0)).where(
                ScreeningDecision.run_id == run.id, ScreeningDecision.center_id == center.id
            )
        ) or 0
        safety = db.scalar(
            select(func.count(ScreeningDecision.id)).where(
                ScreeningDecision.run_id == run.id,
                ScreeningDecision.center_id == center.id,
                ScreeningDecision.safety_retain.is_(True),
            )
        ) or 0

        # Blocking rows: unresolved source rows that feed protocol criteria and
        # are attached to a subject.
        blocking = db.scalar(
            select(func.count(NormalizedRecord.id))
            .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
            .where(
                SourceRecord.center_id == center.id,
                SourceRecord.upload_id.in_(upload_ids),
                SourceRecord.active.is_(True),
                NormalizedRecord.status != "ok",
                NormalizedRecord.kind.in_(BLOCKING_KINDS),
                NormalizedRecord.subject_id.is_not(None),
            )
        ) or 0

        # How many adjudicated subjects still have unresolved blocking rows?
        # Superseded revisions are audit-only and never block an inclusion.
        blocking_subject_ids = set(
            r[0]
            for r in db.execute(
                select(NormalizedRecord.subject_id)
                .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
                .where(
                    SourceRecord.center_id == center.id,
                    SourceRecord.upload_id.in_(upload_ids),
                    SourceRecord.active.is_(True),
                    NormalizedRecord.status != "ok",
                    NormalizedRecord.kind.in_(BLOCKING_KINDS),
                    NormalizedRecord.subject_id.is_not(None),
                )
                .distinct()
            ).all()
        )
        included_ids = set(
            r[0]
            for r in db.execute(
                select(ScreeningDecision.subject_id).where(
                    ScreeningDecision.run_id == run.id,
                    ScreeningDecision.center_id == center.id,
                    ScreeningDecision.outcome == INCLUDED,
                )
            ).all()
        )
        # Only included subjects are held to the "no unresolved rows" rule; a
        # subject legitimately parked in `insufficient_data` is expected to have
        # them.
        polluted = sorted(included_ids & blocking_subject_ids)

        # Subjects with a decision but no linked record in scope, or duplicate
        # decisions, would break the subject identity.
        decided_ids = set(
            r[0]
            for r in db.execute(
                select(ScreeningDecision.subject_id).where(
                    ScreeningDecision.run_id == run.id, ScreeningDecision.center_id == center.id
                )
            ).all()
        )
        linked_subject_ids = set(
            r[0]
            for r in db.execute(
                select(NormalizedRecord.subject_id)
                .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
                .where(
                    SourceRecord.center_id == center.id,
                    SourceRecord.upload_id.in_(upload_ids),
                    NormalizedRecord.subject_id.is_not(None),
                )
                .distinct()
            ).all()
        )
        orphans = sorted(decided_ids - linked_subject_ids)

        # Subjects whose decision count differs from 1 (duplicate rows).
        dup_rows = db.execute(
            select(ScreeningDecision.subject_id, func.count())
            .where(ScreeningDecision.run_id == run.id, ScreeningDecision.center_id == center.id)
            .group_by(ScreeningDecision.subject_id)
            .having(func.count() != 1)
        ).all()

        checks = {
            "row_conservation": {
                "expected": raw_rows,
                "actual": ok + pend + fail,
                "ok": raw_rows == ok + pend + fail,
            },
            "subject_conservation": {
                "expected": len(linked_subject_ids),
                "actual": subjects_total,
                "ok": len(linked_subject_ids) == subjects_total,
            },
            "row_linkage": {
                "raw_rows": raw_rows,
                "linked_rows": linked,
                "unlinked_rows": unlinked,
                "unlinked_but_ok": unlinked_ok,
                "ok": raw_rows == linked + unlinked and unlinked_ok == 0,
            },
            "no_orphan_decisions": {"orphans": orphans, "ok": not orphans},
            "no_duplicate_decisions": {
                "duplicates": [{"subject_id": s, "n": n} for s, n in dup_rows],
                "ok": not dup_rows,
            },
            "no_polluted_inclusions": {
                "subjects_with_unresolved_rows": polluted,
                "unresolved_rows": blocking,
                "ok": not polluted,
            },
        }
        balanced = all(c["ok"] for c in checks.values())

        if existing is None:
            existing = Reconciliation(run_id=run.id, center_id=center.id)
            db.add(existing)
        existing.raw_rows = raw_rows
        existing.normalized_ok = ok
        existing.pending = pend
        existing.failed = fail
        existing.subjects_total = subjects_total
        existing.included = outcomes.get(INCLUDED, 0)
        existing.missing_data = outcomes.get(INSUFFICIENT, 0)
        existing.identity_review = int(identity_review)
        existing.excluded_ineligible = outcomes.get(INELIGIBLE, 0)
        existing.consent_withdrawn = outcomes.get(WITHDRAWN, 0)
        existing.pending_subjects = outcomes.get(PENDING, 0)
        existing.merged_visits = int(merged)
        existing.balanced = balanced
        existing.checks = checks

        total["raw_rows"] += raw_rows
        total["normalized_ok"] += ok
        total["pending"] += pend
        total["failed"] += fail
        total["subjects_total"] += subjects_total
        total["included"] += outcomes.get(INCLUDED, 0)
        total["missing_data"] += outcomes.get(INSUFFICIENT, 0)
        total["identity_review"] += outcomes.get(PENDING, 0)
        total["excluded_ineligible"] += outcomes.get(INELIGIBLE, 0)
        total["consent_withdrawn"] += outcomes.get(WITHDRAWN, 0)
        total["pending_subjects"] += outcomes.get(PENDING, 0)
        total["merged_visits"] += int(merged)
        total["unlinked_rows"] += unlinked
        total["safety_retained"] += int(safety)
        all_balanced = all_balanced and balanced

    run.counts = total
    run.counts["balanced"] = all_balanced
    run.counts["centers"] = len(centers)
    run.counts["per_center"] = {
        str(rec.center_id): rec.balanced
        for rec in db.scalars(
            select(Reconciliation).where(Reconciliation.run_id == run.id)
        ).all()
    }
    db.commit()
    return run.counts
