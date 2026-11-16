"""Clinician-driven corrections: late mappings, identity merges, re-sends.

These cover the "the index must not miss indirect effects" requirement: a
correction in one place has to reach every subject it can influence.
"""
from __future__ import annotations

from scripts import scenario


def _mapping_id(client, center_code: str, kind: str, source_code: str) -> int:
    rows = client.get(
        f"/mappings?center_code={center_code}", headers=scenario.auth("reviewer")
    ).json()
    match = [m for m in rows if m["kind"] == kind and m["source_code"] == source_code]
    assert match, f"no mapping {kind}:{source_code} for {center_code}"
    return match[-1]["id"]


def test_reapply_dry_run_then_apply_fixes_pending_rows(base):
    client = base["client"]
    # SOUTH's S-02 used medication code M-99 which has no mapping.
    pending_before = client.get(
        f"/screening-runs/{base['run']['id']}/decisions?outcome=pending_adjudication",
        headers=scenario.auth("analysis"),
    ).json()
    assert any("normalization_pending:medication" in d["reason_codes"] for d in pending_before)

    created = client.post(
        "/mappings",
        json={
            "center_code": "SOUTH",
            "kind": "medication",
            "source_code": "M-99",
            "study_code": "METFORMIN",
            "note": "legacy code for metformin",
        },
        headers=scenario.auth("reviewer"),
    ).json()

    dry = client.post(
        f"/mappings/{created['id']}/reapply",
        headers=scenario.auth("reviewer"),
    ).json()
    assert dry["dry_run"] is True
    assert dry["matched_rows"] == 1
    assert dry["fixed_rows"] == 1
    assert dry["affected_subjects"]

    applied = client.post(
        f"/mappings/{created['id']}/reapply?dry_run=false",
        headers=scenario.auth("reviewer"),
    ).json()
    assert applied["fixed_rows"] == 1

    # A re-screening now includes that patient instead of leaving them pending.
    run2 = scenario.start_run(
        client, base["revision_id"], base["cutoff"]["id"], "full"
    ).json()
    outs = scenario.outcomes(client, run2["id"])
    assert outs != scenario.outcomes(client, base["run"]["id"])
    detail = client.get(
        f"/screening-runs/{run2['id']}", headers=scenario.auth("analysis")
    ).json()
    assert detail["counts"]["balanced"] is True
    assert detail["counts"]["pending_subjects"] < base["run"]["counts"]["pending_subjects"]


def test_incremental_picks_up_mapping_correction(base):
    client = base["client"]
    created = client.post(
        "/mappings",
        json={
            "center_code": "SOUTH",
            "kind": "medication",
            "source_code": "M-99",
            "study_code": "METFORMIN",
        },
        headers=scenario.auth("reviewer"),
    ).json()
    client.post(f"/mappings/{created['id']}/reapply?dry_run=false", headers=scenario.auth("reviewer"))

    inc = scenario.start_run(
        client,
        base["revision_id"],
        base["cutoff"]["id"],
        "incremental",
        base_run_id=base["run"]["id"],
    )
    assert inc.status_code in (200, 201), inc.text
    inc = inc.json()
    full = scenario.start_run(
        client, base["revision_id"], base["cutoff"]["id"], "full"
    ).json()
    assert scenario.outcomes(client, inc["id"]) == scenario.outcomes(client, full["id"])
    # The dependency index flagged the affected subject rather than reusing it.
    assert inc["affected_subject_count"] >= 1
    assert inc["reused_count"] > 0


def test_identity_confirmation_merges_and_invalidates_both_sides(base):
    client = base["client"]
    cand = client.get("/identity-candidates", headers=scenario.auth("privacy")).json()[0]
    decision = client.post(
        f"/identity-candidates/{cand['id']}/decide",
        json={"status": "confirmed", "note": "same person, two hospital numbers"},
        headers=scenario.auth("privacy"),
    )
    assert decision.status_code == 200

    # Only one subject remains active for that person.
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Subject

    with SessionLocal() as db:
        merged = db.scalars(
            select(Subject).where(Subject.merged_into_subject_id.is_not(None))
        ).all()
        assert len(merged) == 1
        survivor = db.get(Subject, merged[0].merged_into_subject_id)
        assert survivor.merged_into_subject_id is None

    run2 = scenario.start_run(
        client, base["revision_id"], base["cutoff"]["id"], "full"
    ).json()
    detail = client.get(
        f"/screening-runs/{run2['id']}", headers=scenario.auth("analysis")
    ).json()
    # Merging removed a subject from the count and reconciliation still balances.
    assert detail["counts"]["subjects_total"] == base["run"]["counts"]["subjects_total"] - 1
    assert detail["counts"]["balanced"] is True


