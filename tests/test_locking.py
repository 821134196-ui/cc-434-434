"""批次锁定:两名员工同时分拣同一批次只允许一单成功;订单取消释放锁定。"""
import threading
import unittest

from common import AquaTestCase

from app import db
from app.services import (ServiceError, cancel_order, create_order, entry_batch,
                          sort_out)


class TestBatchLocking(AquaTestCase):
    def setUp(self):
        super().setUp()
        self.grouper = self.id_of("species", "石斑鱼")
        self.haifeng = self.id_of("suppliers", "海丰水产")
        self.customer = self.id_of("customers", "悦海酒楼")
        self.tank = self.tank_id("A-02")  # 300kg, 盐度25
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank, 100)
        self.batch_id = r["batch"]["id"]

    def _make_item(self, qty):
        o = create_order(self.customer,
                         [{"species_id": self.grouper, "spec": "750g/条", "qty": qty}])
        return db.q1("SELECT * FROM order_items WHERE order_id=?", (o["order_id"],))

    def test_concurrent_dedup_double_sort_only_one_wins(self):
        """两名员工同时分拣同一批次(余量 100kg,各要 80kg):只允许一单成功。"""
        item = self._make_item(160)
        results, errors = [], []

        def worker(name):
            try:
                results.append(sort_out(item["id"], self.batch_id, 80, name))
            except ServiceError as e:
                errors.append(e)

        t1 = threading.Thread(target=worker, args=("小张",))
        t2 = threading.Thread(target=worker, args=("小李",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(results), 1, "只能有一单分拣成功")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)
        b = self.batch(self.batch_id)
        self.assertAlmostEqual(b["current_weight"], 20.0, places=3)
        self.assertAlmostEqual(b["out_weight"], 80.0, places=3)

    def test_over_remaining_rejected(self):
        item = self._make_item(200)
        with self.assertRaises(ServiceError) as ctx:
            sort_out(item["id"], self.batch_id, 120, "小张")
        self.assertEqual(ctx.exception.status, 409)

    def test_over_order_item_rejected(self):
        item = self._make_item(30)
        with self.assertRaises(ServiceError) as ctx:
            sort_out(item["id"], self.batch_id, 40, "小张")
        self.assertIn("超量分拣", ctx.exception.message)

    def test_cancel_order_releases_lock(self):
        item = self._make_item(40)
        sort_out(item["id"], self.batch_id, 40, "小张")
        self.assertAlmostEqual(self.batch(self.batch_id)["current_weight"], 60.0)
        cancel_order(item["order_id"], operator="管理员")
        b = self.batch(self.batch_id)
        self.assertAlmostEqual(b["current_weight"], 100.0, places=3)
        self.assertAlmostEqual(b["out_weight"], 0.0, places=3)
        order = db.q1("SELECT * FROM orders WHERE id=?", (item["order_id"],))
        self.assertEqual(order["status"], "已取消")
        self.assertEqual(len(self.alerts("订单取消")), 1)

    def test_sort_cancelled_order_rejected(self):
        item = self._make_item(40)
        cancel_order(item["order_id"], operator="管理员")
        with self.assertRaises(ServiceError) as ctx:
            sort_out(item["id"], self.batch_id, 10, "小张")
        self.assertIn("已取消", ctx.exception.message)

    def test_species_mismatch_rejected(self):
        shrimp = self.id_of("species", "基围虾")
        o = create_order(self.customer,
                         [{"species_id": shrimp, "spec": "40头/斤", "qty": 10}])
        item = db.q1("SELECT * FROM order_items WHERE order_id=?", (o["order_id"],))
        with self.assertRaises(ServiceError) as ctx:
            sort_out(item["id"], self.batch_id, 10, "小张")
        self.assertIn("不匹配", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
