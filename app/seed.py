"""演示数据与样例流程:档口/水槽/品种/供应商/客户/员工 + 入池、分拣、报损样例。"""
from .db import get_conn, q1, transaction
from .devices import hub
from .services import (cancel_order, create_order, entry_batch, generate_settlement,
                       report_loss, reweigh_batch, sort_out)


def seed_base():
    """基础演示数据(幂等:已有数据则跳过)。"""
    if q1("SELECT id FROM species LIMIT 1"):
        return False
    with transaction() as conn:
        stalls = [("1号档口", "A区"), ("2号档口", "B区")]
        for name, zone in stalls:
            conn.execute("INSERT INTO stalls(name,zone) VALUES(?,?)", (name, zone))

        # 水槽:zone / 编号 / 容量kg / 盐度‰
        tanks = [
            ("A-01", 1, "A区", 300, 25), ("A-02", 1, "A区", 300, 25),
            ("A-03", 1, "A区", 200, 30), ("B-01", 2, "B区", 400, 18),
            ("B-02", 2, "B区", 250, 25), ("B-03", 2, "B区", 150, 12),
        ]
        for code, stall_id, zone, cap, sal in tanks:
            conn.execute(
                "INSERT INTO tanks(code,stall_id,zone,capacity_kg,salinity) "
                "VALUES(?,?,?,?,?)", (code, stall_id, zone, cap, sal))

        # 品种:盐度/水温适应区间 + 演示结算单价(元/kg)
        species = [
            ("波士顿龙虾", 22, 30, 8, 15, 320.0),
            ("石斑鱼",     24, 32, 18, 26, 96.0),
            ("基围虾",     15, 28, 20, 30, 76.0),
            ("梭子蟹",     20, 30, 12, 22, 110.0),
            ("生蚝",       24, 32, 10, 20, 24.0),
            ("鲍鱼",       26, 33, 14, 20, 180.0),
        ]
        for name, smin, smax, tmin, tmax, price in species:
            conn.execute(
                "INSERT INTO species(name,salinity_min,salinity_max,temp_min,temp_max,"
                "unit_price) VALUES(?,?,?,?,?,?)",
                (name, smin, smax, tmin, tmax, price))

        # 混养冲突:龙虾与蟹互残;虾与石斑(被捕食)
        def sid(name):
            return conn.execute("SELECT id FROM species WHERE name=?", (name,)).fetchone()["id"]
        for a, b in [("波士顿龙虾", "梭子蟹"), ("基围虾", "石斑鱼")]:
            x, y = sorted((sid(a), sid(b)))
            conn.execute(
                "INSERT INTO species_conflicts(species_a,species_b) VALUES(?,?)", (x, y))

        suppliers = [("海丰水产", "陈老板 138****0001"),
                     ("蓝洋渔获", "林老板 139****0002"),
                     ("南潮贝类合作社", "许老板 137****0003")]
        for name, contact in suppliers:
            conn.execute("INSERT INTO suppliers(name,contact) VALUES(?,?)", (name, contact))

        customers = [("悦海酒楼", "0755-****0001"), ("鲜直达超市", "0755-****0002"),
                     ("潮味食府", "0755-****0003"), ("散客-王先生", "136****0009")]
        for name, contact in customers:
            conn.execute("INSERT INTO customers(name,contact) VALUES(?,?)", (name, contact))

        def supid(name):
            return conn.execute("SELECT id FROM suppliers WHERE name=?",
                                (name,)).fetchone()["id"]
        users = [("管理员", "admin", None), ("小张", "staff", None), ("小李", "staff", None),
                 ("海丰水产账号", "supplier", supid("海丰水产")),
                 ("蓝洋渔获账号", "supplier", supid("蓝洋渔获"))]
        for name, role, sup in users:
            conn.execute(
                "INSERT INTO users(name,role,supplier_id) VALUES(?,?,?)", (name, role, sup))
    return True


def _id(table, name):
    return q1(f"SELECT id FROM {table} WHERE name=?", (name,))["id"]


