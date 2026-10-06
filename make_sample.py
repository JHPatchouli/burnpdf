#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一份用于测试的示例 PDF（中文报价单，3 页）。"""

from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
OUT = HERE / "samples" / "sample-quote.pdf"
FONT = "china-s"          # PyMuPDF 内置简体中文字体


def frame(page, title, subtitle=""):
    w, h = page.rect.width, page.rect.height
    page.draw_rect(pymupdf.Rect(40, 40, w - 40, h - 40), color=(0.82, 0.82, 0.82), width=0.7)
    page.insert_text((56, 86), title, fontname=FONT, fontsize=21, color=(0.08, 0.08, 0.08))
    if subtitle:
        page.insert_text((56, 110), subtitle, fontname=FONT, fontsize=10.5, color=(0.42, 0.42, 0.42))
    page.draw_line(pymupdf.Point(56, 122), pymupdf.Point(w - 56, 122),
                   color=(0.78, 0.78, 0.78), width=0.8)


def table(page, x, y, widths, rows, row_h=25, head=True):
    total = sum(widths)
    for r, row in enumerate(rows):
        cy = y + r * row_h
        fill = (0.94, 0.95, 0.97) if (head and r == 0) else None
        if fill:
            page.draw_rect(pymupdf.Rect(x, cy, x + total, cy + row_h),
                           color=None, fill=fill)
        cx = x
        for c, cell in enumerate(row):
            page.draw_rect(pymupdf.Rect(cx, cy, cx + widths[c], cy + row_h),
                           color=(0.78, 0.78, 0.8), width=0.6)
            page.insert_text((cx + 7, cy + row_h * 0.68), str(cell),
                             fontname=FONT, fontsize=9.5,
                             color=(0.15, 0.15, 0.15))
            cx += widths[c]
    return y + len(rows) * row_h


def main():
    doc = pymupdf.open()

    # ---- 第 1 页：报价单 ----
    p1 = doc.new_page()
    frame(p1, "产品报价单", "报价单号 QUO-XH202-0109    报价日期 2026-09-30    有效期 30 天")
    y = 160
    for line in [
        "致：深圳市 XX 电子科技有限公司（采购部）",
        "发件：东莞市 XX 精密制造有限公司   联系人 / 电话：李工 138-0000-0000",
        "",
        "一、产品明细（含税单价，人民币）",
    ]:
        if line:
            p1.insert_text((56, y), line, fontname=FONT, fontsize=11, color=(0.12, 0.12, 0.12))
        y += 20
    y = table(p1, 56, y + 8, [110, 96, 70, 80, 70, 90], [
        ["型号", "产品名称", "材质", "数量(个)", "单价(元)", "金额(元)"],
        ["XH-202", "电机支架", "SUS304", "5,000", "3.20", "16,000.00"],
        ["XH-203", "齿轮箱上盖", "ADC12", "5,000", "2.85", "14,250.00"],
        ["XH-205", "密封圈", "硅胶", "20,000", "0.42", "8,400.00"],
        ["XH-210", "线束总成", "UL1007", "5,000", "4.60", "23,000.00"],
        ["", "合计", "", "", "", "61,650.00"],
    ])
    y += 26
    for line in [
        "二、价格说明",
        "1. 上述单价为含 13% 增值税价，MOQ 5,000 个；数量低于 MOQ 单价上浮 8%。",
        "2. 报价不含模具费；模具费一次性 28,000 元，量产满 50,000 个后返还 50%。",
        "3. 报价有效期至 2026-10-30，逾期需重新确认。",
    ]:
        p1.insert_text((56, y), line, fontname=FONT, fontsize=10.5, color=(0.2, 0.2, 0.2))
        y += 20

    # ---- 第 2 页：技术参数 ----
    p2 = doc.new_page()
    frame(p2, "技术参数与工艺说明", "对应型号 XH-202 / XH-203 / XH-205 / XH-210")
    y = table(p2, 56, 156, [140, 120, 150, 106], [
        ["项目", "规格要求", "检测方法", "备注"],
        ["材质", "SUS304 / ADC12 / 硅胶", "材质报告", "每批随货"],
        ["表面处理", "拉丝 + 钝化", "目视 / 膜厚仪", "膜厚 8-12μm"],
        ["平面度", "≤ 0.15 mm", "塞尺 / 三坐标", "全检"],
        ["盐雾测试", "48h 无红锈", "GB/T 10125", "抽检 5 件"],
        ["装配扭矩", "0.8-1.2 N·m", "扭力扳手", "100% 记录"],
    ])
    y += 30
    for line in [
        "包装与交付",
        "· 内包装：防静电袋 + 珍珠棉；外箱：五层瓦楞纸箱，每箱 500 个。",
        "· 交期：样品 7 个工作日，批量 18 个工作日（含 3 个工作日检测）。",
        "· 付款：30% 定金，出货前付清余款；月结 30 天可另议。",
        "",
        "本页参数以最终确认的图纸与检验标准为准。",
    ]:
        if line:
            p2.insert_text((56, y), line, fontname=FONT, fontsize=10.5, color=(0.2, 0.2, 0.2))
        y += 20

    # ---- 第 3 页：条款 ----
    p3 = doc.new_page()
    frame(p3, "商务条款与签署", "本页为报价单组成部分")
    y = 160
    for line in [
        "一、质量与售后",
        "1. 不良率超过 0.5% 的部分，由供方免费补货；重大批量异常 48 小时内响应。",
        "2. 质保期 12 个月，自客户签收之日起算。",
        "",
        "二、知识产权与保密",
        "1. 本报价单及所附图纸仅供贵司内部评估使用，未经书面同意不得转发第三方。",
        "2. 双方对交易过程中知悉的技术与商务信息负有保密义务，有效期 3 年。",
        "",
        "三、签署",
    ]:
        if line:
            p3.insert_text((56, y), line, fontname=FONT, fontsize=10.5, color=(0.2, 0.2, 0.2))
        y += 20
    table(p3, 56, y + 10, [150, 150, 216], [
        ["需方（盖章）", "供方（盖章）", "备注"],
        ["", "", "本页签字确认价格与交期"],
        ["", "", ""],
    ], row_h=46)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    doc.close()
    print("已生成：", OUT, f"（{OUT.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
