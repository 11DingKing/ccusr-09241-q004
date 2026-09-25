"""领域枚举：服务等级、会诊状态、链路角色、授权与调用方职责。"""

from __future__ import annotations

from enum import Enum


class ServiceLevel(str, Enum):
    """最低服务等级，决定链路质量底线与是否允许共享故障域。"""

    GOLD = "GOLD"
    SILVER = "SILVER"
    BRONZE = "BRONZE"

    @property
    def rank(self) -> int:
        return {"GOLD": 3, "SILVER": 2, "BRONZE": 1}[self.value]


class LinkKind(str, Enum):
    PRIMARY = "PRIMARY"
    BACKUP = "BACKUP"


class ConsultationStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (ConsultationStatus.COMPLETED, ConsultationStatus.CANCELLED)


class PhaseKind(str, Enum):
    """操作阶段：关键操作阶段对自动/手动变迁施加更严约束。"""

    PREP = "PREP"
    CRITICAL = "CRITICAL"
    WRAPUP = "WRAPUP"


class AuthorizationKind(str, Enum):
    PATIENT_CONSENT = "PATIENT_CONSENT"
    INSTITUTION = "INSTITUTION"


class AuthorizationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ResourceType(str, Enum):
    DOCTOR = "DOCTOR"
    EQUIPMENT = "EQUIPMENT"


class HoldStatus(str, Enum):
    HELD = "HELD"
    RELEASED = "RELEASED"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class Role(str, Enum):
    """调用方职责，决定可见字段集合与可调用接口。"""

    SCHEDULER = "scheduler"
    CLINICIAN = "clinician"
    OPS = "ops"
    AUDITOR = "auditor"
    ADMIN = "admin"
