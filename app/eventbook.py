"""交接班事件簿的领域编译器：确定性状态机 + 暂停区间派生。

港方交接班只留下**按时发生的作业事件**，结算员无需手工整理暂停区间：
事件簿依次包含开工、暂停、复工和完工四类事件，编译器以确定性状态机校验
首尾、配对与严格递增时间，再把每一对有效的「暂停 → 复工」转换为现有
左闭右开区间 ``[pause_at, resume_at)``，交给原计费规则（``intervals``）处理。

状态机（状态迁移完全确定，与时间无关，仅时间严格递增单独校验）::

    未开工 --开工--> 作业中 --暂停--> 暂停中 --复工--> 作业中 --完工--> 已完工

非法转换包括：缺少开工或完工、连续暂停、未暂停即复工、暂停后直接完工，
以及相邻事件时间未严格递增（时间倒退/相等）。所有错误都定位到事件下标。
"""

from dataclasses import dataclass, field
from datetime import datetime

# 事件类型：开工 / 暂停 / 复工 / 完工。
EVENT_START_WORK = "start_work"
EVENT_PAUSE = "pause"
EVENT_RESUME = "resume"
EVENT_FINISH_WORK = "finish_work"

EVENT_TYPES = (
    EVENT_START_WORK,
    EVENT_PAUSE,
    EVENT_RESUME,
    EVENT_FINISH_WORK,
)

# 错误信息中的中文事件名。
EVENT_LABELS = {
    EVENT_START_WORK: "开工",
    EVENT_PAUSE: "暂停",
    EVENT_RESUME: "复工",
    EVENT_FINISH_WORK: "完工",
}

# 状态机状态。
STATE_NOT_STARTED = "not_started"
STATE_WORKING = "working"
STATE_PAUSED = "paused"
STATE_FINISHED = "finished"


class EventCompileError(ValueError):
    """事件簿不合法；``index`` 定位到触发错误的事件下标。"""

    def __init__(self, index: int, message: str) -> None:
        self.index = index
        super().__init__(f"events[{index}] {message}")


@dataclass(frozen=True)
class CompiledEventBook:
    """编译结果：作业边界、派生暂停区间与编译摘要要素。"""

    work_start: datetime
    work_end: datetime
    # 每对有效「暂停 → 复工」形成的左闭右开区间，按下标顺序、互不重叠
    pauses: list[tuple[datetime, datetime]]
    event_count: int
    transitions: list[str] = field(default_factory=list)

    @property
    def valid_pause_count(self) -> int:
        return len(self.pauses)

    def summary(self) -> dict:
        """编译摘要：首尾事件、事件数、有效暂停数与完整状态迁移序列。"""
        return {
            "event_count": self.event_count,
            "valid_pause_count": self.valid_pause_count,
            "transitions": list(self.transitions),
            "work_start_event": EVENT_START_WORK,
            "work_end_event": EVENT_FINISH_WORK,
        }


def compile_event_book(
    events: list[tuple[str, datetime]],
) -> CompiledEventBook:
    """把原始事件序列编译为作业边界与派生暂停区间。

    依次校验：事件簿非空、首项为开工、末项为完工、相邻事件时间严格递增、
    每次状态迁移合法（暂停/复工配对，完工时不得仍在暂停）。任一不符抛出
    :class:`EventCompileError`，错误下标即事件簿中的事件位置。
    """
    if not events:
        raise EventCompileError(0, "事件簿不能为空，至少包含开工与完工")

    first_kind = events[0][0]
    if first_kind != EVENT_START_WORK:
        raise EventCompileError(
            0, f"事件簿必须以开工开始，首项却是{EVENT_LABELS[first_kind]}"
        )
    last_kind = events[-1][0]
    if last_kind != EVENT_FINISH_WORK:
        raise EventCompileError(
            len(events) - 1,
            f"事件簿必须以完工结束，末项却是{EVENT_LABELS[last_kind]}",
        )

    state = STATE_NOT_STARTED
    work_start: datetime | None = None
    work_end: datetime | None = None
    pause_started_at: datetime | None = None
    pauses: list[tuple[datetime, datetime]] = []
    transitions: list[str] = []
    previous_at: datetime | None = None

    for index, (kind, at) in enumerate(events):
        # 严格递增：与上一事件相等也算时间倒退。
        if previous_at is not None and at <= previous_at:
            raise EventCompileError(
                index,
                f"事件时间必须严格递增：{at.isoformat()} 不晚于上一事件"
                f" {previous_at.isoformat()}",
            )

        if kind == EVENT_START_WORK:
            if state != STATE_NOT_STARTED:
                raise EventCompileError(
                    index, "开工只能出现一次且必须位于事件簿首位"
                )
            state = STATE_WORKING
            work_start = at
        elif kind == EVENT_PAUSE:
            if state == STATE_PAUSED:
                raise EventCompileError(index, "连续暂停：暂停前必须先复工")
            if state != STATE_WORKING:
                raise EventCompileError(
                    index,
                    f"当前状态不允许暂停（{EVENT_LABELS[kind]}事件）",
                )
            state = STATE_PAUSED
            pause_started_at = at
        elif kind == EVENT_RESUME:
            if state != STATE_PAUSED:
                raise EventCompileError(index, "未暂停即复工：复工前必须先暂停")
            # 暂停/复工配对成功：派生左闭右开 [暂停时刻, 复工时刻)。
            pauses.append((pause_started_at, at))
            pause_started_at = None
            state = STATE_WORKING
        elif kind == EVENT_FINISH_WORK:
            if state == STATE_PAUSED:
                raise EventCompileError(index, "暂停后直接完工：完工前必须先复工")
            if state != STATE_WORKING:
                raise EventCompileError(
                    index,
                    f"当前状态不允许完工（{EVENT_LABELS[kind]}事件）",
                )
            state = STATE_FINISHED
            work_end = at
        else:  # 理论上 schema 已拒绝，保留防御性分支
            raise EventCompileError(index, f"未知事件类型：{kind!r}")

        transitions.append(kind)
        previous_at = at

    return CompiledEventBook(
        work_start=work_start,
        work_end=work_end,
        pauses=pauses,
        event_count=len(events),
        transitions=transitions,
    )
