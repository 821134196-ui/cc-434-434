"""容量校验:水槽容量、盐度适配、混养冲突。"""
import unittest

from common import AquaTestCase

from app import db
from app.services import ServiceError, entry_batch


class TestCapacity(AquaTestCase):
    def setUp(self):
        super().setUp()
        self.grouper = self.id_of("species", "石斑鱼")      # 盐度 24~32
        self.lobster = self.id_of("species", "波士顿龙虾")  # 盐度 22~30
        self.crab = self.id_of("species", "梭子蟹")
        self.haifeng = self.id_of("suppliers", "海丰水产")
        self.tank_a3 = self.tank_id("A-03")   # 容量 200kg, 盐度 30
        self.tank_b3 = self.tank_id("B-03")   # 容量 150kg, 盐度 12

    def test_capacity_overflow_rejected(self):
        entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank_a3, 150)
        with self.assertRaises(ServiceError) as ctx:
            entry_batch(self.grouper, "750g/条", self.haifeng, "S2", self.tank_a3, 60)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("容量不足", ctx.exception.message)

    def test_capacity_exact_fit_ok(self):
        entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank_a3, 120)
        r = entry_batch(self.grouper, "750g/条", self.haifeng, "S2", self.tank_a3, 80)
        self.assertEqual(r["batch"]["current_weight"], 80)

    def test_salinity_mismatch_rejected(self):
        # 石斑鱼要求 24~32‰,B-03 盐度 12‰
        with self.assertRaises(ServiceError) as ctx:
            entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank_b3, 10)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("盐度不适配", ctx.exception.message)

    def test_mixed_species_conflict_rejected(self):
        entry_batch(self.lobster, "500g/只", self.haifeng, "S1", self.tank_a3, 50)
        with self.assertRaises(ServiceError) as ctx:
            entry_batch(self.crab, "300g/只", self.haifeng, "S2", self.tank_a3, 30)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("混养冲突", ctx.exception.message)
        # 冲突应产生告警记录
        alerts = self.alerts("混养冲突")
        self.assertEqual(len(alerts), 1)

    def test_same_species_no_conflict(self):
        entry_batch(self.lobster, "500g/只", self.haifeng, "S1", self.tank_a3, 50)
        r = entry_batch(self.lobster, "400g/只", self.haifeng, "S2", self.tank_a3, 40)
        self.assertTrue(r["batch"]["id"])

    def test_invalid_weight_rejected(self):
        with self.assertRaises(ServiceError):
            entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank_a3, 0)
        with self.assertRaises(ServiceError):
            entry_batch(self.grouper, "750g/条", self.haifeng, "S1", self.tank_a3, -5)


if __name__ == "__main__":
    unittest.main()
