"""电位复测专页：与状态改动脱钩的记账表。

- 只有还原中缸允许新建/更新复测；闲置与可染色拒绝。
- 同缸复测序号唯一（先业务校验，再靠 uniq_retest_seq_per_vat 兜住并发双交）。
- 电位必填且不得高于 -480 mV；新建与更新共用同一校验。
- 任意被拒都重新渲染本页（不跳转空白、不留半截写入）。
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
import json

from app.auth import get_current_user
from app.db import get_db
from app.models import RedoxRetest, Vat, Workshop
from app.services.vat_rules import (
    VatRuleError,
    assert_vat_accepts_retest,
    validate_retest_redox,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _vat_brief(vat: Vat) -> dict:
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "retestCount": len(vat.retests),
    }


def _retest_row(r: RedoxRetest) -> dict:
    return {
        "id": r.id,
        "seq": r.seq,
        "redoxMv": float(r.redoxMv),
        "sampledAt": r.sampledAt.strftime("%Y-%m-%dT%H:%M"),
        "sampledAtLabel": r.sampledAt.strftime("%Y-%m-%d %H:%M"),
        "operator": r.operator,
    }


def _page_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
    form: Optional[dict] = None,
):
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.retests))
        .order_by(Vat.code)
        .all()
    )
    selected = None
    if selected_vat:
        vat = db.get(Vat, selected_vat)
        if vat:
            retests = (
                db.query(RedoxRetest)
                .filter(RedoxRetest.vat_id == vat.id)
                .order_by(RedoxRetest.seq)
                .all()
            )
            selected = {
                **_vat_brief(vat),
                "retests": [_retest_row(r) for r in retests],
                "nextSeq": (retests[-1].seq + 1) if retests else 1,
            }
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_brief(v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "selected": selected,
        "error": error,
        "form": form or {},
        "status_labels": STATUS_LABELS,
        "active": "retests",
    }


def _parse_int(raw: str, label: str) -> int:
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        raise VatRuleError(f"{label}必须是整数。")


def _parse_dt(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        raise VatRuleError("采样时刻格式无效。")


@router.get("/retests", response_class=HTMLResponse)
async def retest_page(
    request: Request,
    vat: Optional[int] = None,
    workshop: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(
        request,
        "retests.html",
        _page_context(request, db, user, workshop, vat),
    )


@router.post("/retests/vats/{pk}/create", response_class=HTMLResponse)
async def retest_create(
    pk: int,
    request: Request,
    seq: str = Form(""),
    redoxMv: str = Form(""),
    sampledAt: str = Form(""),
    operator: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ws = int(workshop) if workshop.strip() else None
    vat = (
        db.query(Vat)
        .options(joinedload(Vat.retests))
        .filter(Vat.id == pk)
        .first()
    )
    if not vat:
        return RedirectResponse("/retests", status_code=303)
    form = {
        "seq": seq, "redoxMv": redoxMv, "sampledAt": sampledAt, "operator": operator,
    }
    try:
        assert_vat_accepts_retest(vat)
        seq_no = _parse_int(seq, "复测序号")
        redox = _parse_redox(redoxMv)
        sampled = _parse_dt(sampledAt)
        operator_name = operator.strip()
        if not operator_name:
            raise VatRuleError("复测人必须填写。")
        if any(r.seq == seq_no for r in vat.retests):
            raise VatRuleError(f"本缸已存在复测序号 {seq_no}，同缸序号须唯一。")
        db.add(
            RedoxRetest(
                vat_id=vat.id,
                seq=seq_no,
                redoxMv=redox,
                sampledAt=sampled,
                operator=operator_name,
            )
        )
        # flush 让唯一约束在同事务内拦下并发的同序号双交；失败则整笔回滚。
        db.flush()
        db.commit()
        return RedirectResponse(
            f"/retests?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303
        )
    except (VatRuleError, InvalidOperation) as exc:
        db.rollback()
        error = exc.message if isinstance(exc, VatRuleError) else f"复测电位数值无效：{exc}"
    except IntegrityError:
        db.rollback()
        error = "本缸该复测序号已存在（同序号连交只入库一笔），请改用新序号。"
    return render(
        request,
        "retests.html",
        _page_context(request, db, user, ws, pk, error, form),
        status_code=400,
    )


@router.post("/retests/{retest_id}/update", response_class=HTMLResponse)
async def retest_update(
    retest_id: int,
    request: Request,
    seq: str = Form(""),
    redoxMv: str = Form(""),
    sampledAt: str = Form(""),
    operator: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ws = int(workshop) if workshop.strip() else None
    row = (
        db.query(RedoxRetest)
        .options(joinedload(RedoxRetest.vat).joinedload(Vat.retests))
        .filter(RedoxRetest.id == retest_id)
        .first()
    )
    if not row:
        return RedirectResponse("/retests", status_code=303)
    pk = row.vat_id
    form = {
        "seq": seq, "redoxMv": redoxMv, "sampledAt": sampledAt, "operator": operator,
        "editId": retest_id,
    }
    try:
        # 更新与新建共用同一道缸态/序号/电位校验。
        assert_vat_accepts_retest(row.vat)
        seq_no = _parse_int(seq, "复测序号")
        redox = _parse_redox(redoxMv)
        sampled = _parse_dt(sampledAt)
        operator_name = operator.strip()
        if not operator_name:
            raise VatRuleError("复测人必须填写。")
        clash = any(r.seq == seq_no and r.id != row.id for r in row.vat.retests)
        if clash:
            raise VatRuleError(f"本缸已存在复测序号 {seq_no}，同缸序号须唯一。")
        row.seq = seq_no
        row.redoxMv = redox
        row.sampledAt = sampled
        row.operator = operator_name
        db.flush()
        db.commit()
        return RedirectResponse(
            f"/retests?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303
        )
    except (VatRuleError, InvalidOperation) as exc:
        db.rollback()
        error = exc.message if isinstance(exc, VatRuleError) else f"复测电位数值无效：{exc}"
    except IntegrityError:
        db.rollback()
        error = "本缸该复测序号已存在，序号保持同缸唯一。"
    return render(
        request,
        "retests.html",
        _page_context(request, db, user, ws, pk, error, form),
        status_code=400,
    )


def _parse_redox(raw: str) -> Decimal:
    if raw is None or not raw.strip():
        return validate_retest_redox(None)
    return validate_retest_redox(Decimal(raw.strip()))
