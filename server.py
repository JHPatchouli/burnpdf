#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阅后即焚 PDF 阅读服务（自建最小实现）
======================================================
思路：不发 PDF 文件，只发一条由服务端控制的链接。

  · PDF 在服务端就转成图片（原文件永远不下发）
  · 点「开始阅读」才扣次数；扣完次数链接即失效
  · 支持：限总次数 / 单次可看时长 / 到期时间 / 水印烧进图 / 一键作废 / 访问日志
  · 60 秒宽限：扣了次数但一页都没加载出来（网络抖动、误触）→ 自动退还次数

依赖：PyMuPDF（pymupdf），其余全是标准库。
运行：D:\\Python312\\python.exe server.py
"""

from __future__ import annotations

import base64
import contextlib
import json
import mimetypes
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import ipaddress

try:
    import pymupdf
except ImportError:  # 旧包名
    import fitz as pymupdf  # type: ignore

# ua-parser 是【可选】依赖，装在 ./vendor（挂载目录里 → 容器重建不丢，不用改镜像）。
# 它比自己写的正则准得多（实测能认出 Samsung SM-S918B 这种具体机型、
# 以及「微信内置浏览器」）。缺了也不影响服务：parse_ua() 会自动退回正则实现。
_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))
try:
    from ua_parser import parse as _ua_parse
except Exception:
    _ua_parse = None

# ---------------------------------------------------------------- 配置

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = DATA / "docs"
DB_PATH = DATA / "burn.db"
ADMIN_KEY_FILE = DATA / "admin.key"

HOST = "0.0.0.0"
PORT = 8770

DEFAULT_LIMIT = 1          # 默认：只能看 1 次
DEFAULT_DURATION = 600     # 默认单次可看 600 秒
DEFAULT_EXPIRE_DAYS = 3    # 默认 3 天后链接失效
DEFAULT_SCALE = 2.0        # 渲染倍率（2.0 ≈ 144dpi，够手机看）
DEFAULT_QUALITY = 88       # JPEG 质量
DEFAULT_STRIPS = 1         # 每页切几条（>1 可防止「长按保存一整页」）
IDLE_REFUND_SEC = 60       # 扣次后多少秒内没加载到页面就退次数
SCRAPE_SEC = 2.0           # 多少秒内取完整份文档的图块 → 判定为脚本抓图
SCRAPE_MIN_BLOCKS = 8      # 块数太少没有判定意义（防止误报小文档）
BACKUP_KEEP_DAYS = 7       # burn.db 备份保留份数（每天一份）
CLEAN_AFTER_DAYS = 30      # 过期超过这么多天 → 删文档文件（保留 db 记录）
MAX_UPLOAD = 80 * 1024 * 1024

# IP → 归属地。**只在管理台主动点击时才查**，且结果永久缓存。
# 刻意不做「自动批量查询」，就是为了让「把客户 IP 发给第三方」永远由人触发。
# ip-api.com 免费、无需注册、支持中文。
GEO_URL = ("http://ip-api.com/json/%s"
           "?lang=zh-CN&fields=status,country,regionName,city,isp,query")
GEO_TIMEOUT = 6.0

FONT_CJK = "china-s"       # PyMuPDF 内置简体中文字体

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
  token TEXT PRIMARY KEY,
  name TEXT, pages INTEGER, created REAL,
  limit_views INTEGER, used_views INTEGER DEFAULT 0,
  expire_ts REAL, duration_sec INTEGER,
  watermark TEXT, note TEXT,
  recipient TEXT,
  revoked INTEGER DEFAULT 0,
  strips INTEGER DEFAULT 1, quality INTEGER DEFAULT 88,
  scale REAL DEFAULT 2.0, fmt TEXT DEFAULT 'jpeg'
);
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY, token TEXT, ip TEXT, ua TEXT,
  start_ts REAL, last_ts REAL, state TEXT,
  pages_served INTEGER DEFAULT 0,
  scrape_flagged INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT, sid TEXT, ts REAL, ip TEXT, ua TEXT,
  action TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_token ON logs(token, ts);
-- IP 归属地缓存：同一个 IP 只查一次接口
CREATE TABLE IF NOT EXISTS iploc(
  ip TEXT PRIMARY KEY, loc TEXT, ts REAL
);
"""


def now() -> float:
    return time.time()


# ---------------------------------------------------------------- 数据库

@contextlib.contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log(conn, token, sid, action, detail="", ip="", ua=""):
    conn.execute(
        "INSERT INTO logs(token,sid,ts,ip,ua,action,detail) VALUES(?,?,?,?,?,?,?)",
        (token, sid, now(), ip, (ua or "")[:300], action, detail),
    )


def admin_key() -> str:
    if not ADMIN_KEY_FILE.exists():
        ADMIN_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        ADMIN_KEY_FILE.write_text(secrets.token_urlsafe(18), encoding="utf-8")
    return ADMIN_KEY_FILE.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------- 防爆破
# 管理密钥是「全部权限」，裸暴露在公网上必须限速：不限速的话，脚本能无限次试。
# 计数放在进程内存里（重启即清空）—— 不引入 redis 之类的额外依赖。
_FAILS: dict = {}
FAIL_WINDOW = 300.0     # 失败计数窗口：5 分钟
FAIL_MAX = 8            # 窗口内允许失败次数
BLOCK_SEC = 900.0       # 超限后封禁 15 分钟


