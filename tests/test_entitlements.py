from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from wind_dispatch.api import JsonApplication
from wind_dispatch.clock import FrozenClock
from wind_dispatch.entitlements import EntitlementService
from wind_dispatch.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from wind_dispatch.ledger import (
    GrantAvailability,
    daily_slices,
    period_bounds,
    select_first_expiring,
    split_by_period,
    split_quantities,
)


class LedgerRuleTests(unittest.TestCase):
    def test_split_by_period_crosses_year_boundary(self) -> None:
        start = datetime(2026, 12, 20, tzinfo=timezone.utc)
        end = datetime(2027, 1, 10, tzinfo=timezone.utc)
        windows = split_by_period(start, end)
        self.assertEqual([w.period_key for w in windows], ["2026-12", "2027-01"])
        self.assertEqual(windows[0].starts_at, start)
        self.assertEqual(windows[-1].ends_at, end)
        _, next_month = period_bounds("2026-12")
        self.assertEqual(windows[0].ends_at, next_month)
        self.assertEqual(windows[1].starts_at, next_month)

    def test_split_quantities_conserves_total_with_round_remainder(self) -> None:
        windows = split_by_period(
            datetime(2026, 12, 20, tzinfo=timezone.utc),
            datetime(2027, 1, 10, tzinfo=timezone.utc),
        )
        amounts = split_quantities(Decimal("11000"), windows)
        self.assertEqual(sum(amounts, Decimal("0")), Decimal("11000.000"))
        self.assertEqual(amounts[0], Decimal("6285.714"))
        self.assertEqual(amounts[1], Decimal("4714.286"))

    def test_daily_slices_cover_window(self) -> None:
        window = split_by_period(
            datetime(2026, 12, 20, tzinfo=timezone.utc),
            datetime(2026, 12, 23, tzinfo=timezone.utc),
        )[0]
        slices = daily_slices(window, Decimal("3000"))
        self.assertEqual([date for date, _ in slices], ["2026-12-20", "2026-12-21", "2026-12-22"])
        self.assertEqual(sum((qty for _, qty in slices), Decimal("0")), Decimal("3000.000"))

    def test_select_first_expiring_prefers_earlier_expiry(self) -> None:
        selections = select_first_expiring([
            GrantAvailability("LATE", Decimal("100"), "2027-04-01T00:00:00Z"),
            GrantAvailability("EARLY", Decimal("100"), "2027-01-01T00:00:00Z"),
        ], Decimal("150"))
        self.assertEqual(selections, [("EARLY", Decimal("100")), ("LATE", Decimal("50"))])
        with self.assertRaises(ValueError):
            select_first_expiring(
                [GrantAvailability("ONLY", Decimal("10"), "2027-01-01T00:00:00Z")],
                Decimal("20"),
            )


class EntitlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = EntitlementService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"),
                              ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "f1", "name": "一场",
                                              "kind": "offshore-station", "timezone": "Asia/Shanghai",
                                              "capacity_mwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "f2", "name": "二场",
                                              "kind": "storage", "timezone": "Asia/Shanghai",
                                              "capacity_mwh": "500000"})
        self.service.create_route("plan", {"route_id": "r1", "origin_id": "f1", "destination_id": "f2",
                                           "product": "turbine-18mw", "daily_capacity": "10000",
                                           "loss_basis_points": 25, "transit_hours": 36})
        self.window = {
            "applicable_from": "2026-11-01T00:00:00Z",
            "applicable_to": "2027-03-31T23:59:59Z",
            "expires_at": "2027-04-15T23:59:59Z",
        }

    def tearDown(self) -> None:
        self.connection.close()

    def grant(self, entitlement_id: str, grant_type: str, quantity: str, source: str) -> None:
        self.service.grant_entitlement("plan", {
            "entitlement_id": entitlement_id, "facility_id": "f1", "product": "turbine-18mw",
            "grant_type": grant_type, "quantity_mwh": quantity, "source_ref": source,
            "note": entitlement_id, **self.window,
        })

    def plan_payload(self, plan_id: str, quantity: str, key: str | None = None,
                     start: str = "2026-12-20T00:00:00Z",
                     end: str = "2027-01-10T00:00:00Z") -> dict:
        return {"plan_id": plan_id, "route_id": "r1", "starts_at": start, "ends_at": end,
                "quantity_mwh": quantity, "idempotency_key": key or f"key-{plan_id}"}

    def test_grant_records_three_sources_and_traces_balance(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "8000", "doc-1")
        self.grant("G-MNT", "MAINTENANCE_EXEMPT", "2000", "doc-2")
        self.grant("G-CAP", "CAPACITY_COMPENSATION", "1500", "doc-3")
        summary = self.service.account_summary("dispatch", "f1")
        totals = summary["totals_by_product"]["turbine-18mw"]
        self.assertEqual(totals["quantity_mwh"], "11500.000")
        self.assertEqual(totals["available_mwh"], "11500.000")
        detail = self.service.entitlement_detail("audit", "G-MNT")
        self.assertEqual([entry["action"] for entry in detail["entries"]], ["GRANT"])
        self.assertEqual(detail["entries"][0]["source_ref"], "doc-2")
        self.assertTrue(detail["audit_events"])

    def test_confirm_holds_entitlements_and_channel_in_one_transaction(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "8000", "doc-1")
        self.grant("G-MNT", "MAINTENANCE_EXEMPT", "2000", "doc-2")
        self.grant("G-CAP", "CAPACITY_COMPENSATION", "1500", "doc-3")
        plan = self.service.create_plan("dispatch", self.plan_payload("P1", "11000"))
        self.assertEqual([item["period_key"] for item in plan["segments"]], ["2026-12", "2027-01"])
        confirmed = self.service.confirm_plan("dispatch", "P1", 1)
        self.assertEqual(confirmed["state"], "confirmed")
        totals = self.service.account_summary("audit", "f1")["totals_by_product"]["turbine-18mw"]
        self.assertEqual(totals["held_mwh"], "11000.000")
        self.assertEqual(totals["available_mwh"], "500.000")
        reserved = self.connection.execute(
            "SELECT COUNT(*) AS c,SUM(CAST(reserved_mwh AS REAL)) AS total FROM route_reservations "
            "WHERE state='held'"
        ).fetchone()
        self.assertEqual(reserved["c"], 21)  # 12 个 12 月日 + 9 个 1 月日
        self.assertAlmostEqual(reserved["total"], 11000.0, places=3)

    def test_channel_shortfall_rolls_back_entitlement_holds(self) -> None:
        # 额度充足但通道容量不足：额度预占必须随通道预留一起回滚。
        self.grant("G-BIG", "GUARANTEED_VOLUME", "100000", "doc-big")
        self.service.create_route("plan", {"route_id": "r-small", "origin_id": "f1",
                                           "destination_id": "f2", "product": "turbine-18mw",
                                           "daily_capacity": "100", "loss_basis_points": 0,
                                           "transit_hours": 1})
        payload = self.plan_payload("PX", "10000")
        payload["route_id"] = "r-small"
        self.service.create_plan("dispatch", payload)
        with self.assertRaises(Conflict):
            self.service.confirm_plan("dispatch", "PX", 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) c FROM entitlement_holds").fetchone()["c"], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) c FROM route_reservations").fetchone()["c"], 0)
        self.assertEqual(self.service.account_summary("audit", "f1")
                         ["totals_by_product"]["turbine-18mw"]["held_mwh"], "0.000")
        plan = self.service.plan_detail("dispatch", "PX")
        self.assertEqual(plan["state"], "draft")

    def test_reserved_channel_quota_does_not_reappear_for_other_plan(self) -> None:
        self.grant("G-BIG", "GUARANTEED_VOLUME", "100000", "doc-big")
        self.service.create_route("plan", {"route_id": "r-tight", "origin_id": "f1",
                                           "destination_id": "f2", "product": "turbine-18mw",
                                           "daily_capacity": "100", "loss_basis_points": 0,
                                           "transit_hours": 1})
        first = self.plan_payload("PA", "400", start="2027-02-01T00:00:00Z",
                                  end="2027-02-05T00:00:00Z", key="ka")
        first["route_id"] = "r-tight"
        second = self.plan_payload("PB", "100", start="2027-02-01T00:00:00Z",
                                   end="2027-02-05T00:00:00Z", key="kb")
        second["route_id"] = "r-tight"
        self.service.create_plan("dispatch", first)
        self.service.confirm_plan("dispatch", "PA", 1)
        self.service.create_plan("dispatch", second)
        with self.assertRaises(Conflict):
            self.service.confirm_plan("dispatch", "PB", 1)

    def test_create_plan_is_idempotent(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "100", "doc-1")
        payload = self.plan_payload("P1", "10")
        first = self.service.create_plan("dispatch", payload)
        second = self.service.create_plan("dispatch", payload)
        self.assertEqual(first, second)
        changed = dict(payload, quantity_mwh="11")
        with self.assertRaises(Conflict):
            self.service.create_plan("dispatch", changed)

    def test_overage_review_separation_of_duties_and_sla(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "100", "doc-1")
        self.service.create_plan("dispatch", self.plan_payload(
            "P1", "600", start="2027-02-01T00:00:00Z", end="2027-02-05T00:00:00Z"))
        with self.assertRaises(Conflict):
            self.service.confirm_plan("dispatch", "P1", 1)
        submitted = self.service.submit_overage_review("dispatch", "P1", "临时增供")
        self.assertEqual(submitted["state"], "pending_review")
        self.assertEqual(len(submitted["overage_reviews"]), 1)
        review = submitted["overage_reviews"][0]
        # 提交人不能审批自己的例外。
        with self.assertRaises(Forbidden):
            self.service.decide_overage_review("dispatch", review["review_id"], True, "自己批准")
        decided = self.service.decide_overage_review("risk", review["review_id"], False, "驳回")
        self.assertEqual(decided["state"], "draft")
        resubmitted = self.service.submit_overage_review("dispatch", "P1", "再次申请")
        review_id = [item for item in resubmitted["overage_reviews"]
                     if item["state"] == "pending"][0]["review_id"]
        # 超过 24 小时限时后复核自动失效。
        self.clock.advance(hours=25)
        self.assertEqual(self.service.pending_reviews("risk")["count"], 0)
        with self.assertRaises(InvalidState):
            self.service.decide_overage_review("risk", review_id, True, "超时批准")
        self.assertEqual(self.service.plan_detail("dispatch", "P1")["state"], "draft")
        # 额度在驳回与超时后均已解除。
        self.assertEqual(self.service.account_summary("audit", "f1")
                         ["totals_by_product"]["turbine-18mw"]["held_mwh"], "0.000")

    def test_approved_overage_creates_traceable_exception_grant(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "100", "doc-1")
        self.service.create_plan("dispatch", self.plan_payload(
            "P1", "600", start="2027-02-01T00:00:00Z", end="2027-02-05T00:00:00Z"))
        submitted = self.service.submit_overage_review("dispatch", "P1", "临时增供")
        review_id = submitted["overage_reviews"][0]["review_id"]
        decided = self.service.decide_overage_review("plan", review_id, True, "同意")
        self.assertEqual(decided["state"], "confirmed")
        exception = self.service.entitlement_detail("audit", f"OVR-{review_id}")
        self.assertEqual(exception["grant_type"], "OVERAGE_EXCEPTION")
        actions = [(entry["action"], entry["reason_code"]) for entry in exception["entries"]]
        self.assertIn(("GRANT", "OVERAGE_APPROVED"), actions)
        self.assertIn(("HOLD", ""), actions)

    def test_delivery_writeoff_cancel_returns_only_unsettled_part(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "8000", "doc-1")
        self.grant("G-MNT", "MAINTENANCE_EXEMPT", "2000", "doc-2")
        self.grant("G-CAP", "CAPACITY_COMPENSATION", "1500", "doc-3")
        self.service.create_plan("dispatch", self.plan_payload("P1", "11000"))
        self.service.confirm_plan("dispatch", "P1", 1)
        december = "2026-12"
        delivered = self.service.record_delivery("dispatch", "P1", december, "3000")
        self.assertEqual(delivered["state"], "partially_settled")
        # 超过剩余预占的实际电量必须先走超额复核。
        with self.assertRaises(Conflict):
            self.service.record_delivery("dispatch", "P1", december, "99999")
        cancelled = self.service.cancel_plan("dispatch", "P1", delivered["revision"])
        self.assertEqual(cancelled["state"], "cancelled")
        totals = self.service.account_summary("audit", "f1")["totals_by_product"]["turbine-18mw"]
        self.assertEqual(totals["consumed_mwh"], "3000.000")
        self.assertEqual(totals["held_mwh"], "0.000")
        self.assertEqual(totals["available_mwh"], "8500.000")
        returned = self.connection.execute(
            "SELECT COALESCE(SUM(CAST(amount_mwh AS REAL)),0) total FROM entitlement_entries "
            "WHERE action='RETURN'"
        ).fetchone()["total"]
        self.assertAlmostEqual(returned, 8000.0, places=3)
        # 已形成实际电量 3000 mwh 对应的通道预留保留为 consumed，其余释放。
        channel = self.connection.execute(
            "SELECT state,COALESCE(SUM(CAST(reserved_mwh AS REAL)),0) total FROM route_reservations "
            "GROUP BY state"
        ).fetchall()
        by_state = {row["state"]: row["total"] for row in channel}
        self.assertAlmostEqual(by_state["consumed"], 3000.0, places=3)
        self.assertAlmostEqual(by_state["released"], 8000.0, places=3)

    def test_fail_plan_releases_unconsumed_holds(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "500", "doc-1")
        self.service.create_plan("dispatch", self.plan_payload(
            "P1", "400", start="2027-02-01T00:00:00Z", end="2027-02-05T00:00:00Z"))
        self.service.confirm_plan("dispatch", "P1", 1)
        failed = self.service.fail_plan("dispatch", "P1", 2, "送出通道故障")
        self.assertEqual(failed["state"], "failed")
        totals = self.service.account_summary("audit", "f1")["totals_by_product"]["turbine-18mw"]
        self.assertEqual(totals["held_mwh"], "0.000")
        self.assertEqual(totals["available_mwh"], "500.000")

    def test_closed_period_rejects_new_rules_but_keeps_open_periods(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "8000", "doc-1")
        self.service.close_period("plan", "2026-11")
        with self.assertRaises(InvalidState):
            self.service.create_plan("dispatch", self.plan_payload(
                "P-OLD", "10", start="2026-11-20T00:00:00Z", end="2026-11-25T00:00:00Z"))
        with self.assertRaises(InvalidState):
            self.grant("G-OLD", "GUARANTEED_VOLUME", "10", "doc-old")
        # 未结算周期不受影响。
        self.service.create_plan("dispatch", self.plan_payload("P-NEW", "10"))
        self.assertEqual(self.service.plan_detail("audit", "P-NEW")["state"], "draft")

    def test_expired_grant_writes_expire_entry(self) -> None:
        self.service.grant_entitlement("plan", {
            "entitlement_id": "G-SHORT", "facility_id": "f1", "product": "turbine-18mw",
            "grant_type": "GUARANTEED_VOLUME", "quantity_mwh": "100", "source_ref": "doc-s",
            "applicable_from": "2026-08-01T00:00:00Z", "applicable_to": "2026-08-31T23:59:59Z",
            "expires_at": "2026-09-20T00:00:00Z",
        })
        self.service.role_view("dispatch", facility_id="f1")  # 触发到期扫描
        detail = self.service.entitlement_detail("audit", "G-SHORT")
        self.assertEqual(detail["state"], "expired")
        self.assertEqual(detail["expired_mwh"], "100.000")
        self.assertEqual(detail["entries"][-1]["action"], "EXPIRE")

    def test_role_views_match_duties(self) -> None:
        self.grant("G-GUAR", "GUARANTEED_VOLUME", "100", "doc-1")
        station = self.service.role_view("dispatch", facility_id="f1")
        self.assertIn("account", station)
        self.assertIn("plans", station)
        with self.assertRaises(ValidationFailed):
            self.service.role_view("dispatch")
        operator = self.service.role_view("plan")
        self.assertIn("pending_reviews", operator)
        self.assertIn("periods", operator)
        auditor = self.service.role_view("audit")
        self.assertTrue(auditor["audit_chain"]["valid"])
        self.assertIn("entries", auditor)
        with self.assertRaises(Forbidden):
            self.service.grant_entitlement("dispatch", {
                "entitlement_id": "X", "facility_id": "f1", "product": "turbine-18mw",
                "grant_type": "GUARANTEED_VOLUME", "quantity_mwh": "1", "source_ref": "x",
                **self.window,
            })
        with self.assertRaises(Forbidden):
            self.service.decide_overage_review("audit", 1, True, "审计不能审批")
        with self.assertRaises(Forbidden):
            self.service.submit_overage_review("audit", "P1", "审计不能提交")


class EntitlementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = EntitlementService(
            self.connection, FrozenClock(datetime(2026, 9, 24, 8, tzinfo=timezone.utc))
        )
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"),
                              ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "f1", "name": "一场",
                                              "kind": "offshore-station", "timezone": "Asia/Shanghai",
                                              "capacity_mwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "f2", "name": "二场",
                                              "kind": "storage", "timezone": "Asia/Shanghai",
                                              "capacity_mwh": "500000"})
        self.service.create_route("plan", {"route_id": "r1", "origin_id": "f1", "destination_id": "f2",
                                           "product": "turbine-18mw", "daily_capacity": "10000",
                                           "loss_basis_points": 25, "transit_hours": 36})
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def headers(self, actor: str) -> dict[str, str]:
        return {"X-Actor-Id": actor}

    def test_grant_plan_confirm_flow_over_http(self) -> None:
        grant = {"entitlement_id": "G1", "facility_id": "f1", "product": "turbine-18mw",
                 "grant_type": "CAPACITY_COMPENSATION", "quantity_mwh": "500", "source_ref": "doc-1",
                 "applicable_from": "2026-11-01T00:00:00Z",
                 "applicable_to": "2027-03-31T23:59:59Z",
                 "expires_at": "2027-04-15T23:59:59Z"}
        response = self.app.handle("POST", "/entitlements", self.headers("plan"), _json(grant))
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["available_mwh"], "500.000")
        plan = {"plan_id": "P1", "route_id": "r1", "starts_at": "2027-02-01T00:00:00Z",
                "ends_at": "2027-02-05T00:00:00Z", "quantity_mwh": "400",
                "idempotency_key": "k1"}
        created = self.app.handle("POST", "/plans", self.headers("dispatch"), _json(plan))
        self.assertEqual(created.status, 201)
        confirmed = self.app.handle("POST", "/plans/P1/confirm", self.headers("dispatch"),
                                    _json({"expected_revision": 1}))
        self.assertEqual(confirmed.status, 200)
        self.assertEqual(confirmed.body["state"], "confirmed")
        account = self.app.handle("GET", "/entitlements?facility_id=f1", self.headers("audit"))
        self.assertEqual(account.body["totals_by_product"]["turbine-18mw"]["held_mwh"], "400.000")
        review = self.app.handle("GET", "/role-view", self.headers("risk"))
        self.assertEqual(review.status, 200)
        self.assertIn("pending_reviews", review.body)


def _json(value: dict) -> bytes:
    import json

    return json.dumps(value).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
