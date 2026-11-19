"""断网补录与号段预留的端到端测试（通过 FastAPI TestClient 走完整 HTTP 层）。

场景背景：两台扫码器断网时各自按设备顺序保存装箱日志，全线序号缺失；
数据员先预留连续空号段，再提交两份日志求解唯一合法交错顺序补录。
"""

import threading

from app.db import connect, init_db
from app.main import app
from fastapi.testclient import TestClient


def _scan(scan_no, seq, revision, child, parent):
    return {"scan_no": scan_no, "seq": seq, "revision": revision,
            "child": child, "parent": parent}


def _entry(scan_no, child, parent):
    return {"scan_no": scan_no, "child": child, "parent": parent}


def _logs(a_entries, b_entries):
    return {"logs": [
        {"device": "scanner-A", "entries": a_entries},
        {"device": "scanner-B", "entries": b_entries},
    ]}


def _reserve(client, start, length):
    r = client.post("/docs", json={"start_seq": start, "length": length})
    assert r.status_code == 201, r.text
    return r.json()["reservation_id"]


def _scan_count():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]


# --------------------------------------------------------------------------- #
# 号段预留
# --------------------------------------------------------------------------- #
def test_reserve_returns_id_and_queryable_state(client):
    rid = _reserve(client, 10, 4)

    one = client.get(f"/docs/{rid}")
    assert one.status_code == 200
    body = one.json()
    assert body["reservation_id"] == rid
    assert body["start_seq"] == 10 and body["length"] == 4
    assert body["status"] == "active"

    listed = client.get("/docs")
    assert listed.status_code == 200
    assert any(r["reservation_id"] == rid for r in listed.json())

    assert client.get("/docs/rsv-missing").status_code == 404


def test_reserve_length_and_start_validation(client):
    assert client.post("/docs", json={"start_seq": 1, "length": 1}).status_code == 422
    assert client.post("/docs", json={"start_seq": 1, "length": 17}).status_code == 422
    assert client.post("/docs", json={"start_seq": 0, "length": 2}).status_code == 422
    assert client.post("/docs", json={"start_seq": 1, "length": 2}).status_code == 201
    assert client.post("/docs", json={"start_seq": 100, "length": 16}).status_code == 201


def test_reserve_avoids_stored_seqs(client):
    client.post("/scans/batch", json={"scans": [_scan("S1", 5, 1, "B:b1", "C:c1")]})

    r = client.post("/docs", json={"start_seq": 4, "length": 2})  # [4,6) 含已入库 5
    assert r.status_code == 409 and r.json()["error"] == "range_unavailable"

    ok = client.post("/docs", json={"start_seq": 6, "length": 2})  # [6,8) 空闲
    assert ok.status_code == 201


def test_reserve_avoids_other_active_reservations(client):
    _reserve(client, 10, 4)  # [10,14)

    for start, length in [(9, 2), (12, 2), (10, 4), (9, 5), (13, 2)]:
        r = client.post("/docs", json={"start_seq": start, "length": length})
        assert r.status_code == 409, (start, length)
        assert r.json()["error"] == "range_unavailable"

    # 紧邻但不重叠：可以预留
    assert client.post("/docs", json={"start_seq": 7, "length": 2}).status_code == 201
    assert client.post("/docs", json={"start_seq": 14, "length": 2}).status_code == 201


def test_scans_batch_rejects_reserved_range(client):
    _reserve(client, 10, 3)  # 号段 {10, 11, 12}

    # 批次内任一笔落入号段 → 整批拒绝
    r = client.post("/scans/batch", json={"scans": [
        _scan("S1", 9, 1, "B:b1", "C:c1"),
        _scan("S2", 11, 1, "B:b2", "C:c1"),
    ]})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "seq_reserved" and body["conflict_seq"] == 11
    # 整批拒绝：号段外的 S1 也未入库
    assert _scan_count() == 0

    # 号段外照常受理
    ok = client.post("/scans/batch", json={"scans": [
        _scan("S1", 9, 1, "B:b1", "C:c1"),
        _scan("S3", 13, 1, "B:b3", "C:c1"),
    ]})
    assert ok.status_code == 200

    # 更正已有扫描到预留号段同样整批拒绝
    r2 = client.post("/scans/batch", json={"scans": [
        _scan("S1", 10, 2, "B:b1", "C:c1")]})
    assert r2.status_code == 409 and r2.json()["error"] == "seq_reserved"
    # 原扫描保持不变
    with connect() as conn:
        row = conn.execute("SELECT seq, revision FROM scans WHERE scan_no='S1'").fetchone()
        assert (row["seq"], row["revision"]) == (9, 1)


