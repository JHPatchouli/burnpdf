#!/bin/bash
# Build and replace the burnpdf container.
#
#   bash deploy.sh
#   PORT=18770 SSH_PORT=12222 bash deploy.sh
#
# The script adds the burnpdf image, the burnpdf container, and /root/pdf-burn.
# A busy port moves to the next free port. It removes only its own container,
# and it does not change the reverse proxy, firewall policy, or other containers.
set -uo pipefail

APP=burnpdf
IMG=burnpdf:1.0
SRC="$(cd "$(dirname "$0")" && pwd)"
DATA=/root/pdf-burn/data

say() { echo "[deploy] $*" >&2; }
die() { echo "[deploy][错误] $*" >&2; exit 1; }

# ---------- 1 环境 ----------
say "1/6 环境检查"
command -v docker >/dev/null 2>&1 || die "没有 docker，先装（见 README）"
docker info >/dev/null 2>&1 || die "docker daemon 没在跑"
say "  Docker  $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
say "  系统    $(cat /etc/redhat-release 2>/dev/null || uname -sr)"
say "  SELinux $(getenforce 2>/dev/null || echo N/A)   内存 $(free -m | awk 'NR==2{print $2}')MB   磁盘 $(df -h / | awk 'NR==2{print $4}') 可用"

# Remove the previous burnpdf container before choosing ports.
# Otherwise its own ports look busy and the replacement moves elsewhere.
say "2/6 检查同名容器（先删，再选端口）"
if docker ps -a --format '{{.Names}}' | grep -qx "$APP"; then
  say "  已有同名容器，先删除"
  docker rm -f "$APP" >/dev/null || die "删除旧容器失败"
  sleep 1   # wait for docker-proxy to release the ports
fi

# ---------- 3 端口：被占就让位 ----------
say "3/6 端口选择（只找空位，不 kill 任何东西）"
busy() {
  if command -v ss >/dev/null 2>&1; then
    ss -lntH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
  else
    netstat -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
  fi
}
PORT="${PORT:-8770}"
SSH_PORT="${SSH_PORT:-2222}"
while busy "$PORT"; do say "  服务端口 $PORT 被占用 → 试 $((PORT + 1))"; PORT=$((PORT + 1)); done
while busy "$SSH_PORT"; do say "  SSH 端口 $SSH_PORT 被占用 → 试 $((SSH_PORT + 1))"; SSH_PORT=$((SSH_PORT + 1)); done
while [ "$SSH_PORT" = "$PORT" ]; do SSH_PORT=$((SSH_PORT + 1)); done
say "  选用：服务 $PORT ，SSH $SSH_PORT"
if [ "$PORT" != "8770" ] || [ "$SSH_PORT" != "2222" ]; then
  say "  [注意] 期望 8770/2222，实际 $PORT/$SSH_PORT"
  say "         反向代理如果写死了默认端口，需要同步修改"
fi

# ---------- 4 数据目录 ----------
say "4/6 数据目录 $DATA（已存在则保留原数据）"
mkdir -p "$DATA" || die "建目录失败"

# ---------- 5 构建 ----------
say "5/6 构建镜像 $IMG"
if ! docker build -t "$IMG" "$SRC"; then
  die "构建失败（pip 拉包超时的话，把 Dockerfile 里的 pip 换成清华源）"
fi
docker image ls "$IMG" --format '  镜像 {{.Repository}}:{{.Tag}}  {{.Size}}' >&2

# ---------- 6 启动 ----------
say "6/6 启动容器"

# Mount the source directory at /app. Edits there survive a restart, and
# /app/data is the same directory as $DATA. Stop if a required file is missing.
for f in server.py viewer.html admin.html publish.py make_sample.py selftest.py entrypoint.sh; do
  [ -f "$SRC/$f" ] || die "缺少 $f，已中止"
done

# A bind mount hides the image copy, so repeat the entrypoint fixes here.
# The image build fixes entrypoint.sh, but a bind mount hides those fixes.
# Normalize line endings and the executable bit on the host copy before starting.
sed -i 's/\r$//' "$SRC/entrypoint.sh" 2>/dev/null || true
chmod +x "$SRC/entrypoint.sh" || die "无法给 entrypoint.sh 加执行权限"

if ! docker run -d --name "$APP" --restart unless-stopped \
      -p "$PORT:8770" \
      -p "$SSH_PORT:22" \
      -v "$SRC:/app" \
      -e TZ=Asia/Shanghai \
      "$IMG" >/dev/null; then
  die "启动失败"
fi

code=""
for _ in $(seq 1 25); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/admin" 2>/dev/null || true)
  [ "$code" = "200" ] && break
  sleep 1
done
if [ "$code" != "200" ]; then
  docker logs --tail 50 "$APP" >&2
  die "服务没起来（HTTP ${code:-无响应}）"
fi
say "服务已就绪"

say "跑接口自检（21 条）"
docker exec "$APP" python3 /app/selftest.py 2>&1 | tail -4 >&2

# ---------- 报告（stdout） ----------
echo
echo "================= 部署报告 ================="
echo "容器      : $APP   (image $IMG)"
echo "服务端口  : $PORT   ← 容器内 8770，外层反代指这个"
echo "SSH 端口  : $SSH_PORT   ← 容器内 22"
echo "数据目录  : $DATA  （= $SRC/data，随 /app 一起挂载）"
echo "管理密钥  : docker exec $APP cat /app/data/admin.key"
echo "日志      : docker logs -f $APP"
echo "==========================================="
echo "未改动：其他容器 / nginx / firewalld 全局策略 / 系统包"
