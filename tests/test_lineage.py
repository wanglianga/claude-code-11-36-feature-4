"""Sample lineage integrity and analysis usability certification tests.

Covers the acceptance scenarios: late aliquot records freezing a branch,
handover-bounded temperature gaps, mislabel correction, irreversible pools,
device clock skew, constraint-book rule changes, interval-mode centres and
the reference-book gating of cohort runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.security import seed_users

TOKENS = {
    "admin": "admin-token",
    "reviewer": "reviewer-token",
    "analysis": "analysis-token",
    "curator": "sample-curator-token",
    "north": "center-north-token",
    "south": "center-south-token",
}


def auth(who: str) -> dict:
    return {"Authorization": f"Bearer {TOKENS[who]}"}


def readings(start: datetime, end: datetime, temp: float, step_minutes: int = 60) -> list[dict]:
    """Device log: hourly readings plus one exactly at the custody end."""
    out = []
    ts = start
    while ts <= end:
        out.append({"ts": ts.isoformat(), "temp_c": temp})
        ts += timedelta(minutes=step_minutes)
    if out and out[-1]["ts"] != end.isoformat():
        out.append({"ts": end.isoformat(), "temp_c": temp})
    return out


T0 = datetime(2024, 3, 1, 8, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture()
def centers(client):
    """NORTH with device logs (event graph), SOUTH with inventory only."""
    for code, mode in (("NORTH", "event"), ("SOUTH", "interval")):
        r = client.post(
            "/centers",
            json={"code": code, "name": f"{code} centre", "config": {"evidence_mode": mode}},
            headers=auth("reviewer"),
        )
        assert r.status_code == 201, r.text
    with SessionLocal() as db:
        seed_users(db)
    return client


def make_constraint(client, purpose="primary_endpoint", **overrides):
    body = {
        "assay_purpose": purpose,
        "temp_min_c": -80.0,
        "temp_max_c": -70.0,
        "max_duration_hours": 720.0,
        "max_aliquot_count": 3,
        "min_remaining_volume_ul": 100.0,
        "clock_skew_tolerance_seconds": 300,
        "requires_event_graph": True,
        "exception_rules": {},
    }
    body.update(overrides)
    r = client.post("/lineage/constraints", json=body, headers=auth("reviewer"))
    assert r.status_code == 201, r.text
    constraint = r.json()
    r = client.post(
        f"/lineage/constraints/{constraint['id']}/approve", headers=auth("reviewer")
    )
    assert r.status_code == 200, r.text
    return r.json()


def ev(client, who, center, etype, uid, occurred, payload, issued_by="tech-1", expect=201):
    r = client.post(
        "/lineage/evidence",
        json={
            "center_code": center,
            "evidence_type": etype,
            "evidence_uid": uid,
            "occurred_at": iso(occurred) if isinstance(occurred, datetime) else occurred,
            "issued_by": issued_by,
            "payload": payload,
        },
        headers=auth(who),
    )
    assert r.status_code == expect, r.text
    return r.json()


def register_sample(client, who, center, barcode, subject, volume=5000, at=T0):
    return ev(
        client,
        who,
        center,
        "sampling_registration",
        f"REG-{barcode}",
        at,
        {"sample_barcode": barcode, "subject_ref": subject, "volume_ul": volume},
    )


def add_temperature(client, who, center, barcode, uid, start, end, temp=-78.0, skew=30):
    return ev(
        client,
        who,
        center,
        "temperature_series",
        uid,
        end,
        {
            "container_barcode": barcode,
            "device_id": "FRIDGE-1",
            "clock_skew_seconds": skew,
            "readings": readings(start, end, temp),
        },
        issued_by="FRIDGE-1",
    )


def aliquot(client, who, center, uid, parent, children, remaining, at):
    return ev(
        client,
        who,
        center,
        "aliquot",
        uid,
        at,
        {
            "parent_barcode": parent,
            "children": children,
            "parent_remaining_ul": remaining,
        },
    )


def lab_report(client, who, center, uid, barcode, purpose="primary_endpoint", at=None, value=1.1):
    return ev(
        client,
        who,
        center,
        "lab_report",
        uid,
        at or (T0 + timedelta(days=2, hours=1)),
        {
            "report_uid": uid,
            "container_barcode": barcode,
            "assay_purpose": purpose,
            "analyte": "creatinine",
            "value": value,
            "unit": "mg/dL",
        },
        issued_by="central-lab",
    )


def active_cert(client, report_uid):
    r = client.get(
        f"/lineage/certifications?report_uid={report_uid}&status=active",
        headers=auth("curator"),
    )
    assert r.status_code == 200, r.text
    certs = r.json()
    assert len(certs) == 1, certs
    return certs[0]


def certs_for(client, report_uid):
    r = client.get(
        f"/lineage/certifications?report_uid={report_uid}", headers=auth("curator")
    )
    assert r.status_code == 200, r.text
    return r.json()


def container_by_barcode(client, center_code, barcode):
    r = client.get(
        f"/lineage/containers?center_code={center_code}", headers=auth("curator")
    )
    assert r.status_code == 200, r.text
    matches = [c for c in r.json() if c["barcode"] == barcode]
    assert len(matches) == 1, matches
    return matches[0]


def happy_path(client, sample="SMP-N1", aliquot_barcode="ALQ-N1A", report="LAB-N1",
               subject="SUBJ-01", purpose="primary_endpoint"):
    """A fully evidenced chain: registration, temperature, aliquot, report."""
    register_sample(client, "north", "NORTH", sample, subject)
    add_temperature(client, "north", "NORTH", sample, f"TS-{sample}-1",
                    T0, T0 + timedelta(days=1, minutes=30))
    aliquot(client, "north", "NORTH", f"ALQ-{aliquot_barcode}", sample,
            [{"barcode": aliquot_barcode, "volume_ul": 500}], 4500,
            T0 + timedelta(days=1, minutes=30))
    add_temperature(client, "north", "NORTH", aliquot_barcode, f"TS-{aliquot_barcode}-1",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1))
    lab_report(client, "north", "NORTH", report, aliquot_barcode, purpose=purpose)
    return active_cert(client, report)


# ---------------------------------------------------------------------------
# Happy path and certification traceability
# ---------------------------------------------------------------------------


def test_analysis_ready_certification_freezes_its_basis(centers):
    make_constraint(centers)
    cert = happy_path(centers)
    assert cert["verdict"] == "analysis_ready"
    assert cert["reason_codes"] == []
    assert cert["subject_refs"] == ["SUBJ-01"]
    assert cert["cert_version"] == 1

    r = centers.get(f"/lineage/certifications/{cert['id']}", headers=auth("curator"))
    assert r.status_code == 200, r.text
    detail = r.json()
    basis = detail["frozen_basis"]
    # Container chain: sample -> aliquot, with custody intervals.
    barcodes = [c["barcode"] for c in basis["container_chain"]]
    assert barcodes == ["ALQ-N1A", "SMP-N1"] or set(barcodes) == {"ALQ-N1A", "SMP-N1"}
    assert basis["relations"][0]["type"] == "aliquot"
    assert basis["constraint"]["assay_purpose"] == "primary_endpoint"
    assert basis["cert_class"] == "event"
    # Temperature evidence frozen with the series used.
    temps = {frag["barcode"]: frag for frag in basis["temperature"]}
    assert temps["SMP-N1"]["series"][0]["clock_skew_within_tolerance"] is True
    assert temps["ALQ-N1A"]["series"]
    # Original evidence is reachable from the certification.
    evidence_types = {e["evidence_type"] for e in detail["evidence"]}
    assert {"sampling_registration", "aliquot", "temperature_series", "lab_report"} <= evidence_types
    assert detail["basis_hash"]


def test_lineage_endpoint_answers_which_sample(centers):
    make_constraint(centers)
    happy_path(centers)
    alq = container_by_barcode(centers, "NORTH", "ALQ-N1A")
    r = centers.get(
        f"/lineage/containers/{alq['id']}/lineage",
        params={"at": "2024-03-03T09:00:00+00:00"},
        headers=auth("curator"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root_samples"] == [
        {"container_id": body["root_samples"][0]["container_id"], "barcode": "SMP-N1", "subject_ref": "SUBJ-01"}
    ]


def test_reference_book_gates_cohort_run_selection(centers):
    make_constraint(centers)
    cert = happy_path(centers)
    run = centers.post(
        "/lineage/cohort-runs", json={"label": "run-2024-04"}, headers=auth("analysis")
    ).json()
    # analysis_ready certification is accepted and pinned.
    r = centers.post(
        f"/lineage/cohort-runs/{run['id']}/references",
        json={"certification_ids": [cert["id"]]},
        headers=auth("analysis"),
    )
    assert r.status_code == 201, r.text
    refs = r.json()["references"]
    assert refs[0]["certification_id"] == cert["id"]
    assert refs[0]["verdict_at_selection"] == "analysis_ready"
    # Locked runs refuse new selections.
    centers.post(f"/lineage/cohort-runs/{run['id']}/lock", headers=auth("analysis"))
    r = centers.post(
        f"/lineage/cohort-runs/{run['id']}/references",
        json={"certification_ids": [cert["id"]]},
        headers=auth("analysis"),
    )
    assert r.status_code == 409


def test_non_analysis_ready_certifications_cannot_be_selected(centers):
    make_constraint(centers)
    # Unbounded temperature gap -> lineage_pending.
    register_sample(centers, "north", "NORTH", "SMP-G1", "SUBJ-G1")
    add_temperature(centers, "north", "NORTH", "SMP-G1", "TS-G1", T0, T0 + timedelta(days=1, minutes=30))
    aliquot(centers, "north", "NORTH", "ALQ-G1A", "SMP-G1",
            [{"barcode": "ALQ-G1A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
    # Coverage stops 5 hours before the report, no handover -> unbounded gap.
    add_temperature(centers, "north", "NORTH", "ALQ-G1A", "TS-G1A",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=1, hours=20))
    lab_report(centers, "north", "NORTH", "LAB-G1", "ALQ-G1A")
    cert = active_cert(centers, "LAB-G1")
    assert cert["verdict"] == "lineage_pending"
    assert "temperature_gap_unbounded" in cert["reason_codes"]

    run = centers.post(
        "/lineage/cohort-runs", json={"label": "run-gap"}, headers=auth("analysis")
    ).json()
    r = centers.post(
        f"/lineage/cohort-runs/{run['id']}/references",
        json={"certification_ids": [cert["id"]]},
        headers=auth("analysis"),
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Late aliquot records freeze the affected branch
# ---------------------------------------------------------------------------


def test_late_aliquot_freezes_branch_and_resolution_restores(centers):
    make_constraint(centers)
    cert = happy_path(centers)
    assert cert["verdict"] == "analysis_ready"

    # A late aliquot record arrives claiming the same parent one hour before
    # the confirmed aliquot, with a remaining volume that contradicts the
    # confirmed chain (4900 claimed vs 4500 confirmed).
    ev(
        centers,
        "north",
        "NORTH",
        "aliquot",
        "ALQ-LATE-1",
        T0 + timedelta(days=1),
        {
            "parent_barcode": "SMP-N1",
            "children": [{"barcode": "ALQ-N1LATE", "volume_ul": 500}],
            "parent_remaining_ul": 4900,
        },
    )
    # The confirmed chain was NOT overwritten in receipt order: both the old
    # and the new relations are frozen in an open graph issue.
    issues = centers.get("/lineage/graph-issues?status=open", headers=auth("curator")).json()
    assert len(issues) == 1
    issue = issues[0]
    assert issue["issue_type"] == "late_aliquot_conflict"
    sample = container_by_barcode(centers, "NORTH", "SMP-N1")
    detail = centers.get(f"/lineage/containers/{sample['id']}", headers=auth("curator")).json()
    states = {r["id"]: r["state"] for r in detail["relations"]}
    assert set(states.values()) == {"frozen"}

    # The certification depending on the frozen branch drops to pending.
    cert2 = active_cert(centers, "LAB-N1")
    assert cert2["verdict"] == "lineage_pending"
    assert "lineage_frozen" in cert2["reason_codes"]
    assert cert2["cert_version"] == 2

    # Curator resolution keeps the original chain; the late claim is rejected.
    keep = [
        r["id"]
        for r in detail["relations"]
        if r["child_container_id"] == container_by_barcode(centers, "NORTH", "ALQ-N1A")["id"]
    ]
    r = centers.post(
        f"/lineage/graph-issues/{issue['id']}/resolve",
        json={"keep_relation_ids": keep, "note": "late record disproved by courier log"},
        headers=auth("curator"),
    )
    assert r.status_code == 200, r.text
    resolved = r.json()
    assert resolved["status"] == "resolved"
    states = {
        r["id"]: r["state"]
        for r in centers.get(f"/lineage/containers/{sample['id']}", headers=auth("curator")).json()["relations"]
    }
    assert states[keep[0]] == "confirmed"
    assert "rejected" in states.values()

    cert3 = active_cert(centers, "LAB-N1")
    assert cert3["verdict"] == "analysis_ready"
    assert cert3["cert_version"] == 3
    # All versions remain queryable.
    assert {c["cert_version"] for c in certs_for(centers, "LAB-N1")} == {1, 2, 3}


# ---------------------------------------------------------------------------
# Handover-bounded temperature gaps downgrade only the covered assays
# ---------------------------------------------------------------------------


def test_bounded_handover_gap_downgrades_only_covered_assay(centers):
    make_constraint(centers)
    register_sample(centers, "north", "NORTH", "SMP-N2", "SUBJ-02")
    add_temperature(centers, "north", "NORTH", "SMP-N2", "TS-N2", T0, T0 + timedelta(days=1, minutes=30))
    aliquot(
        centers, "north", "NORTH", "ALQ-N2", "SMP-N2",
        [
            {"barcode": "ALQ-N2A", "volume_ul": 500},
            {"barcode": "ALQ-N2B", "volume_ul": 500},
        ],
        4000,
        T0 + timedelta(days=1, minutes=30),
    )
    # ALQ-N2A crosses a handover: coverage stops at 08:00, resumes at 12:00.
    add_temperature(centers, "north", "NORTH", "ALQ-N2A", "TS-N2A-1",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=8))
    ev(
        centers, "north", "NORTH", "handover", "HO-N2A", T0 + timedelta(days=2, hours=12),
        {
            "container_barcode": "ALQ-N2A",
            "from_party": "NORTH lab",
            "to_party": "central lab",
            "departure_signoff_at": iso(T0 + timedelta(days=2, hours=8)),
            "arrival_signoff_at": iso(T0 + timedelta(days=2, hours=12)),
        },
        issued_by="courier-7",
    )
    add_temperature(centers, "north", "NORTH", "ALQ-N2A", "TS-N2A-2",
                    T0 + timedelta(days=2, hours=12), T0 + timedelta(days=3, hours=1))
    lab_report(centers, "north", "NORTH", "LAB-N2A", "ALQ-N2A",
               at=T0 + timedelta(days=3, hours=1))
    # ALQ-N2B has uninterrupted coverage.
    add_temperature(centers, "north", "NORTH", "ALQ-N2B", "TS-N2B",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=3, hours=1))
    lab_report(centers, "north", "NORTH", "LAB-N2B", "ALQ-N2B",
               at=T0 + timedelta(days=3, hours=1))

    gap_cert = active_cert(centers, "LAB-N2A")
    assert gap_cert["verdict"] == "exploratory_only"
    assert gap_cert["reason_codes"] == ["temperature_gap_bounded"]
    detail = centers.get(f"/lineage/certifications/{gap_cert['id']}", headers=auth("curator")).json()
    gaps = [
        g
        for frag in detail["frozen_basis"]["temperature"]
        for g in frag["gaps"]
    ]
    assert len(gaps) == 1 and gaps[0]["bounded_by_evidence_id"] is not None

    # The sibling assay of the same subject is untouched: the downgrade never
    # spills over to the whole subject.
    ok_cert = active_cert(centers, "LAB-N2B")
    assert ok_cert["verdict"] == "analysis_ready"
    view = centers.get("/lineage/subjects/SUBJ-02/usability", headers=auth("curator")).json()
    verdicts = {c["report_uid"]: c["verdict"] for c in view["certifications"]}
    assert verdicts == {"LAB-N2A": "exploratory_only", "LAB-N2B": "analysis_ready"}


# ---------------------------------------------------------------------------
# Mislabel correction pauses new-run references, preserves locked runs
# ---------------------------------------------------------------------------


def test_mislabel_pauses_open_run_and_preserves_locked_run(centers):
    make_constraint(centers)
    cert = happy_path(centers)

    open_run = centers.post(
        "/lineage/cohort-runs", json={"label": "run-open"}, headers=auth("analysis")
    ).json()
    centers.post(
        f"/lineage/cohort-runs/{open_run['id']}/references",
        json={"certification_ids": [cert["id"]]},
        headers=auth("analysis"),
    )
    locked_run = centers.post(
        "/lineage/cohort-runs", json={"label": "run-locked"}, headers=auth("analysis")
    ).json()
    centers.post(
        f"/lineage/cohort-runs/{locked_run['id']}/references",
        json={"certification_ids": [cert["id"]]},
        headers=auth("analysis"),
    )
    centers.post(f"/lineage/cohort-runs/{locked_run['id']}/lock", headers=auth("analysis"))

    alq = container_by_barcode(centers, "NORTH", "ALQ-N1A")
    r = centers.post(
        f"/lineage/containers/{alq['id']}/mislabel",
        json={"reason": "barcode swap confirmed by re-typing"},
        headers=auth("curator"),
    )
    assert r.status_code == 200, r.text

    # The active certification is now unusable; the old version is preserved.
    cert2 = active_cert(centers, "LAB-N1")
    assert cert2["verdict"] == "unusable"
    assert "container_mislabeled" in cert2["reason_codes"]
    versions = {c["cert_version"]: c["status"] for c in certs_for(centers, "LAB-N1")}
    assert versions == {1: "superseded", 2: "active"}

    open_refs = centers.get(f"/lineage/cohort-runs/{open_run['id']}", headers=auth("analysis")).json()["references"]
    assert open_refs[0]["status"] == "paused"
    assert "unusable" in open_refs[0]["pause_reason"]
    locked_refs = centers.get(f"/lineage/cohort-runs/{locked_run['id']}", headers=auth("analysis")).json()["references"]
    assert locked_refs[0]["status"] == "active"
    assert locked_refs[0]["certification_id"] == cert["id"]  # pinned to v1


# ---------------------------------------------------------------------------
# Pools are irreversible without measured evidence
# ---------------------------------------------------------------------------


def test_pool_split_requires_volume_and_quality_evidence(centers):
    make_constraint(centers)
    register_sample(centers, "north", "NORTH", "SMP-N4", "SUBJ-04")
    aliquot(centers, "north", "NORTH", "ALQ-N4-1", "SMP-N4",
            [{"barcode": "ALQ-N4A", "volume_ul": 500}], 4500, T0 + timedelta(hours=1))
    aliquot(centers, "north", "NORTH", "ALQ-N4-2", "SMP-N4",
            [{"barcode": "ALQ-N4B", "volume_ul": 500}], 4000, T0 + timedelta(hours=2))
    ev(
        centers, "north", "NORTH", "merge", "MRG-N4", T0 + timedelta(hours=3),
        {
            "pool_barcode": "POOL-N4",
            "sources": [
                {"barcode": "ALQ-N4A", "volume_ul": 500},
                {"barcode": "ALQ-N4B", "volume_ul": 500},
            ],
            "pool_volume_ul": 1000,
        },
    )
    pool = container_by_barcode(centers, "NORTH", "POOL-N4")
    assert pool["kind"] == "pool"

    # Split without measured volumes / quality evidence: refused, recorded.
    r = centers.post(
        f"/lineage/containers/{pool['id']}/split",
        json={"children": [{"barcode": "ALQ-N4X"}, {"barcode": "ALQ-N4Y"}]},
        headers=auth("curator"),
    )
    assert r.status_code == 409
    issues = centers.get("/lineage/graph-issues", headers=auth("curator")).json()
    rejected = [i for i in issues if i["issue_type"] == "pool_split_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["resolution"]["pool_state"] == "irreversible"
    # No separable lineage was fabricated.
    detail = centers.get(f"/lineage/containers/{pool['id']}", headers=auth("curator")).json()
    assert all(r["relation_type"] == "merge" for r in detail["relations"])

    # With measured volumes and a quality reference the pool may be aliquoted
    # further — as aliquots OF THE POOL; the merge history is untouched.
    r = centers.post(
        f"/lineage/containers/{pool['id']}/split",
        json={
            "children": [{"barcode": "ALQ-N4X"}, {"barcode": "ALQ-N4Y"}],
            "volume_evidence": {
                "measured_volumes_ul": {"ALQ-N4X": 400, "ALQ-N4Y": 400},
                "quality_check_ref": "QC-2024-031",
                "pool_remaining_ul": 200,
            },
        },
        headers=auth("curator"),
    )
    assert r.status_code == 200, r.text
    relations = r.json()["relations"]
    assert {rel["parent_container_id"] for rel in relations} == {pool["id"]}
    merge_relations = [
        rel for rel in centers.get(f"/lineage/containers/{pool['id']}", headers=auth("curator")).json()["relations"]
        if rel["relation_type"] == "merge"
    ]
    assert len(merge_relations) == 2  # history preserved

    # A second split of the pool's remainder must not supersede the first
    # split's relations.
    r = centers.post(
        f"/lineage/containers/{pool['id']}/split",
        json={
            "children": [{"barcode": "ALQ-N4Z"}],
            "volume_evidence": {
                "measured_volumes_ul": {"ALQ-N4Z": 150},
                "quality_check_ref": "QC-2024-032",
                "pool_remaining_ul": 50,
            },
        },
        headers=auth("curator"),
    )
    assert r.status_code == 200, r.text
    detail = centers.get(f"/lineage/containers/{pool['id']}", headers=auth("curator")).json()
    aliquot_children = [
        rel for rel in detail["relations"]
        if rel["relation_type"] == "aliquot" and rel["state"] == "confirmed"
    ]
    assert len(aliquot_children) == 3  # X, Y from split 1; Z from split 2


# ---------------------------------------------------------------------------
# Clock skew beyond tolerance proves continuity only
# ---------------------------------------------------------------------------


def test_clock_skew_beyond_tolerance_caps_at_exploratory(centers):
    make_constraint(centers)
    register_sample(centers, "north", "NORTH", "SMP-N5", "SUBJ-05")
    add_temperature(centers, "north", "NORTH", "SMP-N5", "TS-N5", T0, T0 + timedelta(days=1, minutes=30))
    aliquot(centers, "north", "NORTH", "ALQ-N5A", "SMP-N5",
            [{"barcode": "ALQ-N5A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
    # Device clock is 15 minutes off; tolerance is 5 minutes.
    add_temperature(centers, "north", "NORTH", "ALQ-N5A", "TS-N5A",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1), skew=900)
    lab_report(centers, "north", "NORTH", "LAB-N5", "ALQ-N5A")
    cert = active_cert(centers, "LAB-N5")
    assert cert["verdict"] == "exploratory_only"
    assert "clock_skew_exceeded" in cert["reason_codes"]
    detail = centers.get(f"/lineage/certifications/{cert['id']}", headers=auth("curator")).json()
    frag = [f for f in detail["frozen_basis"]["temperature"] if f["barcode"] == "ALQ-N5A"][0]
    assert frag["continuity_only"] is True
    assert frag["series"][0]["clock_skew_within_tolerance"] is False


# ---------------------------------------------------------------------------
# Excursions and interval-mode centres
# ---------------------------------------------------------------------------


def test_temperature_excursion_is_unusable(centers):
    make_constraint(centers)
    register_sample(centers, "north", "NORTH", "SMP-N6", "SUBJ-06")
    add_temperature(centers, "north", "NORTH", "SMP-N6", "TS-N6", T0, T0 + timedelta(days=1, minutes=30))
    aliquot(centers, "north", "NORTH", "ALQ-N6A", "SMP-N6",
            [{"barcode": "ALQ-N6A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
    add_temperature(centers, "north", "NORTH", "ALQ-N6A", "TS-N6A",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1), temp=-55.0)
    lab_report(centers, "north", "NORTH", "LAB-N6", "ALQ-N6A")
    cert = active_cert(centers, "LAB-N6")
    assert cert["verdict"] == "unusable"
    assert "temperature_excursion" in cert["reason_codes"]


def test_interval_centre_gets_interval_certification_capped_for_sensitive_endpoints(centers):
    make_constraint(centers)  # primary_endpoint requires the event graph
    make_constraint(centers, purpose="safety", requires_event_graph=False)
    register_sample(centers, "south", "SOUTH", "SMP-S1", "SUBJ-S01")
    # Daily inventory observations instead of device series.
    for day in range(3):
        ev(
            centers, "south", "SOUTH", "inventory", f"INV-S1-{day}", T0 + timedelta(days=day, hours=9),
            {"container_barcode": "SMP-S1", "location": "FRIDGE-S1", "temp_c": -79.0},
            issued_by="technician-s1",
        )
    aliquot(centers, "south", "SOUTH", "ALQ-S1A", "SMP-S1",
            [{"barcode": "ALQ-S1A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
    for day in (1, 2):
        ev(
            centers, "south", "SOUTH", "inventory", f"INV-S1A-{day}", T0 + timedelta(days=day, hours=10),
            {"container_barcode": "ALQ-S1A", "location": "FRIDGE-S2", "temp_c": -78.5},
            issued_by="technician-s1",
        )
    lab_report(centers, "south", "SOUTH", "LAB-S1-SAFETY", "ALQ-S1A", purpose="safety")
    lab_report(centers, "south", "SOUTH", "LAB-S1-PRIMARY", "ALQ-S1A", purpose="primary_endpoint")

    safety = active_cert(centers, "LAB-S1-SAFETY")
    assert safety["verdict"] == "analysis_ready"
    assert "not_for_high_sensitivity_endpoint" in safety["usage_restrictions"]
    detail = centers.get(f"/lineage/certifications/{safety['id']}", headers=auth("curator")).json()
    assert detail["frozen_basis"]["cert_class"] == "interval"
    frags = {f["barcode"]: f for f in detail["frozen_basis"]["temperature"]}
    assert frags["ALQ-S1A"]["intraday_unverified"] is True

    primary = active_cert(centers, "LAB-S1-PRIMARY")
    assert primary["verdict"] == "exploratory_only"
    assert "interval_mode_insufficient" in primary["reason_codes"]


# ---------------------------------------------------------------------------
# Constraint-book rule changes version certifications, keep old basis
# ---------------------------------------------------------------------------


def test_rule_change_versions_certifications_and_preserves_old_basis(centers):
    make_constraint(centers, purpose="safety", requires_event_graph=False,
                    temp_min_c=-85.0, temp_max_c=-60.0)
    register_sample(centers, "north", "NORTH", "SMP-N7", "SUBJ-07")
    add_temperature(centers, "north", "NORTH", "SMP-N7", "TS-N7", T0, T0 + timedelta(days=1, minutes=30), temp=-70.0)
    aliquot(centers, "north", "NORTH", "ALQ-N7A", "SMP-N7",
            [{"barcode": "ALQ-N7A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
    add_temperature(centers, "north", "NORTH", "ALQ-N7A", "TS-N7A",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1), temp=-70.0)
    lab_report(centers, "north", "NORTH", "LAB-N7", "ALQ-N7A", purpose="safety")
    cert_v1 = active_cert(centers, "LAB-N7")
    assert cert_v1["verdict"] == "analysis_ready"

    run = centers.post(
        "/lineage/cohort-runs", json={"label": "run-rules"}, headers=auth("analysis")
    ).json()
    centers.post(
        f"/lineage/cohort-runs/{run['id']}/references",
        json={"certification_ids": [cert_v1["id"]]},
        headers=auth("analysis"),
    )

    # Rule change: the band tightens so -70°C becomes an excursion.
    make_constraint(centers, purpose="safety", requires_event_graph=False,
                    temp_min_c=-85.0, temp_max_c=-75.0)
    r = centers.post(
        "/lineage/certifications/readjudicate",
        json={"assay_purpose": "safety"},
        headers=auth("curator"),
    )
    assert r.status_code == 200, r.text

    cert_v2 = active_cert(centers, "LAB-N7")
    assert cert_v2["cert_version"] == 2
    assert cert_v2["verdict"] == "unusable"
    assert "temperature_excursion" in cert_v2["reason_codes"]
    detail = centers.get(f"/lineage/certifications/{cert_v2['id']}", headers=auth("curator")).json()
    constraints = centers.get("/lineage/constraints?assay_purpose=safety", headers=auth("curator")).json()
    v2_constraint = [c for c in constraints if c["version"] == 2][0]
    assert detail["frozen_basis"]["constraint"]["id"] == v2_constraint["id"]

    # The old certification version is preserved with its original basis, and
    # the open run's reference (pinned to v1) is paused, not rewritten.
    old = [c for c in certs_for(centers, "LAB-N7") if c["cert_version"] == 1][0]
    assert old["status"] == "superseded"
    old_detail = centers.get(f"/lineage/certifications/{old['id']}", headers=auth("curator")).json()
    assert old_detail["frozen_basis"]["constraint"]["version"] == 1
    refs = centers.get(f"/lineage/cohort-runs/{run['id']}", headers=auth("analysis")).json()["references"]
    assert refs[0]["status"] == "paused"
    assert refs[0]["certification_id"] == cert_v1["id"]


# ---------------------------------------------------------------------------
# Evidence revisions, roles and misc invariants
# ---------------------------------------------------------------------------


def test_lab_report_revision_supersedes_and_readjudicates(centers):
    make_constraint(centers)
    cert = happy_path(centers)
    assert cert["cert_version"] == 1
    # Corrected result value under the same report uid.
    lab_report(centers, "north", "NORTH", "LAB-N1", "ALQ-N1A", value=0.9)
    cert2 = active_cert(centers, "LAB-N1")
    assert cert2["cert_version"] == 2
    evidence = centers.get(
        "/lineage/evidence?center_code=NORTH&evidence_type=lab_report", headers=auth("curator")
    ).json()
    revisions = sorted((e["revision"], e["active"]) for e in evidence)
    assert revisions == [(1, False), (2, True)]


def test_duplicate_evidence_resend_is_not_activated(centers):
    make_constraint(centers)
    register_sample(centers, "north", "NORTH", "SMP-D1", "SUBJ-D1")
    again = register_sample(centers, "north", "NORTH", "SMP-D1", "SUBJ-D1")
    assert again["duplicate"] is True
    evidence = centers.get(
        "/lineage/evidence?center_code=NORTH&evidence_type=sampling_registration",
        headers=auth("curator"),
    ).json()
    assert len(evidence) == 1


def test_role_isolation(centers):
    make_constraint(centers)
    # A centre uploader cannot submit evidence for another centre.
    r = centers.post(
        "/lineage/evidence",
        json={
            "center_code": "SOUTH",
            "evidence_type": "sampling_registration",
            "evidence_uid": "REG-X",
            "occurred_at": iso(T0),
            "issued_by": "tech-1",
            "payload": {"sample_barcode": "SMP-X", "subject_ref": "S", "volume_ul": 100},
        },
        headers=auth("north"),
    )
    assert r.status_code == 403
    # A reviewer cannot submit evidence at all.
    r = centers.post(
        "/lineage/evidence",
        json={
            "center_code": "NORTH",
            "evidence_type": "sampling_registration",
            "evidence_uid": "REG-Y",
            "occurred_at": iso(T0),
            "issued_by": "tech-1",
            "payload": {"sample_barcode": "SMP-Y", "subject_ref": "S", "volume_ul": 100},
        },
        headers=auth("reviewer"),
    )
    assert r.status_code == 403
    # The analysis lead cannot judge mislabels; the curator cannot lock runs.
    happy_path(centers)
    alq = container_by_barcode(centers, "NORTH", "ALQ-N1A")
    r = centers.post(
        f"/lineage/containers/{alq['id']}/mislabel",
        json={"reason": "x"},
        headers=auth("analysis"),
    )
    assert r.status_code == 403
    run = centers.post(
        "/lineage/cohort-runs", json={"label": "run-roles"}, headers=auth("analysis")
    ).json()
    r = centers.post(f"/lineage/cohort-runs/{run['id']}/lock", headers=auth("curator"))
    assert r.status_code == 403


def test_missing_constraint_marks_lineage_pending(centers):
    # No constraint book entry for the purpose at all.
    register_sample(centers, "north", "NORTH", "SMP-N8", "SUBJ-08")
    add_temperature(centers, "north", "NORTH", "SMP-N8", "TS-N8", T0, T0 + timedelta(days=1, minutes=30))
    aliquot(centers, "north", "NORTH", "ALQ-N8A", "SMP-N8",
            [{"barcode": "ALQ-N8A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
    add_temperature(centers, "north", "NORTH", "ALQ-N8A", "TS-N8A",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1))
    lab_report(centers, "north", "NORTH", "LAB-N8", "ALQ-N8A")
    cert = active_cert(centers, "LAB-N8")
    assert cert["verdict"] == "lineage_pending"
    assert "no_approved_constraint" in cert["reason_codes"]


def test_out_of_order_evidence_readjudicates_when_container_lands(centers):
    """A report arriving before its container's registration is pending; the
    later registration and custody evidence re-adjudicate it automatically."""
    make_constraint(centers)
    lab_report(centers, "north", "NORTH", "LAB-OO1", "ALQ-OO1A")
    cert = active_cert(centers, "LAB-OO1")
    assert cert["verdict"] == "lineage_pending"
    assert "container_not_registered" in cert["reason_codes"]

    register_sample(centers, "north", "NORTH", "SMP-OO1", "SUBJ-OO1")
    add_temperature(centers, "north", "NORTH", "SMP-OO1", "TS-OO1", T0, T0 + timedelta(days=1, minutes=30))
    aliquot(centers, "north", "NORTH", "ALQ-OO1", "SMP-OO1",
            [{"barcode": "ALQ-OO1A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
    add_temperature(centers, "north", "NORTH", "ALQ-OO1A", "TS-OO1A",
                    T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1))
    cert2 = active_cert(centers, "LAB-OO1")
    assert cert2["verdict"] == "analysis_ready"
    assert cert2["cert_version"] > cert["cert_version"]