def seed_samples():
    """生成入池、分拣、报损样例(幂等:已有批次则跳过)。"""
    if q1("SELECT id FROM batches LIMIT 1"):
        return False

    lobster = _id("species", "波士顿龙虾")
    grouper = _id("species", "石斑鱼")
    shrimp = _id("species", "基围虾")
    oyster = _id("species", "生蚝")
    haifeng = _id("suppliers", "海丰水产")
    lanyang = _id("suppliers", "蓝洋渔获")
    nanchao = _id("suppliers", "南潮贝类合作社")

    def tank(code):
        return q1("SELECT id FROM tanks WHERE code=?", (code,))["id"]

    # ---- 入池样例(电子秤读数带幂等键) ----
    w = hub.scale.weigh(120.0)
    r1 = entry_batch(lobster, "500g/只", haifeng, "HF-20260928-01", tank("A-01"),
                     w["weight"], w["device_id"], w["idem_key"])
    w = hub.scale.weigh(180.0)
    r2 = entry_batch(grouper, "750g/条", lanyang, "LY-20260928-03", tank("A-02"),
                     w["weight"], w["device_id"], w["idem_key"])
    w = hub.scale.weigh(90.0)
    r3 = entry_batch(shrimp, "40头/斤", haifeng, "HF-20260928-02", tank("B-02"),
                     w["weight"], w["device_id"], w["idem_key"])
    w = hub.scale.weigh(140.0)
    r4 = entry_batch(oyster, "L级", nanchao, "NC-20260927-11", tank("A-03"),
                     w["weight"], w["device_id"], w["idem_key"])
    b1 = r1["batch"]["id"]
    b2 = r2["batch"]["id"]
    b3 = r3["batch"]["id"]
    b4 = r4["batch"]["id"]

    # ---- 分拣样例:两张订单 ----
    o1 = create_order(_id("customers", "悦海酒楼"), [
        {"species_id": lobster, "spec": "500g/只", "qty": 40},
        {"species_id": grouper, "spec": "750g/条", "qty": 60},
    ])
    o2 = create_order(_id("customers", "鲜直达超市"), [
        {"species_id": shrimp, "spec": "40头/斤", "qty": 30},
    ])
    items = {it["species_id"]: it["id"]
             for o in (o1, o2)
             for it in q1_items(o["order_id"])}
    sort_out(items[lobster], b1, 40, "小张")
    sort_out(items[grouper], b2, 60, "小李")
    sort_out(items[shrimp], b3, 30, "小张")

    # ---- 报损样例 ----
    report_loss(b1, 3.5, "死亡", "凌晨发现 7 只死亡")
    report_loss(b3, 2.0, "缺氧死亡", "夜间溶氧偏低")
    report_loss(b4, 1.2, "机械损伤", "搬运破壳")

    # ---- 补录称重差异样例(待供应商确认) ----
    w = hub.scale.weigh(0, jitter=0)
    reweigh_batch(b2, 118.6, w["device_id"], w["idem_key"], operator="小张")

    # ---- 结算样例:生蚝批次生成结算单 ----
    generate_settlement(b4)

    # ---- 订单取消样例 ----
    o3 = create_order(_id("customers", "潮味食府"), [
        {"species_id": grouper, "spec": "750g/条", "qty": 20},
    ])
    item_id = q1_items(o3["order_id"])[0]["id"]
    sort_out(item_id, b2, 20, "小李")
    cancel_order(o3["order_id"], operator="管理员")

    # 注入一处水温异常,让看板展示固定警示位
    hub.inject_temp_anomaly(tank("A-01"), 8.0)
    return True


def q1_items(order_id):
    from .db import q
    return q("SELECT * FROM order_items WHERE order_id=?", (order_id,))


def seed_all():
    """完整初始化:基础数据 + 样例流程。"""
    created_base = seed_base()
    created_sample = seed_samples()
    return {"base": created_base, "samples": created_sample}
