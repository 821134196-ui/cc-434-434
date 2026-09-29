"""演示数据：档口、水槽、品种、供应商、客户、员工。

供应商/客户/金额均为演示数据。供应商 token 同时用于结算接口的权限校验。
"""
from __future__ import annotations

from .db import DB
from .devices import ScaleAdapter
from .services import WriteService

STALLS = [("海旺水产", "HW-01")]

TANKS = [
    # 区域, 编号, 容量kg, 盐度
    ("A区", "A-01", 120, "海水"),
    ("A区", "A-02", 120, "海水"),
    ("B区", "B-01", 100, "咸淡水"),
    ("B区", "B-02", 80, "淡水"),
    ("C区", "C-01", 150, "海水"),
]

SPECIES = [
    # 名称, 规格, 盐度, 适温, 最低溶氧, 单价, 死亡率阈值
    ("青斑鱼", "1.2-1.8斤/条", "海水", 16, 22, 5.5, 78.0, 0.05),
    ("花蟹", "3-4两/只", "海水", 17, 23, 5.0, 95.0, 0.06),
    ("南美白对虾", "40尾/斤", "咸淡水", 20, 26, 4.5, 42.0, 0.08),
    ("鲈鱼", "1.5-2斤/条", "淡水", 18, 24, 5.0, 38.0, 0.05),
    ("生蚝", "6只/斤", "海水", 10, 20, 4.0, 18.0, 0.04),
]

# 混养冲突对（按品种名）
CONFLICTS = [
    ("青斑鱼", "南美白对虾", "青斑鱼会捕食虾苗"),
    ("花蟹", "生蚝", "花蟹夹食生蚝"),
]

SUPPLIERS = [
    ("林记海鲜批发", "sup_lin_token"),
    ("东港渔业合作社", "sup_donggang_token"),
    ("陈记活鲜", "sup_chen_token"),
]

CUSTOMERS = ["鸿运酒楼", "海鲜舫大排档", "鲜邻超市", "禧宴酒店"]
EMPLOYEES = ["阿强（分拣组长）", "小美（分拣员）", "老周（仓管）"]


def seed(db: DB, reset: bool = False) -> None:
    """写入基础演示数据（幂等；reset=True 时清空全部）。"""
    with db.transaction() as conn:
        if reset:
            for tbl in ("settlements", "settlement_diffs", "losses", "order_items", "orders",
                        "alerts", "holding_logs", "weighings", "batches", "source_batches",
                        "species_conflicts", "employees", "customers", "suppliers",
                        "species", "tanks", "stalls"):
                conn.execute(f"DELETE FROM {tbl}")
        if conn.execute("SELECT COUNT(*) c FROM stalls").fetchone()["c"]:
            return
        for name, code in STALLS:
            conn.execute("INSERT INTO stalls(name,code) VALUES(?,?)", (name, code))
        stall_id = conn.execute("SELECT id FROM stalls WHERE code='HW-01'").fetchone()["id"]
        for zone, code, cap, sal in TANKS:
            conn.execute(
                "INSERT INTO tanks(stall_id,zone,code,capacity_kg,salinity,probe_temp,probe_oxygen) "
                "VALUES(?,?,?,?,?,?,?)", (stall_id, zone, code, cap, sal, 18.5, 6.8))
        for name, spec, sal, tmin, tmax, ox, price, thr in SPECIES:
            conn.execute(
                """INSERT INTO species(name,spec,salinity,temp_min,temp_max,min_oxygen,
                   unit_price,mortality_threshold) VALUES(?,?,?,?,?,?,?,?)""",
                (name, spec, sal, tmin, tmax, ox, price, thr))
        for a, b, note in CONFLICTS:
            ia = conn.execute("SELECT id FROM species WHERE name=?", (a,)).fetchone()["id"]
            ib = conn.execute("SELECT id FROM species WHERE name=?", (b,)).fetchone()["id"]
            conn.execute("INSERT INTO species_conflicts(species_a,species_b,note) VALUES(?,?,?)",
                         (ia, ib, note))
        for name, token in SUPPLIERS:
            conn.execute("INSERT INTO suppliers(name,token) VALUES(?,?)", (name, token))
        for name in CUSTOMERS:
            conn.execute("INSERT INTO customers(name) VALUES(?)", (name,))
        for name in EMPLOYEES:
            conn.execute("INSERT INTO employees(name) VALUES(?)", (name,))


