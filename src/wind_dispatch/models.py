"""海上风电场调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
POWER_PRICE_INDEXES = {"PEAK_VALLEY", "MARKET_SETTLED", "GRID_COMMITTED", "DAY_AHEAD", "REGULATED", "CUSTOM"}
PRODUCTS = {"turbine-18mw", "turbine-16mw", "turbine-14mw", "reactive-compensator", "subsea-cable", "maintenance-vessel"}
ROUTE_KINDS = {"export-corridor", "offshore-station", "station", "storage", "compensation-station"}
GRANT_TYPES = {"GUARANTEED_VOLUME", "MAINTENANCE_EXEMPT", "CAPACITY_COMPENSATION", "OVERAGE_EXCEPTION"}


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


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    market_index: str
    trade_date: str
    close_cny: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        market_index = required_text(raw.get("market_index"), "market_index", 16).upper()
        if market_index not in POWER_PRICE_INDEXES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是 PEAK_VALLEY、MARKET_SETTLED、GRID_COMMITTED、DAY_AHEAD 或 REGULATED")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            market_index=market_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_cny=decimal_value(raw.get("close_cny"), "close_cny", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_mwh=decimal_value(
                raw.get("capacity_mwh"), "capacity_mwh", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的机组类型")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("送出通道起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_mwh: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的机组类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_mwh=decimal_value(
                raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_mwh: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_mwh=decimal_value(
                raw.get("requested_mwh"), "requested_mwh", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    market_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            market_index_drop_percent=decimal_value(
                raw.get("market_index_drop_percent", 0),
                "market_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )


@dataclass(frozen=True, slots=True)
class EntitlementGrant:
    entitlement_id: str
    facility_id: str
    product: str
    grant_type: str
    quantity_mwh: Decimal
    applicable_from: str
    applicable_to: str
    expires_at: str
    source_ref: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EntitlementGrant":
        grant_type = required_text(raw.get("grant_type"), "grant_type", 32)
        if grant_type not in GRANT_TYPES:
            raise ValidationFailed("grant_type 必须是保障性电量、检修免责或容量补偿")
        return cls(
            entitlement_id=identifier(raw.get("entitlement_id"), "entitlement_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=required_text(raw.get("product"), "product", 32),
            grant_type=grant_type,
            quantity_mwh=decimal_value(
                raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001")
            ),
            applicable_from=required_text(raw.get("applicable_from"), "applicable_from", 40),
            applicable_to=required_text(raw.get("applicable_to"), "applicable_to", 40),
            expires_at=required_text(raw.get("expires_at"), "expires_at", 40),
            source_ref=identifier(raw.get("source_ref"), "source_ref"),
            note=str(raw.get("note", "") or "")[:512],
        )


@dataclass(frozen=True, slots=True)
class DeliveryPlanDraft:
    plan_id: str
    route_id: str
    starts_at: str
    ends_at: str
    quantity_mwh: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeliveryPlanDraft":
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            starts_at=required_text(raw.get("starts_at"), "starts_at", 40),
            ends_at=required_text(raw.get("ends_at"), "ends_at", 40),
            quantity_mwh=decimal_value(
                raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001")
            ),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
