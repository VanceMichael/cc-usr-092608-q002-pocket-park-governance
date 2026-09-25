"""口袋公园共建养护服务的 SQLite 存储层。

设计要点：

- 所有状态变更（围挡封闭、设施待修、付款审批……）与对外通知在同一事务内提交，
  通知先写入事务发件箱（notification_outbox），再由投递器异步标记送达；
  系统停机重启后，未落库的操作整体不存在，已落库的状态与待发通知都在。
- 档案类数据统一存入 record_version，按 [valid_from, valid_to) 半开生效区间版本化，
  支持“回到指定日期”的历史查询。
- 资金类记录只追加、不改写：原付款永久保留，返工、撤销、质保追偿以调整单另行登记。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 公园基础名录
CREATE TABLE IF NOT EXISTS park (
    park_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 生效区间档案（统一版本化）
-- entity_type 取值：
--   land_title            地块权属与移交
--   construction_funding  建设资金
--   zone                  园区分区
--   facility              设施资产
--   audience              适用人群
--   inspection_standard   巡检标准
--   maintenance_duty      养护责任
--   enterprise_commitment 企业共建承诺
--   opening_schedule      开放时段
CREATE TABLE IF NOT EXISTS record_version (
    id            INTEGER PRIMARY KEY,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (entity_type, entity_id, version),
    UNIQUE (entity_type, entity_id, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_record_lookup
    ON record_version(entity_type, entity_id, valid_from, valid_to);

-- 每日巡检任务（应检/逾期，责任方在生成时快照）
CREATE TABLE IF NOT EXISTS inspection_task (
    id                INTEGER PRIMARY KEY,
    due_date          TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    target_name       TEXT NOT NULL,
    category          TEXT NOT NULL,
    period_days       INTEGER NOT NULL,
    responsible_party TEXT NOT NULL,
    standard_id       INTEGER NOT NULL REFERENCES record_version(id),
    status            TEXT NOT NULL CHECK (status IN ('due', 'overdue', 'done')),
    created_at        TEXT NOT NULL,
    finished_at       TEXT,
    inspection_id     INTEGER,
    UNIQUE (due_date, target_id)
);
CREATE INDEX IF NOT EXISTS idx_task_status ON inspection_task(status, due_date);

-- 巡检现场证据
CREATE TABLE IF NOT EXISTS inspection (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES inspection_task(id),
    inspector    TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    result       TEXT NOT NULL CHECK (result IN ('normal', 'risk')),
    evidence     TEXT NOT NULL,
    finding      TEXT
);

-- 居民报修（合并处置单后，每位居民的报修仍独立留痕）
CREATE TABLE IF NOT EXISTS report (
    id             INTEGER PRIMARY KEY,
    resident_ref   TEXT NOT NULL,
    contact_handle TEXT NOT NULL,
    park_id        TEXT NOT NULL,
    facility_id    TEXT,
    category       TEXT,
    content        TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    ticket_id      INTEGER NOT NULL REFERENCES work_ticket(id)
);
CREATE INDEX IF NOT EXISTS idx_report_ticket ON report(ticket_id);
CREATE INDEX IF NOT EXISTS idx_report_park ON report(park_id);

-- 处置单（多张报修可汇入同一张）
CREATE TABLE IF NOT EXISTS work_ticket (
    id          INTEGER PRIMARY KEY,
    park_id     TEXT NOT NULL,
    facility_id TEXT,
    category    TEXT,
    risk_level  TEXT NOT NULL CHECK (risk_level IN ('normal', 'high')),
    status      TEXT NOT NULL CHECK (status IN ('open', 'merged', 'resolved')),
    merged_into INTEGER REFERENCES work_ticket(id),
    closure_id  INTEGER,
    source_inspection_id INTEGER,
    assignee    TEXT,
    assigned_at TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

-- 风险分区封闭记录（临时围挡随封闭/开放同生同灭）
CREATE TABLE IF NOT EXISTS zone_closure (
    id                     INTEGER PRIMARY KEY,
    park_id                TEXT NOT NULL,
    zone_id                TEXT NOT NULL,
    ticket_id              INTEGER NOT NULL REFERENCES work_ticket(id),
    risk_level             TEXT NOT NULL CHECK (risk_level IN ('normal', 'high')),
    reason                 TEXT NOT NULL,
    closed_by              TEXT NOT NULL,
    closed_at              TEXT NOT NULL,
    takeover_party         TEXT NOT NULL,
    barrier_kind           TEXT NOT NULL DEFAULT '临时围挡',
    barrier_installed_at   TEXT NOT NULL,
    repair_party           TEXT,
    repair_dispatched_at   TEXT,
    repaired_by            TEXT,
    repaired_at            TEXT,
    reinspected_by         TEXT,
    reinspected_at         TEXT,
    reinspection_passed    INTEGER,
    reopened_by            TEXT,
    reopened_at            TEXT,
    barrier_removed_at     TEXT,
    status                 TEXT NOT NULL
        CHECK (status IN ('closed', 'awaiting_reinspection', 'awaiting_reopen', 'reopened'))
);
CREATE INDEX IF NOT EXISTS idx_closure_park ON zone_closure(park_id, status);

-- 企业养护移交（此前责任不因此灭失，见 maintenance_duty 历史版本与各业务快照）
CREATE TABLE IF NOT EXISTS duty_handover (
    id          INTEGER PRIMARY KEY,
    park_id     TEXT NOT NULL,
    enterprise  TEXT NOT NULL,
    new_party   TEXT NOT NULL,
    handed_at   TEXT NOT NULL,
    note        TEXT
);

-- 居民关注（首次报修自动关注；封闭、重开据此广播）
CREATE TABLE IF NOT EXISTS park_subscription (
    park_id        TEXT NOT NULL,
    resident_ref   TEXT NOT NULL,
    contact_handle TEXT NOT NULL,
    subscribed_at  TEXT NOT NULL,
    PRIMARY KEY (park_id, resident_ref)
);

-- 企业承诺兑现
CREATE TABLE IF NOT EXISTS commitment_fulfillment (
    commitment_entity_id TEXT PRIMARY KEY,
    fulfilled_at         TEXT NOT NULL,
    note                 TEXT,
    evidence             TEXT
);

-- 付款审批（分段付款、调整单统一走审批；停机后状态不丢、幂等键防重复支付）
CREATE TABLE IF NOT EXISTS payment_approval (
    id                 INTEGER PRIMARY KEY,
    park_id            TEXT NOT NULL,
    enterprise         TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('installment', 'adjustment')),
    reason             TEXT NOT NULL
        CHECK (reason IN ('segment', 'rework', 'revocation', 'warranty_recovery')),
    milestone_code     TEXT,
    segment_no         INTEGER,
    amount             INTEGER NOT NULL,
    confirmed_qty      INTEGER,
    related_approval   INTEGER REFERENCES payment_approval(id),
    related_ledger_id  INTEGER REFERENCES payment_ledger(id),
    adjustment_note    TEXT,
    requested_at       TEXT NOT NULL,
    requested_by       TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_at         TEXT,
    decided_by         TEXT,
    ledger_id          INTEGER
);

-- 付款台账（只追加；signed_amount：拨款为正，追回/撤销为负）
CREATE TABLE IF NOT EXISTS payment_ledger (
    id               INTEGER PRIMARY KEY,
    park_id          TEXT NOT NULL,
    enterprise       TEXT NOT NULL,
    kind             TEXT NOT NULL,
    reason           TEXT NOT NULL,
    amount           INTEGER NOT NULL,
    signed_amount    INTEGER NOT NULL,
    milestone_code   TEXT,
    related_ledger   INTEGER REFERENCES payment_ledger(id),
    approval_id      INTEGER NOT NULL REFERENCES payment_approval(id),
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_ledger_park ON payment_ledger(park_id);

-- 事务发件箱：与业务变更同事务提交，停机不丢
CREATE TABLE IF NOT EXISTS notification_outbox (
    id            INTEGER PRIMARY KEY,
    recipient_ref TEXT NOT NULL,
    channel       TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    park_id       TEXT,
    ticket_id     INTEGER,
    report_id     INTEGER,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    delivery_ref  TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON notification_outbox(delivered_at, id);
"""


