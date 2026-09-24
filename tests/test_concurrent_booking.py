"""并发排班：同一时段/同一医生重叠时段在多线程争抢下只能成功一单。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from telemedicine_continuity.domain.enums import Grade, Role
from tests.support import (
    CLINICIAN,
    COMMUNITY,
    HOSPITAL,
    LINK_A,
    LINK_B,
    PATIENT,
    ServiceTestBed,
)


class ConcurrentBookingTests(ServiceTestBed):
    def test_same_slot_only_one_consultation_wins_under_thread_race(self) -> None:
        self.seed_scenario()
        results: list = []
        errors: list = []
        barrier = threading.Barrier(8)

        def attempt(i: int) -> None:
            barrier.wait()  # 尽量同时冲入事务
            try:
                c = self.service.book_consultation(
                    Role.COORDINATOR,
                    patient_id=PATIENT,
                    clinician_id=CLINICIAN,
                    slot_id="S001",
                    org_ids=[HOSPITAL, COMMUNITY],
                    min_grade=Grade.SD_VIDEO,
                    required_link_count=2,
                    idem_key=f"race-{i}",
                )
                results.append(c.consultation_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(attempt, i) for i in range(8)]
            for f in as_completed(futures):
                f.result()

        self.assertEqual(len(results), 1, f"应有且仅有一单成功，实际 {len(results)}")
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(e == "SlotConflict" for e in errors), errors)

        with self.db.read_only() as conn:
            occupied = conn.execute(
                "SELECT COUNT(*) AS n FROM slots WHERE status='occupied'"
            ).fetchone()["n"]
            consultations = conn.execute(
                "SELECT COUNT(*) AS n FROM consultations"
            ).fetchone()["n"]
        self.assertEqual(occupied, 1)
        self.assertEqual(consultations, 1)

    def test_overlapping_slots_same_clinician_are_serialized_too(self) -> None:
        self.seed_scenario(slot="S100")
        # 同一医生另建一个时间重叠的时段
        self.service.create_slot(
            Role.COORDINATOR, slot_id="S101", clinician_id=CLINICIAN,
            start_time="2026-09-24T10:30:00+00:00",
            end_time="2026-09-24T11:30:00+00:00")
        outcome = {}

        def book(slot_id: str, key: str) -> None:
            try:
                c = self.service.book_consultation(
                    Role.COORDINATOR, patient_id=PATIENT, clinician_id=CLINICIAN,
                    slot_id=slot_id, org_ids=[HOSPITAL, COMMUNITY],
                    min_grade=Grade.SD_VIDEO, required_link_count=2, idem_key=key)
                outcome[slot_id] = ("ok", c.consultation_id)
            except Exception as exc:  # noqa: BLE001
                outcome[slot_id] = ("err", type(exc).__name__)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fs = [pool.submit(book, "S100", "k-a"),
                  pool.submit(book, "S101", "k-b")]
            for f in as_completed(fs):
                f.result()

        statuses = sorted(v[0] for v in outcome.values())
        self.assertEqual(statuses, ["err", "ok"])
        error_names = [v[1] for v in outcome.values() if v[0] == "err"]
        self.assertEqual(error_names, ["SlotConflict"])

    def test_identical_idempotency_key_returns_same_consultation(self) -> None:
        self.seed_scenario()
        c1 = self.book(idem_key="same-key")
        c2 = self.book(idem_key="same-key")
        self.assertEqual(c1.consultation_id, c2.consultation_id)
        with self.db.read_only() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM consultations").fetchone()["n"]
            events = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE event_type='consultation.confirmed'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(events, 1)


if __name__ == "__main__":
    unittest.main()