def _fails_gc():
    """_FAILS 是攻击者可以撑大的字典，偶尔清一次，别把内存灌满。"""
    if len(_FAILS) < 1000:
        return
    t = now()
    for k in [k for k, v in _FAILS.items()
              if v["until"] < t and t - v["t0"] > FAIL_WINDOW]:
        _FAILS.pop(k, None)


def fail_check(ip):
    """返回 (还需等待秒数, 剩余额度)。wait>0 表示正处于封禁中。"""
    s = _FAILS.get(ip)
    t = now()
    if not s:
        return 0.0, FAIL_MAX
    if s["until"] > t:
        return s["until"] - t, 0
    if t - s["t0"] > FAIL_WINDOW:   # 窗口已过，重新开始
        _FAILS.pop(ip, None)
        return 0.0, FAIL_MAX
    return 0.0, max(0, FAIL_MAX - s["n"])


def fail_hit(ip):
    """记一次失败。返回 (本次是第几次, 是否刚被封)。"""
    t = now()
    s = _FAILS.get(ip)
    if not s or t - s["t0"] > FAIL_WINDOW:
        s = {"n": 0, "t0": t, "until": 0.0}
    s["n"] += 1
    blocked = s["n"] >= FAIL_MAX
    if blocked:
        s["until"] = t + BLOCK_SEC
        s["n"] = 0
    _FAILS[ip] = s
    _fails_gc()
    return (FAIL_MAX if blocked else s["n"]), blocked


def safe_int(v, default=0) -> int:
    """把 URL 上来的东西转 int，**绝不因为脏输入抛异常**（否则变成 500）。
    截断到 12 位也顺手挡住了 `?k=99999999...` 这种拿超长数字耗 CPU 的玩法。"""
    try:
        return int(str(v)[:12])
    except Exception:
        return default


def safe_float(v, default=0.0) -> float:
    """同上。额外排掉 NaN / inf —— `scale: NaN` 会让 Matrix 构造出奇怪东西。"""
    try:
        f = float(str(v)[:24])
        return default if (f != f or f in (float("inf"), float("-inf"))) else f
    except Exception:
        return default


def _peer_is_local(peer: str) -> bool:
    """对端地址是不是本机/内网（nginx、docker-proxy）。
    对端地址来自 TCP 连接，客户端改不了；X-Forwarded-For 是可以随便伪造的。"""
    try:
        a = ipaddress.ip_address(peer)
        return a.is_loopback or a.is_private
    except Exception:
        return False


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        # 轻量迁移：CREATE TABLE IF NOT EXISTS 不会给【已存在】的表加列，
        # 所以新增字段必须在这里单独补一次。ALTER 是幂等的（先查 PRAGMA）。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "scrape_flagged" not in cols:
            conn.execute("ALTER TABLE sessions "
                         "ADD COLUMN scrape_flagged INTEGER DEFAULT 0")
        # recipient = 「这份链接发给了谁」，取证时靠它把 token 对应到人
        dcols = {r["name"] for r in conn.execute("PRAGMA table_info(docs)")}
        if "recipient" not in dcols:
            conn.execute("ALTER TABLE docs ADD COLUMN recipient TEXT")


_NON_BROWSER = (
    ("curl", "curl"), ("wget", "wget"), ("httpie", "httpie"),
    ("python-urllib", "python"), ("urllib", "python"),
    ("python-requests", "python"), ("python/", "python"),
    ("requests/", "requests"), ("aiohttp", "aiohttp"),
    ("go-http-client", "go"), ("okhttp", "okhttp"), ("java/", "java"),
    ("libwww", "libwww"), ("node-fetch", "node"), ("axios", "axios"),
)
_CRAWLER = ("bot", "spider", "crawler", "scrapy", "phantomjs")


def _f(obj, *path) -> str:
    """安全取 ua-parser 结果里的字段（逐层 None 保护，返回 str）。

    实测 ua-parser 1.0.2：user_agent / os / device **三个字段会各自独立为 None**
    —— 桌面浏览器的 device 是 None，curl 的 os 和 device 都是 None。
    不逐层保护，日志页就会随机 AttributeError。
    """
    cur = obj
    for k in path:
        if cur is None:
            return ""
        cur = getattr(cur, k, None)
    return "" if cur is None else str(cur)


def _ua_script(ua: str) -> str:
    """非浏览器客户端 → 返回标记文案；正常浏览器 → 返回空串。

    为什么不直接信 ua-parser 的 device.family：实测 curl 的 device 是 None，
    只有 Python-urllib 才被标成 'Spider' —— 单靠它判会漏掉 curl 这类。
    所以先看有没有 'Mozilla' 这个浏览器标志。
    """
    low = ua.lower()
    if "mozilla" in low:
        for k in _CRAWLER:
            if k in low:
                return "⚠ 爬虫"
        return ""
    for k, name in _NON_BROWSER:
        if k in low:
            return "⚠ 脚本/命令行（%s）" % name
    for k in _CRAWLER:
        if k in low:
            return "⚠ 爬虫"
    return ""


