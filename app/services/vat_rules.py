"""染缸状态业务规则。

电位复测（RedoxRetest）是与状态改动脱钩的记账：还原中任意登记；
只有把缸位改判为「可染色」(ready) 的同一个判定函数里，才额外核算
复测均值门槛，通过后由调用方把均值回写到该缸最新浸染批次的 redoxMv。
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Sequence

from app.models import DipLot, RedoxRetest, Vat


# 原浸染电位门槛
READY_REDOX_MAX = Decimal("-500")
# 单条复测电位上限（必填，且不得高于该值）
RETEST_REDOX_MAX = Decimal("-480")
# 改可染色所需的连续复测条数与三条均值上限
RETEST_RUN_MIN = 3
READY_MEAN_MAX = Decimal("-510")


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _as_utc(value: datetime) -> datetime:
    """表单提交的本地时刻不带时区，统一按 UTC 比较，避免 aware/naive 报错。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def assert_vat_accepts_retest(vat: Vat) -> None:
    """只有还原中的缸允许登记复测；闲置与可染色一律拒绝。"""
    if vat.status != Vat.STATUS_REDUCING:
        label = {
            Vat.STATUS_IDLE: "闲置",
            Vat.STATUS_READY: "可染色",
        }.get(vat.status, vat.status)
        raise VatRuleError(f"当前缸位为「{label}」，只有还原中的缸允许登记电位复测。")


def validate_retest_redox(redox: Optional[Decimal]) -> Decimal:
    """新建与更新复测共用：电位必填且不得高于 -480 mV。"""
    if redox is None:
        raise VatRuleError("复测电位必须填写。")
    redox = Decimal(redox)
    if redox > RETEST_REDOX_MAX:
        raise VatRuleError(
            f"复测电位不得高于 {RETEST_REDOX_MAX} mV（当前 {redox} mV）。"
        )
    return redox


def latest_retest_run(retests: Sequence[RedoxRetest]) -> list[RedoxRetest]:
    """取序号最大的一段连续复测（按 seq 升序）。"""
    ordered = sorted(retests, key=lambda r: r.seq)
    if not ordered:
        return []
    run = [ordered[-1]]
    for item in reversed(ordered[:-1]):
        if run[0].seq == item.seq + 1:
            run.insert(0, item)
        else:
            break
    return run


def assert_retest_ready_gate(
    latest: Optional[DipLot], retests: Sequence[RedoxRetest]
) -> Decimal:
    """复测均值门槛。返回三条均值（供回写）；任一不满足即中文列出缺项。"""
    failures: list[str] = []
    if not retests:
        failures.append(
            f"尚未登记电位复测（至少需 {RETEST_RUN_MIN} 条连续序号、"
            f"三条均值 ≤ {READY_MEAN_MAX} mV 且最后复测晚于最近浸染）"
        )
        raise VatRuleError("无法设为可染色：" + "；".join(failures) + "。")

    run = latest_retest_run(retests)
    trio = run[-RETEST_RUN_MIN:] if len(run) >= RETEST_RUN_MIN else []

    if len(trio) < RETEST_RUN_MIN:
        have = len(run)
        more = RETEST_RUN_MIN - have
        failures.append(
            f"复测不足 {RETEST_RUN_MIN} 条连续序号"
            f"（当前连续 {have} 条，还差 {more} 条）"
        )
    else:
        mean = sum((Decimal(r.redoxMv) for r in trio), Decimal(0)) / RETEST_RUN_MIN
        if mean > READY_MEAN_MAX:
            seqs = "、".join(str(r.seq) for r in trio)
            failures.append(
                f"连续序号 {seqs} 的三条电位算术平均为 {mean} mV，"
                f"高于 {READY_MEAN_MAX} mV"
            )

    if latest is not None:
        last_retest = max(retests, key=lambda r: (_as_utc(r.sampledAt), r.id))
        if _as_utc(last_retest.sampledAt) <= _as_utc(latest.dippedAt):
            failures.append(
                "最后一次复测时间 "
                f"{_as_utc(last_retest.sampledAt).strftime('%Y-%m-%d %H:%M')} "
                "不晚于该缸最近浸染时间 "
                f"{_as_utc(latest.dippedAt).strftime('%Y-%m-%d %H:%M')}"
            )

    if failures:
        raise VatRuleError("无法设为可染色：" + "；".join(failures) + "。")
    assert trio and mean is not None
    return mean


def assert_can_mark_ready(
    latest: Optional[DipLot], retests: Sequence[RedoxRetest] = ()
) -> Decimal:
    """原浸染电位门槛 + 复测均值门槛；通过返回复测三条均值（供回写）。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > READY_REDOX_MAX:
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 "
            f"{READY_REDOX_MAX} mV。"
        )
    return assert_retest_ready_gate(latest, retests)


def validate_vat_status_change(
    vat: Vat,
    new_status: str,
    latest: Optional[DipLot],
    retests: Sequence[RedoxRetest] = (),
) -> Optional[Decimal]:
    """同一判定函数：改可染色时两道门槛一起算，返回均值（非可染色返回 None）。"""
    if new_status == Vat.STATUS_READY:
        return assert_can_mark_ready(latest, retests)
    return None
