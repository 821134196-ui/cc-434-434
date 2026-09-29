"""SQLite 数据层:schema、连接助手与通用查询。"""
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("AQUA_DB", os.path.join(os.path.dirname(__file__), "..", "data", "aqua.db"))

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stalls (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  zone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tanks (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,          -- 如 A-01
  stall_id INTEGER NOT NULL REFERENCES stalls(id),
  zone TEXT NOT NULL,
  capacity_kg REAL NOT NULL,
  salinity REAL NOT NULL,             -- 当前盐度 ‰
  status TEXT NOT NULL DEFAULT '正常'  -- 正常 / 异常
);

CREATE TABLE IF NOT EXISTS species (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  salinity_min REAL NOT NULL,
  salinity_max REAL NOT NULL,
  temp_min REAL NOT NULL,
  temp_max REAL NOT NULL,
  unit_price REAL NOT NULL DEFAULT 0   -- 演示结算单价(元/kg)
);

-- 混养冲突:同一水槽不允许共存的品种对
CREATE TABLE IF NOT EXISTS species_conflicts (
  species_a INTEGER NOT NULL REFERENCES species(id),
  species_b INTEGER NOT NULL REFERENCES species(id),
  UNIQUE(species_a, species_b)
);

CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  contact TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  contact TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL,                 -- admin / staff / supplier
  supplier_id INTEGER REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT UNIQUE NOT NULL,
  species_id INTEGER NOT NULL REFERENCES species(id),
  spec TEXT NOT NULL,                 -- 规格,如 500g/只
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  source_batch_no TEXT NOT NULL,      -- 来源批次
  tank_id INTEGER NOT NULL REFERENCES tanks(id),
  entry_weight REAL NOT NULL,
  current_weight REAL NOT NULL,
  loss_weight REAL NOT NULL DEFAULT 0,
  out_weight REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT '在养',  -- 在养 / 已清空 / 已结算
  settled INTEGER NOT NULL DEFAULT 0,
  entry_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weighings (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  kind TEXT NOT NULL,                 -- 入池 / 补录 / 出池
  weight REAL NOT NULL,
  device_id TEXT NOT NULL,
  idem_key TEXT UNIQUE,               -- 称重去重键(设备+序号)
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holding_records (
  id INTEGER PRIMARY KEY,
  tank_id INTEGER NOT NULL REFERENCES tanks(id),
  batch_id INTEGER REFERENCES batches(id),
  water_temp REAL,
  salinity REAL,
  oxygen REAL,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  order_no TEXT UNIQUE NOT NULL,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL DEFAULT '待分拣',  -- 待分拣 / 分拣中 / 已完成 / 已取消
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  species_id INTEGER NOT NULL REFERENCES species(id),
  spec TEXT NOT NULL,
  qty REAL NOT NULL,
  sorted_qty REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sortings (
  id INTEGER PRIMARY KEY,
  order_item_id INTEGER NOT NULL REFERENCES order_items(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  qty REAL NOT NULL,
  employee TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '已锁定',  -- 已锁定 / 已释放
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loss_records (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),  -- 必须关联原批次
  qty REAL NOT NULL,
  reason TEXT NOT NULL,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER UNIQUE NOT NULL REFERENCES batches(id),
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  entry_weight REAL NOT NULL,
  loss_weight REAL NOT NULL,
  out_weight REAL NOT NULL,
  settle_weight REAL NOT NULL,
  unit_price REAL NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL DEFAULT '待供应商确认',  -- 待供应商确认 / 已确认 / 已结算
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);

-- 补录称重产生的待结算差异(供应商确认前仅挂账)
CREATE TABLE IF NOT EXISTS settlement_diffs (
  id INTEGER PRIMARY KEY,
  settlement_id INTEGER REFERENCES settlements(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  kind TEXT NOT NULL DEFAULT '称重差异',
  expect_weight REAL NOT NULL,
  actual_weight REAL NOT NULL,
  diff REAL NOT NULL,
  status TEXT NOT NULL DEFAULT '待确认',  -- 待确认 / 已确认 / 已驳回
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,   -- 水温异常/缺氧/混养冲突/称重差异/订单取消/死亡率超阈值/损耗提醒
  tank_id INTEGER,
  batch_id INTEGER,
  message TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '警告',     -- 警告 / 严重
  status TEXT NOT NULL DEFAULT '未处理',  -- 未处理 / 已处理
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_tank ON batches(tank_id, status);
CREATE INDEX IF NOT EXISTS idx_holding_tank ON holding_records(tank_id, id);
CREATE INDEX IF NOT EXISTS idx_sortings_batch ON sortings(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, kind);
"""


def db_path():
    return DB_PATH


def set_db_path(path):
    """测试用:切换数据库文件并重置连接。"""
    global DB_PATH
    DB_PATH = path
    if hasattr(_local, "conn"):
        try:
            _local.conn.close()
        except Exception:
            pass
        del _local.conn


def get_conn():
    """每线程一个连接,开启外键与行字典。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(immediate=True):
    """写事务上下文;immediate=True 使用 BEGIN IMMEDIATE 保证串行写。"""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def q(sql, args=()):
    return get_conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    return get_conn().execute(sql, args).fetchone()
