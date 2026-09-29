"""平台业务规则测试：

- 容量校验（含盐度/混养）
- 批次锁定（已结算不可改重量；订单锁定数量）
- 称重去重（设备 reading_token 唯一）
- 损耗计算（关联原批次、余量、死亡率告警、出清）
- 供应商权限（跨供应商确认被拒）
- 两名员工并发分拣同一批次仅一单成功
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from seafood.db import DB
from seafood.devices import ScaleAdapter
from seafood.monitor import TankMonitor
from seafood.seed import generate_samples, seed
from seafood.services import DomainError, QueryService, WriteService


class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        seed(self.db)
        self.w = WriteService(self.db)
        self.q = QueryService(self.db)
        self.scale = ScaleAdapter()
        # 快捷 id
        self.sp = {r["name"]: r["id"] for r in self.db.query("SELECT id,name FROM species")}
        self.tk = {r["code"]: r["id"] for r in self.db.query("SELECT id,code FROM tanks")}
        self.sup = {r["token"]: r["id"] for r in self.db.query("SELECT id,token FROM suppliers")}
        self.cust = self.db.query_one("SELECT id FROM customers LIMIT 1")["id"]
        self.emp = [r["id"] for r in self.db.query("SELECT id FROM employees ORDER BY id")]

    def intake(self, species, tank, weight, src="SRC-T-1", token="sup_lin_token",
               device="SC-1", tok=None):
        wt, dev, auto_tok = self.scale.exact(weight)
        return self.w.intake(
            species_id=self.sp[species], spec="", source_batch_no=src,
            supplier_id=self.sup[token], weight_kg=wt, tank_id=self.tk[tank],
            device_id=device, reading_token=tok or auto_tok, operator="测试员")

    # ---------------- 容量校验 ----------------
    def test_capacity_exceeded(self):
        self.intake("青斑鱼", "A-01", 100)                 # 容量 120
        with self.assertRaises(DomainError) as ctx:
            self.intake("青斑鱼", "A-01", 30)              # 100+30 > 120
        self.assertEqual(ctx.exception.code, "capacity_exceeded")
        self.assertEqual(ctx.exception.status, 409)

    def test_capacity_released_after_loss(self):
        b = self.intake("青斑鱼", "A-01", 115)
        with self.assertRaises(DomainError):
            self.intake("青斑鱼", "A-01", 10)
        self.w.record_loss(batch_id=b["id"], qty_kg=10, reason="死亡", operator="测试员")
        # 报损后有 10kg 空间（注意容量按当前存量算）
        self.intake("青斑鱼", "A-01", 10)

    def test_salinity_mismatch(self):
        with self.assertRaises(DomainError) as ctx:
            self.intake("鲈鱼", "A-01", 20)                # 鲈鱼淡水 vs A-01 海水
        self.assertEqual(ctx.exception.code, "salinity_mismatch")

    def test_mix_conflict(self):
        self.intake("南美白对虾", "B-01", 30)
        # B-01 是咸淡水，青斑鱼盐度也不匹配；改测 C-01 海水槽：花蟹 vs 生蚝
        self.intake("生蚝", "C-01", 30)
        with self.assertRaises(DomainError) as ctx:
            self.intake("花蟹", "C-01", 20)
        self.assertEqual(ctx.exception.code, "mix_conflict")
        # 冲突同时生成严重告警
        self.assertTrue(self.db.query_one(
            "SELECT 1 FROM alerts WHERE type='mix_conflict' AND status='open'"))

    # ---------------- 批次锁定 ----------------
    def test_settled_batch_weight_locked(self):
        b = self.intake("南美白对虾", "B-01", 30, src="SRC-LOCK-1", token="sup_chen_token")
        sb_id = self.db.query_one("SELECT source_batch_id FROM batches WHERE id=?", (b["id"],))["source_batch_id"]
        st = self.w.create_settlement(sb_id)
        self.w.confirm_settlement(st["id"], "sup_chen_token")
        with self.assertRaises(DomainError) as ctx:
            wt, dev, tok = self.scale.exact(29)
            self.w.reweigh(batch_id=b["id"], corrected_kg=wt, device_id=dev,
                           reading_token=tok, operator="测试员")
        self.assertEqual(ctx.exception.code, "settled_locked")

    def test_order_lock_reduces_available(self):
        b = self.intake("青斑鱼", "A-01", 40)
        self.w.create_order(customer_id=self.cust, employee_id=self.emp[0],
                            items=[{"batch_id": b["id"], "qty_kg": 30}])
        row = self.db.query_one("SELECT * FROM batches WHERE id=?", (b["id"],))
        self.assertAlmostEqual(row["locked_kg"], 30, places=3)
        # 只剩 10kg，再锁 15kg 失败
        with self.assertRaises(DomainError) as ctx:
            self.w.create_order(customer_id=self.cust, employee_id=self.emp[1],
                                items=[{"batch_id": b["id"], "qty_kg": 15}])
        self.assertEqual(ctx.exception.code, "lock_conflict")
        # 取消订单释放后可以重新锁定
        oid = self.db.query_one("SELECT id FROM orders ORDER BY id LIMIT 1")["id"]
        self.w.cancel_order(oid)
        self.w.create_order(customer_id=self.cust, employee_id=self.emp[1],
                            items=[{"batch_id": b["id"], "qty_kg": 15}])

    def test_concurrent_sorting_only_one_wins(self):
        """两名员工同时分拣同一批次：BEGIN IMMEDIATE 串行化，恰好一单成功。"""
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "conc.db")
        fdb = DB(path)
        seed(fdb)
        fw = WriteService(fdb)
        fs = ScaleAdapter()
        sp_id = fdb.query_one("SELECT id FROM species WHERE name='青斑鱼'")["id"]
        tk_id = fdb.query_one("SELECT id FROM tanks WHERE code='A-01'")["id"]
        sup_id = fdb.query_one("SELECT id FROM suppliers WHERE token='sup_lin_token'")["id"]
        wt, dev, tok = fs.exact(20)
        batch = fw.intake(species_id=sp_id, spec="", source_batch_no="SRC-CONC-1",
                          supplier_id=sup_id, weight_kg=wt, tank_id=tk_id,
                          device_id=dev, reading_token=tok, operator="仓管")
        cid = fdb.query_one("SELECT id FROM customers LIMIT 1")["id"]
        emps = [r["id"] for r in fdb.query("SELECT id FROM employees ORDER BY id")][:2]

        barrier = threading.Barrier(2)
        out = []

        def fworker(eid):
            barrier.wait()
            try:
                fw.create_order(customer_id=cid, employee_id=eid,
                                items=[{"batch_id": batch["id"], "qty_kg": 18}])
                out.append("ok")
            except DomainError:
                out.append("fail")

        with ThreadPoolExecutor(2) as ex:
            list(ex.map(fworker, emps))
        self.assertEqual(sorted(out), ["fail", "ok"])
        row = fdb.query_one("SELECT locked_kg FROM batches WHERE id=?", (batch["id"],))
        self.assertAlmostEqual(row["locked_kg"], 18, places=3)
        # 失败一单不能产生残留锁定明细
        locks = fdb.query(
            "SELECT COUNT(*) c FROM order_items oi JOIN orders o ON o.id=oi.order_id "
            "WHERE oi.batch_id=? AND oi.status='锁定'", (batch["id"],))[0]["c"]
        self.assertEqual(locks, 1)

    # ---------------- 称重去重 ----------------
    def test_duplicate_weighing_rejected(self):
        self.intake("青斑鱼", "A-01", 40, device="SC-X", tok="R-0001")
        with self.assertRaises(DomainError) as ctx:
            self.intake("青斑鱼", "A-02", 20, device="SC-X", tok="R-0001")
        self.assertEqual(ctx.exception.code, "duplicate_weighing")

    def test_reweigh_keeps_original_and_pending_diff(self):
        b = self.intake("南美白对虾", "B-01", 35, src="SRC-DIFF-1", token="sup_chen_token")
        r = self.w.reweigh(batch_id=b["id"], corrected_kg=33.8,
                           device_id="SC-2", reading_token="RW-1", operator="测试员")
        self.assertEqual(r["original_kg"], 35.0)
        self.assertEqual(r["original_device"], "SC-1")  # 原始设备保留
        self.assertAlmostEqual(r["delta_kg"], -1.2, places=3)
        self.assertEqual(r["status"], "待确认")
        # 入池原始重量不被修改
        row = self.db.query_one("SELECT weight_in_kg FROM batches WHERE id=?", (b["id"],))
        self.assertEqual(row["weight_in_kg"], 35.0)
        # 超 2% 产生称重差异告警
        self.assertTrue(self.db.query_one(
            "SELECT 1 FROM alerts WHERE type='weight_diff' AND status='open'"))

    # ---------------- 损耗计算 ----------------
    def test_loss_must_reference_batch(self):
        with self.assertRaises(DomainError) as ctx:
            self.w.record_loss(batch_id=99999, qty_kg=1, reason="死亡", operator="x")
        self.assertEqual(ctx.exception.status, 404)

    def test_loss_calculation_and_cleared(self):
        b = self.intake("青斑鱼", "A-01", 20)
        self.w.record_loss(batch_id=b["id"], qty_kg=2, reason="死亡", operator="测试员")
        # 超过可用余量（20-2=18，再报 19 失败）
        with self.assertRaises(DomainError) as ctx:
            self.w.record_loss(batch_id=b["id"], qty_kg=19, reason="死亡", operator="测试员")
        self.assertEqual(ctx.exception.code, "loss_exceeded")
        # 分拣出清 18kg 后批次状态为已出清
        o = self.w.create_order(customer_id=self.cust, employee_id=self.emp[0],
                                items=[{"batch_id": b["id"], "qty_kg": 18}])
        self.w.complete_order(o["id"])
        row = self.db.query_one("SELECT * FROM batches WHERE id=?", (b["id"],))
        self.assertEqual(row["status"], "已出清")
        self.assertAlmostEqual(row["sorted_kg"], 18, places=3)
        self.assertAlmostEqual(row["loss_kg"], 2, places=3)

    def test_mortality_threshold_alert(self):
        # 青斑鱼阈值 5%：20kg 报损 2kg = 10%
        b = self.intake("青斑鱼", "A-01", 20)
        self.w.record_loss(batch_id=b["id"], qty_kg=2, reason="缺氧", operator="测试员")
        self.assertTrue(self.db.query_one(
            "SELECT 1 FROM alerts WHERE type='mortality' AND level='严重' AND status='open'"))

    # ---------------- 供应商权限 ----------------
    def test_supplier_permissions(self):
        b = self.intake("南美白对虾", "B-01", 35, src="SRC-PERM-1", token="sup_chen_token")
        r = self.w.reweigh(batch_id=b["id"], corrected_kg=33.0,
                           device_id="SC-2", reading_token="RW-9", operator="x")
        diff_id = self.db.query_one("SELECT id FROM settlement_diffs WHERE batch_id=?",
                                    (b["id"],))["id"]
        # 无效凭证
        with self.assertRaises(DomainError) as ctx:
            self.w.confirm_diff(diff_id, "bad_token")
        self.assertEqual(ctx.exception.status, 401)
        # 林记不能确认陈记的差异
        with self.assertRaises(DomainError) as ctx:
            self.w.confirm_diff(diff_id, "sup_lin_token")
        self.assertEqual(ctx.exception.code, "forbidden")
        # 归属供应商可确认
        self.assertEqual(self.w.confirm_diff(diff_id, "sup_chen_token")["status"], "已确认")

    def test_settlement_includes_only_confirmed_diff(self):
        b = self.intake("南美白对虾", "B-01", 35, src="SRC-SET-1", token="sup_chen_token")
        sb_id = self.db.query_one("SELECT source_batch_id FROM batches WHERE id=?",
                                  (b["id"],))["source_batch_id"]
        # 未确认差异：对账单不含
        self.w.reweigh(batch_id=b["id"], corrected_kg=33.0, device_id="SC-2",
                       reading_token="RW-10", operator="x")
        st1 = self.w.create_settlement(sb_id)
        self.assertEqual(st1["diff_amount"], 0.0)
        # 对账单一旦生成（待确认）仍可再生成？已结算才拒绝 -> 这里验证待确认可重复开票无副作用
        # 供应商确认结算后来源批次锁定
        self.w.confirm_settlement(st1["id"], "sup_chen_token")
        with self.assertRaises(DomainError) as ctx:
            self.w.create_settlement(sb_id)
        self.assertEqual(ctx.exception.code, "already_settled")

    # ---------------- 监控任务 ----------------
    def test_monitor_detects_heat_and_low_oxygen(self):
        self.intake("青斑鱼", "A-01", 30)
        mon = TankMonitor(self.db, interval=60)
        mon.set_scenario(self.tk["A-01"], "heat")
        mon.scan_once()
        self.assertTrue(self.db.query_one(
            "SELECT 1 FROM alerts WHERE type='water_temp' AND tank_id=?", (self.tk["A-01"],)))
        # 第二轮同类型 open 告警卫重复
        mon.scan_once()
        n = self.db.query(
            "SELECT COUNT(*) c FROM alerts WHERE type='water_temp' AND tank_id=?",
            (self.tk["A-01"],))[0]["c"]
        self.assertEqual(n, 1)
        mon.set_scenario(self.tk["A-01"], "low_oxygen")
        mon.scan_once()
        self.assertTrue(self.db.query_one(
            "SELECT 1 FROM alerts WHERE type IN ('low_oxygen','loss_reminder') AND tank_id=?",
            (self.tk["A-01"],)))

    # ---------------- 样例脚本可运行 ----------------
    def test_sample_generation(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "demo.db")
        db2 = DB(path)
        created = generate_samples(db2)
        self.assertEqual(len(created["intakes"]), 6)
        self.assertEqual(len(created["orders"]), 1)
        self.assertEqual(len(created["losses"]), 1)
        self.assertIsNotNone(created["reweigh"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
