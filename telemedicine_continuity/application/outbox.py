"""发件箱调度：把与业务状态一致提交的通知可靠送达。

- 事件在业务事务中写入，调度器异步外发；
- 发送失败标记 FAILED，可在后续调度中幂等补发；
- 通知端口按 event_id 幂等，重复投递不产生重复副作用。
"""

from __future__ import annotations

from ..infrastructure.sqlite_store import SQLiteStore
from .ports import Clock, Notifier


class OutboxDispatcher:
    def __init__(self, store: SQLiteStore, notifier: Notifier, clock: Clock) -> None:
        self._store = store
        self._notifier = notifier
        self._clock = clock

    def dispatch_pending(self, limit: int = 50) -> dict:
        events = self._store.pending_outbox_events(limit=limit)
        sent = failed = 0
        errors: list[dict] = []
        for event in events:
            now = self._clock.now()
            try:
                self._notifier.send(event.id, event.type, event.payload)
            except Exception as exc:  # 发送失败不阻塞其他事件
                self._store.mark_outbox_failed(event.id, now, str(exc))
                failed += 1
                errors.append({"event_id": event.id, "error": str(exc)})
            else:
                self._store.mark_outbox_sent(event.id, now)
                sent += 1
        return {"dispatched": sent, "failed": failed, "errors": errors}
