"""损耗计算:报损关联原批次、损耗率阈值告警、结算重量扣减。"""
import unittest

from common import AquaTestCase

from app import db
from app.services import (DEATH_RATE_THRESHOLD, ServiceError, entry_batch,
                          generate_settlement, report_loss)


class TestLoss(AquaTestCase):
    def setUp(self):
        super().setUp()
        self.grouper = self.id_of("species", "石斑鱼")
        self.haifeng = self.id_of("suppliers", "海丰水产")
        self.tank = self.tank_id("A-02")
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank, 100)
        self.batch_id = r["batch"]["id"]

    def test_loss_linked_to_batch_and_updates_weights(self):
        out = report_loss(self.batch_id, 5, "死亡", "夜间死亡")
        self.assertEqual(out["loss_id"] > 0, True)
        b = self.batch(self.batch_id)
        self.assertEqual(b["loss_weight"], 5)
        self.assertEqual(b["current_weight"], 95)
        rec = db.q1("SELECT * FROM loss_records WHERE id=?", (out["loss_id"],))
        self.assertEqual(rec["batch_id"], self.batch_id)

    def test_loss_over_remaining_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            report_loss(self.batch_id, 101, "死亡")
        self.assertIn("超过批次余量", ctx.exception.message)

    def test_death_rate_threshold_alert(self):
        """累计损耗率 > 8% 触发死亡率超阈值告警与损耗提醒。"""
        report_loss(self.batch_id, 5, "死亡")
        self.assertEqual(len(self.alerts("死亡率超阈值")), 0)
        out = report_loss(self.batch_id, 4, "死亡")  # 累计 9%
        self.assertAlmostEqual(out["death_rate"], 0.09, places=3)
        self.assertTrue(out["alerts"])
        self.assertEqual(len(self.alerts("死亡率超阈值")), 1)
        self.assertEqual(len(self.alerts("损耗提醒")), 1)
        # 再次报损不重复生成同批次未处理告警
        report_loss(self.batch_id, 1, "死亡")
        self.assertEqual(len(self.alerts("死亡率超阈值")), 1)

    def test_loss_zeroes_batch_status(self):
        report_loss(self.batch_id, 100, "缺氧死亡")
        b = self.batch(self.batch_id)
        self.assertEqual(b["current_weight"], 0)
        self.assertEqual(b["status"], "已清空")

    def test_settlement_weight_deducts_loss(self):
        """结算重量 = 入池 - 损耗;金额 = 结算重量 × 单价。"""
        report_loss(self.batch_id, 10, "死亡")
        st = generate_settlement(self.batch_id)["settlement"]
        self.assertEqual(st["entry_weight"], 100)
        self.assertEqual(st["loss_weight"], 10)
        self.assertEqual(st["settle_weight"], 90)
        price = db.q1("SELECT unit_price FROM species WHERE id=?",
                      (self.grouper,))["unit_price"]
        self.assertAlmostEqual(st["amount"], round(90 * price, 2), places=2)

    def test_loss_requires_valid_batch(self):
        with self.assertRaises(ServiceError) as ctx:
            report_loss(9999, 1, "死亡")
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
