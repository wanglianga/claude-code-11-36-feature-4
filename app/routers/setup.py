"""Centers, code mappings and protocol revisions."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Center, CodeMapping, Protocol, ProtocolRevision, User
from ..schemas import CenterIn, MappingIn, ProtocolIn, RevisionIn
from ..security import audit, require_roles

router = APIRouter(tags=["setup"])


@router.get("/centers")
def list_centers(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    centers = db.scalars(select(Center).order_by(Center.code)).all()
    return [
        {
            "id": c.id,
            "code": c.code,
            "name": c.name,
            "mapping_version": c.mapping_version,
            "active": c.active,
        }
        for c in centers
    ]


@router.post("/centers", status_code=201)
def create_center(
    body: CenterIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    if db.scalar(select(Center).where(Center.code == body.code)):
        raise HTTPException(409, "center code already exists")
    center = Center(code=body.code, name=body.name, config=body.config)
    db.add(center)
    audit(db, user.username, "center.create", "center", body.code, {"name": body.name})
    db.commit()
    return {"id": center.id, "code": center.code}


@router.get("/mappings")
def list_mappings(
    center_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    stmt = select(CodeMapping).join(Center, Center.id == CodeMapping.center_id)
    if center_code:
        stmt = stmt.where(Center.code == center_code)
    rows = db.scalars(stmt.order_by(CodeMapping.id)).all()
    return [
        {
            "id": m.id,
            "center_id": m.center_id,
            "kind": m.kind,
            "source_code": m.source_code,
            "study_code": m.study_code,
            "version": m.version,
            "created_by": m.created_by,
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]


@router.post("/mappings", status_code=201)
def create_mapping(
    body: MappingIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    center = db.scalar(select(Center).where(Center.code == body.center_code))
    if not center:
        raise HTTPException(404, "center not found")
    if body.kind not in {"sex", "diagnosis", "medication", "lab", "consent", "response"}:
        raise HTTPException(422, "unknown mapping kind")
    prior = db.scalar(
        select(CodeMapping)
        .where(
            CodeMapping.center_id == center.id,
            CodeMapping.kind == body.kind,
            CodeMapping.source_code == body.source_code,
        )
        .order_by(CodeMapping.version.desc())
    )
    version = prior.version + 1 if prior else 1
    mapping = CodeMapping(
        center_id=center.id,
        kind=body.kind,
        source_code=body.source_code,
        study_code=body.study_code,
        version=version,
        note=body.note,
        created_by=user.username,
    )
    db.add(mapping)
    center.mapping_version = (center.mapping_version or 0) + 1
    audit(
        db,
        user.username,
        "mapping.create",
        "mapping",
        f"{body.center_code}:{body.kind}:{body.source_code}",
        {"study_code": body.study_code, "version": version},
    )
    db.commit()
    return {"id": mapping.id, "version": version}


@router.post("/mappings/{mapping_id}/reapply")
def reapply_mapping(
    mapping_id: int,
    dry_run: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    """Re-standardize the source rows a new mapping covers.

    ``dry_run=true`` (default) reports which rows would become standardizable
    and which subjects need re-screening. Applying changes only derived
    normalized values; hospital source rows are untouched.
    """
    from ..services.normalize import NormalizationError, apply_mapping

    mapping = db.get(CodeMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "mapping not found")
    try:
        result = apply_mapping(db, mapping, dry_run=dry_run)
    except NormalizationError as exc:
        raise HTTPException(422, str(exc))
    if not dry_run:
        audit(
            db,
            user.username,
            "mapping.reapply",
            "mapping",
            mapping_id,
            {
                "fixed_rows": result.fixed_rows,
                "still_pending": result.still_pending,
                "affected_subjects": result.affected_subjects,
            },
        )
        db.commit()
    return result.__dict__


@router.get("/protocols")
def list_protocols(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    protocols = db.scalars(select(Protocol).order_by(Protocol.id)).all()
    out = []
    for p in protocols:
        revisions = db.scalars(
            select(ProtocolRevision)
            .where(ProtocolRevision.protocol_id == p.id)
            .order_by(ProtocolRevision.id)
        ).all()
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "revisions": [
                    {"id": r.id, "version": r.version, "status": r.status} for r in revisions
                ],
            }
        )
    return out


@router.post("/protocols", status_code=201)
def create_protocol(
    body: ProtocolIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    protocol = Protocol(name=body.name, description=body.description, created_by=user.username)
    db.add(protocol)
    audit(db, user.username, "protocol.create", "protocol", body.name)
    db.commit()
    return {"id": protocol.id}


@router.post("/protocols/{protocol_id}/revisions", status_code=201)
def create_revision(
    protocol_id: int,
    body: RevisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    protocol = db.get(Protocol, protocol_id)
    if not protocol:
        raise HTTPException(404, "protocol not found")
    if db.scalar(
        select(ProtocolRevision).where(
            ProtocolRevision.protocol_id == protocol_id, ProtocolRevision.version == body.version
        )
    ):
        raise HTTPException(409, "revision version already exists")
    revision = ProtocolRevision(
        protocol_id=protocol_id,
        version=body.version,
        rules=body.rules,
        created_by=user.username,
    )
    db.add(revision)
    audit(db, user.username, "protocol.revision", "protocol_revision", body.version)
    db.commit()
    return {"id": revision.id, "version": revision.version, "status": revision.status}


@router.post("/protocols/revisions/{revision_id}/approve")
def approve_revision(
    revision_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer")),
):
    revision = db.get(ProtocolRevision, revision_id)
    if not revision:
        raise HTTPException(404, "revision not found")
    if revision.status != "draft":
        raise HTTPException(409, f"revision is {revision.status}")
    revision.status = "approved"
    revision.approved_by = user.username
    revision.approved_at = datetime.now(timezone.utc)
    audit(db, user.username, "protocol.approve", "protocol_revision", revision_id)
    db.commit()
    return {"id": revision.id, "status": revision.status, "approved_by": revision.approved_by}
