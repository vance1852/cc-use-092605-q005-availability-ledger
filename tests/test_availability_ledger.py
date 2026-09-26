from __future__ import annotations

import sqlite3
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from availability_ledger.acceptance import run as acceptance_run
from availability_ledger.api import JsonApplication
from availability_ledger.clock import FrozenClock
from availability_ledger.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from availability_ledger.ledger import (
    GrantBalance,
    Shortfall,
    balances_from_entries,
    select_fefo,
    split_window_into_periods,
)
from availability_ledger.service import AvailabilityLedgerService


ROOT = Path(__file__).resolve().parents[1]


class SplitTests(unittest.TestCase):
    def test_cross_year_split_conserves_total(self) -> None:
        shares = split_window_into_periods(date(2026, 12, 15), date(2027, 2, 10), Decimal("9000"))
        self.assertEqual([share.period for share in shares], ["2026-12", "2027-01", "2027-02"])
        self.assertEqual([share.days for share in shares], [17, 31, 10])
        self.assertEqual(shares[0].amount_mwh, Decimal("2637.931"))
        self.assertEqual(shares[1].amount_mwh, Decimal("4810.345"))
        self.assertEqual(shares[2].amount_mwh, Decimal("1551.724"))
        self.assertEqual(sum((share.amount_mwh for share in shares), Decimal("0")), Decimal("9000.000"))

    def test_single_period_split(self) -> None:
        shares = split_window_into_periods(date(2027, 3, 1), date(2027, 3, 15), Decimal("120.5"))
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].period, "2027-03")
        self.assertEqual(shares[0].amount_mwh, Decimal("120.500"))


class BalanceTests(unittest.TestCase):
    def test_balances_from_entries(self) -> None:
        grants = [{"grant_id": "g1", "kind": "GUARANTEED_ENERGY", "valid_from": "2026-12-01", "valid_to": "2027-01-31"}]
        entries = [
            {"grant_id": "g1", "action": "GRANT", "amount_mwh": "100"},
            {"grant_id": "g1", "action": "HOLD", "amount_mwh": "40"},
            {"grant_id": "g1", "action": "CONSUME", "amount_mwh": "15"},
            {"grant_id": "g1", "action": "RELEASE", "amount_mwh": "5"},
            {"grant_id": "g1", "action": "CONSUME_OVER", "amount_mwh": "3"},
            {"grant_id": "g1", "action": "EXPIRE", "amount_mwh": "10"},
            {"grant_id": None, "action": "OVERUSE_FLAG", "amount_mwh": "2"},
        ]
        balance = balances_from_entries(grants, entries)["g1"]
        self.assertEqual(balance.granted, Decimal("100"))
        self.assertEqual(balance.held, Decimal("20"))
        self.assertEqual(balance.consumed, Decimal("18"))
        self.assertEqual(balance.expired, Decimal("10"))
        self.assertEqual(balance.available, Decimal("52"))

    def test_select_fefo_prefers_earliest_expiry(self) -> None:
        late = GrantBalance("g-late", "GUARANTEED_ENERGY", "2026-12-01", "2027-01-31", Decimal("10"), Decimal("0"), Decimal("0"), Decimal("0"))
        early = GrantBalance("g-early", "CAPACITY_COMPENSATION", "2026-12-01", "2026-12-31", Decimal("20"), Decimal("0"), Decimal("0"), Decimal("0"))
        picks = select_fefo([late, early], Decimal("25"))
        self.assertEqual(picks, [("g-early", Decimal("20")), ("g-late", Decimal("5"))])
        with self.assertRaises(Shortfall):
            select_fefo([late, early], Decimal("31"))


class LedgerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 12, 20, 8, 0, tzinfo=timezone.utc))
        self.service = AvailabilityLedgerService(self.connection, self.clock)
        self.service.create_user("site-east", "场站会计-东", "site", "fanshi-one")
        self.service.create_user("biz", "经营结算", "business")
        self.service.create_user("audit", "审计", "auditor")
        self.service.publish_rules("biz", {"review_deadline_hours": 48, "expire_unused": True, "overuse_policy": "review", "note": "2026 版核算规则"})
        self.service.open_period("biz", "2026-12")
        self.service.open_period("biz", "2027-01")
        self.service.register_channel("biz", {"route_id": "fanshi-export", "period_capacity_mwh": "120000"})
        self.service.open_account("biz", {"account_id": "acct-1", "facility_id": "fanshi-one", "batch_id": "batch-18mw-a"})
        self.service.register_grant("biz", {"grant_id": "g-guar", "account_id": "acct-1", "kind": "GUARANTEED_ENERGY", "amount_mwh": "60000", "valid_from": "2026-12-01", "valid_to": "2027-01-31", "period": "2026-12", "source_ref": "保障性电量下达", "idempotency_key": "grant-key-1"})
        self.service.register_grant("biz", {"grant_id": "g-maint", "account_id": "acct-1", "kind": "MAINTENANCE_EXEMPTION", "amount_mwh": "20000", "valid_from": "2026-12-01", "valid_to": "2027-02-28", "period": "2026-12", "source_ref": "检修免责核定", "idempotency_key": "grant-key-2"})
        self.service.register_grant("biz", {"grant_id": "g-cap", "account_id": "acct-1", "kind": "CAPACITY_COMPENSATION", "amount_mwh": "15000", "valid_from": "2027-01-01", "valid_to": "2027-03-31", "period": "2027-01", "source_ref": "容量补偿确认", "idempotency_key": "grant-key-3"})

    def tearDown(self) -> None:
        self.connection.close()

    def make_plan(self, plan_id: str = "plan-a", total: str = "40000", route: str = "fanshi-export", window: tuple[str, str] = ("2026-12-25", "2027-01-15"), account: str = "acct-1") -> dict:
        self.service.create_plan("site-east", {"plan_id": plan_id, "account_id": account, "route_id": route, "window_start": window[0], "window_end": window[1], "total_mwh": total, "idempotency_key": f"key-{plan_id}"})
        return self.service.confirm_plan("site-east", plan_id, 1)

    def entry_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) AS c FROM ledger_entries").fetchone()["c"]

    def test_account_is_unique_per_facility_and_batch(self) -> None:
        with self.assertRaises(Conflict):
            self.service.open_account("biz", {"account_id": "acct-dup", "facility_id": "fanshi-one", "batch_id": "batch-18mw-a"})

    def test_grant_registration_is_idempotent(self) -> None:
        payload = {"grant_id": "g-x", "account_id": "acct-1", "kind": "GUARANTEED_ENERGY", "amount_mwh": "5", "valid_from": "2026-12-01", "valid_to": "2027-01-31", "period": "2026-12", "source_ref": "补充下达", "idempotency_key": "grant-key-x"}
        first = self.service.register_grant("biz", payload)
        self.assertEqual(first, self.service.register_grant("biz", payload))
        with self.assertRaises(Conflict):
            self.service.register_grant("biz", dict(payload, amount_mwh="6"))

    def test_held_quota_is_not_visible_to_other_plans(self) -> None:
        self.make_plan("plan-a", "40000")
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].held, Decimal("40000.000"))
        self.assertEqual(balances["g-guar"].available, Decimal("20000.000"))
        self.service.create_plan("site-east", {"plan_id": "plan-b", "account_id": "acct-1", "route_id": "fanshi-export", "window_start": "2026-12-26", "window_end": "2027-01-10", "total_mwh": "60000", "idempotency_key": "key-plan-b"})
        with self.assertRaises(Conflict):
            self.service.confirm_plan("site-east", "plan-b", 1)
        plan_b = self.connection.execute("SELECT state FROM delivery_plans WHERE plan_id='plan-b'").fetchone()
        self.assertEqual(plan_b["state"], "draft")
        self.assertEqual(self.entry_count(), 5)

    def test_confirm_is_atomic_when_quota_short(self) -> None:
        self.service.create_plan("site-east", {"plan_id": "plan-big", "account_id": "acct-1", "route_id": "fanshi-export", "window_start": "2026-12-25", "window_end": "2027-01-15", "total_mwh": "96000", "idempotency_key": "key-plan-big"})
        with self.assertRaises(Conflict):
            self.service.confirm_plan("site-east", "plan-big", 1)
        self.assertEqual(self.entry_count(), 3)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) AS c FROM channel_reservations").fetchone()["c"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) AS c FROM plan_segments").fetchone()["c"], 0)
        smaller = self.make_plan("plan-ok", "40000")
        self.assertEqual(smaller["state"], "confirmed")

    def test_confirm_is_atomic_when_channel_short(self) -> None:
        self.service.register_channel("biz", {"route_id": "route-small", "period_capacity_mwh": "5000"})
        self.service.create_plan("site-east", {"plan_id": "plan-ch", "account_id": "acct-1", "route_id": "route-small", "window_start": "2026-12-25", "window_end": "2026-12-31", "total_mwh": "8000", "idempotency_key": "key-plan-ch"})
        with self.assertRaises(Conflict):
            self.service.confirm_plan("site-east", "plan-ch", 1)
        self.assertEqual(self.entry_count(), 3)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) AS c FROM channel_reservations").fetchone()["c"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) AS c FROM plan_segments").fetchone()["c"], 0)

    def test_cross_year_plan_splits_by_settlement_period(self) -> None:
        created = self.service.create_plan("site-east", {"plan_id": "plan-a", "account_id": "acct-1", "route_id": "fanshi-export", "window_start": "2026-12-25", "window_end": "2027-01-15", "total_mwh": "40000", "idempotency_key": "key-plan-a"})
        self.assertEqual([s["period"] for s in created["segments"]], ["2026-12", "2027-01"])
        self.assertEqual(created["segments"][0]["amount_mwh"], "12727.273")
        self.assertEqual(created["segments"][1]["amount_mwh"], "27272.727")
        confirmed = self.service.confirm_plan("site-east", "plan-a", 1)
        self.assertEqual(confirmed["revision"], 2)
        reservations = self.connection.execute("SELECT period,amount_mwh FROM channel_reservations WHERE plan_id='plan-a' ORDER BY period").fetchall()
        self.assertEqual([(row["period"], row["amount_mwh"]) for row in reservations], [("2026-12", "12727.273"), ("2027-01", "27272.727")])

    def test_cancel_releases_only_undelivered_part(self) -> None:
        self.make_plan("plan-a", "40000")
        self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "10000", "idempotency_key": "del-1"})
        result = self.service.cancel_plan("site-east", "plan-a", 2, "送出窗口调整")
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["released_mwh"], "30000.000")
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].consumed, Decimal("10000.000"))
        self.assertEqual(balances["g-guar"].held, Decimal("0"))
        self.assertEqual(balances["g-guar"].available, Decimal("50000.000"))
        held = self.connection.execute("SELECT COUNT(*) AS c FROM channel_reservations WHERE state='held'").fetchone()["c"]
        self.assertEqual(held, 0)

    def test_fail_plan_releases_only_undelivered_part(self) -> None:
        self.make_plan("plan-a", "40000")
        self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "10000", "idempotency_key": "del-1"})
        result = self.service.fail_plan("site-east", "plan-a", 2, "机组执行失败")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["released_mwh"], "30000.000")
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].consumed, Decimal("10000.000"))

    def test_overuse_goes_to_time_limited_review(self) -> None:
        self.make_plan("plan-a", "40000")
        delivery = self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        review = delivery["review"]
        self.assertEqual(review["over_mwh"], "272.727")
        self.assertEqual(review["deadline_at"], "2026-12-22T08:00:00Z")
        decided = self.service.decide_review("biz", review["review_id"], True, "超发属实")
        self.assertEqual(decided["state"], "approved")
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].consumed, Decimal("13000.000"))
        trace = self.service.trace_account("audit", "acct-1")
        over_entries = [entry for entry in trace["entries"] if entry["action"] == "CONSUME_OVER"]
        self.assertEqual(len(over_entries), 1)
        self.assertEqual(over_entries[0]["review_id"], review["review_id"])

    def test_submitter_cannot_decide_own_exception(self) -> None:
        self.service.create_user("biz2", "经营复核", "business")
        self.make_plan("plan-a", "40000")
        delivery = self.service.record_delivery("biz", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        review_id = delivery["review"]["review_id"]
        with self.assertRaises(Forbidden):
            self.service.decide_review("biz", review_id, True)
        decided = self.service.decide_review("biz2", review_id, False, "退回说明")
        self.assertEqual(decided["state"], "rejected")

    def test_review_deadline_is_enforced(self) -> None:
        self.make_plan("plan-a", "40000")
        delivery = self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        review_id = delivery["review"]["review_id"]
        self.clock.advance(hours=49)
        with self.assertRaises(InvalidState):
            self.service.decide_review("biz", review_id, True)
        review = self.connection.execute("SELECT state FROM exception_reviews WHERE review_id=?", (review_id,)).fetchone()
        self.assertEqual(review["state"], "expired")
        summary = self.service.trace_account("audit", "acct-1")
        self.assertEqual(summary["summary"]["available_mwh"], "55000.000")
        account = self.service._account_summary("acct-1")
        self.assertEqual(account["rejected_overuse_mwh"], "272.727")

    def test_rejected_overuse_is_flagged_not_consumed(self) -> None:
        self.make_plan("plan-a", "40000")
        delivery = self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        self.service.decide_review("biz", delivery["review"]["review_id"], False, "超发不予认可")
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].consumed, Decimal("12727.273"))
        account = self.service._account_summary("acct-1")
        self.assertEqual(account["rejected_overuse_mwh"], "272.727")

    def test_forbid_rule_rejects_overuse_atomically(self) -> None:
        self.service.publish_rules("biz", {"review_deadline_hours": 48, "expire_unused": True, "overuse_policy": "forbid", "note": "2026 修订版"})
        self.make_plan("plan-a", "40000")
        with self.assertRaises(Conflict):
            self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        segment = self.connection.execute("SELECT delivered_mwh FROM plan_segments WHERE plan_id='plan-a' AND period='2026-12'").fetchone()
        self.assertEqual(segment["delivered_mwh"], "0")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) AS c FROM exception_reviews").fetchone()["c"], 0)
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].held, Decimal("40000.000"))

    def test_new_rules_only_apply_to_unsettled_periods(self) -> None:
        settled = self.service.settle_period("biz", "2026-12")
        self.assertEqual(settled["rule_version"], 1)
        repinned = self.service.publish_rules("biz", {"review_deadline_hours": 72, "expire_unused": True, "overuse_policy": "review", "note": "2027 版"})
        self.assertEqual(repinned["rule_version"], 2)
        self.assertEqual(repinned["repinned_periods"], ["2027-01"])
        rows = self.connection.execute("SELECT period,rule_version,state FROM settlement_periods ORDER BY period").fetchall()
        self.assertEqual([(row["period"], row["rule_version"], row["state"]) for row in rows], [("2026-12", 1, "settled"), ("2027-01", 2, "open")])
        with self.assertRaises(InvalidState):
            self.service.register_grant("biz", {"grant_id": "g-late", "account_id": "acct-1", "kind": "GUARANTEED_ENERGY", "amount_mwh": "100", "valid_from": "2026-12-01", "valid_to": "2027-01-31", "period": "2026-12", "source_ref": "补登", "idempotency_key": "grant-key-late"})
        follow_up = self.service.register_grant("biz", {"grant_id": "g-next", "account_id": "acct-1", "kind": "GUARANTEED_ENERGY", "amount_mwh": "100", "valid_from": "2027-01-01", "valid_to": "2027-03-31", "period": "2027-01", "source_ref": "一月下达", "idempotency_key": "grant-key-next"})
        self.assertEqual(follow_up["period"], "2027-01")
        grant = self.connection.execute("SELECT rule_version FROM entitlement_grants WHERE grant_id='g-next'").fetchone()
        self.assertEqual(grant["rule_version"], 2)

    def test_settled_period_blocks_delivery_and_refund(self) -> None:
        self.make_plan("plan-a", "40000")
        self.service.settle_period("biz", "2026-12")
        with self.assertRaises(InvalidState):
            self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "1000", "idempotency_key": "del-1"})
        with self.assertRaises(InvalidState):
            self.service.cancel_plan("site-east", "plan-a", 2, "结算后取消")
        self.clock.current = datetime(2027, 1, 10, 8, 0, tzinfo=timezone.utc)
        result = self.service.cancel_plan("site-east", "plan-a", 2, "进入一月后取消")
        self.assertEqual(result["released_mwh"], "40000.000")
        release = self.connection.execute("SELECT period FROM ledger_entries WHERE action='RELEASE'").fetchall()
        self.assertEqual({row["period"] for row in release}, {"2027-01"})

    def test_settle_blocked_by_pending_review(self) -> None:
        self.make_plan("plan-a", "40000")
        delivery = self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        with self.assertRaises(Conflict):
            self.service.settle_period("biz", "2026-12")
        self.service.decide_review("biz", delivery["review"]["review_id"], True)
        self.assertEqual(self.service.settle_period("biz", "2026-12")["state"], "settled")

    def test_expiry_sweep_uses_current_period_rule(self) -> None:
        self.clock.current = datetime(2027, 2, 1, 9, 0, tzinfo=timezone.utc)
        result = self.service.sweep_expirations("biz")
        self.assertEqual(result["expired_grants"], [{"grant_id": "g-guar", "expired_mwh": "60000.000"}])
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].expired, Decimal("60000.000"))
        self.assertEqual(balances["g-maint"].available, Decimal("20000.000"))

    def test_expiry_sweep_respects_expire_unused_flag(self) -> None:
        self.service.publish_rules("biz", {"review_deadline_hours": 48, "expire_unused": False, "overuse_policy": "review", "note": "不作废版"})
        self.clock.current = datetime(2027, 2, 1, 9, 0, tzinfo=timezone.utc)
        result = self.service.sweep_expirations("biz")
        self.assertEqual(result["expired_grants"], [])
        balances = self.service._balances("acct-1")
        self.assertEqual(balances["g-guar"].available, Decimal("60000.000"))

    def test_sweep_expires_overdue_reviews(self) -> None:
        self.make_plan("plan-a", "40000")
        delivery = self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        self.clock.advance(hours=49)
        result = self.service.sweep_expirations("biz")
        self.assertEqual(result["expired_reviews"], [delivery["review"]["review_id"]])
        review = self.connection.execute("SELECT state FROM exception_reviews").fetchone()
        self.assertEqual(review["state"], "expired")

    def test_delivery_is_idempotent(self) -> None:
        self.make_plan("plan-a", "40000")
        payload = {"period": "2026-12", "actual_mwh": "10000", "idempotency_key": "del-1"}
        first = self.service.record_delivery("site-east", "plan-a", payload)
        self.assertEqual(first, self.service.record_delivery("site-east", "plan-a", payload))
        with self.assertRaises(Conflict):
            self.service.record_delivery("site-east", "plan-a", dict(payload, actual_mwh="10001"))
        self.assertEqual(self.entry_count(), 6)

    def test_role_views_match_duties(self) -> None:
        self.make_plan("plan-a", "40000")
        site = self.service.site_view("site-east", "fanshi-one")
        self.assertEqual(site["accounts"][0]["totals"]["held_mwh"], "40000.000")
        self.assertEqual(len(site["plans"]), 1)
        with self.assertRaises(Forbidden):
            self.service.site_view("site-east", "fanshi-two")
        business = self.service.business_view("biz")
        self.assertEqual(business["by_kind"]["GUARANTEED_ENERGY"]["held_mwh"], "40000.000")
        self.assertEqual(business["channels"][0]["periods"][0]["remaining_mwh"], "107272.727")
        with self.assertRaises(Forbidden):
            self.service.business_view("site-east")
        with self.assertRaises(Forbidden):
            self.service.business_view("audit")
        with self.assertRaises(Forbidden):
            self.service.register_grant("audit", {"grant_id": "g-no", "account_id": "acct-1", "kind": "GUARANTEED_ENERGY", "amount_mwh": "1", "valid_from": "2026-12-01", "valid_to": "2027-01-31", "period": "2026-12", "source_ref": "越权", "idempotency_key": "grant-key-no"})

    def test_site_cannot_touch_other_facility_account(self) -> None:
        self.service.open_account("biz", {"account_id": "acct-2", "facility_id": "fanshi-two", "batch_id": "batch-16mw-a"})
        with self.assertRaises(Forbidden):
            self.service.create_plan("site-east", {"plan_id": "plan-x", "account_id": "acct-2", "route_id": "fanshi-export", "window_start": "2026-12-25", "window_end": "2026-12-31", "total_mwh": "10", "idempotency_key": "key-plan-x"})
        with self.assertRaises(Forbidden):
            self.service.trace_account("site-east", "acct-2")
        self.assertEqual(self.service.trace_account("audit", "acct-2")["account_id"], "acct-2")

    def test_balance_traces_back_to_business_actions(self) -> None:
        self.make_plan("plan-a", "40000")
        delivery = self.service.record_delivery("site-east", "plan-a", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-1"})
        self.service.decide_review("biz", delivery["review"]["review_id"], True, "同意")
        self.service.cancel_plan("site-east", "plan-a", 2, "窗口调整")
        trace = self.service.trace_account("audit", "acct-1")
        actions = [entry["action"] for entry in trace["entries"]]
        self.assertEqual(actions, ["GRANT", "GRANT", "GRANT", "HOLD", "HOLD", "CONSUME", "CONSUME_OVER", "RELEASE"])
        final = trace["entries"][-1]["running"]
        self.assertEqual(final["available_mwh"], "82000.000")
        self.assertEqual(final, trace["summary"])
        hold = trace["entries"][3]
        self.assertEqual((hold["plan_id"], hold["period"], hold["rule_version"], hold["actor_id"]), ("plan-a", "2026-12", 1, "site-east"))
        release = trace["entries"][-1]
        self.assertEqual((release["action"], release["note"]), ("RELEASE", "窗口调整"))
        grant_trace = self.service.trace_account("audit", "acct-1", "g-guar")
        self.assertEqual(grant_trace["entries"][-1]["running"]["available_mwh"], "47000.000")

    def test_audit_chain_detects_tampering(self) -> None:
        self.make_plan("plan-a", "40000")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE ledger_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_validation_and_state_boundaries(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_user("ghost", "无场站场站人员", "site")
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("site-east", {"plan_id": "plan-past", "account_id": "acct-1", "route_id": "fanshi-export", "window_start": "2026-11-01", "window_end": "2026-12-15", "total_mwh": "10", "idempotency_key": "key-plan-past"})
        with self.assertRaises(NotFound):
            self.service.confirm_plan("site-east", "plan-missing", 1)
        self.service.create_plan("site-east", {"plan_id": "plan-draft", "account_id": "acct-1", "route_id": "fanshi-export", "window_start": "2026-12-25", "window_end": "2026-12-31", "total_mwh": "10", "idempotency_key": "key-plan-draft"})
        with self.assertRaises(InvalidState):
            self.service.record_delivery("site-east", "plan-draft", {"period": "2026-12", "actual_mwh": "1", "idempotency_key": "del-x"})


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 12, 20, 8, 0, tzinfo=timezone.utc))
        self.service = AvailabilityLedgerService(self.connection, clock)
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_http_boundary(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        missing_actor = self.app.handle("GET", "/views/business")
        self.assertEqual(missing_actor.status, 422)
        unknown = self.app.handle("GET", "/nope", {"X-Actor-Id": "biz"})
        self.assertEqual(unknown.status, 404)
        created = self.app.handle("POST", "/users", {"X-Actor-Id": "bootstrap"}, '{"user_id":"biz","display_name":"经营","role":"business"}'.encode("utf-8"))
        self.assertEqual(created.status, 201)
        rules = self.app.handle("POST", "/rules", {"X-Actor-Id": "biz"}, b'{"review_deadline_hours":48,"expire_unused":true,"overuse_policy":"review","note":"v1"}')
        self.assertEqual(rules.status, 201)
        view = self.app.handle("GET", "/views/business", {"X-Actor-Id": "biz"})
        self.assertEqual(view.status, 200)
        self.assertEqual(view.body["periods"], [])


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["review"]["self_decide_blocked"])
        self.assertEqual(result["review"]["decision"]["state"], "approved")
        self.assertEqual(result["cancelled"]["released_mwh"], "27272.727")
        self.assertEqual(result["rules_v2_repinned"], ["2027-01"])
        self.assertEqual(result["site_account_totals"]["consumed_mwh"], "13000.000")
        self.assertEqual(result["trace_entries"], 8)
        self.assertEqual(result["trace_final_running"]["available_mwh"], "82000.000")
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
