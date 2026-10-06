#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令行发布（方便脚本化 / 跟 ERP 打通）。

示例：
  D:\\Python312\\python.exe publish.py samples\\sample-quote.pdf ^
      --limit 1 --duration 10 --days 3 --strips 4 ^
      --watermark "仅限 XX 电子 2026-09-30"
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="要发布的 PDF 路径")
    ap.add_argument("--host", default="http://127.0.0.1:8770")
    ap.add_argument("--key", default="", help="管理密钥；不给则读 data/admin.key")
    ap.add_argument("--name", default="")
    ap.add_argument("--limit", type=int, default=1, help="可查看次数")
    ap.add_argument("--duration", type=int, default=10, help="单次可看分钟数")
    ap.add_argument("--days", type=float, default=3, help="链接有效期（天），0=永久")
    ap.add_argument("--strips", type=int, default=1, help="每页切条数")
    ap.add_argument("--scale", type=float, default=2.0, help="渲染倍率，越小越省流量")
    ap.add_argument("--quality", type=int, default=88, help="JPEG 质量 40-95")
    ap.add_argument("--watermark", default="", help="水印文字")
    a = ap.parse_args()

    pdf = Path(a.pdf)
    if not pdf.exists():
        print("找不到文件：", pdf)
        return 2

    key = a.key or (HERE / "data" / "admin.key").read_text(encoding="utf-8").strip()
    payload = {
        "name": a.name or pdf.stem,
        "pdf_b64": base64.b64encode(pdf.read_bytes()).decode("ascii"),
        "limit": a.limit,
        "duration": a.duration * 60,
        "expire_days": a.days,
        "strips": a.strips,
        "scale": a.scale,
        "quality": a.quality,
        "watermark": a.watermark,
    }
    req = urllib.request.Request(
        a.host.rstrip("/") + "/api/publish",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Admin-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("发布失败：", e.code, e.read().decode("utf-8", "ignore"))
        return 1
    except Exception as e:
        print("连接服务失败：", e, "（服务启动了吗？）")
        return 1

    if not res.get("ok"):
        print("发布失败：", res.get("msg"))
        return 1

    base = a.host.rstrip("/")
    print("=" * 60)
    print(f"已发布：{payload['name']}（{res['pages']} 页）")
    print(f"次数   ：{a.limit} 次  ·  单次 {a.duration} 分钟  ·  有效期 {a.days} 天")
    print(f"水印   ：{a.watermark or '（无）'}")
    print("-" * 60)
    print(f"客户链接：{base}/v/{res['token']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
