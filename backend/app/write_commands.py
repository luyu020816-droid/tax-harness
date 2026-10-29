"""A small authorization grammar, not a replacement for model intent selection."""

import re
import unicodedata

FIELD = r"用途|备注"
VERB = r"(?:填写为|填写成|改为|改成|补为|补成|设置为|设为|填为|填成|补填为|填写|补填|填|补|为)"
ASSIGNMENT = re.compile(rf"(?P<field>{FIELD})\s*(?:{VERB})?\s*(?P<value>.+)", re.S)
PROHIBITED = re.compile(
    r"^(?:请)?(?:我(?:只是)?想(?:问|知道|了解)|只(?:需要)?(?:解释|说明|讨论)|仅(?:解释|讨论)|不要(?:执行|保存|修改)|别(?:执行|保存|修改)|假如|假设|如果|上次|以前|有人说)"
)


def _value(text):
    text = text.strip()
    for opening, closing in [("“", "”"), ("「", "」"), ('"', '"'), ("'", "'")]:
        if text.startswith(opening) and text.endswith(closing):
            return text[1:-1]
    return text


def authorize_direct(text, aliases, invoice_id, patch, selection_ref=None):
    """Return authorized/needs_confirmation/prohibited for this exact patch."""
    text = unicodedata.normalize("NFC", text).strip().rstrip("。！!.")
    # Exclude literal assigned values before checking the command envelope.
    # Otherwise a legitimate purpose such as “不要自动付款” looks like a denial.
    envelope = text
    for key, value in patch.items():
        if value:
            field = "用途" if key == "purpose" else "备注"
            literal = re.compile(rf"({field}\s*(?:{VERB})?\s*[“「\"\']?){re.escape(value)}")
            envelope = literal.sub(lambda m: m[1] + "【字段值】", envelope)
    if re.search(
        r"(?:不要|先别|别|禁止|不)(?:执行|保存|修改|写入|授权|清空|清除|删除)|只(?:用于|作)?(?:解释|讨论)|假如|假设",
        envelope,
    ):
        return "prohibited"
    if any(
        text.startswith(a) and text.endswith(b) for a, b in [("“", "”"), ("「", "」"), ('"', '"')]
    ):
        return "prohibited"
    if PROHIBITED.match(text):
        return "prohibited"
    parts = re.split(r"[；;]", text)
    if len(parts) > 1:
        companies = "|".join(re.escape(a) for a in aliases)
        read_prefix = rf"(?:检查|查询)(?:{companies})\s*\d{{4}}(?:年|-)\d{{1,2}}月?(?:用途|用途缺项|资料|发票)"
        if not all(re.fullmatch(read_prefix, part.strip()) for part in parts[:-1]):
            return "needs_confirmation"
        text = parts[-1].strip()
    text = re.sub(r"^(?:请|请帮我|帮我|再|先|只|把)+\s*", "", text)
    text = re.sub(r"^填(?=F[0-9])", "", text)
    for alias in sorted(aliases, key=len, reverse=True):
        if text.startswith(alias):
            text = text[len(alias) :].lstrip()
            break
    text = re.sub(r"^(?:的)?(?:发票)?\s*", "", text)
    if text.startswith(invoice_id):
        text = text[len(invoice_id) :]
    elif selection_ref:
        rank = re.match(r"第([一二三四五六七八九十]|[0-9]+)张", text)
        if not rank:
            return "needs_confirmation"
        numbers = {c: n for n, c in enumerate("一二三四五六七八九十", 1)}
        index = numbers.get(rank[1], int(rank[1]) if rank[1].isdigit() else 0)
        if index != selection_ref.get("row_index"):
            return "needs_confirmation"
        text = text[rank.end() :]
    else:
        return "needs_confirmation"
    text = text.lstrip(" 的")
    # Explicitly recognized remainder says other invoices are not authorized.
    text = re.sub(r"[，,；;]\s*其余(?:还)?(?:没|未)确认[。！!]*$", "", text)
    assignments = re.split(r"[，,；;]\s*(?=(?:用途|备注))", text)
    parsed = {}
    for item in assignments:
        match = ASSIGNMENT.fullmatch(item.strip())
        if not match:
            return "needs_confirmation"
        key = "purpose" if match["field"] == "用途" else "note"
        if key in parsed:
            return "needs_confirmation"
        parsed[key] = _value(match["value"])
    # Null/clear commands go through explicit confirmation in this grammar version.
    return (
        "authorized"
        if parsed == patch and all(v is not None for v in patch.values())
        else "needs_confirmation"
    )
