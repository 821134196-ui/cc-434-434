#!/usr/bin/env python3
"""单条本地启动命令:

    python3 run.py [--port 8000] [--fresh]

启动后自动:建库 → 写入演示数据 → 生成入池/分拣/报损样例 → 开启巡检任务 → 起 Web 服务。
浏览器访问 http://127.0.0.1:8000
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import db, tasks
from app.seed import seed_all
from app.web import make_server


def main():
    parser = argparse.ArgumentParser(description="活鲜暂养与损耗结算平台")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--fresh", action="store_true", help="删除旧库重新生成演示数据")
    args = parser.parse_args()

    if args.fresh and os.path.exists(db.db_path()):
        os.remove(db.db_path())

    db.init_db()
    result = seed_all()
    if result["samples"]:
        print("✔ 已生成演示数据:供应商/客户/品种/水槽 + 入池、分拣、报损、结算样例", flush=True)
    else:
        print("✔ 使用已有数据(加 --fresh 可重建)", flush=True)

    tasks.start(interval=5.0)
    print("✔ 进程内巡检任务已启动(水温/溶氧/损耗提醒,每 5s)", flush=True)

    server = make_server(args.host, args.port)
    print(f"✔ 平台已启动: http://{args.host}:{args.port}  (Ctrl+C 停止)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        tasks.stop()
        server.server_close()


if __name__ == "__main__":
    main()
