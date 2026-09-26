"""贯通结算单价、送出通道、机组可用量、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .entitlements import EntitlementService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = EntitlementService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "fanshi-one", "name": "北部海上风电场", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "fanshi-two", "name": "帆石二场", "kind": "offshore-station", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "fanshi-export", "origin_id": "fanshi-one", "destination_id": "fanshi-two", "product": "turbine-18mw", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "fanshi-one", "product": "turbine-18mw", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "fanshi-export", "shipper_id": "station-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "fanshi-export", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "grid-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"fanshi-export": "20"}, "demand_changes": {"fanshi-one:turbine-18mw": "-5"}})
    service.approve_scenario("risk", "grid-recovery", 1)
    scenario = service.run_scenario("plan", "grid-recovery", "2026-09-23")

    # 可用率权益账本：三类额度来源按场站与机组批次分户。
    grant_window = {
        "applicable_from": "2026-11-01T00:00:00Z",
        "applicable_to": "2027-03-31T23:59:59Z",
        "expires_at": "2027-04-15T23:59:59Z",
    }
    service.grant_entitlement("plan", {"entitlement_id": "ent-guaranteed", "facility_id": "fanshi-one", "product": "turbine-18mw", "grant_type": "GUARANTEED_VOLUME", "quantity_mwh": "80000", "source_ref": "guarantee-2027", "note": "保障性电量", **grant_window})
    service.grant_entitlement("plan", {"entitlement_id": "ent-maintenance", "facility_id": "fanshi-one", "product": "turbine-18mw", "grant_type": "MAINTENANCE_EXEMPT", "quantity_mwh": "20000", "source_ref": "outage-waiver-2027", "note": "检修免责", **grant_window})
    service.grant_entitlement("plan", {"entitlement_id": "ent-capacity", "facility_id": "fanshi-one", "product": "turbine-18mw", "grant_type": "CAPACITY_COMPENSATION", "quantity_mwh": "10000", "source_ref": "capacity-comp-2027", "note": "容量补偿", **grant_window})
    cross_year = {"route_id": "fanshi-export", "starts_at": "2026-12-20T00:00:00Z", "ends_at": "2027-01-10T00:00:00Z"}
    service.create_plan("dispatch", {"plan_id": "plan-cross-year", "quantity_mwh": "90000", "idempotency_key": "plan-key-001", **cross_year})
    confirmed_plan = service.confirm_plan("dispatch", "plan-cross-year", 1)
    # 额度不足的跨年计划进入限时复核：提交人不能审批自己的例外。
    service.create_plan("dispatch", {"plan_id": "plan-overage", "quantity_mwh": "30000", "idempotency_key": "plan-key-002", **cross_year})
    overage = service.submit_overage_review("dispatch", "plan-overage", "寒潮增发超出保障性额度")
    for pending_review in overage["overage_reviews"]:
        if pending_review["state"] == "pending":
            overage = service.decide_overage_review("risk", pending_review["review_id"], True, "同意寒潮应急增供")
    approved_plan = overage
    # 登记实际电量形成核销，随后取消主计划，仅返还未形成实际电量的部分。
    delivered_period = confirmed_plan["segments"][0]["period_key"]
    service.record_delivery("dispatch", "plan-cross-year", delivered_period, "50000")
    cross_year_revision = service.plan_detail("dispatch", "plan-cross-year")["revision"]
    cancelled_plan = service.cancel_plan("dispatch", "plan-cross-year", cross_year_revision)
    ledger_totals = service.account_summary("audit", "fanshi-one")["totals_by_product"]["turbine-18mw"]
    role_views = {
        "station": sorted(service.role_view("dispatch", facility_id="fanshi-one").keys()),
        "operator": sorted(service.role_view("plan").keys()),
        "auditor": sorted(service.role_view("audit").keys()),
    }
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"],
              "ledger": {"plan_periods": [segment["period_key"] for segment in confirmed_plan["segments"]],
                         "cross_year_state": confirmed_plan["state"],
                         "overage_state": approved_plan["state"],
                         "cancelled_state": cancelled_plan["state"],
                         "consumed_mwh": ledger_totals["consumed_mwh"],
                         "returned_to_available_mwh": ledger_totals["available_mwh"],
                         "role_views": role_views},
              "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行海上风电场调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
