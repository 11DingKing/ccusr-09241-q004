"""领域与应用层错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """业务规则拒绝。http 层映射为 409/422。"""

    http_status = 422

    def __init__(self, code: str, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ValidationError(DomainError):
    http_status = 400


class NotFound(DomainError):
    http_status = 404


class AuthorizationError(DomainError):
    """调用方职责不足，或无权操作该锁定资源。"""

    http_status = 403


class SlotConflict(DomainError):
    """并发排班下时段已被占用。"""

    http_status = 409


class ConcurrentUpdate(DomainError):
    """乐观锁版本不一致（聚合已被其他事务推进）。"""

    http_status = 409
