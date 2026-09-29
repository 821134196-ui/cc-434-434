"""海鲜档口活鲜暂养平台 —— 存储层（SQLite）"""
from __future__ import annotations

import contextlib
import itertools
import sqlite3
import threading
from pathlib import Path

_mem_seq = itertools.count(1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stalls(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS tanks(
  id INTEGER PRIMARY KEY,
  stall_id INTEGER NOT NULL REFERENCES stalls(id),
  zone TEXT NOT NULL,            -- A区/B区/C区
  code TEXT UNIQUE NOT NULL,     -- A-01
  capacity_kg REAL NOT NULL,     -- 容量上限
  salinity TEXT NOT NULL,        -- 海水/淡水/咸淡水
  probe_temp REAL,               -- 最新探头水温
  probe_oxygen REAL             -- 最新探头溶氧 mg/L
);
CREATE TABLE IF NOT EXISTS species(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  spec TEXT NOT NULL,            -- 默认规格
  salinity TEXT NOT NULL,        -- 盐度要求
  temp_min REAL NOT NULL, temp_max REAL NOT NULL,
  min_oxygen REAL NOT NULL,
  unit_price REAL NOT NULL,      -- 结算单价（演示）
  mortality_threshold REAL NOT NULL DEFAULT 0.05  -- 死亡率告警阈值
);
CREATE TABLE IF NOT EXISTS species_conflicts(
  species_a INTEGER NOT NULL REFERENCES species(id),
  species_b INTEGER NOT NULL REFERENCES species(id),
  note TEXT,
  PRIMARY KEY(species_a, species_b)
);
CREATE TABLE IF NOT EXISTS suppliers(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_batches(
  id INTEGER PRIMARY KEY,
  batch_no TEXT UNIQUE NOT NULL,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  arrived_at TEXT NOT NULL,
  settled INTEGER NOT NULL DEFAULT 0,
  settled_at TEXT
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY,
  batch_no TEXT UNIQUE NOT NULL,      -- 池内批次号
  source_batch_id INTEGER NOT NULL REFERENCES source_batches(id),
  species_id INTEGER NOT NULL REFERENCES species(id),
  spec TEXT NOT NULL,
  tank_id INTEGER NOT NULL REFERENCES tanks(id),
  weight_in_kg REAL NOT NULL,         -- 入池重量（原始）
  remaining_kg REAL NOT NULL,         -- 剩余可分拣
  locked_kg REAL NOT NULL DEFAULT 0,  -- 订单锁定中
  sorted_kg REAL NOT NULL DEFAULT 0,  -- 累计分拣
  loss_kg REAL NOT NULL DEFAULT 0,    -- 累计死亡/损耗
  status TEXT NOT NULL DEFAULT '在池',  -- 在池/已出清
  arrived_at TEXT NOT NULL
);
-- 称重记录：只增不改，补录也保留原始值与设备
CREATE TABLE IF NOT EXISTS weighings(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  kind TEXT NOT NULL,                 -- 入池/补录
  weight_kg REAL NOT NULL,            -- 本次读数
  device_id TEXT NOT NULL,
  reading_token TEXT NOT NULL,        -- 设备读数唯一令牌（去重用）
  operator TEXT NOT NULL,
  note TEXT,
  recorded_at TEXT NOT NULL,
  UNIQUE(device_id, reading_token)
);
CREATE TABLE IF NOT EXISTS holding_logs(
  id INTEGER PRIMARY KEY,
  tank_id INTEGER NOT NULL REFERENCES tanks(id),
  batch_id INTEGER REFERENCES batches(id),
  temp REAL, oxygen REAL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,   -- water_temp/low_oxygen/mix_conflict/weight_diff/order_cancel/mortality/loss_reminder
  level TEXT NOT NULL DEFAULT '警示',
  tank_id INTEGER REFERENCES tanks(id),
  batch_id INTEGER REFERENCES batches(id),
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',  -- open/acked
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY,
  order_no TEXT UNIQUE NOT NULL,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL DEFAULT '待分拣', -- 待分拣/分拣中/已完成/已取消
  locked_by INTEGER REFERENCES employees(id),
  created_at TEXT NOT NULL,
  sorted_at TEXT,
  cancel_reason TEXT
);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  qty_kg REAL NOT NULL,
  status TEXT NOT NULL DEFAULT '锁定'    -- 锁定/已出池/已释放
);
-- 死亡/损耗，必须关联原批次
CREATE TABLE IF NOT EXISTS losses(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  qty_kg REAL NOT NULL,
  reason TEXT NOT NULL,               -- 死亡/缺氧/逃逸/失重
  device_id TEXT,
  operator TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
-- 称重差异（补录 vs 入池），供应商确认前只挂账
CREATE TABLE IF NOT EXISTS settlement_diffs(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  source_batch_id INTEGER NOT NULL REFERENCES source_batches(id),
  original_kg REAL NOT NULL,
  corrected_kg REAL NOT NULL,
  delta_kg REAL NOT NULL,
  amount REAL NOT NULL,               -- 差异金额（负数=扣减）
  status TEXT NOT NULL DEFAULT '待确认', -- 待确认/已确认/已驳回
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS settlements(
  id INTEGER PRIMARY KEY,
  source_batch_id INTEGER NOT NULL REFERENCES source_batches(id),
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  gross_kg REAL NOT NULL,
  loss_kg REAL NOT NULL,
  sorted_kg REAL NOT NULL,
  diff_amount REAL NOT NULL DEFAULT 0,
  payable REAL NOT NULL,
  status TEXT NOT NULL DEFAULT '待确认',
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_tank ON batches(tank_id);
CREATE INDEX IF NOT EXISTS idx_losses_batch ON losses(batch_id);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, type);
"""


class DB:
    """线程安全的 SQLite 封装；写操作使用 BEGIN IMMEDIATE 串行化。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        # :memory: 每连接私有，测试需要跨连接共享 -> 用 shared-cache 命名内存库，
        # 并由 _anchor 保持常驻连接使其不被回收。
        self._anchor: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self.uri = f"file:seafood_mem_{next(_mem_seq)}?mode=memory&cache=shared"
        else:
            self.uri = None
        self.init()

    def connect(self) -> sqlite3.Connection:
        target = self.uri or self.path
        conn = sqlite3.connect(target, timeout=10, isolation_level=None,
                               uri=bool(self.uri))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if self.uri is None:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init(self) -> None:
        if self.uri and self._anchor is None:
            self._anchor = self.connect()  # 常驻锚点连接
        with self._lock, contextlib.closing(self.connect()) as conn:
            conn.executescript(SCHEMA)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock, contextlib.closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock, contextlib.closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchone()

    def transaction(self):
        """返回一个已 BEGIN IMMEDIATE 的连接（用于行锁/条件更新）。"""
        return _Tx(self)


class _Tx:
    def __init__(self, db: DB):
        self.db = db
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.db._lock.acquire()
        self.conn = self.db.connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()
            self.db._lock.release()
        return False
