# 药品追溯装箱扫描 API

单条产线的装箱扫描服务：FastAPI + SQLite。数据员通过批量扫描确认每盒药当前
所在托盘，支持包装层级（盒 → 箱 → 箱/托盘）的转入、拆出、补传与更正。
两台扫码器断网时各自按设备顺序保存装箱日志（全线序号缺失），恢复后通过
号段预留 + 双设备日志求解完成断网补录。

## 包装编码规则

| 前缀 | 含义 | 规则 |
| --- | --- | --- |
| `B:` | 盒（最小包装） | 只能作为子项，只能装入 `C:` 箱 |
| `C:` | 箱 | 可装入另一 `C:` 箱或 `P:` 托盘 |
| `P:` | 托盘 | 只能作顶层父项，**不能作为子项** |

* 非空父码：子项转入该包装，并**原子替换旧归属**（任一时点不会同时归属两处）。
* 父码为 `null`：把子项从当前包装**拆出**。

## 扫描与修订语义

* 每批最多 **100 笔**，字段：`scan_no`（扫描号）、`seq`（全产线序号）、
  `revision`（修订号）、`child`（子码）、`parent`（目标父码或 null）。
* 有效扫描的 `seq` 全局唯一（批次内重复直接 400；与历史冲突在重放时 409）。
* 每个 `scan_no` 只保留**最高修订**：
  * 新修订号必须 **大于** 已存修订，否则整批拒绝（409）；
  * 同号同修订但内容（序号/子码/父码）不一致，整批拒绝（409）；
  * 同号同修订同文重提为幂等空操作。
* 所有生效扫描按 `seq` 全序**重放**，逐笔重建层级；任一笔违反角色规则、
  形成环或序号冲突，整批拒绝并返回**首个冲突序号** `conflict_seq`，
  原扫描与层级保持不变。
* 补传/更正导致后续已存扫描变非法时，同样整批拒绝（重放覆盖全量历史）。
* 新增或更正到**有效预留号段**内的序号时，整批拒绝（409 `seq_reserved`）。

## 断网补录（号段预留 + 双设备日志求解）

两台扫码器断网时各自按设备顺序保存装箱日志，全线序号缺失。恢复后按
以下流程补录：

1. **预留号段**：`POST /docs` 按起始序号和长度预留 **2~16** 个连续空号，
   返回预留号。号段须避开已入库序号和其他有效预留，否则 409。
2. **锁定号段**：预留生效期间，`/scans/batch` 新增或更正到号段内序号
   即整批拒绝；号段外照常受理。
3. **补录提交**：`POST /docs/{预留号}/backfill` 提交两份设备日志
   （各 **1~8** 笔，总笔数须恰好填满号段）。每笔填新扫描号、子码和
   目标父码（空父码表示拆出）；扫描号批内唯一且库中未用。
   求解器枚举两份日志的全部交错（**各设备内部顺序不变**），把新扫描
   以修订 1 放进号段，连同**号段之后已入库的扫描**一起按原包装规则
   全量重放：
   * **仅一种合法顺序**：整批写入修订 1，同时核销预留、更新层级，
     当次返回各扫描号及分配的全线序号；
   * **无解**：报告无解（409 `no_valid_order`）；
   * **多种合法顺序**：返回其中两种合法顺序（409 `ambiguous_order`），
     两者都不入库。
4. **不核销失败**：无解、多解、冲突等任何失败都不核销预留；可修正
   日志后重试，或 `POST /docs/{预留号}/release` **释放**预留 ——
   释放未核销的预留后，普通扫描可使用这些序号。
5. **并发**：求解基于一致性快照在写锁外进行，求解期间普通扫描仍可
   提交；入库时在写事务内复核 —— 若原预留已释放、已核销或有效扫描
   已变（`scans_version` 不一致），整批报冲突（409 `reservation_conflict`）。

预留状态（`active` / `released` / `consumed`）持久化在 SQLite，
重启后保留；已提交的层级随时可用 `GET /boxes/{box}` 核对。

## 一致性保证

* 校验、重放、扫描表 upsert、归属边表整体替换在**同一个 SQLite 事务**内完成，
  失败即回滚，不会只更新上层箱而漏掉内盒。
* WAL 模式 + 进程内写锁串行化并发提交，读写并发下不会出现混合修订。
* 数据落盘到 Docker 卷，重启后从已提交状态恢复。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

* API：<http://localhost:8000>
* 交互式文档（Swagger）：<http://localhost:8000/swagger>
* OpenAPI：<http://localhost:8000/openapi.json>
* SQLite 数据持久化在命名卷 `trace-data`（容器内 `/data/trace.db`）。

## 本地开发（无 Docker）

```bash
pip install -r requirements.txt
pip install pytest httpx
TRACE_DB_PATH=./data/trace.db uvicorn app.main:app --reload
pytest          # 35 个端到端测试
```

## 接口

