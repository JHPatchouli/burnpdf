<div align="center">

# 阅后即焚 PDF

把 PDF 发成可计次的阅读链接。原文件留在服务器上，阅读页只接收带水印的页面图片。

计次 · 单次时长 · 到期时间 · 页面水印 · 访问日志

View counts · Session duration · Expiry · Page watermarks · Access logs

<br>

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](server.py)
[![PyMuPDF](https://img.shields.io/badge/pymupdf-1.28-1f6feb.svg)](https://pymupdf.readthedocs.io/)

</div>

---

打开链接不消耗次数。阅读者点击「开始阅读」后开始计次，并在设定时长内查看。次数用完、链接过期或被作废后，链接不再打开。

截图和拍照无法阻止。水印写在图片上，包含链接标识和阅读时间。

## 功能

| 模块 | 内容 |
|---|---|
| 计次 | 总次数、单次时长、到期时间 |
| 会话 | 刷新不重复扣次；未加载页面时退回次数 |
| 下发 | 原 PDF 不下发，页面以 JPEG 图片提供 |
| 水印 | 发布时写入页面，包含标识、链接和时间 |
| 管理 | 上传、作废、恢复、访问日志 |

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | 发布、计次、取图、作废和访问日志 |
| `viewer.html` | 阅读页 |
| `admin.html` | 管理台 |
| `publish.py` | 命令行发布 |
| `make_sample.py` | 生成示例 PDF |
| `selftest.py` | 针对已启动服务的回归检查 |
| `data/` | 运行后生成，含数据库、页面图片和管理密钥 |

## 计次

1. 打开链接只显示说明，不扣次数。
2. 次数在创建阅读会话时扣除。同一会话在时限内可以继续取图。
3. 刷新页面会复用保存在 `sessionStorage` 中的会话，不重复扣次。
4. 扣次后 60 秒内没有加载到任何页面时，下次打开会退回这一次。
5. 图片地址带会话标识。无效、越界或超时的请求会被拒绝。

## 开始

```bash
pip install pymupdf
python make_sample.py
python server.py
```

首次启动会生成管理密钥，并打印管理台地址：

```text
http://127.0.0.1:8770/admin
```

密钥保存在 `data/admin.key`，之后启动不再打印。打开 `/admin`，填入密钥后上传 PDF 并设置参数。

```bash
python publish.py samples/sample-quote.pdf \
    --limit 1 --duration 10 --days 3 --strips 4 \
    --watermark "SAMPLE 2026-09-30"
```

| 参数 | 说明 |
|---|---|
| `--limit` | 可阅读的总次数 |
| `--duration` | 单次阅读时长，单位分钟 |
| `--days` | 链接有效天数，`0` 表示不按日期失效 |
| `--strips` | 每页切成几条图片下发 |
| `--watermark` | 附加到水印中的文字 |

先启动 `server.py`，再运行 `python selftest.py`。检查内容包括计次、会话内取图、无效会话、刷新不重复扣次、宽限退次、作废、过期和管理接口鉴权。

## 限制

- 水印在发布时生成，不会按每次打开重新编号。
- 管理台不生成二维码。
- 输入格式为 PDF，输出图片为 JPEG。
- 日志保存在 `data/burn.db`，需要自行清理。

容器运行见 [DOCKER.md](DOCKER.md)。
