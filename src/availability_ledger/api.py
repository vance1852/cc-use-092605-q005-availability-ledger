"""无第三方依赖的可用率权益账本 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import LedgerError, ValidationFailed
from .service import AvailabilityLedgerService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: AvailabilityLedgerService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"], payload.get("facility_id")))
            if method == "POST" and path == "/rules":
                return Response(201, self.service.publish_rules(actor, payload))
            if method == "POST" and path == "/periods/open":
                return Response(201, self.service.open_period(actor, payload["period"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "periods" and parts[2] == "settle":
                return Response(200, self.service.settle_period(actor, parts[1]))
            if method == "POST" and path == "/channels":
                return Response(201, self.service.register_channel(actor, payload))
            if method == "POST" and path == "/accounts":
                return Response(201, self.service.open_account(actor, payload))
            if method == "POST" and path == "/grants":
                return Response(201, self.service.register_grant(actor, payload))
            if method == "POST" and path == "/plans":
                return Response(201, self.service.create_plan(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "confirm":
                return Response(200, self.service.confirm_plan(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "cancel":
                return Response(200, self.service.cancel_plan(actor, parts[1], int(payload["expected_revision"]), payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "fail":
                return Response(200, self.service.fail_plan(actor, parts[1], int(payload["expected_revision"]), payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "deliveries":
                return Response(201, self.service.record_delivery(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "reviews" and parts[2] == "decide":
                return Response(200, self.service.decide_review(actor, int(parts[1]), bool(payload["approve"]), payload.get("note", "")))
            if method == "POST" and path == "/maintenance/sweep":
                return Response(200, self.service.sweep_expirations(actor))
            if method == "GET" and path == "/views/site":
                return Response(200, self.service.site_view(actor, query.get("facility_id", [""])[0]))
            if method == "GET" and path == "/views/business":
                return Response(200, self.service.business_view(actor, query.get("period", [None])[0]))
            if method == "GET" and path == "/views/trace":
                return Response(200, self.service.trace_account(actor, query.get("account_id", [""])[0], query.get("grant_id", [None])[0]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LedgerError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AvailabilityLedger/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动机组可用率权益账本服务")
    parser.add_argument("--database", type=Path, default=Path("availability_ledger.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(AvailabilityLedgerService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
