"""请求/响应模型。

编码约定：
* ``B:`` 开头 —— 盒（最小包装，只能作为子项）
* ``C:`` 开头 —— 箱（可装入另一箱或托盘）
* ``P:`` 开头 —— 托盘（只能作为顶层父项，不能作为子项）
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

CODE_PATTERN = r"^[BCP]:\S+$"
MAX_BATCH_SIZE = 100
MIN_RESERVATION_LENGTH = 2
MAX_RESERVATION_LENGTH = 16
MAX_DEVICE_LOG_SIZE = 8


class ScanIn(BaseModel):
    """单条装箱扫描记录。"""

    scan_no: str = Field(..., min_length=1, description="扫描号，全局唯一")
    seq: int = Field(..., ge=1, description="全产线递增序号，有效扫描不得重复")
    revision: int = Field(..., ge=1, description="递增修订号，同扫描号只能增大")
    child: str = Field(..., pattern=CODE_PATTERN, description="子码（B: 盒 / C: 箱 / P: 托盘）")
    parent: Optional[str] = Field(
        ...,
        pattern=CODE_PATTERN,
        description="目标父码：非空=转入该包装并替换旧归属；null=拆出",
    )


class BatchIn(BaseModel):
    """一批扫描提交，至多 100 笔。"""

    scans: List[ScanIn] = Field(..., max_length=MAX_BATCH_SIZE)


class ChangeOut(BaseModel):
    """单个盒码的托盘归属变化。"""

    box: str
    old_pallet: Optional[str]
    new_pallet: Optional[str]
    old_path: List[str] = Field(..., description="变更前完整包装路径，自顶（托盘）向底（盒）")
    new_path: List[str] = Field(..., description="变更后完整包装路径，自顶（托盘）向底（盒）")


class BatchOut(BaseModel):
    accepted: bool
    replayed_scans: int = Field(..., description="重放后当前生效的扫描总数")
    changes: List[ChangeOut]


class BoxPathOut(BaseModel):
    box: str
    parent: Optional[str]
    pallet: Optional[str]
    path: List[str] = Field(..., description="完整当前包装路径，自顶（托盘）向底（盒）")


# --------------------------------------------------------------------------- #
# 断网补录：号段预留与双设备日志求解
# --------------------------------------------------------------------------- #
class ReservationIn(BaseModel):
    """按起始序号与长度预留一段连续空号。"""

    start_seq: int = Field(..., ge=1, description="号段起始全线序号（含）")
    length: int = Field(
        ...,
        ge=MIN_RESERVATION_LENGTH,
        le=MAX_RESERVATION_LENGTH,
        description="号段长度：2~16 个连续序号",
    )


class ReservationOut(BaseModel):
    reservation_id: str = Field(..., description="预留号")
    start_seq: int
    length: int
    status: str = Field(
        ..., description="active 有效 / released 已释放 / consumed 已核销"
    )
    created_at: str
    updated_at: str


class LogEntryIn(BaseModel):
    """设备离线日志中的一笔装箱扫描（不含全线序号与修订号）。"""

    scan_no: str = Field(..., min_length=1, description="新扫描号：批内唯一且库中未使用")
    child: str = Field(..., pattern=CODE_PATTERN, description="子码（B: 盒 / C: 箱 / P: 托盘）")
    parent: Optional[str] = Field(
        ...,
        pattern=CODE_PATTERN,
        description="目标父码：非空=转入该包装并替换旧归属；null=拆出",
    )


class DeviceLogIn(BaseModel):
    """单台扫码器的离线日志：1~8 笔，列表顺序即设备本地保存顺序。"""

    device: str = Field(..., min_length=1, description="扫码器标识")
    entries: List[LogEntryIn] = Field(..., min_length=1, max_length=MAX_DEVICE_LOG_SIZE)


class BackfillIn(BaseModel):
    """断网补录请求：恰好两份设备日志，总笔数须恰好填满预留号段。"""

    logs: List[DeviceLogIn] = Field(..., min_length=2, max_length=2)


class AssignmentOut(BaseModel):
    """补录入库后单笔扫描分到的全线序号。"""

    scan_no: str
    seq: int


class BackfillCommittedOut(BaseModel):
    """唯一合法顺序时的补录提交结果。"""

    status: str = Field(..., description="固定为 committed")
    reservation_id: str
    assignments: List[AssignmentOut] = Field(..., description="按全线序号升序")
    replayed_scans: int = Field(..., description="重放后当前生效的扫描总数")
    changes: List[ChangeOut]
