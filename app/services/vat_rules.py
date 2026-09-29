"""染缸状态业务规则。"""

from decimal import Decimal
from typing import Optional

from app.models import DipLot, RedoxRetest, Vat

RETEST_MV_CEILING = Decimal("-480")
READY_LOT_MV_CEILING = Decimal("-500")
READY_RETEST_MEAN_CEILING = Decimal("-510")
READY_RETEST_MIN_RUN = 3


def _as_aware(dt):
    """表单提交的时间可能无时区，按 UTC 处理后再比较。"""
    if dt is not None and dt.tzinfo is None:
        from datetime import timezone

        return dt.replace(tzinfo=timezone.utc)
    return dt


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > READY_LOT_MV_CEILING:
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def validate_retest_fields(
    vat: Vat,
    seq: int,
    redox_mv: Decimal,
    retests: list[RedoxRetest],
    exclude_id: Optional[int] = None,
) -> None:
    """新建与更新复测共用：序号同缸唯一、电位必填且不得高于 -480 mV。"""
    if seq < 1:
        raise VatRuleError("复测序号须为不小于 1 的整数。")
    if redox_mv is None or redox_mv > RETEST_MV_CEILING:
        raise VatRuleError("复测电位必须填写，且数值不得高于 -480 mV。")
    clash = next(
        (
            r
            for r in retests
            if r.seq == seq and (exclude_id is None or r.id != exclude_id)
        ),
        None,
    )
    if clash is not None:
        raise VatRuleError(f"复测序号 {seq} 在本缸已存在，同缸序号须唯一。")


def evaluate_ready_retests(
    latest: Optional[DipLot], retests: list[RedoxRetest]
) -> tuple[list[RedoxRetest], Decimal]:
    """复测均值门槛：最新至少 3 条连续序号、算术平均 <= -510、
    且最后一次复测时间晚于该缸最近浸染。返回参与均值的复测与平均值。"""
    if latest is None:
        raise VatRuleError("无法设为可染色：该缸尚无浸染批次。")

    ordered = sorted(retests, key=lambda r: (r.seq, r.id))
    run: list[RedoxRetest] = []
    for r in ordered:
        if run and r.seq != run[-1].seq + 1:
            run = []
        run.append(r)

    missing: list[str] = []
    if len(run) < READY_RETEST_MIN_RUN:
        missing.append(
            f"至少 {READY_RETEST_MIN_RUN} 条连续序号的电位复测（当前连续 {len(run)} 条）"
        )

    mean = None
    if len(run) >= READY_RETEST_MIN_RUN:
        tail = run[-READY_RETEST_MIN_RUN:]
        mean = sum((Decimal(r.redoxMv) for r in tail), Decimal(0)) / Decimal(
            READY_RETEST_MIN_RUN
        )
        if mean > READY_RETEST_MEAN_CEILING:
            missing.append(
                f"最近三条复测电位算术平均不高于 -510 mV（当前为 {mean:.2f} mV）"
            )

    last_retest = run[-1].sampledAt if run else None
    if (
        last_retest is None
        or _as_aware(last_retest) <= _as_aware(latest.dippedAt)
    ):
        missing.append("最后一次复测的采样时刻须晚于该缸最近浸染时间")

    if missing:
        raise VatRuleError("无法设为可染色，复测门槛未满足：" + "；".join(missing) + "。")

    return run[-READY_RETEST_MIN_RUN:], mean.quantize(Decimal("0.01"))


def validate_vat_status_change(
    vat: Vat,
    new_status: str,
    latest: Optional[DipLot],
    retests: Optional[list[RedoxRetest]] = None,
) -> tuple[bool, Optional[Decimal]]:
    """浸染电位门槛与复测均值门槛在同一判定函数内完成。

    标记 ready 时：最新浸染电位须 <= -500，且复测均值门槛通过。
    复测通过时返回 (True, 平均值)，调用方负责把平均值回写到最新浸染电位；
    其余状态返回 (False, None)。任一不过即抛 VatRuleError，不留半截改写。
    """
    if new_status != Vat.STATUS_READY:
        return False, None

    assert_can_mark_ready(latest)
    _tail, mean = evaluate_ready_retests(latest, retests or [])
    return True, mean
