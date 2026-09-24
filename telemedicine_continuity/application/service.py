"""应用编排服务。

一个业务方法对应一个（或一组）串行化事务；业务状态、时段占用、
审计日志与通知发件箱在同一事务内提交。
"""

from __future__ import annotations

import sqlite3

from ..domain.admission import (
    DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
    AdmissionRequest,
    effective_grade,
    evaluate_admission,
)
from ..domain.enums import (
    PENDING_CANCEL,
    ConsentStatus,
    ConsultationStatus,
    CriticalPhase,
    Grade,
    Health,
    Role,
    SlotStatus,
)
from ..domain.errors import AuthorizationError, DomainError, NotFound, SlotConflict, ValidationError
from ..domain.models import (
    Clinician,
    Consent,
    Consultation,
    Link,
    Organization,
    Patient,
    Slot,
)
from ..domain.projection import project_consultation
from ..infra.clock import Clock, business_code, new_id, parse_iso
from ..infra.db import Database, Repository
from .policy import REASON_LINK_DOWN, decide_reaction, select_switch

PENDING_PAUSE = "pause_on_stage_exit"


def _require(role: Role, allowed: frozenset[Role], action: str) -> None:
    if role not in allowed:
        raise AuthorizationError(
            "forbidden", f"职责 {role.value} 无权执行 {action}"
        )


