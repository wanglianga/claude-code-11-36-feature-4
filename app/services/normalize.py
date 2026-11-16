"""Normalization of hospital source rows.

Every source row yields exactly one ``normalized_records`` row whose ``status``
is ``ok``, ``pending`` or ``failed``. That one-to-one mapping is what makes the
per-center reconciliation identity hold:

    raw_rows == normalized_ok + pending + failed

Nothing is silently dropped: an unknown code, an unconvertible unit or an
ambiguous date keeps the row, the original text and the reason it could not be
standardized.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Center,
    CodeMapping,
    NormalizedRecord,
    SourceRecord,
    Subject,
    SubjectDependency,
    Upload,
)

log = logging.getLogger(__name__)

RECORD_TYPES = {"case", "diagnosis", "lab", "medication", "followup", "consent"}

LAB_CONVERSIONS = {
    # study_code: canonical unit -> rule for converting each source unit.
    # Each rule returns the canonical value.
    "CREATININE": {
        "canonical": "mg/dL",
        "units": {
            "mg/dl": ("mg/dL", lambda v: v),
            "umol/l": ("mg/dL", lambda v: v / 88.4),
            "µmol/l": ("mg/dL", lambda v: v / 88.4),
            "μmol/l": ("mg/dL", lambda v: v / 88.4),
        },
    },
    "GLUCOSE": {
        "canonical": "mg/dL",
        "units": {
            "mg/dl": ("mg/dL", lambda v: v),
            "mmol/l": ("mg/dL", lambda v: v * 18.0182),
        },
    },
    "HBA1C": {
        "canonical": "%",
        "units": {
            "%": ("%", lambda v: v),
            "mmol/mol": ("%", lambda v: 0.09148 * v + 2.152),
        },
    },
    "HEMOGLOBIN": {
        "canonical": "g/dL",
        "units": {
            "g/dl": ("g/dL", lambda v: v),
            "g/l": ("g/dL", lambda v: v / 10.0),
        },
    },
    "ALT": {
        "canonical": "U/L",
        "units": {"u/l": ("U/L", lambda v: v), "iu/l": ("U/L", lambda v: v)},
    },
}

DATE_FORMATS_FALLBACK = ["%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d"]


class NormalizationError(Exception):
    def __init__(self, issue: str, detail: str | None = None):
        super().__init__(issue)
        self.issue = issue
        self.detail = detail


def subject_key(center: Center, patient_ref: str) -> str:
    raw = f"{settings.subject_salt}:{center.id}:{patient_ref.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_date(value, formats: list[str] | None) -> tuple[date | None, list[str]]:
    """Return (date, issues). Ambiguity is reported, not guessed."""
    if value is None or value == "":
        return None, ["missing_date"]
    if isinstance(value, datetime):
        return value.date(), []
    if isinstance(value, date):
        return value, []
    text = str(value).strip()
    formats = formats or DATE_FORMATS_FALLBACK
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date(), []
        except ValueError:
            continue
    return None, [f"unparseable_date:{text}"]


def parse_number(value) -> tuple[Decimal | None, list[str]]:
    if value is None or value == "":
        return None, ["missing_value"]
    text = str(value).strip().replace(" ", "")
    # European decimal comma is accepted only when it is unambiguous.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text), []
    except InvalidOperation:
        return None, [f"unparseable_number:{value}"]


def convert_lab(code: str, value: Decimal, unit: str | None) -> dict:
    table = LAB_CONVERSIONS.get(code)
    if not table:
        return {
            "status": "pending",
            "issues": [f"no_conversion_rule:{code}"],
            "converted": None,
            "study_unit": None,
            "conversion": None,
        }
    if not unit:
        return {
            "status": "pending",
            "issues": [f"missing_unit:{code}"],
            "converted": None,
            "study_unit": table["canonical"],
            "conversion": None,
        }
    key = unit.strip().lower().replace("μ", "µ")
    # normalise micro sign variants to one lookup key
    key = key.replace("μ", "µ")
    rule = table["units"].get(key)
    if not rule:
        return {
            "status": "pending",
            "issues": [f"unconvertible_unit:{code}:{unit}"],
            "converted": None,
            "study_unit": table["canonical"],
            "conversion": None,
        }
    study_unit, fn = rule
    converted = Decimal(str(fn(float(value)))).quantize(Decimal("0.000001"))
    return {
        "status": "ok",
        "issues": [],
        "converted": converted,
        "study_unit": study_unit,
        "conversion": {
            "source_unit": unit,
            "study_unit": study_unit,
            "factor": "formula" if study_unit != unit.strip() else 1.0,
            "rule": f"{code}:{unit}->{study_unit}",
        },
    }


def latest_mappings(db: Session, center_id: int) -> dict[tuple[str, str], CodeMapping]:
    """Latest mapping per (kind, source pattern).

    Hospital systems are inconsistent about code case, so lookup is exact first
    and case-insensitive second.
    """
    rows = db.scalars(
        select(CodeMapping).where(CodeMapping.center_id == center_id).order_by(CodeMapping.version)
    ).all()
    out: dict[tuple[str, str], CodeMapping] = {}
    for row in rows:
        out[(row.kind, str(row.source_code).strip())] = row
        out.setdefault((row.kind, str(row.source_code).strip().casefold()), row)
    return out


def map_code(mappings, kind: str, source_code: str | None, issues: list[str]) -> str | None:
    if source_code is None or str(source_code).strip() == "":
        issues.append(f"missing_code:{kind}")
        return None
    key = str(source_code).strip()
    mapping = mappings.get((kind, key)) or mappings.get((kind, key.casefold()))
    if not mapping:
        issues.append(f"unmapped_code:{kind}:{key}")
        return None
    return mapping.study_code


def resolve_subject(db: Session, center_id: int, patient_ref: str | None) -> Subject | None:
    if not patient_ref:
        return None
    key = subject_key(db.get(Center, center_id), patient_ref)
    subject = db.scalar(
        select(Subject).where(Subject.center_id == center_id, Subject.patient_key_hash == key)
    )
    seen = set()
    while subject and subject.merged_into_subject_id and subject.id not in seen:
        seen.add(subject.id)
        subject = db.get(Subject, subject.merged_into_subject_id)
    return subject


def get_or_create_subject(db: Session, center: Center, patient_ref: str) -> Subject:
    subject = resolve_subject(db, center.id, patient_ref)
    if subject:
        return subject
    existing = db.scalar(
        select(Subject).where(
            Subject.center_id == center.id,
            Subject.patient_key_hash == subject_key(center, patient_ref),
        )
    )
    if existing:
        return existing
    subject = Subject(
        study_id=_study_id(db),
        center_id=center.id,
        patient_key_hash=subject_key(center, patient_ref),
    )
    db.add(subject)
    db.flush()
    return subject


def _study_id(db: Session) -> str:
    import secrets

    while True:
        candidate = "S-" + secrets.token_hex(4).upper()
        if not db.scalar(select(Subject.id).where(Subject.study_id == candidate)):
            return candidate


def normalize_source_record(
    db: Session,
    source: SourceRecord,
    center: Center,
    mappings,
    *,
    persist: bool = True,
) -> tuple[NormalizedRecord | None, Subject | None]:
    """Standardize one source row.

    Called at ingest time with ``persist=True`` (one normalized row per source
    row) and again from the mapping-correction path with ``persist=False`` so a
    corrected code mapping can be re-applied without duplicating the row.
    """
    payload = source.payload or {}
    kind = source.record_type
    issues: list[str] = []
    normalized: dict = {}
    effective: date | None = None
    value_num = None
    converted = None
    study_unit = None
    conversion = None
    source_code = None
    study_code = None
    status = "ok"
    subject: Subject | None = None

    formats = (center.config or {}).get("date_formats")

    if kind == "case":
        patient_ref = payload.get("patient_ref")
        if not patient_ref:
            issues.append("missing_patient_ref")
        else:
            subject = get_or_create_subject(db, center, str(patient_ref))
        sex_code = payload.get("sex")
        sex = map_code(mappings, "sex", sex_code, issues)
        birth, birth_issues = parse_date(payload.get("birth_date"), formats)
        issues.extend(birth_issues)
        first_visit, fv_issues = parse_date(payload.get("first_visit_date"), formats)
        issues.extend(fv_issues)
        effective = first_visit or birth
        normalized = {
            "sex": sex,
            "birth_date": birth.isoformat() if birth else None,
            "first_visit_date": first_visit.isoformat() if first_visit else None,
            "treatment_code": payload.get("treatment_code"),
            "enrollment_date": (parse_date(payload.get("enrollment_date"), formats)[0] or None),
        }
        if normalized["enrollment_date"]:
            normalized["enrollment_date"] = normalized["enrollment_date"].isoformat()
        if subject:
            subject.family_initial = (payload.get("family_initial") or subject.family_initial or None)
            subject.given_initial = (payload.get("given_initial") or subject.given_initial or None)
            subject.birth_date = birth or subject.birth_date
            initialise_candidates(db, center, subject)

    elif kind == "diagnosis":
        source_code = payload.get("code")
        study_code = map_code(mappings, "diagnosis", source_code, issues)
        effective, date_issues = parse_date(payload.get("diagnosis_date"), formats)
        issues.extend(date_issues)
        normalized = {
            "code": study_code,
            "source_code": source_code,
            "diagnosis_date": effective.isoformat() if effective else None,
        }
        subject = resolve_subject(db, center.id, payload.get("patient_ref"))
        if subject is None:
            issues.append("subject_not_found")

    elif kind == "lab":
        source_code = payload.get("code")
        study_code = map_code(mappings, "lab", source_code, issues)
        value_num, v_issues = parse_number(payload.get("value"))
        issues.extend(v_issues)
        conv = {"status": "pending", "issues": [], "converted": None, "study_unit": None, "conversion": None}
        if study_code and value_num is not None:
            conv = convert_lab(study_code, value_num, payload.get("unit"))
            issues.extend(conv["issues"])
        elif value_num is not None:
            issues.append("lab_code_unmapped")
        converted = conv["converted"]
        study_unit = conv["study_unit"]
        conversion = conv["conversion"]
        effective, date_issues = parse_date(payload.get("collected_at"), formats)
        issues.extend(date_issues)
        normalized = {
            "code": study_code,
            "value": str(value_num) if value_num is not None else None,
            "study_value": float(converted) if converted is not None else None,
            "study_unit": study_unit,
            "collected_at": effective.isoformat() if effective else None,
        }
        subject = resolve_subject(db, center.id, payload.get("patient_ref"))
        if subject is None:
            issues.append("subject_not_found")

    elif kind == "medication":
        source_code = payload.get("code")
        study_code = map_code(mappings, "medication", source_code, issues)
        effective, date_issues = parse_date(payload.get("start_date"), formats)
        issues.extend(date_issues)
        normalized = {
            "code": study_code,
            "start_date": effective.isoformat() if effective else None,
            "dose": payload.get("dose"),
            "dose_unit": payload.get("dose_unit"),
        }
        subject = resolve_subject(db, center.id, payload.get("patient_ref"))
        if subject is None:
            issues.append("subject_not_found")

    elif kind == "followup":
        source_code = payload.get("response")
        study_code = map_code(mappings, "response", source_code, issues) if source_code else None
        effective, date_issues = parse_date(payload.get("visit_date"), formats)
        issues.extend(date_issues)
        normalized = {
            "visit_date": effective.isoformat() if effective else None,
            "response": study_code,
            "visit_type": payload.get("visit_type", "scheduled"),
        }
        subject = resolve_subject(db, center.id, payload.get("patient_ref"))
        if subject is None:
            issues.append("subject_not_found")

    elif kind == "consent":
        raw_status = str(payload.get("status") or "").strip().lower()
        consent = map_code(mappings, "consent", raw_status, issues)
        withdrawal_date = None
        if consent == "withdrawn":
            withdrawal_date, wd_issues = parse_date(payload.get("withdrawal_date"), formats)
            issues.extend(wd_issues)
            effective = withdrawal_date
        normalized = {
            "consent": consent,
            "withdrawal_date": withdrawal_date.isoformat() if withdrawal_date else None,
            "allow_retention": bool(payload.get("allow_retention", False)),
        }
        subject = resolve_subject(db, center.id, payload.get("patient_ref"))
        if subject is None:
            issues.append("subject_not_found")
    else:
        issues.append(f"unknown_record_type:{kind}")

    # An unparseable payload value is a hard failure; a code/unit we cannot map
    # is pending adjudication. Missing fields make the row pending for review.
    if any(i.startswith("unparseable_") or i.startswith("missing_") for i in issues):
        status = "failed" if any(i.startswith("unparseable_") for i in issues) else "pending"
    elif issues:
        status = "pending"

    record = NormalizedRecord(
        source_record_id=source.id,
        subject_id=subject.id if subject else None,
        center_id=center.id,
        kind=kind,
        source_code=str(source_code) if source_code is not None else None,
        study_code=study_code,
        status=status,
        issues=issues,
        original_value=str(payload.get("value")) if payload.get("value") is not None else None,
        original_unit=payload.get("unit"),
        study_unit=study_unit,
        value_num=value_num,
        converted_value=converted,
        conversion=conversion,
        effective_date=effective,
        normalized=normalized,
        created_at=datetime.now().astimezone(),
    )
    if not persist:
        return record, subject
    db.add(record)
    db.flush()
    if subject:
        exists = db.scalar(
            select(SubjectDependency.id).where(
                SubjectDependency.subject_id == subject.id,
                SubjectDependency.source_record_id == source.id,
            )
        )
        if not exists:
            db.add(SubjectDependency(subject_id=subject.id, source_record_id=source.id))
    return record, subject


def initialise_candidates(db: Session, center: Center, subject: Subject) -> None:
    """Flag likely duplicates for human adjudication; never auto-merge."""
    if not (subject.family_initial and subject.given_initial and subject.birth_date):
        return
    from ..models import IdentityCandidate

    others = db.scalars(
        select(Subject).where(
            Subject.center_id == center.id,
            Subject.id != subject.id,
            Subject.merged_into_subject_id.is_(None),
            Subject.family_initial == subject.family_initial,
            Subject.given_initial == subject.given_initial,
            Subject.birth_date == subject.birth_date,
        )
    ).all()
    for other in others:
        a, b = sorted([subject.id, other.id])
        exists = db.scalar(
            select(IdentityCandidate.id).where(
                IdentityCandidate.subject_a_id == a,
                IdentityCandidate.subject_b_id == b,
                IdentityCandidate.status == "pending",
            )
        )
        if not exists:
            db.add(
                IdentityCandidate(
                    subject_a_id=a,
                    subject_b_id=b,
                    evidence={
                        "center_id": center.id,
                        "reason": "same_initials_and_birth_date",
                        "auto_merged": False,
                    },
                    status="pending",
                )
            )


def ingest_upload(db: Session, upload: Upload, max_rows: int | None = None) -> dict:
    """Read rows from an upload, append source records and normalize them.

    Resumable: ``upload.last_row_no`` records the last committed source row so a
    crash mid-file continues where it stopped. Rows are treated as immutable
    once committed.
    """
    center = db.get(Center, upload.center_id)
    payload = upload.payload or {}
    rows = payload.get("records") or []
    upload.total_rows = len(rows)
    mappings = latest_mappings(db, center.id)
    start = upload.last_row_no
    end = len(rows) if max_rows is None else min(len(rows), start + max_rows)
    normalized = 0

    for idx in range(start, end):
        raw = rows[idx]
        row_no = idx + 1
        record_type = str(raw.get("record_type", "")).strip().lower()
        payload_hash = hashlib.sha256(
            json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        source_uid = raw.get("source_uid")
        patient_ref = raw.get("patient_ref")
        if not source_uid:
            seed = f"{record_type}:{patient_ref}:{raw.get('visit_date') or raw.get('collected_at') or raw.get('diagnosis_date') or ''}"
            source_uid = f"{record_type.upper()}-" + hashlib.sha256(seed.encode()).hexdigest()[:16]

        prior = db.scalars(
            select(SourceRecord)
            .where(SourceRecord.center_id == center.id, SourceRecord.source_uid == source_uid)
            .order_by(SourceRecord.revision.desc())
        ).first()

        revision = 1
        active = True
        supersedes_id = None
        if prior:
            revision = prior.revision + 1
            if prior.payload_hash == payload_hash and prior.record_type == record_type:
                # Byte-identical re-send: keep it in the audit trail but never
                # let it become the active version.
                active = False
                supersedes_id = None
            else:
                prior.active = False
                supersedes_id = prior.id

        source = SourceRecord(
            upload_id=upload.id,
            center_id=center.id,
            source_uid=str(source_uid),
            revision=revision,
            record_type=record_type if record_type in RECORD_TYPES else "unknown",
            source_row_no=row_no,
            payload=raw,
            payload_hash=payload_hash,
            active=active,
            supersedes_id=supersedes_id,
        )
        db.add(source)
        db.flush()

        record, _ = normalize_source_record(db, source, center, mappings)
        if not active:
            # A byte-identical re-send is normalized (so row conservation holds)
            # but documented as a duplicate that must not be counted twice.
            record.issues = list(record.issues) + ["duplicate_of_existing_revision"]
        normalized += 1
        upload.last_row_no = row_no
        upload.processed_rows = row_no
        if idx % 200 == 0:
            db.commit()

    upload.processed_rows = upload.last_row_no
    if upload.last_row_no >= len(rows):
        upload.status = "completed"
        upload.processed_at = datetime.now().astimezone()
    elif upload.last_row_no > 0:
        upload.status = "partial"
    else:
        upload.status = "received"
    db.commit()
    return {
        "upload_id": upload.id,
        "status": upload.status,
        "processed_rows": upload.processed_rows,
        "total_rows": upload.total_rows,
        "normalized_rows": normalized,
        "next_row": upload.last_row_no,
    }


def summarize_normalization(db: Session, center_id: int, upload_ids: list[int]) -> dict:
    """Row counts per normalization status for the given uploads."""
    if not upload_ids:
        return {"raw_rows": 0, "ok": 0, "pending": 0, "failed": 0}
    rows = db.execute(
        select(NormalizedRecord.status, func.count())
        .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
        .where(SourceRecord.center_id == center_id, SourceRecord.upload_id.in_(upload_ids))
        .group_by(NormalizedRecord.status)
    ).all()
    counts = {status: int(n) for status, n in rows}
    raw = db.scalar(
        select(func.count(SourceRecord.id)).where(
            SourceRecord.center_id == center_id, SourceRecord.upload_id.in_(upload_ids)
        )
    )
    return {
        "raw_rows": int(raw or 0),
        "ok": counts.get("ok", 0),
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
    }


@dataclass
class MappingRemediation:
    """Outcome of re-standardizing the source rows a mapping covers."""

    mapping_id: int
    center_code: str
    kind: str
    source_code: str
    study_code: str
    dry_run: bool
    matched_rows: int
    fixed_rows: int
    still_pending: int
    affected_subjects: list[int]
    affected_uploads: list[int]


def source_code_for(source: SourceRecord) -> str | None:
    """The hospital-local code a source row carries, before mapping."""
    payload = source.payload or {}
    kind = source.record_type
    if kind in ("lab", "diagnosis", "medication"):
        return _as_text(payload.get("code"))
    if kind == "consent":
        return _as_text(payload.get("status"))
    if kind == "followup":
        return _as_text(payload.get("response"))
    if kind == "case":
        return _as_text(payload.get("sex"))
    return None


def _as_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping_key(value: str | None) -> str:
    return (value or "").strip().casefold()


def _subject_family(db: Session, subject_id: int) -> set[int]:
    """A subject plus anyone provisionally merged with them."""
    out = {subject_id}
    root = db.get(Subject, subject_id)
    seen = set()
    while root and root.merged_into_subject_id and root.id not in seen:
        seen.add(root.id)
        out.add(root.id)
        root = db.get(Subject, root.merged_into_subject_id)
    if root:
        out.add(root.id)
    for child in db.scalars(
        select(Subject.id).where(Subject.merged_into_subject_id.in_(out))
    ).all():
        out.add(child)
    return out


def apply_mapping(db: Session, mapping: CodeMapping, dry_run: bool = True) -> MappingRemediation:
    """Re-standardize every active source row a new or corrected mapping covers.

    Source rows are never modified: only derived normalized values change, so
    the original hospital coding remains in the audit trail. A subsequent
    screening run picks the corrections up.
    """
    center = db.get(Center, mapping.center_id)
    kind = mapping.kind
    wanted = _mapping_key(mapping.source_code)

    record_types = {
        "sex": ["case"],
        "diagnosis": ["diagnosis"],
        "medication": ["medication"],
        "lab": ["lab"],
        "consent": ["consent"],
        "response": ["followup"],
    }.get(kind, [])
    if not record_types:
        raise NormalizationError(f"unknown_mapping_kind:{kind}")

    sources = db.scalars(
        select(SourceRecord).where(
            SourceRecord.center_id == center.id,
            SourceRecord.active.is_(True),
            SourceRecord.record_type.in_(record_types),
        )
    ).all()
    matching = [s for s in sources if _mapping_key(source_code_for(s)) == wanted]

    mappings = latest_mappings(db, center.id)
    affected_subjects: set[int] = set()
    upload_ids: set[int] = set()
    fixed = still_pending = 0

    for source in matching:
        record = db.scalar(
            select(NormalizedRecord).where(NormalizedRecord.source_record_id == source.id)
        )
        if record is None:
            continue
        recomputed, subject = normalize_source_record(db, source, center, mappings, persist=False)
        if recomputed is None:
            continue
        if recomputed.status == "ok":
            fixed += 1
        else:
            still_pending += 1
        anchor = subject.id if subject else record.subject_id
        if anchor:
            affected_subjects.update(_subject_family(db, anchor))
        upload_ids.add(source.upload_id)
        if not dry_run:
            record.subject_id = recomputed.subject_id or record.subject_id
            record.study_code = recomputed.study_code
            record.status = recomputed.status
            record.issues = recomputed.issues
            record.study_unit = recomputed.study_unit
            record.value_num = recomputed.value_num
            record.converted_value = recomputed.converted_value
            record.conversion = recomputed.conversion
            record.effective_date = recomputed.effective_date
            record.normalized = recomputed.normalized

    if not dry_run:
        db.commit()

    return MappingRemediation(
        mapping_id=mapping.id,
        center_code=center.code,
        kind=kind,
        source_code=mapping.source_code,
        study_code=mapping.study_code,
        dry_run=dry_run,
        matched_rows=len(matching),
        fixed_rows=fixed,
        still_pending=still_pending,
        affected_subjects=sorted(affected_subjects),
        affected_uploads=sorted(upload_ids),
    )