def test_release_frees_range_and_normal_scans_work(client):
    rid = _reserve(client, 10, 2)

    r = client.post(f"/docs/{rid}/release")
    assert r.status_code == 200 and r.json()["status"] == "released"

    # 释放后普通扫描可用这些序号
    ok = client.post("/scans/batch", json={"scans": [
        _scan("S1", 10, 1, "B:b1", "C:c1"),
        _scan("S2", 11, 1, "B:b2", "C:c1"),
    ]})
    assert ok.status_code == 200

    # 已释放的预留不能重复释放；不存在的预留 404
    assert client.post(f"/docs/{rid}/release").status_code == 409
    assert client.post("/docs/rsv-missing/release").status_code == 404

    # 释放后的号段不再阻挡新预留（此处另起相邻空闲段）
    assert client.post("/docs", json={"start_seq": 12, "length": 2}).status_code == 201


# --------------------------------------------------------------------------- #
# 补录：求解与入库
# --------------------------------------------------------------------------- #
def test_backfill_unique_solution_commits(client):
    # 已入库：c2 在 c1 内
    client.post("/scans/batch", json={"scans": [_scan("BASE", 1, 1, "C:c2", "C:c1")]})
    rid = _reserve(client, 10, 2)

    # A1: c1 装入 c2 —— 若 c2 仍在 c1 内则成环；B1: c2 上托盘可拆掉该边。
    # 唯一合法顺序：B1 先（10），A1 后（11）。
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "C:c1", "C:c2")],
        [_entry("B1", "C:c2", "P:p1")],
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "committed"
    assert body["reservation_id"] == rid
    assert body["assignments"] == [
        {"scan_no": "B1", "seq": 10},
        {"scan_no": "A1", "seq": 11},
    ]
    assert body["replayed_scans"] == 3

    # 预留被核销
    assert client.get(f"/docs/{rid}").json()["status"] == "consumed"
    # 补录扫描以修订 1 入库
    with connect() as conn:
        rows = conn.execute(
            "SELECT scan_no, seq, revision FROM scans ORDER BY seq"
        ).fetchall()
        assert [(r["scan_no"], r["seq"], r["revision"]) for r in rows] == [
            ("BASE", 1, 1), ("B1", 10, 1), ("A1", 11, 1)]

    # 层级已更新：盒路径 API 可核对（c1 在 c2 内，c2 在 p1 上）
    client.post("/scans/batch", json={"scans": [_scan("S9", 20, 1, "B:bx", "C:c1")]})
    assert client.get("/boxes/B:bx").json()["path"] == [
        "P:p1", "C:c2", "C:c1", "B:bx"]


def test_backfill_no_solution_keeps_reservation(client):
    rid = _reserve(client, 10, 2)
    # 两种交错都成环：c1->c2 与 c2->c1 无论谁先谁后
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "C:c1", "C:c2")],
        [_entry("B1", "C:c2", "C:c1")],
    ))
    assert r.status_code == 409
    assert r.json()["error"] == "no_valid_order"
    # 补录失败不核销预留，也不写入任何扫描
    assert client.get(f"/docs/{rid}").json()["status"] == "active"
    assert _scan_count() == 0


