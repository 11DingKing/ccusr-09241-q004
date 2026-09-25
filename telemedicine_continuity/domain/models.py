"""领域模型：以数据类表达编排中心的核心实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import (
    AuthorizationKind,
    AuthorizationStatus,
    ConsultationStatus,
    HoldStatus,
    LinkKind,
    OutboxStatus,
    PhaseKind,
    ResourceType,
    ServiceLevel,
)


@dataclass(frozen=True)
class Institution:
    id: str
    name: str
    tier: str


@dataclass(frozen=True)
class LinkPath:
    """一条可用链路；failure_domain 标识其所属故障域，用于共享风险判断。"""

    id: str
    institution_id: str
    kind: LinkKind
    failure_domain: str


@dataclass(frozen=True)
class ProbeSnapshot:
    """链路探测快照；只在新鲜度窗口内参与准入与重估。"""

    link_id: str
    measured_at: datetime
    latency_ms: float
    loss_pct: float
    availability: float


@dataclass
class Authorization:
    """授权/患者同意：带有效期，撤销后不可恢复。"""

    id: str
    kind: AuthorizationKind
    subject_id: str
    scope: str
    valid_from: datetime
    valid_until: datetime
    status: AuthorizationStatus
    payload: dict = field(default_factory=dict)
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class Patient:
    """患者身份信息属于敏感字段，接口层按职责过滤。"""

    ref: str
    name: str
    medical_record_no: str


@dataclass
class Consultation:
    id: str
    idempotency_key: str
    status: ConsultationStatus
    phase: PhaseKind
    slot_start: datetime
    slot_end: datetime
    doctor_id: str
    equipment_id: str
    patient: Patient
    min_service_level: ServiceLevel
    current_service_level: ServiceLevel | None
    institution_ids: list[str]
    primary_link_id: str
    backup_link_id: str | None
    active_link_id: str
    consent_id: str
    shared_risk: bool
    pre_pause_status: ConsultationStatus | None = None
    pause_reason: str | None = None
    cancel_reason: str | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ResourceHold:
    """资源占用记录；持久化保存，进程重启后依然生效。"""

    id: str
    consultation_id: str
    resource_type: ResourceType
    resource_id: str
    slot_start: datetime
    slot_end: datetime
    status: HoldStatus


@dataclass(frozen=True)
class ManualLock:
    """人工锁定：锁定期间禁止自动变迁，到期自动失效。"""

    id: str
    consultation_id: str
    operator: str
    reason: str
    created_at: datetime
    expires_at: datetime
    released_at: datetime | None = None


@dataclass
class OutboxEvent:
    """通知发件箱事件；与业务状态在同一事务中提交。"""

    id: str
    consultation_id: str
    type: str
    payload: dict
    status: OutboxStatus
    attempts: int
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