class OrchestrationService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        snapshot_max_age_seconds: int = DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
    ) -> None:
        self.db = db
        self.repo = Repository()
        self.clock = clock
        self.snapshot_max_age = snapshot_max_age_seconds

    # ============================================================ 基础数据

    def register_org(self, role: Role, org_id: str, name: str) -> Organization:
        _require(role, frozenset({Role.COORDINATOR}), "注册机构")
        org = Organization(org_id=org_id, name=name)
        with self.db.transaction() as conn:
            self.repo.upsert_org(conn, org)
        return org

    def register_clinician(self, role: Role, clinician_id: str, name: str, org_id: str) -> Clinician:
        _require(role, frozenset({Role.COORDINATOR}), "注册医生")
        with self.db.transaction() as conn:
            if self.repo.get_org(conn, org_id) is None:
                raise ValidationError("org_unknown", f"机构 {org_id} 不存在")
            clinician = Clinician(clinician_id, name, org_id)
            self.repo.upsert_clinician(conn, clinician)
        return clinician

    def register_patient(
        self, role: Role, patient_id: str, name: str, national_id: str, contact_phone: str
    ) -> Patient:
        _require(role, frozenset({Role.COORDINATOR}), "注册患者")
        patient = Patient(patient_id, name, national_id, contact_phone)
        with self.db.transaction() as conn:
            self.repo.upsert_patient(conn, patient)
        return patient

    def register_link(
        self,
        role: Role,
        *,
        link_id: str,
        name: str,
        fault_domain: str,
        grade: int,
        org_ids: list[str] | tuple[str, ...],
    ) -> Link:
        _require(role, frozenset({Role.COORDINATOR}), "注册链路")
        grade = _grade_to_int(grade)
        link = Link(
            link_id=link_id,
            name=name,
            fault_domain=fault_domain,
            grade=grade,
            health=Health.UNKNOWN,
            latency_ms=None,
            loss_rate=None,
            org_ids=tuple(org_ids),
            snapshot_at=None,
        )
        with self.db.transaction() as conn:
            self.repo.register_link(conn, link, self.clock.now_iso())
        return link

    def report_snapshot(
        self,
        role: Role,
        link_id: str,
        *,
        health: str,
        latency_ms: int | None = None,
        loss_rate: float | None = None,
        snapshot_at: str | None = None,
    ) -> dict:
        """链路工程师上报探测快照；上报后联动评估受影响会诊。"""
        _require(role, frozenset({Role.LINK_ENGINEER}), "上报链路探测快照")
        health_enum = Health(health)
        at = snapshot_at or self.clock.now_iso()
        with self.db.transaction() as conn:
            if self.repo.get_link(conn, link_id) is None:
                raise NotFound("link_unknown", f"链路 {link_id} 不存在")
            self.repo.upsert_snapshot(conn, link_id, health_enum, latency_ms, loss_rate, at)
        # 快照变化后的联动反应在独立事务中逐个落地
        effects = self.evaluate_changes(link_id=link_id)
        return {"link_id": link_id, "health": health_enum.value, "effects": effects}

    def grant_consent(
        self, role: Role, patient_id: str, *, valid_from: str | None, valid_until: str
    ) -> Consent:
        _require(role, frozenset({Role.COORDINATOR}), "登记患者授权")
        frm = valid_from or self.clock.now_iso()
        if parse_iso(valid_until) <= parse_iso(frm):
            raise ValidationError("consent_range_invalid", "授权结束时间必须晚于开始时间")
        consent = Consent(patient_id, ConsentStatus.GRANTED.value, frm, valid_until)
        with self.db.transaction() as conn:
            if self.repo.get_patient(conn, patient_id) is None:
                raise NotFound("patient_unknown", f"患者 {patient_id} 不存在")
            self.repo.set_consent(conn, consent, self.clock.now_iso())
            self.repo.add_audit(
                conn,
                consultation_id=None,
                event_type="consent.granted",
                actor_role=role.value,
                actor_id=None,
                details={"patient_id": patient_id, "valid_until": valid_until},
                created_at=self.clock.now_iso(),
            )
        return consent

    def revoke_consent(self, role: Role, patient_id: str) -> dict:
        """撤销授权，并立即联动该患者全部活跃会诊（取消或延后取消）。"""
        _require(role, frozenset({Role.COORDINATOR}), "撤销患者授权")
        changed = False
        with self.db.transaction() as conn:
            consent = self.repo.get_consent(conn, patient_id)
            if consent is None:
                raise NotFound("consent_unknown", f"患者 {patient_id} 无授权记录")
            if consent.state == ConsentStatus.GRANTED.value:
                consent = Consent(
                    patient_id,
                    ConsentStatus.REVOKED.value,
                    consent.valid_from,
                    consent.valid_until,
                )
                self.repo.set_consent(conn, consent, self.clock.now_iso())
                self.repo.add_outbox(
                    conn,
                    event_id=new_id(),
                    event_key=f"consent.revoked:{patient_id}:{self.clock.now_iso()}",
                    event_type="consent.revoked",
                    aggregate_id=patient_id,
                    payload={"patient_id": patient_id, "revoked_at": self.clock.now_iso()},
                    created_at=self.clock.now_iso(),
                )
                self.repo.add_audit(
                    conn,
                    consultation_id=None,
                    event_type="consent.revoked",
                    actor_role=role.value,
                    actor_id=None,
                    details={"patient_id": patient_id},
                    created_at=self.clock.now_iso(),
                )
                changed = True
        effects = self.evaluate_changes(patient_id=patient_id)
        return {"patient_id": patient_id, "state": ConsentStatus.REVOKED.value,
                "changed": changed, "effects": effects}

    def create_slot(
        self, role: Role, *, slot_id: str, clinician_id: str, start_time: str, end_time: str
    ) -> Slot:
        _require(role, frozenset({Role.COORDINATOR}), "创建诊疗时段")
        if parse_iso(end_time) <= parse_iso(start_time):
            raise ValidationError("slot_range_invalid", "时段结束时间必须晚于开始时间")
        slot = Slot(slot_id, clinician_id, start_time, end_time, SlotStatus.FREE.value, None)
        with self.db.transaction() as conn:
            if self.repo.get_clinician(conn, clinician_id) is None:
                raise NotFound("clinician_unknown", f"医生 {clinician_id} 不存在")
            self.repo.create_slot(conn, slot)
        return slot

    # ============================================================ 排班准入

    def book_consultation(
        self,
        role: Role,
        *,
        patient_id: str,
        clinician_id: str,
        slot_id: str,
        org_ids: list[str],
        min_grade: str | int,
        required_link_count: int = 1,
        candidate_link_ids: list[str] | None = None,
        idem_key: str | None = None,
    ) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR}), "安排会诊")
        grade = _grade_to_int(min_grade)
        if not org_ids:
            raise ValidationError("orgs_required", "至少需要一个参与机构")
        if required_link_count < 1:
            raise ValidationError("invalid_required_link_count", "所需链路数量必须大于等于 1")

        with self.db.transaction() as conn:
            # 幂等重放：相同业务键直接返回既有聚合
            if idem_key:
                existing = conn.execute(
                    "SELECT consultation_id FROM consultations WHERE idem_key=?",
                    (idem_key,),
                ).fetchone()
                if existing:
                    return self.repo.get_consultation(conn, existing["consultation_id"])

            patient = self.repo.get_patient(conn, patient_id)
            if patient is None:
                raise NotFound("patient_unknown", f"患者 {patient_id} 不存在")
            clinician = self.repo.get_clinician(conn, clinician_id)
            if clinician is None:
                raise NotFound("clinician_unknown", f"医生 {clinician_id} 不存在")
            slot = self.repo.get_slot(conn, slot_id)
            if slot is None:
                raise NotFound("slot_unknown", f"时段 {slot_id} 不存在")
            if slot.clinician_id != clinician_id:
                raise ValidationError("slot_clinician_mismatch", "时段不属于该医生")
            for org_id in org_ids:
                if self.repo.get_org(conn, org_id) is None:
                    raise NotFound("org_unknown", f"机构 {org_id} 不存在")

            # 同一医生重叠时段冲突（不同 slot_id 的并发争抢也被挡住）
            overlap = self.repo.find_clinician_overlap(
                conn, clinician_id, slot.start_time, slot.end_time
            )
            if overlap is not None:
                raise SlotConflict(
                    "slot_overlap",
                    f"医生时段与已占用时段 {overlap.slot_id} 冲突",
                    details={"conflict_slot_id": overlap.slot_id},
                )

            if not self.repo.occupy_slot(conn, slot_id, "__pending__"):
                raise SlotConflict("slot_occupied", f"时段 {slot_id} 已被占用")

            links = {link.link_id: link for link in self.repo.list_links(conn)}
            consent = self.repo.get_consent(conn, patient_id)
            req = AdmissionRequest(
                patient_id=patient_id,
                clinician_id=clinician_id,
                slot_start=slot.start_time,
                slot_end=slot.end_time,
                org_ids=tuple(org_ids),
                min_grade=grade,
                required_link_count=required_link_count,
                candidate_link_ids=tuple(candidate_link_ids or ()),
            )
            decision = evaluate_admission(
                req,
                links=links,
                consent=consent,
                clock=self.clock,
                snapshot_max_age_seconds=self.snapshot_max_age,
            )
            if not decision.allowed:
                # 事务回滚即释放占用；显式释放更直观
                self.repo.release_slot(conn, slot_id)
                raise DomainError(
                    "admission_rejected",
                    "准入判断未通过：" + "；".join(i.message for i in decision.issues),
                    details={"issues": [{"code": i.code, "message": i.message}
                                        for i in decision.issues]},
                )

            now = self.clock.now_iso()
            cid = new_id()
            chosen_links = tuple(
                lid for lid in [decision.active_link_id, decision.backup_link_id] if lid
            )
            consultation = Consultation(
                consultation_id=cid,
                code=business_code("HZ"),
                patient_id=patient_id,
                clinician_id=clinician_id,
                slot_id=slot_id,
                link_ids=chosen_links,
                org_ids=tuple(org_ids),
                min_grade=grade,
                required_link_count=required_link_count,
                status=ConsultationStatus.CONFIRMED,
                active_link_id=decision.active_link_id,
                backup_link_id=decision.backup_link_id,
                effective_grade=decision.effective_grade,
                consent_valid_until=consent.valid_until if consent else "",
                consent_state=ConsentStatus.GRANTED.value,
                critical_phase=CriticalPhase.NORMAL,
                pause_reason=None,
                locked_by=None,
                pending_effect=None,
                idem_key=idem_key,
                created_at=now,
                updated_at=now,
                version=0,
            )
            self.repo.insert_consultation(conn, consultation)
            conn.execute(
                "UPDATE slots SET consultation_id=? WHERE slot_id=?", (cid, slot_id)
            )
            self.repo.add_audit(
                conn,
                consultation_id=cid,
                event_type="consultation.confirmed",
                actor_role=role.value,
                actor_id=None,
                details={"code": consultation.code,
                         "active_link_id": consultation.active_link_id,
                         "backup_link_id": consultation.backup_link_id},
                created_at=now,
            )
            self.repo.add_outbox(
                conn,
                event_id=new_id(),
                event_key=f"consultation.confirmed:{idem_key or cid}",
                event_type="consultation.confirmed",
                aggregate_id=cid,
                payload={"consultation_id": cid, "code": consultation.code,
                         "clinician_id": clinician_id, "slot_id": slot_id},
                created_at=now,
            )
            return consultation

    # ============================================================ 条件联动

    def evaluate_changes(
        self, *, patient_id: str | None = None, link_id: str | None = None
    ) -> list[dict]:
        """对活跃会诊依据最新快照/授权推导并落地反应。人工锁定的会诊跳过。"""
        effects: list[dict] = []
        with self.db.read_only() as conn:
            consultations = self.repo.list_consultations(conn)
        active = [c for c in consultations if c.is_active()]
        if patient_id:
            active = [c for c in active if c.patient_id == patient_id]
        if link_id:
            active = [c for c in active if link_id in c.link_ids]
        for c in active:
            try:
                effect = self._evaluate_one(c.consultation_id)
            except DomainError as exc:
                effect = {"consultation_id": c.consultation_id, "action": "blocked",
                          "reason": exc.message}
            if effect:
                effects.append(effect)
        return effects

    def _evaluate_one(self, cid: str) -> dict | None:
        with self.db.transaction() as conn:
            c = self.repo.get_consultation(conn, cid)
            if c is None or not c.is_active():
                return None
            if c.locked_by is not None:
                return {"consultation_id": cid, "action": "skipped_locked",
                        "reason": f"会诊已被 {c.locked_by} 人工锁定，自动联动不介入"}
            slot = self.repo.get_slot(conn, c.slot_id)
            links = {lid: self.repo.get_link(conn, lid) for lid in c.link_ids}
            links = {k: v for k, v in links.items() if v is not None}
            # 联动时也允许在该会诊机构覆盖的全部链路中重新选择备选
            for link in self.repo.list_links(conn):
                if all(org in link.org_ids for org in c.org_ids):
                    links.setdefault(link.link_id, link)
            consent = self.repo.get_consent(conn, c.patient_id)

            # 同步授权快照（撤销/续期可能发生在两次联动之间）
            consent_dirty = (
                c.consent_state != (consent.state if consent else "missing")
                or c.consent_valid_until != (consent.valid_until if consent else "")
            )
            if consent:
                c.consent_state = consent.state
                c.consent_valid_until = consent.valid_until

            reaction = decide_reaction(
                c,
                slot_end=slot.end_time if slot else c.consent_valid_until,
                links=links,
                consent=consent,
                clock=self.clock,
                snapshot_max_age_seconds=self.snapshot_max_age,
            )
            if reaction.action == "none":
                dirty = consent_dirty
                if reaction.backup_link_id != c.backup_link_id:
                    c.backup_link_id = reaction.backup_link_id
                    if reaction.backup_link_id and reaction.backup_link_id not in c.link_ids:
                        c.link_ids = tuple([*c.link_ids, reaction.backup_link_id])
                    dirty = True
                if dirty:
                    c.updated_at = self.clock.now_iso()
                    self.repo.persist_consultation(conn, c)
                return None

            # 已处于同一坏状态：重复探测不再产生重复事件
            if (
                reaction.action == "switch"
                and c.status is ConsultationStatus.SWITCHED
                and reaction.target_link_id == c.active_link_id
            ):
                return None
            if (
                reaction.action == "pause"
                and c.status is ConsultationStatus.PAUSED
            ):
                return None
            if (
                reaction.action == "degrade"
                and c.status is ConsultationStatus.DEGRADED
                and reaction.effective_grade == c.effective_grade
                and reaction.target_link_id == c.active_link_id
            ):
                return None

            if reaction.action == "defer":
                pending = PENDING_CANCEL if "consent" in reaction.reason else PENDING_PAUSE
                if c.pending_effect != pending:
                    c.pending_effect = pending
                    c.updated_at = self.clock.now_iso()
                    self.repo.persist_consultation(conn, c)
                    self._audit(conn, c, "consultation.effect_deferred",
                                {"pending_effect": pending, "reason": reaction.reason})
                return {"consultation_id": cid, "action": "deferred",
                        "pending_effect": pending, "reason": reaction.reason}

            return self._apply_reaction(conn, c, reaction, actor="system")

    # ============================================================ 人工操作

    def _load_active(self, conn: sqlite3.Connection, cid: str) -> Consultation:
        c = self.repo.get_consultation(conn, cid)
        if c is None:
            raise NotFound("consultation_unknown", f"会诊 {cid} 不存在")
        if not c.is_active():
            raise DomainError("consultation_terminal",
                              f"会诊已处于终态 {c.status.value}")
        return c

    def _check_lock(self, c: Consultation, actor_id: str | None) -> None:
        if c.locked_by is not None and c.locked_by != actor_id:
            raise AuthorizationError(
                "locked_by_other",
                f"会诊已被 {c.locked_by} 人工锁定，仅锁定者可操作",
            )

    def _replayed(
        self, conn: sqlite3.Connection, c: Consultation,
        event_type: str, idem_key: str | None,
    ) -> Consultation | None:
        """相同幂等键的操作已落库时直接返回当前聚合（客户端重试安全）。"""
        if not idem_key:
            return None
        row = conn.execute(
            "SELECT 1 FROM outbox WHERE event_key=?",
            (f"{event_type}:{c.consultation_id}:{idem_key}",),
        ).fetchone()
        return c if row else None

    def switch_path(
        self, role: Role, cid: str, *, actor_id: str | None = None,
        target_link_id: str | None = None, idem_key: str | None = None,
    ) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR}), "切换链路")
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            self._check_lock(c, actor_id)
            replayed = self._replayed(conn, c, "consultation.switched", idem_key)
            if replayed:
                return replayed
            self._guard_critical(c, "切换链路")
            active = self.repo.get_link(conn, c.active_link_id)
            candidates = {lid: self.repo.get_link(conn, lid) for lid in c.link_ids}
            for link in self.repo.list_links(conn):
                if all(org in link.org_ids for org in c.org_ids):
                    candidates.setdefault(link.link_id, link)
            candidates = {k: v for k, v in candidates.items() if v is not None}

            if target_link_id:
                target = candidates.get(target_link_id)
                if target is None:
                    raise NotFound("link_unknown", f"链路 {target_link_id} 不可用于该会诊")
                if active and target.fault_domain == active.fault_domain:
                    raise DomainError(
                        "shared_fault_domain",
                        f"目标链路与当前主链路共享故障域 {target.fault_domain}，切换无意义",
                    )
                grade_ok = effective_grade(target) >= c.min_grade
                fresh = target.snapshot_at is not None and (
                    self.clock.now() - parse_iso(target.snapshot_at)
                ).total_seconds() <= self.snapshot_max_age
                if not (target.health.value in ("up", "degraded") and grade_ok and fresh):
                    raise DomainError("target_not_eligible",
                                      "目标链路健康度、等级或快照时效不满足准入")
                backup_id = c.active_link_id if active and active.fault_domain != target.fault_domain \
                    and effective_grade(active) >= c.min_grade else c.backup_link_id
                new_active, new_backup, grade = target_link_id, backup_id, effective_grade(target)
            else:
                choice = select_switch(
                    c, candidates, clock=self.clock,
                    snapshot_max_age_seconds=self.snapshot_max_age,
                )
                if choice is None:
                    raise DomainError("no_alternative_path", "当前无故障域独立的可用备选路径")
                target_link, backup_link = choice
                new_active = target_link.link_id
                new_backup = backup_link.link_id if backup_link else None
                grade = effective_grade(target_link)

            old = c.active_link_id
            c.active_link_id = new_active
            c.backup_link_id = new_backup if new_backup != new_active else None
            c.effective_grade = grade
            c.status = ConsultationStatus.SWITCHED
            c.updated_at = self.clock.now_iso()
            if new_active not in c.link_ids:
                c.link_ids = tuple([*c.link_ids, new_active])
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.switched", idem_key,
                        {"from_link_id": old, "to_link_id": new_active})
            self._audit(conn, c, "consultation.switched",
                        {"from": old, "to": new_active, "by": actor_id})
            return c

    def limited_degrade(
        self, role: Role, cid: str, *, actor_id: str | None = None,
        grade: str | int, reason: str = "人工有限降级", idem_key: str | None = None,
    ) -> Consultation:
        """有限降级：关键操作阶段唯一允许的链路调整。"""
        _require(role, frozenset({Role.COORDINATOR, Role.CLINICIAN}), "有限降级")
        target_grade = _grade_to_int(grade)
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            self._check_lock(c, actor_id)
            replayed = self._replayed(conn, c, "consultation.degraded", idem_key)
            if replayed:
                return replayed
            c.status = ConsultationStatus.DEGRADED
            c.effective_grade = min(c.effective_grade, target_grade)
            c.pause_reason = None
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.degraded", idem_key,
                        {"effective_grade": c.effective_grade, "reason": reason})
            self._audit(conn, c, "consultation.degraded", {"reason": reason, "by": actor_id})
            return c

    def pause(
        self, role: Role, cid: str, *, actor_id: str | None = None,
        reason: str = "人工暂停", idem_key: str | None = None,
    ) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR}), "暂停会诊")
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            self._check_lock(c, actor_id)
            replayed = self._replayed(conn, c, "consultation.paused", idem_key)
            if replayed:
                return replayed
            self._guard_critical(c, "暂停会诊")
            c.status = ConsultationStatus.PAUSED
            c.pause_reason = reason
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.paused", idem_key, {"reason": reason})
            self._audit(conn, c, "consultation.paused", {"reason": reason, "by": actor_id})
            return c

    def resume(
        self, role: Role, cid: str, *, actor_id: str | None = None,
        idem_key: str | None = None,
    ) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR}), "恢复会诊")
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            self._check_lock(c, actor_id)
            replayed = self._replayed(conn, c, "consultation.resumed", idem_key)
            if replayed:
                return replayed
            self._guard_critical(c, "恢复会诊")
            links = {lid: self.repo.get_link(conn, lid) for lid in c.link_ids}
            active = links.get(c.active_link_id)
            if active is None or active.health in (Health.DOWN, Health.UNKNOWN):
                raise DomainError("link_unavailable", "主链路仍不可用，无法恢复")
            if effective_grade(active) < c.min_grade:
                raise DomainError("grade_insufficient", "主链路等级仍低于最低服务等级")
            c.status = ConsultationStatus.CONFIRMED
            c.pause_reason = None
            c.effective_grade = effective_grade(active)
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.resumed", idem_key, {})
            self._audit(conn, c, "consultation.resumed", {"by": actor_id})
            return c

    def cancel(
        self, role: Role, cid: str, *, actor_id: str | None = None,
        reason: str = "人工取消", idem_key: str | None = None,
    ) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR}), "取消会诊")
        with self.db.transaction() as conn:
            # 取消会进入终态，重试需在终态校验之前完成幂等重放
            if idem_key:
                row = conn.execute(
                    "SELECT 1 FROM outbox WHERE event_key=?",
                    (f"consultation.cancelled:{cid}:{idem_key}",),
                ).fetchone()
                if row:
                    return self.repo.get_consultation(conn, cid)
            c = self._load_active(conn, cid)
            self._check_lock(c, actor_id)
            if c.critical_phase is CriticalPhase.CRITICAL:
                # 关键操作阶段不可立即取消：登记待阶段结束执行
                c.pending_effect = PENDING_CANCEL
                c.updated_at = self.clock.now_iso()
                self.repo.persist_consultation(conn, c)
                self._audit(conn, c, "consultation.cancel_deferred",
                            {"reason": reason, "by": actor_id})
                return c
            self._do_cancel(conn, c, reason, idem_key, actor=actor_id)
            return c

    def complete(self, role: Role, cid: str, *, actor_id: str | None = None) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR, Role.CLINICIAN}), "完成会诊")
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            self._check_lock(c, actor_id)
            c.status = ConsultationStatus.COMPLETED
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self.repo.release_slot(conn, c.slot_id)
            self._event(conn, c, "consultation.completed", None, {})
            self._audit(conn, c, "consultation.completed", {"by": actor_id})
            return c

    # ---- 关键操作阶段 ----
    def set_critical_phase(
        self, role: Role, cid: str, critical: bool, *, actor_id: str | None = None
    ) -> Consultation:
        _require(role, frozenset({Role.CLINICIAN, Role.COORDINATOR}), "标记关键操作阶段")
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            self._check_lock(c, actor_id)
            previous = c.critical_phase
            c.critical_phase = CriticalPhase.CRITICAL if critical else CriticalPhase.NORMAL
            c.updated_at = self.clock.now_iso()

            pending_after_exit: str | None = None
            if previous is CriticalPhase.CRITICAL and not critical:
                # 阶段结束：落地延迟效果。落地前复查条件，若期间条件已解除则不再动作
                pending_after_exit = c.pending_effect
                c.pending_effect = None
                if pending_after_exit == PENDING_CANCEL:
                    latest_consent = self.repo.get_consent(conn, c.patient_id)
                    slot = self.repo.get_slot(conn, c.slot_id)
                    slot_end = slot.end_time if slot else c.consent_valid_until
                    consent_ok = (
                        latest_consent is not None
                        and latest_consent.state == ConsentStatus.GRANTED.value
                        and parse_iso(latest_consent.valid_until) >= parse_iso(slot_end)
                        and parse_iso(latest_consent.valid_until) >= self.clock.now()
                    )
                    if consent_ok:
                        self.repo.persist_consultation(conn, c)
                        self._audit(conn, c, "consultation.deferred_effect_cleared",
                                    {"pending": pending_after_exit})
                    else:
                        self._do_cancel(conn, c, "关键操作阶段结束，执行延后取消",
                                        None, actor="system")
                    self._audit(conn, c, "consultation.phase_changed",
                                {"from": previous.value, "to": c.critical_phase.value,
                                 "by": actor_id, "pending": pending_after_exit})
                    return c
                if pending_after_exit == PENDING_PAUSE:
                    # 阶段结束：用最新快照重新评估（可能已恢复，或出现备选可切换）
                    links = {lid: self.repo.get_link(conn, lid) for lid in c.link_ids}
                    links = {k: v for k, v in links.items() if v is not None}
                    for link in self.repo.list_links(conn):
                        if all(org in link.org_ids for org in c.org_ids):
                            links.setdefault(link.link_id, link)
                    latest_consent = self.repo.get_consent(conn, c.patient_id)
                    slot = self.repo.get_slot(conn, c.slot_id)
                    follow_up = decide_reaction(
                        c,
                        slot_end=slot.end_time if slot else c.consent_valid_until,
                        links=links, consent=latest_consent, clock=self.clock,
                        snapshot_max_age_seconds=self.snapshot_max_age,
                    )
                    if follow_up.action in ("switch", "pause", "degrade", "cancel"):
                        self._apply_reaction(conn, c, follow_up, actor="system")
                    else:
                        self.repo.persist_consultation(conn, c)
                        self._audit(conn, c, "consultation.deferred_effect_cleared",
                                    {"pending": pending_after_exit})
                    self._audit(conn, c, "consultation.phase_changed",
                                {"from": previous.value, "to": c.critical_phase.value,
                                 "by": actor_id, "pending": pending_after_exit})
                    return c

            self.repo.persist_consultation(conn, c)
            self._audit(conn, c, "consultation.phase_changed",
                        {"from": previous.value, "to": c.critical_phase.value,
                         "by": actor_id})
            return c

    # ---- 人工锁定 ----
    def lock(self, role: Role, cid: str, actor_id: str) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR}), "人工锁定会诊")
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            if c.locked_by is not None and c.locked_by != actor_id:
                raise AuthorizationError(
                    "locked_by_other", f"会诊已被 {c.locked_by} 锁定"
                )
            c.locked_by = actor_id
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._audit(conn, c, "consultation.locked", {"by": actor_id})
            return c

    def unlock(self, role: Role, cid: str, actor_id: str) -> Consultation:
        _require(role, frozenset({Role.COORDINATOR}), "解除人工锁定")
        with self.db.transaction() as conn:
            c = self._load_active(conn, cid)
            if c.locked_by is None:
                return c
            if c.locked_by != actor_id:
                raise AuthorizationError(
                    "locked_by_other", f"会诊已被 {c.locked_by} 锁定，仅锁定者可解除"
                )
            c.locked_by = None
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._audit(conn, c, "consultation.unlocked", {"by": actor_id})
            return c

    # ============================================================ 查询

    def get_consultation_view(self, role: Role, cid: str) -> dict:
        with self.db.read_only() as conn:
            c = self.repo.get_consultation(conn, cid)
            if c is None:
                raise NotFound("consultation_unknown", f"会诊 {cid} 不存在")
            patient = self.repo.get_patient(conn, c.patient_id)
            return project_consultation(c, role=role, patient=patient)

    def list_consultation_views(self, role: Role) -> list[dict]:
        with self.db.read_only() as conn:
            consultations = self.repo.list_consultations(conn)
            patient_ids = {c.patient_id for c in consultations}
            patients = {
                pid: self.repo.get_patient(conn, pid) for pid in patient_ids
            }
            return [project_consultation(c, role=role, patient=patients.get(c.patient_id))
                    for c in consultations]

    def list_slots(self, role: Role) -> list[dict]:
        _require(role, frozenset({Role.COORDINATOR, Role.AUDITOR, Role.CLINICIAN}), "查询时段")
        with self.db.read_only() as conn:
            return [
                {
                    "slot_id": s.slot_id,
                    "clinician_id": s.clinician_id,
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "status": s.status,
                    "consultation_id": s.consultation_id,
                }
                for s in self.repo.list_slots(conn)
            ]

    def list_links(self, role: Role) -> list[dict]:
        from ..domain.projection import project_link
        with self.db.read_only() as conn:
            return [project_link(link) for link in self.repo.list_links(conn)]

    # ============================================================ 内部辅助

    def _guard_critical(self, c: Consultation, action: str) -> None:
        if c.critical_phase is CriticalPhase.CRITICAL:
            raise DomainError(
                "critical_phase",
                f"关键操作阶段不允许{action}，请等待阶段结束或使用有限降级",
            )

    def _do_cancel(
        self, conn: sqlite3.Connection, c: Consultation, reason: str,
        idem_key: str | None, *, actor: str | None,
    ) -> None:
        c.status = ConsultationStatus.CANCELLED
        c.pending_effect = None
        c.updated_at = self.clock.now_iso()
        self.repo.persist_consultation(conn, c)
        self.repo.release_slot(conn, c.slot_id)
        self._event(conn, c, "consultation.cancelled", idem_key, {"reason": reason})
        self._audit(conn, c, "consultation.cancelled", {"reason": reason, "by": actor})

    def _apply_reaction(
        self, conn: sqlite3.Connection, c: Consultation, reaction, *, actor: str
    ) -> dict:
        result = {"consultation_id": c.consultation_id, "action": reaction.action,
                  "reason": reaction.reason}
        if reaction.action == "cancel":
            self._do_cancel(conn, c, reaction.reason, None, actor=actor)
        elif reaction.action == "switch":
            old = c.active_link_id
            c.active_link_id = reaction.target_link_id
            c.backup_link_id = reaction.backup_link_id
            c.effective_grade = reaction.effective_grade
            c.status = ConsultationStatus.SWITCHED
            c.updated_at = self.clock.now_iso()
            if reaction.target_link_id not in c.link_ids:
                c.link_ids = tuple([*c.link_ids, reaction.target_link_id])
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.switched", None,
                        {"from_link_id": old, "to_link_id": reaction.target_link_id,
                         "auto": True})
            self._audit(conn, c, "consultation.switched",
                        {"from": old, "to": reaction.target_link_id, "auto": True})
        elif reaction.action == "degrade":
            c.status = ConsultationStatus.DEGRADED
            c.active_link_id = reaction.target_link_id or c.active_link_id
            c.effective_grade = reaction.effective_grade
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.degraded", None,
                        {"effective_grade": reaction.effective_grade,
                         "reason": reaction.reason, "auto": True})
            self._audit(conn, c, "consultation.degraded", {"reason": reaction.reason})
        elif reaction.action == "pause":
            c.status = ConsultationStatus.PAUSED
            c.pause_reason = reaction.reason
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.paused", None, {"reason": reaction.reason})
            self._audit(conn, c, "consultation.paused", {"reason": reaction.reason})
        elif reaction.action == "resume":
            c.status = ConsultationStatus.CONFIRMED
            c.active_link_id = reaction.target_link_id or c.active_link_id
            c.backup_link_id = reaction.backup_link_id
            c.effective_grade = reaction.effective_grade
            c.pause_reason = None
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.resumed", None, {"reason": reaction.reason})
            self._audit(conn, c, "consultation.resumed", {"reason": reaction.reason})
        elif reaction.action == "restore":
            c.status = ConsultationStatus.CONFIRMED
            c.effective_grade = reaction.effective_grade
            c.backup_link_id = reaction.backup_link_id
            c.updated_at = self.clock.now_iso()
            self.repo.persist_consultation(conn, c)
            self._event(conn, c, "consultation.restored", None, {"reason": reaction.reason})
            self._audit(conn, c, "consultation.restored", {"reason": reaction.reason})
        else:
            raise DomainError("unsupported_reaction", f"不支持的反应动作 {reaction.action}")
        return result

    def _event(
        self, conn: sqlite3.Connection, c: Consultation, event_type: str,
        idem_key: str | None, payload: dict,
    ) -> None:
        key = f"{event_type}:{c.consultation_id}:{idem_key}" if idem_key \
            else f"{event_type}:{c.consultation_id}:{c.version + 1}"
        body = {"consultation_id": c.consultation_id, "code": c.code,
                "status": c.status.value, **payload}
        self.repo.add_outbox(
            conn,
            event_id=new_id(),
            event_key=key,
            event_type=event_type,
            aggregate_id=c.consultation_id,
            payload=body,
            created_at=self.clock.now_iso(),
        )

    def _audit(
        self, conn: sqlite3.Connection, c: Consultation, event_type: str, details: dict
    ) -> None:
        self.repo.add_audit(
            conn,
            consultation_id=c.consultation_id,
            event_type=event_type,
            actor_role=None,
            actor_id=details.get("by"),
            details=details,
            created_at=self.clock.now_iso(),
        )


def _grade_to_int(value: str | int) -> int:
    if isinstance(value, int):
        grade = value
    else:
        try:
            grade = Grade[value.upper()].value
        except KeyError:
            raise ValidationError("grade_invalid", f"未知服务等级 {value}")
    if grade == Grade.NONE:
        raise ValidationError("grade_invalid", "链路等级必须为 AUDIO/SD_VIDEO/HD_VIDEO")
    return grade
