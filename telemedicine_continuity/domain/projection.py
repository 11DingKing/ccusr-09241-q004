"""字段级返回投影：同一会诊按调用方职责呈现不同字段。

敏感字段（患者身份证号、联系电话、授权明细、锁定操作者等）
仅向具备相应职责的调用方返回。
"""

from __future__ import annotations

from .admission import effective_grade
from .enums import Role
from .models import Consultation, Link, Patient

_GRADE_NAME = {0: "NONE", 1: "AUDIO", 2: "SD_VIDEO", 3: "HD_VIDEO"}


def _mask(patient_id: str) -> str:
    tail = patient_id[-4:] if len(patient_id) >= 4 else "****"
    return f"****{tail}"


def project_patient(patient: Patient | None, role: Role) -> dict | None:
    if patient is None:
        return None
    base = {"patient_id": patient.patient_id}
    if role is Role.LINK_ENGINEER:
        # 链路工程师不需要也不能获得患者标识
        return {"patient_id": _mask(patient.patient_id), "name": "***"}
    if role is Role.COORDINATOR:
        # 排班协调仅需姓名核对
        return {**base, "name": patient.name}
    # clinician / auditor 可见完整信息
    return {
        **base,
        "name": patient.name,
        "national_id": patient.national_id,
        "contact_phone": patient.contact_phone,
    }


def project_link(link: Link | None, *, include_identity: bool = True) -> dict | None:
    if link is None:
        return None
    data = {
        "link_id": link.link_id,
        "fault_domain": link.fault_domain,
        "grade": _GRADE_NAME[link.grade],
        "health": link.health.value,
        "latency_ms": link.latency_ms,
        "loss_rate": link.loss_rate,
        "snapshot_at": link.snapshot_at,
        "effective_grade": _GRADE_NAME[effective_grade(link)],
        "org_ids": list(link.org_ids),
    }
    if include_identity:
        data["name"] = link.name
    return data


def project_consultation(
    c: Consultation,
    *,
    role: Role,
    patient: Patient | None = None,
) -> dict:
    """按职责裁剪会诊视图。"""
    view: dict = {
        "consultation_id": c.consultation_id,
        "code": c.code,
        "status": c.status.value,
        "clinician_id": c.clinician_id,
        "slot_id": c.slot_id,
        "org_ids": list(c.org_ids),
        "min_grade": _GRADE_NAME[c.min_grade],
        "required_link_count": c.required_link_count,
        "active_link_id": c.active_link_id,
        "backup_link_id": c.backup_link_id,
        "effective_grade": _GRADE_NAME[c.effective_grade],
        "critical_phase": c.critical_phase.value,
        "pause_reason": c.pause_reason,
        "locked": c.locked_by is not None,
        "pending_effect": c.pending_effect,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "version": c.version,
        "patient": project_patient(patient, role) if patient else None,
    }

    # 授权明细：链路工程师不可见
    if role is not Role.LINK_ENGINEER:
        view["consent_state"] = c.consent_state
        view["consent_valid_until"] = c.consent_valid_until

    # 锁定操作者：仅协调方与审计可见
    if role in (Role.COORDINATOR, Role.AUDITOR):
        view["locked_by"] = c.locked_by

    return view
