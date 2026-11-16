# 临床研究病例队列重算服务

面向多中心临床研究的病例筛选与可复算分析队列。目标：在**相同方案修订 + 相同数据截止批次**下，
全量重算与增量重算必须得到**逐病例一致**的队列；补传后只重算受影响病例，同时不遗漏间接影响。

本仓库同时提供**研究样本谱系完整性与分析可用性认证数据服务**（`/lineage`，见第 5 节）：
采样证据库、容器关系图、方案约束册、可用性裁定器与分析引用簿，回答“某份检验结果在何时属于
哪份样本、能否进入队列分析”，并把裁定依据冻结成可回查的认证版本。

技术栈：Python 3.12 + FastAPI + PostgreSQL 18。人群、编码、检验值和中心名称**全部为虚构数据**。

---

## 1. 快速启动

### 方式 A：Docker Compose（一键启动）

```bash
cp .env.example .env
docker compose up --build -d
```

- API：<http://127.0.0.1:8000>
- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI 3.1 文档：<http://127.0.0.1:8000/openapi.json>（仓库内另存 `openapi/openapi.json`）
- 健康检查：`curl http://127.0.0.1:8000/health`

### 方式 B：本地进程（使用本机已装的 PostgreSQL / Redis）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 运行所需
.venv/bin/pip install -r requirements-dev.txt    # 另加 pytest/httpx/pyflakes

# 建库（已存在则忽略报错）
PGPASSWORD=ccdev /opt/postgresql-18/bin/createdb -h 127.0.0.1 -U ccdev cohorthub

export COHORT_DATABASE_URL="postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/cohorthub"
export COHORT_SUBJECT_SALT="dev-only-salt-change-me"
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动会自动建表并写入演示账号。也可以直接用 Makefile：`make install`、`make serve`、`make demo`、`make test`。

> 本任务只要求 PostgreSQL；Redis 未使用（运行中断恢复通过数据库中逐中心检查点实现，不依赖外部队列）。

### 一键复现完整业务场景

```bash
make demo
# 等价于 COHORT_DATABASE_URL=... .venv/bin/python -m scripts.seed_demo
```

脚本会依次执行并打印：全量筛选 → 各中心对账表 → 隐私管理员裁决身份候选 → 锁定快照并发布报告 v1
→ 迟到补传（含检验结果更正）→ 增量重算 → 校验与全量重算逐病例一致 → 新快照取代旧快照、旧报告字节不变。

### 一键复现谱系认证场景

```bash
make demo-lineage
# 等价于 COHORT_DATABASE_URL=... .venv/bin/python -m scripts.seed_lineage_demo
```

脚本依次演示：两种证据模式中心与约束册 → 完整证据链签发可用于分析认证并进入引用簿 →
温控交接缺口只降级所覆盖检验 → 迟到分装冻结分支与裁定恢复 → 错贴暂停新队列引用、保留历史运行 →
混样无证据不可拆分 → 时钟偏移只证连续 → 区间中心限制高敏感终点 → 规则变更后认证版本化、旧依据冻结。

---

## 2. 测试账号（虚构，仅本地）

鉴权为 `Authorization: Bearer <token>`。`GET /demo-accounts` 可列出，**生产部署必须替换为医院 SSO**。

| 账号 | Token | 角色 | 能力 |
| --- | --- | --- | --- |
| `admin` | `admin-token` | admin | 全部权限 + 审计日志 |
| `rule_reviewer` (`reviewer`) | `reviewer-token` | 规则审核 | 建中心/映射、方案修订与审批、重放映射、等价性校验、方案约束册 |
| `analysis.lead` | `analysis-token` | 分析负责人 | 冻结截止批次、发起筛选、建/锁快照、随访计划、统计与报告、审计、分析引用簿 |
| `privacy.admin` | `privacy-token` | 隐私管理员 | 裁决身份候选、查看原始患者引用、撤回授权 |
| `sample.curator` | `sample-curator-token` | 样本谱系管理员 | 图谱冲突裁定、错贴判定、混样拆分审核、触发/批量重裁定 |
| `north.uploader` | `center-north-token` | 中心上传 | 仅 NORTH 中心的数据上传、采样证据提交与自身拒收报告 |
| `south.uploader` | `center-south-token` | 中心上传 | 同上，SOUTH |
| `east.uploader` | `center-east-token` | 中心上传 | 同上，EAST |

