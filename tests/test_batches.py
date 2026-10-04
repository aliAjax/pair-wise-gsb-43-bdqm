import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class BatchChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.v1 = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "vendor1")
        self.v2 = self.service.create_vendor("proc1", "procurement", "V-102", "远山系统", "vendor2")
        self.v3 = self.service.create_vendor("proc1", "procurement", "V-103", "北辰信息", "vendor3")
        self.criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        tender = self.service.create_tender(
            "proc1", "procurement", "B-001", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), self.criteria,
        )
        tender = self.service.publish_tender("proc1", "procurement", tender["id"], tender["version"])
        self.tender_id = tender["id"]
        self.service.submit_bid("vendor1", "vendor", self.tender_id, self.v1["id"],
                                {"报价": 800000, "质量": 90}, 800000)
        self.service.submit_bid("vendor2", "vendor", self.tender_id, self.v2["id"],
                                {"报价": 700000, "质量": 80}, 700000)

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self):
        time.sleep(2.1)
        return self.service.open_bids("proc1", "procurement", self.tender_id,
                                      self.service.get_tender("proc1", "procurement", self.tender_id)["tender"]["version"])

    def _tender(self):
        return self.service.get_tender("sup", "supervisor", self.tender_id)["tender"]

    def _score(self, evaluator, bid_id, values, attempt_no=1):
        return self.service.evaluate_bid(evaluator, "evaluator", bid_id, values, attempt_no=attempt_no)

    def test_batch_freezes_roster_criteria_and_version(self):
        opened = self._open()
        self.assertEqual(2, len(opened["bids"]))
        batch = opened["batch"]
        self.assertEqual("initial", batch["reason"])
        self.assertEqual(1, batch["batch_no"])
        self.assertEqual({b["id"] for b in opened["bids"]}, {r["bid_id"] for r in batch["roster"]})
        self.assertEqual(["报价", "质量"], [c["name"] for c in batch["criteria"]])
        self.assertEqual({r["bid_id"] for r in batch["roster"]}, {b["id"] for b in opened["bids"]})
        for row in batch["roster"]:
            self.assertTrue(row["required"])

    def test_crash_retry_same_attempt_keeps_only_same_batch_rows(self):
        opened = self._open()
        bid_id = opened["bids"][0]["id"]
        batch_id = opened["batch"]["id"]

        # 模拟崩溃：同一 attempt 只写入一个评分项。
        self._score("eval1", bid_id, {"报价": 800000}, attempt_no=1)
        recovered = self._score("eval1", bid_id, {"报价": 800000, "质量": 90}, attempt_no=1)
        self.assertTrue(recovered["retry_recovered"])
        self.assertEqual(1, len(recovered["evaluations"]))  # 只补齐缺失项，不产生重复行
        # 完整重放的 attempt 再提交被拒。
        with self.assertRaises(DomainError) as ctx:
            self._score("eval1", bid_id, {"报价": 800000, "质量": 90}, attempt_no=1)
        self.assertEqual(409, ctx.exception.status)

        with self.service.connect() as conn:
            rows = conn.execute(
                "SELECT criterion,attempt_no,valid FROM evaluations WHERE batch_id=? AND bid_id=? AND evaluator='eval1' ORDER BY id",
                (batch_id, bid_id),
            ).fetchall()
        self.assertEqual([("报价", 1, 1), ("质量", 1, 1)], [tuple(r) for r in rows])

    def test_new_attempt_supersedes_old_rows(self):
        opened = self._open()
        bid_id = opened["bids"][0]["id"]
        batch_id = opened["batch"]["id"]
        self._score("eval1", bid_id, {"报价": 800000, "质量": 60}, attempt_no=1)
        with self.assertRaises(DomainError):
            self._score("eval1", bid_id, {"报价": 800000}, attempt_no=1)  # 已完整，禁止同号覆盖
        self._score("eval1", bid_id, {"报价": 780000, "质量": 95}, attempt_no=2)
        with self.service.connect() as conn:
            rows = conn.execute(
                "SELECT criterion,attempt_no,valid FROM evaluations WHERE batch_id=? AND bid_id=? AND evaluator='eval1' ORDER BY id",
                (batch_id, bid_id),
            ).fetchall()
        self.assertEqual([("报价", 1, 0), ("质量", 1, 0), ("报价", 2, 1), ("质量", 2, 1)],
                         [tuple(r) for r in rows])
        # 编号必须连续。
        with self.assertRaises(DomainError):
            self._score("eval1", bid_id, {"报价": 780000, "质量": 95}, attempt_no=4)

    def test_conflict_invalidates_only_related_scores(self):
        opened = self._open()
        bid1, bid2 = opened["bids"][0]["id"], opened["bids"][1]["id"]
        self._score("eval1", bid1, {"报价": 800000, "质量": 90})
        self._score("eval1", bid2, {"报价": 700000, "质量": 80})
        self.service.declare_conflict("eval1", "evaluator", self.tender_id, "eval1",
                                      self.v1["id"], "曾受雇于启明科技")
        detail = self.service.recover_tender("sup", "supervisor", self.tender_id)
        current = detail["current_batch"]
        self.assertEqual("reassess", current["decision"])
        self.assertEqual(2, current["invalid_evaluations"])  # 仅 bid1 两条失效
        self.assertEqual(2, current["valid_evaluations"])    # bid2 继续使用
        # 评分不全不能授标。
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup", "supervisor", self.tender_id, self._tender()["version"])
        self.assertEqual(409, ctx.exception.status)
        # eval2 无冲突，用新的重评补齐 bid1。
        self._score("eval2", bid1, {"报价": 800000, "质量": 90})
        self.service.award_tender("sup", "supervisor", self.tender_id, self._tender()["version"])

    def test_disqualification_drops_bid_but_keeps_other_results(self):
        opened = self._open()
        bid1, bid2 = opened["bids"][0]["id"], opened["bids"][1]["id"]
        self._score("eval1", bid1, {"报价": 800000, "质量": 90})
        self._score("eval1", bid2, {"报价": 700000, "质量": 80})
        bid1_version = next(b["version"] for b in self.service.get_tender("sup", "supervisor", self.tender_id)["bids"]
                            if b["id"] == bid1)
        result = self.service.disqualify_bid("proc1", "procurement", bid1, "资质造假", bid1_version)
        roster = {r["bid_id"]: r for r in result["batch"]["roster"]}
        self.assertFalse(roster[bid1]["required"])
        self.assertEqual("bid_inactive", roster[bid1]["excluded_reason"])
        self.assertTrue(roster[bid2]["required"])
        # 剩余投标评分齐全，直接可授标给 bid2。
        award = self.service.award_tender("sup", "supervisor", self.tender_id, self._tender()["version"])
        self.assertEqual(bid2, award["award"]["winner"]["bid_id"])
        with self.service.connect() as conn:
            invalid = conn.execute(
                "SELECT COUNT(*) AS c FROM evaluations WHERE bid_id=? AND valid=0", (bid1,)
            ).fetchone()["c"]
        self.assertEqual(2, invalid)

    def test_complaint_freezes_award_and_new_batch_excludes_old_scores(self):
        opened = self._open()
        bid1, bid2 = opened["bids"][0]["id"], opened["bids"][1]["id"]
        self._score("eval1", bid1, {"报价": 800000, "质量": 90})
        self._score("eval1", bid2, {"报价": 700000, "质量": 80})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender_id, "评分标准理解有误")
        frozen = complaint["frozen_batch"]
        self.assertEqual(2, frozen["batch_no"])
        self.assertIsNone(frozen["activated_at"])
        # 冻结期不能授标、不能评分。
        with self.assertRaises(DomainError):
            self.service.award_tender("sup", "supervisor", self.tender_id, self._tender()["version"])
        with self.assertRaises(DomainError) as ctx:
            self._score("eval1", bid1, {"报价": 800000, "质量": 90}, attempt_no=2)
        self.assertEqual(409, ctx.exception.status)
        self.service.resolve_complaint("sup", "supervisor", complaint["id"],
                                       "accepted", "按新规则重评")
        # 新批次旧分不可见，attempt 从 1 重新开始，且名册只含当时有效投标。
        new_eval = self._score("eval1", bid1, {"报价": 790000, "质量": 70}, attempt_no=1)
        self.assertEqual(2, new_eval["batch_id"])
        with self.assertRaises(DomainError):  # 评分不全，不能用旧批次结果凑数授标
            self.service.award_tender("sup", "supervisor", self.tender_id, self._tender()["version"])
        self._score("eval1", bid2, {"报价": 700000, "质量": 80}, attempt_no=1)
        award = self.service.award_tender("sup", "supervisor", self.tender_id, self._tender()["version"])
        self.assertEqual(2, award["award"]["round"])
        detail = self.service.recover_tender("aud", "auditor", self.tender_id)
        self.assertEqual("ended", detail["current_batch"]["decision"])

    def test_rejected_complaint_cancels_new_batch_and_resumes_old(self):
        opened = self._open()
        bid1 = opened["bids"][0]["id"]
        bid2 = opened["bids"][1]["id"]
        self._score("eval1", bid1, {"报价": 800000, "质量": 90})
        self._score("eval1", bid2, {"报价": 700000, "质量": 80})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender_id, "不成立的投诉")
        self.service.resolve_complaint("sup", "supervisor", complaint["id"],
                                       "rejected", "投诉无依据")
        detail = self.service.recover_tender("sup", "supervisor", self.tender_id)
        self.assertEqual(1, detail["current_batch"]["batch_no"])
        self.assertEqual("ready", detail["current_batch"]["decision"])
        batches = {b["batch_no"]: b["status"] for b in detail["batches"]}
        self.assertEqual({1: "completed", 2: "cancelled"}, batches)
        award = self.service.award_tender("sup", "supervisor", self.tender_id, self._tender()["version"])
        self.assertEqual(1, award["award"]["round"])

    def test_restart_replays_decisions_from_written_records(self):
        opened = self._open()
        bid1, bid2 = opened["bids"][0]["id"], opened["bids"][1]["id"]
        self._score("eval1", bid1, {"报价": 800000, "质量": 90})
        # 模拟服务重启：只凭库里记录重放。
        restarted = ProcurementService(Path(self.tmp.name) / "test.db")
        result = restarted.recover_all()
        current = result["recovered_batches"][0]
        self.assertEqual("continue", current["decision"])
        self.assertEqual(4, current["required_cells"])
        self.assertEqual(2, current["covered_cells"])
        self._score("eval1", bid2, {"报价": 700000, "质量": 80})
        ProcurementService(Path(self.tmp.name) / "test.db")  # 再重启
        detail = self.service.recover_tender("sup", "supervisor", self.tender_id)
        self.assertEqual("ready", detail["current_batch"]["decision"])

    def test_concurrent_award_only_one_succeeds(self):
        opened = self._open()
        bid1, bid2 = opened["bids"][0]["id"], opened["bids"][1]["id"]
        self._score("eval1", bid1, {"报价": 800000, "质量": 90})
        self._score("eval1", bid2, {"报价": 700000, "质量": 80})
        version = self._tender()["version"]
        # 同一版本两个授标调用：只有第一个能成功。
        award = self.service.award_tender("sup1", "supervisor", self.tender_id, version)
        self.assertEqual("awarded", award["tender"]["status"])
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup2", "supervisor", self.tender_id, version)
        self.assertEqual(409, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
