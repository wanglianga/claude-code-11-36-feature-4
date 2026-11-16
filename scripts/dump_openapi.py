"""Write the OpenAPI document to openapi/openapi.json.

The service is a backend-only API, so the machine-readable contract and the
Swagger UI served at /docs are part of the deliverable.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.main import app

OUT = Path(__file__).resolve().parent.parent / "openapi"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    spec = app.openapi()
    (OUT / "openapi.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {OUT / 'openapi.json'} ({len(spec['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