角色隔离要点：
- 中心上传者**只能看到随机研究号**（`study_id`），看不到 `patient_ref`；
- 身份候选**只能由隐私管理员**确认/拒绝，普通角色调用返回 403；
- 锁快照、发起统计只有分析负责人可做；规则审核员不能锁快照。

---

## 3. 使用 API 走完整流程

```bash
B=http://127.0.0.1:8000
SR='Authorization: Bearer reviewer-token'
SA='Authorization: Bearer analysis-token'
SP='Authorization: Bearer privacy-token'
SN='Authorization: Bearer center-north-token'
```

1. **建中心 + 编码映射**（审核员）`POST /centers`、`POST /mappings`
2. **方案 v1.0**（审核员）`POST /protocols` → `POST /protocols/{id}/revisions` → `POST /protocols/revisions/{id}/approve`
   筛选只接受 **approved** 修订。
3. **中心上传**（各中心上传者）`POST /uploads`，行会被立即标准化；`GET /uploads/{id}/rejection-report`
   查看无法标准化的行及原因（含“未被激活的重复补发”清单）。
   中断后 `POST /uploads/{id}/resume` 从最后已提交行继续。
4. **冻结截止批次**（分析负责人/审核员）`POST /cutoffs {label, upload_ids}`，只接受 `completed` 上传。
5. **执行筛选** `POST /screening-runs {protocol_revision_id, cutoff_batch_id, strategy}`。
6. **逐中心对账** `GET /screening-runs/{id}` → `reconciliation[]` 与 `counts`，全部 `balanced=true` 才能继续。
7. **身份候选**（隐私管理员）`GET /identity-candidates` → `POST /identity-candidates/{id}/decide`。
8. **快照** `POST /screening-runs/{id}/snapshots` → `POST /snapshots/{id}/lock`（锁定后不可改）。
9. **随访计划** `POST /snapshots/{id}/follow-up-plans`（与快照、统计引用同一方案修订）。
10. **统计与报告** `POST /snapshots/{id}/analysis-runs` → `POST /reports`（版本化、冻结）。
    统计结果按中心给出纳入数与该快照冻结时的治疗分组分布（`treatment_by_center`），
    治疗分组在**建快照时冻结**写入 `snapshot_cases.treatment_code`，后续修订不会回改已有统计。
11. **迟到补传**：新上传 → 新截止批次 → `strategy=incremental` + `base_run_id`
    → `POST /screening-runs/{id}/verify-equivalence` 与全量结果逐病例比对。

---

## 4. 关键设计

### 4.1 两种重算方式与一致性保证

| | 全量 `full` | 增量 `incremental` |
| --- | --- | --- |
| 做法 | 扫描冻结范围内所有中心与病例 | 复用基线运行中指纹未变且未被依赖索引标记的决策，其余重算 |
| 依赖 | 无 | 新上传行 → 病例；新增/变更编码映射 → 相关源编码病例；身份候选**新建或裁决** → 双侧病例及其合并族 |
| 风险 | 每次补传都要处理所有中心 | 需维护依赖索引、防遗漏间接影响 |

**一致性由三点共同保证**：

1. **判定函数是纯函数**：`(方案规则, 截止范围内该病例的全部规范化记录, 身份裁决状态)` → 决策。
   两种策略调用同一个 `evaluate_subject`，不存在两套口径。
2. **输入指纹**：每条决策保存 `input_fingerprint`（规则哈希 + 每条有效源行的 revision/payload 哈希
   + 标准化结果 + 身份待判状态）。指纹相同的复用与重算结果必然相同。
3. **依赖索引 + 强制复核**：增量只复用「指纹相同 **且** 未被依赖索引标记」的决策；
   被标记的病例即使指纹偶然相同也会重算。`POST /screening-runs/{id}/verify-equivalence`
   再用另一种策略跑一遍同一修订/截止点，逐病例比对 outcome、原因码与纳入集合校验和。

