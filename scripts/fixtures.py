"""Fictional multi-centre sample data.

Every patient, code and identifier here is invented. The data deliberately
exercises:

* three hospitals with different local coding, date formats and lab units
* a creatinine value exactly on the inclusion boundary in µmol/L (converted)
* a haemoglobin value exactly on the exclusion boundary in g/L (converted)
* repeat visits on the index date (merged)
* a withdrawn consent with and without retention permission
* an unmapped medication code (pending adjudication)
* an unparseable date (failed normalization)
* two patient references that look like the same person (candidate only)
* a late upload and a revised lab result (amendment keeping the old revision)
"""
from __future__ import annotations

from datetime import date

CENTER_SPECS = {
    "NORTH": {
        "name": "North Teaching Hospital (fictional)",
        "config": {"date_formats": ["%Y-%m-%d"]},
        "date": lambda d: d.isoformat(),
        "sex": {"M": "M", "F": "F"},
        "dx": {"T2DM": "T2DM", "T1DM": "T1DM", "ADV": "ADV"},
        "lab": {"CREATININE": "CREA", "HEMOGLOBIN": "HGB", "HBA1C": "A1C"},
        "med": {"METFORMIN": "MET", "OTHER": "OTH"},
        "consent": {"granted": "Y", "withdrawn": "N", "declined": "D"},
        "resp": {"CR": "CR", "PR": "PR", "PD": "PD"},
    },
    "SOUTH": {
        "name": "South General Hospital (fictional)",
        "config": {"date_formats": ["%d/%m/%Y"]},
        "date": lambda d: d.strftime("%d/%m/%Y"),
        "sex": {"M": "1", "F": "2"},
        "dx": {"T2DM": "E11", "T1DM": "E10", "ADV": "RARE"},
        "lab": {"CREATININE": "KREA", "HEMOGLOBIN": "HB", "HBA1C": "A1C"},
        "med": {"METFORMIN": "M-01", "OTHER": "M-02"},
        "consent": {"granted": "granted", "withdrawn": "withdrawn", "declined": "declined"},
        "resp": {"CR": "1", "PR": "2", "PD": "3"},
    },
    "EAST": {
        "name": "East Community Hospital (fictional)",
        "config": {"date_formats": ["%d.%m.%Y"]},
        "date": lambda d: d.strftime("%d.%m.%Y"),
        "sex": {"M": "男", "F": "女"},
        "dx": {"T2DM": "T2DM", "T1DM": "T1DM", "ADV": "ADV"},
        "lab": {"CREATININE": "SCR", "HEMOGLOBIN": "HGB", "HBA1C": "A1C"},
        "med": {"METFORMIN": "BIG", "OTHER": "OTH"},
        "consent": {"granted": "同意", "withdrawn": "撤回", "declined": "拒绝"},
        "resp": {"CR": "good", "PR": "ok", "PD": "poor"},
    },
}

PROTOCOL_RULES = {
    "index_date": {
        "basis": "first_visit",
        "merge_visit_types": ["screening", "baseline"],
    },
    "consent": {"required": True},
    "inclusion": {
        "age_min": 18,
        "age_max": 75,
        "diagnosis": {"codes": ["T2DM"], "window_days": 3650},
        "lab": {
            "code": "CREATININE",
            "study_unit": "mg/dL",
            "min": 0.4,
            "max": 2.0,
            "window_days_before": 30,
            "window_days_after": 0,
        },
        "medication": {"codes": ["METFORMIN"], "within_days_after_index": 30},
    },
    "exclusion": {
        "diagnosis": {"codes": ["ADV"]},
        "lab": {"code": "HEMOGLOBIN", "max": 8.0, "window_days_before": 30},
    },
    "follow_up": {
        "visits": [
            {"day": 30, "kind": "safety"},
            {"day": 90, "kind": "efficacy"},
            {"day": 180, "kind": "efficacy"},
        ]
    },
    "safety": {"allow_retention_after_withdrawal": True},
}


