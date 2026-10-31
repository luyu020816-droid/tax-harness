"""Public synthetic scan fixtures. Run with the isolated OCR Python runtime."""

import argparse
from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont


def image(
    lines, number, total, *, columns=False, table=False, font_path="C:/Windows/Fonts/msyh.ttc"
):
    page = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.truetype(font_path, 32)
    draw.text((120, 110), "合成资料使用与保管制度", font=font, fill="black")
    for index, text in enumerate(lines):
        draw.text((120, 240 + index * 85), text, font=font, fill="black")
    if columns:
        for x, items in [
            (120, ["左栏：日常要求", "内部资料不得发到外部群聊。", "未经批准不得自行外发。"]),
            (880, ["右栏：审批限制", "书面批准后仅分享脱敏汇总。", "不得公开客户身份信息。"]),
        ]:
            for row, text in enumerate(items):
                draw.text((x, 650 + row * 75), text, font=font, fill="black")
    if table:
        for x in (120, 600, 1000, 1500):
            draw.line((x, 1050, x, 1350), fill="black", width=3)
        for y in (1050, 1150, 1250, 1350):
            draw.line((120, y, 1500, y), fill="black", width=3)
        for row, items in enumerate(
            [
                ["资料类型", "保留期限（年）", "要求"],
                ["普通记录", "3", "到期审批"],
                ["涉诉记录", "5", "不得提前销毁"],
            ]
        ):
            for x, text in zip((140, 620, 1020), items, strict=False):
                draw.text((x, 1070 + 100 * row), text, font=font, fill="black")
    draw.text((120, 2100), f"第{number}页 共{total}页", font=font, fill="black")
    return page


def append_image(doc, img):
    output = BytesIO()
    img.save(output, format="PNG")
    page = doc.new_page(width=595.28, height=841.89)
    page.insert_image(page.rect, stream=output.getvalue())


def create(directory, font):
    directory.mkdir(parents=True, exist_ok=False)
    doc = fitz.open()
    page = doc.new_page(width=595.28, height=841.89)
    page.insert_text(
        (72, 100),
        "SYNTHETIC POLICY: native text cover; rules follow on scanned pages.",
        fontsize=11,
    )
    append_image(
        doc,
        image(
            ["未经负责人书面批准，不得外发内部资料。", "下一页载明例外和限制，须一并阅读。"],
            2,
            3,
            columns=True,
            font_path=font,
        ),
    )
    append_image(
        doc,
        image(
            [
                "前页规则的例外：经书面批准，可对外提供脱敏汇总表。",
                "上述例外不允许公开客户手机号。",
            ],
            3,
            3,
            table=True,
            font_path=font,
        ),
    )
    doc.save(directory / "mixed.pdf", deflate=True)
    doc.delete_page(2)
    doc.save(directory / "missing.pdf", deflate=True)
    doc.close()
    low = fitz.open()
    append_image(
        low, image(["低清晰度测试，不得发布空成功。"], 1, 1, font_path=font).resize((150, 212))
    )
    low.save(directory / "low.pdf", deflate=True)
    low.close()
    (directory / "corrupt.pdf").write_bytes(b"%PDF-1.7\ninvalid-truncated-fixture")
    print("Synthetic OCR fixtures written.", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--font", default="C:/Windows/Fonts/msyh.ttc")
    a = p.parse_args()
    create(a.output, a.font)
