"""恢复流程：进程退出再启动后的一致性问题兜底。

恢复只做清理与重估，绝不新增占用：
- 过期人工锁定自动释放；
- 已终止会诊遗留的资源占用被释放（崩溃残留的兜底）；
- 停机期间授权/同意失效的会诊按规则重估（同意失效强制取消）；
- 发件箱中未送达的事件保持待调度状态，由调度器幂等补发。
"""

from __future__ import annotations

from ..infrastructure.sqlite_store import SQLiteStore
from .orchestrator import OrchestratorService
from .ports import Clock


class RecoveryManager:
    def __init__(self, store: SQLiteStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def recover(self, orchestrator: OrchestratorService) -> dict:
        now = self._clock.now()
        expired_locks = self._store.expire_locks(now)
        released_holds = self._store.release_orphan_holds()

        reevaluated: list[dict] = []
        for c in self._store.list_open_consultations():
            outcome = orchestrator.reevaluate(c.id, trigger="recovery")
            if outcome and outcome.get("from") != outcome.get("to"):
                reevaluated.append(outcome)

        pending_outbox = len(self._store.pending_outbox_events(limit=1000))
        return {
            "expired_locks": expired_locks,
            "released_orphan_holds": released_holds,
            "reevaluated": reevaluated,
            "pending_outbox_events": pending_outbox,
        }
