"""条件变化时的反应策略（纯领域决策，由服务层落地）。

运行期与排班期标准不同：排班要求 required_link_count 条故障域独立链路齐备；
运行期以“维持诊疗连续”为优先——存在更优跨域链路就切换，都不可用才暂停，
链路连通但等级不足时有限降级。

三种特殊情形遵循不同规则：
- 患者同意失效：正常阶段必须取消；关键操作阶段登记“阶段结束后取消”，延后落地；
- 关键操作阶段：切换/暂停/取消等打断性动作一律延后，仅允许有限降级；
- 人工锁定：策略层不决策，由服务层直接跳过自动动作（仅锁定者可人工处置）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.admission import (
    DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
    effective_grade,
)
from ..domain.enums import ConsentStatus, Health
from ..domain.models import Consultation, Consent, Link
from ..infra.clock import Clock, parse_iso

REASON_LINK_DOWN = "link_down"
REASON_GRADE = "grade_below_sla"
REASON_CONSENT = "consent_invalid"
REASON_STALE = "snapshot_stale"


@dataclass(frozen=True)
class Reaction:
    action: str  # none/switch/degrade/pause/resume/restore/cancel/defer
    target_link_id: str | None = None
    backup_link_id: str | None = None
    effective_grade: int | None = None
    reason: str = ""


def _consent_invalid(consent: Consent | None, now_iso: str, slot_end: str) -> str | None:
    if consent is None:
        return "缺少患者授权同意记录"
    if consent.state != ConsentStatus.GRANTED.value:
        return "患者授权已撤销"
    if parse_iso(consent.valid_until) < parse_iso(now_iso):
        return f"患者授权已于 {consent.valid_until} 到期"
    if parse_iso(consent.valid_until) < parse_iso(slot_end):
        return "患者授权有效期不能覆盖诊疗结束时间"
    return None


def _eligible_links(
    c: Consultation,
    links: dict[str, Link],
    *,
    now_iso: str,
    snapshot_max_age_seconds: int,
) -> list[Link]:
    """当前新鲜探测、健康且等级不低于 SLA 的候选链路。"""
    now = parse_iso(now_iso)
    result: list[Link] = []
    for link in links.values():
        if not all(org in link.org_ids for org in c.org_ids):
            continue
        if link.health not in (Health.UP, Health.DEGRADED):
            continue
        if link.snapshot_at is None:
            continue
        if (now - parse_iso(link.snapshot_at)).total_seconds() > snapshot_max_age_seconds:
            continue
        if effective_grade(link) < c.min_grade:
            continue
        result.append(link)
    result.sort(key=effective_grade, reverse=True)
    return result


def _pick_alternative(eligible: list[Link], current: Link | None) -> Link | None:
    """优先选择与当前主链路不同故障域的最优链路；无跨域时退回同域最优。"""
    if current is None:
        return eligible[0] if eligible else None
    cross = [l for l in eligible if l.fault_domain != current.fault_domain]
    return cross[0] if cross else (eligible[0] if eligible else None)


def _fresh_and_healthy(link: Link | None, *, now_iso: str, max_age: int) -> bool:
    if link is None or link.health not in (Health.UP, Health.DEGRADED):
        return False
    if link.snapshot_at is None:
        return False
    return (parse_iso(now_iso) - parse_iso(link.snapshot_at)).total_seconds() <= max_age


def select_switch(
    c: Consultation,
    links: dict[str, Link],
    *,
    clock: Clock,
    snapshot_max_age_seconds: int = DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
) -> tuple[Link, Link | None] | None:
    """人工/自动共用的择路：返回（新主链路，新备选）；无可用跨域链路时返回 None。"""
    now_iso = clock.now_iso()
    eligible = _eligible_links(
        c, links, now_iso=now_iso, snapshot_max_age_seconds=snapshot_max_age_seconds
    )
    active = links.get(c.active_link_id)
    target = _pick_alternative(
        [l for l in eligible if l.link_id != c.active_link_id], active
    )
    if target is None:
        return None
    backup = next(
        (l for l in eligible
         if l.link_id != target.link_id and l.fault_domain != target.fault_domain),
        None,
    )
    return target, backup


def decide_reaction(
    c: Consultation,
    *,
    slot_end: str,
    links: dict[str, Link],
    consent: Consent | None,
    clock: Clock,
    snapshot_max_age_seconds: int = DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
) -> Reaction:
    """根据最新快照与授权推导应当发生的动作。调用方负责锁定/落地门控。"""
    now_iso = clock.now_iso()
    critical = c.critical_phase.value == "critical"

    consent_problem = _consent_invalid(consent, now_iso, slot_end)
    if consent_problem:
        if critical:
            return Reaction("defer", reason=REASON_CONSENT + ":" + consent_problem)
        return Reaction("cancel", reason=REASON_CONSENT + ":" + consent_problem)

    active = links.get(c.active_link_id)
    eligible = _eligible_links(
        c, links, now_iso=now_iso, snapshot_max_age_seconds=snapshot_max_age_seconds
    )
    alternative = _pick_alternative(
        [l for l in eligible if l.link_id != c.active_link_id], active
    )
    active_usable = _fresh_and_healthy(
        active, now_iso=now_iso, max_age=snapshot_max_age_seconds
    )
    active_grade = effective_grade(active) if active is not None else 0

    # 暂停后的恢复判断优先：链路可用即恢复
    if c.status.value == "paused" and (c.pause_reason or "").startswith(REASON_LINK_DOWN):
        if active_usable and active_grade >= c.min_grade:
            target = active
            backup = alternative
        elif alternative is not None:
            target = alternative
            active_for_backup = active
            backup = _pick_alternative(
                [l for l in eligible if l.link_id != target.link_id
                 and l.fault_domain != target.fault_domain],
                target,
            )
        else:
            target = None
            backup = None
        if target is not None:
            return Reaction(
                "resume", target_link_id=target.link_id,
                backup_link_id=backup.link_id if backup else None,
                effective_grade=effective_grade(target),
                reason="链路恢复，解除暂停",
            )
        return Reaction("none")

    # 1) 主链路不可用（中断/未知/快照失鲜）
    if not active_usable:
        stale = active is not None and active.health in (Health.UP, Health.DEGRADED) \
            and not _fresh_and_healthy(active, now_iso=now_iso,
                                       max_age=snapshot_max_age_seconds)
        reason = (REASON_STALE + ":主链路探测快照失鲜") if stale \
            else (REASON_LINK_DOWN + f":主链路 {c.active_link_id} 不可用")
        if alternative is None:
            if critical:
                return Reaction("defer", reason=REASON_LINK_DOWN + ":主链路不可用且关键操作阶段不可切换")
            return Reaction("pause", reason=reason + "，且无可用备选")
        if critical:
            return Reaction("defer", reason=REASON_LINK_DOWN + ":关键操作阶段不可切换，待阶段结束处理")
        return Reaction(
            "switch", target_link_id=alternative.link_id,
            backup_link_id=None,
            effective_grade=effective_grade(alternative),
            reason=reason + "，切换至可用备选路径",
        )

    # 2) 主链路连通但等级低于 SLA
    if active_grade < c.min_grade:
        if alternative is not None and not critical:
            return Reaction(
                "switch", target_link_id=alternative.link_id,
                backup_link_id=None,
                effective_grade=effective_grade(alternative),
                reason=REASON_GRADE + ":主链路等级不足，切换至达标备选",
            )
        # 无备选，或关键阶段：有限降级继续
        return Reaction(
            "degrade", target_link_id=active.link_id, effective_grade=active_grade,
            reason=REASON_GRADE + ":无达 SLA 的备选路径，有限降级继续",
        )

    # 3) 主链路正常且达 SLA
    if c.status.value == "degraded":
        # 等级恢复即结束有限降级（冗余状态通过 backup_link_id 体现）
        backup = next((l for l in eligible if l.link_id != active.link_id
                       and l.fault_domain != active.fault_domain), None)
        return Reaction(
            "restore", target_link_id=active.link_id,
            backup_link_id=backup.link_id if backup else None,
            effective_grade=active_grade, reason="主链路服务等级已恢复",
        )

    # 4) 平静期刷新备选指针（冗余丧失时 backup 为空，不产生通知；
    #    此后主链路一旦故障即落入暂停分支）
    backup = next((l for l in eligible if l.link_id != active.link_id
                   and l.fault_domain != active.fault_domain), None)
    backup_id = backup.link_id if backup else None
    if backup_id != c.backup_link_id:
        return Reaction("none", target_link_id=active.link_id,
                        backup_link_id=backup_id, effective_grade=active_grade)
    return Reaction("none")
