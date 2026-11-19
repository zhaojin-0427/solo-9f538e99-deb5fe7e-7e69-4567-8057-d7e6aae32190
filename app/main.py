"""FastAPI 入口：单条产线装箱扫描 API（含断网补录号段预留）。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .schemas import (
    BackfillCommittedOut,
    BackfillIn,
    BatchIn,
    BatchOut,
    BoxPathOut,
    ReservationIn,
    ReservationOut,
)
from .service import (
    AmbiguousOrder,
    BatchError,
    NoValidOrder,
    ReplayConflict,
    ReservationNotFound,
    ReservationStateConflict,
    ReservationUnavailable,
    RevisionConflict,
    SeqReserved,
    backfill_reservation,
    create_reservation,
    get_box_path,
    get_reservation,
    list_reservations,
    release_reservation,
    submit_batch,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="药品追溯装箱扫描 API",
    version="1.1.0",
    description=(
        "单条产线扫描提交与包装层级查询。每批至多 100 笔；非空父码表示转入并替换旧归属，"
        "空值表示拆出。冲突批次整体拒绝，原扫描与层级不变。断网补录：通过 /docs 预留 "
        "2~16 个连续空号，提交两份设备日志求解唯一合法顺序后整批写入。"
    ),
    lifespan=lifespan,
    docs_url="/swagger",  # /docs 路径让位给号段预留接口
)


@app.exception_handler(BatchError)
def _handle_batch_error(_: Request, exc: BatchError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": exc.code, "message": exc.message, **exc.extra},
    )


@app.exception_handler(RevisionConflict)
def _handle_revision_conflict(_: Request, exc: RevisionConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "revision_conflict",
            "message": exc.reason,
            "scan_no": exc.scan_no,
            "conflict_seq": exc.seq,
        },
    )


@app.exception_handler(ReplayConflict)
def _handle_replay_conflict(_: Request, exc: ReplayConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "replay_conflict",
            "message": exc.reason,
            "conflict_seq": exc.conflict_seq,
        },
    )


@app.exception_handler(SeqReserved)
def _handle_seq_reserved(_: Request, exc: SeqReserved) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "seq_reserved",
            "message": (
                f"序号 {exc.seq} 处于有效预留 {exc.reservation_id} 的号段 "
                f"[{exc.start}, {exc.start + exc.length})，整批拒绝"
            ),
            "conflict_seq": exc.seq,
            "reservation_id": exc.reservation_id,
        },
    )


@app.exception_handler(ReservationNotFound)
def _handle_reservation_not_found(_: Request, exc: ReservationNotFound) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "reservation_not_found",
            "message": str(exc),
            "reservation_id": exc.reservation_id,
        },
    )


@app.exception_handler(ReservationUnavailable)
def _handle_reservation_unavailable(
    _: Request, exc: ReservationUnavailable
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": "range_unavailable", "message": str(exc)},
    )


@app.exception_handler(ReservationStateConflict)
def _handle_reservation_state_conflict(
    _: Request, exc: ReservationStateConflict
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "reservation_conflict",
            "message": exc.reason,
            "reservation_id": exc.reservation_id,
        },
    )


@app.exception_handler(NoValidOrder)
def _handle_no_valid_order(_: Request, exc: NoValidOrder) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "no_valid_order",
            "message": "两份日志的所有交错顺序均违反包装规则，无合法顺序；预留未核销",
            "reservation_id": exc.reservation_id,
        },
    )


@app.exception_handler(AmbiguousOrder)
def _handle_ambiguous_order(_: Request, exc: AmbiguousOrder) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "ambiguous_order",
            "message": "存在多种合法顺序，两者都不入库；预留未核销",
            "reservation_id": exc.reservation_id,
            "orders": exc.orders,
        },
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/scans/batch", response_model=BatchOut)
def submit(batch: BatchIn) -> dict:
    """提交一批扫描（≤100 笔），全部成功才生效，否则整批拒绝。

    新增或更正到有效预留号段内的序号时整批拒绝（409 seq_reserved）。
    """

    return submit_batch(batch)


@app.get("/boxes/{box:path}", response_model=BoxPathOut)
def box_path(box: str) -> dict:
    """按盒码查询完整当前包装路径（自顶向底）。"""

    result = get_box_path(box)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "box_not_found",
                "message": f"盒码 {box} 不存在或当前已拆出，无包装归属",
                "box": box,
            },
        )
    return result


# --------------------------------------------------------------------------- #
# 断网补录：号段预留与双设备日志求解
# --------------------------------------------------------------------------- #
@app.post("/docs", response_model=ReservationOut, status_code=201)
def reserve(payload: ReservationIn) -> dict:
    """按起始序号和长度预留 2~16 个连续空号，返回预留号。

    号段须避开已入库序号和其他有效预留，否则 409。
    """

    return create_reservation(payload)


@app.get("/docs", response_model=List[ReservationOut])
def list_all() -> list:
    """列出全部号段预留（含已释放/已核销）。"""

    return list_reservations()


@app.get("/docs/{reservation_id}", response_model=ReservationOut)
def get_one(reservation_id: str) -> dict:
    """按预留号查询号段状态。"""

    return get_reservation(reservation_id)


@app.post("/docs/{reservation_id}/release", response_model=ReservationOut)
def release(reservation_id: str) -> dict:
    """释放未核销的预留；释放后普通扫描可使用这些序号。"""

    return release_reservation(reservation_id)


@app.post("/docs/{reservation_id}/backfill", response_model=BackfillCommittedOut)
def backfill(reservation_id: str, payload: BackfillIn) -> dict:
    """断网补录：提交两份设备日志（各 1~8 笔，总笔数填满号段）。

    仅一种合法顺序时整批写入修订 1 并核销预留，返回各扫描号分配到的
    全线序号；无解报告无解；多解返回两种合法顺序且均不入库。
    """

    return backfill_reservation(reservation_id, payload)
