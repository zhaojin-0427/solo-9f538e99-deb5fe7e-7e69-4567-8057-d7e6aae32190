"""核心业务逻辑：扫描修订合并、按序号重放、层级校验、路径计算与断网补录。

设计要点
--------
1. 每个扫描号只保留最高修订；同号同修订异文整批拒绝，低修订整批拒绝。
2. 所有生效扫描按 ``seq`` 全序重放，先在内存中构建归属边表，全部合法后
   才在同一事务内整体替换数据库边表并 upsert 扫描表 —— 任一步失败则
   回滚，旧扫描与层级保持不变，不会只更新上层箱而漏掉内盒。
3. 重放中发现的首个非法序号通过 ``ReplayConflict.conflict_seq`` 返回。
4. 断网期间两台扫码器各自按设备顺序保存日志：数据员先通过 ``/docs``
   预留一段连续空号（避开已入库序号与其他有效预留），普通批次新增或
   更正到有效预留号段内即整批拒绝；释放未核销的预留后号段重新开放。
5. 补录提交预留号与两份设备日志：枚举两份日志的全部交错（各自内部
   顺序不变），把新扫描（修订 1）放进号段，连同号段之后已入库的扫描
   一起按原包装规则全量重放。仅一种合法顺序时整批写入并核销预留；
   无解报告无解；多解返回两种合法顺序且均不入库。失败一律不核销预留。
6. 求解基于一致性快照在写锁外进行，普通扫描在求解期间仍可提交；
   入库前在写事务内复核预留状态与扫描版本（``meta.scans_version``），
   预留已释放/已核销或有效扫描已变即整批报冲突。
"""

from __future__ import annotations

import sqlite3
import uuid
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .db import connect, transaction, write_lock
from .schemas import BackfillIn, BatchIn, LogEntryIn, ReservationIn, ScanIn


