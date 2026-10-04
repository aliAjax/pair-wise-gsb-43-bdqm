import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
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

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(2, len(opened["bids"]))
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 80})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
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


if __name__ == "__main__":
    unittest.main()
