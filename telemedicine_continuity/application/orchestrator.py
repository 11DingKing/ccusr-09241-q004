"""编排服务：准入判断、条件变化重估与会诊生命周期操作。

规则要点：
- 患者同意失效：无论处于何种阶段、是否被人工锁定，一律强制取消；
- 关键操作阶段：仅允许满足完整服务等级的无缝切换，禁止自动降级/暂停/取消；
- 人工锁定：锁定期间禁止一切自动变迁，仅提示人工复核；
- 所有状态变迁与发件箱事件在同一事务提交。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..domain.enums import (
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
from ..domain.errors import AdmissionRejected, InvalidTransition, SlotConflict
from ..domain.models import (
    Authorization,
    Consultation,
    Institution,
    LinkPath,
    ManualLock,
    OutboxEvent,
    Patient,
    ProbeSnapshot,
    ResourceHold,
)
from ..domain.policies import (
    LOCK_TTL,
    SERVICE_LEVEL_FLOORS,
    authorization_covers,
    degraded_floor,
    degraded_level,
    evaluate_admission,
    quality_meets,
)
from ..infrastructure.sqlite_store import SQLiteStore
from .ports import Clock, IdGenerator

# 发件箱事件类型
EVT_CONFIRMED = "CONSULTATION_CONFIRMED"
EVT_PATH_SWITCHED = "PATH_SWITCHED"
EVT_DEGRADED = "CONSULTATION_DEGRADED"
EVT_PAUSED = "CONSULTATION_PAUSED"
EVT_RESUMED = "CONSULTATION_RESUMED"
EVT_CANCELLED = "CONSULTATION_CANCELLED"
EVT_COMPLETED = "CONSULTATION_COMPLETED"
EVT_CONSENT_INVALID = "CONSENT_INVALID_CANCELLED"
EVT_RISK_ESCALATION = "RISK_ESCALATION"
EVT_MANUAL_REVIEW = "MANUAL_REVIEW_REQUIRED"


class OrchestratorService:
    def __init__(self, store: SQLiteStore, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    # ------------------------------------------------------------------
    # 注册类操作
    # ------------------------------------------------------------------
    def register_institution(self, inst_id: str, name: str, tier: str) -> dict:
        with self._store.transaction():
            self._store.upsert_institution(Institution(id=inst_id, name=name, tier=tier))
        return {"id": inst_id}

    def register_link(self, link_id: str, institution_id: str, kind: str, failure_domain: str) -> dict:
        with self._store.transaction():
            self._store.upsert_link(
                LinkPath(
                    id=link_id,
                    institution_id=institution_id,
                    kind=LinkKind(kind),
                    failure_domain=failure_domain,
                )
            )
        return {"id": link_id}

    def record_probe(
        self,
        link_id: str,
        latency_ms: float,
        loss_pct: float,
        availability: float,
        measured_at: datetime | None = None,
    ) -> dict:
        """写入探测快照，并在同一事务内重估所有受影响的会诊。"""
        now = self._clock.now()
        snap = ProbeSnapshot(
            link_id=link_id,
            measured_at=measured_at or now,
            latency_ms=latency_ms,
            loss_pct=loss_pct,
            availability=availability,
        )
        changed: list[dict] = []
        with self._store.transaction():
            self._store.add_probe(snap)
            for c in self._store.list_consultations_touching_link(link_id):
                outcome = self._reevaluate_locked(c, trigger=f"probe:{link_id}")
                if outcome:
                    changed.append(outcome)
        return {"link_id": link_id, "reevaluated": changed}

    def grant_authorization(
        self,
        auth_id: str,
        kind: str,
        subject_id: str,
        scope: str,
        valid_from: datetime,
        valid_until: datetime,
        payload: dict | None = None,
    ) -> dict:
        auth = Authorization(
            id=auth_id,
            kind=AuthorizationKind(kind),
            subject_id=subject_id,
            scope=scope,
            valid_from=valid_from,
            valid_until=valid_until,
            status=AuthorizationStatus.ACTIVE,
            payload=payload or {},
        )
        resumed: list[dict] = []
        with self._store.transaction():
            self._store.upsert_authorization(auth)
            # 新授权可能使此前暂停的会诊恢复
            for c in self._store.list_open_consultations():
                if c.status == ConsultationStatus.PAUSED and (
                    c.consent_id == auth_id or subject_id in c.institution_ids
                ):
                    outcome = self._reevaluate_locked(c, trigger=f"grant:{auth_id}")
                    if outcome:
                        resumed.append(outcome)
        return {"id": auth_id, "reevaluated": resumed}

    def revoke_authorization(self, auth_id: str) -> dict:
        """撤销授权：患者同意被撤销时强制取消；机构授权撤销按常规条件变化处理。"""
        now = self._clock.now()
        affected: list[dict] = []
        with self._store.transaction():
            auth = self._store.get_authorization(auth_id)
            if auth is None:
                from ..domain.errors import NotFound

                raise NotFound(f"授权不存在: {auth_id}")
            self._store.mark_authorization_revoked(auth_id, now)
            for c in self._store.list_open_consultations():
                if auth.kind == AuthorizationKind.PATIENT_CONSENT and c.consent_id == auth_id:
                    outcome = self._reevaluate_locked(c, trigger=f"revoke:{auth_id}")
                elif auth.kind == AuthorizationKind.INSTITUTION and auth.subject_id in c.institution_ids:
                    outcome = self._reevaluate_locked(c, trigger=f"revoke:{auth_id}")
                else:
                    outcome = None
                if outcome:
                    affected.append(outcome)
        return {"id": auth_id, "revoked": True, "affected": affected}

    # ------------------------------------------------------------------
    # 排班（准入 + 占用 + 幂等）
    # ------------------------------------------------------------------
    def schedule_consultation(
        self,
        *,
        idempotency_key: str,
        slot_start: datetime,
        slot_end: datetime,
        institution_ids: list[str],
        doctor_id: str,
        equipment_id: str,
        patient: dict,
        min_service_level: str,
        consent_id: str,
        primary_link_id: str,
        backup_link_id: str | None = None,
    ) -> dict:
        now = self._clock.now()
        with self._store.transaction():
            existing = self._store.find_consultation_by_idempotency_key(idempotency_key)
            if existing is not None:
                return {"consultation_id": existing.id, "status": existing.status.value,
                        "replayed": True, "shared_risk": existing.shared_risk, "warnings": []}

            level = ServiceLevel(min_service_level)
            primary = self._store.get_link(primary_link_id)
            backup = self._store.get_link(backup_link_id) if backup_link_id else None
            consent = self._store.get_authorization(consent_id)
            inst_auths = {
                iid: self._store.find_institution_authorization(iid) for iid in institution_ids
            }
            decision = evaluate_admission(
                now=now,
                slot_start=slot_start,
                slot_end=slot_end,
                min_level=level,
                institution_ids=institution_ids,
                primary_link=primary,
                backup_link=backup,
                primary_probe=self._store.latest_probe(primary_link_id),
                backup_probe=self._store.latest_probe(backup_link_id) if backup_link_id else None,
                consent=consent,
                institution_auths=inst_auths,
            )
            if not decision.approved:
                raise AdmissionRejected(decision.reasons)

            # 资源占用检查与写入处于同一串行化事务，构成原子临界区
            for rtype, rid in ((ResourceType.DOCTOR, doctor_id), (ResourceType.EQUIPMENT, equipment_id)):
                conflicts = self._store.find_overlapping_holds(rtype, rid, slot_start, slot_end)
                if conflicts:
                    raise SlotConflict(
                        f"资源 {rid} 在该时段已被占用",
                        details={"resource_id": rid, "conflict_consultation": conflicts[0].consultation_id},
                    )

            consultation_id = self._ids.new_id("con")
            consultation = Consultation(
                id=consultation_id,
                idempotency_key=idempotency_key,
                status=ConsultationStatus.CONFIRMED,
                phase=PhaseKind.PREP,
                slot_start=slot_start,
                slot_end=slot_end,
                doctor_id=doctor_id,
                equipment_id=equipment_id,
                patient=Patient(
                    ref=patient["ref"],
                    name=patient["name"],
                    medical_record_no=patient["medical_record_no"],
                ),
                min_service_level=level,
                current_service_level=level,
                institution_ids=list(institution_ids),
                primary_link_id=primary_link_id,
                backup_link_id=backup_link_id,
                active_link_id=primary_link_id,
                consent_id=consent_id,
                shared_risk=decision.shared_risk,
                created_at=now,
                updated_at=now,
            )
            try:
                self._store.insert_consultation(consultation)
            except sqlite3.IntegrityError:
                # 唯一约束兜底：并发写入同一幂等键时返回既有记录，不产生重复占用
                existing = self._store.find_consultation_by_idempotency_key(idempotency_key)
                return {
                    "consultation_id": existing.id,
                    "status": existing.status.value,
                    "replayed": True,
                    "shared_risk": existing.shared_risk,
                    "warnings": [],
                }
            for rtype, rid in ((ResourceType.DOCTOR, doctor_id), (ResourceType.EQUIPMENT, equipment_id)):
                self._store.insert_hold(
                    ResourceHold(
                        id=self._ids.new_id("hold"),
                        consultation_id=consultation_id,
                        resource_type=rtype,
                        resource_id=rid,
                        slot_start=slot_start,
                        slot_end=slot_end,
                        status=HoldStatus.HELD,
                    )
                )
            self._emit_locked(consultation_id, EVT_CONFIRMED, {
                "slot_start": slot_start.isoformat(),
                "slot_end": slot_end.isoformat(),
                "min_service_level": level.value,
                "shared_risk": decision.shared_risk,
            })
            self._store.add_evaluation_log(
                consultation_id, "schedule", "CONFIRMED", decision.warnings, now
            )
            return {
                "consultation_id": consultation_id,
                "status": ConsultationStatus.CONFIRMED.value,
                "replayed": False,
                "shared_risk": decision.shared_risk,
                "warnings": decision.warnings,
            }

    # ------------------------------------------------------------------
    # 手动操作
    # ------------------------------------------------------------------
    def perform_action(self, consultation_id: str, action: str, actor: str, **params: Any) -> dict:
        now = self._clock.now()
        with self._store.transaction():
            c = self._store.get_consultation(consultation_id)
            handler = {
                "start": self._act_start,
                "set_phase": self._act_set_phase,
                "lock": self._act_lock,
                "unlock": self._act_unlock,
                "switch_path": self._act_switch_path,
                "degrade": self._act_degrade,
                "pause": self._act_pause,
                "resume": self._act_resume,
                "cancel": self._act_cancel,
                "complete": self._act_complete,
            }.get(action)
            if handler is None:
                raise InvalidTransition(f"未知操作: {action}")
            return handler(c, actor, now, **params)

    def _require_open(self, c: Consultation) -> None:
        if c.status.is_terminal:
            raise InvalidTransition(f"会诊已终止（{c.status.value}），不可再操作")

    def _save_locked(self, c: Consultation, now: datetime) -> None:
        c.updated_at = now
        self._store.update_consultation(c, expected_version=c.version)

    def _act_start(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        self._require_open(c)
        if c.status != ConsultationStatus.CONFIRMED:
            raise InvalidTransition("仅已确认的会诊可以开始")
        self._ensure_consent_valid(c, now)
        c.status = ConsultationStatus.ACTIVE
        self._save_locked(c, now)
        return {"status": c.status.value}

    def _act_set_phase(self, c: Consultation, actor: str, now: datetime, **params: Any) -> dict:
        self._require_open(c)
        if c.status not in (ConsultationStatus.ACTIVE, ConsultationStatus.DEGRADED):
            raise InvalidTransition("仅进行中的会诊可以切换操作阶段")
        c.phase = PhaseKind(params["phase"])
        self._save_locked(c, now)
        return {"status": c.status.value, "phase": c.phase.value}

    def _act_lock(self, c: Consultation, actor: str, now: datetime, **params: Any) -> dict:
        self._require_open(c)
        reason = params.get("reason") or "人工锁定"
        self._store.insert_lock(
            ManualLock(
                id=self._ids.new_id("lock"),
                consultation_id=c.id,
                operator=actor,
                reason=reason,
                created_at=now,
                expires_at=now + LOCK_TTL,
            )
        )
        return {"locked": True, "operator": actor, "reason": reason}

    def _act_unlock(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        lock = self._store.active_lock(c.id, now)
        if lock is None:
            return {"locked": False}
        self._store.release_lock(lock.id, now)
        # 解锁后立即重估，把锁定期间积压的条件变化一次性处理
        outcome = self._reevaluate_locked(c, trigger=f"unlock:{actor}")
        return {"locked": False, "reevaluated": outcome}

    def _act_switch_path(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        self._require_open(c)
        if c.status in (ConsultationStatus.PAUSED,):
            raise InvalidTransition("暂停中的会诊请先恢复再切换链路")
        target = c.backup_link_id if c.active_link_id == c.primary_link_id else c.primary_link_id
        if target is None:
            raise InvalidTransition("没有可切换的备选链路")
        floor = SERVICE_LEVEL_FLOORS[c.current_service_level or c.min_service_level]
        if not quality_meets(self._store.latest_probe(target), floor, now):
            raise InvalidTransition("目标链路不满足当前服务等级底线")
        c.active_link_id = target
        self._save_locked(c, now)
        self._emit_locked(c.id, EVT_PATH_SWITCHED, {"active_link_id": target, "actor": actor})
        return {"status": c.status.value, "active_link_id": target}

    def _act_degrade(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        self._require_open(c)
        if c.phase == PhaseKind.CRITICAL:
            raise InvalidTransition("关键操作阶段禁止降级")
        if c.status not in (ConsultationStatus.CONFIRMED, ConsultationStatus.ACTIVE):
            raise InvalidTransition("当前状态不可降级")
        self._apply_degrade_locked(c, now)
        return {"status": c.status.value, "current_service_level": c.current_service_level.value}

    def _act_pause(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        self._require_open(c)
        if c.phase == PhaseKind.CRITICAL:
            raise InvalidTransition("关键操作阶段禁止暂停")
        self._ensure_consent_valid(c, now)
        self._apply_pause_locked(c, now, reason="MANUAL")
        return {"status": c.status.value}

    def _act_resume(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        self._require_open(c)
        if c.status != ConsultationStatus.PAUSED:
            raise InvalidTransition("仅暂停中的会诊可以恢复")
        outcome = self._reevaluate_locked(c, trigger=f"resume:{actor}")
        if c.status == ConsultationStatus.PAUSED:
            raise InvalidTransition("恢复条件未满足", details={"last_evaluation": outcome})
        return {"status": c.status.value}

    def _act_cancel(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        self._require_open(c)
        if c.phase == PhaseKind.CRITICAL:
            raise InvalidTransition("关键操作阶段禁止取消；若患者同意失效系统将自动强制取消")
        self._apply_cancel_locked(c, now, reason="MANUAL")
        return {"status": c.status.value}

    def _act_complete(self, c: Consultation, actor: str, now: datetime, **_: Any) -> dict:
        self._require_open(c)
        if c.status not in (ConsultationStatus.ACTIVE, ConsultationStatus.DEGRADED):
            raise InvalidTransition("仅进行中的会诊可以完成")
        c.status = ConsultationStatus.COMPLETED
        self._save_locked(c, now)
        self._store.release_holds(c.id)
        self._emit_locked(c.id, EVT_COMPLETED, {"actor": actor})
        return {"status": c.status.value}

    # ------------------------------------------------------------------
    # 重估（条件变化的核心入口）
    # ------------------------------------------------------------------
    def reevaluate(self, consultation_id: str, trigger: str = "manual") -> dict | None:
        with self._store.transaction():
            c = self._store.get_consultation(consultation_id)
            return self._reevaluate_locked(c, trigger=trigger)

    def _reevaluate_locked(self, c: Consultation, trigger: str) -> dict | None:
        """在持有事务的前提下重估单个会诊；返回状态变化摘要或 None。"""
        now = self._clock.now()
        if c.status.is_terminal:
            return None
        before = c.status.value

        # 规则一：患者同意失效 —— 无条件强制取消，凌驾于人工锁定与关键阶段之上
        consent = self._store.get_authorization(c.consent_id)
        if not authorization_covers(consent, now, c.slot_start, c.slot_end):
            self._apply_cancel_locked(c, now, reason="CONSENT_INVALID")
            self._emit_locked(c.id, EVT_CONSENT_INVALID, {
                "consent_id": c.consent_id,
                "consent_status": consent.status.value if consent else "MISSING",
                "phase": c.phase.value,
            })
            self._store.add_evaluation_log(c.id, trigger, "CANCELLED", ["CONSENT_INVALID"], now)
            return {"consultation_id": c.id, "from": before, "to": c.status.value,
                    "reason": "CONSENT_INVALID"}

        # 规则二：人工锁定 —— 冻结自动变迁，仅提示人工复核
        if self._store.active_lock(c.id, now) is not None:
            self._store.add_evaluation_log(c.id, trigger, "HELD_BY_LOCK", [], now)
            self._emit_locked(c.id, EVT_MANUAL_REVIEW, {"trigger": trigger, "operator_lock": True})
            return {"consultation_id": c.id, "from": before, "to": before,
                    "reason": "HELD_BY_MANUAL_LOCK"}

        # 规则三：机构授权失效 —— 按常规条件变化处理（暂停，待授权恢复）
        lapsed = [
            iid for iid in c.institution_ids
            if not authorization_covers(
                self._store.find_institution_authorization(iid), now, c.slot_start, c.slot_end
            )
        ]
        if lapsed:
            if c.status == ConsultationStatus.PAUSED and c.pause_reason == "INSTITUTION_AUTH_LAPSED":
                return None
            if c.phase == PhaseKind.CRITICAL:
                self._emit_locked(c.id, EVT_RISK_ESCALATION, {
                    "reason": "INSTITUTION_AUTH_LAPSED_IN_CRITICAL", "institutions": lapsed,
                })
                self._store.add_evaluation_log(
                    c.id, trigger, "ESCALATED", ["INSTITUTION_AUTH_LAPSED"], now
                )
                return {"consultation_id": c.id, "from": before, "to": before,
                        "reason": "INSTITUTION_AUTH_LAPSED_IN_CRITICAL"}
            self._apply_pause_locked(c, now, reason="INSTITUTION_AUTH_LAPSED")
            self._store.add_evaluation_log(
                c.id, trigger, "PAUSED", ["INSTITUTION_AUTH_LAPSED"], now
            )
            return {"consultation_id": c.id, "from": before, "to": c.status.value,
                    "reason": "INSTITUTION_AUTH_LAPSED"}

        # 规则四：链路质量 —— 切换备选路径 / 有限降级 / 暂停 / 恢复
        return self._reevaluate_links_locked(c, trigger, before, now)

    def _reevaluate_links_locked(
        self, c: Consultation, trigger: str, before: str, now: datetime
    ) -> dict | None:
        floor_full = SERVICE_LEVEL_FLOORS[c.min_service_level]
        active_probe = self._store.latest_probe(c.active_link_id)
        primary_probe = self._store.latest_probe(c.primary_link_id)
        backup_probe = self._store.latest_probe(c.backup_link_id) if c.backup_link_id else None

        active_ok = quality_meets(active_probe, floor_full, now)
        primary_ok = quality_meets(primary_probe, floor_full, now)
        backup_ok = quality_meets(backup_probe, floor_full, now) if c.backup_link_id else False

        if c.status == ConsultationStatus.PAUSED:
            # 仅在链路恢复完整服务等级且授权有效时恢复
            if primary_ok or backup_ok:
                target = c.primary_link_id if primary_ok else c.backup_link_id
                c.active_link_id = target
                c.status = c.pre_pause_status or ConsultationStatus.CONFIRMED
                c.pre_pause_status = None
                c.pause_reason = None
                c.current_service_level = c.min_service_level
                self._save_locked(c, now)
                self._emit_locked(c.id, EVT_RESUMED, {"active_link_id": target})
                self._store.add_evaluation_log(c.id, trigger, "RESUMED", [], now)
                return {"consultation_id": c.id, "from": before, "to": c.status.value,
                        "reason": "LINK_RECOVERED"}
            return None

        if active_ok:
            if c.status == ConsultationStatus.DEGRADED:
                # 质量恢复，回到降级前的基础状态与完整服务等级
                c.status = c.pre_pause_status or ConsultationStatus.CONFIRMED
                c.pre_pause_status = None
                c.current_service_level = c.min_service_level
                self._save_locked(c, now)
                self._emit_locked(c.id, EVT_RESUMED, {"active_link_id": c.active_link_id})
                self._store.add_evaluation_log(c.id, trigger, "RESTORED", [], now)
                return {"consultation_id": c.id, "from": before, "to": c.status.value,
                        "reason": "LINK_RECOVERED"}
            return None

        # 当前链路不满足完整等级：优先无缝切换到满足完整等级的备选路径
        alternate = c.backup_link_id if c.active_link_id == c.primary_link_id else c.primary_link_id
        alternate_probe = backup_probe if alternate == c.backup_link_id else primary_probe
        alternate_ok = backup_ok if alternate == c.backup_link_id else primary_ok

        if alternate is not None and alternate_ok:
            c.active_link_id = alternate
            if c.status == ConsultationStatus.DEGRADED:
                c.status = c.pre_pause_status or ConsultationStatus.CONFIRMED
                c.pre_pause_status = None
                c.current_service_level = c.min_service_level
            self._save_locked(c, now)
            self._emit_locked(c.id, EVT_PATH_SWITCHED, {
                "active_link_id": alternate, "trigger": trigger,
            })
            self._store.add_evaluation_log(c.id, trigger, "PATH_SWITCHED", [], now)
            return {"consultation_id": c.id, "from": before, "to": c.status.value,
                    "reason": "LINK_SWITCHED", "active_link_id": alternate}

        # 规则五：关键操作阶段 —— 无法无缝切换时保持现状并升级告警，禁止降级/暂停
        if c.phase == PhaseKind.CRITICAL:
            self._emit_locked(c.id, EVT_RISK_ESCALATION, {
                "reason": "LINK_BELOW_LEVEL_IN_CRITICAL",
                "active_link_id": c.active_link_id,
            })
            self._store.add_evaluation_log(
                c.id, trigger, "ESCALATED", ["LINK_BELOW_LEVEL_IN_CRITICAL"], now
            )
            return {"consultation_id": c.id, "from": before, "to": before,
                    "reason": "LINK_BELOW_LEVEL_IN_CRITICAL"}

        # 已处于降级且当前链路仍满足降级底线：维持现状，避免重复事件
        dfloor = degraded_floor(c.min_service_level)
        if (
            c.status == ConsultationStatus.DEGRADED
            and dfloor is not None
            and quality_meets(active_probe, dfloor, now)
        ):
            return None

        # 有限降级：当前或备选链路满足降一档底线
        best_link, best_probe = c.active_link_id, active_probe
        if (
            dfloor is not None
            and alternate is not None
            and alternate_probe is not None
            and quality_meets(alternate_probe, dfloor, now)
            and not quality_meets(best_probe, dfloor, now)
        ):
            best_link, best_probe = alternate, alternate_probe
        if dfloor is not None and quality_meets(best_probe, dfloor, now):
            self._apply_degrade_locked(c, now, link=best_link)
            self._store.add_evaluation_log(c.id, trigger, "DEGRADED", [], now)
            return {"consultation_id": c.id, "from": before, "to": c.status.value,
                    "reason": "LINK_DEGRADED"}

        # 无路可走：暂停等待恢复
        self._apply_pause_locked(c, now, reason="LINK_BELOW_FLOOR")
        self._store.add_evaluation_log(c.id, trigger, "PAUSED", ["LINK_BELOW_FLOOR"], now)
        return {"consultation_id": c.id, "from": before, "to": c.status.value,
                "reason": "LINK_BELOW_FLOOR"}

    # ------------------------------------------------------------------
    # 状态变迁原语（均假设处于事务内）
    # ------------------------------------------------------------------
    def _apply_cancel_locked(self, c: Consultation, now: datetime, reason: str) -> None:
        c.status = ConsultationStatus.CANCELLED
        c.cancel_reason = reason
        self._save_locked(c, now)
        self._store.release_holds(c.id)
        self._emit_locked(c.id, EVT_CANCELLED, {"reason": reason})

    def _apply_pause_locked(self, c: Consultation, now: datetime, reason: str) -> None:
        if c.status in (ConsultationStatus.CONFIRMED, ConsultationStatus.ACTIVE):
            c.pre_pause_status = c.status
        elif c.status == ConsultationStatus.DEGRADED and c.pre_pause_status is None:
            c.pre_pause_status = ConsultationStatus.CONFIRMED
        c.status = ConsultationStatus.PAUSED
        c.pause_reason = reason
        self._save_locked(c, now)
        self._emit_locked(c.id, EVT_PAUSED, {"reason": reason})

    def _apply_degrade_locked(
        self, c: Consultation, now: datetime, link: str | None = None
    ) -> None:
        target_level = degraded_level(c.min_service_level)
        if target_level is None:
            raise InvalidTransition("BRONZE 等级不允许再降级")
        dfloor = degraded_floor(c.min_service_level)
        probe = self._store.latest_probe(link or c.active_link_id)
        if not quality_meets(probe, dfloor, now):
            raise InvalidTransition("当前链路不满足降级底线")
        if link:
            c.active_link_id = link
        if c.status in (ConsultationStatus.CONFIRMED, ConsultationStatus.ACTIVE):
            c.pre_pause_status = c.status
        c.status = ConsultationStatus.DEGRADED
        c.current_service_level = target_level
        self._save_locked(c, now)
        self._emit_locked(c.id, EVT_DEGRADED, {
            "current_service_level": target_level.value,
            "active_link_id": c.active_link_id,
        })

    def _ensure_consent_valid(self, c: Consultation, now: datetime) -> None:
        consent = self._store.get_authorization(c.consent_id)
        if not authorization_covers(consent, now, c.slot_start, c.slot_end):
            raise InvalidTransition("患者同意已失效，会诊不可继续")

    def _emit_locked(self, consultation_id: str, event_type: str, payload: dict) -> None:
        now = self._clock.now()
        self._store.insert_outbox_event(
            OutboxEvent(
                id=self._ids.new_id("evt"),
                consultation_id=consultation_id,
                type=event_type,
                payload=payload,
                status=OutboxStatus.PENDING,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_consultation(self, consultation_id: str) -> Consultation:
        return self._store.get_consultation(consultation_id)
