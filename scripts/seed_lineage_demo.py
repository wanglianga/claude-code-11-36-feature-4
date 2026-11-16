"""End-to-end demo of the sample lineage & usability certification service.

    python -m scripts.seed_lineage_demo            # in-process, resets the demo DB
    python -m scripts.seed_lineage_demo --base-url http://127.0.0.1:8000

The script walks through every acceptance scenario of the lineage service:

  1. two centres with different evidence modes (full event graph vs daily
     inventory) and a versioned constraint book;
  2. a fully evidenced chain certified analysis_ready and selected into a
     cohort run through the reference book;
  3. a handover-bounded temperature gap downgrading only the covered assay;
  4. a late aliquot record freezing the affected branch (never overwriting
     the confirmed chain) and its curator resolution;
  5. a mislabel judgement pausing the open run's reference while the locked
     run keeps its original certification;
  6. an irreversible pool refusing an evidence-free split;
  7. a device clock skew capping the assay at exploratory use;
  8. an inventory-only centre capped for high-sensitivity endpoints;
  9. a constraint-book rule change versioning certifications while old
     bases stay frozen.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from scripts.seed_demo import Api, reset_local_database

T0 = datetime(2024, 3, 1, 8, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def readings(start: datetime, end: datetime, temp: float, step_minutes: int = 60) -> list[dict]:
    out = []
    ts = start
    while ts <= end:
        out.append({"ts": ts.isoformat(), "temp_c": temp})
        ts += timedelta(minutes=step_minutes)
    if out and out[-1]["ts"] != end.isoformat():
        out.append({"ts": end.isoformat(), "temp_c": temp})
    return out


class LineageDemo:
    def __init__(self, api: Api):
        self.api = api

    # -- evidence helpers -------------------------------------------------
    def ev(self, who, center, etype, uid, occurred, payload, issued_by="tech-1"):
        return self.api.post(
            "/lineage/evidence",
            who,
            json={
                "center_code": center,
                "evidence_type": etype,
                "evidence_uid": uid,
                "occurred_at": iso(occurred),
                "issued_by": issued_by,
                "payload": payload,
            },
        )

    def register_sample(self, who, center, barcode, subject, volume=5000, at=T0):
        return self.ev(
            who, center, "sampling_registration", f"REG-{barcode}", at,
            {"sample_barcode": barcode, "subject_ref": subject, "volume_ul": volume},
        )

    def temperature(self, who, center, barcode, uid, start, end, temp=-78.0, skew=30):
        return self.ev(
            who, center, "temperature_series", uid, end,
            {
                "container_barcode": barcode,
                "device_id": "FRIDGE-1",
                "clock_skew_seconds": skew,
                "readings": readings(start, end, temp),
            },
            issued_by="FRIDGE-1",
        )

    def aliquot(self, who, center, uid, parent, children, remaining, at):
        return self.ev(
            who, center, "aliquot", uid, at,
            {"parent_barcode": parent, "children": children, "parent_remaining_ul": remaining},
        )

    def lab_report(self, who, center, uid, barcode, purpose="primary_endpoint", at=None, value=1.1):
        return self.ev(
            who, center, "lab_report", uid, at or (T0 + timedelta(days=2, hours=1)),
            {
                "report_uid": uid,
                "container_barcode": barcode,
                "assay_purpose": purpose,
                "analyte": "creatinine",
                "value": value,
                "unit": "mg/dL",
            },
            issued_by="central-lab",
        )

    def cert(self, report_uid):
        certs = self.api.get(f"/lineage/certifications?report_uid={report_uid}&status=active", "curator")
        assert len(certs) == 1, certs
        return certs[0]

    def container(self, center_code, barcode):
        rows = self.api.get(f"/lineage/containers?center_code={center_code}", "curator")
        matches = [c for c in rows if c["barcode"] == barcode]
        assert len(matches) == 1, matches
        return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None, help="use a running server instead of in-process")
    args = parser.parse_args(argv)

    api = Api(args.base_url)
    demo = LineageDemo(api)
    try:
        if not args.base_url:
            reset_local_database()
        if api.get("/lineage/constraints", "curator"):
            raise SystemExit("constraint book is not empty; reset the demo database first")

        print("== 1. 两中心两种证据模式 + 方案约束册 ==")
        for code, mode, label in (
            ("NORTH", "event", "设备日志完整事件图"),
            ("SOUTH", "interval", "每日盘点+签收交接"),
        ):
            api.post(
                "/centers", "reviewer",
                json={"code": code, "name": f"{code} centre", "config": {"evidence_mode": mode}},
            )
            print(f"中心 {code}: {label}")
        from app.db import SessionLocal
        from app.security import seed_users

        with SessionLocal() as db:
            seed_users(db)

        c_primary = api.post(
            "/lineage/constraints", "reviewer",
            json={
                "assay_purpose": "primary_endpoint",
                "temp_min_c": -80.0, "temp_max_c": -70.0,
                "max_duration_hours": 720.0, "max_aliquot_count": 3,
                "min_remaining_volume_ul": 100.0,
                "clock_skew_tolerance_seconds": 300,
                "requires_event_graph": True,
            },
        )
        api.post(f"/lineage/constraints/{c_primary['id']}/approve", "reviewer")
        c_safety = api.post(
            "/lineage/constraints", "reviewer",
            json={
                "assay_purpose": "safety",
                "temp_min_c": -85.0, "temp_max_c": -60.0,
                "max_duration_hours": 720.0, "max_aliquot_count": 3,
                "min_remaining_volume_ul": 100.0,
                "clock_skew_tolerance_seconds": 300,
                "requires_event_graph": False,
            },
        )
        api.post(f"/lineage/constraints/{c_safety['id']}/approve", "reviewer")
        print("约束册：primary_endpoint v1（-80~-70°C，需完整事件图）、safety v1（-85~-60°C）")

        print("\n== 2. 完整证据链 → 可用于分析认证 ==")
        demo.register_sample("north", "NORTH", "SMP-N1", "SUBJ-01")
        demo.temperature("north", "NORTH", "SMP-N1", "TS-N1", T0, T0 + timedelta(days=1, minutes=30))
        demo.aliquot("north", "NORTH", "ALQ-REC-N1", "SMP-N1",
                     [{"barcode": "ALQ-N1A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
        demo.temperature("north", "NORTH", "ALQ-N1A", "TS-N1A",
                         T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1))
        demo.lab_report("north", "NORTH", "LAB-N1", "ALQ-N1A")
        cert = demo.cert("LAB-N1")
        print(f"LAB-N1 裁定：{cert['verdict']}（认证 v{cert['cert_version']}，依据哈希 {cert['basis_hash'][:12]}…）")

        run_open = api.post("/lineage/cohort-runs", "server", json={"label": "cohort-2024-W10"})
        api.post(
            f"/lineage/cohort-runs/{run_open['id']}/references", "server",
            json={"certification_ids": [cert["id"]]},
        )
        run_locked = api.post("/lineage/cohort-runs", "server", json={"label": "cohort-2024-W09"})
        api.post(
            f"/lineage/cohort-runs/{run_locked['id']}/references", "server",
            json={"certification_ids": [cert["id"]]},
        )
        api.post(f"/lineage/cohort-runs/{run_locked['id']}/lock", "server")
        print(f"引用簿：开放运行 #{run_open['id']} 与已锁定运行 #{run_locked['id']} 均选择认证 #{cert['id']}")

        print("\n== 3. 温控交接缺口：仅缺口覆盖的检验降为探索 ==")
        demo.register_sample("north", "NORTH", "SMP-N2", "SUBJ-02")
        demo.temperature("north", "NORTH", "SMP-N2", "TS-N2", T0, T0 + timedelta(days=1, minutes=30))
        demo.aliquot("north", "NORTH", "ALQ-REC-N2", "SMP-N2",
                     [{"barcode": "ALQ-N2A", "volume_ul": 500}, {"barcode": "ALQ-N2B", "volume_ul": 500}],
                     4000, T0 + timedelta(days=1, minutes=30))
        demo.temperature("north", "NORTH", "ALQ-N2A", "TS-N2A-1",
                         T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=8))
        demo.ev("north", "NORTH", "handover", "HO-N2A", T0 + timedelta(days=2, hours=12),
                {"container_barcode": "ALQ-N2A", "from_party": "NORTH lab", "to_party": "central lab",
                 "departure_signoff_at": iso(T0 + timedelta(days=2, hours=8)),
                 "arrival_signoff_at": iso(T0 + timedelta(days=2, hours=12))},
                issued_by="courier-7")
        demo.temperature("north", "NORTH", "ALQ-N2A", "TS-N2A-2",
                         T0 + timedelta(days=2, hours=12), T0 + timedelta(days=3, hours=1))
        demo.lab_report("north", "NORTH", "LAB-N2A", "ALQ-N2A", at=T0 + timedelta(days=3, hours=1))
        demo.temperature("north", "NORTH", "ALQ-N2B", "TS-N2B",
                         T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=3, hours=1))
        demo.lab_report("north", "NORTH", "LAB-N2B", "ALQ-N2B", at=T0 + timedelta(days=3, hours=1))
        gap_cert = demo.cert("LAB-N2A")
        ok_cert = demo.cert("LAB-N2B")
        print(f"LAB-N2A（跨交接断链 4h，两端签收可界定）：{gap_cert['verdict']} {gap_cert['reason_codes']}")
        print(f"LAB-N2B（同一受试者，覆盖完整）：{ok_cert['verdict']} —— 未整例排除")

        print("\n== 4. 迟到分装与已确认链冲突：冻结分支，不按接收顺序覆盖 ==")
        demo.ev("north", "NORTH", "aliquot", "ALQ-LATE-N1", T0 + timedelta(days=1),
                {"parent_barcode": "SMP-N1",
                 "children": [{"barcode": "ALQ-N1LATE", "volume_ul": 500}],
                 "parent_remaining_ul": 4900})
        issues = api.get("/lineage/graph-issues?status=open", "curator")
        issue = issues[0]
        cert_frozen = demo.cert("LAB-N1")
        print(f"迟到记录声称剩余 4900µL 与已确认 4500µL 冲突 → 图谱问题 #{issue['id']}（{issue['issue_type']}）")
        print(f"LAB-N1 认证降级：{cert_frozen['verdict']} {cert_frozen['reason_codes']}")
        sample = demo.container("NORTH", "SMP-N1")
        relations = api.get(f"/lineage/containers/{sample['id']}", "curator")["relations"]
        keep = [r["id"] for r in relations
                if r["child_container_id"] == demo.container("NORTH", "ALQ-N1A")["id"]]
        api.post(f"/lineage/graph-issues/{issue['id']}/resolve", "curator",
                 json={"keep_relation_ids": keep, "note": "快递底单证明迟到记录为误录"})
        cert_back = demo.cert("LAB-N1")
        print(f"裁定保留原链、拒绝迟到记录 → LAB-N1 恢复：{cert_back['verdict']}（认证 v{cert_back['cert_version']}）")

        print("\n== 5. 错贴纠正：新队列引用暂停，历史运行保留原认证 ==")
        alq = demo.container("NORTH", "ALQ-N1A")
        api.post(f"/lineage/containers/{alq['id']}/mislabel", "curator",
                 json={"reason": "复测确认条码贴错"})
        cert_bad = demo.cert("LAB-N1")
        open_refs = api.get(f"/lineage/cohort-runs/{run_open['id']}", "server")["references"]
        locked_refs = api.get(f"/lineage/cohort-runs/{run_locked['id']}", "server")["references"]
        print(f"ALQ-N1A 判错贴 → LAB-N1 认证 v{cert_bad['cert_version']}：{cert_bad['verdict']} {cert_bad['reason_codes']}")
        print(f"开放运行 #{run_open['id']} 引用状态：{open_refs[0]['status']}（暂停继续纳入）")
        print(f"锁定运行 #{run_locked['id']} 引用状态：{locked_refs[0]['status']}，仍指向认证 #{locked_refs[0]['certification_id']}（历史依据不变）")

        print("\n== 6. 混样不可拆分：无分装质量/剩余量证据不得虚构可分离谱系 ==")
        demo.register_sample("north", "NORTH", "SMP-N3", "SUBJ-03")
        demo.aliquot("north", "NORTH", "ALQ-REC-N3-1", "SMP-N3",
                     [{"barcode": "ALQ-N3A", "volume_ul": 500}], 4500, T0 + timedelta(hours=1))
        demo.aliquot("north", "NORTH", "ALQ-REC-N3-2", "SMP-N3",
                     [{"barcode": "ALQ-N3B", "volume_ul": 500}], 4000, T0 + timedelta(hours=2))
        demo.ev("north", "NORTH", "merge", "MRG-N3", T0 + timedelta(hours=3),
                {"pool_barcode": "POOL-N3",
                 "sources": [{"barcode": "ALQ-N3A", "volume_ul": 500},
                             {"barcode": "ALQ-N3B", "volume_ul": 500}],
                 "pool_volume_ul": 1000})
        pool = demo.container("NORTH", "POOL-N3")
        r = api.client.post(
            f"/lineage/containers/{pool['id']}/split",
            json={"children": [{"barcode": "ALQ-N3X"}, {"barcode": "ALQ-N3Y"}]},
            headers=api.as_("curator"),
        )
        print(f"无证据拆分 POOL-N3 → HTTP {r.status_code}（拒绝，混样保持不可逆）")
        r2 = api.post(
            f"/lineage/containers/{pool['id']}/split", "curator",
            json={"children": [{"barcode": "ALQ-N3X"}, {"barcode": "ALQ-N3Y"}],
                  "volume_evidence": {"measured_volumes_ul": {"ALQ-N3X": 400, "ALQ-N3Y": 400},
                                      "quality_check_ref": "QC-2024-031",
                                      "pool_remaining_ul": 200}},
        )
        parents = {rel["parent_container_id"] for rel in r2["relations"]}
        print(f"有实测体积+质检证据 → 允许作为混样的等分（父容器仍为 POOL-N3：{parents == {pool['id']}}）")

        print("\n== 7. 温控设备时钟偏移超容差：只证明序列连续 ==")
        demo.register_sample("north", "NORTH", "SMP-N4", "SUBJ-04")
        demo.temperature("north", "NORTH", "SMP-N4", "TS-N4", T0, T0 + timedelta(days=1, minutes=30))
        demo.aliquot("north", "NORTH", "ALQ-REC-N4", "SMP-N4",
                     [{"barcode": "ALQ-N4A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
        demo.temperature("north", "NORTH", "ALQ-N4A", "TS-N4A",
                         T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1), skew=900)
        demo.lab_report("north", "NORTH", "LAB-N4", "ALQ-N4A")
        skew_cert = demo.cert("LAB-N4")
        print(f"设备偏移 900s > 容差 300s → LAB-N4：{skew_cert['verdict']} {skew_cert['reason_codes']}")

        print("\n== 8. 区间式保管链中心：签发区间认证并限制高敏感终点 ==")
        demo.register_sample("south", "SOUTH", "SMP-S1", "SUBJ-S01")
        for day in range(3):
            demo.ev("south", "SOUTH", "inventory", f"INV-S1-{day}", T0 + timedelta(days=day, hours=9),
                    {"container_barcode": "SMP-S1", "location": "FRIDGE-S1", "temp_c": -79.0},
                    issued_by="technician-s1")
        demo.aliquot("south", "SOUTH", "ALQ-REC-S1", "SMP-S1",
                     [{"barcode": "ALQ-S1A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
        for day in (1, 2):
            demo.ev("south", "SOUTH", "inventory", f"INV-S1A-{day}", T0 + timedelta(days=day, hours=10),
                    {"container_barcode": "ALQ-S1A", "location": "FRIDGE-S2", "temp_c": -78.5},
                    issued_by="technician-s1")
        demo.lab_report("south", "SOUTH", "LAB-S1-SAFETY", "ALQ-S1A", purpose="safety")
        demo.lab_report("south", "SOUTH", "LAB-S1-PRIMARY", "ALQ-S1A", purpose="primary_endpoint")
        s_safety = demo.cert("LAB-S1-SAFETY")
        s_primary = demo.cert("LAB-S1-PRIMARY")
        print(f"safety 用途：{s_safety['verdict']}（区间认证，限制：{s_safety['usage_restrictions']}）")
        print(f"primary_endpoint 用途：{s_primary['verdict']} {s_primary['reason_codes']} —— 低成本记录不伪装成同等证明")

        print("\n== 9. 规则变更：认证版本化，旧依据冻结可查 ==")
        demo.register_sample("north", "NORTH", "SMP-N5", "SUBJ-05")
        demo.temperature("north", "NORTH", "SMP-N5", "TS-N5", T0, T0 + timedelta(days=1, minutes=30), temp=-70.0)
        demo.aliquot("north", "NORTH", "ALQ-REC-N5", "SMP-N5",
                     [{"barcode": "ALQ-N5A", "volume_ul": 500}], 4500, T0 + timedelta(days=1, minutes=30))
        demo.temperature("north", "NORTH", "ALQ-N5A", "TS-N5A",
                         T0 + timedelta(days=1, minutes=30), T0 + timedelta(days=2, hours=1), temp=-70.0)
        demo.lab_report("north", "NORTH", "LAB-N5", "ALQ-N5A", purpose="safety")
        n5_v1 = demo.cert("LAB-N5")
        print(f"safety v1 规则下 LAB-N5（-70°C）：{n5_v1['verdict']}")
        c_safety2 = api.post(
            "/lineage/constraints", "reviewer",
            json={"assay_purpose": "safety", "temp_min_c": -85.0, "temp_max_c": -75.0,
                  "max_duration_hours": 720.0, "max_aliquot_count": 3,
                  "min_remaining_volume_ul": 100.0, "clock_skew_tolerance_seconds": 300,
                  "requires_event_graph": False},
        )
        api.post(f"/lineage/constraints/{c_safety2['id']}/approve", "reviewer")
        api.post("/lineage/certifications/readjudicate", "curator", json={"assay_purpose": "safety"})
        n5_v2 = demo.cert("LAB-N5")
        versions = api.get("/lineage/certifications?report_uid=LAB-N5", "curator")
        print(f"safety v2（上限收紧至 -75°C）重裁定 → LAB-N5 认证 v{n5_v2['cert_version']}：{n5_v2['verdict']} {n5_v2['reason_codes']}")
        print(f"认证版本链：{[(v['cert_version'], v['verdict'], v['status']) for v in versions]}")

        print("\n== 结果摘要 ==")
        summary = {
            "analysis_ready_cert": cert["id"],
            "gap_downgraded": gap_cert["verdict"],
            "frozen_then_resolved": cert_back["verdict"],
            "mislabel_open_run": open_refs[0]["status"],
            "mislabel_locked_run": locked_refs[0]["status"],
            "interval_capped": s_primary["verdict"],
            "rule_change_versions": [v["cert_version"] for v in versions],
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    import sys

    sys.exit(main())
