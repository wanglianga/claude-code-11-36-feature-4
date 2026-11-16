"""CLI helper: create schema + demo users, optionally seed sample data."""
from __future__ import annotations

import sys

from .db import Base, SessionLocal, engine
from .security import seed_users


def init_schema() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_users(db)
    finally:
        db.close()


def main() -> int:
    init_schema()
    print("schema initialized and demo users seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