def test_incremental_after_identity_confirmation_matches_full(base):
    client = base["client"]
    cand = client.get("/identity-candidates", headers=scenario.auth("privacy")).json()[0]
    client.post(
        f"/identity-candidates/{cand['id']}/decide",
        json={"status": "confirmed"},
        headers=scenario.auth("privacy"),
    )
    inc = scenario.start_run(
        client,
        base["revision_id"],
        base["cutoff"]["id"],
        "incremental",
        base_run_id=base["run"]["id"],
    )
    assert inc.status_code in (200, 201), inc.text
    full = scenario.start_run(
        client, base["revision_id"], base["cutoff"]["id"], "full"
    ).json()
    assert scenario.outcomes(client, inc.json()["id"]) == scenario.outcomes(client, full["id"])
    assert inc.json()["affected_subject_count"] >= 2


def test_identical_resend_does_not_change_the_cohort(base):
    client = base["client"]
    from scripts import fixtures

    original = next(
        u for u in fixtures.BATCH1_UPLOADS if u["center_code"] == "NORTH"
    )
    resent = {**original, "batch_code": "north-2024-wk10-resend"}
    upload = scenario.post_upload(client, resent)
    assert upload["summary"]["raw_rows"] == len(original["records"])

    cutoff2 = scenario.make_cutoff(
        client,
        "cutoff-resend",
        list(base["uploads"].values()) + [upload["id"]],
    )
    run2 = scenario.start_run(client, base["revision_id"], cutoff2["id"], "full").json()
    assert scenario.outcomes(client, run2["id"]) == scenario.outcomes(
        client, base["run"]["id"]
    )
    assert run2["counts"]["balanced"] is True
    # Duplicates are visible in the audit trail, not silently merged away.
    report = client.get(
        f"/uploads/{upload['id']}/rejection-report", headers=scenario.auth("reviewer")
    ).json()
    assert len(report["duplicates_not_activated"]) == len(original["records"])


def test_revised_consent_withdrawal_is_honoured(base):
    client = base["client"]
    # NORTH's N-01..N-07 consents are already "granted". Withdraw N-01 via a new
    # upload row with the same source_uid (a new revision of the consent row).
    rows = [
        {
            "record_type": "consent",
            "source_uid": "NORTH-P-N01-CONSENT",
            "patient_ref": "P-N01",
            "status": "N",
            "withdrawal_date": "2024-04-02",
            "allow_retention": True,
        }
    ]
    upload = scenario.post_upload(
        client,
        {"center_code": "NORTH", "batch_code": "north-withdraw-n01", "records": rows},
    )
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-withdraw", list(base["uploads"].values()) + [upload["id"]]
    )
    run2 = scenario.start_run(client, base["revision_id"], cutoff2["id"], "full").json()
    outs = scenario.outcomes(client, run2["id"])
    before = scenario.outcomes(client, base["run"]["id"])
    changed = [sid for sid in outs if outs[sid] != before.get(sid)]
    assert len(changed) == 1
    assert outs[changed[0]] == "consent_withdrawn"
    detail = client.get(
        f"/screening-runs/{run2['id']}", headers=scenario.auth("analysis")
    ).json()
    assert detail["counts"]["safety_retained"] == 2
    assert detail["counts"]["balanced"] is True


def test_incremental_catches_indirect_identity_effect(base):
    """A new patient row can create a duplicate candidate with an *old* subject.

    Only the new subject receives a source row, so a scan limited to changed
    rows would reuse the old subject's decision. The dependency index must
    invalidate both sides.
    """
    client = base["client"]

    # Pick a NORTH patient with a unique initials/birth combo in batch 1.
    rows = [
        {
            "record_type": "case",
            "source_uid": "NORTH-P-N01-DUP-CASE",
            "patient_ref": "P-N01-DUP",
            "family_initial": "K",
            "given_initial": "M",
            "sex": "F",
            "birth_date": "1970-03-05",
            "first_visit_date": "2024-04-05",
            "enrollment_date": "2024-04-05",
        }
    ]
    upload = scenario.post_upload(
        client,
        {"center_code": "NORTH", "batch_code": "north-dup-identity", "records": rows},
    )
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-dup-identity", list(base["uploads"].values()) + [upload["id"]]
    )

    inc = scenario.start_run(
        client,
        base["revision_id"],
        cutoff2["id"],
        "incremental",
        base_run_id=base["run"]["id"],
    )
    assert inc.status_code in (200, 201), inc.text
    full = scenario.start_run(
        client, base["revision_id"], cutoff2["id"], "full"
    ).json()

    assert scenario.outcomes(client, inc.json()["id"]) == scenario.outcomes(
        client, full["id"]
    )
    # Both the new subject and the existing look-alike are pending adjudication.
    outs = scenario.outcomes(client, inc.json()["id"])
    pending = [sid for sid, outcome in outs.items() if outcome == "pending_adjudication"]
    assert len(pending) >= 3
    # The old look-alike was re-evaluated (its decision is not reused), even
    # though it received no new source row of its own.
    decisions = scenario.decisions_by_study(client, inc.json()["id"])
    recomputed = [d for d in decisions.values() if d["reused"] is False]
    assert len(recomputed) == 2
    assert any(
        "identity_candidate_unadjudicated" in d["reason_codes"] for d in recomputed
    )
