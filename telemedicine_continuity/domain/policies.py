"""领域策略：准入评估、链路质量底线与授权有效期判断。

全部为纯函数，便于确定性测试与复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .enums import AuthorizationKind, AuthorizationStatus, LinkKind, ServiceLevel
from .models import Authorization, LinkPath, ProbeSnapshot

# 探测快照新鲜度窗口：超出该窗口的探测结果不得参与判断。
PROBE_FRESHNESS = timedelta(minutes=5)

# 人工锁定时长：到期后恢复自动重估。
LOCK_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class QualityFloor:
    max_latency_ms: float
    max_loss_pct: float
    min_availability: float


SERVICE_LEVEL_FLOORS: dict[ServiceLevel, QualityFloor] = {
    ServiceLevel.GOLD: QualityFloor(max_latency_ms=80, max_loss_pct=0.5, min_availability=0.999),
    ServiceLevel.SILVER: QualityFloor(max_latency_ms=150, max_loss_pct=1.0, min_availability=0.995),
    ServiceLevel.BRONZE: QualityFloor(max_latency_ms=300, max_loss_pct=3.0, min_availability=0.990),
}


def degraded_floor(level: ServiceLevel) -> QualityFloor | None:
    """有限降级底线：最多降一档；BRONZE 不允许再降级。"""
    if level == ServiceLevel.GOLD:
        return SERVICE_LEVEL_FLOORS[ServiceLevel.SILVER]
    if level == ServiceLevel.SILVER:
        return SERVICE_LEVEL_FLOORS[ServiceLevel.BRONZE]
    return None


def degraded_level(level: ServiceLevel) -> ServiceLevel | None:
    if level == ServiceLevel.GOLD:
        return ServiceLevel.SILVER
    if level == ServiceLevel.SILVER:
        return ServiceLevel.BRONZE
    return None


def probe_is_fresh(snapshot: ProbeSnapshot | None, now: datetime) -> bool:
    return snapshot is not None and now - snapshot.measured_at <= PROBE_FRESHNESS


def quality_meets(
    snapshot: ProbeSnapshot | None, floor: QualityFloor, now: datetime
) -> bool:
    """快照在新鲜度窗口内且各项指标满足底线。"""
    if not probe_is_fresh(snapshot, now):
        return False
    return (
        snapshot.latency_ms <= floor.max_latency_ms
        and snapshot.loss_pct <= floor.max_loss_pct
        and snapshot.availability >= floor.min_availability
    )


def authorization_covers(
    auth: Authorization | None, now: datetime, slot_start: datetime, slot_end: datetime
) -> bool:
    """授权必须处于有效状态且有效期完整覆盖诊疗时段。"""
    if auth is None or auth.status != AuthorizationStatus.ACTIVE:
        return False
    return auth.valid_from <= slot_start and auth.valid_until >= slot_end and auth.valid_until >= now


@dataclass(frozen=True)
class AdmissionDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    shared_risk: bool = False


def evaluate_admission(
    *,
    now: datetime,
    slot_start: datetime,
    slot_end: datetime,
    min_level: ServiceLevel,
    institution_ids: list[str],
    primary_link: LinkPath | None,
    backup_link: LinkPath | None,
    primary_probe: ProbeSnapshot | None,
    backup_probe: ProbeSnapshot | None,
    consent: Authorization | None,
    institution_auths: dict[str, Authorization | None],
) -> AdmissionDecision:
    """把诊疗时段、参与机构、链路快照、共享风险、最低服务等级与授权有效期一起纳入准入判断。"""
    reasons: list[str] = []
    warnings: list[str] = []

    if slot_end <= slot_start:
        reasons.append("SLOT_WINDOW_INVALID")

    if consent is None:
        reasons.append("CONSENT_MISSING")
    elif consent.kind != AuthorizationKind.PATIENT_CONSENT:
        reasons.append("CONSENT_KIND_INVALID")
    elif not authorization_covers(consent, now, slot_start, slot_end):
        reasons.append(
            "CONSENT_REVOKED" if consent.status == AuthorizationStatus.REVOKED else "CONSENT_WINDOW_INVALID"
        )

    for inst_id in institution_ids:
        auth = institution_auths.get(inst_id)
        if auth is None:
            reasons.append(f"INSTITUTION_AUTH_MISSING:{inst_id}")
        elif not authorization_covers(auth, now, slot_start, slot_end):
            reasons.append(f"INSTITUTION_AUTH_INVALID:{inst_id}")

    floor = SERVICE_LEVEL_FLOORS[min_level]
    if primary_link is None:
        reasons.append("PRIMARY_LINK_MISSING")
    elif primary_link.kind != LinkKind.PRIMARY:
        reasons.append("PRIMARY_LINK_KIND_INVALID")
    elif primary_link.institution_id not in institution_ids:
        reasons.append("PRIMARY_LINK_INSTITUTION_MISMATCH")
    elif not probe_is_fresh(primary_probe, now):
        reasons.append("PRIMARY_PROBE_STALE")
    elif not quality_meets(primary_probe, floor, now):
        reasons.append("PRIMARY_LINK_BELOW_SERVICE_LEVEL")

    shared_risk = False
    requires_diverse_backup = min_level in (ServiceLevel.GOLD, ServiceLevel.SILVER)
    if backup_link is None:
        if requires_diverse_backup:
            reasons.append("BACKUP_LINK_MISSING")
    else:
        if backup_link.kind != LinkKind.BACKUP:
            reasons.append("BACKUP_LINK_KIND_INVALID")
        elif backup_link.institution_id not in institution_ids:
            reasons.append("BACKUP_LINK_INSTITUTION_MISMATCH")
        else:
            if primary_link is not None and backup_link.failure_domain == primary_link.failure_domain:
                shared_risk = True
                if requires_diverse_backup:
                    reasons.append("SHARED_FAILURE_DOMAIN")
                else:
                    warnings.append("SHARED_FAILURE_DOMAIN")
            if not probe_is_fresh(backup_probe, now):
                reasons.append("BACKUP_PROBE_STALE")
            elif not quality_meets(backup_probe, floor, now):
                reasons.append("BACKUP_LINK_BELOW_SERVICE_LEVEL")

    return AdmissionDecision(
        approved=not reasons,
        reasons=reasons,
        warnings=warnings,
        shared_risk=shared_risk,
    )