增量还受两道闸门约束：**跨方案规则变更时拒绝**（必须全量）；**新截止批次必须包含基线批次全部上传**。

中断恢复：`ScreeningRunCenter` 逐中心检查点 —— 崩溃后 `POST /screening-runs/{id}/resume`
只重跑未完成中心，已完成中心不重算。中心内部会先清空该中心本运行的决策再重写，因此半途中断也安全。

### 4.2 五类结局严格分开

| outcome | 含义 | 是否入分析队列 |
| --- | --- | --- |
| `included` | 满足全部入排 | 是 |
| `insufficient_data` | 记录可读，但方案所需信息缺失（如无肌酐） | 否，**不判为不符合** |
| `pending_adjudication` | 标准化未决（未映射编码、不可换算单位）或身份候选未裁决 | 否，等待人工 |
| `excluded_ineligible` | 明确不符合（年龄、诊断、数值越界等） | 否 |
| `consent_withdrawn` | 撤回同意 / 拒绝参加 | 否；仅 `allow_retention` 且方案允许时进入**安全分析** |

“缺资料”与“明确不符合”不会互相折叠：只要方案关心的位置读不出来，就是 `pending`/`insufficient`，
绝不因为读不出来而判成排除。每条决策都带 `reason_codes` 与 `evidence[]`（指向 `source_record_id`），
可从统计一路追到原始行。

### 4.3 对账（锁快照的前置条件）

分析负责人必须在锁快照前确认每个中心守恒。`GET /screening-runs/{id}` 返回：

- `raw_rows == normalized_ok + pending + failed`（行守恒）
- `subjects_total == included + missing_data + pending_subjects + excluded_ineligible + consent_withdrawn`（病例守恒）
- `pending_subjects` 含 `identity_review` 子计数（身份待判），它不额外占桶
- 无孤儿决策、无重复决策、纳入病例不含未解决的有效源行
- `row_linkage.unlinked_but_ok == 0`：能标准化却挂不到任何病例的行同样会使对账失败
- `no_polluted_inclusions`：纳入病例若仍有 `pending/failed` 的有效源行则失败

任一检查失败 → `balanced=false` → `POST /screening-runs/{id}/snapshots` 返回 409。

### 4.4 数据导入与修订

- 源行**只追加**：同一 `source_uid` 的新内容生成 `revision+1`，旧版本 `active=false` 但保留，
  故统计可追到任一历史版本。
- 字节完全相同的重复补发会落库留痕，但**不激活**，且可见（`GET /uploads/{id}/rejection-report`
  的 `duplicates_not_activated` 清单）。
- 单位换算**并列保存**：`original_value/original_unit` 与 `converted_value/study_unit` 同时保留，
  并记录换算规则。边界值按换算后精确比较（如 176.8 µmol/L → 2.000000 mg/dL 落在纳入上界内）。
- 编码映射可版本化；新增映射后用 `POST /mappings/{id}/reapply?dry_run=true` 预览，
  再 `dry_run=false` 重放标准化（只改派生值，不动源行）。

### 4.5 快照、随访、统计、报告

- 快照、随访计划、统计运行**都携带同一个 `protocol_revision_id` 与截止批次**。
- 快照锁定后不可变；新数据产生**新快照**，`supersedes_snapshot_id` 指向旧快照，
  旧快照标记 `superseded` 但仍可读。
- 报告版本递增、冻结、带 `content_hash`；旧报告内容与哈希**逐字节不变**，仍指向旧快照。
- 撤回授权：`POST /subjects/{study_id}/withdraw` 立即取消未来随访任务（已完成任务保留）；
  下一轮筛选会把该病例记为 `consent_withdrawn`，仅在 `allow_retention` 且方案允许时进入安全分析。

### 4.6 身份与隐私

- 病例默认按 `(中心, 患者引用哈希)` 识别；姓名首字母 + 生日相同**只生成候选**，绝不自动合并。
- 只有隐私管理员能裁决；确认后保留较小研究号为存活主体，另一主体 `merged_into`，
  归一化行改挂到存活主体（源行仍指向原中心原始记录）。
- `GET /subjects/{study_id}/trace` 对普通角色把 `patient_ref` 置空，仅隐私管理员可见。

