#!/bin/bash
# Run sshd and the application. If either exits, stop the container.
set -e

mkdir -p /run/sshd

echo "[entrypoint] sshd 监听 :22"
/usr/sbin/sshd -D -e &
SSHD_PID=$!

echo "[entrypoint] burnpdf 监听 :8770"
python3 /app/server.py &
APP_PID=$!

shutdown() {
  kill -TERM "$SSHD_PID" "$APP_PID" 2>/dev/null || true
}
trap shutdown TERM INT

wait -n || true
echo "[entrypoint] 有进程退出，容器停止"
shutdown
exit 1