def _records(center: str, ref: str, **kw) -> list[dict]:
    """Build the hospital rows for one patient in that centre's local encoding."""
    spec = CENTER_SPECS[center]
    fmt = spec["date"]
    rows: list[dict] = []
    initials = kw.get("initials", ("A", "B"))

    rows.append(
        {
            "record_type": "case",
            "source_uid": f"{center}-{ref}-CASE",
            "patient_ref": ref,
            "family_initial": initials[0],
            "given_initial": initials[1],
            "sex": spec["sex"][kw["sex"]],
            "birth_date": fmt(kw["birth"]),
            "first_visit_date": fmt(kw["first_visit"]),
            "enrollment_date": fmt(kw["first_visit"]),
            "treatment_code": kw.get("treatment", "STD"),
        }
    )
    if kw.get("diagnosis"):
        rows.append(
            {
                "record_type": "diagnosis",
                "source_uid": f"{center}-{ref}-DX",
                "patient_ref": ref,
                "code": spec["dx"][kw["diagnosis"]],
                "diagnosis_date": fmt(kw["diagnosis_date"]),
            }
        )
    for i, lab in enumerate(kw.get("labs", []), start=1):
        rows.append(
            {
                "record_type": "lab",
                "source_uid": f"{center}-{ref}-LAB-{lab['code']}-{i}",
                "patient_ref": ref,
                "code": spec["lab"][lab["code"]],
                "value": str(lab["value"]),
                "unit": lab["unit"],
                "collected_at": fmt(lab["collected_on"]),
            }
        )
    if kw.get("medication"):
        rows.append(
            {
                "record_type": "medication",
                "source_uid": f"{center}-{ref}-MED",
                "patient_ref": ref,
                "code": kw.get("medication_source") or spec["med"][kw["medication"]],
                "start_date": fmt(kw["medication_date"]),
                "dose": kw.get("dose", "500 mg"),
                "dose_unit": "mg",
            }
        )
    if kw.get("consent"):
        row = {
            "record_type": "consent",
            "source_uid": f"{center}-{ref}-CONSENT",
            "patient_ref": ref,
            "status": spec["consent"][kw["consent"]],
            "allow_retention": kw.get("allow_retention", False),
        }
        if kw.get("withdrawal_date"):
            row["withdrawal_date"] = fmt(kw["withdrawal_date"])
        rows.append(row)
    for i, visit in enumerate(kw.get("visits", []), start=1):
        rows.append(
            {
                "record_type": "followup",
                "source_uid": f"{center}-{ref}-FU-{i}",
                "patient_ref": ref,
                "visit_date": fmt(visit["on"]),
                "visit_type": visit.get("type", "screening"),
                "response": spec["resp"].get(visit.get("response", "CR")),
            }
        )
    return rows


def _n(center, ref, **kw):
    return _records(center, ref, **kw)


