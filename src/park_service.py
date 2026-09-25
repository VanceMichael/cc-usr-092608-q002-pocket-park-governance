"""口袋公园共建养护服务（领域层）。

覆盖以下能力：

1. 有生效区间的档案：地块权属与移交、建设资金、设施资产、适用人群、巡检标准、
   养护责任、企业共建承诺、开放时段；区间半开 [valid_from, valid_to)，支持 as-of 查询。
2. 每日算单：按检查周期生成应检项目，扫描逾期并锁定责任方；巡检人员随后提交现场证据。
3. 报修汇单：相近报修合并到同一处置单统一派单，每位报修居民仍分别收到自己的进度通知。
4. 风险分区：高风险只封闭受影响区域；维修完成后必须由“另一名人员”复检通过；
   重新开放不得早于风险解除（复检通过）的时刻。
5. 共建资金：随确认工程量分段支付；返工、撤销、质保追偿不抹掉原付款，另立调整单；
   企业移交养护后，此前承诺与质保责任不随之消失。
6. 一致性：围挡、待修设施、付款审批都落库在同一事务里，跨系统停机保持一致。
7. 街道视图：当前可用区域、下一次巡检、未兑现承诺、资金占用；
   争议时可回到指定日期，复原当时封闭原因、接管方与居民已收到的通知。

所有时间参数由调用方显式传入（ISO 8601 字符串），使服务确定可测、不依赖系统时钟。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .park_store import Store, Txn, dumps, loads

# -- 常量 --------------------------------------------------------------------------

ENTITY_TYPES = frozenset(
    {
        "land_title",  # 地块权属与移交
        "construction_funding",  # 建设资金
        "zone",  # 园区分区
        "facility",  # 设施资产（饮水机/医药箱/儿童设施/照明/智慧设备……）
        "audience",  # 适用人群
        "inspection_standard",  # 巡检标准
        "maintenance_duty",  # 养护责任
        "enterprise_commitment",  # 企业共建承诺
        "opening_schedule",  # 开放时段
    }
)

# 有差异化检查周期的设施类别
CATEGORY_CHILDREN = "children"  # 儿童设施
CATEGORY_BARRIER_FREE = "barrier_free"  # 无障碍通道
CATEGORY_LIGHTING = "lighting"  # 夜间照明


class DomainError(ValueError):
    """业务规则被违反。"""


# -- 工具 --------------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex


def _one(conn, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _require(value: Any, message: str) -> Any:
    if value is None:
        raise DomainError(message)
    return value


# -- 服务 --------------------------------------------------------------------------

class ParkService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ===== 公园与档案 ==========================================================

    def register_park(self, park_id: str, name: str, now: str) -> None:
        with self.store.txn() as conn:
            conn.execute(
                "INSERT INTO park(park_id, name, created_at) VALUES (?,?,?)",
                (park_id, name, now),
            )

    def put_record(
        self,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
        valid_from: str,
    ) -> int:
        """登记一版档案。

        若该实体在 valid_from 当日已有生效版本，则视为同日更正（覆盖该版）；
        否则新开一版，并把上一版的 valid_to 收口为 valid_from。版本号只增不减。
        """
        if entity_type not in ENTITY_TYPES:
            raise DomainError(f"未知档案类型: {entity_type}")
        if not isinstance(payload, dict) or not payload:
            raise DomainError("档案内容不能为空")
        with self.store.txn() as conn:
            current = _one(
                conn,
                "SELECT * FROM record_version WHERE entity_type=? AND entity_id=? "
                "AND valid_from=?",
                (entity_type, entity_id, valid_from),
            )
            if current is not None:
                conn.execute(
                    "UPDATE record_version SET payload=?, created_at=? WHERE id=?",
                    (dumps(payload), valid_from, current["id"]),
                )
                return int(current["id"])
            previous = _one(
                conn,
                "SELECT * FROM record_version WHERE entity_type=? AND entity_id=? "
                "AND valid_to IS NULL ORDER BY version DESC",
                (entity_type, entity_id),
            )
            version = 1 if previous is None else int(previous["version"]) + 1
            if previous is not None and previous["valid_from"] >= valid_from:
                raise DomainError("新生效日期不得早于现行版本")
            if previous is not None:
                conn.execute(
                    "UPDATE record_version SET valid_to=? WHERE id=?",
                    (valid_from, previous["id"]),
                )
            cur = conn.execute(
                "INSERT INTO record_version(entity_type, entity_id, version, valid_from, "
                "valid_to, payload, created_at) VALUES (?,?,?,?,NULL,?,?)",
                (entity_type, entity_id, version, valid_from, dumps(payload), valid_from),
            )
            return int(cur.lastrowid)

    def get_record(
        self, entity_type: str, entity_id: str, on_date: str | None = None
    ) -> dict[str, Any] | None:
        """取现行版本（on_date=None）或指定日期当日生效的版本。"""
        with self.store.txn() as conn:
            if on_date is None:
                row = _one(
                    conn,
                    "SELECT * FROM record_version WHERE entity_type=? AND entity_id=? "
                    "AND valid_to IS NULL",
                    (entity_type, entity_id),
                )
            else:
                row = _one(
                    conn,
                    "SELECT * FROM record_version WHERE entity_type=? AND entity_id=? "
                    "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
                    (entity_type, entity_id, on_date, on_date),
                )
            if row is None:
                return None
            result = dict(row)
            result["payload"] = loads(result["payload"])
            return result

    def list_records(
        self, entity_type: str, park_id: str, on_date: str | None = None
    ) -> list[dict[str, Any]]:
        """列出某公园在某日生效的某类档案（payload 中以 park_id 归属）。"""
        with self.store.txn() as conn:
            rows = conn.execute(
                "SELECT * FROM record_version WHERE entity_type=?",
                (entity_type,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for raw in rows:
            item = dict(raw)
            payload = loads(item["payload"])
            if payload.get("park_id") != park_id:
                continue
            if on_date is not None and not (
                item["valid_from"] <= on_date
                and (item["valid_to"] is None or item["valid_to"] > on_date)
            ):
                continue
            item["payload"] = payload
            out.append(item)
        return out

    # ===== 每日算单与巡检 ======================================================

    def generate_daily_tasks(self, today: str) -> dict[str, list[int]]:
        """每天先算出应检项目与逾期责任。

        - 巡检标准（inspection_standard）按设施类别或具体设施给出检查周期；
        - 应检：上次完成日 + 周期 <= 今日，或从未检过且标准已生效；
        - 逾期：已生成的 due 任务到期日早于今日，标记 overdue；
        - 责任方取该设施当前养护责任版本，在生成任务时快照（日后移交不改旧账）。
        返回 {"created": 任务id列表, "marked_overdue": 任务id列表}。
        """
        created: list[int] = []
        with self.store.txn() as conn:
            # 1) 既有任务逾期
            overdue_rows = conn.execute(
                "SELECT id FROM inspection_task WHERE status IN ('due','overdue') "
                "AND due_date < ?",
                (today,),
            ).fetchall()
            for row in overdue_rows:
                conn.execute(
                    "UPDATE inspection_task SET status='overdue' WHERE id=?",
                    (row["id"],),
                )
            marked_overdue = [int(r["id"]) for r in overdue_rows]

            # 2) 按现行设施与其巡检标准生成应检（含停机漏跑的补算）
            facilities = conn.execute(
                "SELECT * FROM record_version WHERE entity_type='facility' AND valid_to IS NULL"
            ).fetchall()
            for fac in facilities:
                payload = loads(fac["payload"])
                target_id = fac["entity_id"]
                standard = self._standard_for(conn, payload, target_id, today)
                if standard is None:
                    continue
                period_days = int(standard["payload"]["period_days"])
                if period_days <= 0:
                    raise DomainError("巡检周期必须为正整数天")
                latest = _one(
                    conn,
                    "SELECT MAX(due_date) AS due_date FROM inspection_task WHERE target_id=?",
                    (target_id,),
                )
                if latest is not None and latest["due_date"] is not None:
                    due_date = _shift_day(latest["due_date"], period_days)
                else:
                    due_date = today  # 首次应检
                # 一次补算到今日为止的全部漏跑周期（停机多日也不丢检）
                while due_date <= today:
                    exists = _one(
                        conn,
                        "SELECT id FROM inspection_task WHERE target_id=? AND due_date=?",
                        (target_id, due_date),
                    )
                    if exists is None:
                        # 责任方按“应检当日”生效的责任版本解析，移交不影响历史应检责任
                        duty = self._duty_for(conn, payload["park_id"], target_id, due_date)
                        responsible = (
                            duty["payload"]["responsible_party"]
                            if duty
                            else standard["payload"].get("default_party", "园林养护单位")
                        )
                        cur = conn.execute(
                            "INSERT INTO inspection_task(due_date, target_id, target_name, category, "
                            "period_days, responsible_party, standard_id, status, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                due_date,
                                target_id,
                                payload.get("name", target_id),
                                payload.get("category", "general"),
                                period_days,
                                responsible,
                                standard["id"],
                                "overdue" if due_date < today else "due",
                                today,
                            ),
                        )
                        created.append(int(cur.lastrowid))
                    due_date = _shift_day(due_date, period_days)
        return {"created": created, "marked_overdue": marked_overdue}

    @staticmethod
    def _standard_for(conn, facility_payload: dict, target_id: str, on_date: str):
        """取生效标准：优先设施专用（scope=target），其次类别通用（scope=category）。"""
        rows = conn.execute(
            "SELECT * FROM record_version WHERE entity_type='inspection_standard' "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
            (on_date, on_date),
        ).fetchall()
        candidates = []
        for raw in rows:
            item = dict(raw)
            item["payload"] = loads(item["payload"])
            scope = item["payload"].get("scope")
            if scope == "target" and item["payload"].get("target_id") == target_id:
                candidates.append((0, item))
            elif scope == "category" and item["payload"].get(
                "category"
            ) == facility_payload.get("category"):
                candidates.append((1, item))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]

    @staticmethod
    def _duty_for(conn, park_id: str, target_id: str, on_date: str):
        rows = conn.execute(
            "SELECT * FROM record_version WHERE entity_type='maintenance_duty' "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
            (on_date, on_date),
        ).fetchall()
        for raw in rows:
            item = dict(raw)
            item["payload"] = loads(item["payload"])
            if item["payload"].get("park_id") != park_id:
                continue
            scopes = item["payload"].get("facility_ids") or []
            categories = item["payload"].get("categories") or []
            fac = _one(
                conn,
                "SELECT payload FROM record_version WHERE entity_type='facility' "
                "AND entity_id=? AND valid_to IS NULL",
                (target_id,),
            )
            category = loads(fac["payload"]).get("category") if fac else None
            if target_id in scopes or (categories and category in categories):
                return item
        # 园区兜底责任
        for raw in rows:
            item = dict(raw)
            item["payload"] = loads(item["payload"])
            if item["payload"].get("park_id") == park_id and not item["payload"].get(
                "facility_ids"
            ) and not item["payload"].get("categories"):
                return item
        return None

    def submit_inspection(
        self,
        task_id: int,
        inspector: str,
        result: str,
        evidence: str,
        submitted_at: str,
        finding: str | None = None,
    ) -> int:
        """巡检人员提交现场证据。

        正常时返回巡检记录 id；发现风险时自动挂起一张高风险处置单并返回处置单 id
        （封闭分区与接管方随后由管理人员确认）。
        """
        if result not in ("normal", "risk"):
            raise DomainError("巡检结论非法")
        if not evidence.strip():
            raise DomainError("现场证据不能为空")
        with self.store.txn() as conn:
            task = _require(
                _one(conn, "SELECT * FROM inspection_task WHERE id=?", (task_id,)),
                "巡检任务不存在",
            )
            if task["status"] == "done":
                raise DomainError("该任务已完成巡检")
            cur = conn.execute(
                "INSERT INTO inspection(task_id, inspector, submitted_at, result, evidence, finding) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, inspector, submitted_at, result, evidence, finding),
            )
            inspection_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE inspection_task SET status='done', finished_at=?, inspection_id=? WHERE id=?",
                (submitted_at, inspection_id, task_id),
            )
            ticket_id: int | None = None
            if result == "risk":
                # 发现风险即挂起高风险处置单；是否封闭、围哪个分区由管理人员确认。
                fac = _one(
                    conn,
                    "SELECT payload FROM record_version WHERE entity_type='facility' "
                    "AND entity_id=? AND valid_to IS NULL",
                    (task["target_id"],),
                )
                park_id = loads(fac["payload"])["park_id"] if fac else None
                tcur = conn.execute(
                    "INSERT INTO work_ticket(park_id, facility_id, category, risk_level, status, "
                    "source_inspection_id, created_at) VALUES (?,?,?, 'high','open',?,?)",
                    (park_id, task["target_id"], task["category"], inspection_id, submitted_at),
                )
                ticket_id = int(tcur.lastrowid)
            return inspection_id if ticket_id is None else ticket_id

    def next_inspection(self, park_id: str, today: str) -> dict[str, Any] | None:
        """街道看板：下一次巡检（取本公园未完成任务中最早到期者）。"""
        with self.store.txn() as conn:
            row = _one(
                conn,
                "SELECT t.* FROM inspection_task t "
                "JOIN record_version rv ON rv.entity_id=t.target_id "
                "AND rv.entity_type='facility' AND rv.valid_to IS NULL "
                "WHERE t.status IN ('due','overdue') "
                "AND json_extract(rv.payload, '$.park_id')=? "
                "ORDER BY CASE t.status WHEN 'overdue' THEN 0 ELSE 1 END, t.due_date LIMIT 1",
                (park_id,),
            )
            if row is not None:
                return dict(row)
            # 无待办任务时，按周期预测下一应检日
            return self._predict_next(conn, park_id, today)

    def _predict_next(self, conn, park_id: str, today: str) -> dict[str, Any] | None:
        rows = conn.execute(
            "SELECT * FROM record_version WHERE entity_type='facility' AND valid_to IS NULL"
        ).fetchall()
        best: dict[str, Any] | None = None
        for fac in rows:
            payload = loads(fac["payload"])
            if payload.get("park_id") != park_id:
                continue
            standard = self._standard_for(conn, payload, fac["entity_id"], today)
            if standard is None:
                continue
            period = int(standard["payload"]["period_days"])
            latest = _one(
                conn,
                "SELECT MAX(due_date) AS d FROM inspection_task WHERE target_id=?",
                (fac["entity_id"],),
            )
            if latest is not None and latest["d"] is not None:
                due = _shift_day(latest["d"], period)
            else:
                due = _shift_day(today, period)
            if best is None or due < best["due_date"]:
                best = {
                    "due_date": due,
                    "target_id": fac["entity_id"],
                    "target_name": payload.get("name", fac["entity_id"]),
                    "category": payload.get("category", "general"),
                    "status": "scheduled",
                    "responsible_party": self._duty_for(conn, park_id, fac["entity_id"], today),
                }
                if isinstance(best["responsible_party"], dict):
                    best["responsible_party"] = best["responsible_party"]["payload"][
                        "responsible_party"
                    ]
        return best

    # ===== 报修与处置单 ========================================================

    def submit_report(
        self,
        resident_ref: str,
        contact_handle: str,
        park_id: str,
        content: str,
        received_at: str,
        facility_id: str | None = None,
        category: str | None = None,
        similarity_window_days: int = 7,
    ) -> dict[str, int]:
        """居民报修；相近（同园、同设施/类别、时间窗内、未结）报修汇入同一处置单。

        每位居民在受理、并单、派单、结案等节点都收到属于自己的通知。
        返回 {"report_id", "ticket_id", "merged": 0/1}。
        """
        with self.store.txn() as conn:
            ticket = self._find_similar_ticket(
                conn, park_id, facility_id, category, received_at, similarity_window_days
            )
            if ticket is None:
                cur = conn.execute(
                    "INSERT INTO work_ticket(park_id, facility_id, category, risk_level, status, created_at) "
                    "VALUES (?,?,?, 'normal','open',?)",
                    (park_id, facility_id, category, received_at),
                )
                ticket_id = int(cur.lastrowid)
                merged = 0
            else:
                ticket_id = int(ticket["id"])
                merged = 1
            cur = conn.execute(
                "INSERT INTO report(resident_ref, contact_handle, park_id, facility_id, category, "
                "content, received_at, ticket_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    resident_ref,
                    contact_handle,
                    park_id,
                    facility_id,
                    category,
                    content,
                    received_at,
                    ticket_id,
                ),
            )
            report_id = int(cur.lastrowid)
            conn.execute(
                "INSERT OR IGNORE INTO park_subscription(park_id, resident_ref, contact_handle, subscribed_at) "
                "VALUES (?,?,?,?)",
                (park_id, resident_ref, contact_handle, received_at),
            )
            self._notify(
                conn,
                resident_ref,
                contact_handle,
                "报修已受理" if not merged else "报修已并入处置单",
                f"您反映的“{content}”已登记，处置单号 #{ticket_id}，"
                + ("与相近报修合并处理。" if merged else "已分派核查。"),
                "report_received" if not merged else "report_merged",
                park_id,
                ticket_id,
                report_id,
                received_at,
            )
            return {"report_id": report_id, "ticket_id": ticket_id, "merged": merged}

    @staticmethod
    def _find_similar_ticket(
        conn, park_id, facility_id, category, received_at, window_days
    ):
        """在时间窗内找同园未结处置单：同设施优先，其次同类别。"""
        window_start = _shift_day(received_at[:10], -window_days)
        if facility_id is not None:
            row = _one(
                conn,
                "SELECT * FROM work_ticket WHERE park_id=? AND status='open' "
                "AND facility_id=? AND created_at >= ? ORDER BY id LIMIT 1",
                (park_id, facility_id, window_start),
            )
            if row is not None:
                return row
        if category is not None:
            return _one(
                conn,
                "SELECT * FROM work_ticket WHERE park_id=? AND status='open' "
                "AND category=? AND created_at >= ? ORDER BY id LIMIT 1",
                (park_id, category, window_start),
            )
        return None

    def merge_tickets(self, source_ticket_id: int, target_ticket_id: int, now: str) -> int:
        """管理人员把两张已分立的相近处置单合并：源单挂到目标单，双方报修人都收到通知。

        源单上每位居民的报修仍独立留痕，只是统一看目标单进度；返回目标单 id。
        """
        if source_ticket_id == target_ticket_id:
            raise DomainError("处置单不能与自身合并")
        with self.store.txn() as conn:
            source = _require(
                _one(conn, "SELECT * FROM work_ticket WHERE id=?", (source_ticket_id,)),
                "源处置单不存在",
            )
            target = _require(
                _one(conn, "SELECT * FROM work_ticket WHERE id=?", (target_ticket_id,)),
                "目标处置单不存在",
            )
            if source["park_id"] != target["park_id"]:
                raise DomainError("不同公园的处置单不能合并")
            if source["status"] == "resolved" or target["status"] == "resolved":
                raise DomainError("已结案处置单不能合并")
            if target["status"] == "merged":
                raise DomainError("目标处置单本身已被合并")
            conn.execute(
                "UPDATE work_ticket SET status='merged', merged_into=? WHERE id=?",
                (target_ticket_id, source_ticket_id),
            )
            conn.execute(
                "UPDATE report SET ticket_id=? WHERE ticket_id=?",
                (target_ticket_id, source_ticket_id),
            )
            # 若源单已触发封闭，则封闭与高风险标记归并到目标单
            if source["closure_id"] is not None:
                conn.execute(
                    "UPDATE zone_closure SET ticket_id=? WHERE id=?",
                    (target_ticket_id, source["closure_id"]),
                )
                conn.execute(
                    "UPDATE work_ticket SET risk_level='high', "
                    "closure_id=COALESCE(closure_id,?) WHERE id=?",
                    (source["closure_id"], target_ticket_id),
                )
            self._notify_reporters(
                conn,
                target_ticket_id,
                "处置单已合并",
                f"相近报修已统一并入处置单 #{target_ticket_id} 处理，您仍会收到本单全部进度。",
                "ticket_merged",
                now,
            )
            return target_ticket_id

    def assign_ticket(self, ticket_id: int, assignee: str, now: str) -> None:
        """派单：明确由谁接单，通知该单上的每一位报修居民。"""
        with self.store.txn() as conn:
            ticket = self._open_root(conn, ticket_id)
            conn.execute(
                "UPDATE work_ticket SET assignee=?, assigned_at=? WHERE id=?",
                (assignee, now, ticket["id"]),
            )
            self._notify_reporters(
                conn,
                ticket["id"],
                "处置单已派单",
                f"处置单 #{ticket['id']} 已由 {assignee} 接单处理。",
                "ticket_assigned",
                now,
            )

    def resolve_ticket(self, ticket_id: int, now: str, note: str = "") -> None:
        """普通处置单结案（高风险须走复检重开流程）。"""
        with self.store.txn() as conn:
            ticket = self._open_root(conn, ticket_id)
            if ticket["risk_level"] == "high":
                raise DomainError("高风险处置单须经复检通过并重新开放后结案")
            conn.execute(
                "UPDATE work_ticket SET status='resolved', resolved_at=? WHERE id=?",
                (now, ticket["id"]),
            )
            self._notify_reporters(
                conn,
                ticket["id"],
                "处置已完成",
                f"处置单 #{ticket['id']} 已处理完成。{note}",
                "ticket_resolved",
                now,
            )

    def resident_progress(self, resident_ref: str) -> list[dict[str, Any]]:
        """居民查询自己的每张报修及其处置单进度（各收各的进度）。"""
        with self.store.txn() as conn:
            rows = conn.execute(
                "SELECT r.id AS report_id, r.content, r.received_at, t.id AS ticket_id, "
                "t.status AS ticket_status, t.risk_level, t.assignee, t.resolved_at "
                "FROM report r JOIN work_ticket t ON t.id = r.ticket_id "
                "WHERE r.resident_ref=? ORDER BY r.id",
                (resident_ref,),
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _open_root(conn, ticket_id: int):
        ticket = _require(
            _one(conn, "SELECT * FROM work_ticket WHERE id=?", (ticket_id,)),
            "处置单不存在",
        )
        if ticket["status"] == "merged":
            ticket = _one(conn, "SELECT * FROM work_ticket WHERE id=?", (ticket["merged_into"],))
        if ticket["status"] == "resolved":
            raise DomainError("处置单已结案")
        return ticket

    @staticmethod
    def _notify_reporters(conn, ticket_id, subject, body, event_type, now):
        rows = conn.execute(
            "SELECT DISTINCT resident_ref, contact_handle FROM report WHERE ticket_id=?",
            (ticket_id,),
        ).fetchall()
        for row in rows:
            ParkService._notify(
                conn,
                row["resident_ref"],
                row["contact_handle"],
                subject,
                body,
                event_type,
                None,
                ticket_id,
                None,
                now,
            )

    @staticmethod
    def _notify(
        conn,
        recipient_ref,
        contact_handle,
        subject,
        body,
        event_type,
        park_id,
        ticket_id,
        report_id,
        now,
    ):
        conn.execute(
            "INSERT INTO notification_outbox(recipient_ref, channel, subject, body, event_type, "
            "park_id, ticket_id, report_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                recipient_ref,
                "resident:" + contact_handle,
                subject,
                body,
                event_type,
                park_id,
                ticket_id,
                report_id,
                now,
            ),
        )

    # ===== 风险、分区封闭与复检重开 ============================================

    def escalate_risk(
        self,
        ticket_id: int,
        zone_id: str,
        reason: str,
        closed_by: str,
        takeover_party: str,
        closed_at: str,
        repair_party: str | None = None,
        inspection_id: int | None = None,
    ) -> int:
        """发现高风险：只关闭受影响区域，立起临时围挡，通知关注居民与全部报修人。"""
        with self.store.txn() as conn:
            ticket = self._open_root(conn, ticket_id)
            park_id = ticket["park_id"]
            # 同一分区已有未解除封闭时拒绝重复围挡
            active = _one(
                conn,
                "SELECT id FROM zone_closure WHERE park_id=? AND zone_id=? AND status!='reopened'",
                (park_id, zone_id),
            )
            if active is not None:
                raise DomainError("该区域已有未解除的封闭")
            cur = conn.execute(
                "INSERT INTO zone_closure(park_id, zone_id, ticket_id, risk_level, reason, "
                "closed_by, closed_at, takeover_party, barrier_installed_at, repair_party, "
                "repair_dispatched_at, status) VALUES (?,?,?, 'high',?,?,?,?,?,?,?, 'closed')",
                (
                    park_id,
                    zone_id,
                    ticket["id"],
                    reason,
                    closed_by,
                    closed_at,
                    takeover_party,
                    closed_at,
                    repair_party,
                    closed_at if repair_party else None,
                ),
            )
            closure_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE work_ticket SET risk_level='high', closure_id=?, "
                "assignee=COALESCE(assignee,?), assigned_at=COALESCE(assigned_at,?) "
                "WHERE id=?",
                (closure_id, repair_party or takeover_party, closed_at, ticket["id"]),
            )
            if inspection_id is not None:
                conn.execute(
                    "UPDATE work_ticket SET source_inspection_id=? WHERE id=?",
                    (inspection_id, ticket["id"]),
                )
            body = (
                f"因{reason}，本公园 {zone_id} 区域临时封闭（仅封闭受影响区域），"
                f"由{takeover_party}接管处置，其他区域可正常使用。"
            )
            self._broadcast_park(conn, park_id, "区域临时封闭通知", body, "zone_closed", ticket["id"], closed_at)
            return closure_id

    def complete_repair(
        self, closure_id: int, repaired_by: str, repaired_at: str, repair_note: str = ""
    ) -> None:
        """维修方完成作业，记录待复检；维修人不得复检自己的活。"""
        with self.store.txn() as conn:
            closure = self._get_active_closure(conn, closure_id)
            conn.execute(
                "UPDATE zone_closure SET status='awaiting_reinspection', repaired_by=?, "
                "repaired_at=?, repair_party=COALESCE(repair_party,?) WHERE id=?",
                (repaired_by, repaired_at, repaired_by, closure_id),
            )
            self._broadcast_park(
                conn,
                closure["park_id"],
                "维修完成待复检",
                f"{closure['zone_id']} 区域维修已完成，正在安排独立复检，暂未开放。",
                "repair_completed",
                closure["ticket_id"],
                repaired_at,
            )

    def pass_reinspection(
        self, closure_id: int, inspector: str, passed: bool, inspected_at: str, note: str = ""
    ) -> None:
        """另一名人员复检：不通过则退回维修；通过则风险解除，但不早于此刻开放。"""
        with self.store.txn() as conn:
            closure = self._get_active_closure(conn, closure_id)
            if closure["status"] != "awaiting_reinspection":
                raise DomainError("当前状态不可复检")
            if inspector == closure["repaired_by"]:
                raise DomainError("复检人员不得与维修人员为同一人")
            if not passed:
                conn.execute(
                    "UPDATE zone_closure SET status='closed', reinspected_by=?, "
                    "reinspected_at=?, reinspection_passed=0 WHERE id=?",
                    (inspector, inspected_at, closure_id),
                )
                self._broadcast_park(
                    conn,
                    closure["park_id"],
                    "复检未通过",
                    f"{closure['zone_id']} 区域复检未通过，继续封闭返工。{note}",
                    "reinspection_failed",
                    closure["ticket_id"],
                    inspected_at,
                )
                return
            conn.execute(
                "UPDATE zone_closure SET status='awaiting_reopen', reinspected_by=?, "
                "reinspected_at=?, reinspection_passed=1 WHERE id=?",
                (inspector, inspected_at, closure_id),
            )
            self._broadcast_park(
                conn,
                closure["park_id"],
                "复检通过",
                f"{closure['zone_id']} 区域风险已解除，将按程序重新开放。",
                "reinspection_passed",
                closure["ticket_id"],
                inspected_at,
            )

    def reopen_zone(self, closure_id: int, reopened_by: str, reopened_at: str) -> None:
        """重新开放：不得早于风险解除（复检通过）时刻；同时撤围挡、结工单。"""
        with self.store.txn() as conn:
            closure = self._get_active_closure(conn, closure_id)
            if closure["status"] != "awaiting_reopen":
                raise DomainError("尚未复检通过，不能开放")
            if reopened_at < closure["reinspected_at"]:
                raise DomainError("重新开放不得早于风险解除时刻")
            conn.execute(
                "UPDATE zone_closure SET status='reopened', reopened_by=?, reopened_at=?, "
                "barrier_removed_at=? WHERE id=?",
                (reopened_by, reopened_at, reopened_at, closure_id),
            )
            conn.execute(
                "UPDATE work_ticket SET status='resolved', resolved_at=? WHERE id=?",
                (reopened_at, closure["ticket_id"]),
            )
            self._broadcast_park(
                conn,
                closure["park_id"],
                "区域重新开放",
                f"{closure['zone_id']} 区域已重新开放，围挡已撤除，欢迎使用。",
                "zone_reopened",
                closure["ticket_id"],
                reopened_at,
            )

    @staticmethod
    def _get_active_closure(conn, closure_id: int):
        closure = _require(
            _one(conn, "SELECT * FROM zone_closure WHERE id=?", (closure_id,)),
            "封闭记录不存在",
        )
        if closure["status"] == "reopened":
            raise DomainError("该封闭已解除")
        return closure

    @staticmethod
    def _broadcast_park(conn, park_id, subject, body, event_type, ticket_id, now):
        rows = conn.execute(
            "SELECT DISTINCT resident_ref, contact_handle FROM park_subscription WHERE park_id=?",
            (park_id,),
        ).fetchall()
        # 报修但未订阅的极端情况不存在（报修即订阅）；这里再补工单报修人
        extra = conn.execute(
            "SELECT DISTINCT resident_ref, contact_handle FROM report WHERE ticket_id=?",
            (ticket_id,),
        ).fetchall()
        seen = {r["resident_ref"] for r in rows}
        for row in list(rows) + [r for r in extra if r["resident_ref"] not in seen]:
            ParkService._notify(
                conn,
                row["resident_ref"],
                row["contact_handle"],
                subject,
                body,
                event_type,
                park_id,
                ticket_id,
                None,
                now,
            )

    # ===== 企业承诺与养护移交 ==================================================

    def fulfill_commitment(self, commitment_entity_id: str, fulfilled_at: str, note: str = "") -> None:
        with self.store.txn() as conn:
            exists = _one(
                conn,
                "SELECT id FROM record_version WHERE entity_type='enterprise_commitment' "
                "AND entity_id=?",
                (commitment_entity_id,),
            )
            _require(exists, "承诺不存在")
            conn.execute(
                "INSERT OR REPLACE INTO commitment_fulfillment(commitment_entity_id, fulfilled_at, note) "
                "VALUES (?,?,?)",
                (commitment_entity_id, fulfilled_at, note),
            )

    def handover_maintenance(
        self,
        park_id: str,
        enterprise: str,
        new_party: str,
        handed_at: str,
        note: str = "",
    ) -> None:
        """企业移交养护：登记移交，并新开养护责任版本；此前承诺与质保责任不消灭。

        历史 inspection_task 的 responsible_party、封闭记录 takeover_party、
        付款台账均为快照/只追加记录，移交不回溯改写。
        """
        entity_id = f"duty-{park_id}"
        with self.store.txn() as conn:
            conn.execute(
                "INSERT INTO duty_handover(park_id, enterprise, new_party, handed_at, note) "
                "VALUES (?,?,?,?,?)",
                (park_id, enterprise, new_party, handed_at, note),
            )
            previous = _one(
                conn,
                "SELECT * FROM record_version WHERE entity_type='maintenance_duty' "
                "AND entity_id=? AND valid_to IS NULL ORDER BY version DESC",
                (entity_id,),
            )
            if previous is not None:
                conn.execute(
                    "UPDATE record_version SET valid_to=? WHERE id=?",
                    (handed_at, previous["id"]),
                )
                version = int(previous["version"]) + 1
            else:
                version = 1
            conn.execute(
                "INSERT INTO record_version(entity_type, entity_id, version, valid_from, valid_to, "
                "payload, created_at) VALUES ('maintenance_duty', ?, ?, ?, NULL, ?, ?)",
                (
                    entity_id,
                    version,
                    handed_at,
                    dumps(
                        {
                            "park_id": park_id,
                            "responsible_party": new_party,
                            "transferred_from": enterprise,
                            "note": "养护移交后生效；移交前责任不免除",
                        }
                    ),
                    handed_at,
                ),
            )

    # ===== 共建资金：分段支付与调整 ============================================

    def request_installment(
        self,
        park_id: str,
        enterprise: str,
        segment_no: int,
        milestone_code: str,
        amount: int,
        confirmed_qty: int,
        requested_by: str,
        requested_at: str,
    ) -> int:
        """共建预算随确认工程量分段支付：发起一段付款审批。"""
        if amount <= 0:
            raise DomainError("分段付款金额必须为正")
        if confirmed_qty < 0:
            raise DomainError("确认工程量不能为负")
        key = f"pay:{park_id}:seg{segment_no}:{milestone_code}"
        with self.store.txn() as conn:
            dup = _one(
                conn,
                "SELECT id FROM payment_approval WHERE idempotency_key=?",
                (key,),
            )
            if dup is not None:
                raise DomainError("该分段付款已发起，不得重复支付")
            cur = conn.execute(
                "INSERT INTO payment_approval(park_id, enterprise, kind, reason, milestone_code, "
                "segment_no, amount, confirmed_qty, requested_at, requested_by, idempotency_key, status) "
                "VALUES (?,?, 'installment','segment',?,?,?,?,?,?,?, 'pending')",
                (
                    park_id,
                    enterprise,
                    milestone_code,
                    segment_no,
                    amount,
                    confirmed_qty,
                    requested_at,
                    requested_by,
                    key,
                ),
            )
            return int(cur.lastrowid)

    def decide_approval(
        self, approval_id: int, approve: bool, decided_by: str, decided_at: str
    ) -> int | None:
        """付款审批；批准即按只追加方式入台账。返回台账 id（驳回为 None）。"""
        with self.store.txn() as conn:
            approval = _require(
                _one(conn, "SELECT * FROM payment_approval WHERE id=?", (approval_id,)),
                "付款审批不存在",
            )
            if approval["status"] != "pending":
                raise DomainError("该审批已决断")
            if not approve:
                conn.execute(
                    "UPDATE payment_approval SET status='rejected', decided_at=?, decided_by=? WHERE id=?",
                    (decided_at, decided_by, approval_id),
                )
                return None
            cur = conn.execute(
                "INSERT INTO payment_ledger(park_id, enterprise, kind, reason, amount, signed_amount, "
                "milestone_code, related_ledger, approval_id, created_at, created_by, idempotency_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    approval["park_id"],
                    approval["enterprise"],
                    approval["kind"],
                    approval["reason"],
                    approval["amount"],
                    approval["amount"] if approval["reason"] == "segment" else -approval["amount"],
                    approval["milestone_code"],
                    approval["related_ledger_id"],
                    approval_id,
                    decided_at,
                    decided_by,
                    f"ledger:{approval['idempotency_key']}",
                ),
            )
            ledger_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE payment_approval SET status='approved', decided_at=?, decided_by=?, ledger_id=? "
                "WHERE id=?",
                (decided_at, decided_by, ledger_id, approval_id),
            )
            return ledger_id

    def request_adjustment(
        self,
        park_id: str,
        enterprise: str,
        reason: str,
        amount: int,
        requested_by: str,
        requested_at: str,
        related_ledger_id: int,
        note: str = "",
    ) -> int:
        """返工、撤销、质保追偿：原付款保留，另行立调整单（金额登记为正向数额，台账记负）。

        related_ledger_id 指向被调整的原付款——原台账行不删不改。
        即使企业已移交养护，质保追偿仍向原企业发起（付款与移交解耦）。
        """
        if reason not in ("rework", "revocation", "warranty_recovery"):
            raise DomainError("调整原因非法")
        if amount <= 0:
            raise DomainError("调整金额必须为正")
        with self.store.txn() as conn:
            original = _one(
                conn, "SELECT * FROM payment_ledger WHERE id=?", (related_ledger_id,)
            )
            _require(original, "被调整的原付款不存在")
            if original["park_id"] != park_id:
                raise DomainError("调整单与原付款不属于同一公园")
            count_row = _one(
                conn,
                "SELECT COUNT(*) AS c FROM payment_ledger WHERE related_ledger=? AND reason=?",
                (related_ledger_id, reason),
            )
            key = f"adj:{park_id}:{related_ledger_id}:{reason}:{count_row['c'] + 1}"
            related_approval = original["approval_id"]
            cur = conn.execute(
                "INSERT INTO payment_approval(park_id, enterprise, kind, reason, amount, "
                "related_approval, related_ledger_id, adjustment_note, requested_at, requested_by, "
                "idempotency_key, status) "
                "VALUES (?,?, 'adjustment',?,?,?,?,?,?,?,?, 'pending')",
                (
                    park_id,
                    enterprise,
                    reason,
                    amount,
                    related_approval,
                    related_ledger_id,
                    note,
                    requested_at,
                    requested_by,
                    key,
                ),
            )
            return int(cur.lastrowid)

    def funds_snapshot(self, park_id: str) -> dict[str, Any]:
        """街道看板：资金占用 = 已批拨款净额 + 在途审批占用。"""
        with self.store.txn() as conn:
            paid_row = _one(
                conn,
                "SELECT COALESCE(SUM(signed_amount),0) AS net FROM payment_ledger WHERE park_id=?",
                (park_id,),
            )
            pending_row = _one(
                conn,
                "SELECT COALESCE(SUM(amount),0) AS pending_reserved FROM payment_approval "
                "WHERE park_id=? AND status='pending' AND kind='installment'",
                (park_id,),
            )
            adjustment_pending = _one(
                conn,
                "SELECT COALESCE(SUM(amount),0) AS pending_recovery FROM payment_approval "
                "WHERE park_id=? AND status='pending' AND kind='adjustment'",
                (park_id,),
            )
            ledger = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM payment_ledger WHERE park_id=? ORDER BY id", (park_id,)
                ).fetchall()
            ]
            return {
                "net_paid": int(paid_row["net"]),
                "pending_installment_reserved": int(pending_row["pending_reserved"]),
                "pending_adjustment_recovery": int(adjustment_pending["pending_recovery"]),
                "occupied": int(paid_row["net"]) + int(pending_row["pending_reserved"]),
                "ledger": ledger,
            }

    # ===== 街道看板与历史回溯 ==================================================

    def park_dashboard(self, park_id: str, today: str) -> dict[str, Any]:
        """街道一眼看全：当前可用区域、下一次巡检、未兑现承诺、资金占用。"""
        with self.store.txn() as conn:
            zones = [
                loads(r["payload"])
                for r in conn.execute(
                    "SELECT payload FROM record_version WHERE entity_type='zone' AND valid_to IS NULL"
                ).fetchall()
                if loads(r["payload"]).get("park_id") == park_id
            ]
            zones.sort(key=lambda z: (z.get("seq", 10**9), z.get("zone_id", "")))
            closures = conn.execute(
                "SELECT * FROM zone_closure WHERE park_id=? AND status!='reopened'",
                (park_id,),
            ).fetchall()
            closed_zone_ids = {r["zone_id"] for r in closures}
            available = [
                z.get("name", z.get("zone_id"))
                for z in zones
                if z.get("zone_id") not in closed_zone_ids
            ]
            if not zones:  # 未划分分区时给出整体状态
                available = [] if closed_zone_ids else ["全园"]
            commitments = []
            for r in conn.execute(
                "SELECT rv.entity_id, rv.payload FROM record_version rv "
                "WHERE rv.entity_type='enterprise_commitment' AND rv.valid_to IS NULL"
            ).fetchall():
                payload = loads(r["payload"])
                if payload.get("park_id") != park_id:
                    continue
                done = _one(
                    conn,
                    "SELECT fulfilled_at FROM commitment_fulfillment WHERE commitment_entity_id=?",
                    (r["entity_id"],),
                )
                if done is None:
                    commitments.append(
                        {"commitment_id": r["entity_id"], "content": payload.get("content")}
                    )
            opening = None
            for r in conn.execute(
                "SELECT payload FROM record_version WHERE entity_type='opening_schedule' AND valid_to IS NULL"
            ).fetchall():
                payload = loads(r["payload"])
                if payload.get("park_id") == park_id:
                    opening = payload
        funds = self.funds_snapshot(park_id)
        return {
            "park_id": park_id,
            "as_of": today,
            "available_areas": available,
            "closed_areas": sorted(closed_zone_ids),
            "active_closures": [
                {
                    "closure_id": r["id"],
                    "zone_id": r["zone_id"],
                    "reason": r["reason"],
                    "takeover_party": r["takeover_party"],
                    "closed_at": r["closed_at"],
                    "status": r["status"],
                }
                for r in closures
            ],
            "next_inspection": self.next_inspection(park_id, today),
            "unfulfilled_commitments": commitments,
            "opening_schedule": opening,
            "funds": {
                "net_paid": funds["net_paid"],
                "occupied": funds["occupied"],
                "pending_installment_reserved": funds["pending_installment_reserved"],
                "pending_adjustment_recovery": funds["pending_adjustment_recovery"],
            },
        }

    def history_on(self, park_id: str, on_date: str) -> dict[str, Any]:
        """争议回溯：指定日期当日——为何封闭、谁接管、居民收到了什么通知。"""
        with self.store.txn() as conn:
            closures = [
                {
                    "zone_id": r["zone_id"],
                    "reason": r["reason"],
                    "closed_by": r["closed_by"],
                    "takeover_party": r["takeover_party"],
                    "repair_party": r["repair_party"],
                    "closed_at": r["closed_at"],
                    "state_on_date": "封闭中",
                    "later_reopened_at": r["reopened_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM zone_closure WHERE park_id=? AND date(closed_at) <= date(?) "
                    "AND (reopened_at IS NULL OR date(reopened_at) > date(?)) ORDER BY closed_at",
                    (park_id, on_date, on_date),
                ).fetchall()
            ]
            notices = [
                dict(r)
                for r in conn.execute(
                    "SELECT recipient_ref, subject, body, event_type, created_at, delivered_at "
                    "FROM notification_outbox WHERE park_id=? AND date(created_at) <= date(?) "
                    "ORDER BY id",
                    (park_id, on_date),
                ).fetchall()
            ]
            open_schedule = None
            for r in conn.execute(
                "SELECT payload FROM record_version WHERE entity_type='opening_schedule' "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
                (on_date, on_date),
            ).fetchall():
                payload = loads(r["payload"])
                if payload.get("park_id") == park_id:
                    open_schedule = payload
        return {
            "park_id": park_id,
            "on_date": on_date,
            "closures": closures,
            "opening_schedule": open_schedule,
            "notices": notices,
        }


# -- 日期工具 ----------------------------------------------------------------------

def _shift_day(iso_date: str, delta_days: int) -> str:
    import datetime as _dt

    day = _dt.date.fromisoformat(iso_date)
    return (day + _dt.timedelta(days=delta_days)).isoformat()