def parse_ua(ua: str) -> str:
    """User-Agent 说人话：设备 · 系统 · 客户端。专供取证。

    优先用 ua-parser（准确，细到具体机型），拿不到就退回下面的正则实现，
    所以本文件不依赖它也能跑。
    """
    if not ua:
        return "未知"
    ua = str(ua)[:512]

    # 脚本/爬虫优先判 —— 这才是取证时最想一眼看到的
    mark = _ua_script(ua)
    if mark:
        return mark

    if _ua_parse is not None:
        try:
            r = _ua_parse(ua)
            dev = _f(r, "device", "family")
            osf = _f(r, "os", "family")
            osv = _f(r, "os", "major")
            cli = _f(r, "user_agent", "family")
            clv = _f(r, "user_agent", "major")
            if dev == "Spider":        # 已被上面判过，这里不重复显示
                dev = ""
            out = []
            if dev:
                out.append(dev)
            elif osf in ("Windows", "Mac OS X", "Linux", "Ubuntu", "Debian",
                         "Fedora", "Chrome OS", "ChromeOS"):
                out.append("电脑")      # 桌面没有「设备型号」，标成电脑更实在
            if osf:
                out.append((osf + " " + osv).strip() if osv else osf)
            if cli:
                out.append((cli + " " + clv).strip() if clv else cli)
            if out:
                return " · ".join(out)
            # ua-parser 什么都认不出 = 垃圾/极冷门 UA。
            # 不能再退回正则实现 —— 那里的 dev 默认值就是「电脑」，
            # 于是 \x01\x02\x03 这种会被误报成一台电脑。
            return "未知"
        except Exception:
            pass        # 任何意外都退回正则实现，绝不让日志页炸掉

    return _parse_ua_re(ua)


def _parse_ua_re(ua: str) -> str:
    """正则实现：ua-parser 缺失或抛异常时的退路。"""
    u = ua
    dev, sysv, app = "电脑", "", ""

    if "iPhone" in u:
        dev = "iPhone"
    elif "iPad" in u:
        dev = "iPad"
    elif "Android" in u:
        m = re.search(r"Android[^;]*;\s*([^;)]+)", u)
        dev = (m.group(1).strip() if m else "Android")[:28]
    elif "Windows" in u:
        dev = "Windows"
    elif "Macintosh" in u:
        dev = "Mac"
    elif "Linux" in u:
        dev = "Linux"

    m = re.search(r"(?:iPhone OS|CPU OS|Android)\s*([\d_]+)", u)
    if m:
        sysv = m.group(1).replace("_", ".")

    lu = u.lower()
    if "micromessenger" in lu:
        m = re.search(r"MicroMessenger/([\d\.]+)", u, re.I)
        app = "微信" + ((" " + m.group(1)) if m else "")
    elif "alipayclient" in lu:
        app = "支付宝"
    elif "qqbrowser" in lu or re.search(r"\bQQ/", u):
        app = "QQ"
    elif "edg/" in lu:
        app = "Edge"
    elif "firefox/" in lu:
        app = "Firefox"
    elif "chrome/" in lu:
        app = "Chrome"
    elif "safari/" in lu:
        app = "Safari"

    out = [dev]
    if sysv:
        out.append(sysv)
    if app:
        out.append(app)
    return " · ".join(out)


def geo_lookup(ip: str) -> str:
    """查 IP 归属地（仅 IPv4，免费接口不支持 v6）。失败返回空串。

    隐私：这一步会把该 IP 发给 ip-api.com。之所以做成「由人点击触发 + 永久缓存」
    而不自动批量查，就是为了不把客户 IP 静默送出去。
    """
    try:
        if ipaddress.ip_address(ip).version != 4:
            return ""
    except Exception:
        return ""
    try:
        req = urllib.request.Request(GEO_URL % ip,
                                     headers={"User-Agent": "burnpdf-geo/1.0"})
        with urllib.request.urlopen(req, timeout=GEO_TIMEOUT) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return ""
    if not isinstance(d, dict) or d.get("status") != "success":
        return ""
    bits = [str(d.get("country") or ""), str(d.get("regionName") or ""),
            str(d.get("city") or "")]
    loc = " ".join([b for b in bits if b]).strip()
    isp = str(d.get("isp") or "").strip()
    return (loc + (" · " + isp if isp else "")).strip()[:120]


def housekeeping(conn):
    """生产环境必做的两件事，都幂等（启动时 + 每次发布时各跑一次）：

    1) burn.db 每日备份，保留最近 BACKUP_KEEP_DAYS 份
       db 是唯一真相源（所有链接/次数/日志都在里面），没备份的话误删就全没了。
       用 sqlite 官方的在线备份 API，不需要停服务。
    2) 清理「已过期超过 CLEAN_AFTER_DAYS 天」的文档文件
       只删渲染图和原始 PDF，**保留 db 记录** —— 列表和审计还在，
       只是打不开。既省磁盘，又不丢账。
    """
    conn.commit()                      # 先落盘，保证备份拿到的是完整状态

    try:
        bk = DATA / "backups"
        bk.mkdir(parents=True, exist_ok=True)
        dest = bk / ("burn-" + time.strftime("%Y%m%d") + ".db")
        if not dest.exists():
            with sqlite3.connect(dest) as b:
                conn.backup(b)
            for old in sorted(bk.glob("burn-*.db"))[:-BACKUP_KEEP_DAYS]:
                old.unlink(missing_ok=True)
    except Exception:
        traceback.print_exc()

    try:
        cutoff = now() - CLEAN_AFTER_DAYS * 86400
        rows = conn.execute(
            "SELECT token FROM docs WHERE expire_ts > 0 AND expire_ts < ?",
            (cutoff,)).fetchall()
        for r in rows:
            shutil.rmtree(DOCS / r["token"], ignore_errors=True)
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- 渲染

