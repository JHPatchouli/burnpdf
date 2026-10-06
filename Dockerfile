# syntax=docker/dockerfile:1
#
# 阅后即焚 PDF —— 容器镜像
#
# 构建：
#   docker build -t burnpdf:1.0 .
#
# 运行（端口一定要映射出来）：
#   docker run -d --name burnpdf --restart unless-stopped \
#     -p 8770:8770 -p 2222:22 \
#     -v "$PWD/data:/app/data" burnpdf:1.0
#
# 说明：
#   · 8770 = 对外服务端口（外层 Nginx/Caddy 反代指着它）
#   · 22   = 容器内 sshd（只允许公钥登录，禁止密码）

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

# ---- 系统依赖：sshd（运维登入）+ 时区 ----
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        openssh-server tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && mkdir -p /run/sshd

# ---- sshd 策略：只许 root 用公钥登录，禁止密码 ----
RUN sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config \
 && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config \
 && sed -i 's/^#\?UsePAM.*/UsePAM no/' /etc/ssh/sshd_config \
 && ssh-keygen -A

# ---- 授权公钥（只 COPY 公钥，私钥绝不进镜像）----
COPY ssh/authorized_keys /root/.ssh/authorized_keys
RUN chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys

# ---- 应用依赖：只有 PyMuPDF 一个 ----
WORKDIR /app
RUN pip install --no-cache-dir pymupdf==1.28.2

# ---- 应用代码（显式列出，避免把 data/ 和私钥带进镜像）----
COPY server.py viewer.html admin.html publish.py make_sample.py selftest.py /app/
COPY README.md DEPLOY.md /app/

RUN mkdir -p /app/data

# entrypoint.sh 从 Windows 传上来会带 CRLF → 一定要清掉，否则报
# 「/bin/bash^M: bad interpreter」
COPY entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

EXPOSE 8770 22

CMD ["/app/entrypoint.sh"]
