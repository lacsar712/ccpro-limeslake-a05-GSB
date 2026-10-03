"""炭窑焖烧志领域服务：一次业务动作在单个事务内完成，UI 三处只从事务后的库状态重算。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from charclamp.domain.models import BurnShift, Clamp
from charclamp.domain.rules import RuleError, assert_clamp_accepts_shift


class ShiftRejected(RuleError):
    """班次被门闩拒绝（窑不存在 / 已封窑 / 第一班已被他人登记），无任何写入。"""


async def register_burn_shift(
    db: AsyncSession,
    *,
    clamp_id: int,
    started_at: datetime,
    peak_temp_c: float | None,
    charcoal_grade: str,
    notes: str,
    expected_status: str | None,
) -> BurnShift:
    """
    登记一个焖烧班次。单一事实源是事务提交后的数据库：

    1. SELECT ... FOR UPDATE 锁住该窑行——两人同时给同一已码窑登记第一班时，
       后到者在锁上排队，拿到的是先行者提交后的 burning 态；
    2. 窑态门闩：drawn 封窑拒登；表单快照 expected_status 与现状不符则拒登；
    3. stacked -> burning 的推进本身也是条件更新（CAS），
       rowcount != 0 才算拿到「第一班」资格；后到者 expected=stacked 与现状
       burning 不符，直接拒登，绝不插第二张卡。

    任何拒绝都在 add(shift) 之前抛出，因此「非法班禁止先插卡」。
    """
    clamp = (
        await db.execute(
            select(Clamp).where(Clamp.id == clamp_id).with_for_update()
        )
    ).scalar_one_or_none()
    if clamp is None:
        raise ShiftRejected("所选炭窑不存在，班次未登记")

    assert_clamp_accepts_shift(clamp, expected_status)

    if clamp.status == Clamp.STATUS_STACKED:
        promoted = await db.execute(
            update(Clamp)
            .where(Clamp.id == clamp_id, Clamp.status == Clamp.STATUS_STACKED)
            .values(status=Clamp.STATUS_BURNING)
        )
        if promoted.rowcount != 1:
            # 并发兜底：锁内快照与条件更新之间不可能被插，除非有人绕过门闩。
            raise ShiftRejected("该窑第一班刚被他人登记，本次未写入，请刷新查看")
        await db.refresh(clamp, ["status"])

    shift = BurnShift(
        clamp_id=clamp_id,
        started_at=started_at,
        peak_temp_c=peak_temp_c,
        charcoal_grade=charcoal_grade,
        notes=notes,
    )
    db.add(shift)
    return shift
