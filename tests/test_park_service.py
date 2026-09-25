"""口袋公园共建养护服务的验收测试。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.park_service import (
    ADJUST_WARRANTY,
    APPLICABLE_POPULATION,
    CONSTRUCTION_FUND,
    ENTERPRISE_COMMITMENT,
    FACILITY_ASSET,
    INSPECTION_STANDARD,
    LAND_OWNERSHIP,
    LAND_TRANSFER,
    MAINTENANCE_RESPONSIBILITY,
    OPENING_HOURS,
    ParkService,
)

START = date(2026, 1, 1)


def build_service() -> ParkService:
    """登记一座两区域公园，含三类设施、巡检标准和养护责任。"""
    service = ParkService()
    service.register_park("P1", "街角口袋公园", ["east", "west"], START)
    for category, cycle in (("children", 7), ("accessible", 14), ("lighting", 30)):
        service.register_record(
            INSPECTION_STANDARD,
            "P1",
            START,
            {"category": category, "cycle_days": cycle, "description": f"{category}巡检"},
        )
    service.register_record(
        FACILITY_ASSET, "P1", START,
        {"name": "儿童滑梯", "zone_id": "east", "category": "children"},
    )
    service.register_record(
        FACILITY_ASSET, "P1", START,
        {"name": "无障碍坡道", "zone_id": "east", "category": "accessible"},
    )
    service.register_record(
        FACILITY_ASSET, "P1", START,
        {"name": "庭院灯", "zone_id": "west", "category": "lighting"},
    )
    service.register_record(
        MAINTENANCE_RESPONSIBILITY, "P1", START,
        {"party": "园林一队", "category": "children"},
    )
    service.register_record(
        MAINTENANCE_RESPONSIBILITY, "P1", START,
        {"party": "社区物业", "category": "accessible"},
    )
    service.register_record(
        MAINTENANCE_RESPONSIBILITY, "P1", START,
        {"party": "共建企业A", "category": "lighting"},
    )
    return service


def facility_ids(service: ParkService) -> dict[str, str]:
    result = {}
    for record in service.active_records(FACILITY_ASSET, "P1", START):
        result[record.payload["category"]] = record.record_id
    return result


class IntervalRecordTest(unittest.TestCase):
    def test_records_keep_effective_intervals(self) -> None:
        service = ParkService()
        service.register_park("P1", "街角口袋公园", ["east"], START)
        for kind in (
            LAND_OWNERSHIP,
            LAND_TRANSFER,
            CONSTRUCTION_FUND,
            FACILITY_ASSET,
            APPLICABLE_POPULATION,
            INSPECTION_STANDARD,
            ENTERPRISE_COMMITMENT,
            OPENING_HOURS,
        ):
            payload = {
                FACILITY_ASSET: {"name": "座椅", "zone_id": "east", "category": "site"},
                INSPECTION_STANDARD: {"category": "site", "cycle_days": 30},
                ENTERPRISE_COMMITMENT: {
                    "party": "共建企业A",
                    "text": "每季度补植",
                    "due": "2026-04-01",
                },
                CONSTRUCTION_FUND: {"budget": 100},
            }.get(kind, {"note": kind})
            service.register_record(kind, "P1", START, payload)
        old = service.register_record(
            MAINTENANCE_RESPONSIBILITY, "P1", START, {"party": "园林一队", "scope": "all"}
        )
        service.close_record(old, date(2026, 3, 1))
        service.register_record(
            MAINTENANCE_RESPONSIBILITY, "P1", date(2026, 3, 1),
            {"party": "社区物业", "scope": "all"},
        )
        before = service.active_records(MAINTENANCE_RESPONSIBILITY, "P1", date(2026, 2, 1))
        after = service.active_records(MAINTENANCE_RESPONSIBILITY, "P1", date(2026, 3, 1))
        self.assertEqual([r.payload["party"] for r in before], ["园林一队"])
        self.assertEqual([r.payload["party"] for r in after], ["社区物业"])
        with self.assertRaisesRegex(ValueError, "未知记录种类"):
            service.register_record("unknown", "P1", START, {})


class InspectionPlanTest(unittest.TestCase):
    def test_daily_plan_cycles_and_overdue_responsibility(self) -> None:
        service = build_service()
        ids = facility_ids(service)
        plan = service.compute_daily_plan("P1", date(2026, 1, 8))
        self.assertEqual([item["facility_id"] for item in plan], [ids["children"]])
        self.assertFalse(plan[0]["overdue"])
        plan = service.compute_daily_plan("P1", date(2026, 1, 20))
        by_category = {item["category"]: item for item in plan}
        self.assertEqual(set(by_category), {"children", "accessible"})
        self.assertEqual(by_category["children"]["overdue_days"], 12)
        self.assertEqual(by_category["children"]["responsible"], "园林一队")
        self.assertEqual(by_category["accessible"]["overdue_days"], 5)
        self.assertEqual(by_category["accessible"]["responsible"], "社区物业")

    def test_evidence_requires_plan_and_matches_items(self) -> None:
        service = build_service()
        ids = facility_ids(service)
        with self.assertRaisesRegex(ValueError, "先计算当日巡检计划"):
            service.submit_evidence("P1", ids["children"], "巡检员甲", date(2026, 1, 8))
        service.compute_daily_plan("P1", date(2026, 1, 8))
        with self.assertRaisesRegex(ValueError, "不在当日应检项目"):
            service.submit_evidence("P1", ids["accessible"], "巡检员甲", date(2026, 1, 8))
        service.submit_evidence("P1", ids["children"], "巡检员甲", date(2026, 1, 8))
        with self.assertRaisesRegex(ValueError, "已收到现场证据"):
            service.submit_evidence("P1", ids["children"], "巡检员甲", date(2026, 1, 8))
        plan = service.compute_daily_plan("P1", date(2026, 1, 15))
        self.assertEqual(
            {item["category"] for item in plan}, {"children", "accessible"}
        )


class ReportMergeTest(unittest.TestCase):
    def test_reports_merge_but_residents_keep_own_progress(self) -> None:
        service = build_service()
        _, order_a = service.submit_report(
            "P1", "居民甲", "饮水机不出水", date(2026, 2, 1), zone_id="east"
        )
        _, order_b = service.submit_report(
            "P1", "居民乙", "饮水机漏水", date(2026, 2, 2), zone_id="east"
        )
        self.assertEqual(order_a, order_b)
        service.complete_repair(order_a, by="维修丙", on=date(2026, 2, 3))
        notices_a = service.notices_for("P1", "居民甲")
        notices_b = service.notices_for("P1", "居民乙")
        self.assertEqual([n["kind"] for n in notices_a], ["accepted", "repair_done"])
        self.assertEqual([n["kind"] for n in notices_b], ["merged", "repair_done"])
        self.assertTrue(all(n["order_id"] == order_a for n in notices_b))


class RiskZoneTest(unittest.TestCase):
    def _raise_high_risk(self) -> tuple[ParkService, str, str]:
        service = build_service()
        ids = facility_ids(service)
        service.compute_daily_plan("P1", date(2026, 1, 8))
        outcome = service.submit_evidence(
            "P1", ids["children"], "巡检员甲", date(2026, 1, 8),
            result="issue", risk_level="high", notes="滑梯立柱松动",
        )
        return service, outcome["risk_id"], ids["children"]

    def test_high_risk_closes_only_affected_zone(self) -> None:
        service, _, _ = self._raise_high_risk()
        overview = service.park_overview("P1", date(2026, 1, 8))
        self.assertEqual(overview["available_zones"], ["west"])
        self.assertEqual([z["zone"] for z in overview["closed_zones"]], ["east"])
        self.assertIn("滑梯立柱松动", overview["closed_zones"][0]["reason"])

    def test_reopen_requires_other_inspector_and_resolved_risk(self) -> None:
        service, risk_id, _ = self._raise_high_risk()
        closure = service.park_overview("P1", date(2026, 1, 8))["closed_zones"][0]
        with self.assertRaisesRegex(ValueError, "风险尚未解除"):
            service.reopen_zones(closure["closure_id"], date(2026, 1, 9))
        order_id = service._risk(risk_id)["order_id"]
        service.complete_repair(order_id, by="维修丙", on=date(2026, 1, 9))
        with self.assertRaisesRegex(ValueError, "不能是维修人"):
            service.reinspect(risk_id, by="维修丙", on=date(2026, 1, 10))
        service.reinspect(risk_id, by="复检丁", on=date(2026, 1, 10))
        with self.assertRaisesRegex(ValueError, "不得早于风险解除"):
            service.reopen_zones(closure["closure_id"], date(2026, 1, 9))
        service.reopen_zones(closure["closure_id"], date(2026, 1, 10))
        overview = service.park_overview("P1", date(2026, 1, 10))
        self.assertEqual(sorted(overview["available_zones"]), ["east", "west"])

    def test_failed_reinspection_returns_order_to_rework(self) -> None:
        service, risk_id, _ = self._raise_high_risk()
        order_id = service._risk(risk_id)["order_id"]
        service.complete_repair(order_id, by="维修丙", on=date(2026, 1, 9))
        service.reinspect(risk_id, by="复检丁", on=date(2026, 1, 10), passed=False)
        self.assertEqual(service._order(order_id)["status"], "open")
        service.complete_repair(order_id, by="维修丙", on=date(2026, 1, 11))
        service.reinspect(risk_id, by="复检丁", on=date(2026, 1, 12))
        self.assertEqual(service._risk(risk_id)["resolved_at"], "2026-01-12")


class FundTest(unittest.TestCase):
    def _service_with_fund(self) -> tuple[ParkService, str]:
        service = build_service()
        fund_id = service.register_record(
            CONSTRUCTION_FUND, "P1", START, {"budget": 1000, "source": "政企共建"}
        )
        return service, fund_id

    def test_segmented_payment_and_adjustments_keep_original(self) -> None:
        service, fund_id = self._service_with_fund()
        seg1 = service.confirm_segment(fund_id, "园路铺装", 400, "铺装120㎡", date(2026, 2, 1))
        seg2 = service.confirm_segment(fund_id, "绿化种植", 300, "乔木20株", date(2026, 2, 10))
        payment = service.approve_payment(seg1, payee="共建企业A", approved_by="街道经办", on=date(2026, 2, 15))
        with self.assertRaisesRegex(ValueError, "已支付"):
            service.approve_payment(seg1, payee="共建企业A", approved_by="街道经办", on=date(2026, 2, 16))
        adjustment = service.adjust_payment(
            payment, ADJUST_WARRANTY, -100, date(2026, 3, 1), note="质保期内返修扣款"
        )
        occupancy = service.fund_occupancy("P1", date(2026, 3, 2))[0]
        self.assertEqual(occupancy["paid"], 400)
        self.assertEqual(occupancy["committed_unpaid"], 300)
        self.assertEqual(occupancy["adjustments"], -100)
        self.assertEqual(occupancy["occupancy"], 300)
        # 原付款保持不动，调整另行登记
        self.assertEqual(service._state.payments[payment]["amount"], 400)
        self.assertEqual(service._state.adjustments[0]["adjustment_id"], adjustment)
        with self.assertRaisesRegex(ValueError, "原付款不存在"):
            service.adjust_payment("PAY-9999", ADJUST_WARRANTY, -1, date(2026, 3, 2))
        self.assertEqual(seg2, service._state.segments[seg2]["segment_id"])

    def test_handover_preserves_prior_responsibility(self) -> None:
        service, fund_id = self._service_with_fund()
        seg = service.confirm_segment(fund_id, "照明安装", 200, "灯具12套", date(2026, 2, 1))
        payment = service.approve_payment(seg, payee="共建企业A", approved_by="街道经办", on=date(2026, 2, 5))
        adjustment = service.adjust_payment(
            payment, ADJUST_WARRANTY, -50, date(2026, 3, 1), note="质保追偿"
        )
        result = service.handover_maintenance("P1", "共建企业A", "园林一队", date(2026, 6, 1))
        self.assertIn(adjustment, result["kept_liabilities"]["warranty_adjustments"])
        before = service.active_records(MAINTENANCE_RESPONSIBILITY, "P1", date(2026, 5, 15))
        after = service.active_records(MAINTENANCE_RESPONSIBILITY, "P1", date(2026, 6, 1))
        lighting_before = [r for r in before if r.payload.get("category") == "lighting"]
        lighting_after = [r for r in after if r.payload.get("category") == "lighting"]
        self.assertEqual(lighting_before[0].payload["party"], "共建企业A")
        self.assertEqual(lighting_after[0].payload["party"], "园林一队")
        # 移交后针对原付款的追偿仍记在原企业名下
        late = service.adjust_payment(payment, ADJUST_WARRANTY, -30, date(2026, 7, 1))
        self.assertEqual(service._state.adjustments[-1]["responsible"], "共建企业A")
        self.assertNotEqual(late, adjustment)


class DurabilityTest(unittest.TestCase):
    def test_state_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            service = build_service()
            # 把内存版的事件落到文件：改用文件版重放同样操作
            service = ParkService(path)
            service.register_park("P1", "街角口袋公园", ["east", "west"], START)
            service.register_record(
                INSPECTION_STANDARD, "P1", START, {"category": "children", "cycle_days": 7}
            )
            service.register_record(
                FACILITY_ASSET, "P1", START,
                {"name": "儿童滑梯", "zone_id": "east", "category": "children"},
            )
            fund_id = service.register_record(
                CONSTRUCTION_FUND, "P1", START, {"budget": 500}
            )
            fencing_id = service.place_fencing("P1", "east", "滑梯检修围挡", date(2026, 1, 5))
            _, order_id = service.submit_report(
                "P1", "居民甲", "滑梯晃动", date(2026, 1, 6), zone_id="east"
            )
            segment_id = service.confirm_segment(fund_id, "基础加固", 200, "混凝土3方", date(2026, 1, 7))
            service.compute_daily_plan("P1", date(2026, 1, 8))

            # 模拟系统停机后重启：围挡、待修设施、付款审批、当日计划保持一致
            restored = ParkService(path)
            overview = restored.park_overview("P1", date(2026, 1, 8))
            self.assertEqual([f["fencing_id"] for f in overview["active_fencing"]], [fencing_id])
            self.assertEqual([o["order_id"] for o in overview["pending_repairs"]], [order_id])
            self.assertEqual(overview["fund_occupancy"][0]["committed_unpaid"], 200)
            facility_id = overview["due_items"][0]["facility_id"]
            restored.submit_evidence("P1", facility_id, "巡检员甲", date(2026, 1, 8))
            payment = restored.approve_payment(segment_id, payee="共建企业A", approved_by="街道经办", on=date(2026, 1, 9))

            again = ParkService(path)
            self.assertEqual(again.fund_occupancy("P1", date(2026, 1, 10))[0]["paid"], 200)
            again.remove_fencing(fencing_id, date(2026, 1, 10))
            self.assertEqual(again.park_overview("P1", date(2026, 1, 10))["active_fencing"], [])
            self.assertEqual(again._state.payments[payment]["payee"], "共建企业A")


class OverviewAndReplayTest(unittest.TestCase):
    def test_overview_and_explain_as_of(self) -> None:
        service = build_service()
        ids = facility_ids(service)
        commitment = service.register_record(
            ENTERPRISE_COMMITMENT, "P1", START,
            {"party": "共建企业A", "text": "春节前补种绿篱", "due": "2026-01-05"},
        )
        kept = service.register_record(
            ENTERPRISE_COMMITMENT, "P1", START,
            {"party": "共建企业A", "text": "儿童节前检修秋千", "due": "2026-05-20"},
        )
        service.fulfill_commitment(kept, date(2026, 1, 4))
        fund_id = service.register_record(CONSTRUCTION_FUND, "P1", START, {"budget": 800})
        seg = service.confirm_segment(fund_id, "照明安装", 200, "灯具12套", date(2026, 1, 3))
        service.approve_payment(seg, payee="共建企业A", approved_by="街道经办", on=date(2026, 1, 4))
        service.submit_report("P1", "居民甲", "滑梯晃动", date(2026, 1, 6), zone_id="east")
        risk_id = service.flag_risk("P1", ["east"], date(2026, 1, 7), by="园林一队")
        service.handover_maintenance("P1", "共建企业A", "园林一队", date(2026, 2, 1))

        overview = service.park_overview("P1", date(2026, 1, 20))
        self.assertEqual(overview["available_zones"], ["west"])
        self.assertEqual(overview["next_inspection"]["date"], "2026-01-31")
        self.assertEqual(
            [c["record_id"] for c in overview["unfulfilled_commitments"]], [commitment]
        )
        self.assertEqual(overview["fund_occupancy"][0]["occupancy"], 200)

        # 争议回放：1 月 20 日为何封闭、由谁负责、居民收到什么
        replay = service.explain_as_of("P1", date(2026, 1, 20))
        self.assertEqual([z["zone"] for z in replay["closed_zones"]], ["east"])
        self.assertIn(risk_id, replay["closed_zones"][0]["risk_id"])
        self.assertEqual(replay["responsibilities"]["lighting"], "共建企业A")
        self.assertEqual(replay["handovers"], [])
        self.assertEqual(
            [n["kind"] for n in replay["notices"] if n["resident_id"] == "居民甲"],
            ["accepted"],
        )
        # 移交之后再看：接管人变了，封闭原因仍然可查
        later = service.explain_as_of("P1", date(2026, 2, 2))
        self.assertEqual(later["responsibilities"]["lighting"], "园林一队")
        self.assertEqual(later["handovers"][0]["from_party"], "共建企业A")
        self.assertEqual(later["handovers"][0]["to_party"], "园林一队")


if __name__ == "__main__":
    unittest.main()
