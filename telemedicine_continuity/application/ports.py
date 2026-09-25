"""可替换端口：时钟、标识生成与通知发送。"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str: ...


class Notifier(Protocol):
    """通知发送端口；实现方必须按 event_id 幂等。"""

    def send(self, event_id: str, event_type: str, payload: dict) -> None: ...


class NotificationError(Exception):
    """发送失败；调度器会据此把事件标记为 FAILED 以便补发。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class UuidGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"


class FileNotifier:
    """把通知写入本地 JSONL 文件；按 event_id 去重，进程重启后仍保持幂等。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._delivered: set[str] = set()
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._delivered.add(line.split("\t", 1)[0])

    def send(self, event_id: str, event_type: str, payload: dict) -> None:
        import json

        with self._lock:
            if event_id in self._delivered:
                return
            record = json.dumps(
                {"event_id": event_id, "type": event_type, "payload": payload},
                ensure_ascii=False,
                sort_keys=True,
            )
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(f"{event_id}\t{record}\n")
            self._delivered.add(event_id)

    def delivered_ids(self) -> set[str]:
        with self._lock:
            return set(self._delivered)
