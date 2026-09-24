"""跨进程重启测试：真实启动 HTTP 服务进程，强杀后用同一数据库重启。

证明：
- 进程退出再启动不会重复占用时段，也不会丢失已确认会诊/待发通知；
- 重启后待发通知仍可幂等补发且只投递一次；
- 崩溃残留 sending 状态在新进程启动时回收。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(base: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("服务未在预期时间内启动")


def _req(base: str, method: str, path: str, body: dict | None = None,
         headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ServerProcess:
    def __init__(self, db_path: str, port: int) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "telemedicine_continuity.main",
             "--host", "127.0.0.1", "--port", str(port), "--db", db_path],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base = f"http://127.0.0.1:{port}"
        _wait_health(self.base)

    def kill9(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _seed(base: str) -> None:
    for org_id, name in [("H-LEAD", "牵头医院"), ("C-01", "社区中心")]:
        assert _req(base, "POST", "/api/orgs",
                    {"org_id": org_id, "name": name},
                    {"X-Role": "coordinator"})[0] == 201
    assert _req(base, "POST", "/api/clinicians",
                {"clinician_id": "D001", "name": "张医生", "org_id": "H-LEAD"},
                {"X-Role": "coordinator"})[0] == 201
    assert _req(base, "POST", "/api/patients",
                {"patient_id": "P001", "name": "李四",
                 "national_id": "110101199001011234", "contact_phone": "13800001234"},
                {"X-Role": "coordinator"})[0] == 201
    for link_id, name, domain in [
        ("LINK-A1", "政务专网", "FD-A"),
        ("LINK-B1", "5G 互联网", "FD-B"),
    ]:
        assert _req(base, "POST", "/api/links",
                    {"link_id": link_id, "name": name, "fault_domain": domain,
                     "grade": "HD_VIDEO", "org_ids": ["H-LEAD", "C-01"]},
                    {"X-Role": "coordinator"})[0] == 201
        assert _req(base, "POST", f"/api/links/{link_id}/snapshots",
                    {"health": "up", "latency_ms": 40, "loss_rate": 0.0},
                    {"X-Role": "link_engineer"})[0] == 200
    assert _req(base, "POST", "/api/consents",
                {"patient_id": "P001", "valid_from": "2026-09-01T00:00:00+00:00",
                 "valid_until": "2026-12-31T23:59:59+00:00"},
                {"X-Role": "coordinator"})[0] == 201
    assert _req(base, "POST", "/api/slots",
                {"slot_id": "S001", "clinician_id": "D001",
                 "start_time": "2026-09-24T10:00:00+00:00",
                 "end_time": "2026-09-24T11:00:00+00:00"},
                {"X-Role": "coordinator"})[0] == 201


BOOK = {
    "patient_id": "P001", "clinician_id": "D001", "slot_id": "S001",
    "org_ids": ["H-LEAD", "C-01"], "min_grade": "SD_VIDEO",
    "required_link_count": 2,
}


class RestartDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "restart.db")
        self.port = _free_port()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_kill9_and_restart_keeps_occupation_and_delivers_once(self) -> None:
        server = ServerProcess(self.db_path, self.port)
        try:
            _seed(server.base)
            status, booked = _req(server.base, "POST", "/api/consultations", BOOK,
                                  {"X-Role": "coordinator", "Idempotency-Key": "k1"})
            self.assertEqual(status, 201)
            cid = booked["consultation_id"]
            # 通知尚未投递即强杀进程（模拟宕机/断电）
            server.kill9()
        finally:
            if server.proc.poll() is None:
                server.stop()

        # 同库重启
        server2 = ServerProcess(self.db_path, _free_port())
        try:
            # 已确认会诊与时段占用完好无损
            status, view = _req(server2.base, "GET", f"/api/consultations/{cid}",
                                headers={"X-Role": "coordinator"})
            self.assertEqual(status, 200)
            self.assertEqual(view["status"], "confirmed")

            status, slots = _req(server2.base, "GET", "/api/slots",
                                 headers={"X-Role": "coordinator"})
            self.assertEqual(status, 200)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0]["status"], "occupied")

            # 重启后重复排班必须冲突，不能产生第二个占用
            status, err = _req(server2.base, "POST", "/api/consultations", BOOK,
                               {"X-Role": "coordinator", "Idempotency-Key": "k2"})
            self.assertEqual(status, 409)

            # 相同幂等键重放仍返回同一会诊
            status, replay = _req(server2.base, "POST", "/api/consultations", BOOK,
                                  {"X-Role": "coordinator", "Idempotency-Key": "k1"})
            self.assertEqual(status, 201)
            self.assertEqual(replay["consultation_id"], cid)

            # 待发通知补发，且只有一条确认通知
            status, delivery = _req(server2.base, "POST", "/api/outbox/deliver",
                                    {"limit": 50}, {"X-Role": "coordinator"})
            self.assertEqual(status, 200)
            confirmed = [e for e in delivery["delivered"]
                         if e["event_type"] == "consultation.confirmed"]
            self.assertEqual(len(confirmed), 1)
            self.assertEqual(confirmed[0]["aggregate_id"], cid)

            # 再补一轮，没有重复投递
            status, delivery2 = _req(server2.base, "POST", "/api/outbox/deliver",
                                     {"limit": 50}, {"X-Role": "coordinator"})
            self.assertEqual(delivery2["stats"]["sent"], 0)
        finally:
            server2.stop()

    def test_crash_during_sending_is_reclaimed_by_next_process(self) -> None:
        server = ServerProcess(self.db_path, self.port)
        try:
            _seed(server.base)
            status, booked = _req(server.base, "POST", "/api/consultations", BOOK,
                                  {"X-Role": "coordinator", "Idempotency-Key": "k1"})
            self.assertEqual(status, 201)
            server.kill9()
        finally:
            if server.proc.poll() is None:
                server.stop()

        # 手工把事件置为 sending，模拟 claim 之后、置 sent 之前崩溃
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE outbox SET status='sending', attempts=1")
        conn.commit()
        conn.close()

        server2 = ServerProcess(self.db_path, _free_port())
        try:
            # 启动恢复把 sending 回收为 pending，补发成功
            status, delivery = _req(server2.base, "POST", "/api/outbox/deliver",
                                    {"limit": 50}, {"X-Role": "coordinator"})
            self.assertEqual(status, 200)
            self.assertEqual(delivery["stats"]["sent"], 1)
            status, rows = _req(server2.base, "GET", "/api/outbox",
                                headers={"X-Role": "auditor"})
            self.assertEqual(rows[0]["status"], "sent")
            self.assertEqual(rows[0]["attempts"], 2)  # 崩溃前 1 次 + 补发 1 次
        finally:
            server2.stop()


if __name__ == "__main__":
    unittest.main()
