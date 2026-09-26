"""可用率权益账本的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ledger_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('site','business','auditor')),
    facility_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounting_rules (
    rule_version INTEGER PRIMARY KEY AUTOINCREMENT,
    review_deadline_hours INTEGER NOT NULL,
    expire_unused INTEGER NOT NULL CHECK(expire_unused IN (0,1)),
    overuse_policy TEXT NOT NULL CHECK(overuse_policy IN ('forbid','review')),
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_periods (
    period TEXT PRIMARY KEY,
    rule_version INTEGER NOT NULL REFERENCES accounting_rules(rule_version),
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','settled')),
    settled_by TEXT REFERENCES ledger_users(user_id),
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_channels (
    route_id TEXT PRIMARY KEY,
    period_capacity_mwh TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entitlement_accounts (
    account_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(facility_id, batch_id)
);

CREATE TABLE IF NOT EXISTS entitlement_grants (
    grant_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES entitlement_accounts(account_id),
    kind TEXT NOT NULL CHECK(kind IN ('GUARANTEED_ENERGY','MAINTENANCE_EXEMPTION','CAPACITY_COMPENSATION')),
    amount_mwh TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    period TEXT NOT NULL REFERENCES settlement_periods(period),
    rule_version INTEGER NOT NULL REFERENCES accounting_rules(rule_version),
    source_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_to > valid_from)
);

CREATE INDEX IF NOT EXISTS idx_grants_account
ON entitlement_grants(account_id, valid_to);

CREATE TABLE IF NOT EXISTS delivery_plans (
    plan_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES entitlement_accounts(account_id),
    route_id TEXT NOT NULL REFERENCES ledger_channels(route_id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    total_mwh TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','confirmed','cancelled','failed')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(window_end >= window_start)
);

CREATE TABLE IF NOT EXISTS plan_segments (
    segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES delivery_plans(plan_id),
    period TEXT NOT NULL REFERENCES settlement_periods(period),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    amount_mwh TEXT NOT NULL,
    delivered_mwh TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','delivering','closed')),
    UNIQUE(plan_id, period)
);

CREATE INDEX IF NOT EXISTS idx_segments_plan
ON plan_segments(plan_id, period);

CREATE TABLE IF NOT EXISTS channel_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES delivery_plans(plan_id),
    segment_id INTEGER NOT NULL REFERENCES plan_segments(segment_id),
    route_id TEXT NOT NULL REFERENCES ledger_channels(route_id),
    period TEXT NOT NULL,
    amount_mwh TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_route_period
ON channel_reservations(route_id, period, state);

CREATE TABLE IF NOT EXISTS exception_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES entitlement_accounts(account_id),
    plan_id TEXT NOT NULL REFERENCES delivery_plans(plan_id),
    segment_id INTEGER NOT NULL REFERENCES plan_segments(segment_id),
    over_mwh TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','rejected','expired')),
    deadline_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    submitted_at TEXT NOT NULL,
    decided_by TEXT REFERENCES ledger_users(user_id),
    decided_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_reviews_state
ON exception_reviews(state, deadline_at);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES entitlement_accounts(account_id),
    grant_id TEXT REFERENCES entitlement_grants(grant_id),
    plan_id TEXT REFERENCES delivery_plans(plan_id),
    segment_id INTEGER REFERENCES plan_segments(segment_id),
    review_id INTEGER REFERENCES exception_reviews(review_id),
    action TEXT NOT NULL CHECK(action IN
        ('GRANT','HOLD','RELEASE','CONSUME','CONSUME_OVER','EXPIRE','OVERUSE_FLAG')),
    amount_mwh TEXT NOT NULL,
    period TEXT NOT NULL REFERENCES settlement_periods(period),
    rule_version INTEGER NOT NULL REFERENCES accounting_rules(rule_version),
    actor_id TEXT NOT NULL REFERENCES ledger_users(user_id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_account
ON ledger_entries(account_id, grant_id, entry_id);

CREATE INDEX IF NOT EXISTS idx_entries_segment
ON ledger_entries(plan_id, segment_id);

CREATE TABLE IF NOT EXISTS ledger_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ledger_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_ledger_audit_entity
ON ledger_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
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
