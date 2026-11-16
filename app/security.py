"""Authentication, role enforcement and audit helpers.

Demo tokens are seeded for the sample environment; production deployment would
replace them with hospital SSO. Tokens are stored only as SHA-256 digests.
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuditLog, User

DEMO_USERS = [
    ("admin", "admin-token", "admin", None),
    ("north.uploader", "center-north-token", "center_uploader", "NORTH"),
    ("south.uploader", "center-south-token", "center_uploader", "SOUTH"),
    ("east.uploader", "center-east-token", "center_uploader", "EAST"),
    ("reviewer", "reviewer-token", "rule_reviewer", None),
    ("analysis.lead", "analysis-token", "analysis_lead", None),
    ("privacy.admin", "privacy-token", "privacy_admin", None),
    ("sample.curator", "sample-curator-token", "sample_curator", None),
]

ALL_ROLES = {
    "admin",
    "center_uploader",
    "rule_reviewer",
    "analysis_lead",
    "privacy_admin",
    "sample_curator",
}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def seed_users(db: Session) -> None:
    """Create demo users and (re)link centre staff to their centre.

    Safe to call repeatedly: it is how a centre uploader gets their centre
    association after the centre row exists.
    """
    from .models import Center

    centers = {c.code: c for c in db.scalars(select(Center)).all()}
    for username, token, role, center_code in DEMO_USERS:
        center_id = centers[center_code].id if center_code and center_code in centers else None
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            db.add(
                User(
                    username=username,
                    role=role,
                    center_id=center_id,
                    token_hash=hash_token(token),
                )
            )
        elif center_id is not None and user.center_id != center_id:
            user.center_id = center_id
    db.commit()


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    user = db.scalar(select(User).where(User.token_hash == hash_token(token), User.active.is_(True)))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


def require_roles(*roles: str):
    """Role gate. With no arguments any authenticated active user passes."""

    def _dep(user: User = Depends(get_current_user)) -> User:
        if not roles or user.role == "admin" or user.role in roles:
            return user
        raise HTTPException(status_code=403, detail=f"Requires role: {', '.join(roles)}")

    return _dep


def audit(db: Session, actor: str, action: str, entity: str, entity_id=None, detail=None) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            entity=entity,
            entity_id=None if entity_id is None else str(entity_id),
            detail=detail or {},
        )
    )


def new_study_id() -> str:
    return "S-" + secrets.token_hex(4).upper()
