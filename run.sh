#!/usr/bin/env bash
# 一条命令启动：重置 SQLite、生成入池/分拣/报损样例、打开水槽看板
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
exec python3 -m seafood.app --demo --port "$PORT"
