"""海鲜档口活鲜暂养与损耗结算平台 —— HTTP 服务（仅标准库）。

启动示例：
    python3 -m seafood.app --demo          # 重置库 + 样例数据 + 看板，端口 8000
    python3 -m seafood.app --port 9000     # 使用已有库启动
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .db import DB
from .monitor import TankMonitor
from .seed import generate_samples, seed
from .services import DomainError, QueryService, WriteService

STATIC_DIR = Path(__file__).parent / "static"

_db: DB = None  # type: ignore
_queries: QueryService = None  # type: ignore
_writes: WriteService = None  # type: ignore
_monitor: TankMonitor = None  # type: ignore


class Handler(BaseHTTPRequestHandler):
    server_version = "SeafoodTank/1.0"

    # ---------- 基础工具 ----------
    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON", "bad_json", 400)

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---------- GET ----------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                self._api_get(path)
            else:
                self._serve_static(path)
        except DomainError as e:
            self._send_json({"error": e.code, "message": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": "internal", "message": str(e)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            self._api_post(path)
        except DomainError as e:
            self._send_json({"error": e.code, "message": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": "internal", "message": str(e)}, 500)

    # ---------- API 路由 ----------
    def _api_get(self, path: str) -> None:
        if path == "/api/overview":
            return self._send_json({
                "tanks": _queries.tanks_overview(),
                "alerts": _queries.alerts(True),
            })
        if path == "/api/batches":
            return self._send_json({"batches": _queries.batches()})
        if path == "/api/holding":
            return self._send_json({"logs": _queries.holding_logs()})
        if path == "/api/orders":
            return self._send_json({"orders": _queries.orders()})
        if path == "/api/losses":
            return self._send_json({"losses": _queries.losses()})
        if path == "/api/settlements":
            return self._send_json({
                "settlements": _queries.settlements(),
                "diffs": _queries.pending_diffs(),
            })
        if path == "/api/sources":
            rows = _db.query(
                """SELECT sb.*, su.name supplier_name,
                          (SELECT COUNT(*) FROM batches b WHERE b.source_batch_id=sb.id) batch_cnt,
                          (SELECT COALESCE(SUM(weight_in_kg),0) FROM batches b WHERE b.source_batch_id=sb.id) gross_kg,
                          (SELECT COALESCE(SUM(loss_kg),0) FROM batches b WHERE b.source_batch_id=sb.id) loss_kg
                   FROM source_batches sb JOIN suppliers su ON su.id=sb.supplier_id
                   ORDER BY sb.id DESC""")
            return self._send_json({"sources": [dict(r) for r in rows]})
        if path == "/api/meta":
            return self._send_json({
                "species": [dict(r) for r in _db.query("SELECT * FROM species ORDER BY id")],
                "tanks": [dict(r) for r in _db.query(
                    "SELECT id,zone,code,capacity_kg,salinity FROM tanks ORDER BY zone,code")],
                "suppliers": [dict(r) for r in _db.query(
                    "SELECT id,name,token FROM suppliers ORDER BY id")],
                "customers": [dict(r) for r in _db.query("SELECT id,name FROM customers ORDER BY id")],
                "employees": [dict(r) for r in _db.query("SELECT id,name FROM employees ORDER BY id")],
            })
        raise DomainError("接口不存在", "not_found", 404)

    def _api_post(self, path: str) -> None:
        body = self._body()

        if path == "/api/intake":
            b = _writes.intake(
                species_id=int(body["species_id"]), spec=body.get("spec", ""),
                source_batch_no=body["source_batch_no"], supplier_id=int(body["supplier_id"]),
                weight_kg=float(body["weight_kg"]), tank_id=int(body["tank_id"]),
                device_id=body.get("device_id", "SCALE-WEB"),
                reading_token=body.get("reading_token") or f"web-{os.getpid()}-{_fresh_n()}",
                operator=body.get("operator", "老周（仓管）"),
                note=body.get("note", ""))
            return self._send_json({"ok": True, "batch": b}, 201)

        if path == "/api/reweigh":
            r = _writes.reweigh(
                batch_id=int(body["batch_id"]), corrected_kg=float(body["corrected_kg"]),
                device_id=body.get("device_id", "SCALE-WEB"),
                reading_token=body.get("reading_token") or f"web-{os.getpid()}-{_fresh_n()}",
                operator=body.get("operator", "老周（仓管）"))
            return self._send_json({"ok": True, "result": r}, 201)

        if path == "/api/losses":
            l = _writes.record_loss(
                batch_id=int(body["batch_id"]), qty_kg=float(body["qty_kg"]),
                reason=body["reason"], operator=body.get("operator", "老周（仓管）"),
                device_id=body.get("device_id", "MANUAL"))
            return self._send_json({"ok": True, "loss": l}, 201)

        if path == "/api/orders":
            o = _writes.create_order(
                customer_id=int(body["customer_id"]),
                employee_id=int(body["employee_id"]),
                items=body["items"])
            return self._send_json({"ok": True, "order": o}, 201)

        if path.endswith("/complete"):
            oid = int(path.split("/")[-2])
            return self._send_json({"ok": True, "result": _writes.complete_order(oid)})
        if path.endswith("/cancel"):
            oid = int(path.split("/")[-2])
            return self._send_json({"ok": True, "result": _writes.cancel_order(oid, body.get("reason", "客户取消"))})

        if path.startswith("/api/diffs/") and path.endswith("/confirm"):
            did = int(path.split("/")[-2])
            return self._send_json({"ok": True,
                                    "result": _writes.confirm_diff(did, body["token"])})

        if path == "/api/settlements":
            return self._send_json({"ok": True,
                                    "settlement": _writes.create_settlement(int(body["source_batch_id"]))}, 201)
        if path.startswith("/api/settlements/") and path.endswith("/confirm"):
            sid = int(path.split("/")[-2])
            return self._send_json({"ok": True,
                                    "result": _writes.confirm_settlement(sid, body["token"])})

        if path.startswith("/api/alerts/") and path.endswith("/ack"):
            _writes.ack_alert(int(path.split("/")[-2]))
            return self._send_json({"ok": True})

        if path == "/api/devices/scenario":
            _monitor.set_scenario(int(body["tank_id"]), body.get("scenario", "normal"))
            return self._send_json({"ok": True})
        if path == "/api/monitor/scan":
            return self._send_json({"ok": True, "new_alerts": _monitor.scan_once()})

        raise DomainError("接口不存在", "not_found", 404)

    # ---------- 静态文件 ----------
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(
            target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_seq = 0


def _fresh_n() -> int:
    global _seq
    _seq += 1
    return _seq


def build_app(db_path: str, reset: bool = False, with_samples: bool = False,
              with_monitor: bool = True, monitor_interval: float = 5.0) -> tuple[DB, TankMonitor | None]:
    global _db, _queries, _writes, _monitor
    _db = DB(db_path)
    seed(_db, reset=reset)
    if with_samples:
        generate_samples(_db)
    _queries = QueryService(_db)
    _writes = WriteService(_db)
    # 监控器实例常驻（手动注入/采样接口可用），仅按需启动后台线程
    _monitor = TankMonitor(_db, interval=monitor_interval)
    _monitor.scan_once()  # 启动即采一轮，按在养品种刷新安全基线
    if with_monitor:
        _monitor.start()
    return _db, _monitor


def main() -> None:
    ap = argparse.ArgumentParser(description="海鲜档口活鲜暂养与损耗结算平台")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=str(Path(__file__).parent / "seafood.db"))
    ap.add_argument("--demo", action="store_true",
                    help="重置数据库并生成入池/分拣/报损样例后启动")
    ap.add_argument("--reset", action="store_true", help="仅清空业务数据")
    ap.add_argument("--no-monitor", action="store_true", help="关闭后台探头轮询")
    ap.add_argument("--interval", type=float, default=5.0, help="探头采样间隔秒")
    args = ap.parse_args()

    _, monitor = build_app(args.db, reset=(args.demo or args.reset),
                           with_samples=args.demo, with_monitor=not args.no_monitor,
                           monitor_interval=args.interval)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"海鲜档口平台已启动: http://localhost:{args.port}  (Ctrl+C 停止)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor:
            monitor.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
