"""End-to-end demo against a running API or in-process.

    python -m scripts.seed_demo            # in-process, resets the demo DB
    python -m scripts.seed_demo --base-url http://127.0.0.1:8000   # live server

The script tells the whole story required by the business case:

  1. freezes a first cutoff and runs a full screening;
  2. prints the per-centre reconciliation the analysis lead must sign off;
  3. locks a snapshot and publishes report v1;
  4. a late centre upload arrives, creating a second cutoff;
  5. an incremental recomputation and an independent full recomputation agree
     subject-by-subject, and report v1 is untouched;
  6. report v2 points at the new snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

TOKENS = {
    "server": "analysis-token",
    "reviewer": "reviewer-token",
    "privacy": "privacy-token",
    "curator": "sample-curator-token",
    "north": "center-north-token",
    "south": "center-south-token",
    "east": "center-east-token",
}


class Api:
    def __init__(self, base_url: str | None):
        if base_url:
            import httpx

            self.client = httpx.Client(base_url=base_url, timeout=60)
            self.close = self.client.close
        else:
            from fastapi.testclient import TestClient

            from app.main import app

            self.client = TestClient(app)
            self.client.__enter__()
            self.close = self.client.__exit__

    def as_(self, who: str) -> dict:
        return {"Authorization": f"Bearer {TOKENS[who]}"}

    def get(self, path: str, who: str = "server", **kw):
        r = self.client.get(path, headers=self.as_(who), **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, who: str = "server", **kw):
        r = self.client.post(path, headers=self.as_(who), **kw)
        r.raise_for_status()
        return r.json()


SAFE_RESET_DATABASES = {"cohorthub", "cohorthub_test", "cohorthub_demo"}


def reset_local_database() -> None:
    """Rebuild the demo schema.

    Guarded so a mis-pointed COHORT_DATABASE_URL cannot wipe an unrelated
    database: the target must be one of the known demo databases, or the caller
    must explicitly opt in with COHORT_ALLOW_RESET=1.
    """
    from sqlalchemy import text

    from app.db import Base, SessionLocal, engine
    from app.security import seed_users

    name = engine.url.database
    if name not in SAFE_RESET_DATABASES and os.environ.get("COHORT_ALLOW_RESET") != "1":
        raise SystemExit(
            f"refusing to drop schema of database {name!r}. "
            "Use one of %s, or set COHORT_ALLOW_RESET=1."
            % sorted(SAFE_RESET_DATABASES)
        )

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_users(db)


def seed(api: Api) -> dict:
    from scripts import fixtures

    existing = api.get("/centers")
    if existing:
        raise SystemExit(
            "the target database is not empty (%d centres). Reset it first, or point "
            "COHORT_DATABASE_URL at a fresh demo database." % len(existing)
        )
    centers = {}
    for code, spec in fixtures.CENTER_SPECS.items():
        centers[code] = api.post(
            "/centers",
            "reviewer",
            json={"code": code, "name": spec["name"], "config": spec["config"]},
        )["id"]
    # Relink centre uploaders to their centre.
    from app.db import SessionLocal
    from app.security import seed_users

    with SessionLocal() as db:
        seed_users(db)

    for code in fixtures.CENTER_SPECS:
        for mapping in fixtures.mappings_for(code):
            api.post("/mappings", "reviewer", json=mapping)

    proto = api.post(
        "/protocols",
        "reviewer",
        json={"name": "DKD-MET cohort (fictional)", "description": "Demo protocol"},
    )
    rev = api.post(
        f"/protocols/{proto['id']}/revisions",
        "reviewer",
        json={"version": "v1.0", "rules": fixtures.PROTOCOL_RULES},
    )
    api.post(f"/protocols/revisions/{rev['id']}/approve", "reviewer")

    uploads = {}
    for upload in fixtures.BATCH1_UPLOADS:
        who = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}[upload["center_code"]]
        uploads[upload["batch_code"]] = api.post("/uploads", who, json=upload)["id"]
    return {
        "centers": centers,
        "protocol_id": proto["id"],
        "revision_id": rev["id"],
        "uploads": uploads,
    }


def _print_reconciliation(run_detail: dict) -> None:
    print("\n每中心对账（分析负责人签核前必须全部 balanced=true）")
    header = (
        f"{'中心':6}{'原始行':>7}{'标准化':>7}{'待判行':>7}{'失败行':>7}"
        f"{'病例':>6}{'纳入':>6}{'缺资料':>7}{'待判':>6}{'不符合':>7}{'撤回':>6}{'守恒':>6}"
    )
    print(header)
    for c in run_detail["reconciliation"]:
        print(
            f"{c['center_code']:6}{c['raw_rows']:>8}{c['normalized_ok']:>9}{c['pending']:>9}"
            f"{c['failed']:>8}{c['subjects_total']:>7}{c['included']:>7}"
            f"{c['missing_data']:>8}{c['pending_subjects']:>7}"
            f"{c['excluded_ineligible']:>9}{c['consent_withdrawn']:>7}"
            f"{str(c['balanced']):>7}"
        )
    t = run_detail["counts"]
    print(
        f"{'合计':6}{t['raw_rows']:>8}{t['normalized_ok']:>9}{t['pending']:>9}"
        f"{t['failed']:>8}{t['subjects_total']:>7}{t['included']:>7}"
        f"{t['missing_data']:>8}{t['pending_subjects']:>7}"
        f"{t['excluded_ineligible']:>9}{t['consent_withdrawn']:>7}"
        f"{str(t['balanced']):>7}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None, help="use a running server instead of in-process")
    args = parser.parse_args(argv)

    api = Api(args.base_url)
    try:
        if not args.base_url:
            reset_local_database()
        parts = seed(api)
        rev = parts["revision_id"]
        uploads = parts["uploads"]

        print("== 1. 冻结截止批次并执行全量筛选 ==")
        cutoff1 = api.post(
            "/cutoffs",
            "server",
            json={"label": "cutoff-2024-04-01", "upload_ids": sorted(uploads.values())},
        )
        run1 = api.post(
            "/screening-runs",
            "server",
            json={
                "protocol_revision_id": rev,
                "cutoff_batch_id": cutoff1["id"],
                "strategy": "full",
            },
        )
        detail1 = api.get(f"/screening-runs/{run1['id']}")
        _print_reconciliation(detail1)

        print("\n== 2. 隐私管理员裁决身份候选（不自动合并） ==")
        cands = api.get("/identity-candidates", "privacy")
        for cand in cands:
            api.post(
                f"/identity-candidates/{cand['id']}/decide",
                "privacy",
                json={"status": "rejected", "note": "demo: distinct per source records"},
            )
        print(f"已裁决 {len(cands)} 条候选，全部由隐私管理员人工确认")

        run1b = api.post(
            "/screening-runs",
            "server",
            json={
                "protocol_revision_id": rev,
                "cutoff_batch_id": cutoff1["id"],
                "strategy": "full",
            },
        )
        print("\n== 3. 锁定快照并发布报告 v1 ==")
        snap1 = api.post(f"/screening-runs/{run1b['id']}/snapshots", "server")
        api.post(f"/snapshots/{snap1['id']}/lock", "server")
        plan1 = api.post(f"/snapshots/{snap1['id']}/follow-up-plans", "server")
        analysis1 = api.post(
            f"/snapshots/{snap1['id']}/analysis-runs", "server", json={"kind": "efficacy"}
        )
        report1 = api.post("/reports", "server", json={"analysis_run_id": analysis1["id"]})
        print(
            f"快照 #{snap1['id']} 病例 {snap1['case_count']}，安全分析 {snap1['safety_count']}；"
            f"随访计划 #{plan1['id']}；报告 v{report1['report_version']}，"
            f"校验和 {report1['content_hash'][:12]}…"
        )

        print("\n== 4. 迟到补录（中心补传 + 结果更正） ==")
        from scripts import fixtures

        late = {}
        for upload in fixtures.BATCH2_UPLOADS:
            who = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}[upload["center_code"]]
            late[upload["batch_code"]] = api.post("/uploads", who, json=upload)["id"]
        cutoff2 = api.post(
            "/cutoffs",
            "server",
            json={
                "label": "cutoff-2024-04-15",
                "upload_ids": sorted(list(uploads.values()) + list(late.values())),
            },
        )

        print("\n== 5. 增量重算 vs 全量重算，同一方案修订与截止点 ==")
        inc = api.post(
            "/screening-runs",
            "server",
            json={
                "protocol_revision_id": rev,
                "cutoff_batch_id": cutoff2["id"],
                "strategy": "incremental",
                "base_run_id": run1b["id"],
            },
        )
        verify = api.post(f"/screening-runs/{inc['id']}/verify-equivalence", "reviewer")
        print(
            f"增量：复用 {inc['reused_count']} 例，重算 {inc['recomputed_count']} 例，"
            f"受影响 {inc['affected_subject_count']} 例"
        )
        print(
            f"与全量重算比较 {verify['subjects_compared']} 例："
            f"{'完全一致' if verify['equal'] else '存在差异'}，"
            f"Mismatch {verify['mismatch_count']}"
        )

        print("\n== 6. 新快照取代旧快照，旧报告不变 ==")
        snap2 = api.post(f"/screening-runs/{inc['id']}/snapshots", "server")
        api.post(f"/snapshots/{snap2['id']}/lock", "server")
        analysis2 = api.post(
            f"/snapshots/{snap2['id']}/analysis-runs", "server", json={"kind": "efficacy"}
        )
        report2 = api.post("/reports", "server", json={"analysis_run_id": analysis2["id"]})
        old = api.get(f"/reports/{report1['id']}")
        print(
            f"新快照 #{snap2['id']}（取代 #{snap2['supersedes_snapshot_id']}）病例 {snap2['case_count']}；"
            f"报告 v{report2['report_version']} 引用方案修订 {report2['content']['protocol']['version']}"
        )
        print(
            f"报告 v1 仍指向快照 #{old['content']['snapshot']['id']}，"
            f"内容校验和未变：{old['content_hash'] == report1['content_hash']}"
        )

        summary = {
            "rev": rev,
            "cutoff1": cutoff1["id"],
            "run1": run1b["id"],
            "snapshot1": snap1["id"],
            "report1": report1["id"],
            "cutoff2": cutoff2["id"],
            "run_incremental": inc["id"],
            "equivalence_equal": verify["equal"],
            "snapshot2": snap2["id"],
            "report2": report2["id"],
        }
        print("\n== 结果摘要 ==")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
