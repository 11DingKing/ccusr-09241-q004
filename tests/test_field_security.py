"""字段级安全：敏感字段只向具有相应职责的调用方返回。"""

from __future__ import annotations

from telemedicine_continuity.domain.enums import Role
from telemedicine_continuity.interfaces.views import MASKED, consultation_view

from helpers import OrchestratorTestCase, schedule_ok


class FieldSecurityTests(OrchestratorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cid = schedule_ok(self.ctx)["consultation_id"]

    def view(self, role: Role) -> dict:
        c = self.ctx.orchestrator.get_consultation(self.cid)
        return consultation_view(self.ctx.store, c, role, self.clock.now())

    def test_scheduler_sees_no_phi_or_probe_metrics(self) -> None:
        view = self.view(Role.SCHEDULER)
        self.assertIn("doctor_id", view)
        self.assertEqual(view["patient_ref"], "pat-1")
        self.assertNotIn("patient", view, "排班员不得看到患者姓名/病历号")
        self.assertNotIn("links", view, "排班员不得看到探测指标")
        self.assertNotIn("payload", view.get("consent_summary", {}))

    def test_clinician_sees_phi_and_consent_payload(self) -> None:
        view = self.view(Role.CLINICIAN)
        self.assertEqual(view["patient"]["name"], "张三")
        self.assertEqual(view["patient"]["medical_record_no"], "MRN-001")
        self.assertEqual(view["consent"]["payload"]["form"], "signed-consent-001")
        self.assertNotIn("links", view, "医生不需要链路探测明细")

    def test_ops_sees_probe_metrics_but_no_phi(self) -> None:
        view = self.view(Role.OPS)
        self.assertIn("links", view)
        self.assertEqual(view["links"]["primary"]["failure_domain"], "domain-1")
        self.assertIsNotNone(view["links"]["primary"]["quality"])
        self.assertNotIn("patient", view)
        self.assertEqual(view["patient_ref"], "pat-1")

    def test_auditor_sees_masked_phi(self) -> None:
        view = self.view(Role.AUDITOR)
        self.assertEqual(view["patient_masked"]["name"], MASKED)
        self.assertEqual(view["patient_masked"]["medical_record_no"], MASKED)
        self.assertEqual(view["consent_masked"]["payload"], MASKED)
        self.assertNotIn("patient", view)

    def test_admin_sees_full_view(self) -> None:
        view = self.view(Role.ADMIN)
        self.assertEqual(view["patient"]["name"], "张三")
        self.assertIn("links", view)
        self.assertIn("consent", view)


if __name__ == "__main__":
    import unittest

    unittest.main()
