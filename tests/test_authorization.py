"""授权撤销：患者同意失效强制取消；机构授权失效按常规条件变化处理。"""

from __future__ import annotations

from datetime import timedelta

from telemedicine_continuity.domain.enums import HoldStatus

from helpers import OrchestratorTestCase, T0, schedule_ok


class AuthorizationRevocationTests(OrchestratorTestCase):
    def test_consent_revocation_forces_cancel_and_releases_holds(self) -> None:
        result = schedule_ok(self.ctx)
        cid = result["consultation_id"]
        self.ctx.orchestrator.perform_action(cid, "start", "scheduler-1")

        report = self.ctx.orchestrator.revoke_authorization("consent-1")

        c = self.ctx.orchestrator.get_consultation(cid)
        self.assertEqual(c.status.value, "CANCELLED")
        self.assertEqual(c.cancel_reason, "CONSENT_INVALID")
        self.assertEqual(self.ctx.store.count_holds(HoldStatus.HELD), 0, "取消后占用必须释放")
        self.assertEqual(report["affected"][0]["reason"], "CONSENT_INVALID")
        event_types = [e.type for e in self.ctx.store.list_outbox_events(cid)]
        self.assertIn("CONSENT_INVALID_CANCELLED", event_types)
        self.assertIn("CONSULTATION_CANCELLED", event_types)

    def test_consent_revocation_overrides_lock_and_critical_phase(self) -> None:
        """患者同意失效的规则凌驾于人工锁定与关键操作阶段之上。"""
        result = schedule_ok(self.ctx)
        cid = result["consultation_id"]
        orch = self.ctx.orchestrator
        orch.perform_action(cid, "start", "scheduler-1")
        orch.perform_action(cid, "set_phase", "doctor-1", phase="CRITICAL")
        orch.perform_action(cid, "lock", "ops-1", reason="专家正在操作")

        orch.revoke_authorization("consent-1")

        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "CANCELLED")
        self.assertEqual(c.cancel_reason, "CONSENT_INVALID")

    def test_consent_expiry_triggers_cancel_on_reevaluate(self) -> None:
        orch = self.ctx.orchestrator
        orch.grant_authorization(
            "consent-expiring", "PATIENT_CONSENT", "pat-1", "telemedicine",
            T0 - timedelta(hours=1), T0 + timedelta(hours=3),
        )
        result = schedule_ok(self.ctx, consent_id="consent-expiring")
        cid = result["consultation_id"]

        self.clock.advance(hours=4)  # 越过同意有效期
        orch.reevaluate(cid, trigger="clock-check")

        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "CANCELLED")
        self.assertEqual(c.cancel_reason, "CONSENT_INVALID")

    def test_institution_auth_revoked_pauses_then_resume_on_regrant(self) -> None:
        """机构授权失效属于常规条件变化：暂停而非取消，授权恢复后可继续。"""
        result = schedule_ok(self.ctx)
        cid = result["consultation_id"]
        orch = self.ctx.orchestrator

        orch.revoke_authorization("auth-inst-b")
        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "PAUSED")
        self.assertEqual(c.pause_reason, "INSTITUTION_AUTH_LAPSED")

        orch.grant_authorization(
            "auth-inst-b-2", "INSTITUTION", "inst-b", "telemedicine",
            T0 - timedelta(days=1), T0 + timedelta(days=30),
        )
        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "CONFIRMED", "机构授权恢复后应自动回到已确认状态")

    def test_institution_auth_lapse_in_critical_phase_escalates_only(self) -> None:
        result = schedule_ok(self.ctx)
        cid = result["consultation_id"]
        orch = self.ctx.orchestrator
        orch.perform_action(cid, "start", "scheduler-1")
        orch.perform_action(cid, "set_phase", "doctor-1", phase="CRITICAL")

        orch.revoke_authorization("auth-inst-b")

        c = orch.get_consultation(cid)
        self.assertEqual(c.status.value, "ACTIVE", "关键阶段不自动暂停")
        event_types = [e.type for e in self.ctx.store.list_outbox_events(cid)]
        self.assertIn("RISK_ESCALATION", event_types)


if __name__ == "__main__":
    import unittest

    unittest.main()
