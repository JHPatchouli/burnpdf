#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 JHPatchouli
# SPDX-License-Identifier: AGPL-3.0-or-later
"""View-limited PDF links.

The original PDF stays on the server. Viewers receive watermarked page images.
A view is counted when reading starts. Limits cover total views, one session's
duration, and an expiry time. A counted view with no loaded page is refunded
after IDLE_REFUND_SEC.

Requires pymupdf. Run: python server.py
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
except ImportError:  # older package name
    import fitz as pymupdf  # type: ignore

# ua-parser is optional. Install it under ./vendor if you want device-level
# user-agent parsing. parse_ua() falls back to a small regex parser without it.
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

DEFAULT_LIMIT = 1          # views allowed per link
DEFAULT_DURATION = 600     # seconds allowed in one reading session
DEFAULT_EXPIRE_DAYS = 3    # link lifetime; 0 means no date expiry
DEFAULT_SCALE = 2.0        # render scale, about 144 dpi
DEFAULT_QUALITY = 88       # JPEG quality
DEFAULT_STRIPS = 1         # horizontal strips per page
IDLE_REFUND_SEC = 60       # refund a view when no page loads within this time
SCRAPE_SEC = 2.0           # full fetch faster than this is logged
SCRAPE_MIN_BLOCKS = 8      # ignore scrape checks for smaller documents
BACKUP_KEEP_DAYS = 7       # daily database backups to keep
CLEAN_AFTER_DAYS = 30      # delete files this long after expiry; keep the row
MAX_UPLOAD = 80 * 1024 * 1024

# IP geolocation runs only when an admin asks for one address, then caches it.
# ip-api.com is free, needs no account, and returns Chinese place names.
GEO_URL = ("http://ip-api.com/json/%s"
           "?lang=zh-CN&fields=status,country,regionName,city,isp,query")
GEO_TIMEOUT = 6.0

FONT_CJK = "china-s"       # built-in simplified Chinese font

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


# Admin-key failures are rate limited in process memory. A restart clears the count.
_FAILS: dict = {}
FAIL_WINDOW = 300.0     # failure counting window, seconds
FAIL_MAX = 8            # failures allowed inside that window
BLOCK_SEC = 900.0       # block duration after the limit is reached


def _fails_gc():
    """Drop expired entries so the failure map cannot grow without bound."""
    if len(_FAILS) < 1000:
        return
    t = now()
    for k in [k for k, v in _FAILS.items()
              if v["until"] < t and t - v["t0"] > FAIL_WINDOW]:
        _FAILS.pop(k, None)


def fail_check(ip):
    """Return seconds still blocked and remaining attempts."""
    s = _FAILS.get(ip)
    t = now()
    if not s:
        return 0.0, FAIL_MAX
    if s["until"] > t:
        return s["until"] - t, 0
    if t - s["t0"] > FAIL_WINDOW:   # window expired; start again
        _FAILS.pop(ip, None)
        return 0.0, FAIL_MAX
    return 0.0, max(0, FAIL_MAX - s["n"])


def fail_hit(ip):
    """Record one failure. Return the count and whether a block just started."""
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
    """Parse an integer from request input. Never raises; caps the digit length."""
    try:
        return int(str(v)[:12])
    except Exception:
        return default


def safe_float(v, default=0.0) -> float:
    """Parse a finite float from request input. Never raises."""
    try:
        f = float(str(v)[:24])
        return default if (f != f or f in (float("inf"), float("-inf"))) else f
    except Exception:
        return default


def _peer_is_local(peer: str) -> bool:
    """True when the TCP peer is loopback or private, such as a local reverse proxy."""
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
        # CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "scrape_flagged" not in cols:
            conn.execute("ALTER TABLE sessions "
                         "ADD COLUMN scrape_flagged INTEGER DEFAULT 0")
        # recipient records who a link was sent to. It is not drawn into the image.
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
    """Read a nested ua-parser field. Any level may be None."""
    cur = obj
    for k in path:
        if cur is None:
            return ""
        cur = getattr(cur, k, None)
    return "" if cur is None else str(cur)


def _ua_script(ua: str) -> str:
    """Return a label for non-browser clients, or an empty string for browsers.

    device.family alone misses clients such as curl, so the Mozilla token is
    checked first.
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
    """Turn a User-Agent into "device, OS, client" for the admin log."""
    if not ua:
        return "未知"
    ua = str(ua)[:512]

    # Non-browser clients are labelled before device parsing.
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
            if dev == "Spider":
                dev = ""
            out = []
            if dev:
                out.append(dev)
            elif osf in ("Windows", "Mac OS X", "Linux", "Ubuntu", "Debian",
                         "Fedora", "Chrome OS", "ChromeOS"):
                out.append("电脑")      # desktop browsers have no device model
            if osf:
                out.append((osf + " " + osv).strip() if osv else osf)
            if cli:
                out.append((cli + " " + clv).strip() if clv else cli)
            if out:
                return " · ".join(out)
            # Unknown input stays unknown. The regex fallback defaults to a desktop.
            return "未知"
        except Exception:
            pass        # fall back to the regex parser

    return _parse_ua_re(ua)


