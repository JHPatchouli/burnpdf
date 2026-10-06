#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression checks for a running server.

    python selftest.py

The checks publish their own documents. One check confirms that a session can
still fetch images after its view has been counted.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8770"
DB = HERE / "data" / "burn.db"
PDF = HERE / "samples" / "sample-quote.pdf"

PASS, FAIL = [], []


def call(path, body=None, *, key=False, method=None):
    hdr = {"Content-Type": "application/json"}
    if key:
        hdr["X-Admin-Key"] = (HERE / "data" / "admin.key").read_text(encoding="utf-8").strip()
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=hdr, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, ""


def call_json(path, body=None, *, key=False):
    hdr = {"Content-Type": "application/json"}
    if key:
        hdr["X-Admin-Key"] = (HERE / "data" / "admin.key").read_text(encoding="utf-8").strip()
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=hdr, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body_txt)
        except Exception:
            return e.code, {"raw": body_txt}


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  -> {extra}" if extra and not cond else ""))


def ensure_pdf():
    """容器里可能没有 samples/，缺了就现场生成一份。"""
    if PDF.exists():
        return
    import make_sample
    make_sample.main()


def publish(limit=1, duration=600, days=3, strips=1, watermark="自测"):
    body = {
        "name": "__selftest__",
        "pdf_b64": base64.b64encode(PDF.read_bytes()).decode("ascii"),
        "limit": limit, "duration": duration, "expire_days": days,
        "strips": strips, "watermark": watermark,
    }
    st, res = call_json("/api/publish", body, key=True)
    assert res.get("ok"), f"发布失败: {res}"
    return res["token"], res["pages"]


def main() -> int:
    print(f"目标服务 {BASE}")
    st, res = call_json("/api/info?token=nope")
    if st != 200:
        print("连不上服务，先把 server.py 跑起来")
        return 2
    ensure_pdf()
    print("\n[1] 发布与准备")
    token, pages = publish(limit=1, strips=4)
    st, info = call_json(f"/api/info?token={token}")
    check("新文档状态为 ready，剩余 1 次",
          info.get("status") == "ready" and info.get("remaining") == 1, info)
    check("页数与源文件一致", info.get("pages") == pages, info)

    st, prep = call_json("/api/prepare", {"token": token})
    check("首次 prepare 成功并扣次", prep.get("ok") and prep.get("limit_left") == 0, prep)
    sid = prep["sid"]

    st, info2 = call_json(f"/api/info?token={token}")
    check("扣次后文档状态变为 used_up", info2.get("status") == "used_up", info2)

    print("\n[2] ★ 扣完次数后本次会话仍必须能取图（回归重点）")
    st, ct = call(f"/v/{token}/p/0?s={sid}&k=0")
    check("取第 1 页返回 200", st == 200, f"实际 {st}")
    check("返回的是图片", ct.startswith("image/"), ct)
    st, _ = call(f"/v/{token}/p/2?s={sid}&k=3")
    check("取最后一页最后一条也返回 200", st == 200, f"实际 {st}")

    print("\n[3] 授权边界")
    st, _ = call(f"/v/{token}/p/0?s=fake-sid-xxxx")
    check("伪造 session 被拒 (403)", st == 403, f"实际 {st}")
    st, _ = call(f"/v/{token}/p/99?s={sid}")
    check("越界页码被拒 (404)", st == 404, f"实际 {st}")

    st, again = call_json("/api/prepare", {"token": token, "sid": sid})
    check("同一会话刷新不重复扣次（reused）", again.get("ok") and again.get("reused") is True, again)
    conn = sqlite3.connect(DB)
    used = conn.execute("SELECT used_views FROM docs WHERE token=?", (token,)).fetchone()[0]
    conn.close()
    check("刷新后已用次数仍是 1（没被重复扣）", used == 1, f"used={used}")

    st, third = call_json("/api/prepare", {"token": token})
    check("没有可用 session 时再次打开被拒", third.get("ok") is False and st == 403, third)

    print("\n[4] 60 秒宽限：扣了次但一页都没看到 → 自动退还")
    token2, _ = publish(limit=1)
    _, p2 = call_json("/api/prepare", {"token": token2})
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE sessions SET start_ts = start_ts - 61 WHERE sid=?", (p2["sid"],))
    conn.commit()
    conn.close()
    _, p3 = call_json("/api/prepare", {"token": token2})
    check("把时间往前推 61 秒后，可以重新打开", p3.get("ok") is True, p3)
    conn = sqlite3.connect(DB)
    used = conn.execute("SELECT used_views FROM docs WHERE token=?", (token2,)).fetchone()[0]
    conn.close()
    check("退还只抵掉那一次，总数仍是 1 而不是 2", used == 1, f"used={used}")

    print("\n[5] 作废 / 恢复")
    token3, _ = publish(limit=5)
    call_json("/api/revoke", {"token": token3}, key=True)
    _, i5 = call_json(f"/api/info?token={token3}")
    check("作废后状态为 revoked", i5.get("status") == "revoked", i5)
    _, prep5 = call_json("/api/prepare", {"token": token3})
    check("作废后无法开始阅读", prep5.get("ok") is False, prep5)
    call_json("/api/revoke", {"token": token3, "restore": True}, key=True)
    _, i6 = call_json(f"/api/info?token={token3}")
    check("恢复后重新可用", i6.get("status") == "ready", i6)

    print("\n[6] 过期 / 不存在")
    token4, _ = publish(limit=5, days=0)
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE docs SET expire_ts=? WHERE token=?", (time.time() - 10, token4))
    conn.commit()
    conn.close()
    _, i7 = call_json(f"/api/info?token={token4}")
    check("过期后状态为 expired", i7.get("status") == "expired", i7)
    _, i8 = call_json("/api/info?token=does-not-exist")
    check("不存在的 token 返回 notfound", i8.get("status") == "notfound", i8)

    print("\n[7] 管理接口需要密钥")
    st, _ = call_json("/api/list")
    check("无密钥读列表被拒 (401)", st == 401, f"实际 {st}")
    st, ls = call_json("/api/list", key=True)
    check("带密钥可以读列表", st == 200 and ls.get("ok"), ls)

    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 52)
    print(f"  通过 {len(PASS)} / {total}")
    if FAIL:
        print("  失败：")
        for f in FAIL:
            print("   - " + f)
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
