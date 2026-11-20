"""Parse a bounded read-only workbook and aggregate its original amount cells."""

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from unicodedata import category as char_category
from unicodedata import normalize
from xml.etree.ElementTree import iterparse
from zipfile import ZipFile

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from app.errors import AppError
from app.xlsx_invoices import validate_archive

PARSER_VERSION = "workbook-2"
MAX_ROWS = 10001  # One header plus 10,000 physical data rows.
MAX_COLUMNS = 50
ALIASES = {
    "date": {"日期", "报销日期", "支付日期"},
    "category": {"类别", "费用类别", "费用类型"},
    "amount": {"实付金额", "实际支付金额", "报销金额", "金额"},
}
TOTAL_LABEL = re.compile(r"^(?:总?合计|小计|总计|汇总|subtotal|grand total|total)(?:[：:]|$)", re.I)
NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?")
CURRENCY_COLUMNS = {
    "币种",
    "支付币种",
    "报销币种",
    "交易币种",
    "结算币种",
    "货币",
    "货币单位",
    "金额单位",
    "currency",
    "currency code",
}
RMB_UNITS = {"CNY", "RMB", "人民币", "人民币元", "元", "¥", "CN¥"}
EXCEL_CURRENCY = re.compile(r"\[\$([^\]-]*)(?:-([^\]]+))?\]")


def clean(value):
    return normalize("NFKC", value).strip()


def _amount_unit(header):
    """Return unit evidence, or None when the heading is not an amount field."""
    for name in sorted(ALIASES["amount"], key=len, reverse=True):
        if not header.startswith(name):
            continue
        suffix = header[len(name) :].strip()
        if (
            not suffix
            or suffix[0] in "([/:"
            or re.fullmatch(
                r"(?:[A-Za-z]{3}|人民币元?|[万千百万亿]?(?:元|角|分)|[\u4e00-\u9fff]{1,4}币|[¥€$£])",
                suffix,
            )
        ):
            return suffix.strip("()[]/: ").upper()
    return None


def _unsupported_format(value):
    value = clean(value).upper()
    for symbol, locale in EXCEL_CURRENCY.findall(value):
        if symbol and (
            symbol not in RMB_UNITS
            or (symbol == "¥" and locale not in {"", "804", "0804", "ZH-CN"})
        ):
            return True
    value = re.sub(r"\[[^\]]*\]|[_*].", "", value)
    if "%" in value or re.search(r"(?<=[#0?]),+(?![#0?])", value):
        return True
    if any(char_category(char) == "Sc" and char != "¥" for char in value):
        return True
    for literal in re.findall(r'"([^"]*)"', value):
        if any(char.isalpha() for char in literal) and "".join(literal.split()) not in RMB_UNITS:
            return True
    return any(
        token not in RMB_UNITS and token != "GENERAL"
        for token in re.findall(r"[A-Z]{3,}|[\u4e00-\u9fff]+", value)
    )


