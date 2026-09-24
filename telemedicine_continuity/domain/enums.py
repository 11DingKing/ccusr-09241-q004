"""领域内使用的枚举与等级定义。"""

from __future__ import annotations

from enum import Enum, IntEnum


class Grade(IntEnum):
    """链路可承载的服务等级，数值越高能力越强。最低服务等级以此表达。"""

    NONE = 0
    AUDIO = 1
    SD_VIDEO = 2
    HD_VIDEO = 3


class Health(str, Enum):
    """探测得到的链路健康度。"""

    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class SlotStatus(str, Enum):
    FREE = "free"
    OCCUPIED = "occupied"


class ConsultationStatus(str, Enum):
    CONFIRMED = "confirmed"
    SWITCHED = "switched"
    DEGRADED = "degraded"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


TERMINAL_STATUSES = frozenset({ConsultationStatus.CANCELLED, ConsultationStatus.COMPLETED})
ACTIVE_STATUSES = frozenset(
    {
        ConsultationStatus.CONFIRMED,
        ConsultationStatus.SWITCHED,
        ConsultationStatus.DEGRADED,
        ConsultationStatus.PAUSED,
    }
)


class CriticalPhase(str, Enum):
    NORMAL = "normal"
    CRITICAL = "critical"


class ConsentStatus(str, Enum):
    GRANTED = "granted"
    REVOKED = "revoked"


class Role(str, Enum):
    """调用方职责。字段级返回与接口准入均以此为准。"""

    COORDINATOR = "coordinator"        # 排班员
    LINK_ENGINEER = "link_engineer"    # 链路工程师
    CLINICIAN = "clinician"            # 诊疗医生
    AUDITOR = "auditor"                # 审计员


class OutboxStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"


# 待关键阶段结束后落地的延迟效果
PENDING_CANCEL = "cancel_on_stage_exit"
