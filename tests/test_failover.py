"""故障域联动：链路劣化触发切换/降级/暂停；关键阶段与人工锁定的差异化规则。"""

from __future__ import annotations

from telemedicine_continuity.domain.errors import InvalidTransition

from helpers import OrchestratorTestCase, schedule_ok

GOOD = {"latency_ms": 20, "loss_pct": 0.1, "availability": 0.9999}
# 低于 GOLD(80ms/0.5%/0.999)，满足 SILVER(150ms/1%/0.995)
SILVER_OK = {"latency_ms": 120, "loss_pct": 0.8, "availability": 0.996}
# 低于 BRONZE(300ms/3%/0.99)
BROKEN = {"latency_ms": 900, "loss_pct": 20.0, "availability": 0.5}


class FailoverTests(OrchestratorTestCase):
    def _confirmed(self) -> str:
        return schedule_ok(self.ctx)["consultation_id"]

    def test_primary_degradation_switches_to_backup(self) -> None:
        cid = self._confirmed()
        self.ctx.orchestrator.record_probe("link-a-primary", **SILVER_OK)

        c = self.ctx.orchestrator.get_consultation(cid)
        self.assertEqual(c.active_link_id, "link-a-backup")
        self.assertEqual(c.status.value, "CONFIRMED")
        event_types = [e.type for e in self.ctx.store.list_outbox_events(cid)]
        self.assertIn("PATH_SWITCHED", event_types)

    def test_both_links_below_full_level_degrades_limited(self) -> None:
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        orch.record_probe("link-a-primary", **SILVER_OK)
        orch.record_probe("link-a-backup", **SILVER_OK)

        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "DEGRADED")
        self.assertEqual(c.current_service_level.value, "SILVER", "GOLD 最多降一档到 SILVER")

    def test_all_links_broken_pauses_consultation(self) -> None:
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        orch.record_probe("link-a-primary", **BROKEN)
        orch.record_probe("link-a-backup", **BROKEN)

        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "PAUSED")
        self.assertEqual(c.pause_reason, "LINK_BELOW_FLOOR")

    def test_link_recovery_resumes_paused_consultation(self) -> None:
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        orch.record_probe("link-a-primary", **BROKEN)
        orch.record_probe("link-a-backup", **BROKEN)
        self.assertEqual(orch.get_consultation(cid).status.value, "PAUSED")

        orch.record_probe("link-a-primary", **GOOD)
        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "CONFIRMED")
        self.assertEqual(c.active_link_id, "link-a-primary")

    def test_critical_phase_blocks_degrade_and_pause(self) -> None:
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        orch.perform_action(cid, "start", "scheduler-1")
        orch.perform_action(cid, "set_phase", "doctor-1", phase="CRITICAL")

        # 主链路劣化、备链路也达不到 GOLD：关键阶段只允许无缝切换，否则升级告警
        orch.record_probe("link-a-primary", **SILVER_OK)
        orch.record_probe("link-a-backup", **SILVER_OK)

        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "ACTIVE", "关键阶段禁止自动降级/暂停")
        event_types = [e.type for e in self.ctx.store.list_outbox_events(cid)]
        self.assertIn("RISK_ESCALATION", event_types)

        with self.assertRaises(InvalidTransition):
            orch.perform_action(cid, "pause", "scheduler-1")
        with self.assertRaises(InvalidTransition):
            orch.perform_action(cid, "cancel", "scheduler-1")
        with self.assertRaises(InvalidTransition):
            orch.perform_action(cid, "degrade", "scheduler-1")

    def test_critical_phase_seamless_switch_allowed(self) -> None:
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        orch.perform_action(cid, "start", "scheduler-1")
        orch.perform_action(cid, "set_phase", "doctor-1", phase="CRITICAL")

        orch.record_probe("link-a-primary", **BROKEN)  # 备链路仍满足 GOLD
        c = orch.get_consultation(cid)
        self.assertEqual(c.active_link_id, "link-a-backup")
        self.assertEqual(c.status.value, "ACTIVE")

    def test_manual_lock_freezes_automatic_transitions(self) -> None:
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        orch.perform_action(cid, "lock", "ops-1", reason="等待家属确认")

        orch.record_probe("link-a-primary", **BROKEN)
        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "CONFIRMED", "锁定期间禁止自动切换")
        self.assertEqual(c.active_link_id, "link-a-primary")
        event_types = [e.type for e in self.ctx.store.list_outbox_events(cid)]
        self.assertIn("MANUAL_REVIEW_REQUIRED", event_types)

        # 解锁后立即重估，积压的条件变化被处理
        orch.perform_action(cid, "unlock", "ops-1")
        c = orch.get_consultation(cid)
        self.assertEqual(c.active_link_id, "link-a-backup")

    def test_failure_domain_outage_affects_all_consultations_in_domain(self) -> None:
        """故障域联动：同一故障域整体失效会波及域内所有链路关联的会诊。"""
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        # domain-1 整体故障：主链路探测恶化
        orch.record_probe("link-a-primary", **BROKEN)
        c = orch.get_consultation(cid)
        self.assertEqual(c.active_link_id, "link-a-backup", "异故障域备链路应接管")

        # domain-2 也故障：两个故障域均不可用，会诊暂停
        orch.record_probe("link-a-backup", **BROKEN)
        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "PAUSED")

    def test_degraded_consultation_restores_when_quality_returns(self) -> None:
        cid = self._confirmed()
        orch = self.ctx.orchestrator
        orch.record_probe("link-a-primary", **SILVER_OK)
        orch.record_probe("link-a-backup", **SILVER_OK)
        self.assertEqual(orch.get_consultation(cid).status.value, "DEGRADED")

        orch.record_probe("link-a-primary", **GOOD)
        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "CONFIRMED")
        self.assertEqual(c.current_service_level.value, "GOLD")


if __name__ == "__main__":
    import unittest

    unittest.main()
