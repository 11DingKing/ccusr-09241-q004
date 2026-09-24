"""通知发件箱中继。

投递语义：
- 业务事务提交时事件以 pending 落库（事务发件箱），状态与业务状态原子一致；
- 中继把 pending 事件 claim 为 sending 后在事务外发送：成功置 sent，失败回 pending 记录错误，
  因此失败通知可反复幂等补发；
- 进程在 sending 期间崩溃，重启时 Database.initialize 将其回收为 pending；
- 接收方按 event_id 去重，崩溃导致的重复投递不会产生重复通知（至少一次投递，幂等接收）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol

from ..domain.models import OutboxEvent
from ..infra.db import Database, Repository


class NotificationTransport(Protocol):
    def send(self, event: OutboxEvent) -> None:
        """发送失败请抛异常，中继将退回 pending 等待补发。"""


class InProcessTransport:
    """测试/本地演示用传输：记录全部投递，按 event_id 幂等去重。

    fail_event_keys 中的事件在第 fail_times 次尝试时抛错，用于模拟失败补发。
    """

    def __init__(self, *, fail_event_keys: set[str] | None = None, fail_times: int = 1) -> None:
        self._lock = threading.Lock()
        self.delivered: list[OutboxEvent] = []
        self.delivered_ids: set[str] = set()
        self.duplicates_suppressed = 0
        self.attempts: dict[str, int] = {}
        self.fail_event_keys = fail_event_keys or set()
        self.fail_times = fail_times

    def send(self, event: OutboxEvent) -> None:
        with self._lock:
            if event.event_id in self.delivered_ids:
                # 接收方幂等：崩溃重投不产生第二条通知
                self.duplicates_suppressed += 1
                return
            attempts = self.attempts.get(event.event_id, 0) + 1
            self.attempts[event.event_id] = attempts
            if event.event_key in self.fail_event_keys and attempts <= self.fail_times:
                raise RuntimeError(f"模拟下游通知失败（第 {attempts} 次）")
            self.delivered_ids.add(event.event_id)
            self.delivered.append(event)

    def export(self) -> list[dict]:
        with self._lock:
            return [
                {"event_id": e.event_id, "event_type": e.event_type,
                 "aggregate_id": e.aggregate_id, "payload": e.payload}
                for e in self.delivered
            ]


@dataclass
class RelayStats:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    deduped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"claimed": self.claimed, "sent": self.sent, "failed": self.failed,
                "deduped": self.deduped, "errors": self.errors}


class OutboxRelay:
    def __init__(self, db: Database, transport: NotificationTransport) -> None:
        self.db = db
        self.repo = Repository()
        self.transport = transport

    def deliver_pending(self, *, limit: int = 100) -> RelayStats:
        stats = RelayStats()
        backed_off: set[str] = set()  # 本批次已失败的事件不当场重试
        for _ in range(limit):
            # 1) 短事务 claim 一条待发事件
            with self.db.transaction() as conn:
                if backed_off:
                    placeholders = ",".join("?" for _ in backed_off)
                    sql = (
                        "SELECT * FROM outbox WHERE status='pending' "
                        f"AND event_id NOT IN ({placeholders}) ORDER BY rowid LIMIT 1"
                    )
                    row = conn.execute(sql, tuple(backed_off)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM outbox WHERE status='pending' ORDER BY rowid LIMIT 1"
                    ).fetchone()
                if row is None:
                    break
                from ..infra.db import _row_to_outbox
                event = _row_to_outbox(row)
                conn.execute(
                    "UPDATE outbox SET status='sending', attempts=attempts+1 WHERE event_id=?",
                    (event.event_id,),
                )
            stats.claimed += 1

            # 2) 事务外发送（发送不持有数据库锁）
            try:
                before = getattr(self.transport, "duplicates_suppressed", 0)
                self.transport.send(event)
                after = getattr(self.transport, "duplicates_suppressed", 0)
                if after > before:
                    stats.deduped += 1
            except Exception as exc:  # 失败退回待发，等待下一批补发
                backed_off.add(event.event_id)
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE outbox SET status='pending', last_error=? WHERE event_id=?",
                        (str(exc), event.event_id),
                    )
                stats.failed += 1
                stats.errors.append(f"{event.event_key}: {exc}")
            else:
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE outbox SET status='sent', last_error=NULL, "
                        "sent_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now') "
                        "WHERE event_id=?",
                        (event.event_id,),
                    )
                stats.sent += 1
        return stats

    def pending_count(self) -> int:
        with self.db.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE status IN ('pending','sending')"
            ).fetchone()["n"]
