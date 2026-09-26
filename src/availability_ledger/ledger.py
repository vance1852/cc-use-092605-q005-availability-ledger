"""确定性的权益拆分、余额归集和额度选择计算。"""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
VOLUME_QUANTUM = Decimal("0.001")


def quantize_volume(value: Decimal) -> Decimal:
    return value.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True, slots=True)
class PeriodShare:
    period: str
    start: date
    end: date
    days: int
    amount_mwh: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "period": self.period,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "amount_mwh": decimal_text(self.amount_mwh),
        }


def split_window_into_periods(window_start: date, window_end: date, total_mwh: Decimal) -> list[PeriodShare]:
    """按结算周期（自然月）拆分跨期计划，总量守恒，舍入余数落在最后一个分段。"""
    if window_end < window_start:
        raise ValueError("window_end 不能早于 window_start")
    if total_mwh <= ZERO:
        raise ValueError("拆分总量必须大于零")
    spans: list[tuple[str, date, date, int]] = []
    cursor = window_start
    while cursor <= window_end:
        segment_end = min(month_last_day(cursor.year, cursor.month), window_end)
        days = (segment_end - cursor).days + 1
        spans.append((period_of(cursor), cursor, segment_end, days))
        cursor = segment_end + timedelta(days=1)
    total_days = (window_end - window_start).days + 1
    shares: list[PeriodShare] = []
    allocated = ZERO
    for index, (period, start, end, days) in enumerate(spans):
        if index < len(spans) - 1:
            amount = quantize_volume(total_mwh * days / total_days)
        else:
            amount = quantize_volume(total_mwh - allocated)
        allocated = quantize_volume(allocated + amount)
        shares.append(PeriodShare(period, start, end, days, amount))
    return shares


@dataclass(frozen=True, slots=True)
class GrantBalance:
    grant_id: str
    kind: str
    valid_from: str
    valid_to: str
    granted: Decimal
    held: Decimal
    consumed: Decimal
    expired: Decimal

    @property
    def available(self) -> Decimal:
        return self.granted - self.held - self.consumed - self.expired

    def as_dict(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "kind": self.kind,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "granted_mwh": decimal_text(self.granted),
            "held_mwh": decimal_text(self.held),
            "consumed_mwh": decimal_text(self.consumed),
            "expired_mwh": decimal_text(self.expired),
            "available_mwh": decimal_text(self.available),
        }


def balances_from_entries(
    grants: Iterable[Mapping[str, object]],
    entries: Iterable[Mapping[str, object]],
) -> dict[str, GrantBalance]:
    """从动作流水归集每个额度来源的余额，任一余额都能反向追到形成它的动作。"""
    granted: dict[str, Decimal] = {}
    held: dict[str, Decimal] = {}
    consumed: dict[str, Decimal] = {}
    expired: dict[str, Decimal] = {}
    for entry in entries:
        grant_id = entry["grant_id"]
        if grant_id is None:
            continue
        amount = Decimal(str(entry["amount_mwh"]))
        action = entry["action"]
        if action == "GRANT":
            granted[grant_id] = granted.get(grant_id, ZERO) + amount
        elif action == "HOLD":
            held[grant_id] = held.get(grant_id, ZERO) + amount
        elif action == "RELEASE":
            held[grant_id] = held.get(grant_id, ZERO) - amount
        elif action == "CONSUME":
            held[grant_id] = held.get(grant_id, ZERO) - amount
            consumed[grant_id] = consumed.get(grant_id, ZERO) + amount
        elif action == "CONSUME_OVER":
            consumed[grant_id] = consumed.get(grant_id, ZERO) + amount
        elif action == "EXPIRE":
            expired[grant_id] = expired.get(grant_id, ZERO) + amount
        else:
            raise ValueError(f"未知流水动作 {action}")
    result: dict[str, GrantBalance] = {}
    for grant in grants:
        grant_id = str(grant["grant_id"])
        result[grant_id] = GrantBalance(
            grant_id=grant_id,
            kind=str(grant["kind"]),
            valid_from=str(grant["valid_from"]),
            valid_to=str(grant["valid_to"]),
            granted=granted.get(grant_id, ZERO),
            held=held.get(grant_id, ZERO),
            consumed=consumed.get(grant_id, ZERO),
            expired=expired.get(grant_id, ZERO),
        )
    return result


class Shortfall(Exception):
    """可选余额不足以覆盖请求量。"""

    def __init__(self, remaining: Decimal) -> None:
        super().__init__(f"可用额度不足，缺口 {decimal_text(remaining)} MWh")
        self.remaining = remaining


def select_fefo(candidates: Sequence[GrantBalance], amount: Decimal) -> list[tuple[str, Decimal]]:
    """按到期日先到期先出选择额度，已被其他计划预占的余额不在候选中。"""
    if amount <= ZERO:
        raise ValueError("选择量必须大于零")
    remaining = quantize_volume(amount)
    picks: list[tuple[str, Decimal]] = []
    ordered = sorted(candidates, key=lambda item: (item.valid_to, item.grant_id))
    for balance in ordered:
        if remaining <= ZERO:
            break
        take = min(balance.available, remaining)
        if take > ZERO:
            picks.append((balance.grant_id, take))
            remaining = quantize_volume(remaining - take)
    if remaining > ZERO:
        raise Shortfall(remaining)
    return picks
