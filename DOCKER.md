# 容器化部署（Dockerfile + 端口映射 + SSH 运维）

> 你要的：**Docker 构建命令** + **端口映射** + **容器 SSH 授权**。
> 下面全部准备好，构建完把连接信息给我，我进来部署和自检。

---

## 1. 容器相关文件（都在 `pdf-burn/`）

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 镜像：python:3.12-slim + PyMuPDF + sshd（只允许公钥登录） |
| `entrypoint.sh` | 同时拉起 sshd 和 `server.py`，任一挂掉容器就重启 |
| `docker-compose.yml` | 端口映射 + 数据卷（推荐用这个） |
| `.dockerignore` | 排除 `data/` 和**私钥**，不会被塞进镜像 |
| `ssh/authorized_keys` | 你自己的公钥。仓库里是占位，构建前把公钥粘进去 |
| `ssh/deploy_key` | 对应私钥，只留在本机。`.gitignore` 已排除，不要提交 |

> ⚠️ 注意：`Dockerfile` 只 **COPY 公钥**，私钥不在 `COPY` 列表里，也在 `.dockerignore` 里排除了。
> 也就是说：**镜像里没有私钥，只有"允许谁进来"的公钥**。

---

## 2. 构建

在 `pdf-burn/` 目录下：

```bash
docker build -t burnpdf:1.0 .
```

> 镜像基于官方 `python:3.12-slim`，再用 `apt` 安装 openssh-server，`pip` 安装 pymupdf。
> 如果 `pip` 装 pymupdf 失败（网络慢），加国内源：
> ```dockerfile
> RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple pymupdf==1.28.2
> ```

---

## 3. 运行（端口一定要映射出来）

### 方式 A：docker compose（推荐）

```bash
docker compose up -d --build
docker compose logs -f
```

### 方式 B：docker run

```bash
docker run -d --name burnpdf --restart unless-stopped \
  -p 8770:8770 \
  -p 2222:22 \
  -v "$PWD/data:/app/data" \
  -e TZ=Asia/Shanghai \
  burnpdf:1.0
```

Windows PowerShell 里 `-v` 的路径写法：

```powershell
docker run -d --name burnpdf --restart unless-stopped `
  -p 8770:8770 -p 2222:22 `
  -v ${PWD}/data:/app/data `
  -e TZ=Asia/Shanghai `
  burnpdf:1.0
```

| 映射 | 含义 |
|---|---|
| `8770:8770` | **对外服务端口** —— 你外层的网络/反代指向宿主机的 8770 |
| `2222:22` | **SSH 运维端口** —— 映射到宿主机 2222，避开宿主机自己的 22 |
| `./data:/app/data` | **必须挂**：SQLite、已渲染图片、管理密钥都在这里；不挂容器一重建就全丢 |

---

## 4. 先自己验一遍（30 秒）

```bash
# 1) 服务活着吗
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8770/admin      # 期望 200

# 2) 管理密钥（在容器里，自己看，别贴到公开地方）
docker exec burnpdf cat /app/data/admin.key

# 3) 内置回归测试：21 条断言，覆盖计次、扣完次仍能取图、刷新不重复扣次、
#    60 秒宽限退次数、作废、过期、鉴权
docker exec burnpdf python3 /app/selftest.py
```

`selftest.py` 期望结尾是：

```
====================================================
  通过 21 / 21
====================================================
```

---

## 5. 部署后自检

1. 确认 `sshd` 和 `server.py` 都在跑（`ps`、`docker logs`）；
2. `curl 127.0.0.1:8770/admin` 应返回 200；
3. 跑 `/app/selftest.py`，21 条应该全部通过；
4. 发布一份样例 PDF（限 1 次），用手机点一次，再点一次确认显示「链接已失效」；
5. 重启容器，确认 `data/` 还在挂载卷上。

登入容器：

```bash
ssh -i ssh/deploy_key -p 2222 -o StrictHostKeyChecking=accept-new root@<IP>
```

---

## 6. 用完怎么撤掉 SSH

```bash
# ① 删掉容器里的授权公钥
docker exec burnpdf rm -f /root/.ssh/authorized_keys

# ② 重建容器时不映射 2222
docker rm -f burnpdf && docker compose up -d --build

# ③ 宿主机防火墙或安全组撤掉 2222
```

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `bash: /app/entrypoint.sh: /bin/bash^M: bad interpreter` | 脚本被 Windows 换成了 CRLF。Dockerfile 里已经 `sed -i 's/\r$//'` 处理；若还报，把 `entrypoint.sh` 转成 LF 再 build |
| `pip install pymupdf` 超时 | 换国内源（见 §2） |
| `curl` 返回 `000` | 容器没起来：`docker logs burnpdf` 看错 |
| 宿主机 2222 被占用 | 换成 `-p 2223:22`，登入时改端口 |
| 数据重启后没了 | 忘了挂 `-v`，见 §3 表格最后一行 |
| 想改端口 8770 | 改 `docker-compose.yml` 左侧数字即可（右侧容器内保持 8770） |

---

## 9. 备份（以后用得上）

```bash
# 整个数据目录打包（含数据库 + 图片 + 管理密钥）
docker run --rm -v "$PWD/data:/d" -v "$PWD:/b" alpine tar czf /b/burnpdf-data-$(date +%F).tgz -C /d .
```
