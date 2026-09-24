"""测试公共支撑：可控时钟 + 临时数据库 + 标准医联体场景。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telemedicine_continuity.application.notification import InProcessTransport, OutboxRelay
from telemedicine_continuity.application.service import OrchestrationService
from telemedicine_continuity.domain.enums import Grade, Health, Role
from telemedicine_continuity.infra.clock import MutableClock
from telemedicine_continuity.infra.db import Database

HOSPITAL = "H-LEAD"
COMMUNITY = "C-01"
CLINICIAN = "D001"
PATIENT = "P001"
# 两条故障域相互独立的链路 + 一条与主链路同域的高风险链路
LINK_A = "LINK-A1"
LINK_B = "LINK-B1"
LINK_A2 = "LINK-A2"


class ServiceTestBed(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.clock = MutableClock()
        self.db = Database(self.db_path)
        self.transport = InProcessTransport()
        self.service = OrchestrationService(self.db, self.clock)
        self.relay = OutboxRelay(self.db, self.transport)
        self._scenario_seeded = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -------------------------------------------------- 场景装配
    def seed_scenario(
        self,
        *,
        links: bool = True,
        snapshots: bool = True,
        consent_until: str = "2026-12-31T23:59:59+00:00",
        slot: str | None = "S001",
    ) -> None:
        s = self.service
        s.register_org(Role.COORDINATOR, HOSPITAL, "牵头三甲医院")
        s.register_org(Role.COORDINATOR, COMMUNITY, "城东社区卫生服务中心")
        s.register_clinician(Role.COORDINATOR, CLINICIAN, "张医生", HOSPITAL)
        s.register_patient(Role.COORDINATOR, PATIENT, "李四", "110101199001011234",
                           "13800001234")
        if links:
            s.register_link(Role.COORDINATOR, link_id=LINK_A, name="政务专网主链路",
                            fault_domain="FD-A", grade=Grade.HD_VIDEO,
                            org_ids=[HOSPITAL, COMMUNITY])
            s.register_link(Role.COORDINATOR, link_id=LINK_B, name="5G 互联网备链路",
                            fault_domain="FD-B", grade=Grade.HD_VIDEO,
                            org_ids=[HOSPITAL, COMMUNITY])
            s.register_link(Role.COORDINATOR, link_id=LINK_A2, name="专网第二条虚链路",
                            fault_domain="FD-A", grade=Grade.SD_VIDEO,
                            org_ids=[HOSPITAL, COMMUNITY])
        if snapshots:
            for link_id in (LINK_A, LINK_B, LINK_A2):
                s.report_snapshot(Role.LINK_ENGINEER, link_id,
                                  health=Health.UP.value, latency_ms=40, loss_rate=0.0,
                                  snapshot_at=self.clock.now_iso())
        s.grant_consent(Role.COORDINATOR, PATIENT,
                        valid_from="2026-09-01T00:00:00+00:00",
                        valid_until=consent_until)
        if slot:
            s.create_slot(Role.COORDINATOR, slot_id=slot, clinician_id=CLINICIAN,
                          start_time="2026-09-24T10:00:00+00:00",
                          end_time="2026-09-24T11:00:00+00:00")
        self.slot_id = slot
        self._scenario_seeded = True

    def book(
        self,
        *,
        slot_id: str | None = None,
        min_grade: str | int = Grade.SD_VIDEO,
        required_link_count: int = 2,
        candidate_link_ids: list[str] | None = None,
        idem_key: str | None = "book-1",
        patient_id: str = PATIENT,
        clinician_id: str = CLINICIAN,
        org_ids: list[str] | None = None,
    ):
        return self.service.book_consultation(
            Role.COORDINATOR,
            patient_id=patient_id,
            clinician_id=clinician_id,
            slot_id=slot_id or self.slot_id,
            org_ids=org_ids or [HOSPITAL, COMMUNITY],
            min_grade=min_grade,
            required_link_count=required_link_count,
            candidate_link_ids=candidate_link_ids,
            idem_key=idem_key,
        )

    def snapshot(self, link_id: str, health: str, *, stale: bool = False,
                 latency_ms: int | None = 40) -> dict:
        if stale:
            from datetime import timedelta
            from telemedicine_continuity.infra.clock import to_iso
            at = to_iso(self.clock.now() - timedelta(minutes=10))
        else:
            at = self.clock.now_iso()
        return self.service.report_snapshot(
            Role.LINK_ENGINEER, link_id, health=health,
            latency_ms=latency_ms, loss_rate=0.01, snapshot_at=at)

    def deliver(self):
        return self.relay.deliver_pending()
