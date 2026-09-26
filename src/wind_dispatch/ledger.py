"""可用率权益账本的纯计算规则：结算时钟、跨年拆分与额度选择。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Sequence

from .planning import decimal_text, quantize_volume


PERIOD_FORMAT = "%Y-%m"
ZERO = Decimal("0")


def period_key(value: datetime) -> str:
    """结算时钟周期键，统一使用 UTC 月份。"""
    return value.astimezone(timezone.utc).strftime(PERIOD_FORMAT)


def period_bounds(key: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(key + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, next_month


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    period_key: str
    starts_at: datetime
    ends_at: datetime  # 排他边界

    @property
    def seconds(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds())


def split_by_period(starts_at: datetime, ends_at: datetime) -> list[PeriodWindow]:
    """按可注入的结算时钟（UTC 月边界）拆分时间窗，支持跨年计划。"""
    if ends_at <= starts_at:
        raise ValueError("结束时间必须晚于开始时间")
    windows: list[PeriodWindow] = []
    current = starts_at.astimezone(timezone.utc)
    end = ends_at.astimezone(timezone.utc)
    while current < end:
        _, next_month = period_bounds(period_key(current))
        window_end = min(next_month, end)
        windows.append(PeriodWindow(period_key(current), current, window_end))
        current = window_end
    return windows


def split_quantities(total: Decimal, windows: Sequence[PeriodWindow]) -> list[Decimal]:
    """按各周期覆盖时长比例拆分计划电量，舍入差额并入最后一个周期。"""
    if total <= ZERO:
        raise ValueError("计划电量必须为正数")
    if not windows:
        raise ValueError("至少需要一个结算周期")
    weights = [window.seconds for window in windows]
    weight_total = sum(weights)
    amounts: list[Decimal] = []
    allocated = ZERO
    for weight in weights[:-1]:
        amount = quantize_volume(total * Decimal(weight) / Decimal(weight_total))
        amounts.append(amount)
        allocated += amount
    remainder = quantize_volume(total - allocated)
    if remainder < ZERO:
        raise ValueError("计划电量拆分出现负余额")
    amounts.append(remainder)
    return amounts


def daily_slices(window: PeriodWindow, quantity: Decimal) -> list[tuple[str, Decimal]]:
    """把周期段电量按覆盖的 UTC 日切片，用于通道容量预留。"""
    dates: list[str] = []
    weights: list[int] = []
    current = window.starts_at
    while current < window.ends_at:
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = day_start + timedelta(days=1)
        slice_end = min(next_day, window.ends_at)
        dates.append(day_start.date().isoformat())
        weights.append(int((slice_end - current).total_seconds()))
        current = slice_end
    return list(zip(dates, _weighted(quantity, weights), strict=True))


def _weighted(total: Decimal, weights: Iterable[int]) -> list[Decimal]:
    weights = list(weights)
    weight_total = sum(weights)
    amounts: list[Decimal] = []
    allocated = ZERO
    for weight in weights[:-1]:
        amount = quantize_volume(total * Decimal(weight) / Decimal(weight_total))
        amounts.append(amount)
        allocated += amount
    amounts.append(quantize_volume(total - allocated))
    return amounts


@dataclass(frozen=True, slots=True)
class GrantAvailability:
    entitlement_id: str
    available_mwh: Decimal
    expires_at: str


def select_first_expiring(
    grants: Sequence[GrantAvailability],
    quantity: Decimal,
) -> list[tuple[str, Decimal]]:
    """额度选择策略：到期早的优先（FEFO），同到期时间按编号排序。"""
    if quantity <= ZERO:
        raise ValueError("预占电量必须为正数")
    remaining = quantize_volume(quantity)
    selections: list[tuple[str, Decimal]] = []
    ordered = sorted(grants, key=lambda item: (item.expires_at, item.entitlement_id))
    for grant in ordered:
        if remaining == ZERO:
            break
        available = quantize_volume(grant.available_mwh)
        if available <= ZERO:
            continue
        taken = min(available, remaining)
        selections.append((grant.entitlement_id, taken))
        remaining = quantize_volume(remaining - taken)
    if remaining > ZERO:
        raise ValueError(f"可用额度不足，缺口 {decimal_text(remaining)} mwh")
    return selections
