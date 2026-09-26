"""贯通分户权益流水、跨年计划确认、限时复核和周期结算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Forbidden
from .service import AvailabilityLedgerService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = AvailabilityLedgerService(connection, FrozenClock(datetime(2026, 12, 20, 8, 0, tzinfo=timezone.utc)))
    service.create_user("site-east", "场站会计-东", "site", "fanshi-one")
    service.create_user("biz", "经营结算", "business")
    service.create_user("audit", "审计", "auditor")
    rules_v1 = service.publish_rules("biz", {"review_deadline_hours": 48, "expire_unused": True, "overuse_policy": "review", "note": "2026 版核算规则"})
    service.open_period("biz", "2026-12")
    service.open_period("biz", "2027-01")
    service.register_channel("biz", {"route_id": "fanshi-export", "period_capacity_mwh": "120000"})
    service.open_account("biz", {"account_id": "acct-1", "facility_id": "fanshi-one", "batch_id": "batch-18mw-a"})
    service.register_grant("biz", {"grant_id": "g-guar", "account_id": "acct-1", "kind": "GUARANTEED_ENERGY", "amount_mwh": "60000", "valid_from": "2026-12-01", "valid_to": "2027-01-31", "period": "2026-12", "source_ref": "保障性电量下达-2026-12", "idempotency_key": "grant-key-1"})
    service.register_grant("biz", {"grant_id": "g-maint", "account_id": "acct-1", "kind": "MAINTENANCE_EXEMPTION", "amount_mwh": "20000", "valid_from": "2026-12-01", "valid_to": "2027-02-28", "period": "2026-12", "source_ref": "检修免责核定-2026-12", "idempotency_key": "grant-key-2"})
    service.register_grant("biz", {"grant_id": "g-cap", "account_id": "acct-1", "kind": "CAPACITY_COMPENSATION", "amount_mwh": "15000", "valid_from": "2027-01-01", "valid_to": "2027-03-31", "period": "2027-01", "source_ref": "容量补偿确认-2027-01", "idempotency_key": "grant-key-3"})
    service.create_plan("site-east", {"plan_id": "plan-001", "account_id": "acct-1", "route_id": "fanshi-export", "window_start": "2026-12-25", "window_end": "2027-01-15", "total_mwh": "40000", "idempotency_key": "plan-key-1"})
    confirmed = service.confirm_plan("site-east", "plan-001", 1)
    delivery = service.record_delivery("site-east", "plan-001", {"period": "2026-12", "actual_mwh": "13000", "idempotency_key": "del-key-1"})
    review_id = delivery["review"]["review_id"]
    self_decide_blocked = False
    try:
        service.decide_review("site-east", review_id, True)
    except Forbidden:
        self_decide_blocked = True
    decided = service.decide_review("biz", review_id, True, "超发部分属实，同意核销")
    cancelled = service.cancel_plan("site-east", "plan-001", 2, "送出窗口调整")
    settled = service.settle_period("biz", "2026-12")
    rules_v2 = service.publish_rules("biz", {"review_deadline_hours": 72, "expire_unused": True, "overuse_policy": "review", "note": "2027 版核算规则"})
    site = service.site_view("site-east", "fanshi-one")
    business = service.business_view("biz")
    trace = service.trace_account("audit", "acct-1")
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "rules_v1": rules_v1["rule_version"],
        "segments": confirmed["segments"],
        "review": {"delivery_review": delivery["review"], "self_decide_blocked": self_decide_blocked, "decision": decided},
        "cancelled": cancelled,
        "settled_period": settled,
        "rules_v2_repinned": rules_v2["repinned_periods"],
        "site_account_totals": site["accounts"][0]["totals"],
        "business_by_kind": business["by_kind"],
        "trace_entries": len(trace["entries"]),
        "trace_final_running": trace["entries"][-1]["running"],
        "audit": service.audit_chain("audit"),
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行可用率权益账本离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
