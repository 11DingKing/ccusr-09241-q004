"""SQLite 持久化适配。

- WAL + 每次事务使用独立连接，BEGIN IMMEDIATE 保证跨线程/跨进程写串行化；
- 时段占用、会诊状态、审计日志与通知发件箱在同一事务提交（原子提交）；
- 发件箱事件以 event_key 去重，状态机 pending -> sending -> sent，
  进程崩溃残留的 sending 在重启时回收为 pending，由接收方按 event_id 幂等去重。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..domain.enums import ConsultationStatus, CriticalPhase, Health
from ..domain.models import (
    Clinician,
    Consent,
    Consultation,
    Link,
    Organization,
    OutboxEvent,
    Patient,
    Slot,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    org_id TEXT PRIMARY KEY,
    name   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clinicians (
    clinician_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    org_id       TEXT NOT NULL REFERENCES organizations(org_id)
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    national_id   TEXT NOT NULL,
    contact_phone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    link_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    fault_domain  TEXT NOT NULL,
    grade         INTEGER NOT NULL,
    org_ids       TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_snapshots (
    link_id     TEXT PRIMARY KEY REFERENCES links(link_id),
    health      TEXT NOT NULL,
    latency_ms  INTEGER,
    loss_rate   REAL,
    snapshot_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    patient_id  TEXT PRIMARY KEY REFERENCES patients(patient_id),
    state       TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    slot_id         TEXT PRIMARY KEY,
    clinician_id    TEXT NOT NULL REFERENCES clinicians(clinician_id),
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'free',
    consultation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_slots_clinician_time
    ON slots(clinician_id, start_time, end_time);

CREATE TABLE IF NOT EXISTS consultations (
    consultation_id     TEXT PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    patient_id          TEXT NOT NULL,
    clinician_id        TEXT NOT NULL,
    slot_id             TEXT NOT NULL,
    link_ids            TEXT NOT NULL,
    org_ids             TEXT NOT NULL,
    min_grade           INTEGER NOT NULL,
    required_link_count INTEGER NOT NULL,
    status              TEXT NOT NULL,
    active_link_id      TEXT NOT NULL,
    backup_link_id      TEXT,
    effective_grade     INTEGER NOT NULL,
    consent_valid_until TEXT NOT NULL,
    consent_state       TEXT NOT NULL,
    critical_phase      TEXT NOT NULL DEFAULT 'normal',
    pause_reason        TEXT,
    locked_by           TEXT,
    pending_effect      TEXT,
    idem_key            TEXT UNIQUE,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consultation_id TEXT,
    event_type      TEXT NOT NULL,
    actor_role      TEXT,
    actor_id        TEXT,
    details         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    event_id     TEXT PRIMARY KEY,
    event_key    TEXT NOT NULL UNIQUE,
    event_type   TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    sent_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
"""


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 30_000) -> None:
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            # 崩溃恢复：claim 后未确认的事件退回待发
            conn.execute("UPDATE outbox SET status='pending' WHERE status='sending'")
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务：拿到保留锁后才执行业务，避免读后写竞争。"""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read_only(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()


# ---------------------------------------------------------------- 行映射

def _row_to_link(row: sqlite3.Row | None) -> Link | None:
    if row is None:
        return None
    return Link(
        link_id=row["link_id"],
        name=row["name"],
        fault_domain=row["fault_domain"],
        grade=row["grade"],
        health=Health(row["health"]) if row["health"] is not None else Health.UNKNOWN,
        latency_ms=row["latency_ms"],
        loss_rate=row["loss_rate"],
        org_ids=tuple(_loads(row["org_ids"])),
        snapshot_at=row["snapshot_at"],
    )


def _row_to_consultation(row: sqlite3.Row | None) -> Consultation | None:
    if row is None:
        return None
    return Consultation(
        consultation_id=row["consultation_id"],
        code=row["code"],
        patient_id=row["patient_id"],
        clinician_id=row["clinician_id"],
        slot_id=row["slot_id"],
        link_ids=tuple(_loads(row["link_ids"])),
        org_ids=tuple(_loads(row["org_ids"])),
        min_grade=row["min_grade"],
        required_link_count=row["required_link_count"],
        status=ConsultationStatus(row["status"]),
        active_link_id=row["active_link_id"],
        backup_link_id=row["backup_link_id"],
        effective_grade=row["effective_grade"],
        consent_valid_until=row["consent_valid_until"],
        consent_state=row["consent_state"],
        critical_phase=CriticalPhase(row["critical_phase"]),
        pause_reason=row["pause_reason"],
        locked_by=row["locked_by"],
        pending_effect=row["pending_effect"],
        idem_key=row["idem_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
    )


def _row_to_outbox(row: sqlite3.Row | None) -> OutboxEvent | None:
    if row is None:
        return None
    return OutboxEvent(
        event_id=row["event_id"],
        event_key=row["event_key"],
        event_type=row["event_type"],
        aggregate_id=row["aggregate_id"],
        payload=_loads(row["payload"]),
        status=row["status"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        sent_at=row["sent_at"],
    )


class Repository:
    """所有方法均以调用方持有的连接为参数，事务边界由服务层决定。"""

    # ---- 基础数据 ----
    def upsert_org(self, conn: sqlite3.Connection, org: Organization) -> None:
        conn.execute(
            "INSERT INTO organizations(org_id, name) VALUES(?,?) "
            "ON CONFLICT(org_id) DO UPDATE SET name=excluded.name",
            (org.org_id, org.name),
        )

    def get_org(self, conn: sqlite3.Connection, org_id: str) -> Organization | None:
        row = conn.execute("SELECT * FROM organizations WHERE org_id=?", (org_id,)).fetchone()
        return Organization(row["org_id"], row["name"]) if row else None

    def upsert_clinician(self, conn: sqlite3.Connection, clinician: Clinician) -> None:
        conn.execute(
            "INSERT INTO clinicians(clinician_id, name, org_id) VALUES(?,?,?) "
            "ON CONFLICT(clinician_id) DO UPDATE SET name=excluded.name, org_id=excluded.org_id",
            (clinician.clinician_id, clinician.name, clinician.org_id),
        )

    def get_clinician(self, conn: sqlite3.Connection, clinician_id: str) -> Clinician | None:
        row = conn.execute(
            "SELECT * FROM clinicians WHERE clinician_id=?", (clinician_id,)
        ).fetchone()
        return Clinician(row["clinician_id"], row["name"], row["org_id"]) if row else None

    def upsert_patient(self, conn: sqlite3.Connection, patient: Patient) -> None:
        conn.execute(
            "INSERT INTO patients(patient_id, name, national_id, contact_phone) VALUES(?,?,?,?) "
            "ON CONFLICT(patient_id) DO UPDATE SET name=excluded.name, "
            "national_id=excluded.national_id, contact_phone=excluded.contact_phone",
            (patient.patient_id, patient.name, patient.national_id, patient.contact_phone),
        )

    def get_patient(self, conn: sqlite3.Connection, patient_id: str) -> Patient | None:
        row = conn.execute(
            "SELECT * FROM patients WHERE patient_id=?", (patient_id,)
        ).fetchone()
        if not row:
            return None
        return Patient(row["patient_id"], row["name"], row["national_id"], row["contact_phone"])

    # ---- 链路与快照 ----
    def register_link(self, conn: sqlite3.Connection, link: Link, now_iso: str) -> None:
        conn.execute(
            "INSERT INTO links(link_id, name, fault_domain, grade, org_ids, registered_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(link_id) DO UPDATE SET "
            "name=excluded.name, fault_domain=excluded.fault_domain, "
            "grade=excluded.grade, org_ids=excluded.org_ids",
            (
                link.link_id,
                link.name,
                link.fault_domain,
                link.grade,
                json.dumps(list(link.org_ids)),
                now_iso,
            ),
        )

    def upsert_snapshot(
        self,
        conn: sqlite3.Connection,
        link_id: str,
        health: Health,
        latency_ms: int | None,
        loss_rate: float | None,
        snapshot_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO link_snapshots(link_id, health, latency_ms, loss_rate, snapshot_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(link_id) DO UPDATE SET health=excluded.health, "
            "latency_ms=excluded.latency_ms, loss_rate=excluded.loss_rate, "
            "snapshot_at=excluded.snapshot_at",
            (link_id, health.value, latency_ms, loss_rate, snapshot_at),
        )

    def _link_select(self) -> str:
        return (
            "SELECT l.*, s.health, s.latency_ms, s.loss_rate, s.snapshot_at "
            "FROM links l LEFT JOIN link_snapshots s ON s.link_id=l.link_id"
        )

    def get_link(self, conn: sqlite3.Connection, link_id: str) -> Link | None:
        row = conn.execute(
            self._link_select() + " WHERE l.link_id=?", (link_id,)
        ).fetchone()
        return _row_to_link(row)

    def list_links(self, conn: sqlite3.Connection) -> list[Link]:
        rows = conn.execute(self._link_select() + " ORDER BY l.link_id").fetchall()
        return [_row_to_link(row) for row in rows]

    def links_by_domain(self, conn: sqlite3.Connection, domain: str) -> list[Link]:
        rows = conn.execute(
            self._link_select() + " WHERE l.fault_domain=? ORDER BY l.link_id", (domain,)
        ).fetchall()
        return [_row_to_link(row) for row in rows]

    # ---- 授权 ----
    def set_consent(self, conn: sqlite3.Connection, consent: Consent, now_iso: str) -> None:
        conn.execute(
            "INSERT INTO consents(patient_id, state, valid_from, valid_until, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(patient_id) DO UPDATE SET state=excluded.state, "
            "valid_from=excluded.valid_from, valid_until=excluded.valid_until, "
            "updated_at=excluded.updated_at",
            (
                consent.patient_id,
                consent.state,
                consent.valid_from,
                consent.valid_until,
                now_iso,
            ),
        )

    def get_consent(self, conn: sqlite3.Connection, patient_id: str) -> Consent | None:
        row = conn.execute(
            "SELECT * FROM consents WHERE patient_id=?", (patient_id,)
        ).fetchone()
        if not row:
            return None
        return Consent(
            patient_id=row["patient_id"],
            state=row["state"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
        )

    # ---- 时段 ----
    def create_slot(
        self, conn: sqlite3.Connection, slot: Slot
    ) -> None:
        conn.execute(
            "INSERT INTO slots(slot_id, clinician_id, start_time, end_time, status, consultation_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                slot.slot_id,
                slot.clinician_id,
                slot.start_time,
                slot.end_time,
                slot.status,
                slot.consultation_id,
            ),
        )

    def get_slot(self, conn: sqlite3.Connection, slot_id: str) -> Slot | None:
        row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
        return self._row_to_slot(row)

    @staticmethod
    def _row_to_slot(row: sqlite3.Row | None) -> Slot | None:
        if row is None:
            return None
        return Slot(
            slot_id=row["slot_id"],
            clinician_id=row["clinician_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            status=row["status"],
            consultation_id=row["consultation_id"],
        )

    def find_clinician_overlap(
        self, conn: sqlite3.Connection, clinician_id: str, start: str, end: str
    ) -> Slot | None:
        """同一医生时间区间重叠且已占用的时段（半开区间）。"""
        row = conn.execute(
            "SELECT * FROM slots WHERE clinician_id=? AND status='occupied' "
            "AND start_time < ? AND end_time > ? LIMIT 1",
            (clinician_id, end, start),
        ).fetchone()
        return self._row_to_slot(row)

    def occupy_slot(
        self, conn: sqlite3.Connection, slot_id: str, consultation_id: str
    ) -> bool:
        cur = conn.execute(
            "UPDATE slots SET status='occupied', consultation_id=? "
            "WHERE slot_id=? AND status='free'",
            (consultation_id, slot_id),
        )
        return cur.rowcount == 1

    def release_slot(self, conn: sqlite3.Connection, slot_id: str) -> None:
        conn.execute(
            "UPDATE slots SET status='free', consultation_id=NULL "
            "WHERE slot_id=?",
            (slot_id,),
        )

    def list_slots(self, conn: sqlite3.Connection) -> list[Slot]:
        rows = conn.execute("SELECT * FROM slots ORDER BY start_time, slot_id").fetchall()
        return [self._row_to_slot(row) for row in rows]

    # ---- 会诊 ----
    def insert_consultation(self, conn: sqlite3.Connection, c: Consultation) -> None:
        conn.execute(
            "INSERT INTO consultations(consultation_id, code, patient_id, clinician_id, slot_id, "
            "link_ids, org_ids, min_grade, required_link_count, status, active_link_id, "
            "backup_link_id, effective_grade, consent_valid_until, consent_state, critical_phase, "
            "pause_reason, locked_by, pending_effect, idem_key, created_at, updated_at, version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.consultation_id,
                c.code,
                c.patient_id,
                c.clinician_id,
                c.slot_id,
                json.dumps(list(c.link_ids)),
                json.dumps(list(c.org_ids)),
                c.min_grade,
                c.required_link_count,
                c.status.value,
                c.active_link_id,
                c.backup_link_id,
                c.effective_grade,
                c.consent_valid_until,
                c.consent_state,
                c.critical_phase.value,
                c.pause_reason,
                c.locked_by,
                c.pending_effect,
                c.idem_key,
                c.created_at,
                c.updated_at,
                c.version,
            ),
        )

    def get_consultation(
        self, conn: sqlite3.Connection, consultation_id: str
    ) -> Consultation | None:
        row = conn.execute(
            "SELECT * FROM consultations WHERE consultation_id=?", (consultation_id,)
        ).fetchone()
        return _row_to_consultation(row)

    def get_consultation_by_code(
        self, conn: sqlite3.Connection, code: str
    ) -> Consultation | None:
        row = conn.execute(
            "SELECT * FROM consultations WHERE code=?", (code,)
        ).fetchone()
        return _row_to_consultation(row)

    def list_consultations(self, conn: sqlite3.Connection) -> list[Consultation]:
        rows = conn.execute(
            "SELECT * FROM consultations ORDER BY created_at, consultation_id"
        ).fetchall()
        return [_row_to_consultation(row) for row in rows]

    def persist_consultation(self, conn: sqlite3.Connection, c: Consultation) -> None:
        cur = conn.execute(
            "UPDATE consultations SET status=?, active_link_id=?, backup_link_id=?, "
            "effective_grade=?, consent_state=?, critical_phase=?, pause_reason=?, "
            "locked_by=?, pending_effect=?, updated_at=?, version=version+1 "
            "WHERE consultation_id=? AND version=?",
            (
                c.status.value,
                c.active_link_id,
                c.backup_link_id,
                c.effective_grade,
                c.consent_state,
                c.critical_phase.value,
                c.pause_reason,
                c.locked_by,
                c.pending_effect,
                c.updated_at,
                c.consultation_id,
                c.version,
            ),
        )
        if cur.rowcount != 1:
            from ..domain.errors import ConcurrentUpdate
            raise ConcurrentUpdate(
                "version_conflict",
                f"会诊 {c.consultation_id} 已被其他事务更新（期望版本 {c.version}）",
            )
        c.version += 1

    # ---- 审计 ----
    def add_audit(
        self,
        conn: sqlite3.Connection,
        *,
        consultation_id: str | None,
        event_type: str,
        actor_role: str | None,
        actor_id: str | None,
        details: dict,
        created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(consultation_id, event_type, actor_role, actor_id, "
            "details, created_at) VALUES(?,?,?,?,?,?)",
            (
                consultation_id,
                event_type,
                actor_role,
                actor_id,
                json.dumps(details, ensure_ascii=False),
                created_at,
            ),
        )

    def list_audit(
        self, conn: sqlite3.Connection, consultation_id: str | None = None
    ) -> list[sqlite3.Row]:
        if consultation_id:
            return conn.execute(
                "SELECT * FROM audit_log WHERE consultation_id=? ORDER BY id",
                (consultation_id,),
            ).fetchall()
        return conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()

    # ---- 发件箱 ----
    def add_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        event_key: str,
        event_type: str,
        aggregate_id: str,
        payload: dict,
        created_at: str,
    ) -> bool:
        """同事务写入发件箱。event_key 冲突时返回 False（幂等保护）。"""
        try:
            conn.execute(
                "INSERT INTO outbox(event_id, event_key, event_type, aggregate_id, payload, "
                "status, created_at) VALUES(?,?,?,?,?, 'pending', ?)",
                (
                    event_id,
                    event_key,
                    event_type,
                    aggregate_id,
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_outbox(self, conn: sqlite3.Connection) -> list[OutboxEvent]:
        rows = conn.execute("SELECT * FROM outbox ORDER BY rowid").fetchall()
        return [_row_to_outbox(row) for row in rows]

    def get_outbox(self, conn: sqlite3.Connection, event_id: str) -> OutboxEvent | None:
        return _row_to_outbox(
            conn.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        )
