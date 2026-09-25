"""端到端覆盖口袋公园共建养护服务的业务约束。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.park_service import (
    CATEGORY_BARRIER_FREE,
    CATEGORY_CHILDREN,
    CATEGORY_LIGHTING,
    DomainError,
    ParkService,
)
from src.park_store import Store, deliver_pending

PARK = "p1"
DAY1 = "2026-04-01T08:00"
DAY1D = "2026-04-01"
DAY2D = "2026-04-02"
DAY8 = "2026-04-08T09:00"
DAY8D = "2026-04-08"
DAY9 = "2026-04-09T10:00"
DAY10 = "2026-04-10T10:00"


def build_world(svc: ParkService) -> None:
    """建立一座由边角地改造的全龄友好口袋公园及其生效档案。"""
    svc.register_park(PARK, "边角地口袋公园", DAY1)
    svc.put_record("land_title", "land-1", {
        "park_id": PARK, "plot_kind": "拆违腾退边角地",
        "owner": "街道办事处", "handed_to": "园林部门", "handover_at": DAY1,
    }, DAY1D)
    svc.put_record("construction_funding", "fund-1", {
        "park_id": PARK, "budget_total": 500000, "source": "财政+企业共建",
    }, DAY1D)
    for seq, (zid, name) in enumerate(
        (("z-child", "儿童活动区"), ("z-path", "无障碍主路"), ("z-lawn", "草坪休憩区")), start=1
    ):
        svc.put_record("zone", zid,
                       {"park_id": PARK, "zone_id": zid, "name": name, "seq": seq}, DAY1D)
    facilities = {
        "fac-swing": ("组合滑梯", CATEGORY_CHILDREN, "z-child"),
        "fac-ramp": ("无障碍坡道", CATEGORY_BARRIER_FREE, "z-path"),
        "fac-lamp": ("庭院照明灯", CATEGORY_LIGHTING, "z-path"),
        "fac-water": ("直饮水机", "general", "z-lawn"),
        "fac-kit": ("医药箱", "general", "z-lawn"),
    }
    for fid, (name, cat, zone) in facilities.items():
        svc.put_record("facility", fid, {
            "park_id": PARK, "name": name, "category": cat, "zone_id": zone,
            "ledger": "设施资产台账/" + fid,
        }, DAY1D)
    svc.put_record("audience", "aud-all", {
        "park_id": PARK, "ages": "全龄友好", "groups": ["儿童", "老人", "行动障碍人士"],
    }, DAY1D)
    standards = {
        CATEGORY_CHILDREN: 7,
        CATEGORY_BARRIER_FREE: 3,
        CATEGORY_LIGHTING: 1,
        "general": 30,
    }
    for cat, period in standards.items():
        svc.put_record("inspection_standard", f"std-{cat}", {
            "scope": "category", "category": cat, "period_days": period,
            "check_items": ["结构稳固", "尖角毛刺", "松动锈蚀"],
            "default_party": "园林养护一组",
        }, DAY1D)
    svc.put_record("maintenance_duty", f"duty-{PARK}", {
        "park_id": PARK, "responsible_party": "园林养护一组",
    }, DAY1D)
    svc.put_record("enterprise_commitment", "c-shade", {
        "park_id": PARK, "enterprise": "共建企业A",
        "content": "加装城市驿站遮阳帘", "due_date": "2026-05-01",
    }, DAY1D)
    svc.put_record("opening_schedule", "open-1", {
        "park_id": PARK, "open": "06:00", "close": "22:00", "note": "全年开放",
    }, DAY1D)


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.svc = ParkService(self.store)
        build_world(self.svc)

    def conn(self):
        return self.store.conn

    # -- 生效区间 -------------------------------------------------------------

    def test_records_are_effective_dated_and_as_of_queryable(self) -> None:
        current = self.svc.get_record("maintenance_duty", f"duty-{PARK}")
        self.assertEqual(current["payload"]["responsible_party"], "园林养护一组")
        self.assertIsNone(current["valid_to"])
        # 新版本生效，旧版本收口
        self.svc.put_record("maintenance_duty", f"duty-{PARK}", {
            "park_id": PARK, "responsible_party": "园林养护二组",
        }, DAY8D)
        self.assertEqual(
            self.svc.get_record("maintenance_duty", f"duty-{PARK}", "2026-04-05")["payload"][
                "responsible_party"
            ],
            "园林养护一组",
        )
        self.assertEqual(
            self.svc.get_record("maintenance_duty", f"duty-{PARK}", DAY8D)["payload"][
                "responsible_party"
            ],
            "园林养护二组",
        )
        with self.assertRaisesRegex(DomainError, "不得早于"):
            self.svc.put_record("maintenance_duty", f"duty-{PARK}", {
                "park_id": PARK, "responsible_party": " retroactive"}, "2026-03-01")

    # -- 每日算单与周期 --------------------------------------------------------

    def test_daily_generation_uses_different_periods_and_marks_overdue(self) -> None:
        first = self.svc.generate_daily_tasks(DAY1D)
        self.assertEqual(len(first["created"]), 5)  # 五类设施各一张
        self.assertEqual(first["marked_overdue"], [])

        second = self.svc.generate_daily_tasks(DAY2D)
        # 照明周期1天：次日新任务到期；前一日四张未完成任务标记逾期
        lamp_new = [
            t for t in [self._task(i) for i in second["created"]]
            if t["target_id"] == "fac-lamp"
        ]
        self.assertEqual(len(lamp_new), 1)
        self.assertEqual(lamp_new[0]["period_days"], 1)
        self.assertGreaterEqual(len(second["marked_overdue"]), 4)
        # 重复运行幂等
        again = self.svc.generate_daily_tasks(DAY2D)
        self.assertEqual(again["created"], [])

    def test_overdue_duty_is_snapshotted_and_survives_handover(self) -> None:
        self.svc.generate_daily_tasks(DAY1D)
        ramp_day1 = self._task_by_target("fac-ramp", DAY1D)
        self.assertEqual(ramp_day1["responsible_party"], "园林养护一组")
        # 04-05 企业移交养护
        self.svc.handover_maintenance(PARK, "共建企业A", "园林养护二组", "2026-04-05T00:00")
        self.svc.generate_daily_tasks(DAY8D)
        # 旧账责任不变：04-01 与补算的 04-04（移交前）仍是一组
        self.assertEqual(self._task(ramp_day1["id"])["responsible_party"], "园林养护一组")
        self.assertEqual(
            self._task_by_target("fac-ramp", "2026-04-04")["responsible_party"], "园林养护一组"
        )
        # 移交后新到期的 04-07 任务记新责任方
        ramp_new = self._task_by_target("fac-ramp", "2026-04-07")
        self.assertEqual(ramp_new["responsible_party"], "园林养护二组")
        self.assertEqual(ramp_new["status"], "overdue")

    def test_inspection_requires_evidence_and_risk_opens_high_ticket(self) -> None:
        self.svc.generate_daily_tasks(DAY1D)
        lamp = self._task_by_target("fac-lamp", DAY1D)
        with self.assertRaisesRegex(DomainError, "现场证据不能为空"):
            self.svc.submit_inspection(lamp["id"], "张巡", "risk", "  ", DAY8)
        ticket_id = self.svc.submit_inspection(
            lamp["id"], "张巡", "risk", "照片：灯罩裸露电线", DAY8, "灯杆漏电"
        )
        ticket = self.conn().execute(
            "SELECT * FROM work_ticket WHERE id=?", (ticket_id,)
        ).fetchone()
        self.assertEqual(ticket["risk_level"], "high")
        self.assertEqual(ticket["facility_id"], "fac-lamp")

    # -- 报修汇单与逐人通知 ----------------------------------------------------

    def test_similar_reports_merge_but_each_resident_keeps_own_progress(self) -> None:
        r1 = self.svc.submit_report("resident-1", "handle-1", PARK, "路灯不亮", DAY1,
                                    facility_id="fac-lamp", category=CATEGORY_LIGHTING)
        r2 = self.svc.submit_report("resident-2", "handle-2", PARK, "这片灯也不亮", DAY1,
                                    facility_id="fac-lamp", category=CATEGORY_LIGHTING)
        r3 = self.svc.submit_report("resident-3", "handle-3", PARK, "饮水机不出水", DAY1,
                                    facility_id="fac-water", category="general")
        self.assertEqual(r1["ticket_id"], r2["ticket_id"])
        self.assertEqual(r2["merged"], 1)
        self.assertNotEqual(r1["ticket_id"], r3["ticket_id"])
        self.svc.assign_ticket(r1["ticket_id"], "光明维修", DAY8)
        notices = self.conn().execute(
            "SELECT recipient_ref, event_type FROM notification_outbox WHERE ticket_id=?",
            (r1["ticket_id"],),
        ).fetchall()
        events = {(n["recipient_ref"], n["event_type"]) for n in notices}
        self.assertIn(("resident-1", "ticket_assigned"), events)
        self.assertIn(("resident-2", "ticket_assigned"), events)
        self.assertNotIn(("resident-3", "ticket_assigned"), events)
        # 每位居民只查到自己的报修与共享处置单进度
        p1 = self.svc.resident_progress("resident-1")
        p2 = self.svc.resident_progress("resident-2")
        self.assertEqual(len(p1), 1)
        self.assertEqual(len(p2), 1)
        self.assertEqual(p1[0]["ticket_id"], p2[0]["ticket_id"])
        self.assertEqual(p1[0]["content"], "路灯不亮")
        self.assertEqual(p2[0]["content"], "这片灯也不亮")

    def test_manual_merge_of_separate_tickets_keeps_every_resident_informed(self) -> None:
        # 两条未被自动并单的报修（超出设施/类别匹配，如先只报了园区）
        r1 = self.svc.submit_report("resident-1", "h1", PARK, "儿童区附近地面翘边", DAY1)
        r2 = self.svc.submit_report("resident-2", "h2", PARK, "坡道旁地面翘边", DAY1,
                                    facility_id="fac-ramp", category=CATEGORY_BARRIER_FREE)
        self.assertNotEqual(r1["ticket_id"], r2["ticket_id"])
        self.svc.merge_tickets(r1["ticket_id"], r2["ticket_id"], DAY2D)
        source = self.conn().execute(
            "SELECT status, merged_into FROM work_ticket WHERE id=?", (r1["ticket_id"],)
        ).fetchone()
        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["merged_into"], r2["ticket_id"])
        # 两单报修人都收到合并通知，且各自进度都指向目标单
        merged_notices = self.conn().execute(
            "SELECT DISTINCT recipient_ref FROM notification_outbox WHERE event_type='ticket_merged'"
        ).fetchall()
        self.assertEqual({r["recipient_ref"] for r in merged_notices},
                         {"resident-1", "resident-2"})
        self.svc.assign_ticket(r2["ticket_id"], "市政维修班", DAY8)
        for resident in ("resident-1", "resident-2"):
            progress = self.svc.resident_progress(resident)
            self.assertEqual(len(progress), 1)
            self.assertEqual(progress[0]["ticket_id"], r2["ticket_id"])
            self.assertEqual(progress[0]["assignee"], "市政维修班")
        # 已结案/跨公园不能并
        with self.assertRaisesRegex(DomainError, "已结案处置单不能合并"):
            done = self.svc.submit_report("resident-9", "h9", PARK, "水没了", DAY1,
                                          facility_id="fac-water", category="general")
            self.svc.resolve_ticket(done["ticket_id"], DAY8)
            self.svc.merge_tickets(done["ticket_id"], r2["ticket_id"], DAY8)

    # -- 风险分区封闭、异员复检、按时序开放 ------------------------------------

    def _high_risk_ticket(self) -> int:
        self.svc.generate_daily_tasks(DAY1D)
        lamp = self._task_by_target("fac-lamp", DAY1D)
        # 两位居民先报修，并入同单
        self.svc.submit_report("resident-1", "h1", PARK, "灯闪", DAY1,
                               facility_id="fac-lamp", category=CATEGORY_LIGHTING)
        ticket = self.svc.submit_inspection(
            lamp["id"], "张巡", "risk", "照片：裸露电线", DAY8, "漏电风险"
        )
        self.svc.submit_report("resident-2", "h2", PARK, "灯罩破损", DAY8,
                               facility_id="fac-lamp", category=CATEGORY_LIGHTING)
        return ticket

    def test_high_risk_closes_only_affected_zone_and_full_reopen_flow(self) -> None:
        ticket = self._high_risk_ticket()
        closure_id = self.svc.escalate_risk(
            ticket, "z-path", "照明设施漏电", "王主任", "园林应急班", DAY8, "光明维修"
        )
        # 只封受影响区域
        board = self.svc.park_dashboard(PARK, DAY8D)
        self.assertEqual(board["closed_areas"], ["z-path"])
        self.assertNotIn("无障碍主路", board["available_areas"])
        self.assertIn("儿童活动区", board["available_areas"])
        self.assertIn("草坪休憩区", board["available_areas"])
        # 重复围挡拒绝
        with self.assertRaisesRegex(DomainError, "已有未解除的封闭"):
            self.svc.escalate_risk(ticket, "z-path", "再次封闭", "王主任", "园林应急班", DAY8)
        # 两位报修居民都收到封闭通知
        closed_notices = self.conn().execute(
            "SELECT DISTINCT recipient_ref FROM notification_outbox WHERE event_type='zone_closed'"
        ).fetchall()
        self.assertEqual({r["recipient_ref"] for r in closed_notices},
                         {"resident-1", "resident-2"})

        self.svc.complete_repair(closure_id, "李工", DAY9)
        # 维修人不得复检自己的活
        with self.assertRaisesRegex(DomainError, "不得与维修人员为同一人"):
            self.svc.pass_reinspection(closure_id, "李工", True, DAY9)
        # 复检未通过：退回继续封闭
        self.svc.pass_reinspection(closure_id, "赵检", False, DAY9, "螺栓未紧固")
        state = self.conn().execute(
            "SELECT status FROM zone_closure WHERE id=?", (closure_id,)
        ).fetchone()
        self.assertEqual(state["status"], "closed")
        # 返工后复检通过
        self.svc.complete_repair(closure_id, "李工", DAY10, "返工紧固")
        self.svc.pass_reinspection(closure_id, "赵检", True, DAY10)
        # 风险解除前不得开放
        with self.assertRaisesRegex(DomainError, "不得早于风险解除"):
            self.svc.reopen_zone(closure_id, "王主任", "2026-04-10T09:59")
        # 未复检通过的高风险工单不能直接结案
        with self.assertRaisesRegex(DomainError, "复检通过并重新开放"):
            self.svc.resolve_ticket(ticket, DAY10)
        self.svc.reopen_zone(closure_id, "王主任", "2026-04-10T11:00")
        final = self.conn().execute(
            "SELECT status, barrier_installed_at, barrier_removed_at FROM zone_closure WHERE id=?",
            (closure_id,),
        ).fetchone()
        self.assertEqual(final["status"], "reopened")
        self.assertIsNotNone(final["barrier_removed_at"])
        ticket_row = self.conn().execute(
            "SELECT status FROM work_ticket WHERE id=?", (ticket,)
        ).fetchone()
        self.assertEqual(ticket_row["status"], "resolved")
        board = self.svc.park_dashboard(PARK, DAY8D)
        self.assertEqual(board["closed_areas"], [])

    # -- 分段付款、调整与质保追偿 ----------------------------------------------

    def test_installment_payments_and_adjustments_keep_original(self) -> None:
        a1 = self.svc.request_installment(PARK, "共建企业A", 1, "M1 基础工程", 100000, 60,
                                          "王主任", DAY1)
        with self.assertRaisesRegex(DomainError, "不得重复支付"):
            self.svc.request_installment(PARK, "共建企业A", 1, "M1 基础工程", 100000, 60,
                                         "王主任", DAY1)
        ledger1 = self.svc.decide_approval(a1, True, "财政科", DAY1)
        self.assertIsNotNone(ledger1)
        # 第二段在途占用
        a2 = self.svc.request_installment(PARK, "共建企业A", 2, "M2 设施安装", 50000, 30,
                                          "王主任", DAY8)
        snap = self.svc.funds_snapshot(PARK)
        self.assertEqual(snap["net_paid"], 100000)
        self.assertEqual(snap["pending_installment_reserved"], 50000)
        self.assertEqual(snap["occupied"], 150000)
        # 返工：原付款保留，另立追回调整
        adj = self.svc.request_adjustment(PARK, "共建企业A", "rework", 20000, "王主任", DAY9,
                                          related_ledger_id=ledger1, note="地面返工")
        self.svc.decide_approval(adj, True, "财政科", DAY9)
        snap = self.svc.funds_snapshot(PARK)
        self.assertEqual(snap["net_paid"], 80000)
        rows = self.conn().execute(
            "SELECT reason, signed_amount, related_ledger FROM payment_ledger ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0]["reason"], "segment")
        self.assertEqual(rows[0]["signed_amount"], 100000)  # 原付款原样保留
        self.assertEqual(rows[1]["reason"], "rework")
        self.assertEqual(rows[1]["signed_amount"], -20000)
        self.assertEqual(rows[1]["related_ledger"], ledger1)

    def test_warranty_recovery_follows_enterprise_after_handover(self) -> None:
        a1 = self.svc.request_installment(PARK, "共建企业A", 1, "M1", 100000, 60, "王主任", DAY1)
        ledger1 = self.svc.decide_approval(a1, True, "财政科", DAY1)
        # 企业移交养护
        self.svc.handover_maintenance(PARK, "共建企业A", "园林养护二组", "2026-05-01T00:00")
        # 质保期内仍向原企业追偿
        adj = self.svc.request_adjustment(PARK, "共建企业A", "warranty_recovery", 10000,
                                          "王主任", "2026-06-01T00:00",
                                          related_ledger_id=ledger1, note="滑梯质保缺陷")
        self.svc.decide_approval(adj, True, "财政科", "2026-06-01T00:00")
        self.assertEqual(self.svc.funds_snapshot(PARK)["net_paid"], 90000)
        # 承诺不因移交消失：未兑现承诺仍挂在原企业名下
        board = self.svc.park_dashboard(PARK, "2026-06-01")
        self.assertEqual(
            [c["commitment_id"] for c in board["unfulfilled_commitments"]], ["c-shade"]
        )
        self.svc.fulfill_commitment("c-shade", "2026-06-02T00:00", "已安装验收")
        board = self.svc.park_dashboard(PARK, "2026-06-02")
        self.assertEqual(board["unfulfilled_commitments"], [])

    # -- 街道看板 --------------------------------------------------------------

    def test_dashboard_shows_next_inspection_and_schedule(self) -> None:
        self.svc.generate_daily_tasks(DAY1D)
        board = self.svc.park_dashboard(PARK, DAY1D)
        self.assertEqual(board["available_areas"], ["儿童活动区", "无障碍主路", "草坪休憩区"])
        self.assertEqual(board["opening_schedule"]["open"], "06:00")
        nxt = board["next_inspection"]
        self.assertIn(nxt["target_id"], {"fac-swing", "fac-ramp", "fac-lamp", "fac-water", "fac-kit"})
        self.assertIn(nxt["status"], {"due", "overdue", "scheduled"})

    # -- 争议回溯 --------------------------------------------------------------

    def test_history_reconstructs_closure_takeover_and_notices(self) -> None:
        ticket = self._high_risk_ticket()
        closure_id = self.svc.escalate_risk(
            ticket, "z-path", "照明设施漏电", "王主任", "园林应急班", DAY8, "光明维修"
        )
        self.svc.complete_repair(closure_id, "李工", DAY9)
        self.svc.pass_reinspection(closure_id, "赵检", True, DAY9)
        self.svc.reopen_zone(closure_id, "王主任", DAY10)
        # 回到封闭当日
        hist = self.svc.history_on(PARK, DAY8D)
        self.assertEqual(len(hist["closures"]), 1)
        self.assertEqual(hist["closures"][0]["reason"], "照明设施漏电")
        self.assertEqual(hist["closures"][0]["takeover_party"], "园林应急班")
        self.assertEqual(hist["closures"][0]["state_on_date"], "封闭中")
        events = {n["event_type"] for n in hist["notices"]}
        self.assertIn("zone_closed", events)
        recipients = {n["recipient_ref"] for n in hist["notices"]}
        self.assertEqual(recipients, {"resident-1", "resident-2"})
        # 重开之后再看更早日期，仍是封闭中（历史不被改写）
        hist_after = self.svc.history_on(PARK, DAY9)
        self.assertEqual(hist_after["closures"][0]["state_on_date"], "封闭中")
        self.assertIn("zone_reopened", {n["event_type"] for n in self.svc.history_on(PARK, "2026-04-10T23:59")["notices"]})

    # -- 跨停机一致性 -----------------------------------------------------------

    def test_state_and_outbox_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "park.db"
            store = Store(db)
            svc = ParkService(store)
            build_world(svc)
            svc.generate_daily_tasks(DAY1D)
            # 走报修+封闭+在途付款，制造围挡与付款审批跨停机
            report = svc.submit_report("resident-x", "hx", PARK, "灯杆打火", DAY1,
                                       facility_id="fac-lamp", category=CATEGORY_LIGHTING)
            closure_id = svc.escalate_risk(
                report["ticket_id"], "z-path", "灯杆打火", "王主任", "园林应急班", DAY8
            )
            approval_id = svc.request_installment(PARK, "共建企业A", 1, "M1", 100000, 60,
                                                  "王主任", DAY1)
            pending_before = store.conn.execute(
                "SELECT COUNT(*) AS c FROM notification_outbox WHERE delivered_at IS NULL"
            ).fetchone()["c"]
            self.assertGreater(pending_before, 0)
            store.close()

            # 系统重启
            store2 = Store(db)
            svc2 = ParkService(store2)
            row = store2.conn.execute(
                "SELECT status FROM zone_closure WHERE id=?", (closure_id,)
            ).fetchone()
            self.assertEqual(row["status"], "closed")  # 围挡状态不丢
            approval = store2.conn.execute(
                "SELECT status FROM payment_approval WHERE id=?", (approval_id,)
            ).fetchone()
            self.assertEqual(approval["status"], "pending")  # 待审批不丢
            # 停机后继续审批并成功入账
            ledger_id = svc2.decide_approval(approval_id, True, "财政科", DAY9)
            self.assertIsNotNone(ledger_id)
            # 发件箱在重启后继续投递
            receipts: list[dict] = []
            delivered = deliver_pending(
                store2.conn, lambda msg: receipts.append(msg) or f"rcpt-{msg['id']}", DAY9
            )
            store2.conn.commit()
            self.assertGreaterEqual(len(delivered), pending_before)
            self.assertTrue(any(m["event_type"] == "zone_closed" for m in receipts))
            store2.close()

    # -- 辅助 ------------------------------------------------------------------

    def _task(self, task_id: int):
        return dict(self.conn().execute(
            "SELECT * FROM inspection_task WHERE id=?", (task_id,)
        ).fetchone())

    def _task_by_target(self, target_id: str, due_date: str):
        return dict(self.conn().execute(
            "SELECT * FROM inspection_task WHERE target_id=? AND due_date=? ORDER BY id",
            (target_id, due_date),
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
