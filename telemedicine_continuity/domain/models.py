"""领域数据模型（贫血结构，业务规则在 admission/lifecycle 中）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import ConsultationStatus, CriticalPhase, Health


@dataclass(frozen=True)
class Organization:
    org_id: str
    name: str


@dataclass(frozen=True)
class Clinician:
    clinician_id: str
    name: str
    org_id: str


@dataclass(frozen=True)
class Patient:
    patient_id: str
    name: str
    national_id: str
    contact_phone: str


@dataclass(frozen=True)
class Slot:
    slot_id: str
    clinician_id: str
    start_time: str
    end_time: str
    status: str
    consultation_id: str | None


@dataclass(frozen=True)
class Link:
    link_id: str
    name: str
    fault_domain: str
    grade: int
    health: Health
    latency_ms: int | None
    loss_rate: float | None
    org_ids: tuple[str, ...]
    snapshot_at: str | None


@dataclass(frozen=True)
class Consent:
    patient_id: str
    state: str
    valid_from: str
    valid_until: str


@dataclass
class Consultation:
    consultation_id: str
    code: str
    patient_id: str
    clinician_id: str
    slot_id: str
    link_ids: tuple[str, ...]
    org_ids: tuple[str, ...]
    min_grade: int
    required_link_count: int
    status: ConsultationStatus
    active_link_id: str
    backup_link_id: str | None
    effective_grade: int
    consent_valid_until: str
    consent_state: str
    critical_phase: CriticalPhase
    pause_reason: str | None
    locked_by: str | None
    pending_effect: str | None
    idem_key: str | None
    created_at: str
    updated_at: str
    version: int

    def is_active(self) -> bool:
        return self.status not in (ConsultationStatus.CANCELLED, ConsultationStatus.COMPLETED)


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    event_key: str
    event_type: str
    aggregate_id: str
    payload: dict
    status: str
    attempts: int
    last_error: str | None
    created_at: str
    sent_at: str | None


@dataclass(frozen=True)
class AdmissionIssue:
    code: str
    message: str


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    issues: tuple[AdmissionIssue, ...] = field(default_factory=tuple)
    active_link_id: str | None = None
    backup_link_id: str | None = None
    effective_grade: int = 0
    shared_domains: tuple[str, ...] = ()
