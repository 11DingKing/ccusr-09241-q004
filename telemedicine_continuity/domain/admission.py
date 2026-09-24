"""会诊准入判断（纯领域规则）。

一次性纳入：诊疗时段、参与机构、链路探测快照、共享风险（故障域）、
最低服务等级、授权（患者同意）有效期。判断不产生任何副作用。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..infra.clock import Clock, parse_iso
from .enums import ConsentStatus, Grade, Health
from .models import AdmissionDecision, AdmissionIssue, Consent, Link

# 探测快照超过该时长即视为不可信
DEFAULT_SNAPSHOT_MAX_AGE_SECONDS = 300
# 劣化链路相对标称等级下降的级数
DEGRADED_GRADE_PENALTY = 1


@dataclass(frozen=True)
class AdmissionRequest:
    patient_id: str
    clinician_id: str
    slot_start: str
    slot_end: str
    org_ids: tuple[str, ...]
    min_grade: int
    required_link_count: int  # 需要几条故障域相互独立的链路（1=无冗余要求）
    candidate_link_ids: tuple[str, ...]  # 空表示在全部已注册链路中选择


def effective_grade(link: Link) -> int:
    """根据探测健康度计算链路当下实际可承载等级。"""
    grade = int(link.grade)
    if link.health is Health.DEGRADED:
        grade = max(int(Grade.NONE), grade - DEGRADED_GRADE_PENALTY)
    return grade


def _covers_orgs(link: Link, org_ids: tuple[str, ...]) -> bool:
    return all(org_id in link.org_ids for org_id in org_ids)


def evaluate_admission(
    req: AdmissionRequest,
    *,
    links: dict[str, Link],
    consent: Consent | None,
    clock: Clock,
    snapshot_max_age_seconds: int = DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
) -> AdmissionDecision:
    issues: list[AdmissionIssue] = []

    # 1) 患者同意：状态与有效期必须完整覆盖诊疗时段
    if consent is None:
        issues.append(AdmissionIssue("consent_missing", "缺少患者授权同意记录"))
    else:
        if consent.state != ConsentStatus.GRANTED.value:
            issues.append(AdmissionIssue("consent_revoked", "患者授权已撤销，不得安排会诊"))
        slot_end = parse_iso(req.slot_end)
        valid_until = parse_iso(consent.valid_until)
        if valid_until < clock.now():
            issues.append(AdmissionIssue("consent_expired", f"患者授权已于 {consent.valid_until} 到期"))
        elif valid_until < slot_end:
            issues.append(
                AdmissionIssue(
                    "consent_not_covers_slot",
                    f"患者授权有效期 {consent.valid_until} 不能覆盖诊疗结束时间 {req.slot_end}",
                )
            )

    if req.required_link_count < 1:
        issues.append(AdmissionIssue("invalid_required_link_count", "所需链路数量必须大于等于 1"))

    # 2) 圈定候选链路
    if req.candidate_link_ids:
        scoped = [links[k] for k in req.candidate_link_ids if k in links]
        missing = set(req.candidate_link_ids) - set(links)
        if missing:
            issues.append(
                AdmissionIssue("link_unknown", f"链路不存在：{', '.join(sorted(missing))}")
            )
    else:
        scoped = list(links.values())

    # 3) 过滤：机构覆盖、健康度、快照时效、最低等级（仅收集原因，最终是否致命取决于能否择优成功）
    eligible: list[Link] = []
    down_or_uncovered: list[str] = []
    stale: list[str] = []
    too_low: list[str] = []
    now = clock.now()

    for link in scoped:
        if not _covers_orgs(link, req.org_ids):
            down_or_uncovered.append(f"{link.link_id}(未覆盖参与机构)")
            continue
        if link.health in (Health.DOWN, Health.UNKNOWN):
            down_or_uncovered.append(f"{link.link_id}({link.health.value})")
            continue
        if link.snapshot_at is None:
            stale.append(f"{link.link_id}(无探测快照)")
            continue
        age = (now - parse_iso(link.snapshot_at)).total_seconds()
        if age > snapshot_max_age_seconds:
            stale.append(f"{link.link_id}(快照过期 {int(age)}s)")
            continue
        if effective_grade(link) < req.min_grade:
            too_low.append(
                f"{link.link_id}(实际等级 {Grade(effective_grade(link)).name} "
                f"< 要求 {Grade(req.min_grade).name})"
            )
            continue
        eligible.append(link)

    # 4) 在合格链路中按“故障域互斥”贪心择优：等级高者优先，同域只取一条
    selected: list[Link] = []
    used_domains: set[str] = set()
    for link in sorted(eligible, key=lambda item: effective_grade(item), reverse=True):
        if link.fault_domain in used_domains:
            continue
        used_domains.add(link.fault_domain)
        selected.append(link)
        if len(selected) == req.required_link_count:
            break

    shared_domains: tuple[str, ...] = ()
    if len(selected) < req.required_link_count:
        # 择优失败，候选链路层面的问题此时才作为致命原因返回
        if down_or_uncovered:
            issues.append(
                AdmissionIssue("link_unavailable",
                               "存在不可用或不覆盖机构的链路：" + "，".join(down_or_uncovered))
            )
        if stale:
            issues.append(AdmissionIssue("snapshot_stale", "探测快照不可信：" + "，".join(stale)))
        if too_low:
            issues.append(AdmissionIssue("grade_insufficient",
                                         "链路服务等级不足：" + "，".join(too_low)))
        # 说明差在哪：合格链路集中在同一故障域？
        domain_counts: dict[str, int] = {}
        for link in eligible:
            domain_counts[link.fault_domain] = domain_counts.get(link.fault_domain, 0) + 1
        shared = sorted(d for d, count in domain_counts.items() if count >= 2)
        if len(domain_counts) < req.required_link_count:
            shared_domains = tuple(shared)
            if eligible and len(domain_counts) == 1 and req.required_link_count > 1:
                issues.append(
                    AdmissionIssue(
                        "shared_fault_domain",
                        f"主备链路共享同一故障域 {next(iter(domain_counts))}，"
                        f"该故障域失效时无可用备选路径",
                    )
                )
            else:
                issues.append(
                    AdmissionIssue(
                        "independent_link_insufficient",
                        f"需要 {req.required_link_count} 条故障域独立的链路，"
                        f"当前仅有 {len(domain_counts)} 个不同故障域可用",
                    )
                )

    if issues:
        return AdmissionDecision(
            allowed=False,
            issues=tuple(issues),
            shared_domains=shared_domains,
        )

    active, backup = selected[0], selected[1] if len(selected) > 1 else None
    return AdmissionDecision(
        allowed=True,
        active_link_id=active.link_id,
        backup_link_id=backup.link_id if backup else None,
        effective_grade=effective_grade(active),
        shared_domains=shared_domains,
    )