def generate_samples(db: DB) -> dict:
    """生成一条命令即可复现的业务样例：入池 -> 分拣 -> 报损 -> 补录差异。"""
    seed(db)  # 幂等确保档口/水槽/品种等基础数据存在
    w = WriteService(db)
    scale = ScaleAdapter()
    q = db.query_one

    def sid(name): return q("SELECT id FROM species WHERE name=?", (name,))["id"]
    def tid(code): return q("SELECT id FROM tanks WHERE code=?", (code,))["id"]
    def supid(token): return q("SELECT id FROM suppliers WHERE token=?", (token,))["id"]
    def cuid(name): return q("SELECT id FROM customers WHERE name=?", (name,))["id"]
    def eid(name): return q("SELECT id FROM employees WHERE name=?", (name,))["id"]

    created = {"intakes": [], "orders": [], "losses": [], "reweigh": None}

    # 1) 入池：青斑鱼 -> A-01；花蟹 -> A-02；对虾 -> B-01；鲈鱼 -> B-02；生蚝 -> C-01
    plan = [
        ("青斑鱼", "SRC-LIN-0928-01", "sup_lin_token", 48.0, "A-01"),
        ("花蟹", "SRC-LIN-0928-01", "sup_lin_token", 30.0, "A-02"),
        ("生蚝", "SRC-DG-0928-02", "sup_donggang_token", 60.0, "C-01"),
        ("南美白对虾", "SRC-CHEN-0928-03", "sup_chen_token", 35.0, "B-01"),
        ("鲈鱼", "SRC-CHEN-0928-03", "sup_chen_token", 28.0, "B-02"),
        ("青斑鱼", "SRC-LIN-0929-04", "sup_lin_token", 45.0, "A-01"),
    ]
    for sp_name, src, token, weight, tank_code in plan:
        wt, dev, tok = scale.exact(weight)
        b = w.intake(species_id=sid(sp_name), spec="", source_batch_no=src,
                     supplier_id=supid(token), weight_kg=wt, tank_id=tid(tank_code),
                     device_id=dev, reading_token=tok, operator="老周（仓管）")
        created["intakes"].append({"batch_no": b["batch_no"], "species": sp_name,
                                   "tank": tank_code, "kg": wt})

    # 2) 分拣出池（订单锁定）
    qing = q("SELECT id FROM batches WHERE batch_no=?", (created["intakes"][0]["batch_no"],))["id"]
    crab = q("SELECT id FROM batches WHERE batch_no=?", (created["intakes"][1]["batch_no"],))["id"]
    o1 = w.create_order(customer_id=cuid("鸿运酒楼"), employee_id=eid("阿强（分拣组长）"),
                        items=[{"batch_id": qing, "qty_kg": 20.0},
                               {"batch_id": crab, "qty_kg": 12.0}])
    w.complete_order(o1["id"])
    created["orders"].append(o1["order_no"])

    # 3) 死亡报损（关联原批次）
    loss = w.record_loss(batch_id=qing, qty_kg=1.5, reason="死亡",
                         operator="老周（仓管）", device_id="MANUAL")
    created["losses"].append(loss["id"])

    # 4) 补录称重（与入池差异较大 -> 待供应商确认的差异挂账）
    shrimp = q("SELECT id FROM batches WHERE batch_no=?",
               (created["intakes"][3]["batch_no"],))["id"]
    wt2, dev2, tok2 = scale.exact(33.8)
    created["reweigh"] = w.reweigh(batch_id=shrimp, corrected_kg=wt2,
                                   device_id=dev2, reading_token=tok2,
                                   operator="老周（仓管）")
    return created
