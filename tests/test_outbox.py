"""发件箱：业务状态与通知一致提交；失败通知可幂等补发。"""

from __future__ import annotations

from telemedicine_continuity.domain.enums import OutboxStatus
from telemedicine_continuity.domain.errors import AdmissionRejected

from helpers import OrchestratorTestCase, schedule_ok


class OutboxTests(OrchestratorTestCase):
    def test_state_and_outbox_commit_atomically(self) -> None:
        result = schedule_ok(self.ctx)
        cid = result["consultation_id"]
        events = self.ctx.store.list_outbox_events(cid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "CONSULTATION_CONFIRMED")
        self.assertEqual(events[0].status, OutboxStatus.PENDING)

    def test_rejected_schedule_leaves_no_outbox_events(self) -> None:
        self.ctx.orchestrator.revoke_authorization("consent-1")
        with self.assertRaises(AdmissionRejected):
            schedule_ok(self.ctx)
        self.assertEqual(self.ctx.store.list_outbox_events(), [], "准入失败不得残留通知事件")

    def test_failed_notification_is_retried_idempotently(self) -> None:
        result = schedule_ok(self.ctx)
        cid = result["consultation_id"]

        self.notifier.fail_next = 1
        report = self.ctx.dispatcher.dispatch_pending()
        self.assertEqual(report["failed"], 1)
        event = self.ctx.store.list_outbox_events(cid)[0]
        self.assertEqual(event.status, OutboxStatus.FAILED)
        self.assertEqual(event.attempts, 1)
        self.assertTrue(event.last_error)

        report = self.ctx.dispatcher.dispatch_pending()
        self.assertEqual(report["dispatched"], 1)
        event = self.ctx.store.list_outbox_events(cid)[0]
        self.assertEqual(event.status, OutboxStatus.SENT)
        self.assertEqual(len(self.notifier.delivered), 1, "补发不得产生重复送达")

        # 已送达事件不再参与调度
        report = self.ctx.dispatcher.dispatch_pending()
        self.assertEqual(report["dispatched"], 0)
        self.assertEqual(len(self.notifier.delivered), 1)

    def test_cancel_emits_event_in_same_transaction(self) -> None:
        result = schedule_ok(self.ctx)
        cid = result["consultation_id"]
        self.ctx.orchestrator.perform_action(cid, "cancel", "scheduler-1")
        events = self.ctx.store.list_outbox_events(cid)
        self.assertEqual([e.type for e in events], ["CONSULTATION_CONFIRMED", "CONSULTATION_CANCELLED"])
        c = self.ctx.orchestrator.get_consultation(cid)
        self.assertEqual(c.status.value, "CANCELLED")


if __name__ == "__main__":
    import unittest

    unittest.main()
