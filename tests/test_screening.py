"""Screening strategies, reconciliation and the equivalence guarantee."""
from __future__ import annotations

from scripts import scenario


def test_full_run_reconciles_every_center(base):
    run = base["run"]
    assert run["status"] == "completed"
    detail = base["client"].get(
        f"/screening-runs/{run['id']}", headers=scenario.auth("analysis")
    ).json()
    assert detail["counts"]["balanced"] is True
    assert len(detail["reconciliation"]) == 3
    for center in detail["reconciliation"]:
        assert center["balanced"] is True
        # raw rows == ok + pending + failed
        assert center["raw_rows"] == center["normalized_ok"] + center["pending"] + center["failed"]
        # every linked subject has exactly one outcome
        assert center["subjects_total"] == (
            center["included"]
            + center["missing_data"]
            + center["pending_subjects"]
            + center["excluded_ineligible"]
            + center["consent_withdrawn"]
        )
        # identity review is a breakdown of pending, never an extra bucket
        assert 0 <= center["identity_review"] <= center["pending_subjects"]
    total = detail["counts"]
    assert total["raw_rows"] == total["normalized_ok"] + total["pending"] + total["failed"]
    assert total["subjects_total"] == (
        total["included"]
        + total["missing_data"]
        + total["pending_subjects"]
        + total["excluded_ineligible"]
        + total["consent_withdrawn"]
    )


def test_outcomes_keep_missing_ineligible_and_withdrawal_separate(base):
    outs = scenario.outcomes(base["client"], base["run"]["id"])
    counts: dict[str, int] = {}
    for outcome in outs.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    # All five categories are represented and none is folded into another.
    assert counts["included"] >= 3
    assert counts["insufficient_data"] >= 2
    assert counts["excluded_ineligible"] >= 5
    assert counts["consent_withdrawn"] == 2
    assert counts["pending_adjudication"] >= 3
    assert sum(counts.values()) == len(outs)


def test_creatinine_on_boundary_is_included_and_above_is_excluded(base):
    decisions = scenario.decisions_by_study(base["client"], base["run"]["id"])
    # P-N04 has 176.8 µmol/L == 2.0 mg/dL exactly -> included.
    boundary = [d for d in decisions.values() if d["outcome"] == "included"]
    reasons = [d["reason_codes"] for d in boundary]
    assert any("lab_ok:CREATININE" in r for r in reasons)
    # P-N07 has 2.5 mg/dL -> excluded out of range with traceable source row.
    out_of_range = [
        d
        for d in decisions.values()
        if any("lab_out_of_range:CREATININE" in c for c in d["reason_codes"])
    ]
    assert out_of_range
    ev = out_of_range[0]["evidence"]
    assert any(e["record_type"] == "lab" for e in ev)


def test_repeat_visit_same_day_is_merged_once(base):
    decisions = scenario.decisions_by_study(base["client"], base["run"]["id"])
    merged = [d for d in decisions.values() if d["visits_merged"] == 1]
    assert len(merged) == 1
    reasons = [e["reason"] for e in merged[0]["evidence"] if e["record_type"] == "followup"]
    assert any(r.startswith("merged_repeat_visit") for r in reasons)


def test_identity_candidate_blocks_until_privacy_admin_decides(base):
    client = base["client"]
    decisions = scenario.decisions_by_study(client, base["run"]["id"])
    pending = [
        d
        for d in decisions.values()
        if "identity_candidate_unadjudicated" in d["reason_codes"]
    ]
    assert len(pending) == 2

    # A regular reviewer cannot decide identity.
    cand = client.get(
        "/identity-candidates", headers=scenario.auth("privacy")
    ).json()
    assert len(cand) == 1
    forbidden = client.post(
        f"/identity-candidates/{cand[0]['id']}/decide",
        json={"status": "rejected"},
        headers=scenario.auth("reviewer"),
    )
    assert forbidden.status_code == 403

    ok = client.post(
        f"/identity-candidates/{cand[0]['id']}/decide",
        json={"status": "rejected", "note": "distinct patients per source record"},
        headers=scenario.auth("privacy"),
    )
    assert ok.status_code == 200


def test_center_staff_cannot_see_patient_reference(base):
    client = base["client"]
    study_id = next(iter(scenario.outcomes(client, base["run"]["id"])))
    trace = client.get(
        f"/subjects/{study_id}/trace", headers=scenario.auth("reviewer")
    )
    assert trace.status_code == 200
    assert all(r["patient_ref"] is None for r in trace.json()["records"])
    # Privacy administrator sees it.
    trace2 = client.get(
        f"/subjects/{study_id}/trace", headers=scenario.auth("privacy")
    ).json()
    assert any(r["patient_ref"] for r in trace2["records"])


def test_incremental_matches_full_after_late_upload(base):
    client = base["client"]
    batch2 = scenario.upload_batch2(client)
    cutoff2 = scenario.make_cutoff(
        client,
        "cutoff-2024-04-15",
        list(base["uploads"].values()) + list(batch2.values()),
    )

    inc = scenario.start_run(
        client,
        base["revision_id"],
        cutoff2["id"],
        "incremental",
        base_run_id=base["run"]["id"],
    )
    assert inc.status_code in (200, 201), inc.text
    inc = inc.json()
    full = scenario.start_run(
        client, base["revision_id"], cutoff2["id"], "full"
    ).json()

    left = scenario.outcomes(client, inc["id"])
    right = scenario.outcomes(client, full["id"])
    assert left == right
    # Increment actually reused unchanged subjects and only recomputed a subset.
    assert inc["reused_count"] > 0
    assert 0 < inc["recomputed_count"] < full["recomputed_count"]
    assert inc["affected_subject_count"] >= 2


