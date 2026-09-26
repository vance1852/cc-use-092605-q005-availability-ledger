"""可用率权益账本领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
GRANT_KINDS = {"GUARANTEED_ENERGY", "MAINTENANCE_EXEMPTION", "CAPACITY_COMPENSATION"}
OVERUSE_POLICIES = {"forbid", "review"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def period_text(value: object, field: str = "period") -> str:
    result = required_text(value, field, 7)
    if not PERIOD.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是 YYYY-MM 结算周期")
    return result


@dataclass(frozen=True, slots=True)
class RuleDraft:
    review_deadline_hours: int
    expire_unused: bool
    overuse_policy: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleDraft":
        hours = raw.get("review_deadline_hours")
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 720:
            raise ValidationFailed("review_deadline_hours 必须是 1 到 720 的整数")
        expire_unused = raw.get("expire_unused")
        if not isinstance(expire_unused, bool):
            raise ValidationFailed("expire_unused 必须是布尔值")
        policy = required_text(raw.get("overuse_policy"), "overuse_policy", 16)
        if policy not in OVERUSE_POLICIES:
            raise ValidationFailed("overuse_policy 必须是 forbid 或 review")
        return cls(
            review_deadline_hours=hours,
            expire_unused=expire_unused,
            overuse_policy=policy,
            note=required_text(raw.get("note", "核算规则"), "note", 256),
        )


@dataclass(frozen=True, slots=True)
class ChannelRegistration:
    route_id: str
    period_capacity_mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChannelRegistration":
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            period_capacity_mwh=decimal_value(
                raw.get("period_capacity_mwh"), "period_capacity_mwh", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class AccountOpening:
    account_id: str
    facility_id: str
    batch_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AccountOpening":
        return cls(
            account_id=identifier(raw.get("account_id"), "account_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
        )


@dataclass(frozen=True, slots=True)
class GrantRegistration:
    grant_id: str
    account_id: str
    kind: str
    amount_mwh: Decimal
    valid_from: str
    valid_to: str
    period: str
    source_ref: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GrantRegistration":
        kind = required_text(raw.get("kind"), "kind", 32).upper()
        if kind not in GRANT_KINDS:
            raise ValidationFailed("kind 必须是 GUARANTEED_ENERGY、MAINTENANCE_EXEMPTION 或 CAPACITY_COMPENSATION")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_to = date_text(raw.get("valid_to"), "valid_to")
        if valid_to <= valid_from:
            raise ValidationFailed("valid_to 必须晚于 valid_from")
        return cls(
            grant_id=identifier(raw.get("grant_id"), "grant_id"),
            account_id=identifier(raw.get("account_id"), "account_id"),
            kind=kind,
            amount_mwh=decimal_value(raw.get("amount_mwh"), "amount_mwh", minimum=Decimal("0.001")),
            valid_from=valid_from,
            valid_to=valid_to,
            period=period_text(raw.get("period")),
            source_ref=required_text(raw.get("source_ref"), "source_ref", 128),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class PlanDraft:
    plan_id: str
    account_id: str
    route_id: str
    window_start: str
    window_end: str
    total_mwh: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanDraft":
        window_start = date_text(raw.get("window_start"), "window_start")
        window_end = date_text(raw.get("window_end"), "window_end")
        if window_end < window_start:
            raise ValidationFailed("window_end 不能早于 window_start")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            account_id=identifier(raw.get("account_id"), "account_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            window_start=window_start,
            window_end=window_end,
            total_mwh=decimal_value(raw.get("total_mwh"), "total_mwh", minimum=Decimal("0.001")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    period: str
    actual_mwh: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeliveryRecord":
        return cls(
            period=period_text(raw.get("period")),
            actual_mwh=decimal_value(raw.get("actual_mwh"), "actual_mwh", minimum=Decimal("0.001")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
