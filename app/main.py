"""FastAPI application entrypoint.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Schema is created on startup for the demo; a real deployment would run a
migration tool. All data in this project is fictional.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .db import Base, SessionLocal, engine, get_db
from .models import User
from .routers import cohort, lineage, privacy, runs, setup, uploads
from .security import DEMO_USERS, get_current_user, seed_users

logging.basicConfig(level=logging.INFO)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        seed_users(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="Clinical Cohort Recompute Service",
    version="1.0.0",
    description=(
        "Multi-centre clinical cohort screening with full and incremental "
        "recomputation, unit/code normalization, reconciliation gates and "
        "immutable versioned snapshots and reports. Includes a sample lineage "
        "integrity and analysis usability certification service (sampling "
        "evidence store, container graph, constraint book, usability "
        "certifications and the analysis reference book) under /lineage. "
        "All patients are fictional."
    ),
    lifespan=lifespan,
)

app.include_router(setup.router)
app.include_router(uploads.router)
app.include_router(runs.router)
app.include_router(cohort.router)
app.include_router(privacy.router)
app.include_router(lineage.router)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("select 1"))
    return {"status": "ok"}


@app.get("/whoami")
def whoami(user: User = Depends(get_current_user)):
    return {
        "username": user.username,
        "role": user.role,
        "center_id": user.center_id,
    }


@app.get("/audit-log")
def audit_log(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from .models import AuditLog

    if user.role != "admin" and user.role != "analysis_lead":
        raise HTTPException(403, "admin or analysis lead required")
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [
        {
            "id": a.id,
            "actor": a.actor,
            "action": a.action,
            "entity": a.entity,
            "entity_id": a.entity_id,
            "detail": a.detail,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@app.get("/demo-accounts")
def demo_accounts(db: Session = Depends(get_db)):
    """Fictional demo identities for local verification only."""
    return [
        {
            "username": username,
            "role": role,
            "center": center_code,
            "token": token,
        }
        for username, token, role, center_code in DEMO_USERS
    ]
