"""活鲜暂养平台核心业务规则。

异常统一抛 ServiceError(status, message),由 web 层转成 JSON 响应。
"""
import itertools
import threading
from datetime import datetime

from .db import q, q1, transaction

DEATH_RATE_THRESHOLD = 0.08      # 死亡率超阈值(8%)触发告警与损耗提醒
WEIGH_DIFF_TOLERANCE = 0.5       # 补录称重差异容忍(kg),超过生成待结算差异

_batch_seq = itertools.count(1)
_order_seq = itertools.count(1)
_seq_lock = threading.Lock()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ServiceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class _ConflictRejected(Exception):
    """入池混养冲突:事务回滚后由外层单独落告警。"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------- 基础查询

def get_species(species_id):
    row = q1("SELECT * FROM species WHERE id=?", (species_id,))
    if not row:
        raise ServiceError(404, f"品种不存在: {species_id}")
    return row


def get_tank(tank_id):
    row = q1("SELECT * FROM tanks WHERE id=?", (tank_id,))
    if not row:
        raise ServiceError(404, f"水槽不存在: {tank_id}")
    return row


def get_batch(batch_id):
    row = q1("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not row:
        raise ServiceError(404, f"批次不存在: {batch_id}")
    return row


def tank_load(conn, tank_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(current_weight),0) AS w FROM batches "
        "WHERE tank_id=? AND status='在养'", (tank_id,)).fetchone()
    return row["w"]


def add_alert(conn, kind, message, tank_id=None, batch_id=None, level="警告", dedup=True):
    """写告警;dedup=True 时同水槽/批次同类型未处理告警不重复生成。"""
    if dedup:
        exist = conn.execute(
            "SELECT id FROM alerts WHERE kind=? AND status='未处理' "
            "AND COALESCE(tank_id,-1)=COALESCE(?,-1) "
            "AND COALESCE(batch_id,-1)=COALESCE(?,-1)",
            (kind, tank_id, batch_id)).fetchone()
        if exist:
            return exist["id"]
    cur = conn.execute(
        "INSERT INTO alerts(kind,tank_id,batch_id,message,level,status,created_at) "
        "VALUES(?,?,?,?,?,'未处理',?)",
        (kind, tank_id, batch_id, message, level, now()))
    return cur.lastrowid


def record_weighing(conn, batch_id, kind, weight, device_id, idem_key=None):
    """写称重记录;idem_key 冲突时返回既有记录(称重去重)。"""
    if idem_key:
        exist = conn.execute(
            "SELECT * FROM weighings WHERE idem_key=?", (idem_key,)).fetchone()
        if exist:
            return dict(exist), True
    cur = conn.execute(
        "INSERT INTO weighings(batch_id,kind,weight,device_id,idem_key,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (batch_id, kind, weight, device_id, idem_key, now()))
    row = conn.execute("SELECT * FROM weighings WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row), False


# ---------------------------------------------------------------- 入池

def entry_batch(species_id, spec, supplier_id, source_batch_no, tank_id,
                weight, device_id="SCALE-01", idem_key=None):
    """批次入池:校验水槽容量、盐度适配与混养冲突,写批次与入池称重。"""
    if weight is None or weight <= 0:
        raise ServiceError(400, "入池重量必须大于 0")
    sp = get_species(species_id)
    tank = get_tank(tank_id)
    if not q1("SELECT id FROM suppliers WHERE id=?", (supplier_id,)):
        raise ServiceError(404, f"供应商不存在: {supplier_id}")

    # 盐度校验:品种适应范围必须覆盖水槽当前盐度
    if not (sp["salinity_min"] <= tank["salinity"] <= sp["salinity_max"]):
        raise ServiceError(
            409, f"盐度不适配:{sp['name']}要求 {sp['salinity_min']}~{sp['salinity_max']}‰,"
                 f"水槽 {tank['code']} 当前 {tank['salinity']}‰")

    try:
        return _do_entry(species_id, spec, supplier_id, source_batch_no,
                         tank_id, weight, device_id, idem_key, sp, tank)
    except _ConflictRejected as c:
        # 入池被拒,但混养冲突告警需独立落库(不能随事务回滚)
        with transaction() as conn:
            add_alert(conn, "混养冲突", c.message, tank_id=tank_id,
                      level="严重", dedup=False)
        raise ServiceError(409, c.message)


def _do_entry(species_id, spec, supplier_id, source_batch_no, tank_id,
              weight, device_id, idem_key, sp, tank):
    with transaction() as conn:
        # 称重去重:同一秤单(设备+序号)重复提交直接返回原批次,不重复入池
        if idem_key:
            exist = conn.execute(
                "SELECT * FROM weighings WHERE idem_key=?", (idem_key,)).fetchone()
            if exist:
                batch = conn.execute("SELECT * FROM batches WHERE id=?",
                                     (exist["batch_id"],)).fetchone()
                return {"batch": dict(batch), "weighing": dict(exist), "deduped": True}

        # 容量校验
        load = tank_load(conn, tank_id)
        if load + weight > tank["capacity_kg"] + 1e-9:
            raise ServiceError(
                409, f"水槽容量不足:{tank['code']} 容量 {tank['capacity_kg']}kg,"
                     f"已养 {load:.1f}kg,本次 {weight}kg 超限")

        # 混养冲突校验
        others = conn.execute(
            "SELECT DISTINCT species_id FROM batches WHERE tank_id=? AND status='在养'",
            (tank_id,)).fetchall()
        for o in others:
            a, b = sorted((species_id, o["species_id"]))
            if a != b and conn.execute(
                    "SELECT 1 FROM species_conflicts WHERE species_a=? AND species_b=?",
                    (a, b)).fetchone():
                other_sp = conn.execute("SELECT name FROM species WHERE id=?",
                                        (o["species_id"],)).fetchone()
                msg = f"混养冲突:{sp['name']} 与 {other_sp['name']} 不能同养于 {tank['code']}"
                raise _ConflictRejected(msg)

        with _seq_lock:
            seq = next(_batch_seq)
        batch_no = "B" + datetime.now().strftime("%Y%m%d") + f"-{seq:04d}"
        cur = conn.execute(
            "INSERT INTO batches(batch_no,species_id,spec,supplier_id,source_batch_no,"
            "tank_id,entry_weight,current_weight,status,entry_time) "
            "VALUES(?,?,?,?,?,?,?,?,'在养',?)",
            (batch_no, species_id, spec, supplier_id, source_batch_no,
             tank_id, weight, weight, now()))
        batch_id = cur.lastrowid
        wrec, deduped = record_weighing(conn, batch_id, "入池", weight, device_id, idem_key)
        batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return {"batch": dict(batch), "weighing": wrec, "deduped": deduped}


# ---------------------------------------------------------------- 补录称重

def reweigh_batch(batch_id, weight, device_id, idem_key=None, operator="系统"):
    """补录称重:保留原始值与设备;与账面差异超容忍时生成待结算差异(挂账)。

    已结算批次禁止再改重量。补录只记录差异,不直接改 current_weight。
    """
    if weight is None or weight <= 0:
        raise ServiceError(400, "补录重量必须大于 0")
    with transaction() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ServiceError(404, f"批次不存在: {batch_id}")
        if batch["settled"]:
            raise ServiceError(409, f"批次 {batch['batch_no']} 已结算,不能再次改重量")

        wrec, deduped = record_weighing(conn, batch_id, "补录", weight, device_id, idem_key)
        result = {"weighing": wrec, "deduped": deduped}
        if deduped:
            result["message"] = "重复称重已去重,返回原始记录"
            return result

        expect = batch["current_weight"]
        diff = round(weight - expect, 3)
        if abs(diff) > WEIGH_DIFF_TOLERANCE:
            # 供应商确认前只生成待结算差异,不改账面重量
            cur = conn.execute(
                "INSERT INTO settlement_diffs(batch_id,kind,expect_weight,actual_weight,"
                "diff,status,created_at) VALUES(?,?,?,?,?,'待确认',?)",
                (batch_id, "称重差异", expect, weight, diff, now()))
            add_alert(conn, "称重差异",
                      f"批次 {batch['batch_no']} 补录 {weight}kg 与账面 {expect}kg "
                      f"差异 {diff:+.2f}kg(设备 {device_id},操作 {operator}),待供应商确认",
                      batch_id=batch_id)
            result["diff_id"] = cur.lastrowid
            result["diff"] = diff
            result["message"] = "差异已挂账为待结算差异,待供应商确认"
        else:
            result["diff"] = diff
            result["message"] = "差异在容忍范围内,仅留档"
        return result


# ---------------------------------------------------------------- 订单与分拣

def create_order(customer_id, items):
    """items: [{species_id, spec, qty}]"""
    if not items:
        raise ServiceError(400, "订单至少包含一行明细")
    if not q1("SELECT id FROM customers WHERE id=?", (customer_id,)):
        raise ServiceError(404, f"客户不存在: {customer_id}")
    with transaction() as conn:
        with _seq_lock:
            seq = next(_order_seq)
        order_no = "O" + datetime.now().strftime("%Y%m%d") + f"-{seq:04d}"
        cur = conn.execute(
            "INSERT INTO orders(order_no,customer_id,status,created_at) VALUES(?,?,?,?)",
            (order_no, customer_id, "待分拣", now()))
        order_id = cur.lastrowid
        for it in items:
            if it["qty"] <= 0:
                raise ServiceError(400, "订单明细数量必须大于 0")
            get_species(it["species_id"])
            conn.execute(
                "INSERT INTO order_items(order_id,species_id,spec,qty) VALUES(?,?,?,?)",
                (order_id, it["species_id"], it["spec"], it["qty"]))
        return {"order_id": order_id, "order_no": order_no}


def sort_out(order_item_id, batch_id, qty, employee):
    """按订单分拣出池:原子扣减批次余量实现并发互斥(两名员工同时分拣只允许一单成功)。"""
    if qty is None or qty <= 0:
        raise ServiceError(400, "分拣数量必须大于 0")
    with transaction() as conn:
        item = conn.execute("SELECT * FROM order_items WHERE id=?", (order_item_id,)).fetchone()
        if not item:
            raise ServiceError(404, f"订单明细不存在: {order_item_id}")
        order = conn.execute("SELECT * FROM orders WHERE id=?", (item["order_id"],)).fetchone()
        if order["status"] == "已取消":
            raise ServiceError(409, f"订单 {order['order_no']} 已取消,不能分拣")

        batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ServiceError(404, f"批次不存在: {batch_id}")
        if batch["settled"]:
            raise ServiceError(409, f"批次 {batch['batch_no']} 已结算,不能分拣")
        if batch["status"] != "在养":
            raise ServiceError(409, f"批次 {batch['batch_no']} 状态为 {batch['status']},不能分拣")
        if batch["species_id"] != item["species_id"]:
            raise ServiceError(409, "批次品种与订单明细不匹配")

        remain = item["qty"] - item["sorted_qty"]
        if qty > remain + 1e-9:
            raise ServiceError(409, f"超量分拣:明细剩余 {remain:.2f}kg,本次 {qty}kg")

        # 原子扣减:并发下只有一单能满足 current_weight >= qty
        cur = conn.execute(
            "UPDATE batches SET current_weight = current_weight - ?, "
            "out_weight = out_weight + ? "
            "WHERE id=? AND settled=0 AND status='在养' AND current_weight >= ?",
            (qty, qty, batch_id, qty))
        if cur.rowcount == 0:
            raise ServiceError(409, f"批次 {batch['batch_no']} 余量不足,分拣失败"
                                    f"(可能被其他员工锁定)")

        conn.execute(
            "UPDATE order_items SET sorted_qty = sorted_qty + ? WHERE id=?",
            (qty, order_item_id))
        conn.execute(
            "INSERT INTO sortings(order_item_id,batch_id,qty,employee,status,created_at) "
            "VALUES(?,?,?,?,'已锁定',?)",
            (order_item_id, batch_id, qty, employee, now()))
        conn.execute(
            "INSERT INTO weighings(batch_id,kind,weight,device_id,idem_key,created_at) "
            "VALUES(?,?,?,?,NULL,?)",
            (batch_id, "出池", qty, "SCALE-OUT", now()))

        # 批次清空 / 订单完成状态推进
        conn.execute(
            "UPDATE batches SET status='已清空' WHERE id=? AND current_weight <= 1e-9",
            (batch_id,))
        _refresh_order_status(conn, item["order_id"])
        return {"sorted": qty, "order_id": item["order_id"],
                "batch_no": batch["batch_no"], "employee": employee}


def _refresh_order_status(conn, order_id):
    rows = conn.execute(
        "SELECT qty, sorted_qty FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    if not rows:
        return
    done = all(r["sorted_qty"] >= r["qty"] - 1e-9 for r in rows)
    any_sorted = any(r["sorted_qty"] > 0 for r in rows)
    status = "已完成" if done else ("分拣中" if any_sorted else "待分拣")
    conn.execute("UPDATE orders SET status=? WHERE id=? AND status != '已取消'",
                 (status, order_id))


def cancel_order(order_id, operator="系统"):
    """订单取消:释放未出池的锁定数量回批次。"""
    with transaction() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ServiceError(404, f"订单不存在: {order_id}")
        if order["status"] == "已取消":
            raise ServiceError(409, "订单已是取消状态")
        locks = conn.execute(
            "SELECT s.* FROM sortings s JOIN order_items oi ON s.order_item_id=oi.id "
            "WHERE oi.order_id=? AND s.status='已锁定'", (order_id,)).fetchall()
        for lk in locks:
            # 已结算批次不回写重量,仅标记释放
            conn.execute(
                "UPDATE batches SET current_weight = current_weight + ?, "
                "out_weight = out_weight - ?, "
                "status = CASE WHEN status='已清空' THEN '在养' ELSE status END "
                "WHERE id=? AND settled=0",
                (lk["qty"], lk["qty"], lk["batch_id"]))
            conn.execute("UPDATE sortings SET status='已释放' WHERE id=?", (lk["id"],))
        conn.execute(
            "UPDATE order_items SET sorted_qty=0 WHERE order_id=?", (order_id,))
        conn.execute("UPDATE orders SET status='已取消' WHERE id=?", (order_id,))
        add_alert(conn, "订单取消",
                  f"订单 {order['order_no']} 被 {operator} 取消,"
                  f"释放 {len(locks)} 笔锁定分拣", dedup=False)
        return {"released": len(locks)}


# ---------------------------------------------------------------- 损耗

def report_loss(batch_id, qty, reason, note=""):
    """死亡/损耗报损:必须关联原批次;累计死亡率超阈值触发告警与损耗提醒。"""
    if qty is None or qty <= 0:
        raise ServiceError(400, "报损数量必须大于 0")
    with transaction() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ServiceError(404, f"批次不存在: {batch_id}")
        if batch["settled"]:
            raise ServiceError(409, f"批次 {batch['batch_no']} 已结算,不能再次改重量")
        if qty > batch["current_weight"] + 1e-9:
            raise ServiceError(409, f"报损数量超过批次余量 {batch['current_weight']:.2f}kg")

        cur = conn.execute(
            "INSERT INTO loss_records(batch_id,qty,reason,note,created_at) "
            "VALUES(?,?,?,?,?)",
            (batch_id, qty, reason, note, now()))
        conn.execute(
            "UPDATE batches SET loss_weight = loss_weight + ?, "
            "current_weight = current_weight - ? WHERE id=?",
            (qty, qty, batch_id))
        conn.execute(
            "UPDATE batches SET status='已清空' WHERE id=? AND current_weight <= 1e-9",
            (batch_id,))

        rate = (batch["loss_weight"] + qty) / batch["entry_weight"]
        alerts = []
        if rate > DEATH_RATE_THRESHOLD:
            msg = (f"批次 {batch['batch_no']} 累计损耗率 {rate*100:.1f}% "
                   f"超过阈值 {DEATH_RATE_THRESHOLD*100:.0f}%")
            add_alert(conn, "死亡率超阈值", msg,
                      tank_id=batch["tank_id"], batch_id=batch_id, level="严重")
            add_alert(conn, "损耗提醒", msg + ",请优先处理该批次",
                      tank_id=batch["tank_id"], batch_id=batch_id)
            alerts.append(msg)
        return {"loss_id": cur.lastrowid, "death_rate": round(rate, 4), "alerts": alerts}


# ---------------------------------------------------------------- 结算

def generate_settlement(batch_id):
    """按批次生成结算单:结算重量 = 入池 - 损耗;金额 = 结算重量 × 单价。"""
    with transaction() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ServiceError(404, f"批次不存在: {batch_id}")
        if batch["settled"]:
            raise ServiceError(409, f"批次 {batch['batch_no']} 已结算")
        exist = conn.execute(
            "SELECT * FROM settlements WHERE batch_id=?", (batch_id,)).fetchone()
        if exist:
            # 已开单后新产生的待确认差异也要挂到该结算单
            conn.execute(
                "UPDATE settlement_diffs SET settlement_id=? "
                "WHERE batch_id=? AND settlement_id IS NULL",
                (exist["id"], batch_id))
            return {"settlement": dict(exist), "existed": True}
        sp = conn.execute("SELECT * FROM species WHERE id=?",
                          (batch["species_id"],)).fetchone()
        settle_weight = round(batch["entry_weight"] - batch["loss_weight"], 3)
        amount = round(settle_weight * sp["unit_price"], 2)
        cur = conn.execute(
            "INSERT INTO settlements(batch_id,supplier_id,entry_weight,loss_weight,"
            "out_weight,settle_weight,unit_price,amount,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'待供应商确认',?)",
            (batch_id, batch["supplier_id"], batch["entry_weight"], batch["loss_weight"],
             batch["out_weight"], settle_weight, sp["unit_price"], amount, now()))
        # 挂起该批次未关联的待确认差异
        conn.execute(
            "UPDATE settlement_diffs SET settlement_id=? "
            "WHERE batch_id=? AND settlement_id IS NULL",
            (cur.lastrowid, batch_id))
        row = conn.execute("SELECT * FROM settlements WHERE id=?",
                           (cur.lastrowid,)).fetchone()
        return {"settlement": dict(row), "existed": False}


def confirm_settlement(settlement_id, user):
    """供应商确认结算:只能确认本供应商的单据,且须先处理完待确认差异。"""
    with transaction() as conn:
        st = conn.execute("SELECT * FROM settlements WHERE id=?",
                          (settlement_id,)).fetchone()
        if not st:
            raise ServiceError(404, f"结算单不存在: {settlement_id}")
        if user["role"] == "supplier":
            if user["supplier_id"] != st["supplier_id"]:
                raise ServiceError(403, "供应商只能确认本单位的结算单")
        elif user["role"] != "admin":
            raise ServiceError(403, "仅供应商或管理员可确认结算")
        if st["status"] != "待供应商确认":
            raise ServiceError(409, f"结算单状态为 {st['status']},不能重复确认")
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM settlement_diffs "
            "WHERE settlement_id=? AND status='待确认'", (settlement_id,)).fetchone()
        if pending["c"] > 0:
            raise ServiceError(409, f"存在 {pending['c']} 条待确认差异,请先处理")
        conn.execute(
            "UPDATE settlements SET status='已结算', confirmed_at=? WHERE id=?",
            (now(), settlement_id))
        conn.execute(
            "UPDATE batches SET settled=1, status='已结算' WHERE id=?",
            (st["batch_id"],))
        return {"settlement_id": settlement_id, "status": "已结算"}


def resolve_diff(diff_id, accept, user):
    """处理待结算差异:接受则把差异计入结算重量,驳回则维持账面。"""
    with transaction() as conn:
        d = conn.execute("SELECT * FROM settlement_diffs WHERE id=?",
                         (diff_id,)).fetchone()
        if not d:
            raise ServiceError(404, f"差异单不存在: {diff_id}")
        if d["status"] != "待确认":
            raise ServiceError(409, "差异单已处理")
        batch = conn.execute("SELECT * FROM batches WHERE id=?",
                             (d["batch_id"],)).fetchone()
        if user["role"] == "supplier" and user["supplier_id"] != batch["supplier_id"]:
            raise ServiceError(403, "供应商只能处理本单位的差异")
        if user["role"] not in ("supplier", "admin"):
            raise ServiceError(403, "无权限处理差异")

        conn.execute("UPDATE settlement_diffs SET status=? WHERE id=?",
                     ("已确认" if accept else "已驳回", diff_id))
        st = conn.execute("SELECT * FROM settlements WHERE id=?",
                          (d["settlement_id"],)).fetchone() if d["settlement_id"] else None
        if accept and st and st["status"] == "待供应商确认":
            new_settle = round(st["settle_weight"] + d["diff"], 3)
            new_amount = round(new_settle * st["unit_price"], 2)
            conn.execute(
                "UPDATE settlements SET settle_weight=?, amount=? WHERE id=?",
                (new_settle, new_amount, st["id"]))
        return {"diff_id": diff_id, "status": "已确认" if accept else "已驳回"}


# ---------------------------------------------------------------- 看板/列表查询

def dashboard():
    """水槽看板:按区域分组,含容量、水温、在养批次与异常。"""
    tanks = q("SELECT * FROM tanks ORDER BY zone, code")
    zones = {}
    for t in tanks:
        t = dict(t)
        batches = q(
            "SELECT b.*, s.name AS species_name FROM batches b "
            "JOIN species s ON s.id=b.species_id "
            "WHERE b.tank_id=? AND b.status='在养' ORDER BY b.id", (t["id"],))
        latest = q1(
            "SELECT * FROM holding_records WHERE tank_id=? ORDER BY id DESC LIMIT 1",
            (t["id"],))
        alerts = q(
            "SELECT * FROM alerts WHERE tank_id=? AND status='未处理' "
            "ORDER BY id DESC LIMIT 5", (t["id"],))
        load = sum(b["current_weight"] for b in batches)
        t["batches"] = [dict(b) for b in batches]
        t["load"] = round(load, 2)
        t["usage"] = round(load / t["capacity_kg"], 4) if t["capacity_kg"] else 0
        t["water_temp"] = latest["water_temp"] if latest else None
        t["oxygen"] = latest["oxygen"] if latest else None
        t["alerts"] = [dict(a) for a in alerts]
        t["abnormal"] = t["status"] != "正常" or len(alerts) > 0
        zones.setdefault(t["zone"], []).append(t)
    return {"zones": [{"zone": z, "tanks": ts} for z, ts in sorted(zones.items())]}


def list_batches(status=None, supplier_id=None):
    sql = ("SELECT b.*, s.name AS species_name, s.unit_price, t.code AS tank_code, "
           "sp.name AS supplier_name FROM batches b "
           "JOIN species s ON s.id=b.species_id "
           "JOIN tanks t ON t.id=b.tank_id "
           "JOIN suppliers sp ON sp.id=b.supplier_id")
    cond, args = [], []
    if status:
        cond.append("b.status=?")
        args.append(status)
    if supplier_id:
        cond.append("b.supplier_id=?")
        args.append(supplier_id)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY b.id DESC"
    return [dict(r) for r in q(sql, args)]


def list_orders():
    orders = [dict(r) for r in q(
        "SELECT o.*, c.name AS customer_name FROM orders o "
        "JOIN customers c ON c.id=o.customer_id ORDER BY o.id DESC")]
    for o in orders:
        items = q(
            "SELECT oi.*, s.name AS species_name FROM order_items oi "
            "JOIN species s ON s.id=oi.species_id WHERE oi.order_id=?", (o["id"],))
        o["items"] = [dict(i) for i in items]
        for i in o["items"]:
            locks = q(
                "SELECT s.*, b.batch_no FROM sortings s JOIN batches b ON b.id=s.batch_id "
                "WHERE s.order_item_id=? ORDER BY s.id DESC", (i["id"],))
            i["sortings"] = [dict(l) for l in locks]
    return orders


def list_losses():
    rows = q(
        "SELECT l.*, b.batch_no, b.spec, b.source_batch_no, b.entry_weight, "
        "b.loss_weight, b.current_weight, b.status AS batch_status, "
        "s.name AS species_name, t.code AS tank_code, sp.name AS supplier_name "
        "FROM loss_records l "
        "JOIN batches b ON b.id=l.batch_id "
        "JOIN species s ON s.id=b.species_id "
        "JOIN tanks t ON t.id=b.tank_id "
        "JOIN suppliers sp ON sp.id=b.supplier_id "
        "ORDER BY l.id DESC")
    result = []
    for r in rows:
        d = dict(r)
        d["death_rate"] = round(r["loss_weight"] / r["entry_weight"], 4) \
            if r["entry_weight"] else 0
        result.append(d)
    return result


def list_settlements(user):
    sql = ("SELECT st.*, b.batch_no, b.spec, s.name AS species_name, "
           "sp.name AS supplier_name FROM settlements st "
           "JOIN batches b ON b.id=st.batch_id "
           "JOIN species s ON s.id=b.species_id "
           "JOIN suppliers sp ON sp.id=st.supplier_id")
    args = []
    if user and user["role"] == "supplier":
        sql += " WHERE st.supplier_id=?"
        args.append(user["supplier_id"])
    sql += " ORDER BY st.id DESC"
    rows = [dict(r) for r in q(sql, args)]
    for r in rows:
        diffs = q("SELECT * FROM settlement_diffs WHERE settlement_id=? ORDER BY id",
                  (r["id"],))
        r["diffs"] = [dict(d) for d in diffs]
    return rows


def list_pending_diffs(user):
    sql = ("SELECT d.*, b.batch_no, sp.name AS supplier_name FROM settlement_diffs d "
           "JOIN batches b ON b.id=d.batch_id "
           "JOIN suppliers sp ON sp.id=b.supplier_id WHERE d.status='待确认'")
    args = []
    if user and user["role"] == "supplier":
        sql += " AND b.supplier_id=?"
        args.append(user["supplier_id"])
    sql += " ORDER BY d.id DESC"
    return [dict(r) for r in q(sql, args)]