def north_upload() -> list[dict]:
    rows = []
    rows += _n(
        "NORTH", "P-N01", sex="F", birth=date(1970, 3, 5), first_visit=date(2024, 2, 1),
        initials=("K", "M"), diagnosis="T2DM", diagnosis_date=date(2023, 11, 1),
        labs=[{"code": "CREATININE", "value": "1.2", "unit": "mg/dL", "collected_on": date(2024, 1, 25)}],
        medication="METFORMIN", medication_date=date(2024, 2, 3), consent="granted",
        visits=[{"on": date(2024, 2, 1)}],
    )
    # Two screening visits on the same day: the protocol merges them.
    rows += _n(
        "NORTH", "P-N02", sex="M", birth=date(1965, 7, 12), first_visit=date(2024, 2, 10),
        initials=("L", "Q"), treatment="INTENSIVE",
        diagnosis="T2DM", diagnosis_date=date(2022, 5, 1),
        labs=[{"code": "CREATININE", "value": "0.9", "unit": "mg/dL", "collected_on": date(2024, 2, 5)}],
        medication="METFORMIN", medication_date=date(2024, 2, 12), consent="granted",
        visits=[
            {"on": date(2024, 2, 10), "type": "screening"},
            {"on": date(2024, 2, 10), "type": "screening"},
        ],
    )
    _n3 = _n(
        "NORTH", "P-N03", sex="F", birth=date(1940, 1, 1), first_visit=date(2024, 2, 15),
        initials=("R", "S"), diagnosis="T2DM", diagnosis_date=date(2023, 1, 5),
        labs=[{"code": "CREATININE", "value": "1.0", "unit": "mg/dL", "collected_on": date(2024, 2, 10)}],
        medication="METFORMIN", medication_date=date(2024, 2, 16), consent="granted",
        visits=[{"on": date(2024, 2, 15)}],
    )
    # Withdrawal arrives as an amendment of the consent row in the same batch.
    for row in _n3:
        if row["record_type"] == "consent":
            row["status"] = "N"
            row["withdrawal_date"] = "2024-03-01"
            row["allow_retention"] = True
    rows += _n3
    # Creatinine exactly 176.8 µmol/L == 2.000000 mg/dL: on the inclusion boundary.
    rows += _n(
        "NORTH", "P-N04", sex="M", birth=date(1980, 4, 20), first_visit=date(2024, 3, 1),
        initials=("T", "V"), treatment="INTENSIVE",
        diagnosis="T2DM", diagnosis_date=date(2023, 6, 1),
        labs=[{"code": "CREATININE", "value": "176.8", "unit": "µmol/L", "collected_on": date(2024, 2, 28)}],
        medication="METFORMIN", medication_date=date(2024, 3, 3), consent="granted",
        visits=[{"on": date(2024, 3, 1)}],
    )
    rows += _n(
        "NORTH", "P-N05", sex="M", birth=date(1955, 9, 9), first_visit=date(2024, 3, 5),
        initials=("W", "X"), diagnosis="ADV", diagnosis_date=date(2023, 9, 1),
        labs=[{"code": "CREATININE", "value": "1.1", "unit": "mg/dL", "collected_on": date(2024, 3, 1)}],
        medication="METFORMIN", medication_date=date(2024, 3, 7), consent="granted",
        visits=[{"on": date(2024, 3, 5)}],
    )
    # No creatinine in the first batch; supplied later.
    rows += _n(
        "NORTH", "P-N06", sex="F", birth=date(1990, 2, 2), first_visit=date(2024, 3, 10),
        initials=("Y", "Z"), diagnosis="T2DM", diagnosis_date=date(2023, 12, 1),
        medication="METFORMIN", medication_date=date(2024, 3, 12), consent="granted",
        visits=[{"on": date(2024, 3, 10)}],
    )
    # Creatinine 2.5 mg/dL is out of range; a corrected result arrives later.
    rows += _n(
        "NORTH", "P-N07", sex="M", birth=date(1972, 6, 6), first_visit=date(2024, 4, 1),
        initials=("C", "D"), diagnosis="T2DM", diagnosis_date=date(2023, 10, 1),
        labs=[{"code": "CREATININE", "value": "2.5", "unit": "mg/dL", "collected_on": date(2024, 3, 28)}],
        medication="METFORMIN", medication_date=date(2024, 4, 3), consent="granted",
        visits=[{"on": date(2024, 4, 1)}],
    )
    return rows


def north_amendment_upload() -> list[dict]:
    """Late correction for N-07 (same source_uid = new revision) and the missing
    creatinine for N-06 (new row)."""
    rows = [
        {
            "record_type": "lab",
            "source_uid": "NORTH-P-N07-LAB-CREATININE-1",
            "patient_ref": "P-N07",
            "code": "CREA",
            "value": "1.1",
            "unit": "mg/dL",
            "collected_at_note": "corrected after lab review",
            "collected_at": "2024-03-28",
        },
        {
            "record_type": "lab",
            "source_uid": "NORTH-P-N06-LAB-CREATININE-LATE",
            "patient_ref": "P-N06",
            "code": "CREA",
            "value": "140",
            "unit": "umol/L",
            "collected_at": "2024-03-08",
        },
    ]
    return rows