### `POST /scans/batch` 提交一批扫描

```json
{
  "scans": [
    {"scan_no": "S001", "seq": 1, "revision": 1, "child": "B:box01", "parent": "C:case01"},
    {"scan_no": "S002", "seq": 2, "revision": 1, "child": "C:case01", "parent": "P:pal01"},
    {"scan_no": "S003", "seq": 3, "revision": 1, "child": "B:box02", "parent": null}
  ]
}
```

成功响应（仅列出**路径发生变化的盒码**，含旧/新托盘与完整路径）：

```json
{
  "accepted": true,
  "replayed_scans": 3,
  "changes": [
    {
      "box": "B:box01",
      "old_pallet": null,
      "new_pallet": "P:pal01",
      "old_path": ["B:box01"],
      "new_path": ["P:pal01", "C:case01", "B:box01"]
    }
  ]
}
```

冲突响应（HTTP 409，指出首个冲突序号，整批未生效）：

```json
{"error": "replay_conflict", "message": "装入 C:c2 会使 C:c1 的层级形成环", "conflict_seq": 11}
```

序号落入有效预留号段（HTTP 409，整批未生效）：

```json
{"error": "seq_reserved", "message": "序号 10 处于有效预留 rsv-… 的号段 [10, 12)，整批拒绝", "conflict_seq": 10, "reservation_id": "rsv-…"}
```

| 状态码 | 场景 |
| --- | --- |
| 200 | 整批生效 |
| 400 | 空批次、批次内扫描号/序号重复 |
| 409 | 修订冲突、序号历史冲突、角色违规、环（带 `conflict_seq`）、序号在有效预留号段内 |
| 422 | 字段类型/编码前缀/批次超 100 笔等 schema 校验失败 |

### `POST /docs` 预留连续空号段

```json
{"start_seq": 10, "length": 4}
```

成功（HTTP 201）：

```json
{
  "reservation_id": "rsv-1a2b3c4d5e6f",
  "start_seq": 10,
  "length": 4,
  "status": "active",
  "created_at": "…",
  "updated_at": "…"
}
```

| 状态码 | 场景 |
| --- | --- |
| 201 | 预留成功，返回预留号 |
| 409 | 号段与已入库序号或其他有效预留重叠（`range_unavailable`） |
| 422 | 起始序号 < 1 或长度不在 2~16 |

### `GET /docs` / `GET /docs/{reservation_id}` 查询预留

列出全部预留（含已释放/已核销）或按预留号查询状态。

### `POST /docs/{reservation_id}/release` 释放预留

仅未核销（`active`）的预留可释放；释放后普通扫描可使用这些序号。
已释放/已核销的预留重复释放返回 409，预留号不存在返回 404。

### `POST /docs/{reservation_id}/backfill` 断网补录

```json
{
  "logs": [
    {"device": "scanner-A", "entries": [
      {"scan_no": "A1", "child": "C:case01", "parent": "C:case02"}
    ]},
    {"device": "scanner-B", "entries": [
      {"scan_no": "B1", "child": "C:case02", "parent": "P:pal01"}
    ]}
  ]
}
```

唯一合法顺序时整批写入（HTTP 200）：

```json
{
  "status": "committed",
  "reservation_id": "rsv-1a2b3c4d5e6f",
  "assignments": [
    {"scan_no": "B1", "seq": 10},
    {"scan_no": "A1", "seq": 11}
  ],
  "replayed_scans": 3,
  "changes": []
}
```

| 状态码 | 场景 |
| --- | --- |
| 200 | 唯一合法顺序：整批写入修订 1、核销预留，返回序号分配 |
| 400 | 总笔数未填满号段、扫描号批内重复或库中已用 |
| 404 | 预留号不存在 |
| 409 | 无解（`no_valid_order`）、多解（`ambiguous_order`，附两种合法顺序且均不入库）、预留已释放/已核销或有效扫描已变（`reservation_conflict`） |
| 422 | 日志份数不为 2、单份日志不在 1~8 笔、编码前缀非法等 |

### `GET /boxes/{box}` 按盒查询当前完整包装路径

```bash
curl http://localhost:8000/boxes/B:box01
```

```json
{
  "box": "B:box01",
  "parent": "C:case01",
  "pallet": "P:pal01",
  "path": ["P:pal01", "C:case01", "B:box01"]
}
```

盒从未出现或最后一次生效操作为拆出时返回 404。

### `GET /health` 健康检查

## 项目结构

```
app/
  main.py      FastAPI 路由与异常映射
  service.py   修订合并、按序号重放、环/角色/双归属校验、路径计算、
               号段预留、双设备日志交错求解与补录入库
  db.py        SQLite 连接、WAL、写锁、建表（scans/edges/reservations/meta）
  schemas.py   Pydantic 请求/响应模型
tests/         端到端测试（含并发、重启恢复、整批回滚、断网补录求解）
Dockerfile
docker-compose.yml
```
