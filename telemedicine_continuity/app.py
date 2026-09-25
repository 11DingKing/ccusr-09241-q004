"""组合根：装配存储、服务、调度器与恢复流程。

每次构建应用实例都会先执行恢复流程：
进程退出再启动后，资源占用不重复、待发通知不丢失。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .application.orchestrator import OrchestratorService
from .application.outbox import OutboxDispatcher
from .application.ports import Clock, FileNotifier, IdGenerator, Notifier, SystemClock, UuidGenerator
from .application.recovery import RecoveryManager
from .infrastructure.sqlite_store import SQLiteStore
from .interfaces.http_api import Api, create_server


@dataclass
class AppContext:
    store: SQLiteStore
    clock: Clock
    ids: IdGenerator
    notifier: Notifier
    orchestrator: OrchestratorService
    dispatcher: OutboxDispatcher
    recovery: RecoveryManager
    api: Api
    recovery_report: dict


def build_app(
    db_path: str | Path,
    *,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    notifier: Notifier | None = None,
) -> AppContext:
    store = SQLiteStore(db_path)
    store.initialize()
    clock = clock or SystemClock()
    ids = ids or UuidGenerator()
    if notifier is None:
        outbox_path = (
            Path(db_path).with_suffix(".outbox.jsonl")
            if str(db_path) != ":memory:"
            else Path("./outbox-delivery.jsonl")
        )
        notifier = FileNotifier(outbox_path)
    orchestrator = OrchestratorService(store, clock, ids)
    dispatcher = OutboxDispatcher(store, notifier, clock)
    recovery = RecoveryManager(store, clock)
    # 启动即恢复：清理崩溃残留、重估停机期间的条件变化
    recovery_report = recovery.recover(orchestrator)
    api = Api(store, orchestrator, dispatcher, recovery, clock)
    return AppContext(
        store=store,
        clock=clock,
        ids=ids,
        notifier=notifier,
        orchestrator=orchestrator,
        dispatcher=dispatcher,
        recovery=recovery,
        api=api,
        recovery_report=recovery_report,
    )


def run_server(host: str, port: int, db_path: str) -> None:
    ctx = build_app(db_path)
    server = create_server(host, port, ctx.api)
    print(f"listening on http://{host}:{port} (db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        ctx.store.close()