---

## 5. 样本谱系完整性与分析可用性认证（`/lineage`）

同一份检验结果，只有证明它来自满足方案要求的那份容器和保存区间，才能进入队列分析。本子服务
与队列筛选解耦，队列侧只通过**分析引用簿**消费认证结果；本服务不安排采样、运输、实验室排班、
实物预留或样本处置。

### 5.1 五个组成部分

| 组成 | 表 | 说明 |
| --- | --- | --- |
| 采样证据库 | `sample_evidence` | 采样登记、容器条码、分装/合并、交接回执、检验报告、温控序列、盘点记录；每条保存原始载荷、`occurred_at`（实际发生）、`received_at`（接收）、`issued_by`（签发主体）与修订链（`revision`/`supersedes_id`，只追加不改写） |
| 容器关系图 | `containers` + `container_relations` | 样本/等分/混样/剩余量之间带有效区间（`valid_from`/`valid_to`）的父子边；同一份等分在同一时段不得归属两个互斥容器 |
| 方案约束册 | `protocol_constraints` | 按检测用途版本化：保存温度、时长、分装次数、最小剩余量、时钟偏移容差、是否要求完整事件图、例外规则（`exception_rules.allowed_excursions`） |
| 可用性裁定器 | `usability_certifications` | 对每份检验报告给出四档裁定并冻结依据（容器链、温控证据、约束版本、理由）为认证版本；重裁定产生新版本，旧版本保留 |
| 分析引用簿 | `cohort_run_refs` + `analysis_references` | 新队列运行只能选择 `analysis_ready` 的认证；引用钉住选择时的认证版本，既有运行保留当时的样本依据 |

### 5.2 两种证据采集模式

中心级配置 `config.evidence_mode`：

- `event`（完整事件图）：每次实物操作（分装、合并、交接、设备温控序列）逐条登记。
  裁定可做逐段温控覆盖、缺口与越温分析，能精确定位受影响等分与分析批次。
- `interval`（区间式保管链）：每日容器盘点 + 已签收交接。录入成本低，但日内顺序与短暂越温
  留为待核范围——认证标记 `cert_class=interval`、附加用途限制 `not_for_high_sensitivity_endpoint`；
  对 `requires_event_graph=true` 的高敏感终点，裁定封顶为 `exploratory_only`，
  避免把低成本记录伪装成同等证明。

两种模式都能回答“检验结果在何时属于哪份样本”：`GET /lineage/containers/{id}/lineage?at=...`
沿有效区间回溯到根样本与受试者。

### 5.3 四档裁定与冻结依据

| verdict | 含义 | 典型理由码 |
| --- | --- | --- |
| `analysis_ready` | 可用于分析 | — |
| `exploratory_only` | 仅可用于探索 | `temperature_gap_bounded`（交接签收可界定的温控缺口）、`clock_skew_exceeded`（时钟偏移超容差，只证序列连续）、`interval_mode_insufficient` |
| `lineage_pending` | 谱系待核 | `lineage_frozen`（分支被冻结）、`temperature_gap_unbounded`、`temperature_evidence_missing`、`container_not_registered`、`no_approved_constraint`、`remaining_volume_unproven` |
| `unusable` | 不可使用 | `container_mislabeled`、`temperature_excursion`、`storage_duration_exceeded`、`aliquot_count_exceeded`、`min_remaining_volume_violated` |

每份认证可回查：`GET /lineage/certifications/{id}` 返回冻结依据（容器链与父子边、温控区间与
缺口、约束版本、用途限制、理由明细）+ 原始证据 + 引用影响 + 全部历史版本。

### 5.4 关键业务规则

- **迟到分装**：晚到的分装记录与已确认父子关系发生数量或时间重叠时，冲突双方边一并
  `frozen` 并生成图谱问题（`graph_issues`），**不按接收顺序覆盖旧链**；依赖该分支的认证降为
  `lineage_pending`。谱系管理员 `POST /lineage/graph-issues/{id}/resolve` 选择保留一侧后，
  另一侧记 `rejected` 并重裁定。
