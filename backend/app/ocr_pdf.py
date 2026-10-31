"""Local Paddle document parsing. Original PDF bytes remain the authority."""

import hashlib
import json
import math
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

from app.errors import AppError

OCR_VERSION = "paddle-structure-3.4.0-v1"


def verify_models(directory):
    root = Path(directory)
    lock = json.loads((root / "installation.json").read_text(encoding="utf-8"))
    for item in lock["models"].values():
        for name, meta in item["files"].items():
            path = (root / item["name"] / name).resolve()
            if not path.is_relative_to(root.resolve()) or path.stat().st_size != meta["bytes"]:
                raise AppError("DEPENDENCY_UNAVAILABLE", "OCR模型安装不完整。", 503)
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != meta["sha256"]:
                    raise AppError("DEPENDENCY_UNAVAILABLE", "OCR模型校验失败。", 503)
    return lock


class Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in ("td", "th"):
            if any(key in ("rowspan", "colspan") and value != "1" for key, value in attrs):
                raise AppError(
                    "DOCUMENT_PARSE_FAILED",
                    "复杂合并单元格需人工整理后接入，未发布不完整表格。",
                    422,
                )
            self.cell = []
        if tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def normalize(text):
    return unicodedata.normalize("NFKC", text).replace("\r", "").strip()


def reading_order(blocks, width):
    """Resolve a regular two-column band without crossing spanning content."""
    right = [
        b
        for b in blocks
        if b["block_label"] not in ("table", "image", "number") and b["block_bbox"][0] > width * 0.5
    ]
    if len(right) < 2:
        return blocks, "model"
    top = min(b["block_bbox"][1] for b in right)
    bottom = max(b["block_bbox"][3] for b in right)
    band = [b for b in blocks if b["block_bbox"][1] < bottom and b["block_bbox"][3] > top]
    left = [b for b in band if b["block_bbox"][2] < width * 0.52]
    if len(left) < 2:
        return blocks, "model"
    if len(left) + len(right) != len(band) or max(b["block_bbox"][2] for b in left) >= min(
        b["block_bbox"][0] for b in right
    ):
        raise AppError("OCR_LOW_QUALITY", "多栏版面存在交叠内容，需人工整理，未猜测阅读顺序。", 422)
    for column in (left, right):
        if max(b["block_bbox"][0] for b in column) >= min(b["block_bbox"][2] for b in column):
            raise AppError("OCR_LOW_QUALITY", "版面超出规则双栏支持范围，请人工整理。", 422)

    def key(block):
        return block["block_bbox"][1], block["block_bbox"][0]

    before = [b for b in blocks if b["block_bbox"][3] <= top]
    after = [b for b in blocks if b["block_bbox"][1] >= bottom]
    ordered = (
        sorted(before, key=key)
        + sorted(left, key=key)
        + sorted(right, key=key)
        + sorted(after, key=key)
    )
    if len(ordered) != len(blocks) or {id(b) for b in ordered} != {id(b) for b in blocks}:
        raise AppError("OCR_LOW_QUALITY", "多栏版面无法完整排序，请人工整理原件。", 422)
    return ordered, "two_columns"


