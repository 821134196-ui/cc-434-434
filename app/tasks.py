"""进程内后台任务:周期读取模拟设备,生成暂养记录、异常告警与损耗提醒。"""
import threading
import time

from .db import q, transaction
from .devices import hub
from .services import DEATH_RATE_THRESHOLD, add_alert, now

TEMP_MARGIN = 1.0      # 水温超出品种适应区间 ±1°C 即告警
OXYGEN_MIN = 4.0       # 溶氧低于 4 mg/L 判定缺氧

_stop = threading.Event()
_thread = None


def check_tank_once(conn, tank, occupied_species):
    """对单个水槽做一次传感采样与异常判定(抽出便于测试)。"""
    reading = hub.read_tank(tank["id"])
    temp, oxygen = reading["water_temp"], reading["oxygen"]

    conn.execute(
        "INSERT INTO holding_records(tank_id,water_temp,salinity,oxygen,note,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (tank["id"], temp, tank["salinity"], oxygen, "自动巡检", now()))

    abnormal = False
    # 水温异常:对照槽内所有在养品种的适应区间
    for sp in occupied_species:
        if temp < sp["temp_min"] - TEMP_MARGIN or temp > sp["temp_max"] + TEMP_MARGIN:
            add_alert(conn, "水温异常",
                      f"水槽 {tank['code']} 水温 {temp}°C 超出 {sp['name']} "
                      f"适应区间 {sp['temp_min']}~{sp['temp_max']}°C",
                      tank_id=tank["id"], level="严重")
            abnormal = True
    # 缺氧
    if oxygen < OXYGEN_MIN:
        add_alert(conn, "缺氧",
                  f"水槽 {tank['code']} 溶氧 {oxygen}mg/L 低于 {OXYGEN_MIN}mg/L",
                  tank_id=tank["id"], level="严重")
        abnormal = True

    conn.execute("UPDATE tanks SET status=? WHERE id=?",
                 ("异常" if abnormal else "正常", tank["id"]))
    return reading


def patrol_once():
    """单轮巡检:所有在养水槽采样 + 死亡率扫描生成损耗提醒。"""
    with transaction() as conn:
        tanks = conn.execute("SELECT * FROM tanks").fetchall()
        for t in tanks:
            species = conn.execute(
                "SELECT DISTINCT s.* FROM batches b JOIN species s ON s.id=b.species_id "
                "WHERE b.tank_id=? AND b.status='在养'", (t["id"],)).fetchall()
            if not species:
                continue
            check_tank_once(conn, t, species)

        # 死亡率超阈值 → 损耗提醒(进程内任务生成)
        rows = conn.execute(
            "SELECT * FROM batches WHERE status='在养' AND entry_weight > 0").fetchall()
        for b in rows:
            rate = b["loss_weight"] / b["entry_weight"]
            if rate > DEATH_RATE_THRESHOLD:
                add_alert(
                    conn, "损耗提醒",
                    f"批次 {b['batch_no']} 损耗率 {rate*100:.1f}% 超阈值,建议尽快出池或处置",
                    tank_id=b["tank_id"], batch_id=b["id"])


def _loop(interval):
    while not _stop.is_set():
        try:
            patrol_once()
        except Exception as exc:  # 后台任务不拖垮主进程
            print(f"[patrol] 巡检异常: {exc}")
        _stop.wait(interval)


def start(interval=5.0):
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,),
                               name="patrol", daemon=True)
    _thread.start()
    return _thread


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=3)