def _parse_ua_re(ua: str) -> str:
    """Regex fallback used when ua-parser is missing or fails."""
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
    """Look up an IPv4 address. Sends that address to ip-api.com."""
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
    """Back up the database once a day and delete files for long-expired documents.

    Database rows are kept so the admin list and audit log still resolve.
    """
    conn.commit()                      # flush before the online backup

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
    """Watermark text: optional label, link token, and local time."""
    bits = []
    u = (user_text or "").strip()
    if u:
        bits.append(u[:32])
    bits.append(token)
    bits.append(time.strftime("%Y-%m-%d %H:%M", time.localtime()))
    return "  \u00b7  ".join(bits)


def add_watermark(page, text: str):
    """Draw a diagonal watermark across the whole page before rasterizing it.

    insert_text only rotates by multiples of 90 degrees, so the angle uses morph.
    """
    if not text:
        return
    rect = page.rect
    w, h = rect.width, rect.height
    fs = max(7.0, min(11.0, min(w, h) / 62))      # about 9.6 pt on A4
    step_x, step_y = 250.0, 125.0
    row = 0
    y = fs * 2.5
    while y < h + step_y:
        x = -70.0 + (step_x / 2 if row % 2 else 0)   # stagger alternate rows
        while x < w + 70:
            pt = pymupdf.Point(x, y)
            try:
                page.insert_text(pt, text, fontname=FONT_CJK, fontsize=fs,
                                 color=(0.45, 0.45, 0.48), fill_opacity=0.16,
                                 morph=(pt, pymupdf.Matrix(-30)))
            except Exception:
                try:   # built-in font if the CJK font is unavailable
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
    """Render source.pdf into data/docs/<token>/ and return the page count."""
    src = DOCS / token / "source.pdf"
    out_dir = DOCS / token
    doc = pymupdf.open(src)
    n_pages = doc.page_count
    mat = pymupdf.Matrix(scale, scale)
    for i in range(n_pages):
        page = doc[i]
        # The token is always included, even when the publisher leaves the label blank.
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
    """Refund a counted view when the session loaded no page."""
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
    """Public link status. It does not include admin fields."""
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

    # ---------- admin key ----------
    def _is_admin(self):
        key = self.headers.get("X-Admin-Key") or ""
        # constant-time comparison
        return key and secrets.compare_digest(key, admin_key())

    def _need_admin(self):
        """The only admin-key check. Failed attempts are counted and then blocked."""
        ip = self._ip()
        wait, _left = fail_check(ip)
        if wait > 0:
            return self._json({"ok": False,
                               "msg": "尝试次数过多，请约 %d 分钟后再试"
                                      % (int(wait / 60) + 1)}, 429)
        if self._is_admin():
            _FAILS.pop(ip, None)          # a valid key clears the counter
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
        """Client address used for logs and rate limits.

        X-Forwarded-For is trusted only when the TCP peer is a local proxy,
        and only its last address is used. A proxy that appends another hop,
        such as a CDN, needs this adjusted.
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
        """Reject direct navigation and cross-site embeds when Sec-Fetch is present.

        Requests without those headers are allowed, because older browsers omit them.
        """
        dest = (self.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if dest in ("document", "iframe", "frame", "embed", "object"):
            return False
        if site == "cross-site":
            return False
        return True
    def log_message(self, fmt, *args):  # access log is stored in the database
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

    # HEAD uses the GET routes. _send() omits the body for it.
    do_HEAD = do_GET

    def _route_get(self):
        # Reject an overlong path before parsing it.
        if len(self.path) > 1024:
            return self._json({"ok": False, "msg": "请求过长"}, 414)
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p in ("/", "/admin"):
            return self._file(ROOT / "admin.html", "text/html; charset=utf-8")
        if p == "/viewer.html":
            return self._file(ROOT / "viewer.html", "text/html; charset=utf-8")

        # Tokens are short URL-safe strings. The pattern also caps length.
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
        # Prevent other sites from framing the viewer. Leave other CSP directives unset.
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Security-Policy": "frame-ancestors 'none'",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "Referrer-Policy": "no-referrer",
        })

    def _page(self, token: str, idx: int, sid: str, k: int = 0):
        """Return one rendered page strip.

        Authorization is the session, not the remaining view count. The count is
        consumed when the session is created. Idle refunds also happen there.
        """
        with db() as conn:
            # Direct and cross-site image loads are rejected and logged.
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

            # A full document fetched within SCRAPE_SEC is logged, not blocked.
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

            # Reuse a live session before checking the remaining view count.
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
        # Stored for the admin only. It is not included in the watermark.
        recipient = str(body.get("recipient") or "").strip()[:60]
        strips = max(1, min(12, safe_int(body.get("strips"), DEFAULT_STRIPS)))
        quality = max(40, min(95, safe_int(body.get("quality"), DEFAULT_QUALITY)))
        # Keep the render scale inside a small range.
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
            housekeeping(conn)

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
    with db() as c:                 # also runs at startup, not only on publish
        try:
            housekeeping(c)
        except Exception:
            traceback.print_exc()
    # Print the admin key only when it is created. Later restarts stay quiet.
    fresh = not ADMIN_KEY_FILE.exists()
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
