"""HTTP 层:标准库 ThreadingHTTPServer 提供 JSON API 与静态页面。

用户身份通过请求头 X-User-Name 传递(演示用),权限在服务层校验。
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import q, q1
from .devices import hub
from .services import (ServiceError, cancel_order, create_order, dashboard,
                       entry_batch, generate_settlement, confirm_settlement,
                       list_batches, list_losses, list_orders, list_pending_diffs,
                       list_settlements, report_loss, resolve_diff, reweigh_batch,
                       sort_out)
from .tasks import patrol_once

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


def current_user(headers):
    """优先按 X-User-Id 识别(ASCII 安全);兼容 X-User-Name(自动修复编码)。"""
    uid = headers.get("X-User-Id")
    if uid:
        row = q1("SELECT * FROM users WHERE id=?", (uid,))
        if row:
            return dict(row)
    name = headers.get("X-User-Name", "")
    if name:
        try:  # 非 ASCII 头值按 latin-1 解码过,还原为 UTF-8
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        row = q1("SELECT * FROM users WHERE name=?", (name,))
        if row:
            return dict(row)
    row = q1("SELECT * FROM users WHERE role='admin' LIMIT 1")
    return dict(row) if row else {"id": 0, "name": "访客", "role": "staff",
                                  "supplier_id": None}


# ------------------------------------------------------------ 路由表
# 每项: (方法, 正则, 处理函数)。处理函数签名 (ctx, **路径参数)。
ROUTES = []


def route(method, pattern):
    regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")
    def deco(fn):
        ROUTES.append((method, regex, fn))
        return fn
    return deco


@route("GET", "/api/meta")
def api_meta(ctx):
    return {
        "species": [dict(r) for r in q("SELECT * FROM species ORDER BY id")],
        "tanks": [dict(r) for r in q("SELECT * FROM tanks ORDER BY zone, code")],
        "suppliers": [dict(r) for r in q("SELECT * FROM suppliers ORDER BY id")],
        "customers": [dict(r) for r in q("SELECT * FROM customers ORDER BY id")],
        "users": [dict(r) for r in q("SELECT * FROM users ORDER BY id")],
        "conflicts": [dict(r) for r in q(
            "SELECT c.*, a.name AS a_name, b.name AS b_name FROM species_conflicts c "
            "JOIN species a ON a.id=c.species_a JOIN species b ON b.id=c.species_b")],
    }


@route("GET", "/api/dashboard")
def api_dashboard(ctx):
    return dashboard()


@route("GET", "/api/batches")
def api_batches(ctx):
    return list_batches(status=ctx["query"].get("status"),
                        supplier_id=ctx["query"].get("supplier_id"))


@route("POST", "/api/entry")
def api_entry(ctx):
    b = ctx["body"]
    return entry_batch(
        species_id=int(b["species_id"]), spec=b["spec"],
        supplier_id=int(b["supplier_id"]), source_batch_no=b["source_batch_no"],
        tank_id=int(b["tank_id"]), weight=float(b["weight"]),
        device_id=b.get("device_id", "SCALE-01"), idem_key=b.get("idem_key"))


@route("POST", "/api/batches/<batch_id>/reweigh")
def api_reweigh(ctx, batch_id):
    b = ctx["body"]
    return reweigh_batch(int(batch_id), float(b["weight"]),
                         b.get("device_id", "SCALE-01"), b.get("idem_key"),
                         operator=ctx["user"]["name"])


@route("GET", "/api/holding")
def api_holding(ctx):
    tank_id = ctx["query"].get("tank_id")
    sql = ("SELECT h.*, t.code AS tank_code FROM holding_records h "
           "JOIN tanks t ON t.id=h.tank_id")
    args = []
    if tank_id:
        sql += " WHERE h.tank_id=?"
        args.append(tank_id)
    sql += " ORDER BY h.id DESC LIMIT 200"
    return [dict(r) for r in q(sql, args)]


@route("GET", "/api/orders")
def api_orders(ctx):
    return list_orders()


@route("POST", "/api/orders")
def api_create_order(ctx):
    b = ctx["body"]
    return create_order(int(b["customer_id"]), b["items"])


@route("POST", "/api/orders/<order_id>/cancel")
def api_cancel_order(ctx, order_id):
    return cancel_order(int(order_id), operator=ctx["user"]["name"])


@route("POST", "/api/sorting")
def api_sorting(ctx):
    b = ctx["body"]
    return sort_out(int(b["order_item_id"]), int(b["batch_id"]),
                    float(b["qty"]), b.get("employee") or ctx["user"]["name"])


@route("GET", "/api/loss")
def api_loss(ctx):
    return list_losses()


@route("POST", "/api/loss")
def api_report_loss(ctx):
    b = ctx["body"]
    return report_loss(int(b["batch_id"]), float(b["qty"]),
                       b["reason"], b.get("note", ""))


@route("GET", "/api/settlements")
def api_settlements(ctx):
    return list_settlements(ctx["user"])


@route("POST", "/api/settlements/generate")
def api_gen_settlement(ctx):
    return generate_settlement(int(ctx["body"]["batch_id"]))


@route("POST", "/api/settlements/<sid>/confirm")
def api_confirm_settlement(ctx, sid):
    return confirm_settlement(int(sid), ctx["user"])


@route("GET", "/api/diffs")
def api_diffs(ctx):
    return list_pending_diffs(ctx["user"])


@route("POST", "/api/diffs/<diff_id>/resolve")
def api_resolve_diff(ctx, diff_id):
    return resolve_diff(int(diff_id), bool(ctx["body"].get("accept")), ctx["user"])


@route("GET", "/api/alerts")
def api_alerts(ctx):
    status = ctx["query"].get("status", "未处理")
    return [dict(r) for r in q(
        "SELECT a.*, t.code AS tank_code, b.batch_no FROM alerts a "
        "LEFT JOIN tanks t ON t.id=a.tank_id "
        "LEFT JOIN batches b ON b.id=a.batch_id "
        "WHERE a.status=? ORDER BY a.id DESC LIMIT 100", (status,))]


@route("POST", "/api/alerts/<alert_id>/resolve")
def api_resolve_alert(ctx, alert_id):
    from .db import transaction
    with transaction() as conn:
        conn.execute("UPDATE alerts SET status='已处理' WHERE id=?", (int(alert_id),))
    return {"ok": True}


@route("POST", "/api/devices/scale/read")
def api_scale_read(ctx):
    """模拟电子秤读数:返回重量 + 设备号 + 幂等键。"""
    true_weight = float(ctx["body"].get("true_weight", 50.0))
    return hub.scale.weigh(true_weight)


@route("POST", "/api/devices/anomaly")
def api_inject_anomaly(ctx):
    """演示用:向水槽注入水温/溶氧异常。"""
    b = ctx["body"]
    tank_id = int(b["tank_id"])
    if b.get("temp_delta"):
        hub.inject_temp_anomaly(tank_id, float(b["temp_delta"]))
    if b.get("oxygen_delta"):
        hub.inject_oxygen_anomaly(tank_id, float(b["oxygen_delta"]))
    return {"ok": True}


@route("POST", "/api/patrol")
def api_patrol(ctx):
    patrol_once()
    return {"ok": True}


# ------------------------------------------------------------ 请求处理

class Handler(BaseHTTPRequestHandler):
    server_version = "AquaPlatform/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---- 工具 ----
    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path):
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                ".css": "text/css", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _parse(self):
        from urllib.parse import urlparse, parse_qsl
        u = urlparse(self.path)
        query = dict(parse_qsl(u.query))
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {}
        return u.path, query, body

    def _dispatch(self, method):
        path, query, body = self._parse()
        if method == "GET" and not path.startswith("/api/"):
            if path in ("/", ""):
                return self._send_file(os.path.join(STATIC_DIR, "index.html"))
            safe = os.path.normpath(path).lstrip("/\\")
            return self._send_file(os.path.join(STATIC_DIR, safe))
        for m, regex, fn in ROUTES:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                ctx = {"query": query, "body": body,
                       "user": current_user(self.headers)}
                try:
                    result = fn(ctx, **match.groupdict())
                    self._send_json(result if result is not None else {"ok": True})
                except ServiceError as e:
                    self._send_json({"error": e.message}, e.status)
                except (KeyError, ValueError, TypeError) as e:
                    self._send_json({"error": f"参数错误: {e}"}, 400)
                except Exception as e:  # 兜底,避免连接悬挂
                    self._send_json({"error": f"服务器错误: {e}"}, 500)
                return
        self._send_json({"error": f"接口不存在: {method} {path}"}, 404)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def make_server(host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), Handler)
