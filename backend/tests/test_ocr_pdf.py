"""Parser boundaries without loading the optional Paddle runtime in pytest."""

import pytest

from app.errors import AppError
from app.knowledge_parser import chunk_blocks, parse_isolated
from app.ocr_pdf import Table, reading_order
from app.ocr_status import options


def block(text, box, label="text"):
    return {"block_content": text, "block_bbox": box, "block_label": label}


def test_two_columns_keep_each_column_complete_and_preserve_all_blocks():
    title = block("Title", [0, 0, 600, 10])
    footer = block("2/3", [0, 100, 600, 110], "number")
    a = block("left1", [10, 20, 240, 30])
    b = block("left2", [10, 40, 240, 50])
    c = block("right1", [320, 20, 540, 30])
    d = block("right2", [320, 40, 540, 50])
    result, mode = reading_order([title, a, c, b, d, footer], 600)
    assert result == [title, a, b, c, d, footer] and mode == "two_columns"


def test_spanning_content_inside_two_columns_is_explicit_failure():
    blocks = [
        block("a", [0, 20, 250, 30]),
        block("b", [0, 60, 250, 70]),
        block("c", [350, 20, 600, 30]),
        block("d", [350, 60, 600, 70]),
        block("spanning", [0, 40, 600, 50]),
    ]
    with pytest.raises(AppError, match="多栏"):
        reading_order(blocks, 600)


def test_plain_page_uses_model_order_without_invented_columns():
    blocks = [block("a", [0, 0, 600, 20]), block("b", [0, 30, 600, 40])]
    assert reading_order(blocks, 600) == (blocks, "model")


def test_table_merged_cells_fail_instead_of_dropping_units():
    table = Table()
    with pytest.raises(AppError):
        table.feed('<table><tr><td colspan="2">年</td></tr></table>')
    regular = Table()
    regular.feed(
        "<table><tr><th>期限(年)</th><th>对象</th></tr><tr><td>3</td><td>汇总表</td></tr></table>"
    )
    assert regular.rows == [["期限(年)", "对象"], ["3", "汇总表"]]


def test_ocr_citation_compacts_metadata_without_losing_page_or_text():
    blocks = []
    for i in range(8):
        blocks.append(
            {
                "ordinal": i,
                "kind": "paragraph",
                "heading_path": "",
                "content": f"第{i}段不得外发。",
                "locator": {
                    "type": "pdf",
                    "page": 2,
                    "unit": "physical_page",
                    "bbox": [10, i * 10, 200, i * 10 + 8],
                    "bbox_unit": "pdf_points",
                    "origin": "top_left",
                    "extraction": "ocr",
                    "ocr_version": "test",
                    "confidence_min": 0.93,
                    "confidence_mean": 0.97,
                    "layout_block": i,
                    "raw_text_sha256": "a" * 64,
                    "normalization": "NFKC+t2s",
                },
            }
        )
    chunks = chunk_blocks(blocks, "制度", len)
    assert len(chunks) == 1
    assert all(b["content"] in chunks[0]["content"] for b in blocks)
    locator = chunks[0]["locator"]
    assert len(locator) == 1
    assert locator[0]["page"] == 2 and locator[0]["layout_blocks"] == list(range(8))
    assert locator[0]["bbox"] == [10, 0, 200, 78]
    assert blocks[0]["locator"]["bbox"] == [10, 0, 200, 8]


def test_optional_ocr_missing_does_not_disable_text_parser(settings, tmp_path):
    assert options(settings.model_copy(update={"knowledge_ocr_runtime": tmp_path})) is None
    assert parse_isolated("不得猜测用途".encode(), ".txt")["blocks"][0]["content"] == "不得猜测用途"
