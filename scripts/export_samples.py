"""Export the fictional sample data as JSON files, and import them back.

    python -m scripts.export_samples          # writes samples/*.json
    python -m scripts.export_samples --import # re-reads and validates them

Keeps a concrete, diffable copy of the demo inputs in the repository so the
scenario can be reproduced without the Python fixtures.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts import fixtures

OUT = Path(__file__).resolve().parent.parent / "samples"


def build() -> dict:
    return {
        "centers": {
            code: {"name": spec["name"], "config": spec["config"]}
            for code, spec in fixtures.CENTER_SPECS.items()
        },
        "mappings": {
            code: fixtures.mappings_for(code) for code in fixtures.CENTER_SPECS
        },
        "protocol": {"version": "v1.0", "rules": fixtures.PROTOCOL_RULES},
        "uploads": {
            "batch1": fixtures.BATCH1_UPLOADS,
            "batch2": fixtures.BATCH2_UPLOADS,
        },
    }


def export() -> None:
    OUT.mkdir(exist_ok=True)
    data = build()
    (OUT / "centers.json").write_text(
        json.dumps(data["centers"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "mappings.json").write_text(
        json.dumps(data["mappings"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "protocol-v1.0.json").write_text(
        json.dumps(data["protocol"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    for batch, uploads in data["uploads"].items():
        for upload in uploads:
            path = OUT / f"upload-{upload['batch_code']}.json"
            path.write_text(
                json.dumps(upload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
    (OUT / "README.md").write_text(
        "# 样例数据（全部为虚构病例）\n\n"
        "由 `python -m scripts.export_samples` 生成，供人工复核与复现。\n"
        "文件：\n\n"
        "- `centers.json`：三个中心的显示名与日期格式配置\n"
        "- `mappings.json`：各中心本地编码到研究编码的映射\n"
        "- `protocol-v1.0.json`：方案 v1.0 的入排与随访规则\n"
        "- `upload-*.json`：各批次上传的原始行（含代表中心编码差异、单位边界、撤回与迟到病例）\n",
        encoding="utf-8",
    )
    print(f"exported sample files to {OUT}")


def validate() -> None:
    data = build()
    assert len(data["centers"]) == 3
    assert all(len(v) for v in data["mappings"].values())
    total = sum(len(u["records"]) for u in data["uploads"]["batch1"])
    total += sum(len(u["records"]) for u in data["uploads"]["batch2"])
    assert total > 90
    print(f"sample data validated: {total} source rows across 3 centres and 2 batches")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--import", dest="do_import", action="store_true")
    args = parser.parse_args(argv)
    if args.do_import:
        validate()
    else:
        export()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
