"""领域错误：接口层依据 code/http_status 做统一映射。"""

from __future__ import annotations


class DomainError(Exception):
    code = "DOMAIN_ERROR"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AdmissionRejected(DomainError):
    code = "ADMISSION_REJECTED"
    http_status = 422

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("准入评估未通过", details={"reasons": reasons})
        self.reasons = reasons


class SlotConflict(DomainError):
    code = "SLOT_CONFLICT"
    http_status = 409


class NotFound(DomainError):
    code = "NOT_FOUND"
    http_status = 404


class InvalidTransition(DomainError):
    code = "INVALID_TRANSITION"
    http_status = 409


class ConcurrentModification(DomainError):
    code = "CONCURRENT_MODIFICATION"
    http_status = 409


class Forbidden(DomainError):
    code = "FORBIDDEN"
    http_status = 403


class Unauthorized(DomainError):
    code = "UNAUTHORIZED"
    http_status = 401
