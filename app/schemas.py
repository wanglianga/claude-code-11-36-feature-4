"""Shared Pydantic schemas and small helpers for the API layer."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CenterIn(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


class MappingIn(BaseModel):
    center_code: str
    kind: str
    source_code: str
    study_code: str
    note: str | None = None


class ProtocolIn(BaseModel):
    name: str
    description: str | None = None


class RevisionIn(BaseModel):
    version: str
    rules: dict[str, Any]


class UploadIn(BaseModel):
    center_code: str
    batch_code: str
    file_name: str | None = None
    records: list[dict[str, Any]]


class CutoffIn(BaseModel):
    label: str
    upload_ids: list[int]


class RunIn(BaseModel):
    protocol_revision_id: int
    cutoff_batch_id: int
    strategy: Literal["full", "incremental"] = "full"
    base_run_id: int | None = None
    trigger_type: Literal["upload", "mapping", "manual"] | None = None
    trigger_ref: str | None = None
    # Fault-injection hooks used by the tests and the demo to prove that an
    # interrupted run resumes from its per-centre checkpoints instead of
    # restarting. Refused unless COHORT_ALLOW_FAULT_INJECTION is enabled.
    max_centers: int | None = None
    crash_after_centers: int | None = None


class IdentityDecisionIn(BaseModel):
    status: Literal["confirmed", "rejected"]
    note: str | None = None


class AnalysisIn(BaseModel):
    kind: Literal["efficacy", "safety"] = "efficacy"


class ReportIn(BaseModel):
    analysis_run_id: int
    note: str | None = None


class ConsentWithdrawalIn(BaseModel):
    allow_retention: bool = False
    withdrawal_date: str | None = None


# ---------------------------------------------------------------------------
# Sample lineage & usability certification
# ---------------------------------------------------------------------------


class EvidenceIn(BaseModel):
    center_code: str
    evidence_type: Literal[
        "sampling_registration",
        "container_barcode",
        "aliquot",
        "merge",
        "handover",
        "inventory",
        "lab_report",
        "temperature_series",
    ]
    evidence_uid: str = Field(min_length=1, max_length=160)
    # When the event actually happened (ISO-8601, issuer's clock).
    occurred_at: str
    issued_by: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)


class ConstraintIn(BaseModel):
    assay_purpose: str = Field(min_length=1, max_length=60)
    protocol_revision_id: int | None = None
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    max_duration_hours: float | None = None
    max_aliquot_count: int | None = None
    min_remaining_volume_ul: float | None = None
    clock_skew_tolerance_seconds: int = 300
    requires_event_graph: bool = False
    exception_rules: dict[str, Any] = Field(default_factory=dict)


class MislabelIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class PoolSplitIn(BaseModel):
    children: list[dict[str, Any]]
    # Measured per-child volumes / aliquot quality proof. Without it a pool
    # stays irreversible and the split is refused.
    volume_evidence: dict[str, Any] | None = None


class ResolveIssueIn(BaseModel):
    keep_relation_ids: list[int]
    note: str | None = None


class CohortRunRefIn(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    screening_run_id: int | None = None


class SelectReferencesIn(BaseModel):
    certification_ids: list[int]


class ReadjudicateIn(BaseModel):
    assay_purpose: str | None = None
    report_uid: str | None = None
