"""并发排班：同一时段同一资源只应有一个会诊成功；幂等重放不产生重复占用。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from telemedicine_continuity.domain.enums import HoldStatus
from telemedicine_continuity.domain.errors import SlotConflict

from helpers import OrchestratorTestCase, schedule_ok


class ConcurrencyTests(OrchestratorTestCase):
    def test_concurrent_schedule_same_slot_only_one_wins(self) -> None:
        results: list[dict] = []
        errors: list[Exception] = []

        def attempt(i: int) -> None:
            try:
                results.append(schedule_ok(self.ctx, key=f"key-{i}"))
            except SlotConflict as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(attempt, range(8)))

        self.assertEqual(len(results), 1, "同一时段同一医生只应成功一个会诊")
        self.assertEqual(len(errors), 7)
        # 占用记录：医生 + 设备各一条，且都属于胜出的会诊
        self.assertEqual(self.ctx.store.count_holds(HoldStatus.HELD), 2)
        holds = self.ctx.store.holds_for_consultation(results[0]["consultation_id"])
        self.assertEqual(len(holds), 2)

    def test_concurrent_same_idempotency_key_single_consultation(self) -> None:
        results: list[dict] = []

        def attempt(_: int) -> None:
            results.append(schedule_ok(self.ctx, key="shared-key"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(attempt, range(8)))

        ids = {r["consultation_id"] for r in results}
        self.assertEqual(len(ids), 1, "同一幂等键并发提交必须收敛到同一个会诊")
        self.assertEqual(self.ctx.store.count_holds(HoldStatus.HELD), 2)
        events = self.ctx.store.list_outbox_events()
        self.assertEqual(len(events), 1, "幂等重放不得重复产生通知事件")

    def test_idempotent_replay_returns_same_result(self) -> None:
        first = schedule_ok(self.ctx, key="replay-key")
        second = schedule_ok(self.ctx, key="replay-key")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["consultation_id"], second["consultation_id"])
        self.assertEqual(self.ctx.store.count_holds(HoldStatus.HELD), 2)

    def test_different_slot_can_schedule_same_doctor(self) -> None:
        from datetime import timedelta

        from helpers import T0

        first = schedule_ok(self.ctx, key="slot-1")
        second = schedule_ok(
            self.ctx,
            key="slot-2",
            slot_start=T0 + timedelta(hours=4),
            slot_end=T0 + timedelta(hours=5),
        )
        self.assertNotEqual(first["consultation_id"], second["consultation_id"])
        self.assertEqual(self.ctx.store.count_holds(HoldStatus.HELD), 4)


if __name__ == "__main__":
    import unittest

    unittest.main()
