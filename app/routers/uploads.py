"""Center uploads: ingest, resume an interrupted batch, list batches."""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Center, NormalizedRecord, SourceRecord, Upload, User
from ..schemas import UploadIn
from ..security import audit, require_roles
from ..services.normalize import ingest_upload, summarize_normalization

router = APIRouter(tags=["uploads"])


def _center_for(user: User, db: Session, center_code: str) -> Center:
    center = db.scalar(select(Center).where(Center.code == center_code))
    if not center:
        raise HTTPException(404, "center not found")
    if user.role == "center_uploader" and user.center_id != center.id:
        raise HTTPException(403, "center uploaders may only upload for their own centre")
    return center


@router.post("/uploads", status_code=201)
def create_upload(
    body: UploadIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("center_uploader")),
):
    center = _center_for(user, db, body.center_code)
    checksum = hashlib.sha256(
        json.dumps(body.records, sort_keys=True, default=str).encode()
    ).hexdigest()
    upload = Upload(
        center_id=center.id,
        batch_code=body.batch_code,
        file_name=body.file_name,
        checksum=checksum,
        payload={"records": body.records},
        status="received",
        total_rows=len(body.records),
        uploaded_by=user.username,
    )
    db.add(upload)
    db.flush()
    audit(
        db,
        user.username,
        "upload.receive",
        "upload",
        upload.id,
        {"center": center.code, "batch_code": body.batch_code, "rows": len(body.records)},
    )
    db.commit()

    result = ingest_upload(db, upload)
    audit(db, user.username, "upload.ingest", "upload", upload.id, result)
    db.commit()
    return {
        "id": upload.id,
        "center_code": center.code,
        "batch_code": upload.batch_code,
        "status": upload.status,
        "total_rows": upload.total_rows,
        "processed_rows": upload.processed_rows,
        "summary": summarize_normalization(db, center.id, [upload.id]),
    }


@router.get("/uploads")
def list_uploads(
    db: Session = Depends(get_db), user: User = Depends(require_roles())
):
    stmt = select(Upload, Center).join(Center, Center.id == Upload.center_id)
    if user.role == "center_uploader":
        stmt = stmt.where(Upload.center_id == user.center_id)
    rows = db.execute(stmt.order_by(Upload.id)).all()
    return [
        {
            "id": u.id,
            "center_code": c.code,
            "batch_code": u.batch_code,
            "status": u.status,
            "total_rows": u.total_rows,
            "processed_rows": u.processed_rows,
            "received_at": u.received_at.isoformat(),
        }
        for u, c in rows
    ]


@router.post("/uploads/{upload_id}/resume")
def resume_upload(
    upload_id: int,
    max_rows: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("center_uploader")),
):
    """Continue an interrupted upload from its last committed row."""
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(404, "upload not found")
    _center_for(user, db, db.get(Center, upload.center_id).code)
    if upload.status == "completed":
        return {"id": upload.id, "status": upload.status, "processed_rows": upload.processed_rows}
    result = ingest_upload(db, upload, max_rows=max_rows)
    audit(
        db,
        user.username,
        "upload.resume",
        "upload",
        upload.id,
        {**result, "resumed_from_row": result["processed_rows"] - result["normalized_rows"]},
    )
    db.commit()
    return result


@router.get("/uploads/{upload_id}/rejection-report")
def rejection_report(
    upload_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("center_uploader", "rule_reviewer")),
):
    """Rows that could not be standardized, with the reason and source row."""
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(404, "upload not found")
    if user.role == "center_uploader" and user.center_id != upload.center_id:
        raise HTTPException(403, "not your centre")
    rows = db.execute(
        select(SourceRecord, NormalizedRecord)
        .join(NormalizedRecord, NormalizedRecord.source_record_id == SourceRecord.id)
        .where(SourceRecord.upload_id == upload_id, NormalizedRecord.status != "ok")
        .order_by(SourceRecord.source_row_no)
    ).all()
    # Byte-identical re-sends are valid rows but deliberately not active. They
    # are reported here so the omission is visible rather than silent.
    dup_rows = db.execute(
        select(SourceRecord, NormalizedRecord)
        .join(NormalizedRecord, NormalizedRecord.source_record_id == SourceRecord.id)
        .where(SourceRecord.upload_id == upload_id, SourceRecord.active.is_(False))
        .order_by(SourceRecord.source_row_no)
    ).all()
    return {
        "upload_id": upload_id,
        "issues": [
            {
                "source_record_id": s.id,
                "source_uid": s.source_uid,
                "source_row_no": s.source_row_no,
                "record_type": s.record_type,
                "status": n.status,
                "issues": n.issues,
                "original_value": n.original_value,
                "original_unit": n.original_unit,
                "subject_id": n.subject_id,
            }
            for s, n in rows
        ],
        "duplicates_not_activated": [
            {
                "source_record_id": s.id,
                "source_uid": s.source_uid,
                "source_row_no": s.source_row_no,
                "revision": s.revision,
                "reason": "byte-identical to the active revision; kept for audit only",
            }
            for s, _ in dup_rows
        ],
    }
