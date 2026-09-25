"""HTTP 接口：通过本地真实请求验证排班、角色过滤、操作与发件箱派发。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta

from telemedicine_continuity.interfaces.http_api import create_server

from helpers import OrchestratorTestCase, T0, schedule_ok


def http(port: int, method: str, path: str, body: dict | None = None,
         role: str | None = "admin", headers: dict | None = None) -> tuple[int, dict]:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=payload, method=method,
        headers={"Content-Type": "application/json"},
    )
    if role is not None:
        req.add_header("X-Role", role)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class HttpApiTests(OrchestratorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = create_server("127.0.0.1", 0, self.ctx.api)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def test_missing_role_rejected(self) -> None:
        status, body = http(self.port, "GET", "/v1/health", role=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_unknown_role_rejected(self) -> None:
        status, _ = http(self.port, "GET", "/v1/health", role="root")
        self.assertEqual(status, 403)

    def test_role_scope_enforced(self) -> None:
        status, _ = http(self.port, "POST", "/v1/outbox/dispatch", {}, role="scheduler")
        self.assertEqual(status, 403, "排班员无权触发发件箱派发")

    def test_schedule_and_fetch_via_http(self) -> None:
        body = {
            "slot_start": (T0 + timedelta(hours=2)).isoformat(),
            "slot_end": (T0 + timedelta(hours=3)).isoformat(),
            "institution_ids": ["inst-a", "inst-b"],
            "doctor_id": "doc-1",
            "equipment_id": "equip-1",
            "patient": {"ref": "pat-1", "name": "张三", "medical_record_no": "MRN-001"},
            "min_service_level": "GOLD",
            "consent_id": "consent-1",
            "primary_link_id": "link-a-primary",
            "backup_link_id": "link-a-backup",
        }
        status, resp = http(self.port, "POST", "/v1/consultations", body,
                            role="scheduler", headers={"Idempotency-Key": "http-key-1"})
        self.assertEqual(status, 201)
        cid = resp["data"]["consultation_id"]

        # 幂等重放
        status, resp2 = http(self.port, "POST", "/v1/consultations", body,
                             role="scheduler", headers={"Idempotency-Key": "http-key-1"})
        self.assertTrue(resp2["data"]["replayed"])
        self.assertEqual(resp2["data"]["consultation_id"], cid)

        # 缺少幂等键
        status, resp3 = http(self.port, "POST", "/v1/consultations", body, role="scheduler")
        self.assertEqual(status, 400)

        # 角色过滤：排班员视图不含患者姓名
        status, view = http(self.port, "GET", f"/v1/consultations/{cid}", role="scheduler")
        self.assertEqual(status, 200)
        self.assertNotIn("patient", view["data"])
        status, view = http(self.port, "GET", f"/v1/consultations/{cid}", role="clinician")
        self.assertEqual(view["data"]["patient"]["name"], "张三")

    def test_action_and_outbox_dispatch_via_http(self) -> None:
        cid = schedule_ok(self.ctx, key="http-action")["consultation_id"]
        status, resp = http(self.port, "POST", f"/v1/consultations/{cid}/actions",
                            {"action": "cancel", "actor": "ops"}, role="scheduler")
        self.assertEqual(status, 201)
        self.assertEqual(resp["data"]["status"], "CANCELLED")

        status, resp = http(self.port, "POST", "/v1/outbox/dispatch", {}, role="ops")
        self.assertEqual(resp["data"]["dispatched"], 2)
        status, resp = http(self.port, "GET", "/v1/outbox/events", role="ops")
        self.assertTrue(all(e["status"] == "SENT" for e in resp["data"]["events"]))

    def test_probe_ingest_triggers_failover_via_http(self) -> None:
        cid = schedule_ok(self.ctx, key="http-failover")["consultation_id"]
        status, resp = http(
            self.port, "POST", "/v1/links/link-a-primary/probes",
            {"latency_ms": 900, "loss_pct": 20.0, "availability": 0.5}, role="ops",
        )
        self.assertEqual(status, 201)
        self.assertTrue(resp["data"]["reevaluated"])
        status, view = http(self.port, "GET", f"/v1/consultations/{cid}", role="ops")
        self.assertEqual(view["data"]["links"]["active_link_id"], "link-a-backup")


if __name__ == "__main__":
    unittest.main()
