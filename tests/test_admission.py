"""准入规则的纯领域测试。"""

from __future__ import annotations

import unittest

from telemedicine_continuity.domain.admission import (
    AdmissionRequest,
    effective_grade,
    evaluate_admission,
)
from telemedicine_continuity.domain.enums import ConsentStatus, Grade, Health
from telemedicine_continuity.domain.models import Consent, Link
from telemedicine_continuity.infra.clock import MutableClock

ORGS = ("H1", "C1")
NOW = "2026-09-24T09:00:00+00:00"


def make_link(link_id: str, *, domain: str, grade: int = Grade.HD_VIDEO,
              health: Health = Health.UP, snapshot_at: str | None = NOW,
              org_ids=ORGS) -> Link:
    return Link(link_id, link_id, domain, grade, health, 40, 0.0, org_ids, snapshot_at)


def consent(until: str = "2026-09-24T12:00:00+00:00",
            state: str = ConsentStatus.GRANTED.value) -> Consent:
    return Consent("P1", state, "2026-09-01T00:00:00+00:00", until)


def request(**overrides) -> AdmissionRequest:
    base = dict(
        patient_id="P1",
        clinician_id="D1",
        slot_start="2026-09-24T10:00:00+00:00",
        slot_end="2026-09-24T11:00:00+00:00",
        org_ids=ORGS,
        min_grade=Grade.SD_VIDEO,
        required_link_count=2,
        candidate_link_ids=("L1", "L2"),
    )
    base.update(overrides)
    return AdmissionRequest(**base)


class AdmissionRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()

    def test_independent_domains_selected_as_primary_and_backup(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A"),
                 "L2": make_link("L2", domain="FD-B")}
        decision = evaluate_admission(request(), links=links, consent=consent(),
                                      clock=self.clock)
        self.assertTrue(decision.allowed, [i.message for i in decision.issues])
        self.assertNotEqual(decision.active_link_id, decision.backup_link_id)
        chosen = {decision.active_link_id, decision.backup_link_id}
        self.assertEqual(chosen, {"L1", "L2"})

    def test_shared_fault_domain_rejected_even_with_two_links(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A"),
                 "L2": make_link("L2", domain="FD-A", grade=Grade.SD_VIDEO)}
        decision = evaluate_admission(request(), links=links, consent=consent(),
                                      clock=self.clock)
        self.assertFalse(decision.allowed)
        self.assertIn("shared_fault_domain", [i.code for i in decision.issues])

    def test_stale_snapshot_rejected(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A",
                                 snapshot_at="2026-09-24T08:50:00+00:00"),
                 "L2": make_link("L2", domain="FD-B")}
        decision = evaluate_admission(request(), links=links, consent=consent(),
                                      clock=self.clock)
        self.assertFalse(decision.allowed)
        self.assertIn("snapshot_stale", [i.code for i in decision.issues])

    def test_missing_snapshot_rejected(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A", snapshot_at=None),
                 "L2": make_link("L2", domain="FD-B", snapshot_at=None)}
        decision = evaluate_admission(request(), links=links, consent=consent(),
                                      clock=self.clock)
        self.assertFalse(decision.allowed)

    def test_consent_expired_rejected(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A"),
                 "L2": make_link("L2", domain="FD-B")}
        decision = evaluate_admission(
            request(), links=links,
            consent=consent(until="2026-09-24T08:00:00+00:00"),
            clock=self.clock)
        self.assertFalse(decision.allowed)
        self.assertIn("consent_expired", [i.code for i in decision.issues])

    def test_consent_must_cover_full_slot(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A"),
                 "L2": make_link("L2", domain="FD-B")}
        decision = evaluate_admission(
            request(), links=links,
            consent=consent(until="2026-09-24T10:30:00+00:00"),
            clock=self.clock)
        self.assertFalse(decision.allowed)
        self.assertIn("consent_not_covers_slot", [i.code for i in decision.issues])

    def test_consent_revoked_rejected(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A"),
                 "L2": make_link("L2", domain="FD-B")}
        decision = evaluate_admission(
            request(), links=links, consent=consent(state=ConsentStatus.REVOKED.value),
            clock=self.clock)
        codes = [i.code for i in decision.issues]
        self.assertIn("consent_revoked", codes)

    def test_consent_missing_rejected(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A"),
                 "L2": make_link("L2", domain="FD-B")}
        decision = evaluate_admission(request(), links=links, consent=None,
                                      clock=self.clock)
        self.assertIn("consent_missing", [i.code for i in decision.issues])

    def test_degraded_link_pays_grade_penalty(self) -> None:
        # 标称 SD_VIDEO，劣化后只够 AUDIO，要求 SD_VIDEO 即不达标
        self.assertEqual(effective_grade(make_link("L", domain="D",
                                                   grade=Grade.SD_VIDEO,
                                                   health=Health.DEGRADED)),
                         Grade.AUDIO)
        links = {"L1": make_link("L1", domain="FD-A", grade=Grade.SD_VIDEO,
                                 health=Health.DEGRADED),
                 "L2": make_link("L2", domain="FD-B", grade=Grade.SD_VIDEO,
                                 health=Health.DEGRADED)}
        decision = evaluate_admission(request(), links=links, consent=consent(),
                                      clock=self.clock)
        self.assertFalse(decision.allowed)
        self.assertIn("grade_insufficient", [i.code for i in decision.issues])

    def test_down_link_excluded_but_other_independent_pair_succeeds(self) -> None:
        # 显式候选含一条中断链路，但另有两条跨域健康链路时应择优成功
        links = {"L1": make_link("L1", domain="FD-A"),
                 "L2": make_link("L2", domain="FD-B"),
                 "L3": make_link("L3", domain="FD-A", health=Health.DOWN)}
        decision = evaluate_admission(
            request(candidate_link_ids=("L1", "L2", "L3")),
            links=links, consent=consent(), clock=self.clock)
        self.assertTrue(decision.allowed, [i.message for i in decision.issues])

    def test_single_link_requirement_does_not_demand_redundancy(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A")}
        decision = evaluate_admission(request(required_link_count=1,
                                              candidate_link_ids=("L1",)),
                                      links=links, consent=consent(),
                                      clock=self.clock)
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.backup_link_id)

    def test_org_coverage_required(self) -> None:
        links = {"L1": make_link("L1", domain="FD-A", org_ids=("H1",)),
                 "L2": make_link("L2", domain="FD-B", org_ids=("H1",))}
        decision = evaluate_admission(request(), links=links, consent=consent(),
                                      clock=self.clock)
        self.assertFalse(decision.allowed)
        self.assertTrue(any("未覆盖" in i.message for i in decision.issues))


if __name__ == "__main__":
    unittest.main()