def south_upload() -> list[dict]:
    rows = []
    rows += _n(
        "SOUTH", "P-S01", sex="F", birth=date(1978, 5, 5), first_visit=date(2024, 3, 1),
        initials=("F", "G"), treatment="INTENSIVE",
        diagnosis="T2DM", diagnosis_date=date(2023, 8, 1),
        labs=[{"code": "CREATININE", "value": "200", "unit": "µmol/L", "collected_on": date(2024, 2, 26)}],
        medication="METFORMIN", medication_date=date(2024, 3, 4), consent="granted",
        visits=[{"on": date(2024, 3, 1)}],
    )
    rows += _n(
        "SOUTH", "P-S02", sex="M", birth=date(1968, 8, 8), first_visit=date(2024, 3, 5),
        initials=("H", "J"), diagnosis="T2DM", diagnosis_date=date(2023, 7, 1),
        labs=[{"code": "CREATININE", "value": "90", "unit": "umol/L", "collected_on": date(2024, 3, 2)}],
        medication="METFORMIN", medication_source="M-99", medication_date=date(2024, 3, 7),
        consent="granted", visits=[{"on": date(2024, 3, 5)}],
    )
    # Same initials and date of birth, different hospital reference: candidate only.
    rows += _n(
        "SOUTH", "P-S03", sex="F", birth=date(1985, 12, 12), first_visit=date(2024, 3, 10),
        initials=("Z", "L"), diagnosis="T2DM", diagnosis_date=date(2023, 2, 1),
        labs=[{"code": "CREATININE", "value": "80", "unit": "µmol/L", "collected_on": date(2024, 3, 6)}],
        medication="METFORMIN", medication_date=date(2024, 3, 11), consent="granted",
        visits=[{"on": date(2024, 3, 10)}],
    )
    rows += _n(
        "SOUTH", "P-S03B", sex="F", birth=date(1985, 12, 12), first_visit=date(2024, 3, 12),
        initials=("Z", "L"), diagnosis="T2DM", diagnosis_date=date(2023, 2, 1),
        labs=[{"code": "CREATININE", "value": "82", "unit": "µmol/L", "collected_on": date(2024, 3, 8)}],
        medication="METFORMIN", medication_date=date(2024, 3, 13), consent="granted",
        visits=[{"on": date(2024, 3, 12)}],
    )
    rows += _n(
        "SOUTH", "P-S04", sex="M", birth=date(2010, 1, 1), first_visit=date(2024, 3, 15),
        initials=("N", "P"), diagnosis="T2DM", diagnosis_date=date(2023, 4, 1),
        labs=[{"code": "CREATININE", "value": "70", "unit": "µmol/L", "collected_on": date(2024, 3, 12)}],
        medication="METFORMIN", medication_date=date(2024, 3, 16), consent="granted",
        visits=[{"on": date(2024, 3, 15)}],
    )
    # Impossible calendar date: the case row cannot be standardized.
    s05 = _n(
        "SOUTH", "P-S05", sex="M", birth=date(1975, 1, 1), first_visit=date(2024, 3, 18),
        initials=("Q", "R"), diagnosis="T2DM", diagnosis_date=date(2023, 5, 1),
        labs=[{"code": "CREATININE", "value": "95", "unit": "µmol/L", "collected_on": date(2024, 3, 14)}],
        medication="METFORMIN", medication_date=date(2024, 3, 19), consent="granted",
        visits=[{"on": date(2024, 3, 18)}],
    )
    for row in s05:
        if row["record_type"] == "case":
            row["first_visit_date"] = "31/02/2024"
            row["enrollment_date"] = "31/02/2024"
    rows += s05
    return rows