def build_mark_text(user_text: str, token: str) -> str:
    """生成可溯源的指纹文本：用户标识 · token · 时间。

    为什么必须有 token：拿到泄露出来的图 → 读出 token → 查得出是哪一份链接
    → 查得出发给了谁（logs 表里每次 open 都记了 ip/ua/时间）。
    为什么必须有时间：方便和日志按时间对上，区分「同一份链接的不同阅读」。
    """
    bits = []
    u = (user_text or "").strip()
    if u:
        bits.append(u[:32])
    bits.append(token)
    bits.append(time.strftime("%Y-%m-%d %H:%M", time.localtime()))
    return "  \u00b7  ".join(bits)


def add_watermark(page, text: str):
    """把指纹水印**铺满**整页（画进 PDF，随后一起转成图片，前端删不掉）。

    为什么要铺满：客户很可能只截局部，只有铺满才能保证任何一块都带水印。
    （早先是 3 行×2 列共 6 处，截中间就完全没有水印，溯源等于失效。）

    为什么要 morph：insert_text 的 rotate 只接受 90 的倍数，
    任意角度的斜向水印必须用 morph=(锚点, Matrix(角度))。
    """
    if not text:
        return
    rect = page.rect
    w, h = rect.width, rect.height
    fs = max(7.0, min(11.0, min(w, h) / 62))      # A4 约 9.6pt
    step_x, step_y = 250.0, 125.0
    row = 0
    y = fs * 2.5
    while y < h + step_y:
        x = -70.0 + (step_x / 2 if row % 2 else 0)   # 隔行错开，不留规律空白
        while x < w + 70:
            pt = pymupdf.Point(x, y)
            try:
                page.insert_text(pt, text, fontname=FONT_CJK, fontsize=fs,
                                 color=(0.45, 0.45, 0.48), fill_opacity=0.16,
                                 morph=(pt, pymupdf.Matrix(-30)))
            except Exception:
                try:   # 字体不可用时退回内置字体（中文会缺字，但绝不崩）
                    page.insert_text(pt, text, fontsize=fs * 0.9,
                                     color=(0.45, 0.45, 0.48), fill_opacity=0.16,
                                     morph=(pt, pymupdf.Matrix(-30)))
                except Exception:
                    pass
            x += step_x
        y += step_y
        row += 1


def render_pdf(token: str, watermark: str, strips: int = 1,
               quality: int = 88, scale: float = 2.0, fmt: str = "jpeg") -> int:
    """把 source.pdf 渲染成图片存到 data/docs/<token>/，返回页数。"""
    src = DOCS / token / "source.pdf"
    out_dir = DOCS / token
    doc = pymupdf.open(src)
    n_pages = doc.page_count
    mat = pymupdf.Matrix(scale, scale)
    for i in range(n_pages):
        page = doc[i]
        # 指纹水印**始终**画：它是「泄露后能追到哪一份链接」的唯一手段，
        # 不能因为发布者没填水印文字就跳过（那样就没法溯源了）。
        add_watermark(page, build_mark_text(watermark, token))
        if strips <= 1:
            pix = page.get_pixmap(matrix=mat)
            (out_dir / f"{i}.jpg").write_bytes(
                pix.tobytes(output=fmt, jpg_quality=quality))
        else:
            r = page.rect
            h = r.height / strips
            for k in range(strips):
                clip = pymupdf.Rect(r.x0, r.y0 + k * h, r.x1, r.y0 + (k + 1) * h)
                pix = page.get_pixmap(matrix=mat, clip=clip)
                (out_dir / f"{i}-{k}.jpg").write_bytes(
                    pix.tobytes(output=fmt, jpg_quality=quality))
    doc.close()
    return n_pages


# ---------------------------------------------------------------- 业务

def refund_idle(conn, token: str):
    """扣了次数但一页都没看到 → 退还次数（避免网络抖动白扣）。"""
    cutoff = now() - IDLE_REFUND_SEC
    rows = conn.execute(
        "SELECT sid FROM sessions WHERE token=? AND state='active' "
        "AND pages_served=0 AND start_ts<?", (token, cutoff)).fetchall()
    for r in rows:
        conn.execute("UPDATE sessions SET state='refunded' WHERE sid=?", (r["sid"],))
        conn.execute("UPDATE docs SET used_views=max(0,used_views-1) WHERE token=?",
                     (token,))
        log(conn, token, r["sid"], "auto-refund",
            f"{IDLE_REFUND_SEC} 秒内未加载到任何页面，已退还 1 次")


