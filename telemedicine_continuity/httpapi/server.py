"""本地 HTTP 接口（标准库实现，零第三方依赖）。

- ThreadingHTTPServer：并发请求真实并行，写操作在数据库事务处串行化；
- X-Role 表达调用方职责，X-Actor-Id 表达操作者（用于人工锁定判定），
  Idempotency-Key 表达幂等键；
- 所有领域错误统一映射为 JSON 错误体。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..application.notification import OutboxRelay
from ..application.service import OrchestrationService
from ..domain.enums import Role
from ..domain.errors import AuthorizationError, DomainError
from ..infra.db import Database

_JSON_CONTENT = "application/json; charset=utf-8"


class ApiContainer:
    def __init__(self, db: Database, service: OrchestrationService,
                 relay: OutboxRelay) -> None:
        self.db = db
        self.service = service
        self.relay = relay


def make_server(host: str, port: int, container: ApiContainer) -> ThreadingHTTPServer:
    handler_cls = _build_handler(container)
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    return server


def _build_handler(container: ApiContainer):
    service = container.service
    relay = container.relay

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "TelemedOrchestrator/1.0"

        # ------------------------------------------------ 基础工具
        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise DomainError("invalid_json", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise DomainError("invalid_json", "请求体必须是 JSON 对象")
            return data

        def _role(self) -> Role:
            value = self.headers.get("X-Role")
            if not value:
                raise DomainError("role_required", "缺少 X-Role 职责头")
            try:
                return Role(value)
            except ValueError:
                raise DomainError("role_unknown", f"未知职责 {value}")

        def _actor(self) -> str | None:
            return self.headers.get("X-Actor-Id")

        def _send_json(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", _JSON_CONTENT)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _domain_error(self, exc: DomainError) -> None:
            self._send_json(exc.http_status, {
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            })

        def log_message(self, fmt: str, *args) -> None:  # 安静输出，由测试/脚本控制
            return

        # ------------------------------------------------ 路由
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                body = self._read_json() if method == "POST" else {}
                for pattern, verbs, fn in ROUTES:
                    m = re.fullmatch(pattern, path)
                    if m and method in verbs:
                        fn(self, body, **m.groupdict())
                        return
                self._send_json(404, {"error": {"code": "not_found",
                                                "message": f"无此接口：{method} {path}"}})
            except DomainError as exc:
                self._domain_error(exc)
            except Exception as exc:  # 防御性兜底
                self._send_json(500, {"error": {"code": "internal_error",
                                                "message": str(exc)}})

        # ------------------------------------------------ 端点
        # ---- 基础数据 ----
        def healthz(self, body: dict) -> None:
            self._send_json(200, {"status": "ok"})

        def create_org(self, body: dict) -> None:
            org = service.register_org(self._role(), body["org_id"], body["name"])
            self._send_json(201, {"org_id": org.org_id, "name": org.name})

        def create_clinician(self, body: dict) -> None:
            c = service.register_clinician(
                self._role(), body["clinician_id"], body["name"], body["org_id"])
            self._send_json(201, {"clinician_id": c.clinician_id, "name": c.name,
                                  "org_id": c.org_id})

        def create_patient(self, body: dict) -> None:
            p = service.register_patient(
                self._role(), body["patient_id"], body["name"],
                body["national_id"], body["contact_phone"])
            self._send_json(201, {"patient_id": p.patient_id, "name": p.name})

        def create_link(self, body: dict) -> None:
            link = service.register_link(
                self._role(),
                link_id=body["link_id"], name=body["name"],
                fault_domain=body["fault_domain"], grade=body["grade"],
                org_ids=body["org_ids"],
            )
            self._send_json(201, {"link_id": link.link_id, "fault_domain": link.fault_domain,
                                  "grade": link.grade})

        def report_snapshot(self, body: dict, link_id: str) -> None:
            result = service.report_snapshot(
                self._role(),
                link_id,
                health=body["health"],
                latency_ms=body.get("latency_ms"),
                loss_rate=body.get("loss_rate"),
                snapshot_at=body.get("snapshot_at"),
            )
            self._send_json(200, result)

        def grant_consent(self, body: dict) -> None:
            consent = service.grant_consent(
                self._role(), body["patient_id"],
                valid_from=body.get("valid_from"), valid_until=body["valid_until"])
            self._send_json(201, {"patient_id": consent.patient_id, "state": consent.state,
                                  "valid_until": consent.valid_until})

        def revoke_consent(self, body: dict, patient_id: str) -> None:
            result = service.revoke_consent(self._role(), patient_id)
            self._send_json(200, result)

        def create_slot(self, body: dict) -> None:
            slot = service.create_slot(
                self._role(), slot_id=body["slot_id"],
                clinician_id=body["clinician_id"],
                start_time=body["start_time"], end_time=body["end_time"])
            self._send_json(201, {"slot_id": slot.slot_id, "status": slot.status})

        # ---- 排班 ----
        def book(self, body: dict) -> None:
            idem = self.headers.get("Idempotency-Key")
            c = service.book_consultation(
                self._role(),
                patient_id=body["patient_id"], clinician_id=body["clinician_id"],
                slot_id=body["slot_id"], org_ids=body["org_ids"],
                min_grade=body.get("min_grade", "AUDIO"),
                required_link_count=int(body.get("required_link_count", 1)),
                candidate_link_ids=body.get("candidate_link_ids"),
                idem_key=idem,
            )
            self._send_json(201, service.get_consultation_view(Role.COORDINATOR,
                                                               c.consultation_id))

        def list_consultations(self, body: dict) -> None:
            self._send_json(200, service.list_consultation_views(self._role()))

        def get_consultation(self, body: dict, cid: str) -> None:
            self._send_json(200, service.get_consultation_view(self._role(), cid))

        def _idem(self) -> str | None:
            return self.headers.get("Idempotency-Key")

        def switch(self, body: dict, cid: str) -> None:
            c = service.switch_path(
                self._role(), cid, actor_id=self._actor(),
                target_link_id=body.get("target_link_id"), idem_key=self._idem())
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def degrade(self, body: dict, cid: str) -> None:
            c = service.limited_degrade(
                self._role(), cid, actor_id=self._actor(),
                grade=body.get("grade", "AUDIO"), reason=body.get("reason", "人工有限降级"),
                idem_key=self._idem())
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def pause(self, body: dict, cid: str) -> None:
            c = service.pause(
                self._role(), cid, actor_id=self._actor(),
                reason=body.get("reason", "人工暂停"), idem_key=self._idem())
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def resume(self, body: dict, cid: str) -> None:
            c = service.resume(self._role(), cid, actor_id=self._actor(),
                               idem_key=self._idem())
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def cancel(self, body: dict, cid: str) -> None:
            c = service.cancel(
                self._role(), cid, actor_id=self._actor(),
                reason=body.get("reason", "人工取消"), idem_key=self._idem())
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def complete(self, body: dict, cid: str) -> None:
            c = service.complete(self._role(), cid, actor_id=self._actor())
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def set_phase(self, body: dict, cid: str) -> None:
            c = service.set_critical_phase(
                self._role(), cid, bool(body["critical"]), actor_id=self._actor())
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def lock(self, body: dict, cid: str) -> None:
            actor = self._actor() or body.get("actor_id")
            if not actor:
                raise DomainError("actor_required", "锁定需要 X-Actor-Id", )
            c = service.lock(self._role(), cid, actor)
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        def unlock(self, body: dict, cid: str) -> None:
            actor = self._actor() or body.get("actor_id")
            if not actor:
                raise DomainError("actor_required", "解锁需要 X-Actor-Id")
            c = service.unlock(self._role(), cid, actor)
            self._send_json(200, service.get_consultation_view(self._role(), c.consultation_id))

        # ---- 查询/运维 ----
        def list_slots(self, body: dict) -> None:
            self._send_json(200, service.list_slots(self._role()))

        def list_links(self, body: dict) -> None:
            self._send_json(200, service.list_links(self._role()))

        def deliver_outbox(self, body: dict) -> None:
            role = self._role()
            if role not in (Role.COORDINATOR, Role.AUDITOR):
                raise AuthorizationError("forbidden", "仅排班协调或审计可触发发件箱补发")
            stats = relay.deliver_pending(limit=int(body.get("limit", 100)))
            delivered = []
            transport = getattr(relay.transport, "export", None)
            if callable(transport):
                delivered = transport()
            self._send_json(200, {"stats": stats.as_dict(), "delivered": delivered})

        def list_outbox(self, body: dict) -> None:
            role = self._role()
            if role is not Role.AUDITOR:
                raise AuthorizationError("forbidden", "仅审计员可查看发件箱明细")
            with container.db.read_only() as conn:
                rows = conn.execute(
                    "SELECT event_id, event_key, event_type, aggregate_id, status, "
                    "attempts, last_error, created_at, sent_at FROM outbox ORDER BY rowid"
                ).fetchall()
            self._send_json(200, [dict(r) for r in rows])

        def list_audit(self, body: dict) -> None:
            role = self._role()
            if role is not Role.AUDITOR:
                raise AuthorizationError("forbidden", "仅审计员可查看审计日志")
            with container.db.read_only() as conn:
                rows = conn.execute(
                    "SELECT id, consultation_id, event_type, actor_role, actor_id, "
                    "details, created_at FROM audit_log ORDER BY id"
                ).fetchall()
            self._send_json(200, [dict(r) for r in rows])

    ROUTES = [
        (r"/healthz", {"GET"}, lambda h, b, **kw: h.healthz(b)),
        (r"/api/orgs", {"POST"}, lambda h, b, **kw: h.create_org(b)),
        (r"/api/clinicians", {"POST"}, lambda h, b, **kw: h.create_clinician(b)),
        (r"/api/patients", {"POST"}, lambda h, b, **kw: h.create_patient(b)),
        (r"/api/links", {"POST"}, lambda h, b, **kw: h.create_link(b)),
        (r"/api/links", {"GET"}, lambda h, b, **kw: h.list_links(b)),
        (r"/api/links/(?P<link_id>[^/]+)/snapshots", {"POST"},
         lambda h, b, **kw: h.report_snapshot(b, kw["link_id"])),
        (r"/api/consents", {"POST"}, lambda h, b, **kw: h.grant_consent(b)),
        (r"/api/consents/(?P<patient_id>[^/]+)/revoke", {"POST"},
         lambda h, b, **kw: h.revoke_consent(b, kw["patient_id"])),
        (r"/api/slots", {"POST"}, lambda h, b, **kw: h.create_slot(b)),
        (r"/api/slots", {"GET"}, lambda h, b, **kw: h.list_slots(b)),
        (r"/api/consultations", {"POST"}, lambda h, b, **kw: h.book(b)),
        (r"/api/consultations", {"GET"}, lambda h, b, **kw: h.list_consultations(b)),
        (r"/api/consultations/(?P<cid>[^/]+)", {"GET"},
         lambda h, b, **kw: h.get_consultation(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/switch", {"POST"},
         lambda h, b, **kw: h.switch(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/degrade", {"POST"},
         lambda h, b, **kw: h.degrade(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/pause", {"POST"},
         lambda h, b, **kw: h.pause(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/resume", {"POST"},
         lambda h, b, **kw: h.resume(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/cancel", {"POST"},
         lambda h, b, **kw: h.cancel(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/complete", {"POST"},
         lambda h, b, **kw: h.complete(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/phase", {"POST"},
         lambda h, b, **kw: h.set_phase(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/lock", {"POST"},
         lambda h, b, **kw: h.lock(b, kw["cid"])),
        (r"/api/consultations/(?P<cid>[^/]+)/unlock", {"POST"},
         lambda h, b, **kw: h.unlock(b, kw["cid"])),
        (r"/api/outbox/deliver", {"POST"}, lambda h, b, **kw: h.deliver_outbox(b)),
        (r"/api/outbox", {"GET"}, lambda h, b, **kw: h.list_outbox(b)),
        (r"/api/audit", {"GET"}, lambda h, b, **kw: h.list_audit(b)),
    ]
    return ApiHandler
