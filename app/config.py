"""Application settings. Only non-secret identifiers belong in code; the
database URL, subject salt and any account password come from the environment.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/cohorthub"
    # Salt for hospital patient references. Must be supplied in real deployments.
    subject_salt: str = "dev-only-salt-change-me"
    # Refuse to mutate a database that already holds data. Used by tests so a
    # mis-pointed DATABASE_URL can never be wiped.
    db_guard_require_empty: bool = False
    # Enables the crash/partial-run fault-injection fields on POST /screening-runs.
    # Demo and tests need it; turn it off in a real deployment.
    allow_fault_injection: bool = True
    model_config = {"env_prefix": "COHORT_", "extra": "ignore"}


settings = Settings()
