"""供应商权限:只能查看/确认本单位结算与差异;员工无权确认。"""
import unittest

from common import AquaTestCase

from app import db
from app.services import (ServiceError, confirm_settlement, entry_batch,
                          generate_settlement, list_settlements, list_pending_diffs,
                          resolve_diff, reweigh_batch)


class TestSupplierPermission(AquaTestCase):
    def setUp(self):
        super().setUp()
        self.grouper = self.id_of("species", "石斑鱼")
        self.lobster = self.id_of("species", "波士顿龙虾")
        self.haifeng = self.id_of("suppliers", "海丰水产")
        self.lanyang = self.id_of("suppliers", "蓝洋渔获")
        self.tank_a1 = self.tank_id("A-01")
        self.tank_a2 = self.tank_id("A-02")

        r1 = entry_batch(self.lobster, "500g/只", self.haifeng, "HF-1", self.tank_a1, 80)
        r2 = entry_batch(self.grouper, "750g/条", self.lanyang, "LY-1", self.tank_a2, 90)
        self.b1, self.b2 = r1["batch"]["id"], r2["batch"]["id"]
        self.st1 = generate_settlement(self.b1)["settlement"]
        self.st2 = generate_settlement(self.b2)["settlement"]

        self.u_hf = dict(db.q1("SELECT * FROM users WHERE name='海丰水产账号'"))
        self.u_ly = dict(db.q1("SELECT * FROM users WHERE name='蓝洋渔获账号'"))
        self.u_staff = dict(db.q1("SELECT * FROM users WHERE name='小张'"))
        self.u_admin = dict(db.q1("SELECT * FROM users WHERE name='管理员'"))

    def test_supplier_sees_only_own_settlements(self):
        rows_hf = list_settlements(self.u_hf)
        self.assertEqual(len(rows_hf), 1)
        self.assertEqual(rows_hf[0]["supplier_id"], self.haifeng)
        rows_admin = list_settlements(self.u_admin)
        self.assertEqual(len(rows_admin), 2)

    def test_supplier_cannot_confirm_others(self):
        with self.assertRaises(ServiceError) as ctx:
            confirm_settlement(self.st2["id"], self.u_hf)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("本单位", ctx.exception.message)

    def test_staff_cannot_confirm(self):
        with self.assertRaises(ServiceError) as ctx:
            confirm_settlement(self.st1["id"], self.u_staff)
        self.assertEqual(ctx.exception.status, 403)

    def test_supplier_confirms_own(self):
        out = confirm_settlement(self.st1["id"], self.u_hf)
        self.assertEqual(out["status"], "已结算")
        b = self.batch(self.b1)
        self.assertEqual(b["settled"], 1)

    def test_pending_diff_scoped_and_confirmed_before_settle(self):
        """补录差异挂账后:供应商须先确认差异才能确认结算;越权处理被拒。"""
        reweigh_batch(self.b1, 74.0, "SCALE-01", idem_key="DK1")  # 差异 -6kg
        generate_settlement(self.b1)  # 关联差异到结算单
        diff = db.q1("SELECT * FROM settlement_diffs WHERE batch_id=?", (self.b1,))
        self.assertEqual(diff["status"], "待确认")

        # 待确认差异未处理,不能确认结算
        with self.assertRaises(ServiceError) as ctx:
            confirm_settlement(self.st1["id"], self.u_hf)
        self.assertIn("待确认差异", ctx.exception.message)

        # 其他供应商不能处理该差异
        with self.assertRaises(ServiceError) as ctx:
            resolve_diff(diff["id"], True, self.u_ly)
        self.assertEqual(ctx.exception.status, 403)

        # 本供应商确认差异 → 计入结算重量 → 可确认结算
        resolve_diff(diff["id"], True, self.u_hf)
        st = db.q1("SELECT * FROM settlements WHERE id=?", (self.st1["id"],))
        self.assertAlmostEqual(st["settle_weight"], 80 - 6, places=3)
        out = confirm_settlement(self.st1["id"], self.u_hf)
        self.assertEqual(out["status"], "已结算")

    def test_pending_diffs_list_scoped(self):
        reweigh_batch(self.b1, 74.0, "SCALE-01", idem_key="DK2")
        reweigh_batch(self.b2, 85.0, "SCALE-01", idem_key="DK3")
        diffs_hf = list_pending_diffs(self.u_hf)
        self.assertEqual(len(diffs_hf), 1)
        self.assertEqual(diffs_hf[0]["batch_id"], self.b1)
        diffs_admin = list_pending_diffs(self.u_admin)
        self.assertEqual(len(diffs_admin), 2)


if __name__ == "__main__":
    unittest.main()
