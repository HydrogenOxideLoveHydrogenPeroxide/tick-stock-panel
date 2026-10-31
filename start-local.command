#!/bin/bash
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT/backend"
export HOST=127.0.0.1 PORT=3018 ENABLE_AI=1 ENABLE_EXTERNAL_PULLS=0
export MPLCONFIGDIR="$ROOT/.mpl-cache" NUMBA_CACHE_DIR="$ROOT/.numba-cache"
echo "打开 http://127.0.0.1:3018；首次访问请设置至少12位独立密码。"
echo "关闭服务：在这个终端按 Control+C。不会自动杀死其他占用端口的程序。"
exec "$ROOT/backend/.venv-local/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 3018 --no-proxy-headers --loop asyncio
