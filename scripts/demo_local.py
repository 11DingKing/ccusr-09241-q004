#!/usr/bin/env python3
"""本地接口请求演示：启动编排中心并通过 HTTP 走完全部关键流程。

运行：
    python3 scripts/demo_local.py

演示内容：
    1) 基础数据装配（机构/医生/患者/链路/探测快照/授权/时段）
    2) 并发排班（多线程同时请求，仅一单成功）
    3) 故障域联动（主链路故障域中断 -> 自动切换；恢复后备选指针刷新）
    4) 授权撤销（正常阶段立即取消；关键阶段延后取消；人工锁定跳过自动联动）
    5) 事务发件箱补发（失败 -> 重试，仅通知一次）
    6) 字段级权限（链路工程师视角的患者信息脱敏）
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "var" / "demo.db"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(base: str) -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("服务启动超时")


def req(base: str, method: str, path: str, body: dict | None = None,
        role: str | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if role:
        hdrs["X-Role"] = role
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> int:
    if DB_PATH.exists():
        shutil.rmtree(DB_PATH.parent, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "telemedicine_continuity.main",
         "--host", "127.0.0.1", "--port", str(port), "--db", str(DB_PATH)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        wait_health(base)
        print(f"编排中心已启动：{base}（数据库 {DB_PATH}）")

        COORD = {"X-Role": "coordinator"}

        # 1) 基础数据
        for org_id, name in [("H-LEAD", "牵头三甲医院"), ("C-01", "城东社区卫生服务中心")]:
            show("注册机构", *req(base, "POST", "/api/orgs",
                                 {"org_id": org_id, "name": name}, role="coordinator"))
        show("注册医生", *req(base, "POST", "/api/clinicians",
                             {"clinician_id": "D001", "name": "张医生",
                              "org_id": "H-LEAD"}, role="coordinator"))
        show("注册患者", *req(base, "POST", "/api/patients",
                             {"patient_id": "P001", "name": "李四",
                              "national_id": "110101199001011234",
                              "contact_phone": "13800001234"}, role="coordinator"))
        for link_id, name, domain in [
            ("LINK-A1", "政务专网主链路", "FD-A"),
            ("LINK-A2", "专网第二条虚链路（同域高风险）", "FD-A"),
            ("LINK-B1", "5G 互联网备链路", "FD-B"),
        ]:
            show(f"注册链路 {link_id}", *req(base, "POST", "/api/links", {
                "link_id": link_id, "name": name, "fault_domain": domain,
                "grade": "HD_VIDEO", "org_ids": ["H-LEAD", "C-01"]}, role="coordinator"))
            show(f"上报快照 {link_id}", *req(
                base, "POST", f"/api/links/{link_id}/snapshots",
                {"health": "up", "latency_ms": 40, "loss_rate": 0.0},
                role="link_engineer"))
        show("登记患者授权（有效期至 2026-12-31）", *req(
            base, "POST", "/api/consents",
            {"patient_id": "P001", "valid_from": "2026-09-01T00:00:00+00:00",
             "valid_until": "2026-12-31T23:59:59+00:00"}, role="coordinator"))
        show("创建两个同一医生时间重叠的时段", *req(
            base, "POST", "/api/slots",
            {"slot_id": "S001", "clinician_id": "D001",
             "start_time": "2026-09-24T10:00:00+00:00",
             "end_time": "2026-09-24T11:00:00+00:00"}, role="coordinator"))

        # 2) 并发排班：6 个请求同时打同一个时段
        payload = {"patient_id": "P001", "clinician_id": "D001", "slot_id": "S001",
                   "org_ids": ["H-LEAD", "C-01"], "min_grade": "SD_VIDEO",
                   "required_link_count": 2}
        barrier = threading.Barrier(6)

        def attempt(i: int):
            barrier.wait()
            return req(base, "POST", "/api/consultations", payload,
                       headers={"X-Role": "coordinator", "Idempotency-Key": f"race-{i}"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = [f.result() for f in as_completed(
                [pool.submit(attempt, i) for i in range(6)])]
        ok = [r for r in results if r[0] == 201]
        conflict = [r for r in results if r[0] == 409]
        print(f"\n=== 并发排班：成功 {len(ok)} 单，冲突 {len(conflict)} 单 ===")
        assert len(ok) == 1 and len(conflict) == 5
        cid = ok[0][1]["consultation_id"]
        print(f"已确认会诊：{ok[0][1]['code']}，主链路 {ok[0][1]['active_link_id']}，"
              f"备链路 {ok[0][1]['backup_link_id']}")

        # 3) 故障域联动：FD-A 中断
        show("FD-A 故障域中断（LINK-A1 探测 down）", *req(
            base, "POST", "/api/links/LINK-A1/snapshots", {"health": "down"},
            role="link_engineer"))
        show("会诊当前状态", *req(base, "GET", f"/api/consultations/{cid}",
                                  role="coordinator"))

        # LINK-A1 恢复：B 仍健康，仅刷新备选指针
        show("LINK-A1 探测恢复 up", *req(
            base, "POST", "/api/links/LINK-A1/snapshots",
            {"health": "up", "latency_ms": 35}, role="link_engineer"))
        show("会诊当前状态（备选指针刷新为 LINK-A1）",
             *req(base, "GET", f"/api/consultations/{cid}", role="coordinator"))

        # 4) 人工锁定后故障联动必须跳过
        show("排班员 scheduler-li 锁定会诊", *req(
            base, "POST", f"/api/consultations/{cid}/lock", {},
            headers={"X-Role": "coordinator", "X-Actor-Id": "scheduler-li"}))
        show("锁定期间 LINK-B1 中断：自动联动跳过", *req(
            base, "POST", "/api/links/LINK-B1/snapshots", {"health": "down"},
            role="link_engineer"))
        show("其他排班员尝试切换被拒", *req(
            base, "POST", f"/api/consultations/{cid}/switch", {},
            headers={"X-Role": "coordinator", "X-Actor-Id": "scheduler-wang"}))
        show("锁定者人工切换到 LINK-A1", *req(
            base, "POST", f"/api/consultations/{cid}/switch", {},
            headers={"X-Role": "coordinator", "X-Actor-Id": "scheduler-li"}))
        show("解除锁定", *req(
            base, "POST", f"/api/consultations/{cid}/unlock", {},
            headers={"X-Role": "coordinator", "X-Actor-Id": "scheduler-li"}))

        # 5) 关键操作阶段 + 授权撤销：延后取消
        show("医生标记进入关键操作阶段", *req(
            base, "POST", f"/api/consultations/{cid}/phase",
            {"critical": True}, role="clinician"))
        show("撤销患者授权（关键阶段：延后取消）", *req(
            base, "POST", "/api/consents/P001/revoke", {}, role="coordinator"))
        show("关键阶段内会诊仍在进行",
             *req(base, "GET", f"/api/consultations/{cid}", role="coordinator"))
        show("关键操作阶段结束 -> 延后取消落地", *req(
            base, "POST", f"/api/consultations/{cid}/phase",
            {"critical": False}, role="clinician"))
        show("会诊终态", *req(base, "GET", f"/api/consultations/{cid}",
                              role="coordinator"))

        # 6) 发件箱补发
        show("发件箱补发全部通知", *req(base, "POST", "/api/outbox/deliver",
                                        {"limit": 100}, role="coordinator"))
        show("再次补发（无重复投递）", *req(base, "POST", "/api/outbox/deliver",
                                           {"limit": 100}, role="coordinator"))
        show("审计日志（审计员视角）", *req(base, "GET", "/api/audit",
                                           role="auditor"))

        # 7) 字段级权限：再造一个会诊给链路工程师看
        req(base, "POST", "/api/links/LINK-B1/snapshots",
            {"health": "up", "latency_ms": 42, "loss_rate": 0.0},
            role="link_engineer")
        req(base, "POST", "/api/consents",
            {"patient_id": "P001", "valid_from": "2026-09-01T00:00:00+00:00",
             "valid_until": "2027-12-31T23:59:59+00:00"}, role="coordinator")
        req(base, "POST", "/api/slots",
            {"slot_id": "S002", "clinician_id": "D001",
             "start_time": "2026-09-25T10:00:00+00:00",
             "end_time": "2026-09-25T11:00:00+00:00"}, role="coordinator")
        _, second = req(base, "POST", "/api/consultations",
                        {**payload, "slot_id": "S002"},
                        headers={"X-Role": "coordinator", "Idempotency-Key": "c2"})
        show("链路工程师视角（患者标识脱敏、无授权明细）",
             *req(base, "GET", f"/api/consultations/{second['consultation_id']}",
                  role="link_engineer"))

        print("\n演示完成。数据库保留在", DB_PATH)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