def test_backfill_ambiguous_returns_two_orders_nothing_committed(client):
    rid = _reserve(client, 10, 2)
    # 两种交错都合法：b1 入 c1、c1 上托盘，先后顺序不影响合法性
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "B:b1", "C:c1")],
        [_entry("B1", "C:c1", "P:p1")],
    ))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "ambiguous_order"
    orders = body["orders"]
    assert len(orders) == 2
    seq_sets = {tuple((x["scan_no"], x["seq"]) for x in order) for order in orders}
    assert seq_sets == {
        (("A1", 10), ("B1", 11)),
        (("B1", 10), ("A1", 11)),
    }
    # 两者都不入库，预留给后续处理
    assert _scan_count() == 0
    assert client.get(f"/docs/{rid}").json()["status"] == "active"


def test_backfill_log_size_must_fill_range(client):
    rid = _reserve(client, 10, 3)  # 号段长度 3

    # 两份日志共 2 笔，填不满号段
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "B:b1", "C:c1")],
        [_entry("B1", "B:b2", "C:c1")],
    ))
    assert r.status_code == 400 and r.json()["error"] == "log_size_mismatch"

    # 每份日志 1~8 笔、必须恰好两份（schema 校验）
    too_many = _logs([_entry(f"A{i}", "B:b", "C:c") for i in range(9)],
                     [_entry("B1", "B:b", "C:c")])
    assert client.post(f"/docs/{rid}/backfill", json=too_many).status_code == 422
    one_log = {"logs": [{"device": "A", "entries": [_entry("X1", "B:b", "C:c")]}]}
    assert client.post(f"/docs/{rid}/backfill", json=one_log).status_code == 422
    three_logs = {"logs": [
        {"device": "A", "entries": [_entry("X1", "B:b", "C:c")]},
        {"device": "B", "entries": [_entry("X2", "B:b", "C:c")]},
        {"device": "C", "entries": [_entry("X3", "B:b", "C:c")]},
    ]}
    assert client.post(f"/docs/{rid}/backfill", json=three_logs).status_code == 422
    # 空父码（拆出）是合法日志内容
    rid2 = _reserve(client, 20, 2)
    r2 = client.post(f"/docs/{rid2}/backfill", json=_logs(
        [_entry("A1", "B:b1", None)],
        [_entry("B1", "B:b2", "C:c1")],
    ))
    # 两种交错均合法（拆出无依赖）→ 多解，证明请求本身被正常受理
    assert r2.status_code == 409 and r2.json()["error"] == "ambiguous_order"


def test_backfill_scan_no_must_be_new_and_unique(client):
    client.post("/scans/batch", json={"scans": [_scan("S1", 1, 1, "B:b0", "C:c0")]})
    rid = _reserve(client, 10, 2)

    # 批内重复
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("DUP", "B:b1", "C:c1")],
        [_entry("DUP", "B:b2", "C:c1")],
    ))
    assert r.status_code == 400 and r.json()["error"] == "duplicate_scan_no"

    # 库中已用
    r2 = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("S1", "B:b1", "C:c1")],
        [_entry("N1", "B:b2", "C:c1")],
    ))
    assert r2.status_code == 400 and r2.json()["error"] == "scan_no_exists"


def test_backfill_requires_active_reservation(client):
    # 已释放的预留
    rid = _reserve(client, 10, 2)
    client.post(f"/docs/{rid}/release")
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "B:b1", "C:c1")],
        [_entry("B1", "B:b2", "C:c1")],
    ))
    assert r.status_code == 409 and r.json()["error"] == "reservation_conflict"

    # 不存在的预留
    r2 = client.post("/docs/rsv-missing/backfill", json=_logs(
        [_entry("A1", "B:b1", "C:c1")],
        [_entry("B1", "B:b2", "C:c1")],
    ))
    assert r2.status_code == 404

    # 已核销的预留：先构造一次成功补录
    client.post("/scans/batch", json={"scans": [_scan("BASE", 5, 1, "C:c2", "C:c1")]})
    rid2 = _reserve(client, 20, 2)
    ok = client.post(f"/docs/{rid2}/backfill", json=_logs(
        [_entry("C1", "C:c1", "C:c2")],
        [_entry("C2", "C:c2", "P:p1")],
    ))
    assert ok.status_code == 200
    again = client.post(f"/docs/{rid2}/backfill", json=_logs(
        [_entry("D1", "B:b1", "C:c1")],
        [_entry("D2", "B:b2", "C:c1")],
    ))
    assert again.status_code == 409
    assert again.json()["error"] == "reservation_conflict"


