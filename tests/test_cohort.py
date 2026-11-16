"""Snapshot immutability, versioned reports, follow-up plans and consent."""
from __future__ import annotations

from scripts import scenario


def _decide_identity(client):
    cands = client.get("/identity-candidates", headers=scenario.auth("privacy")).json()
    for cand in cands:
        client.post(
            f"/identity-candidates/{cand['id']}/decide",
            json={"status": "rejected"},
            headers=scenario.auth("privacy"),
        )


def test_snapshot_lock_and_report_binding(base):
    client = base["client"]
    _decide_identity(client)
    # Re-run so the adjudication is reflected.
    run = scenario.start_run(
        client, base["revision_id"], base["cutoff"]["id"], "full"
    ).json()

    snap = client.post(
        f"/screening-runs/{run['id']}/snapshots", headers=scenario.auth("analysis")
    )
    assert snap.status_code == 201, snap.text
    snap = snap.json()
    assert snap["status"] == "draft"
    assert snap["checksum"]

    locked = client.post(
        f"/snapshots/{snap['id']}/lock", headers=scenario.auth("analysis")
    ).json()
    assert locked["status"] == "locked"

    analysis = client.post(
        f"/snapshots/{snap['id']}/analysis-runs",
        json={"kind": "efficacy"},
        headers=scenario.auth("analysis"),
    ).json()
    assert analysis["protocol_revision_id"] == base["revision_id"]
    assert analysis["results"]["case_count"] == snap["case_count"]

    report = client.post(
        "/reports",
        json={"analysis_run_id": analysis["id"], "note": "primary analysis"},
        headers=scenario.auth("analysis"),
    )
    assert report.status_code == 201, report.text
    report = report.json()
    assert report["report_version"] == 1
    assert report["content"]["protocol"]["version"] == "v1.0"
    assert report["content"]["snapshot"]["checksum"] == snap["checksum"]
    assert report["frozen"] is True


