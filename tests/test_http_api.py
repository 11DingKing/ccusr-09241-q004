"""本地 HTTP 接口测试：真实线程服务器 + HTTP 请求（含并发排班）。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from telemedicine_continuity.application.notification import InProcessTransport, OutboxRelay
from telemedicine_continuity.application.service import OrchestrationService
from telemedicine_continuity.httpapi.server import ApiContainer, make_server
from telemedicine_continuity.infra.clock import MutableClock
from telemedicine_continuity.infra.db import Database


class HttpServerTestBed(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "http.db")
        self.clock = MutableClock()
        db = Database(self.db_path)
        self.transport = InProcessTransport()
        service = OrchestrationService(db, self.clock)
        self.relay = OutboxRelay(db, self.transport)
        container = ApiContainer(db, service, self.relay)
        self.server = make_server("127.0.0.1", 0, container)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def seed(self) -> None:
        for org_id, name in [("H-LEAD", "牵头医院"), ("C-01", "社区中心")]:
            status, _ = self.request("POST", "/api/orgs",
                                     {"org_id": org_id, "name": name},
                                     {"X-Role": "coordinator"})
            self.assertEqual(status, 201)
        self.assertEqual(self.request("POST", "/api/clinicians", {
            "clinician_id": "D001", "name": "张医生", "org_id": "H-LEAD"},
            {"X-Role": "coordinator"})[0], 201)
        self.assertEqual(self.request("POST", "/api/patients", {
            "patient_id": "P001", "name": "李四",
            "national_id": "110101199001011234", "contact_phone": "13800001234"},
            {"X-Role": "coordinator"})[0], 201)
        for link_id, name, domain in [
            ("LINK-A1", "政务专网", "FD-A"),
            ("LINK-B1", "5G 互联网", "FD-B"),
        ]:
            self.assertEqual(self.request("POST", "/api/links", {
                "link_id": link_id, "name": name, "fault_domain": domain,
                "grade": "HD_VIDEO", "org_ids": ["H-LEAD", "C-01"]},
                {"X-Role": "coordinator"})[0], 201)
            status, body = self.request(
                "POST", f"/api/links/{link_id}/snapshots",
                {"health": "up", "latency_ms": 40, "loss_rate": 0.0},
                {"X-Role": "link_engineer"})
            self.assertEqual(status, 200, body)
        self.assertEqual(self.request("POST", "/api/consents", {
            "patient_id": "P001", "valid_from": "2026-09-01T00:00:00+00:00",
            "valid_until": "2026-12-31T23:59:59+00:00"},
            {"X-Role": "coordinator"})[0], 201)
        self.assertEqual(self.request("POST", "/api/slots", {
            "slot_id": "S001", "clinician_id": "D001",
            "start_time": "2026-09-24T10:00:00+00:00",
            "end_time": "2026-09-24T11:00:00+00:00"},
            {"X-Role": "coordinator"})[0], 201)

    def book_payload(self) -> dict:
        return {
            "patient_id": "P001", "clinician_id": "D001", "slot_id": "S001",
            "org_ids": ["H-LEAD", "C-01"], "min_grade": "SD_VIDEO",
            "required_link_count": 2,
        }


class HttpFlowTests(HttpServerTestBed):
    def test_healthz(self) -> None:
        status, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_missing_role_header_is_rejected(self) -> None:
        self.seed()
        status, body = self.request("POST", "/api/consultations", self.book_payload())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "role_required")

    def test_wrong_role_is_forbidden(self) -> None:
        self.seed()
        status, body = self.request("POST", "/api/consultations", self.book_payload(),
                                    {"X-Role": "link_engineer"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_booking_then_fault_domain_switch_and_recovery(self) -> None:
        self.seed()
        status, booked = self.request("POST", "/api/consultations", self.book_payload(),
                                      {"X-Role": "coordinator"})
        self.assertEqual(status, 201, booked)
        self.assertEqual(booked["active_link_id"], "LINK-A1")
        self.assertEqual(booked["backup_link_id"], "LINK-B1")
        cid = booked["consultation_id"]

        # 故障域 FD-A 中断 → 联动切到 FD-B
        status, body = self.request(
            "POST", "/api/links/LINK-A1/snapshots", {"health": "down"},
            {"X-Role": "link_engineer"})
        self.assertEqual(status, 200)
        self.assertEqual(body["effects"][0]["action"], "switch")

        status, view = self.request("GET", f"/api/consultations/{cid}",
                                    headers={"X-Role": "coordinator"})
        self.assertEqual(view["active_link_id"], "LINK-B1")

        # 主链路 B 仍健康：A 恢复不主动切回，仅刷新备选指针
        status, body = self.request(
            "POST", "/api/links/LINK-A1/snapshots",
            {"health": "up", "latency_ms": 35}, {"X-Role": "link_engineer"})
        self.assertEqual(status, 200)
        status, view = self.request("GET", f"/api/consultations/{cid}",
                                    headers={"X-Role": "coordinator"})
        self.assertEqual(view["active_link_id"], "LINK-B1")
        self.assertEqual(view["backup_link_id"], "LINK-A1")

        # 主链路 B 劣化低于 SLA 时，恢复的 A 才成为切换目标
        status, body = self.request(
            "POST", "/api/links/LINK-B1/snapshots",
            {"health": "down"}, {"X-Role": "link_engineer"})
        self.assertEqual(body["effects"][0]["action"], "switch")
        status, view = self.request("GET", f"/api/consultations/{cid}",
                                    headers={"X-Role": "coordinator"})
        self.assertEqual(view["active_link_id"], "LINK-A1")

    def test_consent_revocation_critical_phase_defer_then_cancel(self) -> None:
        self.seed()
        _, booked = self.request("POST", "/api/consultations", self.book_payload(),
                                 {"X-Role": "coordinator"})
        cid = booked["consultation_id"]

        status, _ = self.request("POST", f"/api/consultations/{cid}/phase",
                                 {"critical": True}, {"X-Role": "clinician"})
        self.assertEqual(status, 200)

        status, body = self.request("POST", "/api/consents/P001/revoke", {},
                                    {"X-Role": "coordinator"})
        self.assertEqual(status, 200)
        self.assertEqual(body["effects"][0]["action"], "deferred")

        status, view = self.request("GET", f"/api/consultations/{cid}",
                                    headers={"X-Role": "coordinator"})
        self.assertEqual(view["status"], "confirmed")

        status, _ = self.request("POST", f"/api/consultations/{cid}/phase",
                                 {"critical": False}, {"X-Role": "clinician"})
        self.assertEqual(status, 200)
        status, view = self.request("GET", f"/api/consultations/{cid}",
                                    headers={"X-Role": "coordinator"})
        self.assertEqual(view["status"], "cancelled")

    def test_engineer_view_masks_patient_identity(self) -> None:
        self.seed()
        _, booked = self.request("POST", "/api/consultations", self.book_payload(),
                                 {"X-Role": "coordinator"})
        cid = booked["consultation_id"]
        _, engineer = self.request("GET", f"/api/consultations/{cid}",
                                   headers={"X-Role": "link_engineer"})
        self.assertTrue(engineer["patient"]["patient_id"].startswith("****"))
        self.assertNotIn("consent_state", engineer)

    def test_concurrent_http_booking_only_one_succeeds(self) -> None:
        self.seed()
        results = []
        barrier = threading.Barrier(6)

        def attempt(i: int) -> tuple:
            barrier.wait()
            return self.request(
                "POST", "/api/consultations", self.book_payload(),
                {"X-Role": "coordinator", "Idempotency-Key": f"http-race-{i}"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(attempt, i) for i in range(6)]
            for f in as_completed(futures):
                results.append(f.result())

        created = [r for r in results if r[0] == 201]
        conflicts = [r for r in results if r[0] == 409]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(conflicts), 5)
        self.assertTrue(all(r[1]["error"]["code"] in ("slot_occupied", "slot_overlap")
                            for r in conflicts))

    def test_idempotency_key_replay_returns_same_consultation(self) -> None:
        self.seed()
        s1, b1 = self.request("POST", "/api/consultations", self.book_payload(),
                              {"X-Role": "coordinator", "Idempotency-Key": "idem-9"})
        s2, b2 = self.request("POST", "/api/consultations", self.book_payload(),
                              {"X-Role": "coordinator", "Idempotency-Key": "idem-9"})
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(b1["consultation_id"], b2["consultation_id"])

    def test_outbox_deliver_endpoint(self) -> None:
        self.seed()
        self.request("POST", "/api/consultations", self.book_payload(),
                     {"X-Role": "coordinator"})
        status, body = self.request("POST", "/api/outbox/deliver", {"limit": 10},
                                    {"X-Role": "coordinator"})
        self.assertEqual(status, 200)
        self.assertEqual(body["stats"]["sent"], 1)
        types = [e["event_type"] for e in body["delivered"]]
        self.assertIn("consultation.confirmed", types)

        # 非审计员不能查看发件箱明细
        status, _ = self.request("GET", "/api/outbox", headers={"X-Role": "coordinator"})
        self.assertEqual(status, 403)
        status, rows = self.request("GET", "/api/outbox", headers={"X-Role": "auditor"})
        self.assertEqual(status, 200)
        self.assertEqual(rows[0]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