def test_backfill_replays_scans_beyond_the_range(client):
    # 号段之后已入库的扫描参与重放，决定交错是否合法
    client.post("/scans/batch", json={"scans": [_scan("TAIL", 20, 1, "C:c1", "C:c2")]})
    rid = _reserve(client, 10, 2)

    # 交错一：A1(c2->c1)@10、B1(c2->p1)@11，TAIL(c1->c2)@20 时 c2 已上托盘，合法。
    # 交错二：B1@10、A1@11，TAIL@20 时 c2 在 c1 内，c1->c2 成环，非法。
    # 若不重放号段之后的扫描，交错二会被误判为合法（多解）。
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "C:c2", "C:c1")],
        [_entry("B1", "C:c2", "P:p1")],
    ))
    assert r.status_code == 200, r.text
    assert r.json()["assignments"] == [
        {"scan_no": "A1", "seq": 10},
        {"scan_no": "B1", "seq": 11},
    ]


def test_backfill_multi_entry_logs_keep_device_order(client):
    # 初始层级：c2->c1->c0，且 c9->c2
    client.post("/scans/batch", json={"scans": [
        _scan("BASE1", 1, 1, "C:c2", "C:c1"),
        _scan("BASE2", 2, 1, "C:c1", "C:c0"),
        _scan("BASE3", 3, 1, "C:c9", "C:c2"),
    ]})
    rid = _reserve(client, 10, 4)

    # 链式依赖：A1 需 B1 先拆掉 c9->c2；B2 需 A1 先把 c2 移出 c1；
    # A2 需 B2 先把 c1 移出 c0。唯一合法交错：B1 A1 B2 A2。
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "C:c2", "C:c9"), _entry("A2", "C:c0", "C:c1")],
        [_entry("B1", "C:c9", "P:p1"), _entry("B2", "C:c1", "C:c2")],
    ))
    assert r.status_code == 200, r.text
    assignments = r.json()["assignments"]
    assert assignments == [
        {"scan_no": "B1", "seq": 10},
        {"scan_no": "A1", "seq": 11},
        {"scan_no": "B2", "seq": 12},
        {"scan_no": "A2", "seq": 13},
    ]
    # 各设备内部顺序保持不变
    seq_of = {a["scan_no"]: a["seq"] for a in assignments}
    assert seq_of["A1"] < seq_of["A2"]
    assert seq_of["B1"] < seq_of["B2"]

    # 全量重放后的层级：p1 <- c9 <- c2 <- c1 <- c0
    client.post("/scans/batch", json={"scans": [_scan("S9", 30, 1, "B:bx", "C:c0")]})
    assert client.get("/boxes/B:bx").json()["path"] == [
        "P:p1", "C:c9", "C:c2", "C:c1", "C:c0", "B:bx"]


def test_backfilled_scans_accept_later_corrections(client):
    # 补录入库的扫描就是普通扫描：可更正、受修订规则约束
    client.post("/scans/batch", json={"scans": [_scan("BASE", 1, 1, "C:c2", "C:c1")]})
    rid = _reserve(client, 10, 2)
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "C:c1", "C:c2")],
        [_entry("B1", "C:c2", "P:p1")],
    ))
    assert r.status_code == 200

    # 更高修订更正：把 c1 拆出
    r2 = client.post("/scans/batch", json={"scans": [_scan("A1", 11, 2, "C:c1", None)]})
    assert r2.status_code == 200

    # 低修订整批拒绝
    r3 = client.post("/scans/batch", json={"scans": [_scan("A1", 11, 1, "C:c1", "C:c2")]})
    assert r3.status_code == 409 and r3.json()["error"] == "revision_conflict"


