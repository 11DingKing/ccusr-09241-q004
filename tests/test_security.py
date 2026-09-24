"""字段级返回投影：敏感字段只向具备相应职责的调用方开放。"""

from __future__ import annotations

from telemedicine_continuity.domain.enums import Role
from tests.support import ServiceTestBed


class FieldProjectionTests(ServiceTestBed):
    def test_sensitive_fields_visible_per_role(self) -> None:
        self.seed_scenario()
        c = self.book()

        coordinator = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        engineer = self.service.get_consultation_view(Role.LINK_ENGINEER, c.consultation_id)
        clinician = self.service.get_consultation_view(Role.CLINICIAN, c.consultation_id)
        auditor = self.service.get_consultation_view(Role.AUDITOR, c.consultation_id)

        # 排班员：姓名可见、证件号与电话不可见；锁定者可见
        self.assertEqual(coordinator["patient"]["name"], "李四")
        self.assertNotIn("national_id", coordinator["patient"])
        self.assertNotIn("contact_phone", coordinator["patient"])
        self.assertIn("locked_by", coordinator)
        self.assertIn("consent_valid_until", coordinator)

        # 链路工程师：患者标识脱敏、授权明细不可见
        self.assertNotEqual(engineer["patient"]["patient_id"], "P001")
        self.assertTrue(engineer["patient"]["patient_id"].startswith("****"))
        self.assertNotIn("consent_valid_until", engineer)
        self.assertNotIn("consent_state", engineer)
        self.assertNotIn("locked_by", engineer)
        # 链路工程信息对所有角色一致可见
        self.assertEqual(engineer["active_link_id"], coordinator["active_link_id"])

        # 医生/审计：完整患者信息
        for view in (clinician, auditor):
            self.assertEqual(view["patient"]["national_id"], "110101199001011234")
            self.assertEqual(view["patient"]["contact_phone"], "13800001234")

        self.assertNotIn("locked_by", clinician)  # 医生无需知道锁定操作者
        self.assertIn("locked_by", auditor)

    def test_engineer_cannot_book_and_coordinator_cannot_report_snapshots(self) -> None:
        from telemedicine_continuity.domain.errors import AuthorizationError
        self.seed_scenario()
        with self.assertRaises(AuthorizationError):
            self.service.report_snapshot(Role.COORDINATOR, "LINK-A1", health="up")
        with self.assertRaises(AuthorizationError):
            self.service.book_consultation(
                Role.LINK_ENGINEER, patient_id="P001", clinician_id="D001",
                slot_id="S001", org_ids=["H-LEAD", "C-01"], min_grade="AUDIO")

    def test_auditor_is_read_only(self) -> None:
        from telemedicine_continuity.domain.errors import AuthorizationError
        self.seed_scenario()
        c = self.book()
        with self.assertRaises(AuthorizationError):
            self.service.cancel(Role.AUDITOR, c.consultation_id)
        # 审计可读
        view = self.service.get_consultation_view(Role.AUDITOR, c.consultation_id)
        self.assertEqual(view["code"], c.code)


if __name__ == "__main__":
    unittest.main()
