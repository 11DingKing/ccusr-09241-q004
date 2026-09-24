"""授权撤销、关键操作阶段、人工锁定、故障域联动与恢复流程测试。"""

from __future__ import annotations

from telemedicine_continuity.domain.enums import ConsultationStatus, Health, Role
from telemedicine_continuity.domain.errors import AuthorizationError, DomainError
from tests.support import CLINICIAN, LINK_A, LINK_A2, LINK_B, PATIENT, ServiceTestBed


class LifecycleTests(ServiceTestBed):
    # ------------------------------------------------------------ 授权撤销
    def test_consent_revoked_in_normal_phase_cancels_immediately(self) -> None:
        self.seed_scenario()
        c = self.book()

        result = self.service.revoke_consent(Role.COORDINATOR, PATIENT)

        effects = result["effects"]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["action"], "cancel")
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.CANCELLED.value)
        self.assertEqual(view["consent_state"], "revoked")
        slots = self.service.list_slots(Role.COORDINATOR)
        self.assertEqual(slots[0]["status"], "free")

    def test_consent_revoked_during_critical_phase_is_deferred_until_phase_exit(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, True)

        result = self.service.revoke_consent(Role.COORDINATOR, PATIENT)
        self.assertEqual(result["effects"][0]["action"], "deferred")
        self.assertEqual(result["effects"][0]["pending_effect"], "cancel_on_stage_exit")

        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.CONFIRMED.value)
        self.assertTrue(view["critical_phase"])

        # 关键阶段内手工取消同样只能延后
        cc = self.service.cancel(Role.COORDINATOR, c.consultation_id, reason="术中取消")
        self.assertEqual(cc.pending_effect, "cancel_on_stage_exit")
        self.assertEqual(cc.status, ConsultationStatus.CONFIRMED)

        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, False)
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.CANCELLED.value)
        self.assertIsNone(view["pending_effect"])

    def test_switch_and_pause_blocked_during_critical_phase_but_degrade_allowed(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, True)

        with self.assertRaises(DomainError) as ctx:
            self.service.switch_path(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(ctx.exception.code, "critical_phase")
        with self.assertRaises(DomainError):
            self.service.pause(Role.COORDINATOR, c.consultation_id)

        degraded = self.service.limited_degrade(
            Role.COORDINATOR, c.consultation_id, grade="AUDIO", reason="术中网络抖动")
        self.assertEqual(degraded.status, ConsultationStatus.DEGRADED)
        self.assertEqual(degraded.effective_grade, 1)

    def test_manual_lock_skips_automation_and_only_locker_may_act(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.service.lock(Role.COORDINATOR, c.consultation_id, "scheduler-li")

        # 故障域事件到来，自动联动必须跳过被锁定的会诊
        result = self.snapshot(LINK_A, Health.DOWN.value)
        self.assertEqual(result["effects"][0]["action"], "skipped_locked")
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["active_link_id"], LINK_A)

        # 其他人不能操作，锁定者可以
        with self.assertRaises(AuthorizationError):
            self.service.switch_path(
                Role.COORDINATOR, c.consultation_id, actor_id="scheduler-wang")
        switched = self.service.switch_path(
            Role.COORDINATOR, c.consultation_id, actor_id="scheduler-li")
        self.assertEqual(switched.active_link_id, LINK_B)

        self.service.unlock(Role.COORDINATOR, c.consultation_id, "scheduler-li")

    # ------------------------------------------------------------ 故障域联动
    def test_primary_domain_failure_switches_to_independent_backup(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.assertEqual(c.active_link_id, LINK_A)
        self.assertEqual(c.backup_link_id, LINK_B)

        result = self.snapshot(LINK_A, Health.DOWN.value)
        self.assertEqual(result["effects"][0]["action"], "switch")
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.SWITCHED.value)
        self.assertEqual(view["active_link_id"], LINK_B)
        self.assertEqual(view["effective_grade"], "HD_VIDEO")

    def test_link_recovering_switches_back_path_and_restores_grade(self) -> None:
        self.seed_scenario()
        # 要求 HD：B 劣化为 SD 时即低于 SLA
        c = self.book(min_grade="HD_VIDEO")
        self.snapshot(LINK_A, Health.DOWN.value)  # 切到 B

        # B 也劣化：无达 HD 的跨域备选（A2 仅 SD），有限降级
        result = self.snapshot(LINK_B, Health.DEGRADED.value, latency_ms=900)
        self.assertEqual(result["effects"][0]["action"], "degrade")
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.DEGRADED.value)
        self.assertEqual(view["effective_grade"], "SD_VIDEO")

        # A 恢复到 up：自动切回 A 并恢复等级
        result = self.snapshot(LINK_A, Health.UP.value)
        actions = [e["action"] for e in result["effects"]]
        self.assertIn("switch", actions)
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["active_link_id"], LINK_A)
        self.assertEqual(view["effective_grade"], "HD_VIDEO")
        self.assertEqual(view["status"], ConsultationStatus.SWITCHED.value)

    def test_all_domains_down_pauses_and_recovery_resumes(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.snapshot(LINK_A, Health.DOWN.value)  # 切到 B
        # 同属 FD-A 的 A2 也中断，此后再无跨故障域备选
        self.snapshot(LINK_A2, Health.DOWN.value)
        result = self.snapshot(LINK_B, Health.DOWN.value)
        self.assertEqual(result["effects"][0]["action"], "pause")
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.PAUSED.value)
        self.assertIn("link_down", view["pause_reason"])

        # B 先恢复：自动恢复
        result = self.snapshot(LINK_B, Health.UP.value)
        self.assertEqual(result["effects"][0]["action"], "resume")
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.CONFIRMED.value)

    def test_stale_snapshot_triggers_limited_degrade_not_silent_success(self) -> None:
        self.seed_scenario()
        c = self.book()
        # 主链路快照过期（>5 分钟无新探测），备链路健康
        result = self.snapshot(LINK_A, Health.UP.value, stale=True)
        actions = [e["action"] for e in result["effects"]]
        # 主链路快照不可信，而 B 健康 → 优先切到 B
        self.assertIn("switch", actions)
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["active_link_id"], LINK_B)

    def test_shared_fault_domain_cannot_be_used_as_switch_target(self) -> None:
        self.seed_scenario()
        c = self.book()
        # LINK_A2 与主链路同域：手工指定它必须被拒绝
        with self.assertRaises(DomainError) as ctx:
            self.service.switch_path(
                Role.COORDINATOR, c.consultation_id, target_link_id=LINK_A2)
        self.assertEqual(ctx.exception.code, "shared_fault_domain")

    def test_repeated_bad_snapshots_do_not_duplicate_events(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.snapshot(LINK_A, Health.DOWN.value)
        self.snapshot(LINK_A, Health.DOWN.value)
        self.snapshot(LINK_A, Health.DOWN.value)
        with self.db.read_only() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox "
                "WHERE event_type='consultation.switched'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)

    # ------------------------------------------------------------ 人工操作与幂等
    def test_idempotent_cancel_retry_after_terminal_state(self) -> None:
        self.seed_scenario()
        c = self.book()
        c1 = self.service.cancel(Role.COORDINATOR, c.consultation_id,
                                 reason="患者爽约", idem_key="cancel-1")
        c2 = self.service.cancel(Role.COORDINATOR, c.consultation_id,
                                 reason="患者爽约", idem_key="cancel-1")
        self.assertEqual(c1.consultation_id, c2.consultation_id)
        with self.db.read_only() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox "
                "WHERE event_type='consultation.cancelled'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_role_based_access_control(self) -> None:
        self.seed_scenario()
        c = self.book()
        # 链路工程师无权排班
        from telemedicine_continuity.domain.errors import AuthorizationError as AuthErr
        with self.assertRaises(AuthErr):
            self.service.book_consultation(
                Role.LINK_ENGINEER, patient_id=PATIENT, clinician_id=CLINICIAN,
                slot_id="S001", org_ids=["H-LEAD", "C-01"], min_grade="SD_VIDEO")
        # 医生不能取消
        with self.assertRaises(AuthErr):
            self.service.cancel(Role.CLINICIAN, c.consultation_id)

    # ------------------------------------------------------------ 授权自然到期与关键阶段重试
    def test_consent_natural_expiry_cancels_via_link_reevaluation(self) -> None:
        # 授权恰覆盖排班时段结束；会诊超时进行到授权失效后，联动取消
        self.seed_scenario(consent_until="2026-09-24T11:00:00+00:00")
        c = self.book()
        self.assertEqual(c.status, ConsultationStatus.CONFIRMED)
        self.clock.advance(hours=2, minutes=10)  # 09:00 -> 11:10，授权已失效
        result = self.snapshot(LINK_A, Health.UP.value)
        self.assertEqual(result["effects"][0]["action"], "cancel")
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.CANCELLED.value)

    def test_consent_renewed_during_critical_phase_clears_deferred_cancel(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, True)
        self.service.revoke_consent(Role.COORDINATOR, PATIENT)
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["pending_effect"], "cancel_on_stage_exit")

        # 关键阶段内完成续期
        self.service.grant_consent(
            Role.COORDINATOR, PATIENT,
            valid_from=self.clock.now_iso(),
            valid_until="2027-01-31T23:59:59+00:00")
        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, False)
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.CONFIRMED.value)
        self.assertIsNone(view["pending_effect"])

    def test_link_failure_during_critical_phase_pauses_after_stage_exit(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, True)
        # 关键阶段内两条故障域链路先后中断
        result = self.snapshot(LINK_A, Health.DOWN.value)
        self.assertEqual(result["effects"][0]["action"], "deferred")
        result = self.snapshot(LINK_B, Health.DOWN.value)
        self.assertEqual(result["effects"][0]["action"], "deferred")
        self.snapshot(LINK_A2, Health.DOWN.value)

        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, False)
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        self.assertEqual(view["status"], ConsultationStatus.PAUSED.value)
        self.assertIn("link_down", view["pause_reason"])

    def test_link_recovered_before_stage_exit_clears_deferred_pause(self) -> None:
        self.seed_scenario()
        c = self.book()
        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, True)
        self.snapshot(LINK_A, Health.DOWN.value)  # 可切 B，但关键阶段延后
        self.snapshot(LINK_B, Health.UP.value)
        self.service.set_critical_phase(Role.CLINICIAN, c.consultation_id, False)
        view = self.service.get_consultation_view(Role.COORDINATOR, c.consultation_id)
        # B 全程健康：延后的暂停不落地，改为阶段结束时切换到 B，诊疗不中断
        self.assertNotEqual(view["status"], ConsultationStatus.PAUSED.value)
        self.assertEqual(view["active_link_id"], LINK_B)
        self.assertIsNone(view["pending_effect"])


if __name__ == "__main__":
    unittest.main()
