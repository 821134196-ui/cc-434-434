"""测试公共基座:每个用例独立临时数据库 + 基础演示数据。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402
from app.seed import seed_base  # noqa: E402


class AquaTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.dbfile = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.dbfile)  # 让 init_db 从零建库
        db.set_db_path(self.dbfile)
        db.init_db()
        seed_base()

    def tearDown(self):
        try:
            db.get_conn().close()
        except Exception:
            pass
        if os.path.exists(self.dbfile):
            os.unlink(self.dbfile)

    # ---- 查询助手 ----
    def id_of(self, table, name):
        return db.q1(f"SELECT id FROM {table} WHERE name=?", (name,))["id"]

    def tank_id(self, code):
        return db.q1("SELECT id FROM tanks WHERE code=?", (code,))["id"]

    def batch(self, batch_id):
        return dict(db.q1("SELECT * FROM batches WHERE id=?", (batch_id,)))

    def alerts(self, kind=None):
        if kind:
            return db.q("SELECT * FROM alerts WHERE kind=?", (kind,))
        return db.q("SELECT * FROM alerts")
