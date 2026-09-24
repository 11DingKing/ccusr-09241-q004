"""事务发件箱：原子提交、失败幂等补发、崩溃回收、接收方去重。"""

from __future__ import annotations

from telemedicine_continuity.domain.enums import Health, OutboxStatus, Role
from telemedicine_continuity.infra.db import Database
from tests.support import LINK_A, ServiceTestBed


class OutboxTests(ServiceTestBed):
    def test_business_state_and_notification_commit_together(self) -> None:
        self.seed_scenario()
        self.book()
        with self.db.read_only() as conn:
            event = conn.execute(
                "SELECT * FROM outbox WHERE event_type='consultation.confirmed'"
            ).fetchone()
            consultation = conn.execute(
                "SELECT status FROM consultations WHERE consultation_id=?",
                (event["aggregate_id"],),
            ).fetchone()
            slot = conn.execute(
                "SELECT status, consultation_id FROM slots WHERE slot_id='S001'"
            ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["status"], OutboxStatus.PENDING.value)
        self.assertEqual(consultation["status"], "confirmed")
        self.assertEqual(slot["status"], "occupied")
        self.assertEqual(slot["consultation_id"], event["aggregate_id"])

    def test_failed_notification_is_retried_idempotently_until_success(self) -> None:
        self.seed_scenario()
        c = self.book()
        # 让确认事件第一次发送失败
        self.transport.fail_event_keys = {
            f"consultation.confirmed:book-1"
        }
        stats1 = self.deliver()
        self.assertEqual(stats1.failed, 1)
        self.assertEqual(stats1.sent, 0)
        with self.db.read_only() as conn:
            row = conn.execute(
                "SELECT status, attempts, last_error FROM outbox "
                "WHERE event_type='consultation.confirmed'"
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("模拟下游通知失败", row["last_error"])

        # 故障排除后补发：同一条通知，attempts 累计，仅投递一次
        self.transport.fail_event_keys = set()
        stats2 = self.deliver()
        self.assertEqual(stats2.sent, 1)
        self.assertEqual(len(self.transport.delivered), 1)
        self.assertEqual(self.transport.delivered[0].aggregate_id, c.consultation_id)

    def test_sending_event_after_crash_is_reclaimed_on_restart(self) -> None:
        self.seed_scenario()
        self.book()
        # 模拟进程在 claim(sending) 之后、确认之前崩溃
        with self.db.transaction() as conn:
            conn.execute("UPDATE outbox SET status='sending', attempts=1")

        # 全新进程重新打开同一数据库（WAL 文件保留已提交状态）
        db2 = Database(self.db_path)
        with db2.read_only() as conn:
            statuses = [r["status"] for r in conn.execute("SELECT status FROM outbox")]
        self.assertTrue(all(s == "pending" for s in statuses), statuses)
        db2_path = db2  # 保持句柄至测试结束

    def test_restart_does_not_duplicate_occupation_or_events(self) -> None:
        self.seed_scenario()
        self.book()
        self.deliver()
        # 进程重启：重新打开数据库
        db2 = Database(self.db_path)
        from telemedicine_continuity.application.notification import OutboxRelay
        relay2 = OutboxRelay(db2, self.transport)
        self.assertEqual(relay2.pending_count(), 0)
        with db2.read_only() as conn:
            occupied = conn.execute(
                "SELECT COUNT(*) AS n FROM slots WHERE status='occupied'"
            ).fetchone()["n"]
            events = conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
        self.assertEqual(occupied, 1)
        self.assertEqual(events, 1)

    def test_link_event_flow_is_atomic_too(self) -> None:
        self.seed_scenario()
        self.book()
        before = len(self.transport.delivered)
        self.snapshot(LINK_A, Health.DOWN.value)
        self.deliver()
        types = [e.event_type for e in self.transport.delivered[before:]]
        self.assertIn("consultation.switched", types)


if __name__ == "__main__":
    unittest.main()