# --------------------------------------------------------------------------- #
# 并发与持久化
# --------------------------------------------------------------------------- #
def test_backfill_conflict_when_scans_change_during_solve(client, monkeypatch):
    client.post("/scans/batch", json={"scans": [_scan("BASE", 1, 1, "C:c2", "C:c1")]})
    rid = _reserve(client, 10, 2)

    from app import service
    original = service.solve_interleavings

    def racing_solve(*args, **kwargs):
        solutions = original(*args, **kwargs)
        # 求解完成后、入库前：另一普通批次抢先提交（求解期间普通扫描可提交）
        r = client.post("/scans/batch", json={"scans": [
            _scan("RACE", 50, 1, "B:race", "C:race")]})
        assert r.status_code == 200
        return solutions

    monkeypatch.setattr(service, "solve_interleavings", racing_solve)
    r = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "C:c1", "C:c2")],
        [_entry("B1", "C:c2", "P:p1")],
    ))
    # 有效扫描已变 → 整批报冲突，预留不核销、扫描不入库
    assert r.status_code == 409
    assert r.json()["error"] == "reservation_conflict"
    assert client.get(f"/docs/{rid}").json()["status"] == "active"
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scans WHERE scan_no IN ('A1', 'B1')"
        ).fetchone()[0] == 0

    # 无竞争时重试即可成功（快照含 RACE，号段之后扫描照常参与重放）
    monkeypatch.setattr(service, "solve_interleavings", original)
    r2 = client.post(f"/docs/{rid}/backfill", json=_logs(
        [_entry("A1", "C:c1", "C:c2")],
        [_entry("B1", "C:c2", "P:p1")],
    ))
    assert r2.status_code == 200


def test_normal_scans_not_blocked_while_solving(client, monkeypatch):
    # 求解（可能较慢）不持有写锁：普通扫描在求解窗口内照常提交
    client.post("/scans/batch", json={"scans": [_scan("BASE", 1, 1, "C:c2", "C:c1")]})
    rid = _reserve(client, 10, 2)

    from app import service
    original = service.solve_interleavings
    entered = threading.Event()
    proceed = threading.Event()

    def slow_solve(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "solve_interleavings", slow_solve)

    result = {}

    def do_backfill():
        c = TestClient(app)
        # 唯一解场景：B1 先拆掉 c2->c1，A1 再装入
        result["resp"] = c.post(f"/docs/{rid}/backfill", json=_logs(
            [_entry("A1", "C:c1", "C:c2")],
            [_entry("B1", "C:c2", "P:p1")],
        ))

    t = threading.Thread(target=do_backfill)
    t.start()
    assert entered.wait(timeout=5)
    # 求解进行中：普通批次仍可提交
    r = client.post("/scans/batch", json={"scans": [
        _scan("DURING", 50, 1, "B:d", "C:d")]})
    assert r.status_code == 200
    proceed.set()
    t.join(timeout=5)
    # 但补录入库时检测到有效扫描已变 → 冲突
    assert result["resp"].status_code == 409
    assert result["resp"].json()["error"] == "reservation_conflict"


def test_reservation_state_persists_across_restart(tmp_path, monkeypatch):
    db_file = tmp_path / "restart-rsv.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(db_file))
    init_db(str(db_file))

    with TestClient(app) as c1:
        rid = _reserve(c1, 10, 2)
        assert c1.post("/scans/batch", json={"scans": [
            _scan("S1", 10, 1, "B:b1", "C:c1")]}).status_code == 409

    # 模拟重启：预留状态从 SQLite 恢复，号段仍然锁定
    with TestClient(app) as c2:
        assert c2.get(f"/docs/{rid}").json()["status"] == "active"
        assert c2.post("/scans/batch", json={"scans": [
            _scan("S1", 10, 1, "B:b1", "C:c1")]}).status_code == 409
        # 释放后号段开放
        assert c2.post(f"/docs/{rid}/release").status_code == 200
        assert c2.post("/scans/batch", json={"scans": [
            _scan("S1", 10, 1, "B:b1", "C:c1")]}).status_code == 200
