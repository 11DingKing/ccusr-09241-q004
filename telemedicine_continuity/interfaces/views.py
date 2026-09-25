"""按调用方职责过滤的会诊视图。

敏感字段可见性：
- 患者姓名/病历号、同意书载荷：仅 clinician 与 admin 可见；auditor 看到脱敏占位；
- 链路探测指标与故障域：仅 ops 与 admin 可见；
- 其余角色仅获得履行其职责所需的最小字段集。
"""

from __future__ import annotations

from ..domain.enums import Role
from ..domain.models import Consultation
from ..infrastructure.sqlite_store import SQLiteStore

MASKED = "***"

_BASE_FIELDS = (
    "id", "status", "phase", "slot", "min_service_level", "current_service_level",
    "shared_risk", "cancel_reason", "pause_reason", "version",
)

_ROLE_FIELDS: dict[Role, tuple[str, ...]] = {
    Role.SCHEDULER: _BASE_FIELDS + (
        "institution_ids", "doctor_id", "equipment_id", "patient_ref",
        "consent_summary", "link_summary", "lock",
    ),
    Role.CLINICIAN: _BASE_FIELDS + (
        "institution_ids", "doctor_id", "equipment_id", "patient", "consent", "link_summary", "lock",
    ),
    Role.OPS: _BASE_FIELDS + (
        "institution_ids", "patient_ref", "consent_summary", "links", "lock",
    ),
    Role.AUDITOR: _BASE_FIELDS + (
        "institution_ids", "doctor_id", "equipment_id", "patient_masked",
        "consent_masked", "links", "lock",
    ),
    Role.ADMIN: _BASE_FIELDS + (
        "institution_ids", "doctor_id", "equipment_id", "patient", "consent", "links", "lock",
    ),
}


def _full_view(store: SQLiteStore, c: Consultation, now) -> dict:
    consent = store.get_authorization(c.consent_id)
    lock = store.active_lock(c.id, now)

    def link_block(link_id: str | None) -> dict | None:
        if link_id is None:
            return None
        link = store.get_link(link_id)
        probe = store.latest_probe(link_id)
        return {
            "id": link_id,
            "kind": link.kind.value if link else None,
            "failure_domain": link.failure_domain if link else None,
            "quality": (
                {
                    "measured_at": probe.measured_at.isoformat(),
                    "latency_ms": probe.latency_ms,
                    "loss_pct": probe.loss_pct,
                    "availability": probe.availability,
                }
                if probe
                else None
            ),
        }

    return {
        "id": c.id,
        "status": c.status.value,
        "phase": c.phase.value,
        "slot": {"start": c.slot_start.isoformat(), "end": c.slot_end.isoformat()},
        "min_service_level": c.min_service_level.value,
        "current_service_level": c.current_service_level.value if c.current_service_level else None,
        "shared_risk": c.shared_risk,
        "cancel_reason": c.cancel_reason,
        "pause_reason": c.pause_reason,
        "version": c.version,
        "institution_ids": list(c.institution_ids),
        "doctor_id": c.doctor_id,
        "equipment_id": c.equipment_id,
        "patient": {
            "ref": c.patient.ref,
            "name": c.patient.name,
            "medical_record_no": c.patient.medical_record_no,
        },
        "patient_ref": c.patient.ref,
        "patient_masked": {
            "ref": c.patient.ref,
            "name": MASKED,
            "medical_record_no": MASKED,
        },
        "consent": (
            {
                "id": consent.id,
                "status": consent.status.value,
                "valid_from": consent.valid_from.isoformat(),
                "valid_until": consent.valid_until.isoformat(),
                "payload": consent.payload,
            }
            if consent
            else None
        ),
        "consent_summary": (
            {
                "id": consent.id,
                "status": consent.status.value,
                "valid_until": consent.valid_until.isoformat(),
            }
            if consent
            else None
        ),
        "consent_masked": (
            {
                "id": consent.id,
                "status": consent.status.value,
                "valid_until": consent.valid_until.isoformat(),
                "payload": MASKED,
            }
            if consent
            else None
        ),
        "links": {
            "primary": link_block(c.primary_link_id),
            "backup": link_block(c.backup_link_id),
            "active_link_id": c.active_link_id,
        },
        "link_summary": {"active_link_id": c.active_link_id},
        "lock": (
            {
                "operator": lock.operator,
                "reason": lock.reason,
                "expires_at": lock.expires_at.isoformat(),
            }
            if lock
            else None
        ),
    }


def consultation_view(store: SQLiteStore, c: Consultation, role: Role, now) -> dict:
    """按角色白名单裁剪完整视图；空值字段不下发。"""
    full = _full_view(store, c, now)
    return {key: full[key] for key in _ROLE_FIELDS[role] if full.get(key) is not None}
