"""服务启动入口。

用法：
    python -m telemedicine_continuity.main --db var/orchestrator.db --port 8080

运行数据落在项目根的 var/ 下（已由 .gitignore 排除），不写入源码包目录。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .application.notification import InProcessTransport, OutboxRelay
from .application.service import OrchestrationService
from .httpapi.server import ApiContainer, make_server
from .infra.clock import SystemClock
from .infra.db import Database


def build_container(db_path: str | Path) -> ApiContainer:
    db = Database(db_path)
    service = OrchestrationService(db, SystemClock())
    transport = InProcessTransport()
    relay = OutboxRelay(db, transport)
    return ApiContainer(db, service, relay)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="远程诊疗链路连续性编排中心")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="var/orchestrator.db",
                        help="SQLite 数据文件路径（默认 var/orchestrator.db）")
    args = parser.parse_args(argv)

    container = build_container(args.db)
    server = make_server(args.host, args.port, container)
    print(f"编排中心已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    print("调用方通过 X-Role 指定职责：coordinator/link_engineer/clinician/auditor")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
