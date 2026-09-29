"""领域服务：入池校验、批次锁定、称重去重、损耗、供应商结算。

所有写规则集中在这里，HTTP 层与测试共用。
"""
from __future__ import annotations

from datetime import datetime

from .db import DB


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DomainError(Exception):
    """业务规则违反（HTTP 层映射为 400/403/409）。"""

    def __init__(self, message: str, code: str = "domain_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------- 数据读取
class QueryService:
    def __init__(self, db: DB):
        self.db = db

    def tanks_overview(self) -> list[dict]:
        tanks = self.db.query(
            """SELECT t.*, s.name stall_name FROM tanks t
               JOIN stalls s ON s.id=t.stall_id ORDER BY t.zone, t.code"""
        )
        out = []
        for t in tanks:
            d = dict(t)
            batches = self.db.query(
                """SELECT b.*, sp.name species_name FROM batches b
                   JOIN species sp ON sp.id=b.species_id
                   WHERE b.tank_id=? AND b.status='在池' ORDER BY b.id""",
                (t["id"],),
            )
            d["batches"] = [dict(b) for b in batches]
            d["load_kg"] = round(
                sum(
                    b["weight_in_kg"] - b["sorted_kg"] - b["loss_kg"]
                    for b in batches
                ),
                3,
            )
            d["occupancy"] = (
                round(d["load_kg"] / d["capacity_kg"] * 100, 1)
                if d["capacity_kg"] else 0
            )
            d["abnormal"] = self._tank_abnormal(d)
            out.append(d)
        return out

    def _tank_abnormal(self, tank: dict) -> dict | None:
        for b in tank["batches"]:
            sp = self.db.query_one("SELECT * FROM species WHERE id=?", (b["species_id"],))
            if tank["probe_temp"] is not None and not (
                sp["temp_min"] <= tank["probe_temp"] <= sp["temp_max"]
            ):
                return {"type": "water_temp", "message": f"水温 {tank['probe_temp']}℃ 超出 {sp['name']} 适温 {sp['temp_min']}~{sp['temp_max']}℃"}
            if tank["probe_oxygen"] is not None and tank["probe_oxygen"] < sp["min_oxygen"]:
                return {"type": "low_oxygen", "message": f"溶氧 {tank['probe_oxygen']}mg/L 低于 {sp['name']} 要求 {sp['min_oxygen']}mg/L"}
        return None

    def batches(self, status: str | None = None) -> list[dict]:
        sql = """SELECT b.*, sp.name species_name, sp.unit_price, sp.mortality_threshold,
                        t.code tank_code, t.zone, sb.batch_no source_no, su.name supplier_name,
                        sb.settled source_settled
                 FROM batches b
                 JOIN species sp ON sp.id=b.species_id
                 JOIN tanks t ON t.id=b.tank_id
                 JOIN source_batches sb ON sb.id=b.source_batch_id
                 JOIN suppliers su ON su.id=sb.supplier_id"""
        if status:
            rows = self.db.query(sql + " WHERE b.status=? ORDER BY b.id", (status,))
        else:
            rows = self.db.query(sql + " ORDER BY b.id", ())
        out = []
        for r in rows:
            d = dict(r)
            d["available_kg"] = round(r["weight_in_kg"] - r["sorted_kg"] - r["loss_kg"] - r["locked_kg"], 3)
            d["mortality_rate"] = round(r["loss_kg"] / r["weight_in_kg"], 4) if r["weight_in_kg"] else 0
            out.append(d)
        return out

    def holding_logs(self, tank_id: int | None = None, limit: int = 100) -> list[dict]:
        sql = """SELECT h.*, t.code tank_code FROM holding_logs h
                 JOIN tanks t ON t.id=h.tank_id"""
        if tank_id:
            rows = self.db.query(sql + " WHERE h.tank_id=? ORDER BY h.id DESC LIMIT ?", (tank_id, limit))
        else:
            rows = self.db.query(sql + " ORDER BY h.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def losses(self) -> list[dict]:
        rows = self.db.query(
            """SELECT l.*, b.batch_no, sp.name species_name, t.code tank_code,
                      sb.batch_no source_no
               FROM losses l
               JOIN batches b ON b.id=l.batch_id
               JOIN species sp ON sp.id=b.species_id
               JOIN tanks t ON t.id=b.tank_id
               JOIN source_batches sb ON sb.id=b.source_batch_id
               ORDER BY l.id DESC""")
        return [dict(r) for r in rows]

    def orders(self) -> list[dict]:
        orders = self.db.query(
            """SELECT o.*, c.name customer_name, e.name employee_name
               FROM orders o JOIN customers c ON c.id=o.customer_id
               LEFT JOIN employees e ON e.id=o.locked_by ORDER BY o.id DESC""")
        out = []
        for o in orders:
            d = dict(o)
            d["items"] = [dict(r) for r in self.db.query(
                """SELECT oi.*, b.batch_no, sp.name species_name, sp.spec
                   FROM order_items oi JOIN batches b ON b.id=oi.batch_id
                   JOIN species sp ON sp.id=b.species_id WHERE oi.order_id=?""",
                (o["id"],))]
            out.append(d)
        return out

    def settlements(self) -> list[dict]:
        rows = self.db.query(
            """SELECT st.*, su.name supplier_name, sb.batch_no source_no
               FROM settlements st JOIN suppliers su ON su.id=st.supplier_id
               JOIN source_batches sb ON sb.id=st.source_batch_id
               ORDER BY st.id DESC""")
        return [dict(r) for r in rows]

    def pending_diffs(self) -> list[dict]:
        rows = self.db.query(
            """SELECT d.*, b.batch_no, su.name supplier_name
               FROM settlement_diffs d JOIN batches b ON b.id=d.batch_id
               JOIN source_batches sb ON sb.id=d.source_batch_id
               JOIN suppliers su ON su.id=sb.supplier_id
               ORDER BY d.id DESC""")
        return [dict(r) for r in rows]

    def alerts(self, only_open: bool = True) -> list[dict]:
        sql = """SELECT a.*, t.code tank_code, b.batch_no FROM alerts a
                 LEFT JOIN tanks t ON t.id=a.tank_id
                 LEFT JOIN batches b ON b.id=a.batch_id
                 """ + ("WHERE a.status='open' " if only_open else "") + "ORDER BY a.id DESC LIMIT 100"
        return [dict(r) for r in self.db.query(sql)]


# ---------------------------------------------------------------- 写操作
class WriteService:
    WEIGHT_DIFF_ALERT_RATE = 0.02  # 补录与入池差异超 2% 提示称重差异

    def __init__(self, db: DB):
        self.db = db

    def _alert(self, conn, type_: str, message: str, tank_id=None, batch_id=None,
               level: str = "警示") -> None:
        conn.execute(
            "INSERT INTO alerts(type,level,tank_id,batch_id,message,created_at) VALUES(?,?,?,?,?,?)",
            (type_, level, tank_id, batch_id, message, now()),
        )

    # ---------------- 入池 ----------------
    def intake(self, *, species_id: int, spec: str, source_batch_no: str, supplier_id: int,
               weight_kg: float, tank_id: int, device_id: str, reading_token: str,
               operator: str, note: str = "") -> dict:
        if weight_kg <= 0:
            raise DomainError("入池重量必须大于 0", "bad_weight", 400)
        try:
            with self.db.transaction() as conn:
                return self._intake_tx(conn, species_id, spec, source_batch_no, supplier_id,
                                       weight_kg, tank_id, device_id, reading_token, operator, note)
        except DomainError as e:
            # 混养冲突即使入池被拒，也单独留一条严重告警（原事务已回滚）
            if e.code == "mix_conflict":
                with self.db.transaction() as conn:
                    self._alert(conn, "mix_conflict", str(e), tank_id=tank_id, level="严重")
            raise

    def _intake_tx(self, conn, species_id, spec, source_batch_no, supplier_id,
                   weight_kg, tank_id, device_id, reading_token, operator, note) -> dict:
        sp = conn.execute("SELECT * FROM species WHERE id=?", (species_id,)).fetchone()
        tank = conn.execute("SELECT * FROM tanks WHERE id=?", (tank_id,)).fetchone()
        supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not sp:
            raise DomainError("品种不存在", "not_found", 404)
        if not tank:
            raise DomainError("水槽不存在", "not_found", 404)
        if not supplier:
            raise DomainError("供应商不存在", "not_found", 404)

        # 1) 盐度要求校验
        if sp["salinity"] != tank["salinity"]:
            raise DomainError(
                f"盐度不匹配：{sp['name']} 要求 {sp['salinity']}，水槽 {tank['code']} 为 {tank['salinity']}",
                "salinity_mismatch", 409)

        # 2) 容量校验（现存重量 = 入池 - 已分拣 - 损耗）
        row = conn.execute(
            """SELECT COALESCE(SUM(weight_in_kg-sorted_kg-loss_kg),0) load_kg
               FROM batches WHERE tank_id=? AND status='在池'""", (tank_id,)).fetchone()
        if row["load_kg"] + weight_kg > tank["capacity_kg"] + 1e-9:
            raise DomainError(
                f"水槽 {tank['code']} 容量不足：现有 {row['load_kg']}kg + 本次 {weight_kg}kg "
                f"> 容量 {tank['capacity_kg']}kg", "capacity_exceeded", 409)

        # 3) 混养冲突校验
        others = conn.execute(
            """SELECT DISTINCT species_id FROM batches WHERE tank_id=? AND status='在池'""",
            (tank_id,)).fetchall()
        for o in others:
            if o["species_id"] == species_id:
                continue
            clash = conn.execute(
                """SELECT note FROM species_conflicts
                   WHERE (species_a=? AND species_b=?) OR (species_a=? AND species_b=?)""",
                (species_id, o["species_id"], o["species_id"], species_id)).fetchone()
            if clash:
                msg = f"混养冲突：{sp['name']} 与水槽内既有品种不可混养（{clash['note'] or '习性冲突'}）"
                # 告警由 intake() 在事务回滚后补写，保证拒绝入池也留痕
                raise DomainError(msg, "mix_conflict", 409)

        sb = conn.execute("SELECT * FROM source_batches WHERE batch_no=?",
                          (source_batch_no,)).fetchone()
        if sb is None:
            conn.execute(
                "INSERT INTO source_batches(batch_no,supplier_id,arrived_at) VALUES(?,?,?)",
                (source_batch_no, supplier_id, now()))
            sb = conn.execute("SELECT * FROM source_batches WHERE batch_no=?",
                              (source_batch_no,)).fetchone()

        batch_no = f"P{sb['id']:03d}-B{conn.execute('SELECT COALESCE(MAX(id),0)+1 n FROM batches').fetchone()['n']:03d}"
        conn.execute(
            """INSERT INTO batches(batch_no,source_batch_id,species_id,spec,tank_id,
               weight_in_kg,remaining_kg,arrived_at) VALUES(?,?,?,?,?,?,?,?)""",
            (batch_no, sb["id"], species_id, spec or sp["spec"], tank_id,
             weight_kg, weight_kg, now()))
        batch_id = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]

        # 4) 入池称重（设备读数令牌去重）
        self._insert_weighing(conn, batch_id, "入池", weight_kg, device_id,
                              reading_token, operator, note)
        conn.execute(
            "INSERT INTO holding_logs(tank_id,batch_id,temp,oxygen,recorded_at) VALUES(?,?,?,?,?)",
            (tank_id, batch_id, tank["probe_temp"], tank["probe_oxygen"], now()))
        return dict(conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone())

    def _insert_weighing(self, conn, batch_id, kind, weight_kg, device_id,
                         reading_token, operator, note) -> int:
        dup = conn.execute(
            "SELECT id FROM weighings WHERE device_id=? AND reading_token=?",
            (device_id, reading_token)).fetchone()
        if dup:
            raise DomainError(
                f"设备 {device_id} 读数 {reading_token} 已存在（称重记录 #{dup['id']}），请勿重复提交",
                "duplicate_weighing", 409)
        conn.execute(
            """INSERT INTO weighings(batch_id,kind,weight_kg,device_id,reading_token,
               operator,note,recorded_at) VALUES(?,?,?,?,?,?,?,?)""",
            (batch_id, kind, weight_kg, device_id, reading_token, operator, note, now()))
        return conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]

    # ---------------- 补录称重（保留原始值与设备） ----------------
    def reweigh(self, *, batch_id: int, corrected_kg: float, device_id: str,
                reading_token: str, operator: str) -> dict:
        if corrected_kg <= 0:
            raise DomainError("补录重量必须大于 0", "bad_weight", 400)
        with self.db.transaction() as conn:
            b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not b:
                raise DomainError("批次不存在", "not_found", 404)
            sb = conn.execute("SELECT * FROM source_batches WHERE id=?",
                              (b["source_batch_id"],)).fetchone()
            # 已结算批次不能再次改重量
            if sb["settled"]:
                raise DomainError(
                    f"来源批次 {sb['batch_no']} 已完成结算，批次 {b['batch_no']} 重量不可再修改",
                    "settled_locked", 409)
            original = conn.execute(
                "SELECT * FROM weighings WHERE batch_id=? AND kind='入池' ORDER BY id LIMIT 1",
                (batch_id,)).fetchone()
            original_kg = original["weight_kg"]
            original_device = original["device_id"]
            wid = self._insert_weighing(conn, batch_id, "补录", corrected_kg,
                                        device_id, reading_token, operator,
                                        f"原始入池读数 {original_kg}kg（设备 {original_device}）")
            delta = round(corrected_kg - original_kg, 3)
            sp = conn.execute("SELECT * FROM species WHERE id=?", (b["species_id"],)).fetchone()
            amount = round(delta * sp["unit_price"], 2)
            conn.execute(
                """INSERT INTO settlement_diffs(batch_id,source_batch_id,original_kg,
                   corrected_kg,delta_kg,amount,created_at) VALUES(?,?,?,?,?,?,?)""",
                (batch_id, b["source_batch_id"], original_kg, corrected_kg, delta, amount, now()))
            # 差异超阈值 -> 称重差异告警（但供应商确认前不改动任何账）
            if abs(delta) > original_kg * self.WEIGHT_DIFF_ALERT_RATE:
                self._alert(conn, "weight_diff",
                            f"批次 {b['batch_no']} 补录 {corrected_kg}kg 与入池 {original_kg}kg "
                            f"差异 {delta:+.3f}kg（超 {self.WEIGHT_DIFF_ALERT_RATE:.0%}），待供应商确认",
                            batch_id=batch_id, level="警示")
            return {"weighing_id": wid, "original_kg": original_kg,
                    "original_device": original_device, "corrected_kg": corrected_kg,
                    "delta_kg": delta, "diff_amount": amount, "status": "待确认"}

    # ---------------- 死亡 / 损耗（必须关联原批次） ----------------
    def record_loss(self, *, batch_id: int, qty_kg: float, reason: str,
                    operator: str, device_id: str | None = None) -> dict:
        if qty_kg <= 0:
            raise DomainError("损耗重量必须大于 0", "bad_weight", 400)
        if reason not in ("死亡", "缺氧", "逃逸", "失重"):
            raise DomainError("损耗原因必须是 死亡/缺氧/逃逸/失重", "bad_reason", 400)
        with self.db.transaction() as conn:
            b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not b:
                raise DomainError("批次不存在，损耗必须关联原批次", "not_found", 404)
            available = b["weight_in_kg"] - b["sorted_kg"] - b["loss_kg"] - b["locked_kg"]
            if qty_kg > available + 1e-9:
                raise DomainError(
                    f"损耗 {qty_kg}kg 超过批次 {b['batch_no']} 可用余量 {round(available,3)}kg",
                    "loss_exceeded", 409)
            conn.execute(
                """INSERT INTO losses(batch_id,qty_kg,reason,device_id,operator,recorded_at)
                   VALUES(?,?,?,?,?,?)""",
                (batch_id, qty_kg, reason, device_id, operator, now()))
            loss_id = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            new_loss = round(b["loss_kg"] + qty_kg, 3)
            conn.execute("UPDATE batches SET loss_kg=? WHERE id=?", (new_loss, batch_id))
            if new_loss + b["sorted_kg"] >= b["weight_in_kg"] - 1e-9:
                conn.execute("UPDATE batches SET status='已出清' WHERE id=?", (batch_id,))
            sp = conn.execute("SELECT * FROM species WHERE id=?", (b["species_id"],)).fetchone()
            rate = new_loss / b["weight_in_kg"] if b["weight_in_kg"] else 0
            # 死亡率超阈值
            if rate > sp["mortality_threshold"]:
                self._alert(conn, "mortality",
                            f"批次 {b['batch_no']} {sp['name']} 死亡率 {rate:.1%} 超阈值 "
                            f"{sp['mortality_threshold']:.0%}（累计损耗 {new_loss}kg）",
                            tank_id=b["tank_id"], batch_id=batch_id, level="严重")
            return dict(conn.execute("SELECT * FROM losses WHERE id=?", (loss_id,)).fetchone())

    # ---------------- 订单锁定 / 分拣 ----------------
    def create_order(self, *, customer_id: int, employee_id: int,
                     items: list[dict]) -> dict:
        if not items:
            raise DomainError("订单至少包含一个分拣明细", "empty_order", 400)
        with self.db.transaction() as conn:
            cust = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            emp = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not cust:
                raise DomainError("客户不存在", "not_found", 404)
            if not emp:
                raise DomainError("员工不存在", "not_found", 404)
            # 汇总同批次数量
            agg: dict[int, float] = {}
            for it in items:
                qty = float(it["qty_kg"])
                if qty <= 0:
                    raise DomainError("分拣数量必须大于 0", "bad_weight", 400)
                agg[int(it["batch_id"])] = agg.get(int(it["batch_id"]), 0) + qty

            # 关键：BEGIN IMMEDIATE 下串行校验，两名员工同时分拣同批次只有一单成功
            for bid, qty in agg.items():
                b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
                if not b:
                    raise DomainError(f"批次 #{bid} 不存在", "not_found", 404)
                available = b["weight_in_kg"] - b["sorted_kg"] - b["loss_kg"] - b["locked_kg"]
                if qty > available + 1e-9:
                    raise DomainError(
                        f"批次 {b['batch_no']} 可锁余量不足：需要 {qty}kg，仅剩 {round(available,3)}kg"
                        "（可能正被另一员工分拣）", "lock_conflict", 409)

            n = conn.execute("SELECT COALESCE(MAX(id),0)+1 n FROM orders").fetchone()["n"]
            order_no = f"SO{n:04d}"
            conn.execute(
                """INSERT INTO orders(order_no,customer_id,status,locked_by,created_at)
                   VALUES(?,?,?,?,?)""",
                (order_no, customer_id, "分拣中", employee_id, now()))
            oid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            for bid, qty in agg.items():
                conn.execute(
                    "INSERT INTO order_items(order_id,batch_id,qty_kg,status) VALUES(?,?,?,'锁定')",
                    (oid, bid, qty))
                conn.execute("UPDATE batches SET locked_kg=locked_kg+? WHERE id=?", (qty, bid))
            return {"id": oid, "order_no": order_no, "status": "分拣中"}

    def complete_order(self, order_id: int) -> dict:
        with self.db.transaction() as conn:
            o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not o:
                raise DomainError("订单不存在", "not_found", 404)
            if o["status"] != "分拣中":
                raise DomainError(f"订单状态为 {o['status']}，不能确认出池", "bad_state", 409)
            for it in conn.execute("SELECT * FROM order_items WHERE order_id=? AND status='锁定'",
                                   (order_id,)).fetchall():
                conn.execute(
                    """UPDATE batches SET sorted_kg=sorted_kg+?, locked_kg=locked_kg-?,
                       remaining_kg=remaining_kg-? WHERE id=?""",
                    (it["qty_kg"], it["qty_kg"], it["qty_kg"], it["batch_id"]))
                conn.execute("UPDATE order_items SET status='已出池' WHERE id=?", (it["id"],))
                b = conn.execute("SELECT * FROM batches WHERE id=?", (it["batch_id"],)).fetchone()
                if b["sorted_kg"] + b["loss_kg"] >= b["weight_in_kg"] - 1e-9:
                    conn.execute("UPDATE batches SET status='已出清' WHERE id=?", (b["id"],))
            conn.execute("UPDATE orders SET status='已完成', sorted_at=? WHERE id=?", (now(), order_id))
            return {"id": order_id, "status": "已完成"}

    def cancel_order(self, order_id: int, reason: str = "客户取消") -> dict:
        with self.db.transaction() as conn:
            o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not o:
                raise DomainError("订单不存在", "not_found", 404)
            if o["status"] == "已完成":
                raise DomainError("已完成订单不能取消", "bad_state", 409)
            if o["status"] == "已取消":
                return {"id": order_id, "status": "已取消"}
            for it in conn.execute("SELECT * FROM order_items WHERE order_id=? AND status='锁定'",
                                   (order_id,)).fetchall():
                conn.execute("UPDATE batches SET locked_kg=locked_kg-? WHERE id=?",
                             (it["qty_kg"], it["batch_id"]))
                conn.execute("UPDATE order_items SET status='已释放' WHERE id=?", (it["id"],))
                self._alert(conn, "order_cancel",
                            f"订单 {o['order_no']} 取消，释放批次 {it['batch_id']} 锁定 {it['qty_kg']}kg",
                            batch_id=it["batch_id"], level="警示")
            conn.execute("UPDATE orders SET status='已取消', cancel_reason=? WHERE id=?",
                         (reason, order_id))
            return {"id": order_id, "status": "已取消"}

    # ---------------- 供应商结算与权限 ----------------
    def _supplier_by_token(self, conn, token: str):
        su = conn.execute("SELECT * FROM suppliers WHERE token=?", (token,)).fetchone()
        if not su:
            raise DomainError("供应商凭证无效", "unauthorized", 401)
        return su

    def confirm_diff(self, diff_id: int, token: str) -> dict:
        with self.db.transaction() as conn:
            d = conn.execute("SELECT * FROM settlement_diffs WHERE id=?", (diff_id,)).fetchone()
            if not d:
                raise DomainError("差异单不存在", "not_found", 404)
            su = self._supplier_by_token(conn, token)
            sb = conn.execute("SELECT * FROM source_batches WHERE id=?",
                              (d["source_batch_id"],)).fetchone()
            # 供应商只能确认自己批次的差异
            if sb["supplier_id"] != su["id"]:
                raise DomainError(
                    f"供应商 {su['name']} 无权确认其他供应商批次 {sb['batch_no']} 的差异",
                    "forbidden", 403)
            if d["status"] != "待确认":
                raise DomainError(f"差异单状态为 {d['status']}", "bad_state", 409)
            conn.execute(
                "UPDATE settlement_diffs SET status='已确认', confirmed_at=? WHERE id=?",
                (now(), diff_id))
            return {"id": diff_id, "status": "已确认"}

    def create_settlement(self, source_batch_id: int) -> dict:
        with self.db.transaction() as conn:
            sb = conn.execute("SELECT * FROM source_batches WHERE id=?",
                              (source_batch_id,)).fetchone()
            if not sb:
                raise DomainError("来源批次不存在", "not_found", 404)
            if sb["settled"]:
                raise DomainError(f"来源批次 {sb['batch_no']} 已结算", "already_settled", 409)
            pend = conn.execute(
                "SELECT id FROM settlements WHERE source_batch_id=? AND status='待确认'",
                (source_batch_id,)).fetchone()
            if pend:
                raise DomainError(
                    f"来源批次 {sb['batch_no']} 已存在待确认对账单 #{pend['id']}，请勿重复开票",
                    "settlement_pending", 409)
            batches = conn.execute("SELECT * FROM batches WHERE source_batch_id=?",
                                   (source_batch_id,)).fetchall()
            if not batches:
                raise DomainError("来源批次下无池内批次", "empty", 400)
            gross = round(sum(b["weight_in_kg"] for b in batches), 3)
            loss = round(sum(b["loss_kg"] for b in batches), 3)
            sorted_ = round(sum(b["sorted_kg"] for b in batches), 3)
            # 仅并入供应商已确认的差异
            diff_amount = round(sum(r["amount"] for r in conn.execute(
                "SELECT amount FROM settlement_diffs WHERE source_batch_id=? AND status='已确认'",
                (source_batch_id,)).fetchall()), 2)
            total = 0.0
            for b in batches:
                sp = conn.execute("SELECT * FROM species WHERE id=?", (b["species_id"],)).fetchone()
                total += (b["weight_in_kg"] - b["loss_kg"]) * sp["unit_price"]
            payable = round(total + diff_amount, 2)
            conn.execute(
                """INSERT INTO settlements(source_batch_id,supplier_id,gross_kg,loss_kg,
                   sorted_kg,diff_amount,payable,status,created_at)
                   VALUES(?,?,?,?,?,?,?, '待确认',?)""",
                (source_batch_id, sb["supplier_id"], gross, loss, sorted_, diff_amount,
                 payable, now()))
            sid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            return dict(conn.execute("SELECT * FROM settlements WHERE id=?", (sid,)).fetchone())

    def confirm_settlement(self, settlement_id: int, token: str) -> dict:
        with self.db.transaction() as conn:
            st = conn.execute("SELECT * FROM settlements WHERE id=?", (settlement_id,)).fetchone()
            if not st:
                raise DomainError("结算单不存在", "not_found", 404)
            su = self._supplier_by_token(conn, token)
            if st["supplier_id"] != su["id"]:
                target = conn.execute("SELECT name FROM suppliers WHERE id=?",
                                      (st["supplier_id"],)).fetchone()
                raise DomainError(
                    f"供应商 {su['name']} 无权确认归属 {target['name']} 的结算单 #{settlement_id}",
                    "forbidden", 403)
            if st["status"] != "待确认":
                raise DomainError(f"结算单状态为 {st['status']}", "bad_state", 409)
            conn.execute(
                "UPDATE settlements SET status='已确认', confirmed_at=? WHERE id=?",
                (now(), settlement_id))
            conn.execute("UPDATE source_batches SET settled=1, settled_at=? WHERE id=?",
                         (now(), st["source_batch_id"]))
            # 结算后：该来源批次下所有池内批次重量锁定（reweigh 将拒绝修改）
            return {"id": settlement_id, "status": "已确认", "settled": True}

    def ack_alert(self, alert_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE alerts SET status='acked' WHERE id=?", (alert_id,))
