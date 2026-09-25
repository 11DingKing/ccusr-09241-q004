"""准入判断：时段、机构、链路快照、共享风险、服务等级与授权有效期的联合评估。"""

from __future__ import annotations

from datetime import timedelta

from telemedicine_continuity.domain.errors import AdmissionRejected

from helpers import OrchestratorTestCase, T0, grant_consent, make_ctx, schedule_ok, seed_network


class AdmissionTests(OrchestratorTestCase):
    def test_approved_when_all_conditions_met(self) -> None:
        result = schedule_ok(self.ctx)
        self.assertFalse(result["replayed"])
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertFalse(result["shared_risk"])

    def test_shared_failure_domain_rejected_for_gold(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ctx, clock, _ = make_ctx(tmp)
            seed_network(ctx, shared_domain=True)
            grant_consent(ctx)
            with self.assertRaises(AdmissionRejected) as cm:
                schedule_ok(ctx)
            self.assertIn("SHARED_FAILURE_DOMAIN", cm.exception.reasons)
            ctx.store.close()

    def test_shared_failure_domain_only_warns_for_bronze(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ctx, _, _ = make_ctx(tmp)
            seed_network(ctx, shared_domain=True)
            grant_consent(ctx)
            result = schedule_ok(ctx, min_service_level="BRONZE")
            self.assertEqual(result["status"], "CONFIRMED")
            self.assertTrue(result["shared_risk"])
            self.assertIn("SHARED_FAILURE_DOMAIN", result["warnings"])
            ctx.store.close()

    def test_stale_probe_rejected(self) -> None:
        self.clock.advance(minutes=10)  # 快照超出新鲜度窗口
        with self.assertRaises(AdmissionRejected) as cm:
            schedule_ok(self.ctx)
        self.assertIn("PRIMARY_PROBE_STALE", cm.exception.reasons)

    def test_consent_window_must_cover_slot(self) -> None:
        self.ctx.orchestrator.grant_authorization(
            "consent-short", "PATIENT_CONSENT", "pat-1", "telemedicine",
            T0, T0 + timedelta(hours=2, minutes=30),  # 覆盖不到时段结束
        )
        with self.assertRaises(AdmissionRejected) as cm:
            schedule_ok(self.ctx, consent_id="consent-short")
        self.assertIn("CONSENT_WINDOW_INVALID", cm.exception.reasons)

    def test_revoked_consent_rejected(self) -> None:
        self.ctx.orchestrator.revoke_authorization("consent-1")
        with self.assertRaises(AdmissionRejected) as cm:
            schedule_ok(self.ctx)
        self.assertIn("CONSENT_REVOKED", cm.exception.reasons)

    def test_missing_institution_authorization_rejected(self) -> None:
        self.ctx.orchestrator.revoke_authorization("auth-inst-b")
        with self.assertRaises(AdmissionRejected) as cm:
            schedule_ok(self.ctx)
        self.assertIn("INSTITUTION_AUTH_INVALID:inst-b", cm.exception.reasons)

    def test_backup_below_service_level_rejected(self) -> None:
        self.ctx.orchestrator.record_probe(
            "link-a-backup", latency_ms=500, loss_pct=10.0, availability=0.9
        )
        with self.assertRaises(AdmissionRejected) as cm:
            schedule_ok(self.ctx)
        self.assertIn("BACKUP_LINK_BELOW_SERVICE_LEVEL", cm.exception.reasons)

    def test_invalid_slot_window_rejected(self) -> None:
        with self.assertRaises(AdmissionRejected) as cm:
            schedule_ok(self.ctx, slot_start=T0 + timedelta(hours=3), slot_end=T0 + timedelta(hours=2))
        self.assertIn("SLOT_WINDOW_INVALID", cm.exception.reasons)


if __name__ == "__main__":
    import unittest

    unittest.main()
