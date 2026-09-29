"""进程内监控任务：周期采样探头，更新水槽看板并生成告警/损耗提醒。

- 水温超出品种适温区间 -> water_temp 告警
- 溶氧低于品种要求 -> low_oxygen 告警，并对该槽在池批次生成 loss_reminder
- 同一水槽同类 open 告警去重，不重复刷
"""
from __future__ import annotations

import threading
import time

from .db import DB
from .devices import WaterProbeAdapter
from .services import QueryService, WriteService, now


class TankMonitor(threading.Thread):
    def __init__(self, db: DB, interval: float = 5.0):
        super().__init__(daemon=True, name="tank-monitor")
        self.db = db
        self.interval = interval
        self.queries = QueryService(db)
        self.writes = WriteService(db)
        self._stop = threading.Event()
        self.probes: dict[int, WaterProbeAdapter] = {}

    def stop(self) -> None:
        self._stop.set()

    def set_scenario(self, tank_id: int, scenario: str) -> None:
        """测试/演示接口：给某水槽注入 heat / low_oxygen / normal。"""
        self._probe_for(self.db.query_one("SELECT * FROM tanks WHERE id=?", (tank_id,)))
        self.probes[tank_id].set_scenario(scenario)

    def _baseline_for(self, tank) -> tuple[float, float]:
        """按水槽内在养品种给出安全基线：适温中值、溶氧高于下限 1.2mg/L。"""
        r = self.db.query_one(
            """SELECT AVG((sp.temp_min+sp.temp_max)/2.0) t, AVG(sp.min_oxygen)+1.2 o
               FROM batches b JOIN species sp ON sp.id=b.species_id
               WHERE b.tank_id=? AND b.status='在池'""", (tank["id"],))
        if r and r["t"] is not None:
            return round(r["t"], 1), round(r["o"], 1)
        sal_base = {"海水": 18.5, "咸淡水": 22.0, "淡水": 20.0}
        return sal_base.get(tank["salinity"], 19.0), 6.8

    def _probe_for(self, tank) -> WaterProbeAdapter:
        if tank["id"] not in self.probes:
            bt, bo = self._baseline_for(tank)
            self.probes[tank["id"]] = WaterProbeAdapter(tank["code"], base_temp=bt,
                                                        base_oxygen=bo)
        return self.probes[tank["id"]]

    def scan_once(self) -> int:
        """执行一轮采样，返回本轮新增告警数。"""
        new_alerts = 0
        with self.db.transaction() as conn:
            tanks = conn.execute("SELECT * FROM tanks ORDER BY id").fetchall()
            for tank in tanks:
                reading = self._probe_for(tank).read()
                conn.execute("UPDATE tanks SET probe_temp=?, probe_oxygen=? WHERE id=?",
                             (reading.temp_c, reading.oxygen_mg_l, tank["id"]))
                conn.execute(
                    "INSERT INTO holding_logs(tank_id,temp,oxygen,recorded_at) VALUES(?,?,?,?)",
                    (tank["id"], reading.temp_c, reading.oxygen_mg_l, now()))

                batches = conn.execute(
                    "SELECT * FROM batches WHERE tank_id=? AND status='在池'",
                    (tank["id"],)).fetchall()
                for b in batches:
                    sp = conn.execute("SELECT * FROM species WHERE id=?",
                                      (b["species_id"],)).fetchone()
                    if not (sp["temp_min"] <= reading.temp_c <= sp["temp_max"]):
                        msg = (f"水温异常：{tank['code']} {reading.temp_c}℃，超出 {sp['name']} "
                               f"适温 {sp['temp_min']}~{sp['temp_max']}℃，请检查制冷")
                        if not self._has_open(conn, "water_temp", tank["id"]):
                            self._alert(conn, "water_temp", msg, tank["id"], b["id"], "严重")
                            new_alerts += 1
                    if reading.oxygen_mg_l < sp["min_oxygen"]:
                        msg = (f"缺氧：{tank['code']} 溶氧 {reading.oxygen_mg_l}mg/L < "
                               f"{sp['name']} 要求 {sp['min_oxygen']}mg/L，请增氧")
                        if not self._has_open(conn, "low_oxygen", tank["id"]):
                            self._alert(conn, "low_oxygen", msg, tank["id"], b["id"], "严重")
                            # 进程内任务生成损耗提醒，提示仓管核查死亡数量
                            self._alert(conn, "loss_reminder",
                                        f"损耗提醒：{tank['code']} 缺氧风险，批次 {b['batch_no']} "
                                        f"({sp['name']}) 请立即抽检并报损",
                                        tank["id"], b["id"], "警示")
                            new_alerts += 2
        return new_alerts

    @staticmethod
    def _has_open(conn, type_: str, tank_id: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM alerts WHERE type=? AND tank_id=? AND status='open' LIMIT 1",
            (type_, tank_id)).fetchone() is not None

    @staticmethod
    def _alert(conn, type_: str, message: str, tank_id: int, batch_id: int, level: str) -> None:
        conn.execute(
            "INSERT INTO alerts(type,level,tank_id,batch_id,message,status,created_at) "
            "VALUES(?,?,?,?,?,'open',?)",
            (type_, level, tank_id, batch_id, message, now()))

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.scan_once()
            except Exception as exc:  # 监控线程不能因单轮错误退出
                print(f"[monitor] scan error: {exc}")
