"""Normalization: multi-centre coding, unit conversion boundaries, revisions."""
from __future__ import annotations

from scripts import scenario


def test_row_conservation_and_expected_normalization(client):
    scenario.seed_centers(client)
    uploads = scenario.upload_batch1(client)

    report = client.get(
        f"/uploads/{uploads['north-2024-wk10']}/rejection-report",
        headers=scenario.auth("reviewer"),
    ).json()
    # NORTH normalizes cleanly once mappings exist.
    assert report["issues"] == []

    south = client.get(
        f"/uploads/{uploads['south-2024-wk10']}/rejection-report",
        headers=scenario.auth("reviewer"),
    ).json()
    by_uid = {i["source_uid"]: i for i in south["issues"]}
    assert "SOUTH-P-S05-CASE" in by_uid
    assert by_uid["SOUTH-P-S05-CASE"]["status"] == "failed"
    assert any("unparseable_date:31/02/2024" in i for i in by_uid["SOUTH-P-S05-CASE"]["issues"])
    assert by_uid["SOUTH-P-S02-MED"]["status"] == "pending"


def test_unit_conversion_boundaries_are_preserved(client, session):
    scenario.seed_centers(client)
    scenario.upload_batch1(client)
    from sqlalchemy import select

    from app.models import NormalizedRecord, SourceRecord

    rows = session.execute(
        select(SourceRecord.source_uid, NormalizedRecord)
        .join(NormalizedRecord, NormalizedRecord.source_record_id == SourceRecord.id)
        .where(SourceRecord.record_type == "lab")
    ).all()
    by_uid = {uid: rec for uid, rec in rows}

    # 176.8 µmol/L -> 2.000000 mg/dL: exactly on the inclusive upper bound.
    n04 = by_uid["NORTH-P-N04-LAB-CREATININE-1"]
    assert float(n04.converted_value) == 2.0
    assert n04.original_unit == "µmol/L"
    assert n04.study_unit == "mg/dL"
    assert n04.conversion["source_unit"] == "µmol/L"

    # 80 g/L -> 8.0 g/dL: exactly on the exclusion threshold (excluded at <=).
    e03 = by_uid["EAST-P-E03-LAB-HEMOGLOBIN-2"]
    assert float(e03.converted_value) == 8.0
    assert e03.original_value == "80"

    # South's µmol/L value is converted with the same rule.
    s01 = by_uid["SOUTH-P-S01-LAB-CREATININE-1"]
    assert abs(float(s01.converted_value) - 200 / 88.4) < 1e-5


def test_amendment_keeps_old_revision_and_activates_new(client, session):
    scenario.seed_centers(client)
    scenario.upload_batch1(client)
    from sqlalchemy import select

    from app.models import SourceRecord

    before = session.scalar(
        select(SourceRecord).where(SourceRecord.source_uid == "NORTH-P-N07-LAB-CREATININE-1")
    )
    assert before.revision == 1 and before.active is True

    scenario.upload_batch2(client)
    # The API wrote through a different session; drop any cached rows.
    session.expire_all()
    versions = session.scalars(
        select(SourceRecord)
        .where(SourceRecord.source_uid == "NORTH-P-N07-LAB-CREATININE-1")
        .order_by(SourceRecord.revision)
    ).all()
    assert [v.revision for v in versions] == [1, 2]
    assert versions[0].active is False
    assert versions[1].active is True
    assert versions[1].supersedes_id == versions[0].id
    assert versions[0].payload["value"] == "2.5"
    assert versions[1].payload["value"] == "1.1"


def test_interrupted_upload_resumes_from_last_row(client, session):
    scenario.seed_centers(client)
    from sqlalchemy import select

    from app.models import Center, SourceRecord, Upload
    from app.services.normalize import ingest_upload
    from scripts.fixtures import east_upload

    center = session.scalar(select(Center).where(Center.code == "EAST"))
    upload = Upload(
        center_id=center.id,
        batch_code="resume-test",
        checksum="x",
        payload={"records": east_upload()},
        status="received",
        total_rows=0,
        uploaded_by="north.uploader",
    )
    session.add(upload)
    session.commit()

    first = ingest_upload(session, upload, max_rows=3)
    assert first["status"] == "partial"
    assert first["processed_rows"] == 3
    second = ingest_upload(session, upload)
    assert second["status"] == "completed"
    assert second["processed_rows"] == len(east_upload())
    # Resuming must not duplicate source rows.
    uids = session.scalars(
        select(SourceRecord.source_uid).where(SourceRecord.upload_id == upload.id)
    ).all()
    assert len(uids) == len(set(uids)) == len(east_upload())
