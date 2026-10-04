import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (  # noqa: E402
    BATCH_COMPLETE,
    BATCH_CONTINUE,
    BATCH_FINISHED,
    BATCH_REEVALUATE,
    DomainError,
    ProcurementService,
)


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = ProcurementService(self.db_path)
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, number, price, quality):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": quality}, price)

    def open(self):
        time.sleep(2.1)
        return self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])

    def evaluate(self, actor, bid, price, quality):
        return self.service.evaluate_bid(actor, "evaluator", bid["id"], {"报价": price, "质量": quality})

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        opened = self.open()
        self.assertEqual(2, len(opened["bids"]))
        self.evaluate("eval1", opened["bids"][0], 800000, 90)
        self.evaluate("eval2", opened["bids"][0], 800000, 90)
        self.evaluate("eval1", opened["bids"][1], 700000, 80)
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        self.assertEqual(BATCH_COMPLETE, current["active_batch_progress"]["determination"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])
        state = self.service.batch_state("sup1", "supervisor", self.tender["id"])
        self.assertEqual(BATCH_FINISHED, state["determination"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        self.open()
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.evaluate("eval1", bid, 800000, 90)
        self.assertEqual(403, ctx.exception.status)
        self.evaluate("eval2", bid, 800000, 90)
        with self.assertRaises(DomainError) as ctx2:
            self.evaluate("eval2", bid, 800000, 90)
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        self.open()
        self.evaluate("eval1", bid, 800000, 90)
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertTrue(current["tender"]["award_frozen"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "按新规则重评")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual("reevaluation", updated["status"])
        self.assertEqual(2, updated["evaluation_round"])
        with self.assertRaises(DomainError) as ctx:
            self.service.open_bids("vendor1", "vendor", self.tender["id"], updated["version"])
        self.assertEqual(403, ctx.exception.status)

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_batch_fixes_bids_criteria_and_version_snapshots(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B5", 800000, 90)
        bid2 = self.bid(self.vendor2, "vendor2", "B6", 700000, 80)
        self.open()
        state = self.service.batch_state("sup1", "supervisor", self.tender["id"])
        self.assertEqual(1, state["batch_no"])
        self.assertEqual({bid1["id"], bid2["id"]}, {m["bid_id"] for m in state["member_bids"]})
        self.assertEqual(["报价", "质量"], state["criteria"])
        # 未评完前判定为继续
        self.assertEqual(BATCH_CONTINUE, state["determination"])
        self.assertIn(str(bid1["id"]), state["missing_scores"])

    def test_retry_attempt_number_survives_crash_and_only_same_batch_kept(self):
        bid = self.bid(self.vendor1, "vendor1", "B7", 800000, 90)
        opened = self.open()
        batch_id = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["active_batch"]["id"]

        # 模拟服务崩溃：尝试1只写入一个评分项（报价）就中断
        with self._conn() as conn:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO evaluations(batch_id,bid_id,evaluation_round,attempt_no,evaluator,criterion,
                                           raw_value,score,score_status,created_at,updated_at)
                   VALUES(?,?,1,1,'eval1','报价',800000,100,'valid',?,?)""",
                (batch_id, bid["id"], now, now),
            )

        # 重启恢复：半批尝试被标记 superseded，判定为需重评
        init_report = []
        original_recover = ProcurementService.recover

        def capturing_recover(self):
            result = original_recover(self)
            init_report.append(result)
            return result

        ProcurementService.recover = capturing_recover
        try:
            restarted = ProcurementService(self.db_path)
        finally:
            ProcurementService.recover = original_recover
        report = init_report[0]
        self.assertEqual(BATCH_REEVALUATE, report["recovered"][0]["determination"])
        self.assertEqual(1, report["recovered"][0]["orphan_rows"])
        with self._conn() as conn:
            orphan = conn.execute(
                "SELECT score_status,invalidate_reason FROM evaluations WHERE attempt_no=1"
            ).fetchone()
            self.assertEqual("superseded", orphan["score_status"])

        # 恢复后重试：只保留同一批结果，attempt_no 递增为 2，两个评分项齐全
        result = restarted.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(2, result["attempt_no"])
        self.assertEqual(batch_id, result["batch_id"])
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT criterion,score_status,attempt_no FROM evaluations WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
            self.assertEqual(["superseded", "valid", "valid"], [r["score_status"] for r in rows])
            self.assertTrue(all(r["attempt_no"] == 2 for r in rows if r["score_status"] == "valid"))

        # 再次重启：没有孤儿，判定完成
        second = ProcurementService(self.db_path)
        report2 = second.recover()
        self.assertEqual(BATCH_COMPLETE, report2["recovered"][0]["determination"])
        self.assertEqual(0, report2["recovered"][0]["orphan_rows"])

    def test_conflict_change_invalidates_only_related_scores(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B8", 800000, 90)
        bid2 = self.bid(self.vendor2, "vendor2", "B9", 700000, 80)
        self.open()
        self.evaluate("eval1", bid1, 800000, 90)
        self.evaluate("eval1", bid2, 700000, 80)
        self.evaluate("eval2", bid2, 700000, 85)

        # 开标后新增冲突：eval1 对 vendor1 的历史评分失效，其他结果保留
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "新发现亲属关系")
        with self._conn() as conn:
            bad = conn.execute(
                "SELECT score_status,invalidate_reason FROM evaluations WHERE bid_id=? AND evaluator='eval1'",
                (bid1["id"],),
            ).fetchall()
            self.assertTrue(all(r["score_status"] == "invalidated" for r in bad))
            kept = conn.execute(
                "SELECT COUNT(*) AS c FROM evaluations WHERE bid_id=? AND score_status='valid'", (bid2["id"],)
            ).fetchone()["c"]
            self.assertEqual(4, kept)  # eval1 + eval2 各两个评分项仍有效

        state = self.service.batch_state("sup1", "supervisor", self.tender["id"])
        self.assertEqual(BATCH_REEVALUATE, state["determination"])
        self.assertEqual(["报价", "质量"], state["missing_scores"][str(bid1["id"])])
        # 重评：无冲突的 eval2 补齐 bid1，bid2 结果继续使用，不重新评分
        self.evaluate("eval2", bid1, 800000, 92)
        state2 = self.service.batch_state("sup1", "supervisor", self.tender["id"])
        self.assertEqual(BATCH_COMPLETE, state2["determination"])

    def test_disqualification_invalidates_bid_scores_but_others_remain(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B10", 800000, 90)
        bid2 = self.bid(self.vendor2, "vendor2", "B11", 700000, 80)
        opened = self.open()
        self.evaluate("eval1", bid1, 800000, 90)
        self.evaluate("eval1", bid2, 700000, 80)
        target = next(b for b in opened["bids"] if b["id"] == bid1["id"])
        self.service.disqualify_bid("proc1", "procurement", bid1["id"], "资质不符", target["version"])
        with self._conn() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) AS c FROM evaluations WHERE bid_id=? AND score_status='valid'", (bid1["id"],)
            ).fetchone()["c"])
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) AS c FROM evaluations WHERE bid_id=? AND score_status='valid'", (bid2["id"],)
            ).fetchone()["c"])
        # 废标后批次仍可完成（只剩 bid2 有效），授标给 bid2
        state = self.service.batch_state("sup1", "supervisor", self.tender["id"])
        self.assertEqual(BATCH_COMPLETE, state["determination"])
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual(bid2["id"], award["award"]["winner"]["bid_id"])

    def test_award_blocked_when_batch_scores_incomplete(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B12", 800000, 90)
        bid2 = self.bid(self.vendor2, "vendor2", "B13", 700000, 80)
        self.open()
        self.evaluate("eval1", bid1, 800000, 90)
        # bid2 完全没评
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("评分不全", str(ctx.exception))

    def test_accepted_complaint_starts_new_batch_and_old_scores_excluded(self):
        bid = self.bid(self.vendor1, "vendor1", "B14", 800000, 90)
        opened = self.open()
        self.evaluate("eval1", bid, 800000, 90)
        first = self.service.batch_state("sup1", "supervisor", self.tender["id"])
        self.assertEqual(BATCH_COMPLETE, first["determination"])

        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "规则适用错误")
        self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "更换规则重评")

        # 旧批封存，新批活动且没有任何评分
        state = self.service.batch_state("sup1", "supervisor", self.tender["id"])
        self.assertEqual(2, state["batch_no"])
        self.assertEqual("active", state["status"])
        self.assertEqual(BATCH_CONTINUE, state["determination"])
        self.assertEqual(2, len(state["history"]))
        self.assertEqual("superseded", state["history"][0]["status"])
        with self._conn() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) AS c FROM evaluations WHERE batch_id=?", (state["batch_id"],)
            ).fetchone()["c"])

        # 新批未评完不能授标
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])

        # 在新批重评后授标：快照只统计新批有效分
        self.evaluate("eval1", opened["bids"][0], 750000, 70)
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual(2, award["award"]["round"])
        self.assertEqual(state["batch_id"], award["award"]["batch_id"])

        # 旧分仍留在旧批但永远不会被聚合读到
        with self._conn() as conn:
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) AS c FROM evaluations WHERE evaluation_round=1 AND score_status='valid'"
            ).fetchone()["c"])
        winner_score = award["award"]["winner"]["score"]
        self.assertGreater(winner_score, 0)

    def test_rejected_complaint_unfreezes_when_batch_complete(self):
        bid = self.bid(self.vendor1, "vendor1", "B15", 800000, 90)
        self.open()
        self.evaluate("eval1", bid, 800000, 90)
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "无理投诉")
        self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "rejected", "投诉不成立")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertFalse(current["tender"]["award_frozen"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])

    def test_restart_decides_continue_reevaluate_and_finished_from_records(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B16", 800000, 90)
        bid2 = self.bid(self.vendor2, "vendor2", "B17", 700000, 80)
        opened = self.open()
        batch_id = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["active_batch"]["id"]

        # 场景1：bid1 完整评分，bid2 未评 -> continue
        self.evaluate("eval1", bid1, 800000, 90)
        report = ProcurementService(self.db_path).recover()
        self.assertEqual(BATCH_CONTINUE, report["recovered"][0]["determination"])

        # 场景2：制造 bid1 半批崩溃残留 -> reevaluate
        with self._conn() as conn:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO evaluations(batch_id,bid_id,evaluation_round,attempt_no,evaluator,criterion,
                                           raw_value,score,score_status,created_at,updated_at)
                   VALUES(?,?,1,1,'eval9','质量',80,80,'valid',?,?)""",
                (batch_id, bid2["id"], now, now),
            )
        report2 = ProcurementService(self.db_path).recover()
        self.assertEqual(BATCH_REEVALUATE, report2["recovered"][0]["determination"])

        # 场景3：补齐后授标 -> finished
        self.service = ProcurementService(self.db_path)
        self.evaluate("eval1", bid2, 700000, 80)
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        report3 = ProcurementService(self.db_path).recover()
        self.assertEqual(BATCH_FINISHED, report3["recovered"][0]["determination"])


if __name__ == "__main__":
    unittest.main()
