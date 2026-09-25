"""HTTP 接口：本地 REST 风格 API，仅依赖标准库。

- X-Role 头识别调用方职责；缺失/非法分别返回 401/403；
- 排班接口要求 Idempotency-Key 头，重放返回原结果；
- 领域错误统一映射为 {"ok": false, "error": {...}}。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..application.orchestrator import OrchestratorService
from ..application.outbox import OutboxDispatcher
from ..application.recovery import RecoveryManager
from ..domain.enums import Role
from ..domain.errors import DomainError, Forbidden, Unauthorized
from ..infrastructure.sqlite_store import SQLiteStore
from .views import consultation_view

# 路由所需角色；None 表示仅需合法身份
RouteHandler = Callable[["Request"], Any]


class Request:
    def __init__(self, handler: "ApiHandler", match: re.Match, body: dict, role: Role | None):
        self._handler = handler
        self.params = match.groupdict()
        self.body = body
        self.role = role
        self.headers = handler.headers


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Api:
    """把领域服务装配为路由表，便于测试与复用。"""

    def __init__(
        self,
        store: SQLiteStore,
        orchestrator: OrchestratorService,
        dispatcher: OutboxDispatcher,
        recovery: RecoveryManager,
        clock,
    ) -> None:
        self.store = store
        self.orchestrator = orchestrator
        self.dispatcher = dispatcher
        self.recovery = recovery
        self.clock = clock
        self.routes: list[tuple[str, re.Pattern, frozenset[Role] | None, RouteHandler]] = []
        self._register()

    def _add(self, method: str, pattern: str, roles: frozenset[Role] | None, fn: RouteHandler) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), roles, fn))

    def _register(self) -> None:
        sched_admin = frozenset({Role.SCHEDULER, Role.ADMIN})
        ops_admin = frozenset({Role.OPS, Role.ADMIN})
        clin_admin = frozenset({Role.CLINICIAN, Role.ADMIN})
        write_roles = frozenset({Role.SCHEDULER, Role.OPS, Role.CLINICIAN, Role.ADMIN})

        self._add("GET", r"/v1/health", None, lambda r: {"status": "ok"})
        self._add("POST", r"/v1/institutions", ops_admin, self._create_institution)
        self._add("POST", r"/v1/links", ops_admin, self._create_link)
        self._add("POST", r"/v1/links/(?P<link_id>[^/]+)/probes", ops_admin, self._create_probe)
        self._add("POST", r"/v1/authorizations", write_roles, self._create_authorization)
        self._add("POST", r"/v1/authorizations/(?P<auth_id>[^/]+)/revoke", clin_admin, self._revoke_authorization)
        self._add("POST", r"/v1/consultations", sched_admin, self._schedule)
        self._add("GET", r"/v1/consultations/(?P<cid>[^/]+)", None, self._get_consultation)
        self._add("POST", r"/v1/consultations/(?P<cid>[^/]+)/actions", write_roles, self._action)
        self._add("POST", r"/v1/consultations/(?P<cid>[^/]+)/evaluate", ops_admin, self._evaluate)
        self._add("GET", r"/v1/outbox/events", ops_admin, self._outbox_events)
        self._add("POST", r"/v1/outbox/dispatch", ops_admin, self._outbox_dispatch)
        self._add("POST", r"/v1/admin/recover", frozenset({Role.ADMIN}), self._recover)

    # ------------------------------------------------------------------
    def _create_institution(self, req: Request) -> Any:
        b = req.body
        return self.orchestrator.register_institution(b["id"], b["name"], b.get("tier", "standard"))

    def _create_link(self, req: Request) -> Any:
        b = req.body
        return self.orchestrator.register_link(b["id"], b["institution_id"], b["kind"], b["failure_domain"])

    def _create_probe(self, req: Request) -> Any:
        b = req.body
        return self.orchestrator.record_probe(
            req.params["link_id"],
            latency_ms=float(b["latency_ms"]),
            loss_pct=float(b["loss_pct"]),
            availability=float(b["availability"]),
            measured_at=_parse_dt(b["measured_at"]) if b.get("measured_at") else None,
        )

    def _create_authorization(self, req: Request) -> Any:
        b = req.body
        return self.orchestrator.grant_authorization(
            b["id"], b["kind"], b["subject_id"], b.get("scope", "telemedicine"),
            _parse_dt(b["valid_from"]), _parse_dt(b["valid_until"]), b.get("payload"),
        )

    def _revoke_authorization(self, req: Request) -> Any:
        return self.orchestrator.revoke_authorization(req.params["auth_id"])

    def _schedule(self, req: Request) -> Any:
        key = req.headers.get("Idempotency-Key")
        if not key:
            raise DomainError("缺少 Idempotency-Key 头", details={"hint": "Idempotency-Key"})
        b = req.body
        return self.orchestrator.schedule_consultation(
            idempotency_key=key,
            slot_start=_parse_dt(b["slot_start"]),
            slot_end=_parse_dt(b["slot_end"]),
            institution_ids=list(b["institution_ids"]),
            doctor_id=b["doctor_id"],
            equipment_id=b["equipment_id"],
            patient=b["patient"],
            min_service_level=b["min_service_level"],
            consent_id=b["consent_id"],
            primary_link_id=b["primary_link_id"],
            backup_link_id=b.get("backup_link_id"),
        )

    def _get_consultation(self, req: Request) -> Any:
        c = self.orchestrator.get_consultation(req.params["cid"])
        return consultation_view(self.store, c, req.role, self.clock.now())

    def _action(self, req: Request) -> Any:
        b = req.body
        actor = b.get("actor") or req.role.value
        params = {k: v for k, v in b.items() if k not in ("action", "actor")}
        return self.orchestrator.perform_action(req.params["cid"], b["action"], actor, **params)

    def _evaluate(self, req: Request) -> Any:
        return self.orchestrator.reevaluate(req.params["cid"], trigger="api") or {"changed": False}

    def _outbox_events(self, req: Request) -> Any:
        events = self.store.list_outbox_events()
        return {
            "events": [
                {
                    "id": e.id,
                    "consultation_id": e.consultation_id,
                    "type": e.type,
                    "status": e.status.value,
                    "attempts": e.attempts,
                    "last_error": e.last_error,
                }
                for e in events
            ]
        }

    def _outbox_dispatch(self, req: Request) -> Any:
        return self.dispatcher.dispatch_pending()

    def _recover(self, req: Request) -> Any:
        return self.recovery.recover(self.orchestrator)


class ApiHandler(BaseHTTPRequestHandler):
    api: Api = None  # type: ignore[assignment]
    server_version = "TelemedicineContinuity/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # 静默访问日志
        return

    # --------------------------------------------------------------
    def _handle(self, method: str) -> None:
        try:
            result, status = self._dispatch(method)
        except DomainError as exc:
            self._send(exc.http_status, {"ok": False, "error": {
                "code": exc.code, "message": exc.message, "details": exc.details,
            }})
        except (KeyError, ValueError) as exc:
            self._send(400, {"ok": False, "error": {
                "code": "BAD_REQUEST", "message": f"请求参数无效: {exc}", "details": {},
            }})
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"ok": False, "error": {
                "code": "INTERNAL", "message": str(exc), "details": {},
            }})
        else:
            self._send(status, {"ok": True, "data": result})

    def _dispatch(self, method: str) -> tuple[Any, int]:
        path = self.path.split("?", 1)[0]
        for route_method, pattern, roles, fn in self.api.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            role = self._require_role(roles)
            body = self._read_body() if method == "POST" else {}
            result = fn(Request(self, match, body, role))
            return result, 200 if method == "GET" else 201
        raise NotFound404(path)

    def _require_role(self, roles: frozenset[Role] | None) -> Role:
        raw = self.headers.get("X-Role")
        if raw is None:
            raise Unauthorized("缺少 X-Role 头")
        try:
            role = Role(raw)
        except ValueError:
            raise Forbidden(f"未知职责: {raw}") from None
        if roles is not None and role not in roles:
            raise Forbidden(f"职责 {role.value} 无权访问该接口")
        return role

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


class NotFound404(DomainError):
    code = "NOT_FOUND"
    http_status = 404

    def __init__(self, path: str) -> None:
        super().__init__("接口不存在", details={"path": path})


def create_server(host: str, port: int, api: Api) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"api": api})
    return ThreadingHTTPServer((host, port), handler)