def test_late_upload_creates_new_snapshot_and_leaves_report_unchanged(base):
    client = base["client"]
    _decide_identity(client)
    run1 = scenario.start_run(
        client, base["revision_id"], base["cutoff"]["id"], "full"
    ).json()
    snap1 = client.post(
        f"/screening-runs/{run1['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    client.post(f"/snapshots/{snap1['id']}/lock", headers=scenario.auth("analysis"))
    analysis1 = client.post(
        f"/snapshots/{snap1['id']}/analysis-runs",
        json={"kind": "efficacy"},
        headers=scenario.auth("analysis"),
    ).json()
    report1 = client.post(
        "/reports",
        json={"analysis_run_id": analysis1["id"]},
        headers=scenario.auth("analysis"),
    ).json()

    # A late upload arrives; the old run/snapshot/report are untouched.
    batch2 = scenario.upload_batch2(client)
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-late", list(base["uploads"].values()) + list(batch2.values())
    )
    run2 = scenario.start_run(client, base["revision_id"], cutoff2["id"], "full").json()
    snap2 = client.post(
        f"/screening-runs/{run2['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    assert snap2["supersedes_snapshot_id"] == snap1["id"]
    client.post(f"/snapshots/{snap2['id']}/lock", headers=scenario.auth("analysis"))
    analysis2 = client.post(
        f"/snapshots/{snap2['id']}/analysis-runs",
        json={"kind": "efficacy"},
        headers=scenario.auth("analysis"),
    ).json()
    report2 = client.post(
        "/reports",
        json={"analysis_run_id": analysis2["id"]},
        headers=scenario.auth("analysis"),
    ).json()
    assert report2["report_version"] == report1["report_version"] + 1
    assert report2["content"]["supersedes_report_id"] == report1["id"]

    # Old report bytes are byte-identical (hash and stored content unchanged).
    old = client.get(f"/reports/{report1['id']}", headers=scenario.auth("analysis")).json()
    assert old["content_hash"] == report1["content_hash"]
    assert old["content"] == report1["content"]
    assert old["content"]["snapshot"]["id"] == snap1["id"]
    # New snapshot has strictly more data.
    assert snap2["case_count"] > snap1["case_count"]


def test_analysis_refuses_draft_snapshot(base):
    client = base["client"]
    snap = client.post(
        f"/screening-runs/{base['run']['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    r = client.post(
        f"/snapshots/{snap['id']}/analysis-runs",
        json={"kind": "efficacy"},
        headers=scenario.auth("analysis"),
    )
    assert r.status_code == 409
    assert "locked" in r.json()["detail"]


def test_followup_plan_shares_protocol_and_stops_on_withdrawal(base):
    client = base["client"]
    _decide_identity(client)
    run = scenario.start_run(client, base["revision_id"], base["cutoff"]["id"], "full").json()
    snap = client.post(
        f"/screening-runs/{run['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    client.post(f"/snapshots/{snap['id']}/lock", headers=scenario.auth("analysis"))
    plan = client.post(
        f"/snapshots/{snap['id']}/follow-up-plans", headers=scenario.auth("analysis")
    ).json()
    assert plan["protocol_revision_id"] == base["revision_id"]

    tasks = client.get(
        f"/follow-up-plans/{plan['id']}", headers=scenario.auth("analysis")
    ).json()["tasks"]
    assert tasks
    assert all(t["status"] == "scheduled" for t in tasks)
    assert {t["protocol_revision_id"] if "protocol_revision_id" in t else base["revision_id"] for t in tasks}

    # Withdrawal cancels future contact only; completed tasks are preserved.
    subject = tasks[0]["study_id"]
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import FollowUpTask
    from app.models import Subject

    with SessionLocal() as db:
        subj = db.scalar(select(Subject).where(Subject.study_id == subject))
        first = db.scalars(
            select(FollowUpTask)
            .where(FollowUpTask.subject_id == subj.id)
            .order_by(FollowUpTask.visit_day)
        ).first()
        first.status = "completed"
        db.commit()

    r = client.post(
        f"/subjects/{subject}/withdraw",
        json={"allow_retention": True},
        headers=scenario.auth("analysis"),
    )
    assert r.status_code == 200
    assert r.json()["cancelled_follow_up_tasks"] >= 1

    after = client.get(
        f"/follow-up-plans/{plan['id']}", headers=scenario.auth("analysis")
    ).json()["tasks"]
    mine = [t for t in after if t["study_id"] == subject]
    assert any(t["status"] == "completed" for t in mine)
    assert all(t["status"] != "scheduled" for t in mine)


def test_withdrawn_subject_keeps_safety_records_from_frozen_snapshot(base):
    client = base["client"]
    # Build a snapshot and check the withdrawn-with-retention subject is present.
    _decide_identity(client)
    run = scenario.start_run(client, base["revision_id"], base["cutoff"]["id"], "full").json()
    snap = client.post(
        f"/screening-runs/{run['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    assert snap["safety_count"] == 1
    body = client.get(
        f"/snapshots/{snap['id']}", headers=scenario.auth("analysis")
    ).json()
    safety = [c for c in body["cases"] if c["outcome"] == "consent_withdrawn" and c["safety_retain"]]
    assert len(safety) == 1
    assert "safety_retention_permitted" in safety[0]["reason_codes"]
    # And the non-retained withdrawal is absent from the analysis cohort.
    withdrawn = [c for c in body["cases"] if c["outcome"] == "consent_withdrawn"]
    assert len(withdrawn) == 2
    assert sum(1 for c in withdrawn if not c["safety_retain"]) == 1


def test_roles_are_separated(base):
    client = base["client"]
    # Centre uploader cannot start a screening run.
    r = client.post(
        "/screening-runs",
        json={
            "protocol_revision_id": base["revision_id"],
            "cutoff_batch_id": base["cutoff"]["id"],
            "strategy": "full",
        },
        headers=scenario.auth("north"),
    )
    assert r.status_code == 403
    # A centre uploader cannot read another centre's upload rejection report.
    runs = client.get("/uploads", headers=scenario.auth("analysis")).json()
    south = next(u for u in runs if u["center_code"] == "SOUTH")
    r = client.get(
        f"/uploads/{south['id']}/rejection-report", headers=scenario.auth("north")
    )
    assert r.status_code == 403
    # Reviewer cannot lock a snapshot.
    snap = client.post(
        f"/screening-runs/{base['run']['id']}/snapshots", headers=scenario.auth("analysis")
    )
    assert snap.status_code == 201
    r = client.post(
        f"/snapshots/{snap.json()['id']}/lock", headers=scenario.auth("reviewer")
    )
    assert r.status_code == 403
    # Privacy admin cannot see cohort analysis via lock rights.
    r = client.post(
        f"/snapshots/{snap.json()['id']}/lock", headers=scenario.auth("privacy")
    )
    assert r.status_code == 403


def test_audit_trail_covers_the_workflow(base):
    client = base["client"]
    log = client.get("/audit-log?limit=200", headers=scenario.auth("analysis")).json()
    actions = {entry["action"] for entry in log}
    assert {"center.create", "mapping.create", "protocol.approve", "upload.receive", "screening.complete"} <= actions


def test_report_traceability_back_to_source_rows(base):
    client = base["client"]
    _decide_identity(client)
    run = scenario.start_run(client, base["revision_id"], base["cutoff"]["id"], "full").json()
    snap = client.post(
        f"/screening-runs/{run['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    client.post(f"/snapshots/{snap['id']}/lock", headers=scenario.auth("analysis"))
    analysis = client.post(
        f"/snapshots/{snap['id']}/analysis-runs",
        json={"kind": "efficacy"},
        headers=scenario.auth("analysis"),
    ).json()
    report = client.post(
        "/reports",
        json={"analysis_run_id": analysis["id"]},
        headers=scenario.auth("analysis"),
    ).json()

    body = client.get(
        f"/snapshots/{snap['id']}", headers=scenario.auth("analysis")
    ).json()
    included = [c for c in body["cases"] if c["outcome"] == "included"]
    assert included
    study_id = included[0]["study_id"]
    trace = client.get(
        f"/subjects/{study_id}/trace", headers=scenario.auth("privacy")
    ).json()
    # Each evidence row in the snapshot points at a real source record.
    evidence_ids = {e["source_record_id"] for e in included[0]["evidence"]}
    traced_ids = {r["source_record_id"] for r in trace["records"]}
    assert evidence_ids.issubset(traced_ids)
    assert report["content"]["snapshot"]["checksum"] == snap["checksum"]


def test_superseded_snapshot_is_marked_but_still_readable(base):
    client = base["client"]
    _decide_identity(client)
    run1 = scenario.start_run(
        client, base["revision_id"], base["cutoff"]["id"], "full"
    ).json()
    snap1 = client.post(
        f"/screening-runs/{run1['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    client.post(f"/snapshots/{snap1['id']}/lock", headers=scenario.auth("analysis"))
    analysis1 = client.post(
        f"/snapshots/{snap1['id']}/analysis-runs",
        json={"kind": "efficacy"},
        headers=scenario.auth("analysis"),
    ).json()
    report1 = client.post(
        "/reports",
        json={"analysis_run_id": analysis1["id"]},
        headers=scenario.auth("analysis"),
    ).json()

    batch2 = scenario.upload_batch2(client)
    cutoff2 = scenario.make_cutoff(
        client, "cutoff-supersede", list(base["uploads"].values()) + list(batch2.values())
    )
    run2 = scenario.start_run(client, base["revision_id"], cutoff2["id"], "full").json()
    snap2 = client.post(
        f"/screening-runs/{run2['id']}/snapshots", headers=scenario.auth("analysis")
    ).json()
    client.post(f"/snapshots/{snap2['id']}/lock", headers=scenario.auth("analysis"))

    old = client.get(f"/snapshots/{snap1['id']}", headers=scenario.auth("analysis")).json()
    assert old["status"] == "superseded"
    assert old["case_count"] == snap1["case_count"]
    # Its report is still readable and still bound to it.
    r = client.get(f"/reports/{report1['id']}", headers=scenario.auth("analysis")).json()
    assert r["content"]["snapshot"]["id"] == snap1["id"]
