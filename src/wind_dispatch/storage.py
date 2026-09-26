"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_mwh TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_mwh TEXT NOT NULL,
    available_mwh TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_mwh TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_mwh TEXT NOT NULL,
    allocated_mwh TEXT NOT NULL DEFAULT '0',
    delivered_mwh TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    generated_mwh TEXT NOT NULL,
    expected_delivered_mwh TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);

CREATE TABLE IF NOT EXISTS entitlements (
    entitlement_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grant_type TEXT NOT NULL
        CHECK(grant_type IN ('GUARANTEED_VOLUME','MAINTENANCE_EXEMPT','CAPACITY_COMPENSATION','OVERAGE_EXCEPTION')),
    quantity_mwh TEXT NOT NULL,
    held_mwh TEXT NOT NULL DEFAULT '0',
    consumed_mwh TEXT NOT NULL DEFAULT '0',
    expired_mwh TEXT NOT NULL DEFAULT '0',
    applicable_from TEXT NOT NULL,
    applicable_to TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','expired','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(CAST(quantity_mwh AS REAL) > 0),
    CHECK(applicable_to >= applicable_from),
    CHECK(expires_at >= applicable_to)
);

CREATE INDEX IF NOT EXISTS idx_entitlements_account
ON entitlements(facility_id, product, expires_at);

CREATE TABLE IF NOT EXISTS entitlement_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id TEXT NOT NULL,
    product TEXT NOT NULL,
    entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
    action TEXT NOT NULL
        CHECK(action IN ('GRANT','HOLD','RELEASE','WRITE_OFF','RETURN','EXPIRE')),
    amount_mwh TEXT NOT NULL,
    plan_id TEXT,
    segment_key TEXT,
    period_key TEXT,
    reason_code TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ent_entries_account
ON entitlement_entries(facility_id, product, entitlement_id, entry_id);

CREATE INDEX IF NOT EXISTS idx_ent_entries_plan
ON entitlement_entries(plan_id, segment_key, entry_id);

CREATE TABLE IF NOT EXISTS delivery_plans (
    plan_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    quantity_mwh TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','pending_review','confirmed','partially_settled','settled','cancelled','failed')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_facility
ON delivery_plans(facility_id, state, starts_at);

CREATE TABLE IF NOT EXISTS delivery_plan_segments (
    segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES delivery_plans(plan_id),
    period_key TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    quantity_mwh TEXT NOT NULL,
    held_mwh TEXT NOT NULL DEFAULT '0',
    overage_mwh TEXT NOT NULL DEFAULT '0',
    actual_mwh TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','held','settled','returned','failed')),
    revision INTEGER NOT NULL DEFAULT 1,
    UNIQUE(plan_id, period_key)
);

CREATE INDEX IF NOT EXISTS idx_segments_period
ON delivery_plan_segments(period_key, state);

CREATE TABLE IF NOT EXISTS entitlement_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
    plan_id TEXT NOT NULL REFERENCES delivery_plans(plan_id),
    segment_key TEXT NOT NULL,
    amount_mwh TEXT NOT NULL,
    consumed_mwh TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','consumed','released')),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_holds_active
ON entitlement_holds(entitlement_id, plan_id, segment_key) WHERE state!='released';

CREATE TABLE IF NOT EXISTS route_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES delivery_plans(plan_id),
    segment_key TEXT NOT NULL,
    reserved_mwh TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released','consumed')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_route_date
ON route_reservations(route_id, service_date, state);

CREATE TABLE IF NOT EXISTS overage_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES delivery_plans(plan_id),
    period_key TEXT NOT NULL,
    shortfall_mwh TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','approved','rejected','expired')),
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES supply_users(user_id),
    reviewed_at TEXT,
    decision_note TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_overage_pending
ON overage_reviews(plan_id, period_key) WHERE state='pending';

CREATE INDEX IF NOT EXISTS idx_overage_reviewer
ON overage_reviews(state, expires_at);

CREATE TABLE IF NOT EXISTS settlement_periods (
    period_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
    closed_by TEXT REFERENCES supply_users(user_id),
    closed_at TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
