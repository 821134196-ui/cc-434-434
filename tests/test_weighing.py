"""称重去重与补录:幂等键去重、补录保留原始值与设备、差异挂账、已结算禁改。"""
import unittest

from common import AquaTestCase

from app import db
from app.services import (ServiceError, confirm_settlement, entry_batch,
                          generate_settlement, report_loss, reweigh_batch,
                          sort_out, create_order)


class TestWeighing(AquaTestCase):
    def setUp(self):
        super().setUp()
        self.grouper = self.id_of("species", "石斑鱼")
        self.haifeng = self.id_of("suppliers", "海丰水产")
        self.tank = self.tank_id("A-02")

    def test_entry_idem_key_dedup(self):
        """同一秤单重复提交:只入池一次、只产生一条称重记录。"""
        r1 = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank,
                         100, device_id="SCALE-01", idem_key="SCALE-01-9001")
        r2 = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank,
                         100, device_id="SCALE-01", idem_key="SCALE-01-9001")
        self.assertFalse(r1["deduped"])
        self.assertTrue(r2["deduped"])
        self.assertEqual(r1["batch"]["id"], r2["batch"]["id"])
        n_batch = db.q1("SELECT COUNT(*) c FROM batches")["c"]
        n_weigh = db.q1("SELECT COUNT(*) c FROM weighings")["c"]
        self.assertEqual(n_batch, 1)
        self.assertEqual(n_weigh, 1)

    def test_reweigh_keeps_original_and_device(self):
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank,
                        100, device_id="SCALE-01", idem_key="K1")
        bid = r["batch"]["id"]
        out = reweigh_batch(bid, 97.0, "SCALE-02", idem_key="K2", operator="小张")
        self.assertFalse(out["deduped"])
        w = db.q1("SELECT * FROM weighings WHERE idem_key='K2'")
        self.assertEqual(w["device_id"], "SCALE-02")
        self.assertEqual(w["kind"], "补录")
        # 原入池称重仍在
        w1 = db.q1("SELECT * FROM weighings WHERE idem_key='K1'")
        self.assertEqual(w1["weight"], 100)

    def test_reweigh_diff_creates_pending_only(self):
        """差异超容忍:只生成待结算差异,不改账面重量。"""
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank, 100)
        bid = r["batch"]["id"]
        out = reweigh_batch(bid, 95.0, "SCALE-01", idem_key="K9")
        self.assertIn("diff_id", out)
        self.assertAlmostEqual(out["diff"], -5.0, places=3)
        b = self.batch(bid)
        self.assertEqual(b["current_weight"], 100, "供应商确认前账面重量不变")
        diff = db.q1("SELECT * FROM settlement_diffs WHERE batch_id=?", (bid,))
        self.assertEqual(diff["status"], "待确认")
        self.assertEqual(len(self.alerts("称重差异")), 1)

    def test_reweigh_small_diff_no_pending(self):
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank, 100)
        bid = r["batch"]["id"]
        out = reweigh_batch(bid, 99.8, "SCALE-01", idem_key="K8")
        self.assertNotIn("diff_id", out)
        n = db.q1("SELECT COUNT(*) c FROM settlement_diffs")["c"]
        self.assertEqual(n, 0)

    def test_reweigh_idem_dedup(self):
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank, 100)
        bid = r["batch"]["id"]
        reweigh_batch(bid, 95.0, "SCALE-01", idem_key="KD")
        out2 = reweigh_batch(bid, 95.0, "SCALE-01", idem_key="KD")
        self.assertTrue(out2["deduped"])
        n = db.q1("SELECT COUNT(*) c FROM settlement_diffs")["c"]
        self.assertEqual(n, 1, "重复补录不重复挂账")

    def test_settled_batch_weight_frozen(self):
        """已结算批次不能再次改重量(补录/报损均被拒)。"""
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank, 100)
        bid = r["batch"]["id"]
        st = generate_settlement(bid)["settlement"]
        admin = {"role": "admin", "supplier_id": None}
        confirm_settlement(st["id"], admin)
        with self.assertRaises(ServiceError) as ctx:
            reweigh_batch(bid, 90, "SCALE-01")
        self.assertIn("已结算", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            report_loss(bid, 1, "死亡")
        self.assertIn("已结算", ctx.exception.message)
        b = self.batch(bid)
        self.assertEqual(b["settled"], 1)
        self.assertEqual(b["status"], "已结算")


if __name__ == "__main__":
    unittest.main()