def test_corrected_lab_flips_exclusion_after_amendment(base):
    client = base["client"]
    before = scenario.decisions_by_study(client, base["run"]["id"])
    excluded = [
        sid
        for sid, d in before.items()
        if any("lab_out_of_range:CREATININE" in c for c in d["reason_codes"])
    ]
    assert len(excluded) == 2  # NORTH N-07 and SOUTH S-01

    batch2 = scenario.upload_batch2(client)
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-amended", list(base["uploads"].values()) + list(batch2.values())
    )
    run2 = scenario.start_run(client, base["revision_id"], cutoff2["id"], "full").json()
    after = scenario.decisions_by_study(client, run2["id"])
    # The NORTH patient whose result was corrected to 1.1 mg/dL is now included.
    flipped = [
        sid
        for sid in excluded
        if sid in after and "lab_out_of_range:CREATININE" in " ".join(after[sid]["reason_codes"])
    ]
    assert len(flipped) == 1
    # The old run's stored decision is unchanged.
    assert before == scenario.decisions_by_study(client, base["run"]["id"])


def test_equivalence_verification_full_against_incremental(base):
    client = base["client"]
    batch2 = scenario.upload_batch2(client)
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-equiv", list(base["uploads"].values()) + list(batch2.values())
    )
    full = scenario.start_run(client, base["revision_id"], cutoff2["id"], "full").json()

    r = client.post(
        f"/screening-runs/{full['id']}/verify-equivalence",
        headers=scenario.auth("reviewer"),
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["equal"] is True
    assert result["mismatch_count"] == 0
    assert result["cohort_checksum"]["left"] == result["cohort_checksum"]["right"]
    assert result["left_strategy"] == "full"
    assert result["right_strategy"] == "incremental"


def test_interrupted_run_resumes_without_restarting_finished_centers(base):
    client = base["client"]
    batch2 = scenario.upload_batch2(client)
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-crash", list(base["uploads"].values()) + list(batch2.values())
    )
    # Crash after the first centre.
    crashed = scenario.start_run(
        client,
        base["revision_id"],
        cutoff2["id"],
        "full",
        crash_after_centers=1,
    )
    assert crashed.status_code == 503
    run = crashed.json()["detail"]["run"]
    assert run["status"] == "partial"
    progress = crashed.json()["detail"]["progress"]
    assert len(progress) == 1

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ScreeningRunCenter

    with SessionLocal() as db:
        states = {
            r.center_id: r.status
            for r in db.scalars(
                select(ScreeningRunCenter).where(ScreeningRunCenter.run_id == run["id"])
            ).all()
        }
    assert list(states.values()).count("completed") == 1
    assert list(states.values()).count("pending") == 2

    resumed = client.post(
        f"/screening-runs/{run['id']}/resume", headers=scenario.auth("analysis")
    )
    assert resumed.status_code in (200, 201), resumed.text
    done = resumed.json()
    assert done["status"] == "completed"
    assert len(done["progress"]) == 2  # only the unfinished centres ran again

    full = scenario.start_run(client, base["revision_id"], cutoff2["id"], "full").json()
    assert scenario.outcomes(client, run["id"]) == scenario.outcomes(client, full["id"])


def test_lock_requires_balanced_reconciliation(base):
    client = base["client"]
    # Tamper: mark the run unbalanced to prove the snapshot gate refuses it.
    from app.db import SessionLocal
    from app.models import ScreeningRun

    with SessionLocal() as db:
        run = db.get(ScreeningRun, base["run"]["id"])
        run.counts = {**run.counts, "balanced": False}
        db.commit()

    r = client.post(
        f"/screening-runs/{base['run']['id']}/snapshots", headers=scenario.auth("analysis")
    )
    assert r.status_code == 409
    assert "Reconciliation failed" in r.json()["detail"]


def test_incremental_refuses_protocol_rule_change(base):
    client = base["client"]
    proto2 = client.post(
        "/protocols",
        json={"name": "amended protocol"},
        headers=scenario.auth("reviewer"),
    ).json()
    rules = {
        **__import__("scripts.fixtures", fromlist=["PROTOCOL_RULES"]).PROTOCOL_RULES,
        "inclusion": {
            **__import__("scripts.fixtures", fromlist=["PROTOCOL_RULES"]).PROTOCOL_RULES["inclusion"],
            "age_max": 70,
        },
    }
    rev2 = client.post(
        f"/protocols/{proto2['id']}/revisions",
        json={"version": "v1.1", "rules": rules},
        headers=scenario.auth("reviewer"),
    ).json()
    client.post(
        f"/protocols/revisions/{rev2['id']}/approve", headers=scenario.auth("reviewer")
    )
    batch2 = scenario.upload_batch2(client)
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-rule-change", list(base["uploads"].values()) + list(batch2.values())
    )
    r = scenario.start_run(
        client, rev2["id"], cutoff2["id"], "incremental", base_run_id=base["run"]["id"]
    )
    assert r.status_code == 409
    assert "protocol rule change" in r.json()["detail"]


def test_unapproved_protocol_cannot_run(base):
    client = base["client"]
    proto = client.post(
        "/protocols", json={"name": "draft only"}, headers=scenario.auth("reviewer")
    ).json()
    rev = client.post(
        f"/protocols/{proto['id']}/revisions",
        json={
            "version": "v0.1",
            "rules": __import__("scripts.fixtures", fromlist=["PROTOCOL_RULES"]).PROTOCOL_RULES,
        },
        headers=scenario.auth("reviewer"),
    ).json()
    r = scenario.start_run(client, rev["id"], base["cutoff"]["id"], "full")
    assert r.status_code == 409
    assert "approved" in r.json()["detail"]
