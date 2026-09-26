"""分户权益流水、送出计划确认、限时复核和结算周期核算规则的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .ledger import (
    ZERO,
    GrantBalance,
    Shortfall,
    balances_from_entries,
    canonical_json,
    decimal_text,
    digest,
    period_of,
    quantize_volume,
    select_fefo,
    split_window_into_periods,
)
from .models import (
    AccountOpening,
    ChannelRegistration,
    DeliveryRecord,
    GrantRegistration,
    PlanDraft,
    RuleDraft,
    period_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "site": {"plan.write", "delivery.write", "view.own", "trace.read"},
    "business": {
        "account.write",
        "grant.write",
        "channel.write",
        "rule.write",
        "period.write",
        "maintenance.run",
        "delivery.write",
        "review.decide",
        "summary.read",
        "trace.read",
    },
    "auditor": {"audit.read", "trace.read"},
}


class AvailabilityLedgerService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ---------- 基础辅助 ----------

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> date:
        return self._now_dt().date()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ledger_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM ledger_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO ledger_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent_replay(self, scope: str, key: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM ledger_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != digest(raw):
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(stored["response_json"])

    def _store_idempotent(self, scope: str, key: str, raw: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO ledger_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(raw), canonical_json(response), self._now()),
        )

    def _latest_rule(self) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM accounting_rules ORDER BY rule_version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise InvalidState("尚未发布核算规则")
        return row

    def _bookable_period(self, period: str) -> sqlite3.Row:
        """返回可入账周期；未开立的周期按最新核算规则自动开立，已结算周期禁止入账。"""
        row = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period=?", (period,)
        ).fetchone()
        if row is None:
            rule = self._latest_rule()
            self.connection.execute(
                "INSERT INTO settlement_periods(period,rule_version) VALUES(?,?)",
                (period, rule["rule_version"]),
            )
            row = self.connection.execute(
                "SELECT * FROM settlement_periods WHERE period=?", (period,)
            ).fetchone()
        if row["state"] != "open":
            raise InvalidState(f"结算周期 {period} 已结算，禁止入账")
        return row

    def _book_entry(
        self,
        *,
        account_id: str,
        action: str,
        amount: Decimal,
        period: str,
        actor_id: str,
        grant_id: str | None = None,
        plan_id: str | None = None,
        segment_id: int | None = None,
        review_id: int | None = None,
        note: str = "",
    ) -> int:
        period_row = self._bookable_period(period)
        cursor = self.connection.execute(
            "INSERT INTO ledger_entries(account_id,grant_id,plan_id,segment_id,review_id,action,amount_mwh,"
            "period,rule_version,actor_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                grant_id,
                plan_id,
                segment_id,
                review_id,
                action,
                decimal_text(quantize_volume(amount)),
                period,
                period_row["rule_version"],
                actor_id,
                note,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _account(self, account_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM entitlement_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise NotFound("权益账户不存在")
        return row

    def _plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("送出计划不存在")
        return row

    def _check_own(self, user: sqlite3.Row, account_id: str) -> sqlite3.Row:
        account = self._account(account_id)
        if user["role"] == "site" and user["facility_id"] != account["facility_id"]:
            raise Forbidden("只能操作本场站的权益账户")
        return account

    def _balances(self, account_id: str) -> dict[str, GrantBalance]:
        grants = self.connection.execute(
            "SELECT * FROM entitlement_grants WHERE account_id=? ORDER BY grant_id", (account_id,)
        ).fetchall()
        entries = self.connection.execute(
            "SELECT grant_id,action,amount_mwh FROM ledger_entries WHERE account_id=?", (account_id,)
        ).fetchall()
        return balances_from_entries([dict(row) for row in grants], [dict(row) for row in entries])

    def _segment_hold_remaining(self, segment_id: int) -> dict[str, Decimal]:
        rows = self.connection.execute(
            "SELECT grant_id,action,amount_mwh FROM ledger_entries "
            "WHERE segment_id=? AND action IN ('HOLD','CONSUME','RELEASE')",
            (segment_id,),
        ).fetchall()
        remaining: dict[str, Decimal] = {}
        for row in rows:
            amount = Decimal(row["amount_mwh"])
            delta = amount if row["action"] == "HOLD" else -amount
            remaining[row["grant_id"]] = remaining.get(row["grant_id"], ZERO) + delta
        return {grant_id: value for grant_id, value in remaining.items() if value > ZERO}

    # ---------- 用户 ----------

    def create_user(
        self, user_id: str, display_name: str, role: str, facility_id: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        facility = facility_id.strip() if isinstance(facility_id, str) and facility_id.strip() else None
        if role == "site" and facility is None:
            raise ValidationFailed("场站人员必须绑定场站")
        if role != "site" and facility is not None:
            raise ValidationFailed("经营与审计人员不绑定场站")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO ledger_users(user_id,display_name,role,facility_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, facility, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "facility_id": facility}

    # ---------- 核算规则与结算周期 ----------

    def publish_rules(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """发布新版核算规则；只重钉未结算周期，已结算周期保留原版本。"""
        self._require(actor_id, "rule.write")
        draft = RuleDraft.from_dict(raw)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO accounting_rules(review_deadline_hours,expire_unused,overuse_policy,note,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (
                    draft.review_deadline_hours,
                    1 if draft.expire_unused else 0,
                    draft.overuse_policy,
                    draft.note,
                    actor_id,
                    self._now(),
                ),
            )
            version = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE settlement_periods SET rule_version=? WHERE state='open'", (version,)
            )
            repinned = [
                row["period"]
                for row in self.connection.execute(
                    "SELECT period FROM settlement_periods WHERE state='open' ORDER BY period"
                ).fetchall()
            ]
            self._audit(
                "accounting_rule",
                str(version),
                "rule.published",
                actor_id,
                {"rule_version": version, "repinned_periods": repinned},
            )
        return {"rule_version": version, "repinned_periods": repinned}

    def open_period(self, actor_id: str, period: str) -> dict[str, Any]:
        self._require(actor_id, "period.write")
        period = period_text(period)
        existing = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period=?", (period,)
        ).fetchone()
        if existing is not None:
            raise Conflict("结算周期已开立")
        with transaction(self.connection, immediate=True):
            rule = self._latest_rule()
            self.connection.execute(
                "INSERT INTO settlement_periods(period,rule_version) VALUES(?,?)",
                (period, rule["rule_version"]),
            )
            self._audit("settlement_period", period, "period.opened", actor_id, {"rule_version": rule["rule_version"]})
        return {"period": period, "rule_version": rule["rule_version"], "state": "open"}

    def settle_period(self, actor_id: str, period: str) -> dict[str, Any]:
        self._require(actor_id, "period.write")
        period = period_text(period)
        row = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period=?", (period,)
        ).fetchone()
        if row is None:
            raise NotFound("结算周期不存在")
        if row["state"] != "open":
            raise InvalidState("结算周期已结算")
        pending = self.connection.execute(
            "SELECT COUNT(*) AS c FROM exception_reviews r "
            "JOIN plan_segments s ON s.segment_id=r.segment_id "
            "WHERE s.period=? AND r.state='pending'",
            (period,),
        ).fetchone()["c"]
        if pending:
            raise Conflict("该周期存在待复核的超额，不能结算")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE settlement_periods SET state='settled',settled_by=?,settled_at=? WHERE period=?",
                (actor_id, self._now(), period),
            )
            self._audit("settlement_period", period, "period.settled", actor_id, {"rule_version": row["rule_version"]})
        return {"period": period, "state": "settled", "rule_version": row["rule_version"]}

    # ---------- 主数据 ----------

    def register_channel(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "channel.write")
        channel = ChannelRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO ledger_channels(route_id,period_capacity_mwh,created_at) VALUES(?,?,?)",
                    (channel.route_id, decimal_text(channel.period_capacity_mwh), self._now()),
                )
                self._audit("channel", channel.route_id, "channel.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出通道已经登记") from exc
        return {"route_id": channel.route_id, "period_capacity_mwh": decimal_text(channel.period_capacity_mwh)}

    def open_account(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "account.write")
        opening = AccountOpening.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO entitlement_accounts(account_id,facility_id,batch_id,created_at) VALUES(?,?,?,?)",
                    (opening.account_id, opening.facility_id, opening.batch_id, self._now()),
                )
                self._audit("account", opening.account_id, "account.opened", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("该场站与机组批次的分户已经存在") from exc
        return {"account_id": opening.account_id, "facility_id": opening.facility_id, "batch_id": opening.batch_id}

    def register_grant(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "grant.write")
        grant = GrantRegistration.from_dict(raw)
        replay = self._idempotent_replay("grant", grant.idempotency_key, raw)
        if replay is not None:
            return replay
        account = self._account(grant.account_id)
        if account["state"] != "open":
            raise InvalidState("权益账户已关闭")
        if grant.period < period_of(self._today()):
            raise ValidationFailed("额度所属周期不能早于当前结算周期")
        response = {
            "grant_id": grant.grant_id,
            "account_id": grant.account_id,
            "kind": grant.kind,
            "amount_mwh": decimal_text(quantize_volume(grant.amount_mwh)),
            "period": grant.period,
        }
        try:
            with transaction(self.connection, immediate=True):
                period_row = self._bookable_period(grant.period)
                self.connection.execute(
                    "INSERT INTO entitlement_grants(grant_id,account_id,kind,amount_mwh,valid_from,valid_to,"
                    "period,rule_version,source_ref,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant.grant_id,
                        grant.account_id,
                        grant.kind,
                        decimal_text(quantize_volume(grant.amount_mwh)),
                        grant.valid_from,
                        grant.valid_to,
                        grant.period,
                        period_row["rule_version"],
                        grant.source_ref,
                        grant.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._book_entry(
                    account_id=grant.account_id,
                    grant_id=grant.grant_id,
                    action="GRANT",
                    amount=grant.amount_mwh,
                    period=grant.period,
                    actor_id=actor_id,
                    note=grant.source_ref,
                )
                self._store_idempotent("grant", grant.idempotency_key, raw, response)
                self._audit("grant", grant.grant_id, "grant.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("额度来源编号或幂等键冲突") from exc
        return response

    # ---------- 送出计划 ----------

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "plan.write")
        draft = PlanDraft.from_dict(raw)
        replay = self._idempotent_replay("plan", draft.idempotency_key, raw)
        if replay is not None:
            return replay
        self._check_own(user, draft.account_id)
        channel = self.connection.execute(
            "SELECT * FROM ledger_channels WHERE route_id=?", (draft.route_id,)
        ).fetchone()
        if channel is None:
            raise NotFound("送出通道不存在")
        if channel["state"] != "active":
            raise InvalidState("送出通道不可用")
        if period_of(date.fromisoformat(draft.window_start)) < period_of(self._today()):
            raise ValidationFailed("计划窗口不能始于已过去的结算周期")
        shares = split_window_into_periods(
            date.fromisoformat(draft.window_start), date.fromisoformat(draft.window_end), draft.total_mwh
        )
        response = {
            "plan_id": draft.plan_id,
            "state": "draft",
            "revision": 1,
            "segments": [share.as_dict() for share in shares],
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO delivery_plans(plan_id,account_id,route_id,window_start,window_end,total_mwh,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        draft.plan_id,
                        draft.account_id,
                        draft.route_id,
                        draft.window_start,
                        draft.window_end,
                        decimal_text(quantize_volume(draft.total_mwh)),
                        draft.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._store_idempotent("plan", draft.idempotency_key, raw, response)
                self._audit("plan", draft.plan_id, "plan.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划编号或幂等键冲突") from exc
        return response

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        """确认送出计划：额度预占与通道预留在同一事务内完成，任一失败整体回滚。"""
        user = self._require(actor_id, "plan.write")
        plan = self._plan(plan_id)
        self._check_own(user, plan["account_id"])
        if plan["state"] != "draft" or plan["revision"] != expected_revision:
            raise InvalidState("计划不是当前草稿版本")
        shares = split_window_into_periods(
            date.fromisoformat(plan["window_start"]),
            date.fromisoformat(plan["window_end"]),
            Decimal(plan["total_mwh"]),
        )
        today_text = self._today().isoformat()
        with transaction(self.connection, immediate=True):
            balances = self._balances(plan["account_id"])
            remaining_available = {grant_id: balance.available for grant_id, balance in balances.items()}
            channel = self.connection.execute(
                "SELECT * FROM ledger_channels WHERE route_id=?", (plan["route_id"],)
            ).fetchone()
            if channel is None or channel["state"] != "active":
                raise InvalidState("送出通道不可用")
            capacity = Decimal(channel["period_capacity_mwh"])
            segment_rows: list[dict[str, Any]] = []
            for share in shares:
                self._bookable_period(share.period)
                cursor = self.connection.execute(
                    "INSERT INTO plan_segments(plan_id,period,start_date,end_date,amount_mwh) VALUES(?,?,?,?,?)",
                    (plan_id, share.period, share.start.isoformat(), share.end.isoformat(), decimal_text(share.amount_mwh)),
                )
                segment_id = int(cursor.lastrowid)
                candidates = [
                    replace(balance, granted=remaining_available[grant_id], held=ZERO, consumed=ZERO, expired=ZERO)
                    for grant_id, balance in balances.items()
                    if remaining_available[grant_id] > ZERO
                    and balance.valid_from <= share.end.isoformat()
                    and balance.valid_to >= share.start.isoformat()
                    and balance.valid_to >= today_text
                ]
                try:
                    picks = select_fefo(candidates, share.amount_mwh)
                except Shortfall as exc:
                    raise Conflict(f"分段 {share.period} 可用额度不足，缺口 {decimal_text(exc.remaining)} MWh") from exc
                for grant_id, amount in picks:
                    remaining_available[grant_id] = quantize_volume(remaining_available[grant_id] - amount)
                    self._book_entry(
                        account_id=plan["account_id"],
                        grant_id=grant_id,
                        plan_id=plan_id,
                        segment_id=segment_id,
                        action="HOLD",
                        amount=amount,
                        period=share.period,
                        actor_id=actor_id,
                    )
                reserved = self.connection.execute(
                    "SELECT amount_mwh FROM channel_reservations WHERE route_id=? AND period=? AND state='held'",
                    (plan["route_id"], share.period),
                ).fetchall()
                used = sum((Decimal(row["amount_mwh"]) for row in reserved), ZERO)
                if used + share.amount_mwh > capacity:
                    raise Conflict(f"分段 {share.period} 通道预留超出周期容量")
                self.connection.execute(
                    "INSERT INTO channel_reservations(plan_id,segment_id,route_id,period,amount_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, segment_id, plan["route_id"], share.period, decimal_text(share.amount_mwh), self._now()),
                )
                segment_rows.append({"segment_id": segment_id, **share.as_dict()})
            cursor = self.connection.execute(
                "UPDATE delivery_plans SET state='confirmed',revision=revision+1 "
                "WHERE plan_id=? AND state='draft' AND revision=?",
                (plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划不是当前草稿版本")
            self._audit(
                "plan",
                plan_id,
                "plan.confirmed",
                actor_id,
                {"segments": len(segment_rows), "total_mwh": decimal_text(Decimal(plan["total_mwh"]))},
            )
        return {
            "plan_id": plan_id,
            "state": "confirmed",
            "revision": expected_revision + 1,
            "segments": segment_rows,
        }

    def record_delivery(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记分段实际电量；超出预占的部分按周期核算规则进入限时复核。"""
        user = self._require(actor_id, "delivery.write")
        record = DeliveryRecord.from_dict(raw)
        replay = self._idempotent_replay("delivery", record.idempotency_key, raw)
        if replay is not None:
            return replay
        plan = self._plan(plan_id)
        self._check_own(user, plan["account_id"])
        if plan["state"] != "confirmed":
            raise InvalidState("计划未确认或已终止")
        segment = self.connection.execute(
            "SELECT * FROM plan_segments WHERE plan_id=? AND period=?", (plan_id, record.period)
        ).fetchone()
        if segment is None:
            raise NotFound("计划没有该结算周期的分段")
        if segment["state"] == "closed":
            raise InvalidState("分段已关闭")
        with transaction(self.connection, immediate=True):
            period_row = self._bookable_period(record.period)
            rule = self.connection.execute(
                "SELECT * FROM accounting_rules WHERE rule_version=?", (period_row["rule_version"],)
            ).fetchone()
            remaining = self._segment_hold_remaining(segment["segment_id"])
            grants = self.connection.execute(
                "SELECT g.* FROM entitlement_grants g JOIN ledger_entries e ON e.grant_id=g.grant_id "
                "WHERE e.segment_id=? GROUP BY g.grant_id",
                (segment["segment_id"],),
            ).fetchall()
            valid_to = {row["grant_id"]: row["valid_to"] for row in grants}
            consume_total = min(record.actual_mwh, sum(remaining.values(), ZERO))
            left = consume_total
            for grant_id in sorted(remaining, key=lambda item: (valid_to.get(item, ""), item)):
                if left <= ZERO:
                    break
                take = min(remaining[grant_id], left)
                left = quantize_volume(left - take)
                self._book_entry(
                    account_id=plan["account_id"],
                    grant_id=grant_id,
                    plan_id=plan_id,
                    segment_id=segment["segment_id"],
                    action="CONSUME",
                    amount=take,
                    period=record.period,
                    actor_id=actor_id,
                )
            review_info: dict[str, Any] | None = None
            excess = quantize_volume(record.actual_mwh - consume_total)
            if excess > ZERO:
                if rule["overuse_policy"] == "forbid":
                    raise Conflict("超出预占额度且当前核算规则不允许超额")
                deadline = self._now_dt() + timedelta(hours=int(rule["review_deadline_hours"]))
                cursor = self.connection.execute(
                    "INSERT INTO exception_reviews(account_id,plan_id,segment_id,over_mwh,deadline_at,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        plan["account_id"],
                        plan_id,
                        segment["segment_id"],
                        decimal_text(excess),
                        utc_text(deadline),
                        actor_id,
                        self._now(),
                    ),
                )
                review_info = {
                    "review_id": int(cursor.lastrowid),
                    "over_mwh": decimal_text(excess),
                    "deadline_at": utc_text(deadline),
                    "state": "pending",
                }
            delivered = quantize_volume(Decimal(segment["delivered_mwh"]) + record.actual_mwh)
            self.connection.execute(
                "UPDATE plan_segments SET delivered_mwh=?,state='delivering' WHERE segment_id=?",
                (decimal_text(delivered), segment["segment_id"]),
            )
            response = {
                "plan_id": plan_id,
                "period": record.period,
                "consumed_mwh": decimal_text(quantize_volume(consume_total)),
                "delivered_mwh": decimal_text(delivered),
                "review": review_info,
            }
            self._store_idempotent("delivery", record.idempotency_key, raw, response)
            self._audit(
                "plan",
                plan_id,
                "delivery.recorded",
                actor_id,
                {"period": record.period, "actual_mwh": decimal_text(record.actual_mwh), "review": review_info},
            )
        return response

    def _terminate_plan(
        self, actor_id: str, plan_id: str, expected_revision: int, reason: str, target_state: str
    ) -> dict[str, Any]:
        """取消或执行失败：只退回尚未形成实际电量的预占部分，已核销部分保持不变。"""
        user = self._require(actor_id, "plan.write")
        plan = self._plan(plan_id)
        self._check_own(user, plan["account_id"])
        if plan["state"] != "confirmed" or plan["revision"] != expected_revision:
            raise InvalidState("计划不是当前已确认版本")
        if not reason.strip():
            raise ValidationFailed("终止原因不能为空")
        pending = self.connection.execute(
            "SELECT COUNT(*) AS c FROM exception_reviews WHERE plan_id=? AND state='pending'", (plan_id,)
        ).fetchone()["c"]
        if pending:
            raise Conflict("存在待复核的超额，不能终止计划")
        current_period = period_of(self._today())
        released = ZERO
        with transaction(self.connection, immediate=True):
            self._bookable_period(current_period)
            segments = self.connection.execute(
                "SELECT * FROM plan_segments WHERE plan_id=? AND state<>'closed' ORDER BY segment_id", (plan_id,)
            ).fetchall()
            for segment in segments:
                for grant_id, amount in sorted(self._segment_hold_remaining(segment["segment_id"]).items()):
                    self._book_entry(
                        account_id=plan["account_id"],
                        grant_id=grant_id,
                        plan_id=plan_id,
                        segment_id=segment["segment_id"],
                        action="RELEASE",
                        amount=amount,
                        period=current_period,
                        actor_id=actor_id,
                        note=reason.strip(),
                    )
                    released = quantize_volume(released + amount)
                self.connection.execute(
                    "UPDATE plan_segments SET state='closed' WHERE segment_id=?", (segment["segment_id"],)
                )
            self.connection.execute(
                "UPDATE channel_reservations SET state='released' WHERE plan_id=? AND state='held'", (plan_id,)
            )
            cursor = self.connection.execute(
                "UPDATE delivery_plans SET state=?,revision=revision+1 WHERE plan_id=? AND state='confirmed' AND revision=?",
                (target_state, plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划不是当前已确认版本")
            self._audit(
                "plan",
                plan_id,
                f"plan.{target_state}",
                actor_id,
                {"released_mwh": decimal_text(released), "reason": reason.strip()},
            )
        return {"plan_id": plan_id, "state": target_state, "released_mwh": decimal_text(released)}

    def cancel_plan(self, actor_id: str, plan_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        return self._terminate_plan(actor_id, plan_id, expected_revision, reason, "cancelled")

    def fail_plan(self, actor_id: str, plan_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        return self._terminate_plan(actor_id, plan_id, expected_revision, reason, "failed")

    # ---------- 超额限时复核 ----------

    def _expire_review(self, review: sqlite3.Row, actor_id: str, note: str) -> None:
        current_period = period_of(self._today())
        self.connection.execute(
            "UPDATE exception_reviews SET state='expired',decided_at=? WHERE review_id=? AND state='pending'",
            (self._now(), review["review_id"]),
        )
        self._book_entry(
            account_id=review["account_id"],
            plan_id=review["plan_id"],
            segment_id=review["segment_id"],
            review_id=review["review_id"],
            action="OVERUSE_FLAG",
            amount=Decimal(review["over_mwh"]),
            period=current_period,
            actor_id=actor_id,
            note=note,
        )
        self._audit(
            "review", str(review["review_id"]), "review.expired", actor_id, {"over_mwh": review["over_mwh"]}
        )

    def decide_review(self, actor_id: str, review_id: int, approve: bool, note: str = "") -> dict[str, Any]:
        """复核超额：提交人不能审批自己的例外，超过时限自动作废。"""
        self._require(actor_id, "review.decide")
        review = self.connection.execute(
            "SELECT * FROM exception_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if review is None:
            raise NotFound("复核单不存在")
        if review["state"] != "pending":
            raise InvalidState("复核已处理")
        if actor_id == review["submitted_by"]:
            raise Forbidden("提交人不能审批自己的例外")
        if parse_utc(review["deadline_at"], "deadline_at") < self._now_dt():
            with transaction(self.connection, immediate=True):
                self._expire_review(review, actor_id, "复核超时自动作废")
            raise InvalidState("复核已超时，超额部分已标记")
        current_period = period_of(self._today())
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exception_reviews SET state=?,decided_by=?,decided_at=?,note=? "
                "WHERE review_id=? AND state='pending'",
                ("approved" if approve else "rejected", actor_id, self._now(), note.strip(), review_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("复核已处理")
            if approve:
                balances = self._balances(review["account_id"])
                today_text = self._today().isoformat()
                candidates = [
                    balance
                    for balance in balances.values()
                    if balance.available > ZERO and balance.valid_to >= today_text
                ]
                try:
                    picks = select_fefo(candidates, Decimal(review["over_mwh"]))
                except Shortfall as exc:
                    raise Conflict("账户可用额度不足以核销超额") from exc
                for grant_id, amount in picks:
                    self._book_entry(
                        account_id=review["account_id"],
                        grant_id=grant_id,
                        plan_id=review["plan_id"],
                        segment_id=review["segment_id"],
                        review_id=review_id,
                        action="CONSUME_OVER",
                        amount=amount,
                        period=current_period,
                        actor_id=actor_id,
                        note=note.strip(),
                    )
            else:
                self._book_entry(
                    account_id=review["account_id"],
                    plan_id=review["plan_id"],
                    segment_id=review["segment_id"],
                    review_id=review_id,
                    action="OVERUSE_FLAG",
                    amount=Decimal(review["over_mwh"]),
                    period=current_period,
                    actor_id=actor_id,
                    note=note.strip() or "复核驳回",
                )
            self._audit(
                "review",
                str(review_id),
                "review.approved" if approve else "review.rejected",
                actor_id,
                {"over_mwh": review["over_mwh"], "note": note.strip()},
            )
        return {
            "review_id": review_id,
            "state": "approved" if approve else "rejected",
            "decided_by": actor_id,
            "decided_at": self._now(),
        }

    def sweep_expirations(self, actor_id: str) -> dict[str, Any]:
        """按当前周期核算规则作废到期未用额度，并把超时复核标记为已过期。"""
        self._require(actor_id, "maintenance.run")
        today_text = self._today().isoformat()
        current_period = period_of(self._today())
        expired_grants: list[dict[str, Any]] = []
        expired_reviews: list[int] = []
        with transaction(self.connection, immediate=True):
            period_row = self._bookable_period(current_period)
            rule = self.connection.execute(
                "SELECT * FROM accounting_rules WHERE rule_version=?", (period_row["rule_version"],)
            ).fetchone()
            accounts = self.connection.execute("SELECT account_id FROM entitlement_accounts").fetchall()
            for account in accounts:
                balances = self._balances(account["account_id"])
                for balance in balances.values():
                    if balance.valid_to < today_text and balance.available > ZERO:
                        if not rule["expire_unused"]:
                            continue
                        self._book_entry(
                            account_id=account["account_id"],
                            grant_id=balance.grant_id,
                            action="EXPIRE",
                            amount=balance.available,
                            period=current_period,
                            actor_id=actor_id,
                            note="到期未用额度作废",
                        )
                        expired_grants.append(
                            {"grant_id": balance.grant_id, "expired_mwh": decimal_text(balance.available)}
                        )
            pending = self.connection.execute(
                "SELECT * FROM exception_reviews WHERE state='pending' AND deadline_at<?",
                (self._now(),),
            ).fetchall()
            for review in pending:
                self._expire_review(review, actor_id, "复核超时自动作废")
                expired_reviews.append(review["review_id"])
            self._audit(
                "maintenance",
                current_period,
                "maintenance.swept",
                actor_id,
                {"expired_grants": len(expired_grants), "expired_reviews": len(expired_reviews)},
            )
        return {"expired_grants": expired_grants, "expired_reviews": expired_reviews}

    # ---------- 分角色视图与余额溯源 ----------

    def _account_summary(self, account_id: str) -> dict[str, Any]:
        account = self._account(account_id)
        balances = self._balances(account_id)
        totals = {"granted": ZERO, "held": ZERO, "consumed": ZERO, "expired": ZERO, "available": ZERO}
        by_kind: dict[str, dict[str, Decimal]] = {}
        for balance in balances.values():
            bucket = by_kind.setdefault(
                balance.kind, {"granted": ZERO, "held": ZERO, "consumed": ZERO, "expired": ZERO, "available": ZERO}
            )
            for key in totals:
                totals[key] += getattr(balance, key) if key != "available" else balance.available
                bucket[key] += getattr(balance, key) if key != "available" else balance.available
        flagged_rows = self.connection.execute(
            "SELECT amount_mwh FROM ledger_entries WHERE account_id=? AND action='OVERUSE_FLAG'",
            (account_id,),
        ).fetchall()
        flagged = sum((Decimal(row["amount_mwh"]) for row in flagged_rows), ZERO)
        pending_rows = self.connection.execute(
            "SELECT over_mwh FROM exception_reviews WHERE account_id=? AND state='pending'",
            (account_id,),
        ).fetchall()
        pending = sum((Decimal(row["over_mwh"]) for row in pending_rows), ZERO)
        return {
            "account_id": account_id,
            "facility_id": account["facility_id"],
            "batch_id": account["batch_id"],
            "state": account["state"],
            "totals": {f"{key}_mwh": decimal_text(quantize_volume(value)) for key, value in totals.items()},
            "by_kind": {
                kind: {f"{key}_mwh": decimal_text(quantize_volume(value)) for key, value in values.items()}
                for kind, values in sorted(by_kind.items())
            },
            "grants": [balance.as_dict() for balance in balances.values()],
            "rejected_overuse_mwh": decimal_text(quantize_volume(flagged)),
            "pending_review_mwh": decimal_text(quantize_volume(pending)),
        }

    def site_view(self, actor_id: str, facility_id: str) -> dict[str, Any]:
        """场站人员视图：只看得到本场站的分户、计划与待复核事项。"""
        user = self._require(actor_id, "view.own")
        if user["facility_id"] != facility_id:
            raise Forbidden("只能查看本场站视图")
        accounts = self.connection.execute(
            "SELECT account_id FROM entitlement_accounts WHERE facility_id=? ORDER BY account_id", (facility_id,)
        ).fetchall()
        plans = self.connection.execute(
            "SELECT p.* FROM delivery_plans p JOIN entitlement_accounts a ON a.account_id=p.account_id "
            "WHERE a.facility_id=? ORDER BY p.plan_id",
            (facility_id,),
        ).fetchall()
        plan_rows = []
        for plan in plans:
            segments = self.connection.execute(
                "SELECT period,amount_mwh,delivered_mwh,state FROM plan_segments WHERE plan_id=? ORDER BY period",
                (plan["plan_id"],),
            ).fetchall()
            plan_rows.append(
                {
                    "plan_id": plan["plan_id"],
                    "account_id": plan["account_id"],
                    "route_id": plan["route_id"],
                    "state": plan["state"],
                    "window_start": plan["window_start"],
                    "window_end": plan["window_end"],
                    "total_mwh": plan["total_mwh"],
                    "segments": [dict(row) for row in segments],
                }
            )
        reviews = self.connection.execute(
            "SELECT r.review_id,r.account_id,r.plan_id,r.over_mwh,r.deadline_at FROM exception_reviews r "
            "JOIN entitlement_accounts a ON a.account_id=r.account_id "
            "WHERE a.facility_id=? AND r.state='pending' ORDER BY r.review_id",
            (facility_id,),
        ).fetchall()
        return {
            "facility_id": facility_id,
            "accounts": [self._account_summary(row["account_id"]) for row in accounts],
            "plans": plan_rows,
            "pending_reviews": [dict(row) for row in reviews],
        }

    def business_view(self, actor_id: str, period: str | None = None) -> dict[str, Any]:
        """经营人员视图：跨场站汇总、通道占用和待办复核。"""
        self._require(actor_id, "summary.read")
        accounts = self.connection.execute(
            "SELECT account_id FROM entitlement_accounts ORDER BY account_id"
        ).fetchall()
        summaries = [self._account_summary(row["account_id"]) for row in accounts]
        by_kind: dict[str, dict[str, Decimal]] = {}
        for summary in summaries:
            for kind, values in summary["by_kind"].items():
                bucket = by_kind.setdefault(
                    kind, {"granted": ZERO, "held": ZERO, "consumed": ZERO, "expired": ZERO, "available": ZERO}
                )
                for key in bucket:
                    bucket[key] += Decimal(values[f"{key}_mwh"])
        channels = self.connection.execute("SELECT * FROM ledger_channels ORDER BY route_id").fetchall()
        channel_rows = []
        for channel in channels:
            reservations = self.connection.execute(
                "SELECT period,amount_mwh FROM channel_reservations WHERE route_id=? AND state='held' "
                "ORDER BY period",
                (channel["route_id"],),
            ).fetchall()
            used_by_period: dict[str, Decimal] = {}
            for row in reservations:
                used_by_period[row["period"]] = used_by_period.get(row["period"], ZERO) + Decimal(row["amount_mwh"])
            capacity = Decimal(channel["period_capacity_mwh"])
            channel_rows.append(
                {
                    "route_id": channel["route_id"],
                    "period_capacity_mwh": channel["period_capacity_mwh"],
                    "periods": [
                        {
                            "period": period_name,
                            "held_mwh": decimal_text(quantize_volume(used)),
                            "remaining_mwh": decimal_text(quantize_volume(capacity - used)),
                        }
                        for period_name, used in sorted(used_by_period.items())
                    ],
                }
            )
        reviews = self.connection.execute(
            "SELECT review_id,account_id,plan_id,over_mwh,deadline_at,submitted_by,submitted_at "
            "FROM exception_reviews WHERE state='pending' ORDER BY deadline_at,review_id"
        ).fetchall()
        periods = self.connection.execute(
            "SELECT period,rule_version,state FROM settlement_periods ORDER BY period"
        ).fetchall()
        result: dict[str, Any] = {
            "generated_at": self._now(),
            "by_kind": {
                kind: {f"{key}_mwh": decimal_text(quantize_volume(value)) for key, value in values.items()}
                for kind, values in sorted(by_kind.items())
            },
            "accounts": [
                {key: summary[key] for key in ("account_id", "facility_id", "batch_id", "totals", "pending_review_mwh")}
                for summary in summaries
            ],
            "channels": channel_rows,
            "pending_reviews": [dict(row) for row in reviews],
            "periods": [dict(row) for row in periods],
        }
        if period is not None:
            entries = self.connection.execute(
                "SELECT action,amount_mwh FROM ledger_entries WHERE period=?", (period,)
            ).fetchall()
            booked: dict[str, Decimal] = {}
            for row in entries:
                booked[row["action"]] = booked.get(row["action"], ZERO) + Decimal(row["amount_mwh"])
            result["period_filter"] = period
            result["period_booked_mwh"] = {
                action: decimal_text(quantize_volume(amount)) for action, amount in sorted(booked.items())
            }
        return result

    def trace_account(self, actor_id: str, account_id: str, grant_id: str | None = None) -> dict[str, Any]:
        """从任一余额追到形成它的业务动作：逐笔流水附带滚动余额。"""
        user = self._require(actor_id, "trace.read")
        account = self._account(account_id)
        if user["role"] == "site" and user["facility_id"] != account["facility_id"]:
            raise Forbidden("只能追溯本场站账户")
        if grant_id is None:
            rows = self.connection.execute(
                "SELECT * FROM ledger_entries WHERE account_id=? ORDER BY entry_id", (account_id,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM ledger_entries WHERE account_id=? AND grant_id=? ORDER BY entry_id",
                (account_id, grant_id),
            ).fetchall()
        running = {"granted": ZERO, "held": ZERO, "consumed": ZERO, "expired": ZERO}
        entries = []
        for row in rows:
            amount = Decimal(row["amount_mwh"])
            action = row["action"]
            if action == "GRANT":
                running["granted"] += amount
            elif action == "HOLD":
                running["held"] += amount
            elif action == "RELEASE":
                running["held"] -= amount
            elif action == "CONSUME":
                running["held"] -= amount
                running["consumed"] += amount
            elif action == "CONSUME_OVER":
                running["consumed"] += amount
            elif action == "EXPIRE":
                running["expired"] += amount
            available = running["granted"] - running["held"] - running["consumed"] - running["expired"]
            entries.append(
                {
                    "entry_id": row["entry_id"],
                    "action": action,
                    "amount_mwh": row["amount_mwh"],
                    "period": row["period"],
                    "rule_version": row["rule_version"],
                    "actor_id": row["actor_id"],
                    "grant_id": row["grant_id"],
                    "plan_id": row["plan_id"],
                    "segment_id": row["segment_id"],
                    "review_id": row["review_id"],
                    "note": row["note"],
                    "created_at": row["created_at"],
                    "running": {
                        **{f"{key}_mwh": decimal_text(quantize_volume(value)) for key, value in running.items()},
                        "available_mwh": decimal_text(quantize_volume(available)),
                    },
                }
            )
        return {
            "account_id": account_id,
            "facility_id": account["facility_id"],
            "batch_id": account["batch_id"],
            "grant_id": grant_id,
            "summary": self._account_summary(account_id)["totals"],
            "entries": entries,
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM ledger_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