def _xml_cells(archive, path):
    """Keep raw OOXML numerals so Excel money does not round-trip through float."""
    numbers = {}
    reason = None
    last_row = 0
    with archive.open(path) as stream:
        for _, element in iterparse(stream, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag in ("row", "col") and element.get("hidden") in ("1", "true"):
                reason = "工作表包含隐藏行或列。"
            if tag == "mergeCell":
                reason = "工作表包含合并单元格。"
            if tag == "c":
                address = element.get("r", "")
                if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", address):
                    raise AppError("WORKBOOK_UNSUPPORTED", "工作表单元格坐标无效。", 422)
                column, row = re.fullmatch(r"([A-Z]+)([0-9]+)", address).groups()
                last_row = max(last_row, int(row))
                if last_row > MAX_ROWS or column_index_from_string(column) > MAX_COLUMNS:
                    raise AppError("WORKBOOK_UNSUPPORTED", "工作表超出行列上限。", 422)
                if element.get("t", "n") == "n":
                    value = element.find("{*}v")
                    if value is not None:
                        numbers[address] = value.text
                element.clear()
            elif tag in ("row", "col", "mergeCell"):
                element.clear()
    return numbers, reason, last_row


def _cell(cell, raw):
    value = cell.value
    if value is None:
        return None
    if cell.data_type in ("e", "f", "b"):
        return ["invalid", None]
    if isinstance(value, datetime):
        return ["date", value.date().isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if cell.data_type == "n":
        return ["number", raw.get(cell.coordinate)]
    if isinstance(value, str) and len(value) <= 500:
        return ["text", value]
    return ["invalid", None]


def parse_workbook(content):
    validate_archive(content, max_column=MAX_COLUMNS, max_row=MAX_ROWS, allow_formulas=True)
    book = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
    try:
        if len(book.sheetnames) > 20:
            raise AppError("WORKBOOK_UNSUPPORTED", "工作簿最多支持 20 个工作表。", 422)
        sheets = []
        with ZipFile(BytesIO(content)) as archive:
            for sheet in book.worksheets:
                if sheet.sheet_state != "visible":
                    continue
                raw, reason, last_row = _xml_cells(archive, sheet._worksheet_path)
                candidates = {field: {} for field in ALIASES}
                currency_columns, amount_units = {}, {}
                data = []
                header = None
                sheet.reset_dimensions()
                if not reason and last_row:
                    for row_number, cells in enumerate(
                        sheet.iter_rows(max_row=last_row, max_col=MAX_COLUMNS), 1
                    ):
                        if not any(cell.value is not None and cell.value != "" for cell in cells):
                            continue
                        if header is None:
                            header = row_number
                            for index, cell in enumerate(cells, 1):
                                if not isinstance(cell.value, str) or cell.data_type == "f":
                                    continue
                                column = get_column_letter(index)
                                name = clean(cell.value)
                                for field in ("date", "category"):
                                    if name in ALIASES[field]:
                                        candidates[field][column] = cell.value
                                unit = _amount_unit(name)
                                if unit is not None:
                                    candidates["amount"][column] = cell.value
                                    amount_units[column] = unit
                                if (
                                    re.split(r"[(\[:]", name.casefold(), maxsplit=1)[0].strip()
                                    in CURRENCY_COLUMNS
                                ):
                                    currency_columns[column] = cell.value
                            missing = [field for field, cols in candidates.items() if not cols]
                            if missing:
                                labels = {"date": "日期", "category": "费用类别", "amount": "金额"}
                                reason = "缺少列：" + "、".join(labels[field] for field in missing)
                                break
                            indexes = {
                                get_column_letter(index): index - 1
                                for index in range(1, MAX_COLUMNS + 1)
                                if get_column_letter(index) in currency_columns
                                or any(
                                    get_column_letter(index) in cols for cols in candidates.values()
                                )
                            }
                            continue
                        if any(
                            isinstance(cell.value, str) and TOTAL_LABEL.match(clean(cell.value))
                            for cell in cells
                        ):
                            reason = "工作表包含显式汇总行，请仅上传明细。"
                            break
                        unsupported_formats = [
                            col
                            for col in amount_units
                            if cells[indexes[col]].value is not None
                            and _unsupported_format(cells[indexes[col]].number_format)
                        ]
                        data.append(
                            [
                                row_number,
                                {col: _cell(cells[i], raw) for col, i in indexes.items()},
                                unsupported_formats,
                            ]
                        )
                sheets.append(
                    {
                        "name": sheet.title,
                        "header_row": header,
                        "columns": candidates,
                        "currency_columns": currency_columns,
                        "amount_units": amount_units,
                        "reason": reason or ("工作表为空。" if header is None else None),
                        "rows": [] if reason else data,
                    }
                )
    finally:
        book.close()
    return {"parser_version": PARSER_VERSION, "sheets": sheets}


def _date(cell):
    kind, value = cell or (None, None)
    if kind not in ("text", "date") or not re.fullmatch(r"\d{4}([-\/])\d{2}\1\d{2}", clean(value)):
        raise ValueError
    return date.fromisoformat(clean(value).replace("/", "-"))


def _amount(cell):
    kind, value = cell or (None, None)
    if kind == "text":
        value = clean(value)
        if not NUMBER.fullmatch(value):
            raise ValueError
        value = value.replace(",", "")
    elif kind != "number" or value is None or len(value) > 40:
        raise ValueError
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError from exc
    if not amount.is_finite() or abs(amount) >= 10**13:
        raise ValueError
    cents = amount.quantize(Decimal("0.01"))
    if amount != cents:
        if kind != "number" or abs(amount - cents) > Decimal(1).scaleb(amount.adjusted() - 15):
            raise ValueError
    return cents


def _unit_error(sheet, address):
    return AppError(
        "WORKBOOK_UNIT_UNSUPPORTED",
        f"工作表「{sheet}」的 {address} 无法确认为人民币元，请核对币种或金额单位。",
        422,
        details={"sheet": sheet, "cell": address},
    )


def calculate(snapshot, *, sheet, columns, start, end, category, group_by):
    source = next((item for item in snapshot["sheets"] if item["name"] == sheet), None)
    if not source or source["reason"]:
        raise AppError(
            "WORKBOOK_SHEET_UNAVAILABLE",
            source["reason"] if source else "未找到可查询工作表。",
            422,
        )
    if any(columns.get(key) not in candidates for key, candidates in source["columns"].items()):
        raise AppError("WORKBOOK_COLUMN_REQUIRED", "请确认表格日期、费用类别和金额列。", 422)
    if (start is None) != (end is None) or (start and start >= end):
        raise AppError("INVALID_ARGUMENT", "请提供有效日期区间或明确查询全部日期。", 422)
    amount_column = columns["amount"]
    unit = source["amount_units"][amount_column]
    if unit and unit not in RMB_UNITS:
        raise _unit_error(sheet, f"{amount_column}{source['header_row']}")
    category = clean(category) if category is not None else None
    rows, groups = [], defaultdict(lambda: {"count": 0, "total": Decimal(0)})
    total = Decimal(0)
    for row, cells, unsupported_formats in source["rows"]:
        field = "date"
        try:
            day = _date(cells[columns[field]])
            if start is not None and not start <= day < end:
                continue
            field = "category"
            kind, label = cells[columns[field]] or (None, None)
            label = clean(label) if kind == "text" else None
            if (category is not None or group_by == "category") and not label:
                raise ValueError
            if category is not None and category != label:
                continue
            for column in source["currency_columns"]:
                currency_kind, currency = cells[column] or (None, None)
                if currency_kind != "text" or clean(currency).upper() not in RMB_UNITS:
                    raise _unit_error(sheet, f"{column}{row}")
            if amount_column in unsupported_formats:
                raise _unit_error(sheet, f"{amount_column}{row}")
            field = "amount"
            amount = _amount(cells[columns[field]])
        except ValueError as exc:
            address = f"{columns[field]}{row}"
            raise AppError(
                "WORKBOOK_CELL_INVALID",
                f"工作表「{sheet}」的 {address} 无法用于计算。",
                422,
                details={"sheet": sheet, "cell": address},
            ) from exc
        total += amount
        rows.append([row, day.isoformat(), label, format(amount, ".2f")])
        if group_by == "category":
            groups[label]["count"] += 1
            groups[label]["total"] += amount
    return {
        "parser_version": PARSER_VERSION,
        "currency": "CNY",
        "total": format(total, ".2f"),
        "count": len(rows),
        "rows": rows,
        "groups": [
            {"category": key, "count": value["count"], "total": format(value["total"], ".2f")}
            for key, value in sorted(groups.items())
        ],
        "headers": {key: source["columns"][key][column] for key, column in columns.items()},
    }
