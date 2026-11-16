"""Shared pytest fixtures. Tests run against a dedicated database and refuse to
touch anything that is not clearly the test database."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault(
    "COHORT_DATABASE_URL",
    "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/cohorthub_test",
)
os.environ.setdefault("COHORT_SUBJECT_SALT", "test-salt")

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from scripts import scenario  # noqa: E402

TEST_DB_NAME = "cohorthub_test"


@pytest.fixture(scope="session", autouse=True)
def guard_database():
    name = engine.url.database
    assert name == TEST_DB_NAME, (
        f"refusing to run tests against database {name!r}; expected {TEST_DB_NAME!r}"
    )


@pytest.fixture()
def db_reset():
    scenario.reset_schema()


@pytest.fixture()
def client(db_reset):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def base(client):
    """Centres, mappings and an approved v1.0 protocol plus batch-1 uploads."""
    scenario.seed_centers(client)
    proto = scenario.create_protocol(client)
    uploads = scenario.upload_batch1(client)
    cutoff = scenario.make_cutoff(client, "cutoff-2024-04-01", list(uploads.values()))
    run = scenario.start_run(client, proto["revision_id"], cutoff["id"], "full")
    assert run.status_code in (200, 201), run.text
    return {
        "client": client,
        **proto,
        "uploads": uploads,
        "cutoff": cutoff,
        "run": run.json(),
    }
