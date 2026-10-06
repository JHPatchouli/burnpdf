#!/bin/bash
# ============================================================
# burnpdf 一键部署（幂等 / 只增不改）
#
#   bash deploy.sh
#   PORT=18770 SSH_PORT=12222 bash deploy.sh     # 手动指定端口
#
# 安全约定（写死在脚本里，不会越界）：
#   · 只 【新增】镜像 burnpdf:1.0 + 容器 burnpdf + 目录 /root/pdf-burn
#   · 端口被占用 → 自动 +1 让位，【绝不 kill 任何进程】
#   · 只删【自己的】同名容器；不碰任何其他容器
#   · 不改 nginx/caddy 配置、不动 firewalld 全局策略、不重启其他服务
#   · 不跑 docker prune / system prune，不升级系统包
# ============================================================
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

# ---------- 2 同名容器（必须先删！）----------
# 旧容器是最后一个占着默认端口的。若先探测端口再删容器，
# 探测会把【自己的旧容器】当成"端口被占" → 自动漂移到别的端口
# → 外层反代 502，运维 SSH 失联。所以顺序必须是：先删、再选。
say "2/6 检查同名容器（先删，再选端口）"
if docker ps -a --format '{{.Names}}' | grep -qx "$APP"; then
  say "  发现已有的 $APP（我们自己的），先删掉它"
  docker rm -f "$APP" >/dev/null || die "删除旧容器失败"
  sleep 1   # 等 docker-proxy 真正释放端口
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
  say "         外层反代与运维脚本是按这两个端口写的，改了就必须同步改它们"
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

# /app 直接挂宿主机源码目录，好处：
#   · 在容器里改代码 = 改宿主机文件（持久），改完重启容器即生效，不用重建镜像
#   · /app/data 天然就是 $DATA，数据照旧落在宿主机，不用再单独挂一次
#   · 镜像里的 COPY 仍然保留（镜像自包含），只是运行时被这个挂载盖住
# 风险：如果 $SRC 缺文件，挂上去就是个坏容器 → 先校验，缺就中止
for f in server.py viewer.html admin.html publish.py make_sample.py selftest.py entrypoint.sh; do
  [ -f "$SRC/$f" ] || die "源码目录缺 $f —— 挂载 /app 会做出一个坏容器，已中止"
done

# 挂载会【盖住】镜像里的文件，于是构建期做过的修正全部失效，必须在这里补做：
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