- **交接缺口**：温控覆盖在交接处断裂、但两端签收时间可界定缺口时，仅该缺口覆盖的检验降为
  `exploratory_only`，同受试者其他检验不受影响（`GET /lineage/subjects/{ref}/usability`）。
- **错贴纠正**：`POST /lineage/containers/{id}/mislabel` 后，链上所有认证重裁定为 `unusable`；
  引用簿把**开放中**队列运行的引用置为 `paused`（暂停继续纳入），**已锁定**运行的引用
  原样保留（历史依据不变）。
- **混样不可拆分**：无实测剩余体积与分装质检证据的拆分请求被拒绝并记录
  （`pool_split_rejected`），混样保持不可逆，禁止虚构可分离谱系；证据充分时只允许作为
  **混样的等分**继续向下分装，合并历史永不改写。
- **时钟偏移**：设备时钟偏移超过约束容差时，连续读数只证明序列连续，不能证明越温发生在
  允许窗口之外，相关检验封顶 `exploratory_only`。
- **规则变更**：约束册新版本审批后旧版本 `retired`；既有认证冻结在原约束版本上，
  `POST /lineage/certifications/readjudicate` 按新规则生成新认证版本，引用仍钉住旧版本。

### 5.5 主要端点

```
POST /lineage/evidence                      提交采样证据（中心上传/谱系管理员，中心隔离）
GET  /lineage/evidence[/{id}]               证据查询（含修订链）
GET  /lineage/containers[/{id}]             容器与关系
GET  /lineage/containers/{id}/lineage?at=   某时刻归属哪份样本
POST /lineage/containers/{id}/mislabel      错贴判定（谱系管理员）
POST /lineage/containers/{id}/split         混样拆分审核（谱系管理员）
GET  /lineage/graph-issues                  图谱冲突/冻结分支
POST /lineage/graph-issues/{id}/resolve     裁定冻结分支
POST /lineage/constraints[/…/approve]       方案约束册（规则审核）
GET  /lineage/certifications[/{id}]         认证查询/完整依据回查
POST /lineage/lab-reports/{id}/adjudicate   单份重裁定
POST /lineage/certifications/readjudicate   批量重裁定（如规则变更后）
POST /lineage/cohort-runs[/{id}/references] 队列运行登记与认证选择（分析负责人）
POST /lineage/cohort-runs/{id}/lock         锁定引用（成为历史依据）
POST /lineage/cohort-runs/{id}/references/{rid}/resume  暂停引用恢复
GET  /lineage/subjects/{ref}/usability      受试者级检验可用性视图
```

---

## 6. 验证方式

### 6.1 自动化测试

```bash
make db-test          # 建 cohorthub_test 库
make test             # 或：COHORT_DATABASE_URL=.../cohorthub_test .venv/bin/pytest tests -q
```

