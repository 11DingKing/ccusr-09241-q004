"""SQLite 持久化适配。

设计要点：
- 单连接 + 进程内可重入锁，写事务以 BEGIN IMMEDIATE 串行化，
  使并发排班下的资源占用检查与写入构成原子临界区；
- 会诊、资源占用、发件箱事件在同一事务提交，保证状态与通知一致；
- consultations.idempotency_key 唯一约束保证幂等重放不产生重复占用；
- 数据落盘，进程退出重启后占用记录与待发通知不丢失。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

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
from ..domain.errors import ConcurrentModification, NotFound
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    failure_domain TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    loss_pct REAL NOT NULL,
    availability REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_link ON probe_snapshots(link_id, measured_at);
CREATE TABLE IF NOT EXISTS authorizations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS consultations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    doctor_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    patient_json TEXT NOT NULL,
    min_service_level TEXT NOT NULL,
    current_service_level TEXT,
    institution_ids_json TEXT NOT NULL,
    primary_link_id TEXT NOT NULL,
    backup_link_id TEXT,
    active_link_id TEXT NOT NULL,
    consent_id TEXT NOT NULL,
    shared_risk INTEGER NOT NULL,
    pre_pause_status TEXT,
    pause_reason TEXT,
    cancel_reason TEXT,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_holds (
    id TEXT PRIMARY KEY,
    consultation_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holds_resource
    ON resource_holds(resource_type, resource_id, status);
CREATE TABLE IF NOT EXISTS manual_locks (
    id TEXT PRIMARY KEY,
    consultation_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox_events (
    id TEXT PRIMARY KEY,
    consultation_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status);
CREATE TABLE IF NOT EXISTS evaluation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consultation_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        self._local = threading.local()

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator["SQLiteStore"]:
        """写事务：BEGIN IMMEDIATE 串行化写者；嵌套调用并入外层事务。"""
        if getattr(self._local, "in_txn", False):
            yield self
            return
        with self._lock:
            self._local.in_txn = True
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._local.in_txn = False

    # ------------------------------------------------------------------
    # 机构与链路
    # ------------------------------------------------------------------
    def upsert_institution(self, inst: Institution) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO institutions(id, name, tier) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, tier=excluded.tier",
                (inst.id, inst.name, inst.tier),
            )

    def get_institution(self, inst_id: str) -> Institution | None:
        row = self._conn.execute(
            "SELECT * FROM institutions WHERE id=?", (inst_id,)
        ).fetchone()
        return Institution(id=row["id"], name=row["name"], tier=row["tier"]) if row else None

    def upsert_link(self, link: LinkPath) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO links(id, institution_id, kind, failure_domain) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET institution_id=excluded.institution_id, "
                "kind=excluded.kind, failure_domain=excluded.failure_domain",
                (link.id, link.institution_id, link.kind.value, link.failure_domain),
            )

    def get_link(self, link_id: str) -> LinkPath | None:
        row = self._conn.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
        return self._to_link(row) if row else None

    @staticmethod
    def _to_link(row: sqlite3.Row) -> LinkPath:
        return LinkPath(
            id=row["id"],
            institution_id=row["institution_id"],
            kind=LinkKind(row["kind"]),
            failure_domain=row["failure_domain"],
        )

    # ------------------------------------------------------------------
    # 探测快照
    # ------------------------------------------------------------------
    def add_probe(self, snap: ProbeSnapshot) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO probe_snapshots(link_id, measured_at, latency_ms, loss_pct, availability)"
                " VALUES(?,?,?,?,?)",
                (
                    snap.link_id,
                    snap.measured_at.isoformat(),
                    snap.latency_ms,
                    snap.loss_pct,
                    snap.availability,
                ),
            )

    def latest_probe(self, link_id: str) -> ProbeSnapshot | None:
        row = self._conn.execute(
            "SELECT * FROM probe_snapshots WHERE link_id=? ORDER BY measured_at DESC, id DESC LIMIT 1",
            (link_id,),
        ).fetchone()
        if not row:
            return None
        return ProbeSnapshot(
            link_id=row["link_id"],
            measured_at=_dt(row["measured_at"]),
            latency_ms=row["latency_ms"],
            loss_pct=row["loss_pct"],
            availability=row["availability"],
        )

    # ------------------------------------------------------------------
    # 授权
    # ------------------------------------------------------------------
    def upsert_authorization(self, auth: Authorization) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO authorizations(id, kind, subject_id, scope, valid_from, valid_until,"
                " status, payload_json, revoked_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, subject_id=excluded.subject_id,"
                " scope=excluded.scope, valid_from=excluded.valid_from,"
                " valid_until=excluded.valid_until, status=excluded.status,"
                " payload_json=excluded.payload_json, revoked_at=excluded.revoked_at",
                (
                    auth.id,
                    auth.kind.value,
                    auth.subject_id,
                    auth.scope,
                    auth.valid_from.isoformat(),
                    auth.valid_until.isoformat(),
                    auth.status.value,
                    json.dumps(auth.payload, ensure_ascii=False, sort_keys=True),
                    auth.revoked_at.isoformat() if auth.revoked_at else None,
                ),
            )

    def get_authorization(self, auth_id: str) -> Authorization | None:
        row = self._conn.execute(
            "SELECT * FROM authorizations WHERE id=?", (auth_id,)
        ).fetchone()
        return self._to_auth(row) if row else None

    def find_institution_authorization(self, institution_id: str) -> Authorization | None:
        # 优先返回仍处于有效状态的授权；全部失效时返回最近一条以便诊断
        row = self._conn.execute(
            "SELECT * FROM authorizations WHERE kind=? AND subject_id=?"
            " ORDER BY (status='ACTIVE') DESC, valid_until DESC LIMIT 1",
            (AuthorizationKind.INSTITUTION.value, institution_id),
        ).fetchone()
        return self._to_auth(row) if row else None

    def mark_authorization_revoked(self, auth_id: str, revoked_at: datetime) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE authorizations SET status=?, revoked_at=? WHERE id=? AND status=?",
                (
                    AuthorizationStatus.REVOKED.value,
                    revoked_at.isoformat(),
                    auth_id,
                    AuthorizationStatus.ACTIVE.value,
                ),
            )
            if cur.rowcount == 0:
                existing = self.get_authorization(auth_id)
                if existing is None:
                    raise NotFound(f"授权不存在: {auth_id}")

    @staticmethod
    def _to_auth(row: sqlite3.Row) -> Authorization:
        return Authorization(
            id=row["id"],
            kind=AuthorizationKind(row["kind"]),
            subject_id=row["subject_id"],
            scope=row["scope"],
            valid_from=_dt(row["valid_from"]),
            valid_until=_dt(row["valid_until"]),
            status=AuthorizationStatus(row["status"]),
            payload=json.loads(row["payload_json"]),
            revoked_at=_dt(row["revoked_at"]) if row["revoked_at"] else None,
        )

    # ------------------------------------------------------------------
    # 会诊
    # ------------------------------------------------------------------
    def insert_consultation(self, c: Consultation) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO consultations(id, idempotency_key, status, phase, slot_start, slot_end,"
                " doctor_id, equipment_id, patient_json, min_service_level, current_service_level,"
                " institution_ids_json, primary_link_id, backup_link_id, active_link_id, consent_id,"
                " shared_risk, pre_pause_status, pause_reason, cancel_reason, version,"
                " created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    c.id,
                    c.idempotency_key,
                    c.status.value,
                    c.phase.value,
                    c.slot_start.isoformat(),
                    c.slot_end.isoformat(),
                    c.doctor_id,
                    c.equipment_id,
                    json.dumps(
                        {
                            "ref": c.patient.ref,
                            "name": c.patient.name,
                            "medical_record_no": c.patient.medical_record_no,
                        },
                        ensure_ascii=False,
                    ),
                    c.min_service_level.value,
                    c.current_service_level.value if c.current_service_level else None,
                    json.dumps(c.institution_ids),
                    c.primary_link_id,
                    c.backup_link_id,
                    c.active_link_id,
                    c.consent_id,
                    1 if c.shared_risk else 0,
                    c.pre_pause_status.value if c.pre_pause_status else None,
                    c.pause_reason,
                    c.cancel_reason,
                    c.version,
                    c.created_at.isoformat(),
                    c.updated_at.isoformat(),
                ),
            )

    def get_consultation(self, consultation_id: str) -> Consultation:
        row = self._conn.execute(
            "SELECT * FROM consultations WHERE id=?", (consultation_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"会诊不存在: {consultation_id}")
        return self._to_consultation(row)

    def find_consultation_by_idempotency_key(self, key: str) -> Consultation | None:
        row = self._conn.execute(
            "SELECT * FROM consultations WHERE idempotency_key=?", (key,)
        ).fetchone()
        return self._to_consultation(row) if row else None

    def update_consultation(self, c: Consultation, expected_version: int) -> None:
        """乐观并发控制：版本不匹配则拒绝写入。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE consultations SET status=?, phase=?, current_service_level=?,"
                " active_link_id=?, shared_risk=?, pre_pause_status=?, pause_reason=?,"
                " cancel_reason=?, version=?, updated_at=? WHERE id=? AND version=?",
                (
                    c.status.value,
                    c.phase.value,
                    c.current_service_level.value if c.current_service_level else None,
                    c.active_link_id,
                    1 if c.shared_risk else 0,
                    c.pre_pause_status.value if c.pre_pause_status else None,
                    c.pause_reason,
                    c.cancel_reason,
                    expected_version + 1,
                    c.updated_at.isoformat(),
                    c.id,
                    expected_version,
                ),
            )
            if cur.rowcount == 0:
                raise ConcurrentModification(f"会诊 {c.id} 已被并发修改")
            c.version = expected_version + 1

    def list_open_consultations(self) -> list[Consultation]:
        rows = self._conn.execute(
            "SELECT * FROM consultations WHERE status NOT IN (?, ?)",
            (ConsultationStatus.COMPLETED.value, ConsultationStatus.CANCELLED.value),
        ).fetchall()
        return [self._to_consultation(r) for r in rows]

    def list_consultations_touching_link(self, link_id: str) -> list[Consultation]:
        rows = self._conn.execute(
            "SELECT * FROM consultations WHERE status NOT IN (?, ?)"
            " AND (primary_link_id=? OR backup_link_id=? OR active_link_id=?)",
            (
                ConsultationStatus.COMPLETED.value,
                ConsultationStatus.CANCELLED.value,
                link_id,
                link_id,
                link_id,
            ),
        ).fetchall()
        return [self._to_consultation(r) for r in rows]

    @staticmethod
    def _to_consultation(row: sqlite3.Row) -> Consultation:
        patient = json.loads(row["patient_json"])
        return Consultation(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            status=ConsultationStatus(row["status"]),
            phase=PhaseKind(row["phase"]),
            slot_start=_dt(row["slot_start"]),
            slot_end=_dt(row["slot_end"]),
            doctor_id=row["doctor_id"],
            equipment_id=row["equipment_id"],
            patient=Patient(
                ref=patient["ref"],
                name=patient["name"],
                medical_record_no=patient["medical_record_no"],
            ),
            min_service_level=ServiceLevel(row["min_service_level"]),
            current_service_level=(
                ServiceLevel(row["current_service_level"]) if row["current_service_level"] else None
            ),
            institution_ids=json.loads(row["institution_ids_json"]),
            primary_link_id=row["primary_link_id"],
            backup_link_id=row["backup_link_id"],
            active_link_id=row["active_link_id"],
            consent_id=row["consent_id"],
            shared_risk=bool(row["shared_risk"]),
            pre_pause_status=(
                ConsultationStatus(row["pre_pause_status"]) if row["pre_pause_status"] else None
            ),
            pause_reason=row["pause_reason"],
            cancel_reason=row["cancel_reason"],
            version=row["version"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # 资源占用
    # ------------------------------------------------------------------
    def insert_hold(self, hold: ResourceHold) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO resource_holds(id, consultation_id, resource_type, resource_id,"
                " slot_start, slot_end, status) VALUES(?,?,?,?,?,?,?)",
                (
                    hold.id,
                    hold.consultation_id,
                    hold.resource_type.value,
                    hold.resource_id,
                    hold.slot_start.isoformat(),
                    hold.slot_end.isoformat(),
                    hold.status.value,
                ),
            )

    def find_overlapping_holds(
        self,
        resource_type: ResourceType,
        resource_id: str,
        slot_start: datetime,
        slot_end: datetime,
    ) -> list[ResourceHold]:
        rows = self._conn.execute(
            "SELECT * FROM resource_holds WHERE status=? AND resource_type=? AND resource_id=?"
            " AND slot_start < ? AND slot_end > ?",
            (
                HoldStatus.HELD.value,
                resource_type.value,
                resource_id,
                slot_end.isoformat(),
                slot_start.isoformat(),
            ),
        ).fetchall()
        return [self._to_hold(r) for r in rows]

    def holds_for_consultation(self, consultation_id: str) -> list[ResourceHold]:
        rows = self._conn.execute(
            "SELECT * FROM resource_holds WHERE consultation_id=?", (consultation_id,)
        ).fetchall()
        return [self._to_hold(r) for r in rows]

    def release_holds(self, consultation_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE resource_holds SET status=? WHERE consultation_id=? AND status=?",
                (HoldStatus.RELEASED.value, consultation_id, HoldStatus.HELD.value),
            )
            return cur.rowcount

    def release_orphan_holds(self) -> int:
        """释放已终止会诊遗留的占用（崩溃残留的兜底清理）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE resource_holds SET status=? WHERE status=? AND consultation_id IN"
                " (SELECT id FROM consultations WHERE status IN (?, ?))",
                (
                    HoldStatus.RELEASED.value,
                    HoldStatus.HELD.value,
                    ConsultationStatus.COMPLETED.value,
                    ConsultationStatus.CANCELLED.value,
                ),
            )
            return cur.rowcount

    def count_holds(self, status: HoldStatus = HoldStatus.HELD) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM resource_holds WHERE status=?", (status.value,)
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _to_hold(row: sqlite3.Row) -> ResourceHold:
        return ResourceHold(
            id=row["id"],
            consultation_id=row["consultation_id"],
            resource_type=ResourceType(row["resource_type"]),
            resource_id=row["resource_id"],
            slot_start=_dt(row["slot_start"]),
            slot_end=_dt(row["slot_end"]),
            status=HoldStatus(row["status"]),
        )

    # ------------------------------------------------------------------
    # 人工锁定
    # ------------------------------------------------------------------
    def insert_lock(self, lock: ManualLock) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO manual_locks(id, consultation_id, operator, reason, created_at,"
                " expires_at, released_at) VALUES(?,?,?,?,?,?,?)",
                (
                    lock.id,
                    lock.consultation_id,
                    lock.operator,
                    lock.reason,
                    lock.created_at.isoformat(),
                    lock.expires_at.isoformat(),
                    lock.released_at.isoformat() if lock.released_at else None,
                ),
            )

    def active_lock(self, consultation_id: str, now: datetime) -> ManualLock | None:
        row = self._conn.execute(
            "SELECT * FROM manual_locks WHERE consultation_id=? AND released_at IS NULL"
            " AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (consultation_id, now.isoformat()),
        ).fetchone()
        return self._to_lock(row) if row else None

    def release_lock(self, lock_id: str, released_at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE manual_locks SET released_at=? WHERE id=?",
                (released_at.isoformat(), lock_id),
            )

    def expire_locks(self, now: datetime) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE manual_locks SET released_at=? WHERE released_at IS NULL AND expires_at<=?",
                (now.isoformat(), now.isoformat()),
            )
            return cur.rowcount

    @staticmethod
    def _to_lock(row: sqlite3.Row) -> ManualLock:
        return ManualLock(
            id=row["id"],
            consultation_id=row["consultation_id"],
            operator=row["operator"],
            reason=row["reason"],
            created_at=_dt(row["created_at"]),
            expires_at=_dt(row["expires_at"]),
            released_at=_dt(row["released_at"]) if row["released_at"] else None,
        )

    # ------------------------------------------------------------------
    # 发件箱
    # ------------------------------------------------------------------
    def insert_outbox_event(self, event: OutboxEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO outbox_events(id, consultation_id, type, payload_json, status,"
                " attempts, created_at, updated_at, last_error) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event.id,
                    event.consultation_id,
                    event.type,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.status.value,
                    event.attempts,
                    event.created_at.isoformat(),
                    event.updated_at.isoformat(),
                    event.last_error,
                ),
            )

    def pending_outbox_events(self, limit: int = 50) -> list[OutboxEvent]:
        rows = self._conn.execute(
            "SELECT * FROM outbox_events WHERE status IN (?, ?) ORDER BY created_at, id LIMIT ?",
            (OutboxStatus.PENDING.value, OutboxStatus.FAILED.value, limit),
        ).fetchall()
        return [self._to_event(r) for r in rows]

    def list_outbox_events(self, consultation_id: str | None = None) -> list[OutboxEvent]:
        if consultation_id is None:
            rows = self._conn.execute(
                "SELECT * FROM outbox_events ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM outbox_events WHERE consultation_id=? ORDER BY created_at, id",
                (consultation_id,),
            ).fetchall()
        return [self._to_event(r) for r in rows]

    def mark_outbox_sent(self, event_id: str, now: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox_events SET status=?, attempts=attempts+1, updated_at=?, last_error=NULL"
                " WHERE id=?",
                (OutboxStatus.SENT.value, now.isoformat(), event_id),
            )

    def mark_outbox_failed(self, event_id: str, now: datetime, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox_events SET status=?, attempts=attempts+1, updated_at=?, last_error=?"
                " WHERE id=?",
                (OutboxStatus.FAILED.value, now.isoformat(), error, event_id),
            )

    @staticmethod
    def _to_event(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            id=row["id"],
            consultation_id=row["consultation_id"],
            type=row["type"],
            payload=json.loads(row["payload_json"]),
            status=OutboxStatus(row["status"]),
            attempts=row["attempts"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            last_error=row["last_error"],
        )

    # ------------------------------------------------------------------
    # 评估日志
    # ------------------------------------------------------------------
    def add_evaluation_log(
        self, consultation_id: str, trigger: str, decision: str, reasons: list[str], now: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO evaluation_log(consultation_id, trigger, decision, reasons_json, created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    consultation_id,
                    trigger,
                    decision,
                    json.dumps(reasons, ensure_ascii=False),
                    now.isoformat(),
                ),
            )

    def list_evaluation_log(self, consultation_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM evaluation_log WHERE consultation_id=? ORDER BY id",
            (consultation_id,),
        ).fetchall()
        return [
            {
                "trigger": r["trigger"],
                "decision": r["decision"],
                "reasons": json.loads(r["reasons_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
