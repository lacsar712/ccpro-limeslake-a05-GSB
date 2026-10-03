"""炭窑焖烧志业务规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from charclamp.domain.models import BurnShift, Clamp

MIN_PEAK_TEMP_FOR_DRAWN = 400.0


class RuleError(ValueError):
    """业务规则校验失败。"""


@dataclass(frozen=True)
class ShiftDraft:
    """一次班次登记经过校验后的不可变输入。

    started_at 统一为带时区时间；peak_temp_c 缺省为 None。
    """

    clamp_id: int
    started_at: datetime
    peak_temp_c: float | None
    charcoal_grade: str
    notes: str


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    # 与时间轴排序口径 (started_at desc, id desc) 保持一致：同秒班次以后插入者为最新，
    # 抽屉的「最新峰值 → 出炭提示」才不会与时间轴顶部那张卡分叉。
    return max(clamp.shifts, key=lambda s: (s.started_at, s.id))


def validate_shift_input(
    *,
    clamp_id_raw: object,
    started_at_raw: object,
    peak_temp_c_raw: object,
    charcoal_grade_raw: object,
    notes_raw: object,
    now: datetime,
) -> ShiftDraft:
    """
    校验班次登记表单。任何一项非法都抛 RuleError —— 调用方必须在校验通过后
    才能写库（非法班禁止先插卡）。
    """
    try:
        clamp_id = int(str(clamp_id_raw).strip())
    except (AttributeError, TypeError, ValueError):
        raise RuleError("请选择要登记班次的炭窑")

    started_text = (started_at_raw or "").strip() if isinstance(started_at_raw, str) else started_at_raw
    if started_text:
        try:
            started_at = datetime.fromisoformat(str(started_text))
        except ValueError:
            raise RuleError("开始时间格式无效")
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
    else:
        started_at = now

    peak_text = peak_temp_c_raw.strip() if isinstance(peak_temp_c_raw, str) else peak_temp_c_raw
    peak: float | None
    if peak_text in (None, ""):
        peak = None
    else:
        try:
            peak = float(peak_text)
        except (TypeError, ValueError):
            raise RuleError("峰值温度必须是数字")
        if peak < 0 or peak > 2000:
            raise RuleError("峰值温度超出合理范围（0–2000℃）")

    grade = (charcoal_grade_raw or "B").strip() if isinstance(charcoal_grade_raw, str) else "B"
    if not grade or len(grade) > 40:
        raise RuleError("炭品等级无效")

    notes = (notes_raw or "").strip() if isinstance(notes_raw, str) else ""
    return ShiftDraft(
        clamp_id=clamp_id,
        started_at=started_at,
        peak_temp_c=peak,
        charcoal_grade=grade,
        notes=notes,
    )


def can_mark_clamp_drawn(clamp: Clamp) -> tuple[bool, str]:
    """
    炭窑转为「已出炭」(drawn) 的前提：
    最近一条焖烧班次的峰值温度已记录，且 >= 400℃。
    """
    latest = latest_shift_for_clamp(clamp)
    if latest is None:
        return False, "该窑尚无焖烧班次，不能标记为已出炭"
    if latest.peak_temp_c is None:
        return False, "最近班次尚未记录峰值温度，不能标记为已出炭"
    if latest.peak_temp_c < MIN_PEAK_TEMP_FOR_DRAWN:
        return (
            False,
            f"最近班次峰值温度 {latest.peak_temp_c}℃ 低于 {MIN_PEAK_TEMP_FOR_DRAWN:.0f}℃，不能标记为已出炭",
        )
    return True, ""


def assert_can_set_clamp_status(clamp: Clamp, new_status: str) -> None:
    allowed = {Clamp.STATUS_STACKED, Clamp.STATUS_BURNING, Clamp.STATUS_DRAWN}
    if new_status not in allowed:
        raise RuleError(f"无效状态：{new_status}")
    if new_status == Clamp.STATUS_DRAWN:
        ok, msg = can_mark_clamp_drawn(clamp)
        if not ok:
            raise RuleError(msg)
