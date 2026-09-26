"""可用率权益账本：额度授予、跨年计划、预占/核销/返还与限时超额复核。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .ledger import (
    GrantAvailability,
    PeriodWindow,
    daily_slices,
    select_first_expiring,
    split_by_period,
    split_quantities,
)
from .models import DeliveryPlanDraft, EntitlementGrant
from .planning import (
    canonical_json,
    decimal_text,
    digest,
    effective_capacity,
    quantize_volume,
)
from .service import SupplyService
from .storage import transaction


REVIEW_SLA = timedelta(hours=24)
ZERO = Decimal("0")

# 流水动作：GRANT 入账 / HOLD 预占 / WRITE_OFF 核销 / RETURN 返还 / RELEASE 解除 / EXPIRE 到期
ACTION_LABELS = {
    "GRANT": "额度入账",
    "HOLD": "预占",
    "WRITE_OFF": "核销",
    "RETURN": "返还",
    "RELEASE": "解除预占",
    "EXPIRE": "到期失效",
}


class EntitlementService(SupplyService):
    """在供应服务同一 SQLite 库上提供可用率权益分户账本。"""

    # ---- 基础工具 ----------------------------------------------------------

    def _entry(
        self,
        entitlement: sqlite3.Row,
        action: str,
        amount: Decimal,
        *,
        plan_id: str | None,
        segment_key: str | None,
        period: str | None,
        actor_id: str,
        reason_code: str = "",
        source_ref: str = "",
    ) -> None:
        self.connection.execute(
            "INSERT INTO entitlement_entries(facility_id,product,entitlement_id,action,amount_mwh,"
            "plan_id,segment_key,period_key,reason_code,source_ref,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entitlement["facility_id"],
                entitlement["product"],
                entitlement["entitlement_id"],
                action,
                decimal_text(amount),
                plan_id,
                segment_key,
                period,
                reason_code,
                source_ref,
                actor_id,
                self._now(),
            ),
        )

    def _available(self, grant: sqlite3.Row) -> Decimal:
        return quantize_volume(
            Decimal(grant["quantity_mwh"])
            - Decimal(grant["held_mwh"])
            - Decimal(grant["consumed_mwh"])
            - Decimal(grant["expired_mwh"])
        )

    def _adjust_grant(
        self,
        entitlement_id: str,
        *,
        held: Decimal = ZERO,
        consumed: Decimal = ZERO,
        expired: Decimal = ZERO,
    ) -> None:
        """按 Decimal 增量更新分户余额，全部金额以文本存储避免浮点误差。"""
        row = self.connection.execute(
            "SELECT held_mwh,consumed_mwh,expired_mwh FROM entitlements WHERE entitlement_id=?",
            (entitlement_id,),
        ).fetchone()
        if row is None:
            raise NotFound("权益额度不存在")
        self.connection.execute(
            "UPDATE entitlements SET held_mwh=?,consumed_mwh=?,expired_mwh=?,revision=revision+1 "
            "WHERE entitlement_id=?",
            (
                decimal_text(quantize_volume(Decimal(row["held_mwh"]) + held)),
                decimal_text(quantize_volume(Decimal(row["consumed_mwh"]) + consumed)),
                decimal_text(quantize_volume(Decimal(row["expired_mwh"]) + expired)),
                entitlement_id,
            ),
        )

    def _expire_locked(self, actor_id: str) -> None:
        """到期处理。调用方必须已经持有写事务。"""
        now_text = self._now()
        grants = self.connection.execute(
            "SELECT * FROM entitlements WHERE state='active' AND expires_at<=?",
            (now_text,),
        ).fetchall()
        affected_plans: set[str] = set()
        for grant in grants:
            remaining = self._available(grant)
            if remaining > ZERO:
                self._adjust_grant(grant["entitlement_id"], expired=remaining)
                self._entry(grant, "EXPIRE", remaining, plan_id=None, segment_key=None,
                            period=None, actor_id=actor_id)
            self.connection.execute(
                "UPDATE entitlements SET state='expired' WHERE entitlement_id=?",
                (grant["entitlement_id"],),
            )
            if remaining > ZERO:
                self._audit("entitlement", grant["entitlement_id"], "entitlement.expired", actor_id,
                            {"remaining_mwh": decimal_text(remaining)})
        for review in self.connection.execute(
            "SELECT * FROM overage_reviews WHERE state='pending' AND expires_at<?",
            (now_text,),
        ).fetchall():
            self._resolve_review(review, "expired", actor_id, "复核超时未决，预占自动解除")
            affected_plans.add(review["plan_id"])
        for plan_id in affected_plans:
            plan = self.connection.execute(
                "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            self._requeue_or_keep(plan)

    def _apply_expiry(self, actor_id: str) -> None:
        """读路径使用：单独开启立即事务执行到期处理。"""
        with transaction(self.connection, immediate=True):
            self._expire_locked(actor_id)

    def _period_open(self, period: str) -> None:
        row = self.connection.execute(
            "SELECT state FROM settlement_periods WHERE period_key=?", (period,)
        ).fetchone()
        if row is not None and row["state"] == "closed":
            raise InvalidState(f"结算周期 {period} 已关闭，新核算规则只作用于未结算周期")

    def _route_capacity_for_date(self, route_id: str, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        nominal_row = self.connection.execute(
            "SELECT daily_capacity FROM routes WHERE route_id=?", (route_id,)
        ).fetchone()
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages "
            "WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route_id, end, start),
        ).fetchall()
        return effective_capacity(
            Decimal(nominal_row["daily_capacity"]),
            [Decimal(r["capacity_percent"]) for r in rows],
        )

    def _reserve_channel(
        self, route_id: str, plan_id: str, segment_key: str, window: PeriodWindow, amount: Decimal
    ) -> None:
        """通道按日预留：已被其它计划预占的额度不会再次出现在可选余额里。"""
        if amount <= ZERO:
            return
        for service_date, slice_mwh in daily_slices(window, amount):
            reserved = Decimal(str(self.connection.execute(
                "SELECT COALESCE(SUM(CAST(reserved_mwh AS REAL)),0) AS total FROM route_reservations "
                "WHERE route_id=? AND service_date=? AND state IN ('held','consumed')",
                (route_id, service_date),
            ).fetchone()["total"]))
            capacity = self._route_capacity_for_date(route_id, service_date)
            if capacity - reserved < slice_mwh:
                raise Conflict(f"通道 {route_id} 在 {service_date} 容量不足，无法完成预留")
            self.connection.execute(
                "INSERT INTO route_reservations(route_id,service_date,plan_id,segment_key,"
                "reserved_mwh,created_at) VALUES(?,?,?,?,?,?)",
                (route_id, service_date, plan_id, segment_key, decimal_text(slice_mwh), self._now()),
            )

    def _segments(self, plan_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM delivery_plan_segments WHERE plan_id=? ORDER BY period_key", (plan_id,)
        ).fetchall())

    # ---- 额度授予 ----------------------------------------------------------

    def grant_entitlement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "entitlement.grant")
        grant = EntitlementGrant.from_dict(raw)
        try:
            starts = parse_utc(grant.applicable_from, "applicable_from")
            ends = parse_utc(grant.applicable_to, "applicable_to")
            expires = parse_utc(grant.expires_at, "expires_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if ends < starts:
            raise ValidationFailed("applicable_to 不能早于 applicable_from")
        if expires < ends:
            raise ValidationFailed("expires_at 不能早于适用时段结束时间")
        periods = {w.period_key for w in split_by_period(starts, ends)}
        try:
            with transaction(self.connection, immediate=True):
                for period in periods:
                    self._period_open(period)
                if self.connection.execute(
                    "SELECT facility_id FROM facilities WHERE facility_id=?", (grant.facility_id,)
                ).fetchone() is None:
                    raise NotFound("场站不存在")
                self.connection.execute(
                    "INSERT INTO entitlements(entitlement_id,facility_id,product,grant_type,quantity_mwh,"
                    "applicable_from,applicable_to,expires_at,source_ref,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant.entitlement_id, grant.facility_id, grant.product, grant.grant_type,
                        decimal_text(grant.quantity_mwh), utc_text(starts), utc_text(ends), utc_text(expires),
                        grant.source_ref, grant.note, actor_id, self._now(),
                    ),
                )
                row = self.connection.execute(
                    "SELECT * FROM entitlements WHERE entitlement_id=?", (grant.entitlement_id,)
                ).fetchone()
                self._entry(row, "GRANT", grant.quantity_mwh, plan_id=None, segment_key=None,
                            period=None, actor_id=actor_id, source_ref=grant.source_ref)
                self._audit("entitlement", grant.entitlement_id, "entitlement.granted", actor_id, {
                    "grant_type": grant.grant_type,
                    "quantity_mwh": decimal_text(grant.quantity_mwh),
                    "facility_id": grant.facility_id,
                    "product": grant.product,
                    "source_ref": grant.source_ref,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("权益额度编号冲突") from exc
        return self.entitlement_detail(actor_id, grant.entitlement_id)

    # ---- 计划草案与确认 ------------------------------------------------------

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        draft = DeliveryPlanDraft.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='delivery_plan' AND idempotency_key=?",
            (draft.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同计划内容")
            return json.loads(stored["response_json"])
        try:
            starts = parse_utc(draft.starts_at, "starts_at")
            ends = parse_utc(draft.ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if ends <= starts:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (draft.route_id,)).fetchone()
        if route is None:
            raise NotFound("送出通道不存在")
        if route["state"] != "active":
            raise InvalidState("送出通道当前不可安排计划")
        windows = split_by_period(starts, ends)
        amounts = split_quantities(draft.quantity_mwh, windows)
        response: dict[str, Any] = {"plan_id": draft.plan_id, "state": "draft", "segments": []}
        try:
            with transaction(self.connection, immediate=True):
                for window in windows:
                    self._period_open(window.period_key)
                self.connection.execute(
                    "INSERT INTO delivery_plans(plan_id,route_id,facility_id,product,starts_at,ends_at,"
                    "quantity_mwh,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        draft.plan_id, draft.route_id, route["origin_id"], route["product"],
                        utc_text(starts), utc_text(ends), decimal_text(draft.quantity_mwh),
                        draft.idempotency_key, actor_id, self._now(),
                    ),
                )
                for window, amount in zip(windows, amounts, strict=True):
                    cursor = self.connection.execute(
                        "INSERT INTO delivery_plan_segments(plan_id,period_key,starts_at,ends_at,quantity_mwh) "
                        "VALUES(?,?,?,?,?)",
                        (draft.plan_id, window.period_key, utc_text(window.starts_at),
                         utc_text(window.ends_at), decimal_text(amount)),
                    )
                    response["segments"].append({
                        "segment_id": int(cursor.lastrowid),
                        "period_key": window.period_key,
                        "quantity_mwh": decimal_text(amount),
                        "state": "proposed",
                    })
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('delivery_plan',?,?,?,?)",
                    (draft.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("delivery_plan", draft.plan_id, "plan.created", actor_id, {
                    "route_id": draft.route_id,
                    "periods": [w.period_key for w in windows],
                    "quantity_mwh": decimal_text(draft.quantity_mwh),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出计划编号或幂等键冲突") from exc
        return response

    def _candidate_grants(
        self, facility_id: str, product: str, starts_at: str, ends_at: str
    ) -> list[GrantAvailability]:
        rows = self.connection.execute(
            "SELECT * FROM entitlements WHERE state='active' AND facility_id=? AND product=? "
            "AND applicable_from<=? AND applicable_to>=? ORDER BY expires_at,entitlement_id",
            (facility_id, product, ends_at, starts_at),
        ).fetchall()
        return [
            GrantAvailability(row["entitlement_id"], self._available(row), row["expires_at"])
            for row in rows
        ]

    def _hold_segment(
        self,
        plan: sqlite3.Row,
        segment: sqlite3.Row,
        amount: Decimal,
        *,
        allow_shortfall: bool,
        actor_id: str,
    ) -> Decimal:
        """对单个周期段预占额度并预留通道；返回未能覆盖的缺口。"""
        segment_key = f"{plan['plan_id']}:{segment['period_key']}"
        window = PeriodWindow(
            segment["period_key"],
            parse_utc(segment["starts_at"]),
            parse_utc(segment["ends_at"]),
        )
        candidates = self._candidate_grants(
            plan["facility_id"], plan["product"], segment["starts_at"], segment["ends_at"]
        )
        try:
            selections = select_first_expiring(candidates, amount)
            shortfall = ZERO
        except ValueError as exc:
            if not allow_shortfall:
                raise Conflict(
                    f"周期 {segment['period_key']} 可用权益额度不足：{exc}，"
                    "请调整计划或提交限时复核"
                )
            selections = [(item.entitlement_id, item.available_mwh)
                          for item in candidates if item.available_mwh > ZERO]
            covered = quantize_volume(sum((qty for _, qty in selections), ZERO))
            shortfall = quantize_volume(amount - covered)
        for entitlement_id, held_amount in selections:
            if held_amount <= ZERO:
                continue
            self._adjust_grant(entitlement_id, held=held_amount)
            self.connection.execute(
                "INSERT INTO entitlement_holds(entitlement_id,plan_id,segment_key,amount_mwh,created_at) "
                "VALUES(?,?,?,?,?)",
                (entitlement_id, plan["plan_id"], segment_key, decimal_text(held_amount), self._now()),
            )
            grant = self.connection.execute(
                "SELECT * FROM entitlements WHERE entitlement_id=?", (entitlement_id,)
            ).fetchone()
            self._entry(grant, "HOLD", held_amount, plan_id=plan["plan_id"], segment_key=segment_key,
                        period=segment["period_key"], actor_id=actor_id)
        covered = quantize_volume(amount - shortfall)
        self._reserve_channel(plan["route_id"], plan["plan_id"], segment_key, window, covered)
        if covered > ZERO:
            self.connection.execute(
                "UPDATE delivery_plan_segments SET held_mwh=?,state='held',revision=revision+1 "
                "WHERE segment_id=?",
                (decimal_text(Decimal(segment["held_mwh"]) + covered), segment["segment_id"]),
            )
        return shortfall

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        with transaction(self.connection, immediate=True):
            self._expire_locked(actor_id)
            plan = self.connection.execute(
                "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound("送出计划不存在")
            if plan["state"] != "draft" or plan["revision"] != expected_revision:
                raise InvalidState("计划不是可确认的草案版本")
            for segment in self._segments(plan_id):
                self._period_open(segment["period_key"])
                need = quantize_volume(Decimal(segment["quantity_mwh"]) - Decimal(segment["held_mwh"]))
                if need > ZERO:
                    self._hold_segment(
                        plan, segment, need,
                        allow_shortfall=False, actor_id=actor_id,
                    )
            self.connection.execute(
                "UPDATE delivery_plans SET state='confirmed',revision=revision+1,confirmed_at=? "
                "WHERE plan_id=?",
                (self._now(), plan_id),
            )
            self._audit("delivery_plan", plan_id, "plan.confirmed", actor_id,
                        {"segments": len(self._segments(plan_id))})
        return self.plan_detail(actor_id, plan_id)

    # ---- 限时超额复核 -------------------------------------------------------

    def submit_overage_review(self, actor_id: str, plan_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        if not reason or not reason.strip():
            raise ValidationFailed("超额原因不能为空")
        with transaction(self.connection, immediate=True):
            self._expire_locked(actor_id)
            plan = self.connection.execute(
                "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound("送出计划不存在")
            if plan["state"] != "draft":
                raise InvalidState("只有草案计划可以提交超额复核")
            created: list[dict[str, Any]] = []
            for segment in self._segments(plan_id):
                self._period_open(segment["period_key"])
                need = quantize_volume(Decimal(segment["quantity_mwh"]) - Decimal(segment["held_mwh"]))
                if need <= ZERO:
                    continue
                gap = self._hold_segment(
                    plan, segment, need,
                    allow_shortfall=True, actor_id=actor_id,
                )
                if gap <= ZERO:
                    continue
                if self.connection.execute(
                    "SELECT review_id FROM overage_reviews WHERE plan_id=? AND period_key=? AND state='pending'",
                    (plan_id, segment["period_key"]),
                ).fetchone() is not None:
                    raise Conflict(f"周期 {segment['period_key']} 已存在待审超额申请")
                now = self.clock.now()
                cursor = self.connection.execute(
                    "INSERT INTO overage_reviews(plan_id,period_key,shortfall_mwh,reason,submitted_by,"
                    "submitted_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        plan_id, segment["period_key"], decimal_text(gap), reason.strip()[:512],
                        actor_id, utc_text(now), utc_text(now + REVIEW_SLA),
                    ),
                )
                review_id = int(cursor.lastrowid)
                self.connection.execute(
                    "UPDATE delivery_plan_segments SET overage_mwh=? WHERE segment_id=?",
                    (decimal_text(gap), segment["segment_id"]),
                )
                created.append({"review_id": review_id, "period_key": segment["period_key"],
                                "shortfall_mwh": decimal_text(gap)})
            if not created:
                raise InvalidState("额度与通道均充足，应直接确认计划")
            self.connection.execute(
                "UPDATE delivery_plans SET state='pending_review',revision=revision+1 WHERE plan_id=?",
                (plan_id,),
            )
            self._audit("delivery_plan", plan_id, "overage.submitted", actor_id, {"reviews": created})
        return self.plan_detail(actor_id, plan_id)

    def _resolve_review(
        self, review: sqlite3.Row, decision: str, actor_id: str, note: str
    ) -> None:
        """复核驳回/超时：解除该周期已预占部分（核销掉的实际电量不退），周期段退回提议。"""
        plan_id = review["plan_id"]
        period = review["period_key"]
        segment_key = f"{plan_id}:{period}"
        for hold in self.connection.execute(
            "SELECT * FROM entitlement_holds WHERE plan_id=? AND segment_key=? AND state='held'",
            (plan_id, segment_key),
        ).fetchall():
            remaining = quantize_volume(Decimal(hold["amount_mwh"]) - Decimal(hold["consumed_mwh"]))
            if remaining <= ZERO:
                continue
            self._adjust_grant(hold["entitlement_id"], held=-remaining)
            self.connection.execute(
                "UPDATE entitlement_holds SET state='released' WHERE hold_id=?", (hold["hold_id"],)
            )
            grant = self.connection.execute(
                "SELECT * FROM entitlements WHERE entitlement_id=?", (hold["entitlement_id"],)
            ).fetchone()
            self._entry(grant, "RELEASE", remaining, plan_id=plan_id, segment_key=segment_key,
                        period=period, actor_id=actor_id, reason_code="OVERAGE_REJECTED")
        self.connection.execute(
            "UPDATE route_reservations SET state='released' WHERE plan_id=? AND segment_key=? "
            "AND state='held'",
            (plan_id, segment_key),
        )
        self.connection.execute(
            "UPDATE delivery_plan_segments SET held_mwh='0',overage_mwh='0',state='proposed',"
            "revision=revision+1 WHERE plan_id=? AND period_key=?",
            (plan_id, period),
        )
        self.connection.execute(
            "UPDATE overage_reviews SET state=?,reviewed_by=?,reviewed_at=?,decision_note=? WHERE review_id=?",
            (decision, actor_id, self._now(), note[:512], review["review_id"]),
        )

    def _requeue_or_keep(self, plan: sqlite3.Row) -> None:
        """全部周期覆盖则确认；仍有待审保持待审；否则退回草案修改。"""
        pending = self.connection.execute(
            "SELECT COUNT(*) AS c FROM overage_reviews WHERE plan_id=? AND state='pending'",
            (plan["plan_id"],),
        ).fetchone()["c"]
        if pending:
            state = "pending_review"
        else:
            uncovered = self.connection.execute(
                "SELECT COUNT(*) AS c FROM delivery_plan_segments WHERE plan_id=? "
                "AND CAST(held_mwh AS REAL) < CAST(quantity_mwh AS REAL)",
                (plan["plan_id"],),
            ).fetchone()["c"]
            state = "draft" if uncovered else "confirmed"
        if plan["state"] != state:
            confirmed_at = self._now() if state == "confirmed" else None
            self.connection.execute(
                "UPDATE delivery_plans SET state=?,revision=revision+1,confirmed_at=? WHERE plan_id=?",
                (state, confirmed_at, plan["plan_id"]),
            )

    def decide_overage_review(
        self, actor_id: str, review_id: int, approve: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "overage.review")
        with transaction(self.connection, immediate=True):
            self._expire_locked(actor_id)
            review = self.connection.execute(
                "SELECT * FROM overage_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            if review is None:
                raise NotFound("超额复核不存在")
            if review["state"] != "pending":
                raise InvalidState("该超额复核已作出决定或已过期")
            if self.clock.now() > parse_utc(review["expires_at"]):
                raise InvalidState("复核已超过限时")
            # 职责分离：提交人不能审批自己的例外。
            if review["submitted_by"] == actor_id:
                raise Forbidden("提交人不能审批自己的超额例外")
            plan = self.connection.execute(
                "SELECT * FROM delivery_plans WHERE plan_id=?", (review["plan_id"],)
            ).fetchone()
            if not approve:
                self._resolve_review(review, "rejected", actor_id, note or "驳回超额申请")
                self._audit("delivery_plan", plan["plan_id"], "overage.rejected", actor_id,
                            {"review_id": review_id})
                self._requeue_or_keep(plan)
                return self.plan_detail(actor_id, plan["plan_id"])
            segment = self.connection.execute(
                "SELECT * FROM delivery_plan_segments WHERE plan_id=? AND period_key=?",
                (plan["plan_id"], review["period_key"]),
            ).fetchone()
            shortfall = Decimal(review["shortfall_mwh"])
            now_text = self._now()
            # 批准即形成一笔可追溯的例外额度，适用时段限定在该周期段，到期与段末对齐。
            exception_id = f"OVR-{review_id}"
            self.connection.execute(
                "INSERT INTO entitlements(entitlement_id,facility_id,product,grant_type,quantity_mwh,"
                "applicable_from,applicable_to,expires_at,source_ref,note,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    exception_id, plan["facility_id"], plan["product"], "OVERAGE_EXCEPTION",
                    decimal_text(shortfall), segment["starts_at"], segment["ends_at"],
                    segment["ends_at"], f"review-{review_id}",
                    (note or "限时复核批准的超额例外")[:512], actor_id, now_text,
                ),
            )
            grant = self.connection.execute(
                "SELECT * FROM entitlements WHERE entitlement_id=?", (exception_id,)
            ).fetchone()
            self._entry(grant, "GRANT", shortfall, plan_id=plan["plan_id"], segment_key=None,
                        period=review["period_key"], actor_id=actor_id,
                        source_ref=f"review-{review_id}", reason_code="OVERAGE_APPROVED")
            self.connection.execute(
                "UPDATE overage_reviews SET state='approved',reviewed_by=?,reviewed_at=?,decision_note=? "
                "WHERE review_id=?",
                (actor_id, now_text, (note or "")[:512], review_id),
            )
            self._audit("delivery_plan", plan["plan_id"], "overage.approved", actor_id, {
                "review_id": review_id,
                "entitlement_id": exception_id,
                "shortfall_mwh": decimal_text(shortfall),
            })
            # 用例外额度补齐该周期：预占与通道预留仍在同一事务完成。
            segment_key = f"{plan['plan_id']}:{review['period_key']}"
            window = PeriodWindow(
                review["period_key"], parse_utc(segment["starts_at"]), parse_utc(segment["ends_at"])
            )
            self._adjust_grant(exception_id, held=shortfall)
            self.connection.execute(
                "INSERT INTO entitlement_holds(entitlement_id,plan_id,segment_key,amount_mwh,created_at) "
                "VALUES(?,?,?,?,?)",
                (exception_id, plan["plan_id"], segment_key, decimal_text(shortfall), now_text),
            )
            self._entry(grant, "HOLD", shortfall, plan_id=plan["plan_id"], segment_key=segment_key,
                        period=review["period_key"], actor_id=actor_id)
            self._reserve_channel(plan["route_id"], plan["plan_id"], segment_key, window, shortfall)
            self.connection.execute(
                "UPDATE delivery_plan_segments SET held_mwh=?,overage_mwh='0',state='held',"
                "revision=revision+1 WHERE segment_id=?",
                (decimal_text(Decimal(segment["held_mwh"]) + shortfall), segment["segment_id"]),
            )
            self._requeue_or_keep(plan)
        return self.plan_detail(actor_id, plan["plan_id"])

    # ---- 核销、取消与执行失败（只返还未形成实际电量的部分） --------------------

    def record_delivery(
        self, actor_id: str, plan_id: str, period_key: str, actual_mwh: object
    ) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        amount = quantize_volume(Decimal(str(actual_mwh)))
        if amount <= ZERO:
            raise ValidationFailed("实际电量必须为正数")
        with transaction(self.connection, immediate=True):
            plan = self.connection.execute(
                "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound("送出计划不存在")
            if plan["state"] not in ("confirmed", "partially_settled"):
                raise InvalidState("只有已确认计划可以登记实际电量")
            self._period_open(period_key)
            segment = self.connection.execute(
                "SELECT * FROM delivery_plan_segments WHERE plan_id=? AND period_key=?",
                (plan_id, period_key),
            ).fetchone()
            if segment is None:
                raise NotFound("计划中没有该结算周期段")
            if segment["state"] != "held":
                raise InvalidState("该周期段已结算或已返还")
            outstanding = Decimal(segment["held_mwh"])
            if amount > outstanding:
                raise Conflict(
                    f"实际电量 {decimal_text(amount)} 超过剩余预占 {decimal_text(outstanding)}，"
                    "超额使用须先提交限时复核"
                )
            segment_key = f"{plan_id}:{period_key}"
            holds = list(self.connection.execute(
                "SELECT h.*,e.expires_at FROM entitlement_holds h JOIN entitlements e "
                "ON e.entitlement_id=h.entitlement_id WHERE h.plan_id=? AND h.segment_key=? "
                "AND h.state='held' ORDER BY e.expires_at,h.hold_id",
                (plan_id, segment_key),
            ).fetchall())
            to_consume = amount
            for hold in holds:
                if to_consume <= ZERO:
                    break
                available = quantize_volume(Decimal(hold["amount_mwh"]) - Decimal(hold["consumed_mwh"]))
                used = min(available, to_consume)
                new_consumed = Decimal(hold["consumed_mwh"]) + used
                state = "consumed" if new_consumed == Decimal(hold["amount_mwh"]) else "held"
                self.connection.execute(
                    "UPDATE entitlement_holds SET consumed_mwh=?,state=? WHERE hold_id=?",
                    (decimal_text(new_consumed), state, hold["hold_id"]),
                )
                self._adjust_grant(hold["entitlement_id"], held=-used, consumed=used)
                grant = self.connection.execute(
                    "SELECT * FROM entitlements WHERE entitlement_id=?", (hold["entitlement_id"],)
                ).fetchone()
                self._entry(grant, "WRITE_OFF", used, plan_id=plan_id, segment_key=segment_key,
                            period=period_key, actor_id=actor_id, reason_code="DELIVERED")
                to_consume = quantize_volume(to_consume - used)
            new_actual = Decimal(segment["actual_mwh"]) + amount
            new_held = quantize_volume(outstanding - amount)
            seg_state = "settled" if new_held == ZERO else "held"
            if seg_state == "settled":
                self.connection.execute(
                    "UPDATE route_reservations SET state='consumed' WHERE plan_id=? AND segment_key=? "
                    "AND state='held'",
                    (plan_id, segment_key),
                )
            self.connection.execute(
                "UPDATE delivery_plan_segments SET actual_mwh=?,held_mwh=?,state=? WHERE segment_id=?",
                (decimal_text(new_actual), decimal_text(new_held), seg_state, segment["segment_id"]),
            )
            states = {row["state"] for row in self._segments(plan_id)}
            plan_state = "settled" if states == {"settled"} else "partially_settled"
            self.connection.execute(
                "UPDATE delivery_plans SET state=?,revision=revision+1 WHERE plan_id=?",
                (plan_state, plan_id),
            )
            self._audit("delivery_plan", plan_id, "plan.delivered", actor_id, {
                "period_key": period_key, "actual_mwh": decimal_text(amount),
            })
        return self.plan_detail(actor_id, plan_id)

    def _release_unconsumed(self, plan: sqlite3.Row, actor_id: str, reason_code: str) -> None:
        """取消或执行失败：只退回尚未形成实际电量的预占与通道。"""
        for segment in self._segments(plan["plan_id"]):
            segment_key = f"{plan['plan_id']}:{segment['period_key']}"
            released_total = ZERO
            for hold in self.connection.execute(
                "SELECT * FROM entitlement_holds WHERE plan_id=? AND segment_key=? AND state='held'",
                (plan["plan_id"], segment_key),
            ).fetchall():
                remaining = quantize_volume(Decimal(hold["amount_mwh"]) - Decimal(hold["consumed_mwh"]))
                if remaining <= ZERO:
                    continue
                self._adjust_grant(hold["entitlement_id"], held=-remaining)
                self.connection.execute(
                    "UPDATE entitlement_holds SET state='released' WHERE hold_id=?", (hold["hold_id"],)
                )
                grant = self.connection.execute(
                    "SELECT * FROM entitlements WHERE entitlement_id=?", (hold["entitlement_id"],)
                ).fetchone()
                self._entry(grant, "RETURN", remaining, plan_id=plan["plan_id"], segment_key=segment_key,
                            period=segment["period_key"], actor_id=actor_id, reason_code=reason_code)
                released_total += remaining
            delivered = Decimal(segment["actual_mwh"])
            if released_total > ZERO or delivered > ZERO:
                self._split_reservations(plan["plan_id"], segment_key, delivered, released_total)
            if Decimal(segment["actual_mwh"]) > ZERO:
                # 已有实际电量形成核销的周期落账为已结算，其余预占已返还。
                self.connection.execute(
                    "UPDATE delivery_plan_segments SET held_mwh='0',state='settled',revision=revision+1 "
                    "WHERE segment_id=?",
                    (segment["segment_id"],),
                )
            elif segment["state"] in ("held", "proposed"):
                self.connection.execute(
                    "UPDATE delivery_plan_segments SET held_mwh='0',overage_mwh='0',state='returned',"
                    "revision=revision+1 WHERE segment_id=?",
                    (segment["segment_id"],),
                )
        for review in self.connection.execute(
            "SELECT * FROM overage_reviews WHERE plan_id=? AND state='pending'",
            (plan["plan_id"],),
        ).fetchall():
            self.connection.execute(
                "UPDATE overage_reviews SET state='expired',reviewed_by=?,reviewed_at=?,decision_note=? "
                "WHERE review_id=?",
                (actor_id, self._now(), "计划终止，复核自动关闭", review["review_id"]),
            )

    def _split_reservations(
        self, plan_id: str, segment_key: str, delivered: Decimal, released_total: Decimal
    ) -> None:
        """日预留按已核销/退回比例拆成 consumed 与 released，舍入差额并入末行。"""
        rows = list(self.connection.execute(
            "SELECT * FROM route_reservations WHERE plan_id=? AND segment_key=? AND state='held' "
            "ORDER BY reservation_id",
            (plan_id, segment_key),
        ).fetchall())
        original = delivered + released_total
        if not rows or original <= ZERO:
            return
        gross = quantize_volume(sum((Decimal(r["reserved_mwh"]) for r in rows), ZERO))
        keep_total = quantize_volume(gross * delivered / original) if delivered > ZERO else ZERO
        remaining_keep = keep_total
        for index, row in enumerate(rows):
            amount = Decimal(row["reserved_mwh"])
            keep = amount if remaining_keep >= amount else max(ZERO, remaining_keep)
            if index == len(rows) - 1:
                keep = min(amount, max(ZERO, remaining_keep))
            remaining_keep -= keep
            refund = quantize_volume(amount - keep)
            if keep > ZERO:
                self.connection.execute(
                    "UPDATE route_reservations SET reserved_mwh=?,state='consumed' WHERE reservation_id=?",
                    (decimal_text(keep), row["reservation_id"]),
                )
            if refund > ZERO:
                if keep == ZERO:
                    self.connection.execute(
                        "UPDATE route_reservations SET state='released' WHERE reservation_id=?",
                        (row["reservation_id"],),
                    )
                else:
                    self.connection.execute(
                        "UPDATE route_reservations SET reserved_mwh=? WHERE reservation_id=?",
                        (decimal_text(keep), row["reservation_id"]),
                    )
                    self.connection.execute(
                        "INSERT INTO route_reservations(route_id,service_date,plan_id,segment_key,"
                        "reserved_mwh,state,created_at) VALUES(?,?,?,?,?,'released',?)",
                        (row["route_id"], row["service_date"], plan_id, segment_key,
                         decimal_text(refund), self._now()),
                    )

    def cancel_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        with transaction(self.connection, immediate=True):
            plan = self.connection.execute(
                "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound("送出计划不存在")
            if plan["revision"] != expected_revision:
                raise InvalidState("计划版本已变化")
            if plan["state"] not in ("draft", "pending_review", "confirmed", "partially_settled"):
                raise InvalidState("当前计划状态不能取消")
            self._release_unconsumed(plan, actor_id, "PLAN_CANCELLED")
            self.connection.execute(
                "UPDATE delivery_plans SET state='cancelled',revision=revision+1 WHERE plan_id=?",
                (plan_id,),
            )
            self._audit("delivery_plan", plan_id, "plan.cancelled", actor_id, {})
        return self.plan_detail(actor_id, plan_id)

    def fail_plan(self, actor_id: str, plan_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        if not reason or not reason.strip():
            raise ValidationFailed("失败原因不能为空")
        with transaction(self.connection, immediate=True):
            plan = self.connection.execute(
                "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound("送出计划不存在")
            if plan["revision"] != expected_revision:
                raise InvalidState("计划版本已变化")
            if plan["state"] not in ("confirmed", "partially_settled"):
                raise InvalidState("只有已确认计划可以登记执行失败")
            self._release_unconsumed(plan, actor_id, "EXECUTION_FAILED")
            self.connection.execute(
                "UPDATE delivery_plans SET state='failed',revision=revision+1 WHERE plan_id=?",
                (plan_id,),
            )
            self._audit("delivery_plan", plan_id, "plan.failed", actor_id, {"reason": reason.strip()[:512]})
        return self.plan_detail(actor_id, plan_id)

    # ---- 结算周期冻结 -------------------------------------------------------

    def close_period(self, actor_id: str, period_key_text: str) -> dict[str, Any]:
        self._require(actor_id, "period.close")
        with transaction(self.connection, immediate=True):
            self._expire_locked(actor_id)
            pending = self.connection.execute(
                "SELECT COUNT(*) AS c FROM overage_reviews WHERE period_key=? AND state='pending'",
                (period_key_text,),
            ).fetchone()["c"]
            if pending:
                raise InvalidState("仍有限时复核未决定，不能关闭结算周期")
            try:
                self.connection.execute(
                    "INSERT INTO settlement_periods(period_key,state,closed_by,closed_at) "
                    "VALUES(?,'closed',?,?)",
                    (period_key_text, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("结算周期已关闭") from exc
            self._audit("settlement_period", period_key_text, "period.closed", actor_id, {})
        return {"period_key": period_key_text, "state": "closed", "closed_at": self._now()}

    # ---- 可追溯查询与角色视图 ------------------------------------------------

    def _grant_balance(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "entitlement_id": row["entitlement_id"],
            "grant_type": row["grant_type"],
            "facility_id": row["facility_id"],
            "product": row["product"],
            "quantity_mwh": row["quantity_mwh"],
            "held_mwh": row["held_mwh"],
            "consumed_mwh": row["consumed_mwh"],
            "expired_mwh": row["expired_mwh"],
            "available_mwh": decimal_text(self._available(row)),
            "applicable_from": row["applicable_from"],
            "applicable_to": row["applicable_to"],
            "expires_at": row["expires_at"],
            "state": row["state"],
            "source_ref": row["source_ref"],
        }

    def account_summary(self, actor_id: str, facility_id: str, product: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        sql = "SELECT * FROM entitlements WHERE facility_id=?"
        params: list[Any] = [facility_id]
        if product:
            sql += " AND product=?"
            params.append(product)
        sql += " ORDER BY product,grant_type,expires_at,entitlement_id"
        grants = [self._grant_balance(row) for row in self.connection.execute(sql, params).fetchall()]
        totals: dict[str, dict[str, str]] = {}
        for grant in grants:
            bucket = totals.setdefault(grant["product"], {})
            for field in ("quantity_mwh", "held_mwh", "consumed_mwh", "expired_mwh", "available_mwh"):
                bucket[field] = decimal_text(
                    quantize_volume(Decimal(bucket.get(field, "0")) + Decimal(grant[field]))
                )
        return {
            "facility_id": facility_id,
            "product": product,
            "totals_by_product": totals,
            "entitlements": grants,
        }

    def entitlement_detail(self, actor_id: str, entitlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        row = self.connection.execute(
            "SELECT * FROM entitlements WHERE entitlement_id=?", (entitlement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("权益额度不存在")
        entries = [
            {
                "entry_id": item["entry_id"],
                "action": item["action"],
                "action_label": ACTION_LABELS[item["action"]],
                "amount_mwh": item["amount_mwh"],
                "plan_id": item["plan_id"],
                "segment_key": item["segment_key"],
                "period_key": item["period_key"],
                "reason_code": item["reason_code"],
                "source_ref": item["source_ref"],
                "actor_id": item["actor_id"],
                "created_at": item["created_at"],
            }
            for item in self.connection.execute(
                "SELECT * FROM entitlement_entries WHERE entitlement_id=? ORDER BY entry_id",
                (entitlement_id,),
            ).fetchall()
        ]
        audit = [
            {"event_id": item["event_id"], "event_type": item["event_type"],
             "actor_id": item["actor_id"], "created_at": item["created_at"]}
            for item in self.connection.execute(
                "SELECT * FROM supply_audit_events WHERE entity_type='entitlement' AND entity_id=? "
                "ORDER BY event_id",
                (entitlement_id,),
            ).fetchall()
        ]
        return {**self._grant_balance(row), "entries": entries, "audit_events": audit}

    def plan_detail(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        plan = self.connection.execute(
            "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise NotFound("送出计划不存在")
        segments = []
        for segment in self._segments(plan_id):
            segment_key = f"{plan_id}:{segment['period_key']}"
            holds = [dict(item) for item in self.connection.execute(
                "SELECT hold_id,entitlement_id,amount_mwh,consumed_mwh,state FROM entitlement_holds "
                "WHERE plan_id=? AND segment_key=? ORDER BY hold_id",
                (plan_id, segment_key),
            ).fetchall()]
            reservations = [dict(item) for item in self.connection.execute(
                "SELECT service_date,reserved_mwh,state FROM route_reservations WHERE plan_id=? "
                "AND segment_key=? ORDER BY service_date",
                (plan_id, segment_key),
            ).fetchall()]
            segments.append({**dict(segment), "holds": holds, "reservations": reservations})
        reviews = [dict(item) for item in self.connection.execute(
            "SELECT review_id,period_key,shortfall_mwh,reason,state,submitted_by,submitted_at,expires_at,"
            "reviewed_by,reviewed_at,decision_note FROM overage_reviews WHERE plan_id=? ORDER BY review_id",
            (plan_id,),
        ).fetchall()]
        audit = [
            {"event_id": item["event_id"], "event_type": item["event_type"],
             "actor_id": item["actor_id"], "created_at": item["created_at"]}
            for item in self.connection.execute(
                "SELECT * FROM supply_audit_events WHERE entity_type='delivery_plan' AND entity_id=? "
                "ORDER BY event_id",
                (plan_id,),
            ).fetchall()
        ]
        return {**dict(plan), "segments": segments, "overage_reviews": reviews, "audit_events": audit}

    def pending_reviews(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "overage.review")
        self._apply_expiry(actor_id)
        rows = [dict(item) for item in self.connection.execute(
            "SELECT * FROM overage_reviews WHERE state='pending' ORDER BY expires_at,review_id"
        ).fetchall()]
        return {"pending": rows, "count": len(rows)}

    def role_view(self, actor_id: str, facility_id: str | None = None) -> dict[str, Any]:
        """场站、经营、审计三类角色各自的汇总视图。"""
        user = self._user(actor_id)
        role = user["role"]
        self._apply_expiry(actor_id)
        view: dict[str, Any] = {"role": role, "as_of": self._now()}
        if role == "dispatcher":
            self._require(actor_id, "ledger.read")
            if facility_id is None:
                raise ValidationFailed("场站视图需要 facility_id")
            view["account"] = self.account_summary(actor_id, facility_id)
            view["plans"] = [dict(item) for item in self.connection.execute(
                "SELECT plan_id,route_id,state,revision,starts_at,ends_at,quantity_mwh,submitted_at "
                "FROM delivery_plans WHERE facility_id=? ORDER BY submitted_at DESC",
                (facility_id,),
            ).fetchall()]
        elif role in ("planner", "risk"):
            self._require(actor_id, "overage.review")
            view["pending_reviews"] = self.pending_reviews(actor_id)["pending"]
            view["periods"] = [dict(item) for item in self.connection.execute(
                "SELECT * FROM settlement_periods ORDER BY period_key"
            ).fetchall()]
            view["grants"] = [
                self._grant_balance(item)
                for item in self.connection.execute(
                    "SELECT * FROM entitlements ORDER BY facility_id,product,created_at DESC"
                ).fetchall()
            ]
        else:
            self._require(actor_id, "audit.read")
            view["accounts"] = [
                dict(item) for item in self.connection.execute(
                    "SELECT facility_id,product,"
                    "SUM(CAST(quantity_mwh AS REAL)) quantity_mwh,"
                    "SUM(CAST(held_mwh AS REAL)) held_mwh,"
                    "SUM(CAST(consumed_mwh AS REAL)) consumed_mwh,"
                    "SUM(CAST(expired_mwh AS REAL)) expired_mwh "
                    "FROM entitlements GROUP BY facility_id,product ORDER BY facility_id,product"
                ).fetchall()
            ]
            view["plans_by_state"] = [
                dict(item) for item in self.connection.execute(
                    "SELECT state,COUNT(*) AS count FROM delivery_plans GROUP BY state ORDER BY state"
                ).fetchall()
            ]
            view["entries"] = [dict(item) for item in self.connection.execute(
                "SELECT action,COUNT(*) AS count,SUM(CAST(amount_mwh AS REAL)) AS amount_mwh "
                "FROM entitlement_entries GROUP BY action ORDER BY action"
            ).fetchall()]
            view["reviews"] = [dict(item) for item in self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM overage_reviews GROUP BY state ORDER BY state"
            ).fetchall()]
            view["audit_chain"] = self.audit_chain(actor_id)
        return view