class BatchError(Exception):
    """批次自身的静态校验错误（HTTP 400）。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class ReplayConflict(Exception):
    """重放层级规则时的冲突（HTTP 409），带首个冲突序号。"""

    def __init__(self, conflict_seq: int, reason: str) -> None:
        super().__init__(reason)
        self.conflict_seq = conflict_seq
        self.reason = reason


class RevisionConflict(Exception):
    """扫描号修订冲突（HTTP 409）。"""

    def __init__(self, scan_no: str, seq: Optional[int], reason: str) -> None:
        super().__init__(reason)
        self.scan_no = scan_no
        self.seq = seq
        self.reason = reason


class SeqReserved(Exception):
    """批次使用了有效预留号段内的序号（HTTP 409）。"""

    def __init__(self, seq: int, reservation_id: str, start: int, length: int) -> None:
        super().__init__(f"序号 {seq} 处于有效预留 {reservation_id} 的号段内")
        self.seq = seq
        self.reservation_id = reservation_id
        self.start = start
        self.length = length


class ReservationNotFound(Exception):
    """预留号不存在（HTTP 404）。"""

    def __init__(self, reservation_id: str) -> None:
        super().__init__(f"预留 {reservation_id} 不存在")
        self.reservation_id = reservation_id


class ReservationUnavailable(Exception):
    """号段与已入库序号或有效预留重叠，无法预留（HTTP 409）。"""


class ReservationStateConflict(Exception):
    """预留状态或扫描版本冲突（HTTP 409）：已释放/已核销/有效扫描已变。"""

    def __init__(self, reason: str, reservation_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.reservation_id = reservation_id


class NoValidOrder(Exception):
    """两份日志的所有交错顺序均违反包装规则（HTTP 409），预留不核销。"""

    def __init__(self, reservation_id: str) -> None:
        super().__init__("两份日志的所有交错顺序均违反包装规则，无合法顺序")
        self.reservation_id = reservation_id


class AmbiguousOrder(Exception):
    """存在多种合法顺序（HTTP 409）：返回两种示例，均不入库，预留不核销。"""

    def __init__(self, reservation_id: str, orders: List[List[Dict[str, Any]]]) -> None:
        super().__init__("存在多种合法顺序，均未入库")
        self.reservation_id = reservation_id
        self.orders = orders


def kind(code: str) -> str:
    """返回包装层级角色：'B' 盒 / 'C' 箱 / 'P' 托盘。"""

    return code[0]


# --------------------------------------------------------------------------- #
# 层级工具函数（纯函数，便于测试）
# --------------------------------------------------------------------------- #
def path_of(edges: Dict[str, str], code: str) -> List[str]:
    """自顶（托盘/最外层）向底（该码）返回完整包装路径。"""

    chain = [code]
    cur = edges.get(code)
    seen = {code}
    while cur is not None:
        if cur in seen:  # 防御性：正常流程重放时已保证无环
            raise ReplayConflict(-1, "包装层级存在环")
        seen.add(cur)
        chain.append(cur)
        cur = edges.get(cur)
    chain.reverse()
    return chain


def pallet_of(edges: Dict[str, str], code: str) -> Optional[str]:
    """返回当前所在托盘（路径上首个 P: 节点），未上托盘返回 None。"""

    for node in path_of(edges, code):
        if node.startswith("P:"):
            return node
    return None


def validate_batch(batch: BatchIn) -> None:
    """批次内静态校验：非空、扫描号不重复、序号不重复。"""

    if not batch.scans:
        raise BatchError("empty_batch", "每批至少提交 1 笔扫描")

    scan_nos: Dict[str, ScanIn] = {}
    seqs: Dict[int, ScanIn] = {}
    for item in batch.scans:
        if item.scan_no in scan_nos:
            raise BatchError(
                "duplicate_scan_no",
                f"批次内扫描号重复: {item.scan_no}",
                scan_no=item.scan_no,
            )
        scan_nos[item.scan_no] = item

        if item.seq in seqs:
            raise BatchError(
                "duplicate_seq",
                f"批次内序号重复: {item.seq}",
                conflict_seq=item.seq,
            )
        seqs[item.seq] = item


def _check_role(seq: int, child: str, parent: str) -> None:
    """包装角色规则：盒只能入箱；箱可入箱/托盘；托盘不作子项。"""

    child_kind = kind(child)
    parent_kind = kind(parent)

    if child_kind == "P":
        raise ReplayConflict(seq, f"托盘 {child} 不能作为子项装入 {parent}")
    if child_kind == "B" and parent_kind != "C":
        raise ReplayConflict(seq, f"盒 {child} 只能装入 C: 开头的箱，不能装入 {parent}")
    if child_kind == "C" and parent_kind not in ("C", "P"):
        raise ReplayConflict(seq, f"箱 {child} 只能装入另一箱或托盘，不能装入 {parent}")


def _check_cycle(seq: int, edges: Dict[str, str], child: str, parent: str) -> None:
    """若把 child 挂到 parent 下会形成环，则按当前序号拒绝。"""

    cur: Optional[str] = parent
    while cur is not None:
        if cur == child:
            raise ReplayConflict(seq, f"装入 {parent} 会使 {child} 的层级形成环")
        cur = edges.get(cur)


def _apply_record(edges: Dict[str, str], rec: Dict[str, Any]) -> None:
    """在归属边表上应用一笔扫描（拆出/转入），违反角色或成环即抛冲突。"""

    child, parent, seq = rec["child"], rec["parent"], rec["seq"]
    if parent is None:
        # 拆出：删除当前归属（若本来就无归属则为空操作，仍合法）
        edges.pop(child, None)
        return

    _check_role(seq, child, parent)
    _check_cycle(seq, edges, child, parent)
    # 直接覆盖：一个子项任一时点只能归属一处，旧归属被原子替换
    edges[child] = parent


def replay_records(
    records: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """对已合并的扫描记录按序号全序重放，返回 (有序记录, 新归属边表)。"""

    ordered = sorted(records, key=lambda r: (r["seq"], r["scan_no"]))

    # 序号全局唯一（含历史扫描）
    seq_owner: Dict[int, str] = {}
    for rec in ordered:
        owner = seq_owner.get(rec["seq"])
        if owner is not None and owner != rec["scan_no"]:
            raise ReplayConflict(rec["seq"], f"序号 {rec['seq']} 已被扫描号 {owner} 占用")
        seq_owner[rec["seq"]] = rec["scan_no"]

    # 按序号重放层级
    edges: Dict[str, str] = {}
    for rec in ordered:
        _apply_record(edges, rec)

    return ordered, edges


def replay(
    stored: List[sqlite3.Row], incoming: List[ScanIn]
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """合并已存扫描与本批扫描（含修订检查），按序号重放。"""

    merged: Dict[str, Dict[str, Any]] = {
        row["scan_no"]: {
            "scan_no": row["scan_no"],
            "seq": row["seq"],
            "revision": row["revision"],
            "child": row["child"],
            "parent": row["parent"],
        }
        for row in stored
    }

    # 修订检查与合并
    for item in incoming:
        old = merged.get(item.scan_no)
        if old is not None:
            if item.revision < old["revision"]:
                raise RevisionConflict(
                    item.scan_no,
                    item.seq,
                    f"修订号必须递增：已存修订 {old['revision']}，收到 {item.revision}",
                )
            if item.revision == old["revision"] and (
                item.child != old["child"]
                or item.parent != old["parent"]
                or item.seq != old["seq"]
            ):
                raise RevisionConflict(
                    item.scan_no,
                    item.seq,
                    f"扫描号 {item.scan_no} 同修订 {item.revision} 内容不一致，整批拒绝",
                )
        merged[item.scan_no] = {
            "scan_no": item.scan_no,
            "seq": item.seq,
            "revision": item.revision,
            "child": item.child,
            "parent": item.parent,
        }

    return replay_records(list(merged.values()))


def _load_state(conn: sqlite3.Connection) -> Tuple[List[sqlite3.Row], Dict[str, str]]:
    stored = conn.execute("SELECT * FROM scans").fetchall()
    old_edges = {
        row["child"]: row["parent"]
        for row in conn.execute("SELECT child, parent FROM edges")
    }
    return stored, old_edges


def _compute_changes(
    old_edges: Dict[str, str], new_edges: Dict[str, str]
) -> List[Dict[str, Any]]:
    """对比新旧归属边表，返回路径发生变化的盒码清单。"""

    box_codes = sorted(
        {c for c in old_edges if c.startswith("B:")}
        | {c for c in new_edges if c.startswith("B:")}
    )
    changes = []
    for box in box_codes:
        old_path = path_of(old_edges, box) if box in old_edges else [box]
        new_path = path_of(new_edges, box) if box in new_edges else [box]
        if old_path == new_path:
            continue
        changes.append(
            {
                "box": box,
                "old_pallet": pallet_of(old_edges, box) if box in old_edges else None,
                "new_pallet": pallet_of(new_edges, box) if box in new_edges else None,
                "old_path": old_path,
                "new_path": new_path,
            }
        )
    return changes


_SCAN_UPSERT = """
INSERT INTO scans(scan_no, seq, revision, child, parent)
VALUES (:scan_no, :seq, :revision, :child, :parent)
ON CONFLICT(scan_no) DO UPDATE SET
    seq = excluded.seq,
    revision = excluded.revision,
    child = excluded.child,
    parent = excluded.parent,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
