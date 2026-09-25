#!/usr/bin/env python3
"""本地接口演示：通过真实 HTTP 请求验证编排中心的关键流程。

运行：python3 scripts/demo_api.py

场景：
1. 并发排班 —— 同一时段同一医生只应成功一个会诊；
2. 故障域联动 —— 主链路劣化自动切换备链路，双域故障则暂停；
3. 授权撤销 —— 患者同意失效强制取消（人工锁定与关键阶段也不例外）；
4. 进程重启 —— 占用不重复、幂等键重放收敛、发件箱补发不重复。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemedicine_continuity.app import build_app
from telemedicine_continuity.interfaces.http_api import create_server

UTC = timezone.utc
NOW = datetime.now(UTC)
SLOT_START = NOW + timedelta(hours=2)
SLOT_END = NOW + timedelta(hours=3)


def call(port: int, method: str, path: str, body: dict | None = None,
         role: str = "admin", headers: dict | None = None) -> tuple[int, dict]:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=payload, method=method,
        headers={"Content-Type": "application/json"},
    )
    req.add_header("X-Role", role)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def start_server(db_path: str):
    ctx = build_app(db_path)
    server = create_server("127.0.0.1", 0, ctx.api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return ctx, server, server.server_address[1]


def stop_server(ctx, server) -> None:
    server.shutdown()
    server.server_close()
    ctx.store.close()


def seed(port: int) -> None:
    call(port, "POST", "/v1/institutions", {"id": "inst-a", "name": "中心医院", "tier": "tertiary"}, role="ops")
    call(port, "POST", "/v1/institutions", {"id": "inst-b", "name": "社区医院", "tier": "community"}, role="ops")
    call(port, "POST", "/v1/links", {"id": "link-a-primary", "institution_id": "inst-a",
                                     "kind": "PRIMARY", "failure_domain": "domain-1"}, role="ops")
    call(port, "POST", "/v1/links", {"id": "link-a-backup", "institution_id": "inst-a",
                                     "kind": "BACKUP", "failure_domain": "domain-2"}, role="ops")
    for link in ("link-a-primary", "link-a-backup"):
        call(port, "POST", f"/v1/links/{link}/probes",
             {"latency_ms": 20, "loss_pct": 0.1, "availability": 0.9999}, role="ops")
    for inst in ("inst-a", "inst-b"):
        call(port, "POST", "/v1/authorizations",
             {"id": f"auth-{inst}", "kind": "INSTITUTION", "subject_id": inst,
              "valid_from": (NOW - timedelta(days=1)).isoformat(),
              "valid_until": (NOW + timedelta(days=30)).isoformat()}, role="admin")
    call(port, "POST", "/v1/authorizations",
         {"id": "consent-1", "kind": "PATIENT_CONSENT", "subject_id": "pat-1",
          "valid_from": (NOW - timedelta(days=1)).isoformat(),
          "valid_until": (NOW + timedelta(days=30)).isoformat(),
          "payload": {"form": "signed-001"}}, role="clinician")


def consultation_body() -> dict:
    return {
        "slot_start": SLOT_START.isoformat(),
        "slot_end": SLOT_END.isoformat(),
        "institution_ids": ["inst-a", "inst-b"],
        "doctor_id": "doc-1",
        "equipment_id": "equip-1",
        "patient": {"ref": "pat-1", "name": "张三", "medical_record_no": "MRN-001"},
        "min_service_level": "GOLD",
        "consent_id": "consent-1",
        "primary_link_id": "link-a-primary",
        "backup_link_id": "link-a-backup",
    }


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    db_path = str(Path(tmp.name) / "orchestrator.db")

    ctx, server, port = start_server(db_path)
    print(f"服务已启动: http://127.0.0.1:{port} (db={db_path})")
    seed(port)

    # ---------- 1. 并发排班 ----------
    def attempt(i: int):
        return call(port, "POST", "/v1/consultations", consultation_body(),
                    role="scheduler", headers={"Idempotency-Key": f"race-{i}"})

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    summary = {"成功": sum(1 for s, _ in results if s == 201),
               "冲突拒绝": sum(1 for s, b in results if s == 409 and b["error"]["code"] == "SLOT_CONFLICT")}
    show("1. 并发排班：6 个线程抢同一时段同一医生", summary)
    assert summary == {"成功": 1, "冲突拒绝": 5}

    # 幂等重放
    _, first = call(port, "POST", "/v1/consultations", consultation_body(),
                    role="scheduler", headers={"Idempotency-Key": "race-0"})
    show("1b. 幂等键重放", first["data"])
    assert first["data"]["replayed"] is True
    cid = first["data"]["consultation_id"]

    # ---------- 2. 故障域联动 ----------
    _, resp = call(port, "POST", "/v1/links/link-a-primary/probes",
                   {"latency_ms": 900, "loss_pct": 20.0, "availability": 0.5}, role="ops")
    show("2. 主链路(domain-1)劣化 → 自动切换备链路(domain-2)", resp["data"]["reevaluated"])
    _, view = call(port, "GET", f"/v1/consultations/{cid}", role="ops")
    assert view["data"]["links"]["active_link_id"] == "link-a-backup"

    _, resp = call(port, "POST", "/v1/links/link-a-backup/probes",
                   {"latency_ms": 900, "loss_pct": 20.0, "availability": 0.5}, role="ops")
    _, view = call(port, "GET", f"/v1/consultations/{cid}", role="ops")
    show("2b. 两个故障域均不可用 → 会诊暂停", {"status": view["data"]["status"],
                                               "pause_reason": view["data"]["pause_reason"]})
    assert view["data"]["status"] == "PAUSED"

    _, resp = call(port, "POST", "/v1/links/link-a-primary/probes",
                   {"latency_ms": 20, "loss_pct": 0.1, "availability": 0.9999}, role="ops")
    _, view = call(port, "GET", f"/v1/consultations/{cid}", role="ops")
    show("2c. 主链路恢复 → 自动恢复", {"status": view["data"]["status"],
                                       "active_link": view["data"]["links"]["active_link_id"]})
    assert view["data"]["status"] == "CONFIRMED"

    # ---------- 3. 授权撤销（锁定 + 关键阶段也拦不住） ----------
    call(port, "POST", f"/v1/consultations/{cid}/actions", {"action": "start"}, role="scheduler")
    call(port, "POST", f"/v1/consultations/{cid}/actions",
         {"action": "set_phase", "phase": "CRITICAL"}, role="clinician")
    call(port, "POST", f"/v1/consultations/{cid}/actions",
         {"action": "lock", "reason": "专家操作中"}, role="ops")
    _, resp = call(port, "POST", "/v1/authorizations/consent-1/revoke", role="clinician")
    _, view = call(port, "GET", f"/v1/consultations/{cid}", role="admin")
    show("3. 患者同意撤销（关键阶段+人工锁定中）→ 强制取消",
         {"affected": resp["data"]["affected"], "status": view["data"]["status"],
          "cancel_reason": view["data"]["cancel_reason"]})
    assert view["data"]["status"] == "CANCELLED"

    # 字段级安全：排班员视图不含患者姓名
    _, view = call(port, "GET", f"/v1/consultations/{cid}", role="scheduler")
    show("3b. 排班员视图（患者姓名/病历号不可见）", view["data"])
    assert "patient" not in view["data"]

    # ---------- 4. 进程重启 ----------
    # 重启前再排一个仍在占用资源的会诊（原会诊已取消，资源已释放）
    call(port, "POST", "/v1/links/link-a-backup/probes",
         {"latency_ms": 20, "loss_pct": 0.1, "availability": 0.9999}, role="ops")
    call(port, "POST", "/v1/authorizations",
         {"id": "consent-2", "kind": "PATIENT_CONSENT", "subject_id": "pat-2",
          "valid_from": (NOW - timedelta(days=1)).isoformat(),
          "valid_until": (NOW + timedelta(days=30)).isoformat()}, role="clinician")
    body2 = consultation_body() | {"consent_id": "consent-2",
                                   "patient": {"ref": "pat-2", "name": "李四",
                                               "medical_record_no": "MRN-002"}}
    status, resp = call(port, "POST", "/v1/consultations", body2,
                        role="scheduler", headers={"Idempotency-Key": "hold-2"})
    assert status == 201, resp
    cid2 = resp["data"]["consultation_id"]

    stop_server(ctx, server)
    print("\n--- 进程退出，使用同一数据库重新启动 ---")
    ctx2, server2, port2 = start_server(db_path)
    show("4. 启动恢复报告", ctx2.recovery_report)

    status, conflict = call(port2, "POST", "/v1/consultations", body2,
                            role="scheduler", headers={"Idempotency-Key": "after-restart"})
    show("4b. 重启后重叠时段排班仍被拒绝（占用记录随库持久化）", conflict["error"])
    assert status == 409 and conflict["error"]["code"] == "SLOT_CONFLICT"

    _, replay = call(port2, "POST", "/v1/consultations", body2,
                     role="scheduler", headers={"Idempotency-Key": "hold-2"})
    show("4c. 重启后幂等键重放仍收敛到原会诊（不产生重复占用）", replay["data"])
    assert replay["data"]["replayed"] is True
    assert replay["data"]["consultation_id"] == cid2

    _, dispatched = call(port2, "POST", "/v1/outbox/dispatch", {}, role="ops")
    _, events = call(port2, "GET", "/v1/outbox/events", role="ops")
    show("4d. 重启后发件箱补发（幂等）", {"dispatch": dispatched["data"],
                                          "events": events["data"]["events"]})
    _, dispatched2 = call(port2, "POST", "/v1/outbox/dispatch", {}, role="ops")
    assert dispatched2["data"]["dispatched"] == 0, "已送达事件不得重复派发"

    stop_server(ctx2, server2)
    print("\n全部演示场景通过。")


if __name__ == "__main__":
    main()