def east_upload() -> list[dict]:
    rows = []
    # Withdrawn consent without retention permission: no safety analysis.
    e01 = _n(
        "EAST", "P-E01", sex="F", birth=date(1960, 10, 10), first_visit=date(2024, 3, 1),
        initials=("A", "C"), diagnosis="T2DM", diagnosis_date=date(2023, 3, 1),
        labs=[{"code": "CREATININE", "value": "0.8", "unit": "mg/dL", "collected_on": date(2024, 2, 26)}],
        medication="METFORMIN", medication_date=date(2024, 3, 3), consent="withdrawn",
        withdrawal_date=date(2024, 3, 20), allow_retention=False,
        visits=[{"on": date(2024, 3, 1)}],
    )
    rows += e01
    # Haemoglobin 80 g/L == 8.0 g/dL: exactly on the exclusion boundary.
    rows += _n(
        "EAST", "P-E03", sex="M", birth=date(1970, 1, 15), first_visit=date(2024, 3, 5),
        initials=("B", "E"), diagnosis="T2DM", diagnosis_date=date(2023, 1, 15),
        labs=[
            {"code": "CREATININE", "value": "1.0", "unit": "mg/dL", "collected_on": date(2024, 3, 1)},
            {"code": "HEMOGLOBIN", "value": "80", "unit": "g/L", "collected_on": date(2024, 3, 1)},
        ],
        medication="METFORMIN", medication_date=date(2024, 3, 7), consent="granted",
        visits=[{"on": date(2024, 3, 5)}],
    )
    # Creatinine collected well after the index date: outside the window.
    rows += _n(
        "EAST", "P-E04", sex="F", birth=date(1988, 7, 7), first_visit=date(2024, 3, 8),
        initials=("D", "F"), diagnosis="T2DM", diagnosis_date=date(2023, 9, 1),
        labs=[{"code": "CREATININE", "value": "0.7", "unit": "mg/dL", "collected_on": date(2024, 5, 1)}],
        medication="METFORMIN", medication_date=date(2024, 3, 10), consent="granted",
        visits=[{"on": date(2024, 3, 8)}],
    )
    return rows


def east_late_upload() -> list[dict]:
    return _n(
        "EAST", "P-E02", sex="M", birth=date(1982, 3, 3), first_visit=date(2024, 3, 20),
        initials=("G", "H"), treatment="INTENSIVE",
        diagnosis="T2DM", diagnosis_date=date(2023, 6, 1),
        labs=[{"code": "CREATININE", "value": "0.8", "unit": "mg/dL", "collected_on": date(2024, 3, 15)}],
        medication="METFORMIN", medication_date=date(2024, 3, 22), consent="granted",
        visits=[{"on": date(2024, 3, 20)}],
    )


BATCH1_UPLOADS = [
    {"center_code": "NORTH", "batch_code": "north-2024-wk10", "records": north_upload()},
    {"center_code": "SOUTH", "batch_code": "south-2024-wk10", "records": south_upload()},
    {"center_code": "EAST", "batch_code": "east-2024-wk10", "records": east_upload()},
]

BATCH2_UPLOADS = [
    {"center_code": "NORTH", "batch_code": "north-2024-wk14-amend", "records": north_amendment_upload()},
    {"center_code": "EAST", "batch_code": "east-2024-wk14-late", "records": east_late_upload()},
]


def mappings_for(center: str) -> list[dict]:
    spec = CENTER_SPECS[center]
    out = []
    for kind, key in (
        ("sex", "sex"),
        ("diagnosis", "dx"),
        ("lab", "lab"),
        ("medication", "med"),
        ("consent", "consent"),
        ("response", "resp"),
    ):
        for study_code, source_code in spec[key].items():
            out.append(
                {
                    "center_code": center,
                    "kind": kind,
                    "source_code": source_code,
                    "study_code": study_code,
                }
            )
    return out
