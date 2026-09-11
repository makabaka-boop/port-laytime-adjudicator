# 散货船滞期费结算服务（Demurrage Settlement API）

纯后端服务：给定散货船作业区间、停机（暂停）区间与费率，由**同一套边界规则**
计算可复核的滞期费并持久化，结算双方按结果标识回查即可得到完全一致的数字。
同一航次在多个港口形成的既有结果，还可按提交顺序汇总为**不可变的航次封顶清单**
（2–20 个结果 + 非负整数分赔付上限），超限时按比例与最大余数法分配。
复核单笔费用时，还可按结果标识读取**计费时间线**：由持久化的作业边界与合并暂停
扫描出首尾相接、覆盖整个作业区间的连续分段，逐段标明暂停、允许抵扣或计费。

技术栈：**Python 3.12 · FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL 16**，
容器化使用 Docker Compose，测试使用 pytest。

## 结算规则（单一事实来源）

1. 时间：仅接受 **RFC 3339、UTC、精确到整秒** 的时间戳（`2026-09-10T08:00:00Z`
   或 `...+00:00`）。小数秒（`.5`、`.000`）与非零偏移（`+08:00`）一律拒绝。
2. 作业区间为**左闭右开** `[work_start, work_end)`，`work_end` 必须严格晚于
   `work_start`。
3. 每个暂停区间同样为左闭右开，且结束必须严格晚于开始；
   **先裁剪到作业区间**（跨界暂停只取交集，作业外、仅端点相接的部分丢弃），
   再把**重叠或首尾相接**的区间合并；端点相等不会重复扣减、也不增加时长。
4. 净作业秒数 = 作业秒数 − 合并后暂停秒数；再扣除租约约定的**免计滞期允许秒数
   `allowed_seconds`**，实际扣减 `allowed_seconds_used` 以净作业秒数为上限
   （顺序不可颠倒：允许时长冲抵的是净作业时长，暂停不会被补回）；
   可计费秒数 = 净作业秒数 − 实际扣减，计费小时按**不足一小时向上取整**
   （零秒为零小时）。允许秒数为可选非负**严格整数**，省略按 0 处理，
   此时规则与历史完全一致。
5. 总分 = 计费小时 × 费率（非负整数，分/小时）。允许时长等于或超过净作业时长、
   或可计费秒数为零时，费用为零。
6. 只有成功计算才会写库，持久化内容包括：**原始输入、约定允许秒数与实际扣减、
   合并区间、可计费秒数、计费小时、总分值**等；时间倒置、空暂停（端点相等）、
   负费率、非法允许秒数等非法请求 **不会写入任何记录**。
   既有记录由 Alembic 迁移 `0002` 回填零允许时长，迁移前后费用完全一致。

## 目录结构

```
app/
  main.py          FastAPI 应用与 /health
  routers.py       POST/GET 结算接口、计费时间线、结果对比与航次封顶清单接口
  schemas.py       Pydantic v2 请求/响应模型（字段级、可定位错误）
  services.py      计算编排、持久化、序列化、时间线生成、封顶分配（最大余数法）
  intervals.py     裁剪 / 合并 / 取整 / 计费 / 时间线分段纯函数（规则核心）
  timeparse.py     严格 RFC 3339 UTC 整秒解析
  models.py        SQLAlchemy ORM（demurrage_records / voyage_cap_lists / voyage_cap_items）
  db.py config.py  引擎 / 会话 / 配置
alembic/           数据库迁移（0001_initial，0002 回填零允许时长，0003 航次封顶清单）
scripts/
  entrypoint.sh    等待数据库 → alembic upgrade → uvicorn
  verify.py        一次性黑盒验收脚本（仅标准库）
tests/             pytest（时间解析 / 区间规则与时间线 / API / 迁移）
docker-compose.yml Dockerfile requirements.txt
```

## Docker Compose 启动

```bash
# 宿主端口由 API_PORT 覆盖（默认 8000；容器内固定 8000）
API_PORT=9000 docker compose up --build -d

curl http://localhost:9000/health
# {"status":"ok"}
```

