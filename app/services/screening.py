"""Screening engine: evaluates protocol criteria against normalized records.

Two execution strategies share one evaluator:

* ``full``        - re-evaluate every subject in the frozen data scope.
* ``incremental`` - copy decisions from a base run for subjects whose input
                    fingerprint is unchanged, and re-evaluate only subjects
                    reachable from new uploads, changed mappings or confirmed
                    identity links.

The evaluator is a pure function of (protocol revision, normalized records in
the data cutoff, identity state). That is what makes the two strategies
provably equivalent, and ``verify_equivalence`` checks it against a full run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    Center,
    CodeMapping,
    CutoffBatch,
    IdentityCandidate,
    NormalizedRecord,
    ProtocolRevision,
    ScreeningDecision,
    ScreeningRun,
    ScreeningRunCenter,
    SourceRecord,
    Subject,
)

INCLUDED = "included"
INSUFFICIENT = "insufficient_data"
PENDING = "pending_adjudication"
INELIGIBLE = "excluded_ineligible"
WITHDRAWN = "consent_withdrawn"

OUTCOME_LABELS = {
    INCLUDED: "纳入分析队列",
    INSUFFICIENT: "缺资料待补（不纳入，不判定为不符合）",
    PENDING: "待判（标准化或身份关联未决）",
    INELIGIBLE: "明确不符合方案",
    WITHDRAWN: "撤回同意",
}


class InjectedCrash(RuntimeError):
    """Used by tests to simulate an interrupted run."""


@dataclass
class SubjectData:
    subject: Subject
    records: list = field(default_factory=list)  # (SourceRecord, NormalizedRecord)

    def by_kind(self, kind: str):
        return [(s, n) for s, n in self.records if n.kind == kind]

    @property
    def needs_review(self) -> bool:
        return any(n.status in ("pending", "failed") for _, n in self.records)


def rules_hash(rules: dict) -> str:
    return hashlib.sha256(
        json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------
def load_center_subjects(db: Session, center_id: int, upload_ids: list[int]) -> list[Subject]:
    rows = db.scalars(
        select(Subject)
        .join(NormalizedRecord, NormalizedRecord.subject_id == Subject.id)
        .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
        .where(
            Subject.center_id == center_id,
            Subject.merged_into_subject_id.is_(None),
            SourceRecord.center_id == center_id,
            SourceRecord.upload_id.in_(upload_ids),
            SourceRecord.active.is_(True),
        )
        .distinct()
        .order_by(Subject.id)
    ).all()
    # Guard against duplicate rows from the join ordering.
    seen, out = set(), []
    for s in rows:
        if s.id not in seen:
            seen.add(s.id)
            out.append(s)
    return out


def load_subject_data(db: Session, subject_id: int, upload_ids: list[int]) -> SubjectData:
    rows = db.execute(
        select(SourceRecord, NormalizedRecord)
        .join(NormalizedRecord, NormalizedRecord.source_record_id == SourceRecord.id)
        .where(
            NormalizedRecord.subject_id == subject_id,
            SourceRecord.upload_id.in_(upload_ids),
            SourceRecord.active.is_(True),
        )
        .order_by(SourceRecord.id)
    ).all()
    subject = db.get(Subject, subject_id)
    return SubjectData(subject=subject, records=[(s, n) for s, n in rows])


def pending_identity_for(db: Session, subject: Subject) -> bool:
    root = subject
    seen = set()
    while root.merged_into_subject_id and root.id not in seen:
        seen.add(root.id)
        root = db.get(Subject, root.merged_into_subject_id)
    return bool(
        db.scalar(
            select(IdentityCandidate.id).where(
                IdentityCandidate.status == "pending",
                (
                    (IdentityCandidate.subject_a_id == root.id)
                    | (IdentityCandidate.subject_b_id == root.id)
                ),
            )
        )
    )


# ---------------------------------------------------------------------------
# evaluator
# ---------------------------------------------------------------------------
def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _age_on(birth: date | None, moment: date) -> int | None:
    if not birth:
        return None
    return moment.year - birth.year - ((moment.month, moment.day) < (birth.month, birth.day))


def _consent_state(data: SubjectData) -> tuple[str, object, list[dict]]:
    """Latest consent row wins; two different withdrawal dates are ambiguous."""
    consents = [(s, n) for s, n in data.by_kind("consent")]
    if not consents:
        return "missing", None, []
    consents.sort(key=lambda sn: (_date(sn[1].normalized.get("withdrawal_date")) or date.min, sn[0].id))
    s, n = consents[-1]
    state = (n.normalized or {}).get("consent")
    if state not in ("granted", "withdrawn", "declined"):
        return "pending", n, [{"source_record_id": s.id, "reason": "consent_unmapped"}]
    return state, n, [
        {
            "source_record_id": s.id,
            "record_type": "consent",
            "reason": f"consent:{state}",
        }
    ]


def _index_date(rules: dict, data: SubjectData):
    """Return (index_date, visits_merged, evidence, problem).

    The protocol designates an index date; repeated visits that fall on that
    date are merged into one index visit and the merge is recorded.
    """
    cfg = rules.get("index_date") or {}
    basis = cfg.get("basis", "first_visit")
    merge_types = set(cfg.get("merge_visit_types") or ["screening", "baseline"])

    cases = data.by_kind("case")
    anchor = None
    for _, n in cases:
        anchor = anchor or _date((n.normalized or {}).get("first_visit_date"))
        anchor = anchor or _date((n.normalized or {}).get("enrollment_date"))
    if anchor is None:
        return None, 0, [], "anchor_date_missing"

    if basis == "first_diagnosis":
        codes = (rules.get("inclusion", {}).get("diagnosis", {}) or {}).get("codes", [])
        dated = []
        for s, n in data.by_kind("diagnosis"):
            d = _date((n.normalized or {}).get("diagnosis_date"))
            if (n.normalized or {}).get("code") in codes and d is not None:
                dated.append((s, n, d))
        if not dated:
            return None, 0, [], "index_date_unavailable"
        dated.sort(key=lambda c: (c[2], c[0].id))
        index_date = dated[0][2]
    else:
        index_date = anchor

    win = cfg.get("window_days")
    if win is not None and abs((index_date - anchor).days) > int(win):
        return index_date, 0, [], "index_date_outside_window"

    # Merge repeat visits that fall on the index date.
    same_day = [
        (s, n)
        for s, n in data.by_kind("followup")
        if (n.normalized or {}).get("visit_type", "scheduled") in merge_types
        and _date((n.normalized or {}).get("visit_date")) == index_date
    ]
    same_day.sort(key=lambda sn: sn[0].id)
    merged = max(0, len(same_day) - 1)
    evidence = [
        {
            "source_record_id": s.id,
            "record_type": "followup",
            "reason": (
                f"index_visit:{index_date.isoformat()}"
                if i == 0
                else f"merged_repeat_visit:{index_date.isoformat()}"
            ),
        }
        for i, (s, _) in enumerate(same_day)
    ]
    return index_date, merged, evidence, None


def evaluate_subject(
    db: Session,
    rules: dict,
    data: SubjectData,
    identity_pending: bool,
    protocol_revision_id: int,
) -> dict:
    """Pure evaluation for one subject. Returns decision fields + fingerprint."""
    reasons: list[str] = []
    evidence: list[dict] = []
    outcome: str | None = None
    index_date: date | None = None
    visits_merged = 0
    safety_retain = False

    # --- consent ---------------------------------------------------------
    consent, consent_norm, consent_evidence = _consent_state(data)
    evidence.extend(consent_evidence)
    if consent in ("withdrawn", "declined"):
        outcome = WITHDRAWN if consent == "withdrawn" else INELIGIBLE
        reasons.append("consent_withdrawn" if consent == "withdrawn" else "consent_declined")
        if consent == "withdrawn":
            allow = bool((consent_norm.normalized or {}).get("allow_retention"))
            safety_retain = allow and bool(
                (rules.get("safety") or {}).get("allow_retention_after_withdrawal", False)
            )
            if safety_retain:
                reasons.append("safety_retention_permitted")
        elif consent == "pending":
            outcome = PENDING
    elif consent == "pending":
        outcome = PENDING
        reasons.append("consent_unmapped")
    elif consent == "missing" and (rules.get("consent") or {}).get("required", True):
        outcome = INSUFFICIENT
        reasons.append("consent_missing")

    if outcome is None and identity_pending:
        outcome = PENDING
        reasons.append("identity_candidate_unadjudicated")

    # The case row is the anchor for every other criterion. If it could not be
    # standardized the subject is pending, not "missing data" and not eligible.
    if outcome is None:
        cases = data.by_kind("case")
        bad = [(s, n) for s, n in cases if n.status != "ok"]
        if cases and bad:
            outcome = PENDING
            s, n = bad[0]
            reasons.append(f"normalization_{n.status}:case")
            reasons.extend(n.issues or [])
            evidence.append(
                {"source_record_id": s.id, "record_type": "case", "reason": "normalization_pending"}
            )
        elif not cases:
            outcome = INSUFFICIENT
            reasons.append("case_record_missing")

    # Any unresolved row anywhere in the subject's record set needs
    # adjudication, not just the rows a given criterion happens to read. This is
    # what keeps "missing" and "pending" separate: an unreadable row is a
    # question for a human, a readable-but-absent row is genuine missing data.
    if outcome is None:
        unresolved = [(s, n) for s, n in data.records if n.status != "ok"]
        if unresolved:
            outcome = PENDING
            for s, n in unresolved:
                reasons.append(f"normalization_{n.status}:{n.kind}")
                reasons.extend(n.issues or [])
                evidence.append(
                    {
                        "source_record_id": s.id,
                        "record_type": n.kind,
                        "reason": "normalization_pending",
                    }
                )

    # --- index date ------------------------------------------------------
    if outcome is None:
        index_date, visits_merged, idx_evidence, problem = _index_date(rules, data)
        evidence.extend(idx_evidence)
        if problem == "index_date_unavailable":
            outcome = INSUFFICIENT
            reasons.append(problem)
        elif problem:
            outcome = INSUFFICIENT
            reasons.append(problem)

    # --- criteria --------------------------------------------------------
    if outcome is None:
        outcome, reasons, evidence = _apply_criteria(rules, data, index_date, reasons, evidence)

    fingerprint = _fingerprint(rules, data, identity_pending)
    return {
        "outcome": outcome,
        "reason_codes": reasons,
        "evidence": evidence,
        "index_date": index_date,
        "visits_merged": visits_merged,
        "safety_retain": safety_retain,
        "input_fingerprint": fingerprint,
    }


def _apply_criteria(rules, data: SubjectData, index_date: date, reasons, evidence):
    inc = rules.get("inclusion") or {}
    exc = rules.get("exclusion") or {}

    # Any needed record that failed normalization blocks a decision.
    needed_kinds = set()
    if inc.get("diagnosis") or exc.get("diagnosis"):
        needed_kinds.add("diagnosis")
    if inc.get("lab") or exc.get("lab"):
        needed_kinds.add("lab")
    if inc.get("medication") or exc.get("medication"):
        needed_kinds.add("medication")
    needed_codes = set()
    for spec in (inc.get("lab"), exc.get("lab")):
        if spec and spec.get("code"):
            needed_codes.add(spec["code"])
    for s, n in data.records:
        if n.kind not in needed_kinds:
            continue
        if needed_codes and n.kind == "lab" and (n.normalized or {}).get("code") not in needed_codes:
            continue
        if n.status in ("pending", "failed"):
            reasons = reasons + [f"normalization_{n.status}:{n.kind}"] + list(n.issues or [])
            evidence.append(
                {
                    "source_record_id": s.id,
                    "record_type": n.kind,
                    "reason": "normalization_pending",
                }
            )
            return PENDING, reasons, evidence

    # Age
    birth = None
    for _, n in data.by_kind("case"):
        birth = _date((n.normalized or {}).get("birth_date")) or birth
    age = _age_on(birth, index_date)
    age_min, age_max = inc.get("age_min"), inc.get("age_max")
    if age_min is not None or age_max is not None:
        if age is None:
            reasons.append("birth_date_missing")
            return INSUFFICIENT, reasons, evidence
        if age_min is not None and age < age_min:
            reasons.append(f"age_below_min:{age}<{age_min}")
            return INELIGIBLE, reasons, evidence
        if age_max is not None and age > age_max:
            reasons.append(f"age_above_max:{age}>{age_max}")
            return INELIGIBLE, reasons, evidence
        reasons.append(f"age_ok:{age}")

    # Required diagnosis
    diag_spec = inc.get("diagnosis")
    if diag_spec and diag_spec.get("codes"):
        window = int(diag_spec.get("window_days", 3650))
        hits = []
        for s, n in data.by_kind("diagnosis"):
            code = (n.normalized or {}).get("code")
            d = _date((n.normalized or {}).get("diagnosis_date"))
            if code in diag_spec["codes"] and d and (index_date - d).days >= -window:
                hits.append((s, n, code, d))
        if not hits:
            if not data.by_kind("diagnosis"):
                reasons.append("diagnosis_missing")
                return INSUFFICIENT, reasons, evidence
            reasons.append("required_diagnosis_absent:" + ",".join(diag_spec["codes"]))
            return INELIGIBLE, reasons, evidence
        s, n, code, d = sorted(hits, key=lambda h: (h[3], h[0].id))[0]
        evidence.append(
            {"source_record_id": s.id, "record_type": "diagnosis", "reason": f"diagnosis:{code}"}
        )
        reasons.append(f"diagnosis_ok:{code}")

    # Required lab in window
    lab_spec = inc.get("lab")
    if lab_spec and lab_spec.get("code"):
        before = int(lab_spec.get("window_days_before", 30))
        after = int(lab_spec.get("window_days_after", 0))
        vals = []
        for s, n in data.by_kind("lab"):
            if (n.normalized or {}).get("code") != lab_spec["code"]:
                continue
            d = _date((n.normalized or {}).get("collected_at"))
            val = n.converted_value
            if d is None:
                continue
            if -before <= (d - index_date).days <= after and val is not None:
                vals.append((s, n, float(val), d))
        if not vals:
            reasons.append(f"lab_missing:{lab_spec['code']}")
            return INSUFFICIENT, reasons, evidence
        lo, hi = lab_spec.get("min"), lab_spec.get("max")
        in_range = [
            v for v in vals if (lo is None or v[2] >= float(lo)) and (hi is None or v[2] <= float(hi))
        ]
        if not in_range:
            pick = sorted(vals, key=lambda v: (abs((v[3] - index_date).days), v[0].id))[0]
            s, n, val, d = pick
            evidence.append(
                {
                    "source_record_id": s.id,
                    "record_type": "lab",
                    "reason": (
                        f"lab_out_of_range:{lab_spec['code']}={val}{n.study_unit}"
                    ),
                }
            )
            reasons.append(
                f"lab_out_of_range:{lab_spec['code']}:{val}{lab_spec.get('study_unit', '')}"
            )
            return INELIGIBLE, reasons, evidence
        pick = sorted(in_range, key=lambda v: (abs((v[3] - index_date).days), v[0].id))[0]
        s, n, val, d = pick
        evidence.append(
            {
                "source_record_id": s.id,
                "record_type": "lab",
                "reason": f"lab:{lab_spec['code']}={val}{n.study_unit}",
            }
        )
        reasons.append(f"lab_ok:{lab_spec['code']}")

    # Required medication
    med_spec = inc.get("medication")
    if med_spec and med_spec.get("codes"):
        after = int(med_spec.get("within_days_after_index", 30))
        meds = []
        for s, n in data.by_kind("medication"):
            code = (n.normalized or {}).get("code")
            d = _date((n.normalized or {}).get("start_date"))
            if code in med_spec["codes"] and d and -1 <= (d - index_date).days <= after:
                meds.append((s, n, code, d))
        if not meds:
            if not data.by_kind("medication"):
                reasons.append("medication_missing")
                return INSUFFICIENT, reasons, evidence
            reasons.append("required_medication_absent:" + ",".join(med_spec["codes"]))
            return INELIGIBLE, reasons, evidence
        s, n, code, d = sorted(meds, key=lambda m: (m[3], m[0].id))[0]
        evidence.append(
            {"source_record_id": s.id, "record_type": "medication", "reason": f"medication:{code}"}
        )
        reasons.append(f"medication_ok:{code}")

    # Exclusions
    exc_diag = exc.get("diagnosis")
    if exc_diag and exc_diag.get("codes"):
        for s, n in data.by_kind("diagnosis"):
            code = (n.normalized or {}).get("code")
            if code in exc_diag["codes"]:
                evidence.append(
                    {
                        "source_record_id": s.id,
                        "record_type": "diagnosis",
                        "reason": f"exclusion_diagnosis:{code}",
                    }
                )
                reasons.append(f"exclusion_diagnosis:{code}")
                return INELIGIBLE, reasons, evidence

    exc_lab = exc.get("lab")
    if exc_lab and exc_lab.get("code"):
        window = int(exc_lab.get("window_days_before", 30))
        for s, n in data.by_kind("lab"):
            if (n.normalized or {}).get("code") != exc_lab["code"] or n.converted_value is None:
                continue
            d = _date((n.normalized or {}).get("collected_at"))
            if d and -window <= (d - index_date).days <= 0:
                val = float(n.converted_value)
                if exc_lab.get("min") is not None and val >= float(exc_lab["min"]):
                    evidence.append(
                        {
                            "source_record_id": s.id,
                            "record_type": "lab",
                            "reason": f"exclusion_lab:{exc_lab['code']}={val}",
                        }
                    )
                    reasons.append(f"exclusion_lab:{exc_lab['code']}")
                    return INELIGIBLE, reasons, evidence
                if exc_lab.get("max") is not None and val <= float(exc_lab["max"]):
                    evidence.append(
                        {
                            "source_record_id": s.id,
                            "record_type": "lab",
                            "reason": f"exclusion_lab:{exc_lab['code']}={val}",
                        }
                    )
                    reasons.append(f"exclusion_lab:{exc_lab['code']}")
                    return INELIGIBLE, reasons, evidence

    exc_med = exc.get("medication")
    if exc_med and exc_med.get("codes"):
        for s, n in data.by_kind("medication"):
            code = (n.normalized or {}).get("code")
            if code in exc_med["codes"]:
                evidence.append(
                    {
                        "source_record_id": s.id,
                        "record_type": "medication",
                        "reason": f"exclusion_medication:{code}",
                    }
                )
                reasons.append(f"exclusion_medication:{code}")
                return INELIGIBLE, reasons, evidence

    return INCLUDED, reasons, evidence


def _fingerprint(rules: dict, data: SubjectData, identity_pending: bool) -> str:
    material = {
        "rules": rules_hash(rules),
        "identity_pending": identity_pending,
        "records": sorted(
            [
                {
                    "id": s.id,
                    "uid": s.source_uid,
                    "rev": s.revision,
                    "type": s.record_type,
                    "hash": s.payload_hash,
                    "status": n.status,
                    "code": n.study_code,
                    "value": str(n.converted_value) if n.converted_value is not None else None,
                    "unit": n.study_unit,
                    "date": n.effective_date.isoformat() if n.effective_date else None,
                    "norm": n.normalized,
                }
                for s, n in data.records
            ],
            key=lambda r: (r["id"], r["rev"]),
        ),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# run execution
# ---------------------------------------------------------------------------
def affected_subject_ids(
    db: Session,
    base_run: ScreeningRun,
    base_batch: CutoffBatch,
    batch: CutoffBatch,
) -> set[int]:
    """Transitive closure of subjects touched by changes since the base run."""
    affected: set[int] = set()
    base_upload_ids = set(base_batch.upload_ids or [])
    new_upload_ids = [u for u in (batch.upload_ids or []) if u not in base_upload_ids]
    if new_upload_ids:
        rows = db.execute(
            select(NormalizedRecord.subject_id)
            .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
            .where(
                SourceRecord.upload_id.in_(new_upload_ids),
                NormalizedRecord.subject_id.is_not(None),
            )
        ).all()
        affected.update(r[0] for r in rows if r[0])

    # Mappings written after the base run invalidate every subject that has a
    # record of the affected kind/source code in that center.
    changed = db.scalars(
        select(CodeMapping).where(CodeMapping.created_at >= base_run.started_at)
    ).all()
    for mapping in changed:
        rows = db.execute(
            select(NormalizedRecord.subject_id)
            .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
            .where(
                NormalizedRecord.center_id == mapping.center_id,
                NormalizedRecord.kind == mapping.kind,
                NormalizedRecord.source_code == mapping.source_code,
                NormalizedRecord.subject_id.is_not(None),
            )
        ).all()
        affected.update(r[0] for r in rows if r[0])

    # Identity adjudication touches both sides of the candidate and anything
    # provisionally merged into them.
    decided = db.scalars(
        select(IdentityCandidate).where(IdentityCandidate.decided_at >= base_run.started_at)
    ).all()
    for cand in decided:
        for sid in (cand.subject_a_id, cand.subject_b_id):
            affected.add(sid)
            affected.update(_merged_family(db, sid))

    # A newly created candidate makes both subjects pending adjudication, even
    # though only one of them received the new row. This is the indirect effect
    # a naive "changed rows" scan would miss.
    created = db.scalars(
        select(IdentityCandidate).where(IdentityCandidate.created_at >= base_run.started_at)
    ).all()
    for cand in created:
        for sid in (cand.subject_a_id, cand.subject_b_id):
            affected.add(sid)
            affected.update(_merged_family(db, sid))
    return affected


def _merged_family(db: Session, subject_id: int) -> set[int]:
    out = {subject_id}
    changed = True
    while changed:
        changed = False
        children = db.scalars(
            select(Subject.id).where(Subject.merged_into_subject_id.in_(out))
        ).all()
        for child in children:
            if child not in out:
                out.add(child)
                changed = True
    return out


def _scope_upload_ids(db: Session, batch: CutoffBatch) -> list[int]:
    return [int(u) for u in (batch.upload_ids or [])]


def execute_run(
    db: Session,
    run: ScreeningRun,
    *,
    max_centers: int | None = None,
    crash_after_centers: int | None = None,
    progress=None,
) -> ScreeningRun:
    """Execute or resume a run.

    Per-center checkpoints mean a crash never restarts completed centers.
    """
    revision = db.get(ProtocolRevision, run.protocol_revision_id)
    batch = db.get(CutoffBatch, run.cutoff_batch_id)
    rules = revision.rules or {}
    upload_ids = _scope_upload_ids(db, batch)

    base_run = db.get(ScreeningRun, run.base_run_id) if run.base_run_id else None
    base_batch = db.get(CutoffBatch, base_run.cutoff_batch_id) if base_run else None
    incremental = run.strategy == "incremental" and base_run is not None and base_batch is not None

    if not run.started_at:
        run.started_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
    run.status = "running"
    db.commit()

    centers = db.scalars(select(Center).where(Center.active.is_(True)).order_by(Center.id)).all()
    if max_centers is not None:
        centers = centers[:max_centers]

    base_decisions: dict[int, ScreeningDecision] = {}
    affected: set[int] = set()
    if incremental:
        affected = affected_subject_ids(db, base_run, base_batch, batch)
        run.affected_subjects = sorted(affected)
        for dec in db.scalars(
            select(ScreeningDecision).where(ScreeningDecision.run_id == base_run.id)
        ).all():
            base_decisions[dec.subject_id] = dec
        db.commit()

    # Pre-create a checkpoint per centre so an interrupted run's remaining work
    # is visible before any centre starts.
    for center in centers:
        if not db.scalar(
            select(ScreeningRunCenter.id).where(
                ScreeningRunCenter.run_id == run.id, ScreeningRunCenter.center_id == center.id
            )
        ):
            db.add(ScreeningRunCenter(run_id=run.id, center_id=center.id))
    db.commit()

    completed = 0
    for center in centers:
        checkpoint = db.scalar(
            select(ScreeningRunCenter).where(
                ScreeningRunCenter.run_id == run.id, ScreeningRunCenter.center_id == center.id
            )
        )
        if checkpoint.status == "completed":
            continue

        checkpoint.status = "running"
        db.commit()

        subjects = load_center_subjects(db, center.id, upload_ids)
        checkpoint.total_subjects = len(subjects)
        db.commit()

        # Recompute this center from scratch so a half-finished center is safe.
        db.execute(
            delete(ScreeningDecision).where(
                ScreeningDecision.run_id == run.id,
                ScreeningDecision.center_id == center.id,
            )
        )
        db.commit()

        reused = recomputed = 0
        for idx, subject in enumerate(subjects):
            data = load_subject_data(db, subject.id, upload_ids)
            identity_pending = pending_identity_for(db, subject)
            result = evaluate_subject(db, rules, data, identity_pending, revision.id)

            reuse = False
            prior = base_decisions.get(subject.id) if incremental else None
            # Reuse only decisions the dependency index did not flag. The
            # fingerprint check additionally guards against a missed indirect
            # dependency, but the index drives what counts as "recomputed".
            if (
                prior is not None
                and subject.id not in affected
                and prior.input_fingerprint == result["input_fingerprint"]
            ):
                reuse = True

            decision = ScreeningDecision(
                run_id=run.id,
                subject_id=subject.id,
                center_id=center.id,
                outcome=prior.outcome if reuse else result["outcome"],
                reason_codes=list(prior.reason_codes if reuse else result["reason_codes"]),
                evidence=list(prior.evidence if reuse else result["evidence"]),
                index_date=prior.index_date if reuse else result["index_date"],
                visits_merged=prior.visits_merged if reuse else result["visits_merged"],
                safety_retain=prior.safety_retain if reuse else result["safety_retain"],
                input_fingerprint=result["input_fingerprint"],
                reused=reuse,
                reused_from_run_id=base_run.id if reuse else None,
            )
            db.add(decision)
            if reuse:
                reused += 1
            else:
                recomputed += 1
            if idx % 100 == 0:
                checkpoint.processed_subjects = idx + 1
                db.commit()

        checkpoint.processed_subjects = len(subjects)
        checkpoint.status = "completed"
        run.reused_count = (run.reused_count or 0) + reused
        run.recomputed_count = (run.recomputed_count or 0) + recomputed
        db.commit()

        completed += 1
        if progress:
            progress(center.code, len(subjects), reused, recomputed)
        if crash_after_centers is not None and completed >= crash_after_centers:
            db.refresh(run)
            run.status = "partial"
            db.commit()
            raise InjectedCrash(f"injected crash after {completed} centers")

    finalize_run(db, run)
    return run


def finalize_run(db: Session, run: ScreeningRun) -> ScreeningRun:
    from .reconcile import reconcile_run

    reconcile_run(db, run)
    run.status = "completed"
    db.commit()
    return run