测试强制要求数据库名为 `cohorthub_test`（防止误清库），覆盖 50 个用例：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_normalize.py` | 多中心编码差异、µmol/L↔mg/dL 与 g/L↔g/dL 边界、修订保留旧版、上传断点续传 |
| `tests/test_screening.py` | 全量对账守恒、五类结局分列、同日重复就诊归并、身份候选阻塞、角色隔离、增量=全量、更正后翻转、等价性校验、中断续跑、未平衡禁止锁快照、跨规则拒绝增量 |
| `tests/test_corrections.py` | 映射补建（dry-run/apply）、增量捕获映射更正、身份确认合并、**间接身份影响**、重复补发不改队列、撤回修订生效 |
| `tests/test_cohort.py` | 快照锁定与报告绑定、迟到补传送新快照且旧报告不变、草稿快照禁统计、随访计划共享方案版本、撤回只停后续联系、角色隔离、审计、报告可追溯、旧快照标记 superseded |
| `tests/test_lineage.py` | 认证依据冻结、谱系时刻查询、引用簿门控、非 analysis_ready 禁选、**迟到分装冻结分支与裁定恢复**、**交接缺口只降级所覆盖检验**、**错贴暂停开放运行保留锁定运行**、**混样无证据不可拆分（含二次拆分不顶链）**、**时钟偏移只证连续**、越温不可用、区间中心认证与高敏感终点封顶、**规则变更认证版本化**、报告修订重裁定、重复补发不激活、**乱序证据到达后自动重裁定**、角色隔离、约束缺失待核 |

### 6.2 手工验证（已实际执行）

```bash
make demo
make demo-lineage
```

实际运行输出（节选自 `make demo`）：

```
每中心对账（分析负责人签核前必须全部 balanced=true）
中心        原始行    标准化    待判行    失败行    病例    纳入    缺资料    待判    不符合    撤回    守恒
EAST        19       19        0       0      3      0       1      0        1      1   True
NORTH       42       42        0       0      7      3       1      0        2      1   True
SOUTH       36       34        1       1      6      0       0      4        2      0   True
合计          97       95        1       1     16      3       2      4        5      2   True
...
增量：复用 14 例，重算 3 例，受影响 3 例
与全量重算比较 17 例：完全一致，Mismatch 0
新快照 #2（取代 #1）病例 8；报告 v2 引用方案修订 v1.0
报告 v1 仍指向快照 #1，内容校验和未变：True
```

`make demo-lineage` 实际运行输出（节选）：

```
LAB-N1 裁定：analysis_ready（认证 v1，依据哈希 6179dbfa6c79…）
LAB-N2A（跨交接断链 4h，两端签收可界定）：exploratory_only ['temperature_gap_bounded']
LAB-N2B（同一受试者，覆盖完整）：analysis_ready —— 未整例排除
迟到记录声称剩余 4900µL 与已确认 4500µL 冲突 → 图谱问题 #1（late_aliquot_conflict）
LAB-N1 认证降级：lineage_pending ['lineage_frozen']
裁定保留原链、拒绝迟到记录 → LAB-N1 恢复：analysis_ready（认证 v3）
ALQ-N1A 判错贴 → LAB-N1 认证 v4：unusable ['container_mislabeled']
开放运行 #1 引用状态：paused（暂停继续纳入）
锁定运行 #2 引用状态：active，仍指向认证 #1（历史依据不变）
无证据拆分 POOL-N3 → HTTP 409（拒绝，混样保持不可逆）
设备偏移 900s > 容差 300s → LAB-N4：exploratory_only ['clock_skew_exceeded']
safety 用途：analysis_ready（区间认证，限制：['not_for_high_sensitivity_endpoint']）
primary_endpoint 用途：exploratory_only ['interval_mode_insufficient']
safety v2（上限收紧至 -75°C）重裁定 → LAB-N5 认证 v2：unusable ['temperature_excursion']
认证版本链：[(1, 'analysis_ready', 'superseded'), (2, 'unusable', 'active')]
```

样例数据里刻意包含的边界/异常：
- NORTH `P-N04` 肌酐 **176.8 µmol/L = 2.000000 mg/dL**，正好在纳入上界 → 纳入；
- EAST `P-E03` 血红蛋白 **80 g/L = 8.0 g/dL**，正好在排除阈值 → 排除；
- NORTH `P-N02` 同日两次筛选访视 → 归并为 1 个索引日；
- NORTH `P-N03` 撤回且允许保留 → 进入安全分析（`safety_count=1`）；EAST `P-E01` 撤回不允许保留 → 不进入；
- SOUTH `P-S03/P-S03B` 同首字母同生日 → 仅生成候选，待隐私管理员裁决；
- SOUTH `P-S05` 日期 `31/02/2024` → 标准化失败，病例记 `pending_adjudication` 而非排除；
- SOUTH `P-S02` 用药编码 `M-99` 未映射 → `pending_adjudication`，补建映射后可重算；
- NORTH `P-N07` 先为 2.5 mg/dL（越界），补传更正为 1.1 mg/dL → 下一轮筛选中翻转为纳入。

### 6.3 Swagger / OpenAPI

后端项目，提供可运行 OpenAPI 3.1 与 Swagger UI：服务启动后访问 `/docs`；
仓库内 `openapi/openapi.json` 由 `make openapi` 生成（53 个路径，含 `/lineage` 下 21 个）。

### 6.4 本次实际执行过的验证

- `pytest tests -q`：**50 passed**（对本机 PostgreSQL 18 的 `cohorthub_test` 库，每次重建 schema）。
- `python -m scripts.seed_demo`：队列重算完整流程跑通，输出见 6.2。
- `python -m scripts.seed_lineage_demo`：谱系认证九个场景跑通（进程内与 `--base-url` 连真实服务两种方式均执行），输出见 6.2。
- `uvicorn app.main:app` 实际启动后：`/health` 200、`/docs` 200、`/openapi.json` 为 OpenAPI 3.1.0 且含 53 个路径、
  `Bearer analysis-token` 调用 `/whoami` 返回 `analysis_lead`、`Bearer sample-curator-token` 返回 `sample_curator`、
  `GET /lineage/cohort-runs` 200。
- `python -m scripts.export_samples` 导出并校验 `samples/*.json`（105 条源行）。
- 增量 vs 全量实测：增量复用 14 例、重算 3 例；全量重算 17 例；两者逐病例 outcome 完全一致。

关于耗时，需要如实说明：在 16 例样例上，增量与全量的**墙钟时间基本持平**
（实测 full≈230–280 ms，incremental≈300 ms），因为样例极小，逐中心对账、依赖闭包与
每例提交的固定开销占主导。增量节省的是**判定工作量**（3 次判定 vs 17 次），
这一收益随病例总量与中心数增长才会体现在耗时上；本项目没有在更大数据量上做性能压测。

**未在当前容器验证**：容器内无 `docker` 命令，因此 `Dockerfile` 与 `docker-compose.yml` 只做了静态编写，
未实际构建/启动。本地进程方式（方式 B）已完整验证。

---

## 7. 目录结构

```
app/
  main.py            FastAPI 装配、健康检查、审计、演示账号
  models.py          ORM 模型（源记录/标准化/依赖索引/运行/决策/快照/随访/统计/报告
                     + 采样证据/容器/关系/图谱问题/约束册/认证/引用簿）
  security.py        Bearer 鉴权、角色校验、审计、演示账号种子
  services/
    normalize.py     多中心编码/单位/日期标准化，源行修订，断点续传，映射重放
    screening.py     判定纯函数、全量与增量执行、依赖索引、中断检查点
    reconcile.py     逐中心行/病例守恒与污染检查
    cohort.py        快照、随访计划、统计、报告版本化
    lineage.py       采样证据库、容器关系图（分装/合并、冲突冻结、错贴、混样拆分）
    adjudication.py  可用性裁定器、认证版本、分析引用簿
  routers/           setup / uploads / runs / cohort / privacy / lineage
scripts/
  fixtures.py        虚构多中心样例数据
  scenario.py        供 demo 与测试复用的 API 场景
  seed_demo.py       一键复现队列重算完整业务流程
  seed_lineage_demo.py 一键复现谱系认证九个验收场景
  export_samples.py  导出/校验 samples/*.json
  dump_openapi.py    生成 openapi/openapi.json
samples/             导出的样例输入（JSON）
tests/               pytest 用例（含 test_lineage.py 谱系认证 17 例）
```

---

## 8. 已知边界与后续工作

- 鉴权为固定演示 token，生产需接医院 SSO 与真实审计留存策略（`audit_log` 表已就位）。
- 筛选在请求内同步执行；真实多中心规模应改为后台任务队列，但检查点/续跑语义可直接沿用。
- 未实现真实文件解析（只接受 JSON 行）。真实导入需增加 CSV/HL7 解析，标准化入口不变。
- 未使用 Redis：中断恢复依靠数据库检查点，避免额外运行时依赖。
- `POST /screening-runs` 的 `crash_after_centers` / `max_centers` 是故障注入开关（用于验证中断续跑），
  由 `COHORT_ALLOW_FAULT_INJECTION` 控制，真实部署应设为 `false`。
- 谱系服务当前按容器条码在单一中心内构图；跨实验室转交由交接回执的双方签收记录刻画，
  尚未把同一实物在不同中心的容器身份自动并轨（可作为图谱问题由管理员裁定）。
- 温控序列保存在证据载荷内、裁定时解析；大规模部署应把读数落到时序表并建覆盖索引。
- 裁定在证据提交时同步触发；高频设备日志中心应改为后台异步裁定，认证语义不变。
