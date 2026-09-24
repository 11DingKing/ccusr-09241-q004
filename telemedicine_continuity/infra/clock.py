"""可替换的时间与标识端口。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError

    def now_iso(self) -> str:
        return to_iso(self.now())


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class MutableClock(Clock):
    """测试用可控时钟。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def business_code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"
