"""恢复流程：进程退出再启动不得造成重复占用；停机期间的变化在恢复时被处理。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from telemedicine_continuity.app import build_app
from telemedicine_continuity.domain.enums import HoldStatus
from telemedicine_continuity.domain.errors import SlotConflict

from helpers import (
    FakeNotifier,
    ManualClock,
    SeqIds,
    T0,
    grant_consent,
    make_ctx,
    schedule_ok,
    seed_network,
)


class RecoveryTests(unittest.TestCase):
    def test_restart_preserves_holds_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = ManualClock()
            ctx1, _, _ = make_ctx(tmp, clock)
            seed_network(ctx1)
            grant_consent(ctx1)
            first = schedule_ok(ctx1, key="boot-1")
            ctx1.store.close()

            # 模拟进程退出再启动：同一数据库文件构建全新应用实例
            notifier2 = FakeNotifier()
            ctx2 = build_app(Path(tmp) / "test.db", clock=clock, ids=SeqIds(), notifier=notifier2)
            try:
                # 占用依然生效：重叠时段排班被拒绝
                with self.assertRaises(SlotConflict):
                    schedule_ok(ctx2, key="boot-2")
                # 幂等重放返回原会诊，不产生重复占用
                replay = schedule_ok(ctx2, key="boot-1")
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["consultation_id"], first["consultation_id"])
                self.assertEqual(ctx2.store.count_holds(HoldStatus.HELD), 2)
            finally:
                ctx2.store.close()

    def test_recovery_cancels_consultation_with_consent_expired_during_downtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = ManualClock()
            ctx1, _, _ = make_ctx(tmp, clock)
            seed_network(ctx1)
            ctx1.orchestrator.grant_authorization(
                "consent-exp", "PATIENT_CONSENT", "pat-1", "telemedicine",
                T0 - timedelta(hours=1), T0 + timedelta(hours=3),
            )
            cid = schedule_ok(ctx1, key="boot-exp", consent_id="consent-exp")["consultation_id"]
            ctx1.store.close()

            clock.advance(hours=4)  # 停机期间同意过期
            ctx2 = build_app(Path(tmp) / "test.db", clock=clock, ids=SeqIds(), notifier=FakeNotifier())
            try:
                c = ctx2.orchestrator.get_consultation(cid)
                self.assertEqual(c.status.value, "CANCELLED")
                self.assertEqual(c.cancel_reason, "CONSENT_INVALID")
                self.assertEqual(ctx2.store.count_holds(HoldStatus.HELD), 0)
                self.assertTrue(ctx2.recovery_report["reevaluated"])
            finally:
                ctx2.store.close()

    def test_recovery_releases_orphan_holds_and_expired_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = ManualClock()
            ctx1, _, _ = make_ctx(tmp, clock)
            seed_network(ctx1)
            grant_consent(ctx1)
            cid = schedule_ok(ctx1, key="boot-orphan")["consultation_id"]
            ctx1.orchestrator.perform_action(cid, "lock", "ops-1", reason="核对中")
            # 模拟崩溃残留：会诊已取消但占用记录仍为 HELD
            ctx1.orchestrator.perform_action(cid, "cancel", "ops-1")
            ctx1.store._conn.execute(
                "UPDATE resource_holds SET status='HELD' WHERE consultation_id=?", (cid,)
            )
            ctx1.store.close()

            clock.advance(minutes=45)  # 锁定已过期
            ctx2 = build_app(Path(tmp) / "test.db", clock=clock, ids=SeqIds(), notifier=FakeNotifier())
            try:
                self.assertGreaterEqual(ctx2.recovery_report["released_orphan_holds"], 1)
                self.assertGreaterEqual(ctx2.recovery_report["expired_locks"], 1)
                self.assertEqual(ctx2.store.count_holds(HoldStatus.HELD), 0)
            finally:
                ctx2.store.close()

    def test_pending_outbox_survives_restart_and_dispatches_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = ManualClock()
            ctx1, _, notifier1 = make_ctx(tmp, clock)
            seed_network(ctx1)
            grant_consent(ctx1)
            cid = schedule_ok(ctx1, key="boot-outbox")["consultation_id"]
            # 第一次运行不派发，事件滞留发件箱
            ctx1.store.close()

            notifier2 = FakeNotifier()
            ctx2 = build_app(Path(tmp) / "test.db", clock=clock, ids=SeqIds(), notifier=notifier2)
            try:
                self.assertEqual(ctx2.recovery_report["pending_outbox_events"], 1)
                report = ctx2.dispatcher.dispatch_pending()
                self.assertEqual(report["dispatched"], 1)
                # 再次调度：SENT 事件不重投，通知侧按 event_id 幂等
                report2 = ctx2.dispatcher.dispatch_pending()
                self.assertEqual(report2["dispatched"], 0)
                self.assertEqual(len(notifier2.delivered), 1)
            finally:
                ctx2.store.close()


if __name__ == "__main__":
    unittest.main()
