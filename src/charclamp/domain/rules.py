"""炭窑焖烧志业务规则。"""

from __future__ import annotations

from charclamp.domain.models import BurnShift, Clamp

MIN_PEAK_TEMP_FOR_DRAWN = 400.0


class RuleError(ValueError):
    """业务规则校验失败。"""


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    return max(clamp.shifts, key=lambda s: s.started_at)


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


def assert_clamp_accepts_shift(clamp: Clamp, expected_status: str | None) -> None:
    """
    登记班次的窑态门闩（火色只认窑态字段）：
    - 已出炭 (drawn) 的窑封窑，禁止再写入班次；写班次也绝不把窑改成 drawn。
    - 表单打开时看到的窑态快照 expected_status 必须与当前窑态一致，
      否则说明期间有人抢先登记（第一班夺权），本次必须放弃，不得插卡。
    """
    if clamp.status == Clamp.STATUS_DRAWN:
        raise RuleError(f"窑 {clamp.code} 已出炭封窑，不能再登记班次")
    if expected_status is not None and expected_status != clamp.status:
        raise RuleError(
            f"窑 {clamp.code} 状态已由「{expected_status}」变为"
            f"「{clamp.status}」，请刷新后按最新窑态重填"
        )