def doc_state(row) -> dict:
    """给前端用的状态（不含敏感信息）。"""
    if row is None:
        return {"ok": False, "status": "notfound", "msg": "链接不存在"}
    if row["revoked"]:
        return {"ok": False, "status": "revoked", "msg": "该链接已被发布者作废"}
    if row["expire_ts"] and now() > row["expire_ts"]:
        return {"ok": False, "status": "expired", "msg": "该链接已过期"}
    if row["used_views"] >= row["limit_views"]:
        return {"ok": False, "status": "used_up",
                "msg": f"该文件仅可查看 {row['limit_views']} 次，已查看完"}
    return {
        "ok": True,
        "status": "ready",
        "name": row["name"],
        "pages": row["pages"],
        "limit": row["limit_views"],
        "used": row["used_views"],
        "remaining": row["limit_views"] - row["used_views"],
        "duration": row["duration_sec"],
        "expire_ts": row["expire_ts"],
        "strips": row["strips"],
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "BurnPDF/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 小工具 ----------
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8",
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_UPLOAD:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---------- 鉴权：全站唯一的管理密钥校验点 ----------
    def _is_admin(self):
        key = self.headers.get("X-Admin-Key") or ""
        # compare_digest = 常数时间比较，防时序侧信道
        return key and secrets.compare_digest(key, admin_key())

    def _need_admin(self):
        """**唯一**校验点。所有管理动作都必须过这里。

        别在别处再比一次密钥 —— 同一个能力有几个入口，安全参数就要在几个入口上接。
        失败会累计，到上限临时封禁来源 IP（防脚本灌）。
        """
        ip = self._ip()
        wait, _left = fail_check(ip)
        if wait > 0:
            return self._json({"ok": False,
                               "msg": "尝试次数过多，请约 %d 分钟后再试"
                                      % (int(wait / 60) + 1)}, 429)
        if self._is_admin():
            _FAILS.pop(ip, None)          # 成功即清零
            return False
        n, blocked = fail_hit(ip)
        with db() as conn:
            log(conn, "", "", "admin_fail",
                "管理密钥校验失败（第 %d 次%s）peer=%s xff=%s" % (
                    n, "，已临时封禁该来源" if blocked else "",
                    self.client_address[0] if self.client_address else "-",
                    (self.headers.get("X-Forwarded-For") or "-")[:120]),
                ip, self._ua())
        self._json({"ok": False,
                    "msg": "连续错误已达上限，已临时封禁，请稍后再试" if blocked
                           else "管理密钥不正确"}, 401)
        return True

    def _ip(self):
        """客户端 IP（日志展示 + 限速依据）。

        ⚠️ X-Forwarded-For **可以伪造**。实测（2026-09-30）确认本机 nginx 用的是
        `$proxy_add_x_forwarded_for`，它是**追加式**的：
            客户端发的：  X-Forwarded-For: 1.2.3.4           <- 攻击者随便编
            nginx 转发：  X-Forwarded-For: 1.2.3.4, <真实IP>
        所以**真实 IP 是最后一段**。取第一段 = 把限速和审计交给攻击者决定
        （每个请求换一个伪造值，就等于每次都是「新客户端」，限速形同虚设）。

        只有在「TCP 对端确实是本机/内网反代」时才采信 XFF —— 对端地址来自
        TCP 连接本身，客户端改不了。

        如果将来前面再套一层 CDN，真实 IP 会再往左挪一位，这里要跟着改。
        """
        peer = (self.client_address[0] if self.client_address else "") or ""
        if _peer_is_local(peer):
            xff = self.headers.get("X-Forwarded-For") or ""
            if xff:
                return xff.split(",")[-1].strip()[:64]
        return peer

    def _ua(self):
        return self.headers.get("User-Agent") or ""
    def _fetch_ok(self) -> bool:
        """挡住「地址栏直接打开图片」和「别的网站引用图片」。

        依据 Sec-Fetch-* —— 这是浏览器自己加的，而且属于 forbidden header name，
        页面里的 JS 改不了、也伪造不了：
            Sec-Fetch-Dest: image       <- <img src=...> 正常加载          → 放行
            Sec-Fetch-Dest: document    <- 地址栏 / 新标签页直接打开图片 → 拒绝
            Sec-Fetch-Site: cross-site  <- 第三方站点引用                  → 拒绝

        老浏览器、无头工具（curl / requests / 下载器）不发这些头 → 一律放行。
        这是**故意 fail-open**：宁可放过脚本，也绝不能误伤真实客户
        （微信老内核、Safari < 16.4 都不发这个头）。
        所以它能拦掉「复制链接到地址栏」这类普通操作，但拦不住会看
        网络面板的人 —— 那种人本来也能直接截屏。
        """
        dest = (self.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if dest in ("document", "iframe", "frame", "embed", "object"):
            return False
        if site == "cross-site":
            return False
        return True
    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            self._route_get()
        except Exception:
            traceback.print_exc()
            self._json({"ok": False, "msg": "服务内部错误"}, 500)

    def do_POST(self):
        try:
            self._route_post()
        except Exception:
            traceback.print_exc()
            self._json({"ok": False, "msg": "服务内部错误"}, 500)

    # 很多监控/健康检查用 HEAD 探活。不实现的话 BaseHTTPRequestHandler 回 501，
    # 会被误判成「服务挂了」。复用 GET 的路由即可 —— _send() 里已经会跳过 body。
    do_HEAD = do_GET

    def _route_get(self):
        # 先挡超长 URL：别让 parse_qs / 正则去啃几百 KB 的脏串
        if len(self.path) > 1024:
            return self._json({"ok": False, "msg": "请求过长"}, 414)
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p in ("/", "/admin"):
            return self._file(ROOT / "admin.html", "text/html; charset=utf-8")
        if p == "/viewer.html":
            return self._file(ROOT / "viewer.html", "text/html; charset=utf-8")

        # token：字符白名单 + 长度上限（真实 token 只有 12 字符）
        m = re.fullmatch(r"/v/([A-Za-z0-9_\-]{1,64})", p)
        if m:
            return self._viewer(m.group(1))

        m = re.fullmatch(r"/v/([A-Za-z0-9_\-]{1,64})/p/(\d{1,6})", p)
        if m:
            return self._page(m.group(1), safe_int(m.group(2)),
                              (q.get("s") or [""])[0][:64],
                              safe_int((q.get("k") or ["0"])[0]))

        if p == "/api/info":
            return self._api_info((q.get("token") or [""])[0])

        if p == "/api/list":
            if self._need_admin():
                return
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM docs ORDER BY created DESC").fetchall()
                return self._json({"ok": True, "items": [dict(r) | {
                    "status": doc_state(r)["status"]} for r in rows]})

        if p == "/api/logs":
            if self._need_admin():
                return
            token = (q.get("token") or [""])[0]
            with db() as conn:
                if token:
                    rows = conn.execute(
                        "SELECT * FROM logs WHERE token=? ORDER BY ts DESC LIMIT 300",
                        (token,)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM logs ORDER BY ts DESC LIMIT 300").fetchall()
                return self._json({"ok": True, "items": [
                    dict(r) | {"dev": parse_ua(r["ua"])} for r in rows]})

        if p == "/api/iploc":
            if self._need_admin():
                return
            return self._api_iploc(q)

        return self._json({"ok": False, "msg": "not found"}, 404)

    def _route_post(self):
        u = urlparse(self.path)
        p = u.path

        if p == "/api/publish":
            if self._need_admin():
                return
            return self._api_publish()

        if p == "/api/revoke":
            if self._need_admin():
                return
            return self._api_revoke()

        if p == "/api/prepare":
            return self._api_prepare()

        if p == "/api/close":
            return self._api_close()

        return self._json({"ok": False, "msg": "not found"}, 404)

    # ---------- 静态 / 页面 ----------
    def _file(self, path: Path, ctype: str):
        if not path.exists():
            return self._json({"ok": False, "msg": f"缺少文件 {path.name}"}, 500)
        self._send(200, path.read_bytes(), ctype)

    def _viewer(self, token: str):
        with db() as conn:
            refund_idle(conn, token)
        if not (ROOT / "viewer.html").exists():
            return self._json({"ok": False, "msg": "缺少 viewer.html"}, 500)
        html = (ROOT / "viewer.html").read_text(encoding="utf-8")
        html = html.replace("__TOKEN__", token)
        # 只加 frame-ancestors（不动 default-src，避免误伤页内内联样式/脚本）：
        # 防止别人把这个阅读页嵌进他自己的网站。
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Security-Policy": "frame-ancestors 'none'",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "Referrer-Policy": "no-referrer",
        })

    def _page(self, token: str, idx: int, sid: str, k: int = 0):
        """取某一页的图片（被切成多条时用 k 定位第几条）。

        ⚠️ 这里**只能**校验会话，不能拿「剩余次数」当门槛：
        次数在 /api/prepare 时就已经扣掉了，session 才是本次阅读的授权凭证。
        （早期版本在这里复用了 doc_state()，结果「限 1 次」的文档一扣完次就自己
         再也取不到图 —— 这种 bug 接口自测看不出来，必须真在浏览器里跑一遍。）

        也不在这里做「超时退次数」：那个只在 prepare 时做，否则客户点开页面后
        停一会儿没滑动，会话会被误判成「没看到内容」而被作废。
        """
        with db() as conn:
            # 直连图片 / 第三方引用 → 拦掉，并记一条日志供事后查是谁在扒
            if not self._fetch_ok():
                log(conn, token, sid, "blocked",
                    "图片直连被拦 dest=%s site=%s mode=%s referer=%s" % (
                        self.headers.get("Sec-Fetch-Dest"),
                        self.headers.get("Sec-Fetch-Site"),
                        self.headers.get("Sec-Fetch-Mode"),
                        (self.headers.get("Referer") or "-")[:120]),
                    self._ip(), self._ua())
                return self._json({"ok": False,
                                   "msg": "请从阅读页面查看，不支持直接打开"}, 403)
            row = conn.execute("SELECT * FROM docs WHERE token=?", (token,)).fetchone()
            if row is None:
                return self._json({"ok": False, "msg": "链接不存在"}, 403)
            if row["revoked"]:
                return self._json({"ok": False, "msg": "该链接已被发布者作废"}, 403)
            if row["expire_ts"] and now() > row["expire_ts"]:
                return self._json({"ok": False, "msg": "该链接已过期"}, 403)
            s = conn.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
            if not s or s["token"] != token:
                return self._json({"ok": False, "msg": "会话无效，请重新打开链接"}, 403)
            if s["state"] != "active":
                return self._json({"ok": False, "msg": "本次阅读已结束"}, 403)
            if s["start_ts"] + row["duration_sec"] < now():
                conn.execute("UPDATE sessions SET state='timeout' WHERE sid=?", (sid,))
                return self._json({"ok": False, "msg": "本次阅读时长已用完"}, 403)
            if idx < 0 or idx >= row["pages"]:
                return self._json({"ok": False, "msg": "页码越界"}, 404)

            # ★ 异常取图检测（只记审计，不阻断）。
            #   正常人看图靠滚动懒加载，块与块之间必然有时间间隔；
            #   脚本会在极短时间内把整份文档的图块全抓走。
            #   只记录不判罚 —— 零误伤风险，但事后能查出「谁在什么时候
            #   秒抓了哪份文档」，配合水印就能追到人。
            taken = (s["pages_served"] or 0) + 1
            total = (row["pages"] or 0) * max(1, row["strips"] or 1)
            elapsed = now() - (s["start_ts"] or now())
            if (total >= SCRAPE_MIN_BLOCKS and taken >= total
                    and elapsed < SCRAPE_SEC and not s["scrape_flagged"]):
                conn.execute("UPDATE sessions SET scrape_flagged=1 WHERE sid=?", (sid,))
                log(conn, token, sid, "scrape_suspect",
                    "%.2f 秒内取走全部 %d 个图块（正常阅读做不到，疑似脚本批量抓图）"
                    % (elapsed, taken), self._ip(), self._ua())

            conn.execute(
                "UPDATE sessions SET pages_served=pages_served+1, last_ts=? WHERE sid=?",
                (now(), sid))

        strips = row["strips"]
        if strips <= 1:
            fp = DOCS / token / f"{idx}.jpg"
        else:
            k = max(0, min(strips - 1, k))
            fp = DOCS / token / f"{idx}-{k}.jpg"
        if not fp.exists():
            return self._json({"ok": False, "msg": "该页尚未渲染"}, 404)
        self._send(200, fp.read_bytes(), "image/jpeg",
                   {"Content-Disposition": "inline",
                    "X-Robots-Tag": "noindex, nofollow, noarchive"})

    # ---------- 业务接口 ----------
    def _api_iploc(self, q):
        """IP → 归属地，带永久缓存（同一 IP 只查一次接口）。

        缓存的额外好处：「查过哪些 IP」在库里留痕，属可审计行为。
        """
        ip = (q.get("ip") or [""])[0][:64].strip()
        try:
            ok = ipaddress.ip_address(ip).version == 4
        except Exception:
            ok = False
        if not ok:
            return self._json({"ok": False, "msg": "无效 IP（仅支持 IPv4）"}, 400)
        with db() as conn:
            row = conn.execute("SELECT loc FROM iploc WHERE ip=?", (ip,)).fetchone()
            if row and row["loc"]:
                return self._json({"ok": True, "ip": ip, "loc": row["loc"],
                                   "cached": True})
        loc = geo_lookup(ip)
        if not loc:
            return self._json({"ok": False,
                               "msg": "查询失败（接口无响应 / 该 IP 无数据）"}, 502)
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO iploc(ip,loc,ts) VALUES(?,?,?)",
                         (ip, loc, now()))
            log(conn, "", "", "geo_lookup", loc, self._ip(), self._ua())
        return self._json({"ok": True, "ip": ip, "loc": loc, "cached": False})

    def _api_info(self, token: str):
        with db() as conn:
            refund_idle(conn, token)
            row = conn.execute("SELECT * FROM docs WHERE token=?", (token,)).fetchone()
            self._json(doc_state(row))

    def _api_prepare(self):
        """客户点「开始阅读」→ 这里扣次数、建会话。刷新页面时会复用旧会话，不重复扣次。"""
        body = self._body_json()
        token = str(body.get("token") or "").strip()[:64]
        sid_in = str(body.get("sid") or "").strip()[:64]
        with db() as conn:
            refund_idle(conn, token)
            row = conn.execute("SELECT * FROM docs WHERE token=?", (token,)).fetchone()
            if row is None:
                return self._json({"ok": False, "status": "notfound",
                                   "msg": "链接不存在"}, 403)
            if row["revoked"]:
                return self._json({"ok": False, "status": "revoked",
                                   "msg": "该链接已被发布者作废"}, 403)
            if row["expire_ts"] and now() > row["expire_ts"]:
                return self._json({"ok": False, "status": "expired",
                                   "msg": "该链接已过期"}, 403)

            # ★ 顺序很重要：**先看能不能复用已有会话，再判「还有没有次数」**。
            #   否则「限 1 次」的文档在客户持有有效会话时（切后台回来 / 刷新页面）
            #   会直接被判成「已查看完」，明明他还在本次阅读时限内。
            if sid_in:
                s = conn.execute("SELECT * FROM sessions WHERE sid=?", (sid_in,)).fetchone()
                if (s and s["token"] == token and s["state"] == "active"
                        and s["start_ts"] + row["duration_sec"] > now()):
                    return self._json({
                        "ok": True, "sid": s["sid"], "reused": True,
                        "name": row["name"], "pages": row["pages"],
                        "strips": row["strips"],
                        "duration": row["duration_sec"],
                        "limit": row["limit_views"],
                        "limit_left": max(0, row["limit_views"] - row["used_views"]),
                        "left": row["duration_sec"] - (now() - s["start_ts"]),
                    })

            if row["used_views"] >= row["limit_views"]:
                return self._json({"ok": False, "status": "used_up",
                                   "msg": f"该文件仅可查看 {row['limit_views']} 次，已查看完"}, 403)

            sid = secrets.token_urlsafe(12)
            conn.execute(
                "INSERT INTO sessions(sid,token,ip,ua,start_ts,last_ts,state,pages_served)"
                " VALUES(?,?,?,?,?,?,'active',0)",
                (sid, token, self._ip(), self._ua(), now(), now()))
            conn.execute("UPDATE docs SET used_views=used_views+1 WHERE token=?", (token,))
            log(conn, token, sid, "open",
                f"第 {row['used_views'] + 1}/{row['limit_views']} 次打开",
                self._ip(), self._ua())
            self._json({"ok": True, "sid": sid, "reused": False,
                        "name": row["name"], "pages": row["pages"],
                        "strips": row["strips"],
                        "duration": row["duration_sec"],
                        "limit": row["limit_views"],
                        "limit_left": max(0, row["limit_views"] - row["used_views"] - 1)})

    def _api_close(self):
        body = self._body_json()
        sid = str(body.get("sid") or "").strip()[:64]
        with db() as conn:
            s = conn.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
            if s:
                conn.execute("UPDATE sessions SET state='closed', last_ts=? WHERE sid=?",
                             (now(), sid))
                seen = round(s["start_ts"] and (now() - s["start_ts"]) or 0, 1)
                log(conn, s["token"], sid, "close",
                    f"本次阅读 {seen} 秒，共取图 {s['pages_served']} 次")
            self._json({"ok": True})

    def _api_publish(self):
        body = self._body_json()
        raw = body.get("pdf_b64") or ""
        if not raw:
            return self._json({"ok": False, "msg": "没有收到 PDF 内容"}, 400)
        try:
            data = base64.b64decode(raw.split(",")[-1])
        except Exception:
            return self._json({"ok": False, "msg": "PDF base64 解析失败"}, 400)
        if len(data) < 100:
            return self._json({"ok": False, "msg": "PDF 内容为空"}, 400)

        token = secrets.token_urlsafe(9)
        out_dir = DOCS / token
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "source.pdf").write_bytes(data)

        limit = max(1, min(999, safe_int(body.get("limit"), DEFAULT_LIMIT)))
        duration = max(30, min(86400, safe_int(body.get("duration"), DEFAULT_DURATION)))
        if body.get("expire_ts"):
            expire_ts = safe_float(body["expire_ts"], 0.0)
        else:
            days = max(0.0, min(3650.0,
                                safe_float(body.get("expire_days"), DEFAULT_EXPIRE_DAYS)))
            expire_ts = now() + days * 86400 if days > 0 else 0
        watermark = str(body.get("watermark") or "").strip()[:80]
        # 收件人：只存台账，不进水印（泄露者看不到自己被记名）
        recipient = str(body.get("recipient") or "").strip()[:60]
        strips = max(1, min(12, safe_int(body.get("strips"), DEFAULT_STRIPS)))
        quality = max(40, min(95, safe_int(body.get("quality"), DEFAULT_QUALITY)))
        # scale 决定渲染倍率——不夹紧的话 scale=1e9 能把内存直接打爆
        scale = max(0.5, min(4.0, safe_float(body.get("scale"), DEFAULT_SCALE)))
        name = str(body.get("name") or "未命名文档").strip()[:120] or "未命名文档"

        try:
            pages = render_pdf(token, watermark, strips, quality, scale, "jpeg")
        except Exception as e:
            return self._json({"ok": False, "msg": f"PDF 渲染失败：{e}"}, 400)

        with db() as conn:
            conn.execute(
                "INSERT INTO docs(token,name,pages,created,limit_views,used_views,"
                "expire_ts,duration_sec,watermark,note,recipient,revoked,strips,quality,scale,fmt)"
                " VALUES(?,?,?,?,?,0,?,?,?,?,?,0,?,?,?,'jpeg')",
                (token, name, pages, now(), limit, expire_ts, duration,
                 watermark, str(body.get("note") or "")[:500], recipient,
                 strips, quality, scale))
            log(conn, token, "", "publish",
                f"发布 {name}（{pages} 页，限 {limit} 次，单次 {duration} 秒）",
                self._ip(), self._ua())
            housekeeping(conn)      # 顺手做：db 备份 + 清过期文件

        self._json({"ok": True, "token": token, "pages": pages,
                    "url": f"/v/{token}"})

    def _api_revoke(self):
        body = self._body_json()
        token = str(body.get("token") or "").strip()[:64]
        on = 0 if body.get("restore") else 1
        with db() as conn:
            conn.execute("UPDATE docs SET revoked=? WHERE token=?", (on, token))
            log(conn, token, "", "revoke" if on else "restore",
                "作废链接" if on else "恢复链接",
                self._ip(), self._ua())
        self._json({"ok": True})


def main():
    init_db()
    with db() as c:                 # 启动时也做一次：长期不发布也能备份
        try:
            housekeeping(c)
        except Exception:
            traceback.print_exc()
    # 只在「本次才生成」的时候打印一次密钥。
    # 每次启动都打印的话，`docker logs` 里会永久留存明文管理密钥 ——
    # 任何能执行 `docker logs` 的人（或拿到 docker 权限的进程）就等于拿到全部权限。
    fresh = not ADMIN_KEY_FILE.exists()      # 必须在 admin_key() 之前判断
    key = admin_key()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("=" * 64)
    print("  阅后即焚 PDF 服务已启动")
    print("=" * 64)
    print(f"  管理台   http://127.0.0.1:{PORT}/admin")
    if fresh:
        print("  " + "-" * 56)
        print("   首次生成管理密钥，请立即保存（以后不再打印）：")
        print(f"     {key}")
        print("  " + "-" * 56)
    else:
        print("  管理密钥 已存在（为安全起见不在此处打印）")
    print(f"  数据目录 {DATA}")
    print("  停止服务 Ctrl+C")
    print("=" * 64)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