def extract_pdf(content, config):
    import fitz
    import numpy as np
    from opencc import OpenCC

    converter = OpenCC("t2s")

    doc = fitz.open(stream=content, filetype="pdf")
    if doc.is_encrypted or len(doc) > 200:
        raise AppError("DOCUMENT_PARSE_FAILED", "PDF加密或超过200页。", 422)
    blocks = []
    pipeline = None
    scan_pages = 0
    page_texts = []
    model_lock = None

    def add(text, kind, heading, locator):
        original = text
        text = (
            converter.convert(normalize(text))
            if locator.get("extraction") == "ocr"
            else text.strip()
        )
        if locator.get("extraction") == "ocr":
            locator = {
                **locator,
                "normalization": "NFKC+t2s",
                "raw_text_sha256": hashlib.sha256(original.encode()).hexdigest(),
            }
        if text:
            blocks.append(
                {
                    "ordinal": len(blocks),
                    "kind": kind,
                    "heading_path": heading,
                    "content": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "locator": locator,
                }
            )

    try:
        for number, page in enumerate(doc, 1):
            native = page.get_text().strip()
            if native and not page.get_images():
                texts = []
                for part in page.get_text("blocks"):
                    if len(part) > 6 and part[6] == 0:
                        add(
                            part[4],
                            "paragraph",
                            "",
                            {
                                "type": "pdf",
                                "page": number,
                                "unit": "physical_page",
                                "bbox": list(part[:4]),
                                "bbox_unit": "pdf_points",
                                "origin": "top_left",
                                "extraction": "native",
                            },
                        )
                        texts.append(part[4])
                page_texts.append("".join(texts))
                continue
            if not native and not page.get_images() and not page.get_drawings():
                page_texts.append("")
                continue
            scan_pages += 1
            if scan_pages > config["max_pages"]:
                raise AppError("DOCUMENT_TOO_LARGE", "OCR页数超过限制，未发布部分内容。", 413)
            scale = 200 / 72
            if page.rect.width * scale * page.rect.height * scale > config["max_pixels"]:
                raise AppError("DOCUMENT_TOO_LARGE", "单页OCR像素超过限制。", 413)
            # Reject a low-resolution full-page raster before upsampling can
            # disguise its lack of source detail.
            for image in page.get_images(full=True):
                for rect in page.get_image_rects(image[0]):
                    if (
                        rect.get_area() > page.rect.get_area() * 0.5
                        and min(image[2] * 72 / rect.width, image[3] * 72 / rect.height) < 100
                    ):
                        raise AppError(
                            "OCR_LOW_QUALITY", "整页扫描分辨率低于100dpi，请提供更清晰原件。", 422
                        )
            if pipeline is None:
                model_lock = verify_models(config["models"])
                from paddleocr import PPStructureV3

                kwargs = {}
                for role, item in model_lock["models"].items():
                    kwargs[role + "_model_name"] = item["name"]
                    kwargs[role + "_model_dir"] = str(Path(config["models"]) / item["name"])
                pipeline = PPStructureV3(
                    **kwargs,
                    device="cpu",
                    cpu_threads=4,
                    enable_mkldnn=False,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    use_region_detection=False,
                    use_seal_recognition=False,
                    use_formula_recognition=False,
                    use_chart_recognition=False,
                    markdown_ignore_labels=[],
                )
            pix = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False
            )
            image = (
                np.frombuffer(pix.samples, dtype=np.uint8)
                .reshape(pix.height, pix.width, 3)[:, :, ::-1]
                .copy()
            )
            results = list(pipeline.predict(image, use_table_orientation_classify=False))
            if len(results) != 1:
                raise AppError("DOCUMENT_PARSE_FAILED", "OCR页数与输入不一致。", 422)
            result = results[0].json["res"]
            ocr = result["overall_ocr_res"]
            scores = ocr["rec_scores"]
            if (
                not scores
                or not all(math.isfinite(v) and 0 <= v <= 1 for v in scores)
                or min(scores) < config["min_confidence"]
                or sum(scores) / len(scores) < 0.9
            ):
                raise AppError(
                    "OCR_LOW_QUALITY", f"第{number}页识别置信不足，请核对或更换原件。", 422
                )
            text_page = []
            covered = []
            original_blocks = result["parsing_res_list"]
            ordered, order_mode = reading_order(original_blocks, pix.width)
            for block in ordered:
                index = next(i for i, b in enumerate(original_blocks) if b is block)
                text = block["block_content"]
                label = block["block_label"]
                if not text.strip():
                    continue
                bbox = block["block_bbox"]
                locator = {
                    "type": "pdf",
                    "page": number,
                    "unit": "physical_page",
                    "bbox": [round(v / scale, 3) for v in bbox],
                    "bbox_unit": "pdf_points",
                    "origin": "top_left",
                    "extraction": "ocr",
                    "ocr_version": OCR_VERSION,
                    "confidence_min": round(min(scores), 5),
                    "confidence_mean": round(sum(scores) / len(scores), 5),
                    "layout_block": index,
                    "layout_label": label,
                    "reading_order": order_mode,
                    "model_manifest_sha256": hashlib.sha256(
                        json.dumps(model_lock, sort_keys=True).encode()
                    ).hexdigest(),
                }
                if label == "table":
                    table = Table()
                    table.feed(text)
                    if not table.rows or any(len(r) != len(table.rows[0]) for r in table.rows):
                        raise AppError("DOCUMENT_PARSE_FAILED", "表格行列不完整，需人工整理。", 422)
                    for row_index, row in enumerate(table.rows):
                        cells = "\t".join(row)
                        covered.extend(row)
                        text_page.extend(row)
                        add(
                            ("\t".join(table.rows[0]) + "\n" if row_index else "") + cells,
                            "table_row",
                            "",
                            {
                                **locator,
                                "table": index,
                                "row": row_index + 1,
                                "includes_header": row_index > 0,
                            },
                        )
                else:
                    add(
                        text,
                        "paragraph",
                        text if label in ("doc_title", "paragraph_title") else "",
                        locator,
                    )
                    covered.append(text)
                    text_page.append(text)

            def compact(text):
                return re.sub(r"\s+", "", normalize(text))

            extracted = compact("".join(covered))
            missing = [t for t in ocr["rec_texts"] if compact(t) and compact(t) not in extracted]
            if missing:
                raise AppError("OCR_LOW_QUALITY", "版面结果未覆盖全部识别行，未静默丢弃正文。", 422)
            page_texts.append("".join(text_page))
        for index, text in enumerate(page_texts, 1):
            for match in re.finditer(
                r"第\s*(\d+)\s*页\s*[（(]?\s*共\s*(\d+)\s*页", normalize(text)
            ):
                if int(match[1]) != index or int(match[2]) != len(doc):
                    raise AppError(
                        "DOCUMENT_PARSE_FAILED", "印刷页码与物理页数不符，可能缺页或顺序错误。", 422
                    )
        if not blocks:
            raise AppError("DOCUMENT_PARSE_FAILED", "PDF无可用正文，未发布空版本。", 422)
        return {
            "parser_version": OCR_VERSION if scan_pages else "pdf-native-2",
            "blocks": blocks,
            "ocr_pages": scan_pages,
            "physical_pages": len(doc),
        }
    finally:
        if pipeline:
            pipeline.close()
        doc.close()