class Store:
    """封装 SQLite 连接与事务，供领域服务使用。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def txn(self) -> "Txn":
        return Txn(self.conn)

    def close(self) -> None:
        self.conn.close()


class Txn:
    """显式事务上下文：with 块内的所有改动同生共死。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def loads(raw: str) -> Any:
    return json.loads(raw)


# -- 事务发件箱投递 ----------------------------------------------------------------

NotificationSink = Callable[[dict[str, Any]], str]
"""投递通道：接收一条通知，返回投递回执号；抛异常表示未送达。"""


def deliver_pending(
    conn: sqlite3.Connection, sink: NotificationSink, now_iso: str, limit: int = 100
) -> list[int]:
    """把尚未送达的通知逐条投递并标记。

    送达与标记分两步：若标记前停机，重启后会重投一次，通知带唯一 id，
    接收方可据此去重（至少一次语义）。
    """
    rows = conn.execute(
        "SELECT * FROM notification_outbox WHERE delivered_at IS NULL "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    delivered: list[int] = []
    for row in rows:
        message = dict(row)
        receipt = sink(message)
        conn.execute(
            "UPDATE notification_outbox SET delivered_at = ?, delivery_ref = ? WHERE id = ?",
            (now_iso, receipt, row["id"]),
        )
        delivered.append(row["id"])
    return delivered