"""


def _scans_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'scans_version'"
    ).fetchone()
    return 0 if row is None else int(row["value"])


def _bump_scans_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('scans_version', 1) "
        "ON CONFLICT(key) DO UPDATE SET value = value + 1"
    )


def _replace_edges(conn: sqlite3.Connection, new_edges: Dict[str, str]) -> None:
    """整体替换边表：删除与插入在同一事务内，读者只见旧层级或新层级。"""

    conn.execute("DELETE FROM edges")
    conn.executemany(
        "INSERT INTO edges(child, parent) VALUES (?, ?)",
        list(new_edges.items()),
    )


def _check_reserved_seqs(conn: sqlite3.Connection, scans: List[ScanIn]) -> None:
    """新增或更正到有效预留号段内的序号，整批拒绝。"""

    rows = conn.execute(
        "SELECT reservation_id, start_seq, length FROM reservations "
        "WHERE status = 'active'"
    ).fetchall()
    if not rows:
        return
    for item in scans:
        for r in rows:
            if r["start_seq"] <= item.seq < r["start_seq"] + r["length"]:
                raise SeqReserved(
                    item.seq, r["reservation_id"], r["start_seq"], r["length"]
                )


def submit_batch(batch: BatchIn) -> Dict[str, Any]:
    """提交一批扫描；成功返回盒码路径变化，失败保证旧状态不变。"""

    validate_batch(batch)

    with write_lock, transaction() as conn:
        stored, old_edges = _load_state(conn)
        _check_reserved_seqs(conn, batch.scans)
        ordered, new_edges = replay(stored, batch.scans)
        changes = _compute_changes(old_edges, new_edges)

        # 幂等重提（同号同修订同文）不改变有效扫描，不推进版本号
        stored_by_no = {row["scan_no"]: row for row in stored}
        content_changed = any(
            item.scan_no not in stored_by_no
            or stored_by_no[item.scan_no]["revision"] != item.revision
            for item in batch.scans
        )

        _replace_edges(conn, new_edges)
        # upsert 本批扫描（同号同修订重提为幂等空写）
        conn.executemany(_SCAN_UPSERT, [item.model_dump() for item in batch.scans])
        if content_changed:
            _bump_scans_version(conn)
        return {"accepted": True, "replayed_scans": len(ordered), "changes": changes}


def get_box_path(box: str) -> Optional[Dict[str, Any]]:
    """按盒查询完整当前包装路径；盒从未出现或已彻底移除返回 None。"""

    if not box.startswith("B:"):
        raise BatchError("bad_box_code", f"{box} 不是 B: 开头的盒码")

    conn = connect()
    try:
        row = conn.execute("SELECT 1 FROM scans WHERE child = ? LIMIT 1", (box,)).fetchone()
        if row is None:
            return None
        edges = {
            r["child"]: r["parent"]
            for r in conn.execute("SELECT child, parent FROM edges")
        }
    finally:
        conn.close()

    if box not in edges:  # 最后一次生效操作是拆出
        return None

    path = path_of(edges, box)
    return {
        "box": box,
        "parent": edges.get(box),
        "pallet": pallet_of(edges, box),
        "path": path,
    }


# --------------------------------------------------------------------------- #
# 号段预留（/docs）
# --------------------------------------------------------------------------- #
_STATUS_CN = {"active": "有效", "released": "释放", "consumed": "核销"}


def _reservation_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "reservation_id": row["reservation_id"],
        "start_seq": row["start_seq"],
        "length": row["length"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_reservation(payload: ReservationIn) -> Dict[str, Any]:
    """预留 [start_seq, start_seq+length) 连续空号，返回预留号。

    号段须避开已入库序号与其他有效预留；已释放/已核销的预留不再占位。
    """

    start, length = payload.start_seq, payload.length
    end = start + length
    with write_lock, transaction() as conn:
        hit = conn.execute(
            "SELECT seq FROM scans WHERE seq >= ? AND seq < ? ORDER BY seq LIMIT 1",
            (start, end),
        ).fetchone()
        if hit is not None:
            raise ReservationUnavailable(
                f"号段 [{start}, {end}) 与已入库序号 {hit['seq']} 冲突"
            )
        hit = conn.execute(
            "SELECT reservation_id, start_seq, length FROM reservations "
            "WHERE status = 'active' AND start_seq < ? AND start_seq + length > ? "
            "LIMIT 1",
            (end, start),
        ).fetchone()
        if hit is not None:
            raise ReservationUnavailable(
                f"号段 [{start}, {end}) 与有效预留 {hit['reservation_id']} 的号段 "
                f"[{hit['start_seq']}, {hit['start_seq'] + hit['length']}) 重叠"
            )
        reservation_id = f"rsv-{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO reservations(reservation_id, start_seq, length) "
            "VALUES (?, ?, ?)",
            (reservation_id, start, length),
        )
        row = conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return _reservation_dict(row)


def list_reservations() -> List[Dict[str, Any]]:
    """列出全部预留（含已释放/已核销），按创建先后排序。"""

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM reservations ORDER BY created_at, reservation_id"
        ).fetchall()
        return [_reservation_dict(row) for row in rows]
    finally:
        conn.close()


def get_reservation(reservation_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ReservationNotFound(reservation_id)
    return _reservation_dict(row)


def release_reservation(reservation_id: str) -> Dict[str, Any]:
    """释放未核销的预留；释放后普通扫描可使用这些序号。"""

    with write_lock, transaction() as conn:
        row = conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ReservationNotFound(reservation_id)
        if row["status"] != "active":
            raise ReservationStateConflict(
                f"预留 {reservation_id} 已{_STATUS_CN[row['status']]}，"
                "仅未核销的有效预留可释放",
                reservation_id,
            )
        conn.execute(
            "UPDATE reservations SET status = 'released', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE reservation_id = ?",
            (reservation_id,),
        )
        row = conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return _reservation_dict(row)


# --------------------------------------------------------------------------- #
# 断网补录：双设备日志交错求解
# --------------------------------------------------------------------------- #
def solve_interleavings(
    stored_records: Sequence[Dict[str, Any]],
    entries_a: Sequence[LogEntryIn],
    entries_b: Sequence[LogEntryIn],
    start_seq: int,
    max_solutions: int = 2,
) -> List[List[Dict[str, Any]]]:
    """枚举两份设备日志的全部交错（各自内部顺序不变），返回合法顺序列表。

    每个解是按全线序号升序的新扫描记录列表（修订恒为 1）。重放从号段
    之前已入库扫描构建的层级出发，应用本段新扫描后继续应用号段之后
    已入库的扫描 —— 与正常提交同一套包装规则、同一份全量历史。
    至多返回 ``max_solutions`` 个解（调用方只需区分 0 / 1 / 多种）。
    """

    total = len(entries_a) + len(entries_b)
    end_seq = start_seq + total
    prefix = [r for r in stored_records if r["seq"] < start_seq]
    suffix = sorted(
        (r for r in stored_records if r["seq"] >= end_seq),
        key=lambda r: (r["seq"], r["scan_no"]),
    )
    # 防御：有效预留号段内不应存在已入库序号；若有则任何交错都序号冲突
    if any(start_seq <= r["seq"] < end_seq for r in stored_records):
        return []
    try:
        _, base_edges = replay_records(prefix)
    except ReplayConflict:  # 已入库历史不可能非法；防御
        return []

    solutions: List[List[Dict[str, Any]]] = []
    for a_slots in combinations(range(total), len(entries_a)):
        a_set = frozenset(a_slots)
        records: List[Dict[str, Any]] = []
        ai = bi = 0
        for offset in range(total):
            if offset in a_set:
                entry = entries_a[ai]
                ai += 1
            else:
                entry = entries_b[bi]
                bi += 1
            records.append(
                {
                    "scan_no": entry.scan_no,
                    "seq": start_seq + offset,
                    "revision": 1,
                    "child": entry.child,
                    "parent": entry.parent,
                }
            )
        edges = dict(base_edges)
        ok = True
        for rec in records + suffix:
            try:
                _apply_record(edges, rec)
            except ReplayConflict:
                ok = False
                break
        if ok:
            solutions.append(records)
            if len(solutions) >= max_solutions:
                break
    return solutions


def backfill_reservation(reservation_id: str, payload: BackfillIn) -> Dict[str, Any]:
    """断网补录：对预留号段求解两份设备日志的唯一合法交错并整批写入。

    * 两份日志各 1~8 笔，总笔数须恰好填满号段；
    * 每笔为新扫描号（批内唯一且库中未用），修订恒为 1；
    * 仅一种合法顺序：整批写入、核销预留、更新层级，返回序号分配；
    * 无解：报告无解；多解：返回两种合法顺序，两者都不入库；
    * 求解期间普通扫描仍可提交；入库时若预留已释放/已核销或有效
      扫描已变，整批报冲突；任何失败都不核销预留。
    """

    entries_a = payload.logs[0].entries
    entries_b = payload.logs[1].entries
    all_entries = list(entries_a) + list(entries_b)

    seen: set = set()
    for entry in all_entries:
        if entry.scan_no in seen:
            raise BatchError(
                "duplicate_scan_no",
                f"批内扫描号重复: {entry.scan_no}",
                scan_no=entry.scan_no,
            )
        seen.add(entry.scan_no)

    # 一致性快照：预留状态 + 已存扫描 + 扫描版本（短暂持锁读后即释放）
    with write_lock:
        conn = connect()
        try:
            rrow = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            stored = conn.execute("SELECT * FROM scans").fetchall()
            version = _scans_version(conn)
        finally:
            conn.close()

    if rrow is None:
        raise ReservationNotFound(reservation_id)
    if rrow["status"] != "active":
        raise ReservationStateConflict(
            f"预留 {reservation_id} 已{_STATUS_CN[rrow['status']]}，无法补录",
            reservation_id,
        )
    start_seq = rrow["start_seq"]
    length = rrow["length"]
    if len(all_entries) != length:
        raise BatchError(
            "log_size_mismatch",
            f"两份日志共 {len(all_entries)} 笔，须恰好填满号段长度 {length}",
            expected=length,
            actual=len(all_entries),
        )
    stored_records = [dict(row) for row in stored]
    used_scan_nos = {rec["scan_no"] for rec in stored_records}
    for entry in all_entries:
        if entry.scan_no in used_scan_nos:
            raise BatchError(
                "scan_no_exists",
                f"扫描号 {entry.scan_no} 库中已存在",
                scan_no=entry.scan_no,
            )

    # 求解在写锁外进行：普通扫描在此期间仍可提交
    solutions = solve_interleavings(stored_records, entries_a, entries_b, start_seq)

    if not solutions:
        raise NoValidOrder(reservation_id)
    if len(solutions) > 1:
        orders = [
            [{"scan_no": rec["scan_no"], "seq": rec["seq"]} for rec in sol]
            for sol in solutions[:2]
        ]
        raise AmbiguousOrder(reservation_id, orders)

    solution = solutions[0]

    # 唯一合法顺序：写事务内复核快照有效性后整体入库
    with write_lock, transaction() as conn:
        r2 = conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if r2 is None or r2["status"] != "active":
            raise ReservationStateConflict(
                f"预留 {reservation_id} 在求解期间已释放或核销，整批拒绝",
                reservation_id,
            )
        if _scans_version(conn) != version:
            raise ReservationStateConflict(
                "求解期间有效扫描已变化，整批拒绝",
                reservation_id,
            )

        stored2, old_edges = _load_state(conn)
        # 快照未变，重放结果与求解一致；仍在事务内复算以保证不变式
        ordered, new_edges = replay_records(
            [dict(row) for row in stored2] + solution
        )
        changes = _compute_changes(old_edges, new_edges)

        _replace_edges(conn, new_edges)
        conn.executemany(_SCAN_UPSERT, solution)
        conn.execute(
            "UPDATE reservations SET status = 'consumed', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE reservation_id = ?",
            (reservation_id,),
        )
        _bump_scans_version(conn)

    return {
        "status": "committed",
        "reservation_id": reservation_id,
        "assignments": [
            {"scan_no": rec["scan_no"], "seq": rec["seq"]} for rec in solution
        ],
        "replayed_scans": len(ordered),
        "changes": changes,
    }
