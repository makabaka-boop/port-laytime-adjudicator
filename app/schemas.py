"""请求/响应 Pydantic 模型。

字段级校验器保证每个错误都能定位到具体 JSON 路径（body -> 字段 -> 下标）。
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.timeparse import parse_utc_second

# 严格整数：拒绝 1.0、"100"、true 等隐式转换。
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntervalIn(StrictModel):
    start: str = Field(..., description="RFC 3339 UTC 整秒时间")
    end: str = Field(..., description="RFC 3339 UTC 整秒时间，且必须晚于 start")

    @field_validator("start")
    @classmethod
    def _parse_start(cls, value: str) -> str:
        parse_utc_second(value)  # 仅校验，保留原始字符串
        return value

    @field_validator("end")
    @classmethod
    def _parse_end_and_order(cls, value: str, info) -> str:
        end_dt = parse_utc_second(value)
        start_raw = info.data.get("start")
        if start_raw is not None and end_dt <= parse_utc_second(start_raw):
            raise ValueError("结束时间必须晚于开始时间")
        return value


class PauseIn(IntervalIn):
    pass


class DemurrageCreate(StrictModel):
    work_start: str = Field(..., description="作业开始（含），RFC 3339 UTC 整秒")
    work_end: str = Field(..., description="作业结束（不含），必须晚于 work_start")
    rate_cents_per_hour: NonNegativeInt = Field(..., description="费率，非负整数，分/小时")
    pauses: list[PauseIn] = Field(default_factory=list, description="暂停区间，左闭右开")
    allowed_seconds: NonNegativeInt = Field(
        default=0,
        description=(
            "租约约定的免计滞期允许作业秒数，非负严格整数；"
            "省略按 0 处理。先合并暂停得到净作业秒数，再扣减不超过净作业时长的部分。"
        ),
    )

    @field_validator("work_start")
    @classmethod
    def _parse_work_start(cls, value: str) -> str:
        parse_utc_second(value)
        return value

    @field_validator("work_end")
    @classmethod
    def _parse_work_end_and_order(cls, value: str, info) -> str:
        end_dt = parse_utc_second(value)
        start_raw = info.data.get("work_start")
        if start_raw is not None and end_dt <= parse_utc_second(start_raw):
            raise ValueError("作业结束时间必须晚于作业开始时间")
        return value


class IntervalOut(BaseModel):
    start: str
    end: str


class DemurrageResult(BaseModel):
    id: str
    work_start: str
    work_end: str
    pauses: list[IntervalOut]
    pauses_merged: list[IntervalOut]
    rate_cents_per_hour: int
    work_seconds: int
    paused_seconds: int
    allowed_seconds: int
    allowed_seconds_used: int
    billable_seconds: int
    billable_hours: int
    total_cents: int
    created_at: str


class DemurrageCompareRequest(StrictModel):
    base_id: str = Field(..., description="基准结果标识（差异与增减的参照方）")
    candidate_id: str = Field(..., description="候选结果标识（与基准对比的一方）")


class ComparisonChanges(BaseModel):
    """逐项是否变化：作业起止、原始/合并暂停、费率、允许秒数。"""

    work_start: bool
    work_end: bool
    pauses: bool
    pauses_merged: bool
    rate_cents_per_hour: bool
    allowed_seconds: bool


class ComparisonDeltas(BaseModel):
    """候选相对基准的有符号增减值（候选 − 基准）。"""

    paused_seconds: int
    billable_seconds: int
    billable_hours: int
    total_cents: int


class DemurrageComparison(BaseModel):
    base_id: str
    candidate_id: str
    changes: ComparisonChanges
    deltas: ComparisonDeltas


class VoyageCapCreate(StrictModel):
    """航次封顶清单创建请求：按提交顺序引用 2 至 20 个既有结算结果。"""

    result_ids: list[str] = Field(
        ...,
        min_length=2,
        max_length=20,
        description="既有结算结果标识，按提交顺序，2 至 20 个，不得重复",
    )
    cap_cents: NonNegativeInt = Field(
        ..., description="赔付上限，非负严格整数，单位分"
    )


class VoyageCapItemOut(BaseModel):
    """清单明细：提交顺序下标、结果标识、原费用与分配费用快照。"""

    position: int
    result_id: str
    original_cents: int
    allocated_cents: int


class VoyageCapListResult(BaseModel):
    id: str
    cap_cents: int
    original_total_cents: int
    allocated_total_cents: int
    capped: bool
    items: list[VoyageCapItemOut]
    created_at: str
