"""口袋公园共建养护与开放治理服务。

把地块权属与移交、建设资金、设施资产、适用人群、巡检标准、养护责任、
企业共建承诺和开放时段保存为带生效区间的记录。所有变化先追加到事件
日志再应用到内存状态：临时围挡、待修设施和付款审批在系统停机重启后
保持一致；发生争议时可以重放到指定日期，还原当时为何封闭、由谁接管
以及居民收到了什么通知。

记录 payload 中的日期一律使用 ISO 格式字符串（如 "2026-09-25"）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

# 记录种类：全部带生效区间 [valid_from, valid_to)
LAND_OWNERSHIP = "land_ownership"  # 地块权属
LAND_TRANSFER = "land_transfer"  # 地块移交
CONSTRUCTION_FUND = "construction_fund"  # 建设资金
FACILITY_ASSET = "facility_asset"  # 设施资产
APPLICABLE_POPULATION = "applicable_population"  # 适用人群
INSPECTION_STANDARD = "inspection_standard"  # 巡检标准
MAINTENANCE_RESPONSIBILITY = "maintenance_responsibility"  # 养护责任
ENTERPRISE_COMMITMENT = "enterprise_commitment"  # 企业共建承诺
OPENING_HOURS = "opening_hours"  # 开放时段

RECORD_KINDS = frozenset(
    {
        LAND_OWNERSHIP,
        LAND_TRANSFER,
        CONSTRUCTION_FUND,
        FACILITY_ASSET,
        APPLICABLE_POPULATION,
        INSPECTION_STANDARD,
        MAINTENANCE_RESPONSIBILITY,
        ENTERPRISE_COMMITMENT,
        OPENING_HOURS,
    }
)

RISK_HIGH = "high"

ORDER_OPEN = "open"
ORDER_REPAIR_DONE = "repair_done"
ORDER_VERIFIED = "verified"

ADJUST_REWORK = "rework"  # 返工
ADJUST_CANCEL = "cancel"  # 撤销
ADJUST_WARRANTY = "warranty"  # 质保追偿
ADJUST_KINDS = frozenset({ADJUST_REWORK, ADJUST_CANCEL, ADJUST_WARRANTY})


def _parse(raw: str) -> date:
    return date.fromisoformat(raw)


@dataclass
class IntervalRecord:
    """带生效区间的领域记录；valid_to 为 None 表示仍然有效。"""

    record_id: str
    kind: str
    park_id: str
    valid_from: date
    valid_to: date | None
    payload: dict[str, Any]

    def covers(self, on: date) -> bool:
        return self.valid_from <= on and (self.valid_to is None or on < self.valid_to)


class _State:
    """由事件日志重放得到的内存状态。"""

    def __init__(self) -> None:
        self.parks: dict[str, dict[str, Any]] = {}
        self.records: dict[str, IntervalRecord] = {}
        self.plans: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self.evidence: list[dict[str, Any]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.notices: list[dict[str, Any]] = []
        self.risks: dict[str, dict[str, Any]] = {}
        self.closures: dict[str, dict[str, Any]] = {}
        self.fencing: dict[str, dict[str, Any]] = {}
        self.segments: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.adjustments: list[dict[str, Any]] = []
        self.handovers: list[dict[str, Any]] = []
        self.fulfilled_commitments: set[str] = set()

    def apply(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        data = event["data"]
        if kind == "park_registered":
            self.parks[data["park_id"]] = {
                "name": data["name"],
                "zones": list(data["zones"]),
            }
        elif kind == "record_registered":
            self.records[data["record_id"]] = IntervalRecord(
                record_id=data["record_id"],
                kind=data["kind"],
                park_id=data["park_id"],
                valid_from=_parse(data["valid_from"]),
                valid_to=_parse(data["valid_to"]) if data["valid_to"] else None,
                payload=dict(data["payload"]),
            )
        elif kind == "record_closed":
            self.records[data["record_id"]].valid_to = _parse(data["valid_to"])
        elif kind == "daily_plan_computed":
            self.plans[(data["park_id"], event["at"])] = {
                item["facility_id"]: dict(item, status="pending")
                for item in data["items"]
            }
        elif kind == "evidence_submitted":
            self.evidence.append(data)
            plan = self.plans[(data["park_id"], data["plan_date"])]
            plan[data["facility_id"]]["status"] = "done"
        elif kind == "work_order_created":
            self.orders[data["order_id"]] = {
                "order_id": data["order_id"],
                "park_id": data["park_id"],
                "zone_id": data["zone_id"],
                "facility_id": data["facility_id"],
                "source": data["source"],
                "risk_id": data.get("risk_id"),
                "status": ORDER_OPEN,
                "repairer": None,
                "created_at": event["at"],
            }
        elif kind == "report_submitted":
            self.reports[data["report_id"]] = {
                "report_id": data["report_id"],
                "park_id": data["park_id"],
                "resident_id": data["resident_id"],
                "text": data["text"],
                "order_id": data["order_id"],
                "merged": data["merged"],
                "at": event["at"],
            }
        elif kind == "notice_sent":
            self.notices.append({**data, "at": event["at"]})
        elif kind == "repair_completed":
            order = self.orders[data["order_id"]]
            order["status"] = ORDER_REPAIR_DONE
            order["repairer"] = data["by"]
        elif kind == "work_order_reopened":
            self.orders[data["order_id"]]["status"] = ORDER_OPEN
        elif kind == "risk_flagged":
            self.risks[data["risk_id"]] = {
                "risk_id": data["risk_id"],
                "park_id": data["park_id"],
                "zone_ids": list(data["zone_ids"]),
                "level": data["level"],
                "order_id": data["order_id"],
                "flagged_at": event["at"],
                "resolved_at": None,
            }
            if data["order_id"] in self.orders:
                self.orders[data["order_id"]]["risk_id"] = data["risk_id"]
        elif kind == "risk_resolved":
            risk = self.risks[data["risk_id"]]
            risk["resolved_at"] = event["at"]
            risk["inspector"] = data["by"]
            self.orders[risk["order_id"]]["status"] = ORDER_VERIFIED
        elif kind == "zones_closed":
            self.closures[data["closure_id"]] = {
                "closure_id": data["closure_id"],
                "park_id": data["park_id"],
                "zone_ids": list(data["zone_ids"]),
                "reason": data["reason"],
                "risk_id": data["risk_id"],
                "closed_at": event["at"],
                "reopened_at": None,
            }
        elif kind == "zones_reopened":
            self.closures[data["closure_id"]]["reopened_at"] = event["at"]
        elif kind == "fencing_placed":
            self.fencing[data["fencing_id"]] = {
                "fencing_id": data["fencing_id"],
                "park_id": data["park_id"],
                "zone_id": data["zone_id"],
                "note": data["note"],
                "placed_at": event["at"],
                "removed_at": None,
            }
        elif kind == "fencing_removed":
            self.fencing[data["fencing_id"]]["removed_at"] = event["at"]
        elif kind == "segment_confirmed":
            self.segments[data["segment_id"]] = {
                "segment_id": data["segment_id"],
                "fund_id": data["fund_id"],
                "label": data["label"],
                "amount": data["amount"],
                "quantity": data["quantity"],
                "confirmed_at": event["at"],
            }
        elif kind == "payment_made":
            self.payments[data["payment_id"]] = {
                "payment_id": data["payment_id"],
                "segment_id": data["segment_id"],
                "fund_id": data["fund_id"],
                "amount": data["amount"],
                "payee": data["payee"],
                "approved_by": data["approved_by"],
                "paid_at": event["at"],
            }
        elif kind == "payment_adjusted":
            self.adjustments.append({**data, "at": event["at"]})
        elif kind == "maintenance_handover":
            self.handovers.append({**data, "at": event["at"]})
        elif kind == "commitment_fulfilled":
            self.fulfilled_commitments.add(data["record_id"])
        else:
            raise ValueError(f"未知事件类型: {kind}")

    def active_records(
        self, kind: str, park_id: str, on: date
    ) -> list[IntervalRecord]:
        return [
            record
            for record in self.records.values()
            if record.kind == kind and record.park_id == park_id and record.covers(on)
        ]

    def open_closures(self, park_id: str) -> list[dict[str, Any]]:
        return [
            closure
            for closure in self.closures.values()
            if closure["park_id"] == park_id and closure["reopened_at"] is None
        ]


class ParkService:
    """共建养护服务入口；store_path 提供事件日志文件，停机后重放恢复。"""

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._store_path = Path(store_path) if store_path else None
        self._events: list[dict[str, Any]] = []
        if self._store_path and self._store_path.exists():
            for line in self._store_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(json.loads(line))
        self._state = _State()
        for event in self._events:
            self._state.apply(event)

    # ---- 基础设施 ----

    def _emit(self, type_: str, on: date, data: dict[str, Any]) -> dict[str, Any]:
        event = {
            "seq": len(self._events),
            "at": on.isoformat(),
            "type": type_,
            "data": data,
        }
        if self._store_path:
            with self._store_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events.append(event)
        self._state.apply(event)
        return event

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{len(self._events) + 1:04d}"

    def _park(self, park_id: str) -> dict[str, Any]:
        park = self._state.parks.get(park_id)
        if park is None:
            raise ValueError("公园未登记")
        return park

    def _order(self, order_id: str) -> dict[str, Any]:
        order = self._state.orders.get(order_id)
        if order is None:
            raise ValueError("处置单不存在")
        return order

    def _risk(self, risk_id: str) -> dict[str, Any]:
        risk = self._state.risks.get(risk_id)
        if risk is None:
            raise ValueError("风险记录不存在")
        return risk

    # ---- 公园与生效区间记录 ----

    def register_park(
        self, park_id: str, name: str, zones: Iterable[str], on: date
    ) -> None:
        zones = list(zones)
        if not zones or len(set(zones)) != len(zones):
            raise ValueError("公园区域不能为空且不能重复")
        if park_id in self._state.parks:
            raise ValueError("公园已登记")
        self._emit(
            "park_registered",
            on,
            {"park_id": park_id, "name": name, "zones": zones},
        )

    def register_record(
        self,
        kind: str,
        park_id: str,
        valid_from: date,
        payload: dict[str, Any],
        valid_to: date | None = None,
    ) -> str:
        """登记一条带生效区间的记录，返回 record_id。"""
        if kind not in RECORD_KINDS:
            raise ValueError(f"未知记录种类: {kind}")
        park = self._park(park_id)
        if valid_to is not None and valid_to <= valid_from:
            raise ValueError("生效区间无效")
        if kind == FACILITY_ASSET:
            if not payload.get("category") or not payload.get("zone_id"):
                raise ValueError("设施资产需要 category 与 zone_id")
            if payload["zone_id"] not in park["zones"]:
                raise ValueError("设施所在区域未登记")
        if kind == INSPECTION_STANDARD:
            if not payload.get("category") or int(payload.get("cycle_days", 0)) <= 0:
                raise ValueError("巡检标准需要 category 与正数 cycle_days")
        if kind == MAINTENANCE_RESPONSIBILITY and not payload.get("party"):
            raise ValueError("养护责任需要 party")
        if kind == ENTERPRISE_COMMITMENT:
            if not payload.get("party") or not payload.get("text") or not payload.get("due"):
                raise ValueError("共建承诺需要 party、text 与 due")
            _parse(payload["due"])
        record_id = self._next_id("REC")
        self._emit(
            "record_registered",
            valid_from,
            {
                "record_id": record_id,
                "kind": kind,
                "park_id": park_id,
                "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat() if valid_to else None,
                "payload": dict(payload),
            },
        )
        return record_id

    def close_record(self, record_id: str, valid_to: date) -> None:
        record = self._state.records.get(record_id)
        if record is None:
            raise ValueError("记录不存在")
        if record.valid_to is not None:
            raise ValueError("记录已终止")
        if valid_to < record.valid_from:
            raise ValueError("终止日期早于生效日期")
        self._emit(
            "record_closed",
            valid_to,
            {"record_id": record_id, "valid_to": valid_to.isoformat()},
        )

    def active_records(
        self, kind: str, park_id: str, on: date
    ) -> list[IntervalRecord]:
        return self._state.active_records(kind, park_id, on)

    # ---- 每日巡检：先算应检与逾期责任，再收现场证据 ----

    def _last_evidence_date(self, park_id: str, facility_id: str) -> date | None:
        dates = [
            _parse(entry["plan_date"])
            for entry in self._state.evidence
            if entry["park_id"] == park_id and entry["facility_id"] == facility_id
        ]
        return max(dates) if dates else None

    def _responsible_for(
        self, park_id: str, facility: IntervalRecord, on: date
    ) -> str | None:
        for record in self._state.active_records(
            MAINTENANCE_RESPONSIBILITY, park_id, on
        ):
            payload = record.payload
            if (
                payload.get("scope") == "all"
                or payload.get("category") == facility.payload["category"]
                or payload.get("zone") == facility.payload["zone_id"]
            ):
                return payload["party"]
        return None

    def _plan_items(self, park_id: str, on: date) -> list[dict[str, Any]]:
        standards = {
            record.payload["category"]: record
            for record in self._state.active_records(INSPECTION_STANDARD, park_id, on)
        }
        items = []
        for facility in self._state.active_records(FACILITY_ASSET, park_id, on):
            standard = standards.get(facility.payload["category"])
            if standard is None:
                continue
            cycle = timedelta(days=int(standard.payload["cycle_days"]))
            anchor = self._last_evidence_date(park_id, facility.record_id)
            due = (anchor or facility.valid_from) + cycle
            if due <= on:
                items.append(
                    {
                        "facility_id": facility.record_id,
                        "facility_name": facility.payload.get(
                            "name", facility.record_id
                        ),
                        "category": facility.payload["category"],
                        "zone_id": facility.payload["zone_id"],
                        "due": due.isoformat(),
                        "overdue": due < on,
                        "overdue_days": (on - due).days,
                        "responsible": self._responsible_for(park_id, facility, due),
                    }
                )
        items.sort(key=lambda item: (item["due"], item["facility_id"]))
        return items

    def compute_daily_plan(self, park_id: str, on: date) -> list[dict[str, Any]]:
        """计算当日应检项目与逾期责任；同一天重复调用返回同一计划。"""
        self._park(park_id)
        key = (park_id, on.isoformat())
        if key in self._state.plans:
            return list(self._state.plans[key].values())
        items = self._plan_items(park_id, on)
        self._emit(
            "daily_plan_computed", on, {"park_id": park_id, "items": items}
        )
        return items

    def submit_evidence(
        self,
        park_id: str,
        facility_id: str,
        inspector: str,
        on: date,
        result: str = "ok",
        risk_level: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """接收现场证据；必须先算出当日计划，且设施在应检项目中。"""
        plan = self._state.plans.get((park_id, on.isoformat()))
        if plan is None:
            raise ValueError("请先计算当日巡检计划，再接收现场证据")
        item = plan.get(facility_id)
        if item is None:
            raise ValueError("该设施不在当日应检项目中")
        if item["status"] == "done":
            raise ValueError("该应检项目已收到现场证据")
        self._emit(
            "evidence_submitted",
            on,
            {
                "park_id": park_id,
                "plan_date": on.isoformat(),
                "facility_id": facility_id,
                "inspector": inspector,
                "result": result,
                "risk_level": risk_level,
                "notes": notes,
            },
        )
        outcome: dict[str, Any] = {"facility_id": facility_id}
        if risk_level == RISK_HIGH:
            order_id = self._create_order(
                park_id, item["zone_id"], facility_id, "risk", on
            )
            risk_id = self.flag_risk(
                park_id,
                [item["zone_id"]],
                on,
                by=inspector,
                order_id=order_id,
                reason=f"巡检发现高风险：{notes or item['facility_name']}",
            )
            outcome.update(order_id=order_id, risk_id=risk_id)
        return outcome

    # ---- 报修与处置单 ----

    def _create_order(
        self,
        park_id: str,
        zone_id: str | None,
        facility_id: str | None,
        source: str,
        on: date,
    ) -> str:
        order_id = self._next_id("WO")
        self._emit(
            "work_order_created",
            on,
            {
                "order_id": order_id,
                "park_id": park_id,
                "zone_id": zone_id,
                "facility_id": facility_id,
                "source": source,
                "risk_id": None,
            },
        )
        return order_id

    def _notify(
        self,
        park_id: str,
        resident_id: str,
        order_id: str,
        kind: str,
        text: str,
        on: date,
    ) -> None:
        self._emit(
            "notice_sent",
            on,
            {
                "notice_id": self._next_id("NT"),
                "park_id": park_id,
                "resident_id": resident_id,
                "order_id": order_id,
                "kind": kind,
                "text": text,
            },
        )

    def _notify_order_residents(
        self, order_id: str, kind: str, text: str, on: date
    ) -> None:
        order = self._order(order_id)
        for report in self._state.reports.values():
            if report["order_id"] == order_id:
                self._notify(
                    order["park_id"],
                    report["resident_id"],
                    order_id,
                    kind,
                    text,
                    on,
                )

    def _facility_zone(self, park_id: str, facility_id: str) -> str | None:
        record = self._state.records.get(facility_id)
        if record and record.kind == FACILITY_ASSET and record.park_id == park_id:
            return record.payload.get("zone_id")
        return None

    def _find_mergeable_order(
        self, park_id: str, facility_id: str | None, zone_id: str | None
    ) -> dict[str, Any] | None:
        for order in self._state.orders.values():
            if order["park_id"] != park_id or order["status"] != ORDER_OPEN:
                continue
            if facility_id and order["facility_id"] == facility_id:
                return order
            if not facility_id and zone_id and order["zone_id"] == zone_id:
                return order
        return None

    def submit_report(
        self,
        park_id: str,
        resident_id: str,
        text: str,
        on: date,
        facility_id: str | None = None,
        zone_id: str | None = None,
    ) -> tuple[str, str]:
        """居民报修；相近报修并入同一处置单，返回 (report_id, order_id)。"""
        self._park(park_id)
        if facility_id and not zone_id:
            zone_id = self._facility_zone(park_id, facility_id)
        if not zone_id and not facility_id:
            raise ValueError("请提供报修设施或所在区域")
        order = self._find_mergeable_order(park_id, facility_id, zone_id)
        merged = order is not None
        order_id = (
            order["order_id"]
            if merged
            else self._create_order(park_id, zone_id, facility_id, "report", on)
        )
        report_id = self._next_id("RP")
        self._emit(
            "report_submitted",
            on,
            {
                "report_id": report_id,
                "park_id": park_id,
                "resident_id": resident_id,
                "text": text,
                "order_id": order_id,
                "merged": merged,
            },
        )
        if merged:
            self._notify(
                park_id,
                resident_id,
                order_id,
                "merged",
                f"相近报修已并入处置单 {order_id}",
                on,
            )
        else:
            self._notify(
                park_id,
                resident_id,
                order_id,
                "accepted",
                f"报修已受理，处置单 {order_id}",
                on,
            )
        return report_id, order_id

    # ---- 风险分区与复检重开 ----

    def flag_risk(
        self,
        park_id: str,
        zone_ids: Iterable[str],
        on: date,
        by: str,
        level: str = RISK_HIGH,
        order_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        """标记风险并只封闭受影响区域，返回 risk_id。"""
        park = self._park(park_id)
        zone_ids = list(zone_ids)
        unknown = sorted(set(zone_ids) - set(park["zones"]))
        if unknown:
            raise ValueError(f"未知区域: {unknown}")
        if order_id is None:
            order_id = self._create_order(
                park_id, zone_ids[0], None, "risk", on
            )
        risk_id = self._next_id("RISK")
        self._emit(
            "risk_flagged",
            on,
            {
                "risk_id": risk_id,
                "park_id": park_id,
                "zone_ids": zone_ids,
                "level": level,
                "by": by,
                "order_id": order_id,
            },
        )
        self._emit(
            "zones_closed",
            on,
            {
                "closure_id": self._next_id("CL"),
                "park_id": park_id,
                "zone_ids": zone_ids,
                "reason": reason or f"高风险 {risk_id}，临时封闭受影响区域",
                "risk_id": risk_id,
            },
        )
        return risk_id

    def complete_repair(self, order_id: str, by: str, on: date) -> None:
        order = self._order(order_id)
        if order["status"] != ORDER_OPEN:
            raise ValueError("处置单不在待修状态")
        self._emit("repair_completed", on, {"order_id": order_id, "by": by})
        self._notify_order_residents(order_id, "repair_done", "维修已完成，等待复检", on)

    def reinspect(self, risk_id: str, by: str, on: date, passed: bool = True) -> None:
        """复检必须由维修人之外的人员执行。"""
        risk = self._risk(risk_id)
        if risk["resolved_at"] is not None:
            raise ValueError("风险已解除")
        order = self._order(risk["order_id"])
        if order["status"] != ORDER_REPAIR_DONE:
            raise ValueError("维修尚未完成，不能复检")
        if by == order["repairer"]:
            raise ValueError("复检人员不能是维修人")
        if passed:
            self._emit("risk_resolved", on, {"risk_id": risk_id, "by": by})
            self._notify_order_residents(
                risk["order_id"], "verified", "复检通过，风险已解除", on
            )
        else:
            self._emit(
                "work_order_reopened",
                on,
                {"order_id": risk["order_id"], "by": by},
            )
            self._notify_order_residents(
                risk["order_id"], "rework", "复检未通过，已退回返工", on
            )

    def reopen_zones(self, closure_id: str, on: date) -> None:
        closure = self._state.closures.get(closure_id)
        if closure is None:
            raise ValueError("封闭记录不存在")
        if closure["reopened_at"] is not None:
            raise ValueError("区域已重新开放")
        risk = self._risk(closure["risk_id"])
        if risk["resolved_at"] is None:
            raise ValueError("风险尚未解除，不能重新开放")
        if on < _parse(risk["resolved_at"]):
            raise ValueError("重新开放不得早于风险解除")
        self._emit("zones_reopened", on, {"closure_id": closure_id})
        self._notify_order_residents(
            risk["order_id"], "reopened", "受影响区域已重新开放", on
        )

    # ---- 临时围挡 ----

    def place_fencing(self, park_id: str, zone_id: str, note: str, on: date) -> str:
        park = self._park(park_id)
        if zone_id not in park["zones"]:
            raise ValueError("未知区域")
        fencing_id = self._next_id("FEN")
        self._emit(
            "fencing_placed",
            on,
            {
                "fencing_id": fencing_id,
                "park_id": park_id,
                "zone_id": zone_id,
                "note": note,
            },
        )
        return fencing_id

    def remove_fencing(self, fencing_id: str, on: date) -> None:
        fencing = self._state.fencing.get(fencing_id)
        if fencing is None or fencing["removed_at"] is not None:
            raise ValueError("围挡不存在或已拆除")
        self._emit("fencing_removed", on, {"fencing_id": fencing_id})

    # ---- 分段支付与调整 ----

    def confirm_segment(
        self, fund_id: str, label: str, amount: float, quantity: str, on: date
    ) -> str:
        """按确认工程量登记一个支付分段，进入待审批状态。"""
        fund = self._state.records.get(fund_id)
        if fund is None or fund.kind != CONSTRUCTION_FUND:
            raise ValueError("建设资金记录不存在")
        if amount <= 0:
            raise ValueError("分段金额必须为正")
        segment_id = self._next_id("SEG")
        self._emit(
            "segment_confirmed",
            on,
            {
                "segment_id": segment_id,
                "fund_id": fund_id,
                "label": label,
                "amount": amount,
                "quantity": quantity,
            },
        )
        return segment_id

    def approve_payment(
        self, segment_id: str, payee: str, approved_by: str, on: date
    ) -> str:
        segment = self._state.segments.get(segment_id)
        if segment is None:
            raise ValueError("支付分段不存在")
        if any(
            payment["segment_id"] == segment_id
            for payment in self._state.payments.values()
        ):
            raise ValueError("该分段已支付")
        payment_id = self._next_id("PAY")
        self._emit(
            "payment_made",
            on,
            {
                "payment_id": payment_id,
                "segment_id": segment_id,
                "fund_id": segment["fund_id"],
                "amount": segment["amount"],
                "payee": payee,
                "approved_by": approved_by,
            },
        )
        return payment_id

    def adjust_payment(
        self,
        payment_id: str,
        kind: str,
        amount: float,
        on: date,
        responsible: str | None = None,
        note: str = "",
    ) -> str:
        """返工、撤销、质保追偿：保留原付款，另行登记带符号的调整。"""
        if kind not in ADJUST_KINDS:
            raise ValueError(f"未知调整类型: {kind}")
        payment = self._state.payments.get(payment_id)
        if payment is None:
            raise ValueError("原付款不存在")
        adjustment_id = self._next_id("ADJ")
        self._emit(
            "payment_adjusted",
            on,
            {
                "adjustment_id": adjustment_id,
                "payment_id": payment_id,
                "fund_id": payment["fund_id"],
                "kind": kind,
                "amount": amount,
                "responsible": responsible or payment["payee"],
                "note": note,
            },
        )
        return adjustment_id

    def fund_occupancy(self, park_id: str, on: date) -> list[dict[str, Any]]:
        """各建设资金的预算、已付、调整与资金占用。"""
        result = []
        for fund in self._state.active_records(CONSTRUCTION_FUND, park_id, on):
            segments = [
                item
                for item in self._state.segments.values()
                if item["fund_id"] == fund.record_id
            ]
            payments = [
                item
                for item in self._state.payments.values()
                if item["fund_id"] == fund.record_id
            ]
            paid_segment_ids = {item["segment_id"] for item in payments}
            paid = sum(item["amount"] for item in payments)
            adjustments = sum(
                item["amount"]
                for item in self._state.adjustments
                if item["fund_id"] == fund.record_id
            )
            result.append(
                {
                    "fund_id": fund.record_id,
                    "budget": fund.payload.get("budget"),
                    "confirmed": sum(item["amount"] for item in segments),
                    "committed_unpaid": sum(
                        item["amount"]
                        for item in segments
                        if item["segment_id"] not in paid_segment_ids
                    ),
                    "paid": paid,
                    "adjustments": adjustments,
                    "occupancy": paid + adjustments,
                }
            )
        return result

    # ---- 共建承诺与养护移交 ----

    def fulfill_commitment(self, record_id: str, on: date) -> None:
        record = self._state.records.get(record_id)
        if record is None or record.kind != ENTERPRISE_COMMITMENT:
            raise ValueError("共建承诺不存在")
        self._emit("commitment_fulfilled", on, {"record_id": record_id})

    def handover_maintenance(
        self, park_id: str, from_party: str, to_party: str, on: date
    ) -> dict[str, Any]:
        """企业移交养护：责任记录分段生效，此前责任与未了债务留在原企业。"""
        active = [
            record
            for record in self._state.active_records(
                MAINTENANCE_RESPONSIBILITY, park_id, on
            )
            if record.payload.get("party") == from_party
        ]
        if not active:
            raise ValueError("未找到该企业在管的责任")
        closed_ids, new_ids = [], []
        for record in active:
            self.close_record(record.record_id, on)
            closed_ids.append(record.record_id)
            new_ids.append(
                self.register_record(
                    MAINTENANCE_RESPONSIBILITY,
                    park_id,
                    on,
                    {**record.payload, "party": to_party},
                )
            )
        kept_liabilities = {
            "open_risks": [
                risk["risk_id"]
                for risk in self._state.risks.values()
                if risk["park_id"] == park_id and risk["resolved_at"] is None
            ],
            "warranty_adjustments": [
                item["adjustment_id"]
                for item in self._state.adjustments
                if item["kind"] == ADJUST_WARRANTY
                and item["responsible"] == from_party
            ],
        }
        self._emit(
            "maintenance_handover",
            on,
            {
                "park_id": park_id,
                "from_party": from_party,
                "to_party": to_party,
                "closed_record_ids": closed_ids,
                "new_record_ids": new_ids,
                "kept_liabilities": kept_liabilities,
            },
        )
        return {
            "closed_record_ids": closed_ids,
            "new_record_ids": new_ids,
            "kept_liabilities": kept_liabilities,
        }

    # ---- 街道视图与争议回放 ----

    def _next_inspection(self, park_id: str, on: date) -> dict[str, Any] | None:
        standards = {
            record.payload["category"]: record
            for record in self._state.active_records(INSPECTION_STANDARD, park_id, on)
        }
        dues: dict[str, date] = {}
        for facility in self._state.active_records(FACILITY_ASSET, park_id, on):
            standard = standards.get(facility.payload["category"])
            if standard is None:
                continue
            anchor = self._last_evidence_date(park_id, facility.record_id)
            due = (anchor or facility.valid_from) + timedelta(
                days=int(standard.payload["cycle_days"])
            )
            dues[facility.record_id] = due
        if not dues:
            return None
        future = [due for due in dues.values() if due >= on]
        target = min(future) if future else min(dues.values())
        return {
            "date": target.isoformat(),
            "facility_ids": sorted(
                facility_id
                for facility_id, due in dues.items()
                if due == target
            ),
        }

    def park_overview(self, park_id: str, on: date) -> dict[str, Any]:
        """街道查看公园：可用区域、下一次巡检、未兑现承诺和资金占用。"""
        park = self._park(park_id)
        closures = self._state.open_closures(park_id)
        closed_zone_ids = {zone for c in closures for zone in c["zone_ids"]}
        commitments = []
        for record in self._state.records.values():
            if (
                record.kind != ENTERPRISE_COMMITMENT
                or record.park_id != park_id
                or record.record_id in self._state.fulfilled_commitments
            ):
                continue
            due = record.payload.get("due")
            if due and _parse(due) < on:
                commitments.append(
                    {
                        "record_id": record.record_id,
                        "party": record.payload.get("party"),
                        "text": record.payload.get("text"),
                        "due": due,
                    }
                )
        return {
            "park_id": park_id,
            "date": on.isoformat(),
            "available_zones": [
                zone for zone in park["zones"] if zone not in closed_zone_ids
            ],
            "closed_zones": [
                {
                    "zone": zone,
                    "closure_id": closure["closure_id"],
                    "reason": closure["reason"],
                    "since": closure["closed_at"],
                    "risk_id": closure["risk_id"],
                }
                for closure in closures
                for zone in closure["zone_ids"]
            ],
            "active_fencing": [
                {
                    "fencing_id": fencing["fencing_id"],
                    "zone_id": fencing["zone_id"],
                    "note": fencing["note"],
                    "since": fencing["placed_at"],
                }
                for fencing in self._state.fencing.values()
                if fencing["park_id"] == park_id and fencing["removed_at"] is None
            ],
            "next_inspection": self._next_inspection(park_id, on),
            "due_items": self._plan_items(park_id, on),
            "pending_repairs": [
                {
                    "order_id": order["order_id"],
                    "facility_id": order["facility_id"],
                    "zone_id": order["zone_id"],
                    "status": order["status"],
                }
                for order in self._state.orders.values()
                if order["park_id"] == park_id
                and order["status"] in (ORDER_OPEN, ORDER_REPAIR_DONE)
            ],
            "unfulfilled_commitments": sorted(
                commitments, key=lambda item: item["due"]
            ),
            "fund_occupancy": self.fund_occupancy(park_id, on),
        }

    def notices_for(self, park_id: str, resident_id: str) -> list[dict[str, Any]]:
        return [
            notice
            for notice in self._state.notices
            if notice["park_id"] == park_id and notice["resident_id"] == resident_id
        ]

    def explain_as_of(self, park_id: str, on: date) -> dict[str, Any]:
        """回到指定日期：当时为何封闭、由谁接管、居民收到了什么通知。"""
        view = _State()
        cutoff = on.isoformat()
        for event in self._events:
            if event["at"] <= cutoff:
                view.apply(event)
        if park_id not in view.parks:
            raise ValueError("该日期公园尚未登记")
        responsibilities = {}
        for record in view.records.values():
            if (
                record.kind == MAINTENANCE_RESPONSIBILITY
                and record.park_id == park_id
                and record.covers(on)
            ):
                key = (
                    record.payload.get("category")
                    or record.payload.get("zone")
                    or "all"
                )
                responsibilities[key] = record.payload["party"]
        return {
            "park_id": park_id,
            "date": cutoff,
            "closed_zones": [
                {
                    "zone": zone,
                    "closure_id": closure["closure_id"],
                    "reason": closure["reason"],
                    "risk_id": closure["risk_id"],
                    "closed_at": closure["closed_at"],
                }
                for closure in view.open_closures(park_id)
                for zone in closure["zone_ids"]
            ],
            "responsibilities": responsibilities,
            "handovers": [
                {
                    "from_party": handover["from_party"],
                    "to_party": handover["to_party"],
                    "at": handover["at"],
                    "kept_liabilities": handover["kept_liabilities"],
                }
                for handover in view.handovers
                if handover["park_id"] == park_id
            ],
            "notices": [
                {
                    "resident_id": notice["resident_id"],
                    "order_id": notice["order_id"],
                    "kind": notice["kind"],
                    "text": notice["text"],
                    "at": notice["at"],
                }
                for notice in view.notices
                if notice["park_id"] == park_id
            ],
        }
