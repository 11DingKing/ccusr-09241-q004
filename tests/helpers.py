"""测试基础设施：可替换端口的测试实现与夹具工厂。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telemedicine_continuity.app import AppContext, build_app
from telemedicine_continuity.application.ports import NotificationError

UTC = timezone.utc
T0 = datetime(2026, 9, 25, 8, 0, 0, tzinfo=UTC)


class ManualClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


class SeqIds:
    def __init__(self) -> None:
        self._n = 0

    def new_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:05d}"


class FakeNotifier:
    """记录发送历史，可注入失败；按 event_id 幂等去重。"""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str, dict]] = []
        self._seen: set[str] = set()
        self.fail_next = 0

    def send(self, event_id: str, event_type: str, payload: dict) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise NotificationError("模拟发送失败")
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        self.delivered.append((event_id, event_type, payload))


def make_ctx(tmp: str, clock: ManualClock | None = None) -> tuple[AppContext, ManualClock, FakeNotifier]:
    clock = clock or ManualClock()
    notifier = FakeNotifier()
    ctx = build_app(
        Path(tmp) / "test.db",
        clock=clock,
        ids=SeqIds(),
        notifier=notifier,
    )
    return ctx, clock, notifier


def seed_network(ctx: AppContext, *, shared_domain: bool = False) -> dict:
    """构造基础网络：两家机构、各自主备链路、良好探测快照、机构授权。"""
    orch = ctx.orchestrator
    orch.register_institution("inst-a", "中心医院", "tertiary")
    orch.register_institution("inst-b", "社区医院", "community")
    orch.register_link("link-a-primary", "inst-a", "PRIMARY", "domain-1")
    orch.register_link("link-a-backup", "inst-a", "BACKUP", "domain-1" if shared_domain else "domain-2")
    for link_id in ("link-a-primary", "link-a-backup"):
        orch.record_probe(link_id, latency_ms=20, loss_pct=0.1, availability=0.9999)
    for inst in ("inst-a", "inst-b"):
        orch.grant_authorization(
            f"auth-{inst}", "INSTITUTION", inst, "telemedicine",
            T0 - timedelta(days=1), T0 + timedelta(days=30),
        )
    return {"primary": "link-a-primary", "backup": "link-a-backup"}


def grant_consent(ctx: AppContext, consent_id: str = "consent-1", patient_ref: str = "pat-1") -> str:
    ctx.orchestrator.grant_authorization(
        consent_id, "PATIENT_CONSENT", patient_ref, "telemedicine",
        T0 - timedelta(days=1), T0 + timedelta(days=30),
        payload={"form": "signed-consent-001", "signer": "患者本人"},
    )
    return consent_id


def schedule_ok(ctx: AppContext, key: str = "key-1", **overrides) -> dict:
    slot_start = T0 + timedelta(hours=2)
    slot_end = T0 + timedelta(hours=3)
    params = dict(
        idempotency_key=key,
        slot_start=slot_start,
        slot_end=slot_end,
        institution_ids=["inst-a", "inst-b"],
        doctor_id="doc-1",
        equipment_id="equip-1",
        patient={"ref": "pat-1", "name": "张三", "medical_record_no": "MRN-001"},
        min_service_level="GOLD",
        consent_id="consent-1",
        primary_link_id="link-a-primary",
        backup_link_id="link-a-backup",
    )
    params.update(overrides)
    return ctx.orchestrator.schedule_consultation(**params)


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx, self.clock, self.notifier = make_ctx(self._tmp.name)
        self.addCleanup(self.ctx.store.close)
        seed_network(self.ctx)
        grant_consent(self.ctx)