首次启动会自动等待 PostgreSQL 并执行 `alembic upgrade head`。
可用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8000` | 发布到宿主机的端口（`${API_PORT}:8000`） |
| `DATABASE_URL` | 指向 compose 中的 `db` | SQLAlchemy 连接串 |

## 一次性验收服务 `verify`

`verify` 是带 profile 的**一次性**服务：依赖 API 健康检查通过后执行
`scripts/verify.py` 黑盒验收并退出（`restart: no`）。

```bash
# 启动 db + api，健康检查通过后运行验收容器并退出
docker compose --profile verify run --rm verify
```

验收可直接观察到：

- **重复覆盖的停机只扣除一次**：4 段互相重叠/首尾相接的暂停合并为 `01:00–05:00`
  一段，暂停恰好 4 小时（而非 4+2+1+1 小时）；
- **跨界停机仅扣交集**：跨作业起点/终点的暂停只保留作业内的 30 分钟、15 分钟，
  作业外与仅端点相接的暂停不出现；
- **非法请求返回 422**，错误可定位到具体路径（如 `body/pauses/0/end`），
  响应中没有结果标识，按任何 id 都查不到该结算结果；
- **免计滞期允许秒数**：小于净作业时长时仅对余额计费；等于或超过净作业时长时
  费用为零且实际扣减以净作业为上限；暂停先合并、再扣允许时长（顺序不可颠倒）；
  省略该字段的旧格式请求按零处理，费用与历史一致。
- **结果对比**：费率/允许时长变化准确反映费用差额；暂停仅顺序不同不产生差异，
  实际区间变化如实可见；缺失标识的 404 指明基准或候选；相同标识对比为空差异。
- **航次封顶清单**：合计未超上限逐项照录；超限时按比例分配、最大余数法补足
  整分且总额精确等于上限；同余数按提交顺序分配；零费用项分得零；重复标识的
  422 定位到下标，缺失引用的 404 指出下标与标识；清单可按标识完整回放。
- **计费时间线**：相接暂停按已合并快照只形成连续分段，分段首尾相接且覆盖
  整个作业区间；允许时长跨多个工作段消耗并在段内精确切分，耗尽后的工作段
  计费；汇总回显与原结果一致；缺失标识得到带标识的 404。

退出码为 0 即验收通过。

## 接口

### 创建结算 `POST /api/v1/demurrage/calculations`

请求（`allowed_seconds` 可选，省略按 0）：

```json
{
  "work_start": "2026-09-10T00:00:00Z",
  "work_end": "2026-09-10T08:00:00Z",
  "rate_cents_per_hour": 100,
  "pauses": [
    {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T03:00:00Z"},
    {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T05:00:00Z"}
  ],
  "allowed_seconds": 3600
}
```

`201` 响应：

```json
{
  "id": "…uuid…",
  "work_start": "2026-09-10T00:00:00Z",
  "work_end": "2026-09-10T08:00:00Z",
  "pauses": [ "…原始输入…" ],
  "pauses_merged": [
    {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T05:00:00Z"}
  ],
  "rate_cents_per_hour": 100,
  "work_seconds": 28800,
  "paused_seconds": 14400,
  "allowed_seconds": 3600,
  "allowed_seconds_used": 3600,
  "billable_seconds": 10800,
  "billable_hours": 3,
  "total_cents": 300,
  "created_at": "2026-09-10T…Z"
}
```

`allowed_seconds` 是租约约定值（原样回显），`allowed_seconds_used` 是实际
扣减值（`min(allowed_seconds, 净作业秒数)`，约定值超过净作业时二者不同，
此时费用为零）。创建与回查路径保持不变，两字段在两个响应中均完整序列化。

### 按结果标识回查 `GET /api/v1/demurrage/calculations/{id}`

成功返回 `200`（结构同上）；不存在返回 `404`。

### 读取计费时间线 `GET /api/v1/demurrage/calculations/{id}/timeline`

复核单笔费用时，按结果标识读取**计费时间线**：服务读取已持久化的作业边界与
合并暂停，以边界扫描生成**首尾相接且覆盖整个作业区间**的连续分段——先标记
暂停段，再按时间先后把实际抵扣秒数（`allowed_seconds_used`）消耗在非暂停
区间（不足整段时在段内精确切分），剩余部分标记为计费。只读不写库。

`200` 响应（每段含起止、秒数与类别；汇总回显原记录值）：

```json
{
  "id": "…结果标识…",
  "segments": [
    {"start": "2026-09-10T00:00:00Z", "end": "2026-09-10T01:00:00Z",
     "seconds": 3600, "category": "allowed"},
    {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z",
     "seconds": 3600, "category": "pause"},
    {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T03:30:00Z",
     "seconds": 5400, "category": "allowed"},
    {"start": "2026-09-10T03:30:00Z", "end": "2026-09-10T04:30:00Z",
     "seconds": 3600, "category": "billable"}
  ],
  "paused_seconds": 3600,
  "allowed_seconds_used": 9000,
  "billable_seconds": 3600,
  "billable_hours": 1,
  "total_cents": 100
}
```

- `category` 取值：`pause`（暂停）、`allowed`（允许抵扣）、`billable`（计费）；
  分段按时间升序、首尾相接、无零长度段，合计恰为整个作业区间；
- 汇总字段（`paused_seconds` / `allowed_seconds_used` / `billable_seconds` /
  `billable_hours` / `total_cents`）回显原记录，与分段合计一致；
- 结果不存在：`404`，`detail` 回显标识；
- 持久化快照无法形成连续时间线（如合并暂停互相重叠、越界，或分段合计与
  记录不符）：`409` 数据一致性错误，`detail` 含标识与原因，**记录保持原样**。

### 对比两个既有结果 `POST /api/v1/demurrage/comparisons`

复核费用争议时提交两个已持久化的结果标识，返回**从基准到候选**的结构化差异；
只读取原记录，**不生成新的结算记录**。

请求：

```json
{
  "base_id": "…基准结果标识…",
  "candidate_id": "…候选结果标识…"
}
```

`200` 响应（`changes` 逐项标明是否变化，`deltas` 为候选 − 基准的有符号增减）：

```json
{
  "base_id": "…",
  "candidate_id": "…",
  "changes": {
    "work_start": false,
    "work_end": false,
    "pauses": false,
    "pauses_merged": false,
    "rate_cents_per_hour": true,
    "allowed_seconds": false
  },
  "deltas": {
    "paused_seconds": 0,
    "billable_seconds": 0,
    "billable_hours": 0,
    "total_cents": 400
  }
}
```

- 对比以**已持久化值**为准，时间格式与单条查询一致；
- 原始/合并暂停列表**先按时间排序再比较**，仅提交顺序不同不会报告差异，
  实际区间变化则如实标明；
- 两个标识相同时返回空差异（`changes` 全 `false`）与全零增减；
- 任一标识不存在时返回 `404`，`detail` 指明缺失的是 `base_id` 还是
  `candidate_id`；失败请求不写库。

### 生成航次封顶清单 `POST /api/v1/demurrage/voyage-caps`

同一航次在多个港口分别形成的结算结果，可由结算员汇总为一份**不可变**的
航次封顶清单：按提交顺序引用 **2 至 20 个**既有结果标识，并给定非负整数分的
赔付上限 `cap_cents`。

请求：

```json
{
  "result_ids": ["…结果标识1…", "…结果标识2…", "…结果标识3…"],
  "cap_cents": 100
}
```

分配规则：

1. 各项原费用（`total_cents`）合计**未超过**上限时逐项照录；
2. **超过**时按各项原费用比例分配上限：先取 `cap × 原费用 / 合计` 的整数部分，
   剩余整分按**最大余数法**逐项补足，**余数相同按提交顺序**（下标小者优先），
   **零费用项始终分得零**；分配总额精确等于上限。

`201` 响应（清单与明细为创建时刻快照，`created_at` 为创建时间）：

```json
{
  "id": "…清单标识…",
  "cap_cents": 100,
  "original_total_cents": 300,
  "allocated_total_cents": 100,
  "capped": true,
  "items": [
    {"position": 0, "result_id": "…", "original_cents": 100, "allocated_cents": 34},
    {"position": 1, "result_id": "…", "original_cents": 100, "allocated_cents": 33},
    {"position": 2, "result_id": "…", "original_cents": 100, "allocated_cents": 33}
  ],
  "created_at": "2026-09-11T…Z"
}
```

错误：

- `result_ids` 中标识重复：`422`，错误定位到重复出现的下标
  （`body/result_ids/<下标>`）；
- 引用的结果不存在：`404`，`detail` 指出缺失下标与标识
  （如 `result_ids[1]`）；
- `cap_cents` 非法（负数、小数、字符串、布尔等）：`422` 定位到
  `body/cap_cents`；结果数量不足 2 个或超过 20 个：`422` 定位到
  `body/result_ids`；
- 以上失败均**不会写入任何清单或明细**。

### 按清单标识回放 `GET /api/v1/demurrage/voyage-caps/{id}`

成功返回 `200`（结构与创建响应完全一致，明细按提交顺序）；不存在返回 `404`。

### 错误格式（422，字段可定位）

FastAPI/Pydantic 标准结构，每条错误含 `loc`（字段路径）、`msg`、`type`，例如：

```json
{
  "detail": [
    {"loc": ["body", "pauses", 0, "end"],
     "msg": "结束时间必须晚于开始时间",
     "type": "value_error"}
  ]
}
```

小数秒/非 UTC 偏移定位到 `body/<字段>`；暂停问题定位到
`body/pauses/<下标>/start|end`；费率与允许秒数问题分别定位到
`body/rate_cents_per_hour`、`body/allowed_seconds`
（均为严格非负整数，`1.5`、`3600.0`、字符串、布尔、负数均拒绝）。
多余字段（`extra="forbid"`）、缺字段同样 422。非法请求不落任何记录。

## 本地开发与测试（无需 Docker / PostgreSQL）

测试通过 `DATABASE_URL` 使用内存 SQLite：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

迁移在临时 SQLite 文件上验证 `upgrade head` / `downgrade base`。

本地用 SQLite 跑服务：

```bash
DATABASE_URL="sqlite+pysqlite:////tmp/demurrage.db" alembic upgrade head
DATABASE_URL="sqlite+pysqlite:////tmp/demurrage.db" \
  uvicorn app.main:app --reload
python -m scripts.verify http://127.0.0.1:8000   # 黑盒验收
```

生产部署使用 PostgreSQL（见 `docker-compose.yml`）。
