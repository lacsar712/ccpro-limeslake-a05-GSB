from __future__ import annotations

from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, User, utcnow
from charclamp.domain.rules import (
    RuleError,
    assert_can_set_clamp_status,
    can_mark_clamp_drawn,
    validate_shift_input,
)
from charclamp.infra.db import SessionLocal, repeatable_read
from charclamp.infra.security import verify_password

STATUS_LABELS = {
    Clamp.STATUS_STACKED: "已码窑",
    Clamp.STATUS_BURNING: "焖烧中",
    Clamp.STATUS_DRAWN: "已出炭",
}


def _set_flash(request: Request, message: str, category: str = "ok") -> None:
    data = dict(request.session or {})
    data["flash"] = message
    data["flash_cat"] = category
    request.set_session(data)


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    data = dict(request.session or {})
    message = data.pop("flash", None)
    category = data.pop("flash_cat", None)
    if message is not None or category is not None:
        request.set_session(data)
    return message, category


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _load_timeline_context(clamp_id: int | None = None) -> dict[str, Any]:
    # 窑剪影（火色）与时间轴卡片（窑态徽章/峰值）必须来自同一快照，
    # 与抽屉的「最新峰值 → 出炭提示」共用同一份读取口径，三处不得分叉。
    async with SessionLocal() as db, repeatable_read(db):
        clamps = list(
            (
                await db.execute(
                    select(Clamp)
                    .options(selectinload(Clamp.site), selectinload(Clamp.shifts))
                    .order_by(Clamp.code)
                )
            )
            .scalars()
            .all()
        )
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc(), BurnShift.id.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
    }


class AuthController(Controller):
    path = ""
    tags = ["auth"]

    @get("/login", media_type=MediaType.HTML)
    async def login_page(self, request: Request) -> Template:
        flash, flash_cat = _pop_flash(request)
        return Template(
            template_name="login.html",
            context={"flash": flash, "flash_cat": flash_cat},
        )

    @post("/login")
    async def login(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user or not verify_password(password, user.password_hash):
                request.set_session({"flash": "用户名或密码错误", "flash_cat": "error"})
                return Redirect("/login")
            request.set_session({"user_id": user.id})
        return Redirect("/")

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.clear_session()
        return Redirect("/login")


class TimelineController(Controller):
    path = ""
    tags = ["timeline"]

    @get("/", media_type=MediaType.HTML)
    async def timeline(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="timeline.html",
            context={
                **ctx,
                "user": request.user,
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @get("/timeline/partial", media_type=MediaType.HTML)
    async def timeline_partial(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="partials/board.html",
            context={
                **ctx,
                "user": request.user,
            },
        )

    @get("/drawer/shift-new", media_type=MediaType.HTML)
    async def drawer_shift_new(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        async with SessionLocal() as db:
            clamps = list((await db.execute(select(Clamp).order_by(Clamp.code))).scalars().all())
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "user": request.user,
            },
        )

    @get("/drawer/clamp/{clamp_id:int}", media_type=MediaType.HTML)
    async def drawer_clamp(self, request: Request, clamp_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        async with SessionLocal() as db, repeatable_read(db):
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts), selectinload(Clamp.site))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "user": request.user,
            },
        )


class ShiftController(Controller):
    path = "/shifts"
    tags = ["shifts"]

    @post("/new")
    async def create_shift(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")

        # —— 第一步：纯字段校验，不打开/触碰数据库。非法班禁止先插卡。 ——
        try:
            draft = validate_shift_input(
                clamp_id_raw=data.get("clamp_id"),
                started_at_raw=data.get("started_at"),
                peak_temp_c_raw=data.get("peak_temp_c"),
                charcoal_grade_raw=data.get("charcoal_grade"),
                notes_raw=data.get("notes"),
                now=utcnow(),
            )
        except RuleError as exc:
            _set_flash(request, str(exc), "error")
            return Redirect("/")

        # —— 第二步：单事务内完成「窑态翻转 + 插卡」，同成同败。 ——
        async with SessionLocal() as db:
            try:
                async with db.begin():
                    current = (
                        await db.execute(
                            select(Clamp.status).where(Clamp.id == draft.clamp_id)
                        )
                    ).one_or_none()
                    if current is None:
                        raise RuleError("所选炭窑不存在，班次未登记")
                    current_status = current[0]
                    if current_status == Clamp.STATUS_DRAWN:
                        # 已出炭的窑不得再登记焖烧班次；火色只认窑态字段，
                        # 写班次这条路永远只会写 burning，绝不会偷偷改成 drawn。
                        raise RuleError("该窑已出炭，不能再登记焖烧班次")

                    if current_status == Clamp.STATUS_STACKED:
                        # 第一班并发仲裁：条件 UPDATE 是唯一裁决者，且先于任何插卡。
                        # 只有当前仍是 stacked 的窑会被翻成 burning（进焖烧火色）。
                        # 两个并发首班请求中，UPDATE 行锁串行化：抢中的 rowcount=1，
                        # 落后者语句重读到 burning 后 WHERE 失配 → rowcount=0，
                        # 此时还没插任何卡，直接整体回滚，只许一笔入库。
                        flipped = await db.execute(
                            update(Clamp)
                            .where(Clamp.id == draft.clamp_id, Clamp.status == Clamp.STATUS_STACKED)
                            .values(status=Clamp.STATUS_BURNING)
                        )
                        if flipped.rowcount == 0:
                            raise RuleError("该窑的第一班已被他人登记，本班未写入，请刷新后重试")

                    db.add(
                        BurnShift(
                            clamp_id=draft.clamp_id,
                            started_at=draft.started_at,
                            peak_temp_c=draft.peak_temp_c,
                            charcoal_grade=draft.charcoal_grade,
                            notes=draft.notes,
                        )
                    )
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
                return Redirect("/")

        _set_flash(request, "焖烧班次已登记", "ok")
        return Redirect(f"/?clamp_id={draft.clamp_id}")


class ClampController(Controller):
    path = "/clamps"
    tags = ["clamps"]

    @post("/{clamp_id:int}/status")
    async def set_status(
        self,
        request: Request,
        clamp_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        new_status = (data.get("status") or "").strip()
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            try:
                assert_can_set_clamp_status(clamp, new_status)
                clamp.status = new_status
                await db.commit()
                _set_flash(request, f"窑 {clamp.code} 状态已更新", "ok")
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")
