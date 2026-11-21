"""Recognize explicit memory instructions and their values in the current user message."""

import re
import unicodedata

from app.errors import AppError

_SPLIT = re.compile(r"([,;。！？!?\n、:])")
_LIFETIME = re.compile(r"^(?:(?:请)?(?:以后|今后|从今以后|往后|每次|长期|一直|记住|记下))+")
_CORRECTION = re.compile(
    r"^(?:请)?(?:更正|修改|改一下)(?:刚才|之前|原来|原先)?(?:的)?(?:长期)?(?:偏好|要求|设置)"
)
_NEGATED = re.compile(
    r"(?:不要|别|不用|无需|禁止|不必)(?:再|长期|帮我|将其|把它)*(?:记住|记成|记下|保存|记录|保留)"
)
_TEMPORARY = re.compile(r"^(?:只|仅)?(?:这一次|这次|此次|本次|这一轮|本轮)")
_HYPOTHETICAL = re.compile(r"^(?:如果|假如|假设|比如|例如|有人说|他(?:说|问)|她(?:说|问)|请解释)")
_DELETE = re.compile(
    r"^(?:请)?(?:删除|忘掉|不再保留)(?:刚才|之前|原来|原先|这条|该条)?(?:的)?(?:长期)?(?:偏好|记忆)$"
)
_LENGTH = re.compile(
    r"(?P<scene>正文|附录)?(?:我(?:更)?喜欢|我希望|我想要)?(?:请)?"
    r"(?:回复|回答|答复)?(?:要|请|保持|用)?"
    r"(?P<mode>简洁|简短|简明|详细|详尽|适中)(?:地)?(?:回复|回答|答复)?"
)
_LENGTH_MODES = {
    "简洁": "concise",
    "简短": "concise",
    "简明": "concise",
    "详细": "detailed",
    "详尽": "detailed",
    "适中": "moderate",
}
_LENGTH_KEYS = {"response_length", "answer_length", "response_style", "reply_style"}
_SCENE_KEYS = {"body": "body_response_length", "appendix": "appendix_response_length"}
_REPORT_NAME = r"(?:第(?:[0-9]{1,6}|[零一二三四五六七八九十]{1,4})类(?:年度)?附表|月报)"
_EXCLUSIVE_REPORT = re.compile(rf"^(?:仅|只)(?:适用于|用于|限于|适用)?({_REPORT_NAME})(?:[:：，,]|$)")
_REPORT_FORMAT = re.compile(r"^(月报)(?:输出)?格式(?:用|采用|为|[:：])")
_REPORT_DIGITS = "零一二三四五六七八九"
_REPORT_CONTINUATION = re.compile(r"^(?:请)?(?:继续|接着|按原口径|按上次口径|沿用上次口径)")


def _report_key(name):
    if name == "月报":
        return name
    index = name[1:name.index("类")]
    if index.isdigit():
        return int(index)
    if "十" in index:
        tens, _, units = index.partition("十")
        if len(tens) <= 1 and len(units) <= 1 and "十" not in units:
            return 10 * (_REPORT_DIGITS.index(tens) if tens else 1) + (
                _REPORT_DIGITS.index(units) if units else 0
            )
    return _REPORT_DIGITS.index(index) if len(index) == 1 else index


def report_scope(value):
    if not isinstance(value, str):
        return None
    if match := _EXCLUSIVE_REPORT.match(clean(value)) or _REPORT_FORMAT.match(clean(value)):
        return _report_key(match[1])
    return None


def applies_to_request(report, text):
    if report is None or text is None:
        return True
    text = clean(text)
    names = re.findall(_REPORT_NAME, text)
    return report in {_report_key(name) for name in names} or (
        report != "月报" and "附表" in text and not names
    )


def report_continuation(text):
    text = clean(text) if text else ""
    return bool(_REPORT_CONTINUATION.match(text) and not re.search(_REPORT_NAME, text))


def clean(text):
    normalized = unicodedata.normalize("NFKC", text).strip()
    return re.sub(r"[^\S\n]+", "", normalized).strip("。！!.")


def display_format(text):
    """Return dimension, scene and value for formats that contain no enterprise facts."""
    match = _LENGTH.fullmatch(clean(text))
    if match:
        scene = {"正文": "body", "附录": "appendix", None: "default"}[match["scene"]]
        return ("response_length", scene, _LENGTH_MODES[match["mode"]])
    if clean(text) == "金额保留两位小数":
        return ("money_format", "default", "two_decimals")
    if clean(text) == "先列缺项":
        return ("result_order", "default", "missing_first")
    return None


def preference_identity(key, value, signature=None):
    observed = signature or (display_format(value) if isinstance(value, str) else None)
    if observed and observed[0] == "response_length":
        if observed[1] in _SCENE_KEYS:
            return _SCENE_KEYS[observed[1]]
        if key in _LENGTH_KEYS or signature is not None:
            return "response_style"
    if key in ("response_length", "answer_length"):
        return "response_style"
    return key


def value_identity(value):
    signature = display_format(value)
    return signature[2] if signature else clean(value)


def preference_evidence(text, quotes, action, value, *, company_names=()):
    """Return the authorized format, correction signal, and directly supported literal."""
    text = clean(text)
    citations = [clean(quote) for quote in quotes]
    if action == "delete":
        if _DELETE.fullmatch(text) and text in citations:
            return (None, False, None)
        raise clarification()
    named_company = "|".join(re.escape(clean(name)) for name in company_names if clean(name))
    format_value = display_format(value)
    parts = _SPLIT.split(text)
    candidates = []
    correction = False
    durable = False
    indirect = False
    for item, separator in zip(parts[::2], [*parts[1::2], ""], strict=True):
        item = item.strip()
        sentence_end = separator in (";", "。", "？", "?", "!", "！", "\n")
        if indirect or _HYPOTHETICAL.match(item):
            indirect = not sentence_end
            durable = False
            continue
        if _NEGATED.search(item):
            candidates.clear()
            durable = False
            continue
        correction_match = _CORRECTION.match(item)
        if correction_match:
            correction = True
            item = item[correction_match.end() :]
            item = re.sub(r"^(?:为|成|改为|改成)", "", item)
        prefix = _LIFETIME.match(item)
        if prefix:
            durable = True
            item = item[prefix.end() :]
        if correction_match and item:
            durable = True
        literal = re.sub(r"^(?:同时|并且|并|也)", "", item)
        literal = re.sub(r"^(?:只限|仅限|只在|仅在|仅适用于|只适用于)(?=$)", "", literal)
        if named_company:
            literal = re.sub(
                rf"^(?:只限|仅限|只在|仅在|仅|只|在|适用于)?(?:{named_company})(?:的)?",
                "",
                literal,
            )
        if (
            durable
            and literal
            and separator not in ("？", "?")
            and not _TEMPORARY.match(literal)
            and any(literal in quote for quote in citations)
        ):
            candidates.append((literal, display_format(literal)))
        if sentence_end or _TEMPORARY.match(literal):
            durable = False
    if action == "update" and not correction:
        # A new explicitly durable value still needs to match the requested object.
        correction = True
    matches = []
    for literal, signature in candidates:
        if clean(value) == literal or (
            format_value
            and signature
            and format_value[0] == signature[0]
            and format_value[2] == signature[2]
            and format_value[1] in ("default", signature[1])
        ):
            matches.append((literal, signature))
    signatures = {signature for _, signature in matches}
    if len(signatures) != 1:
        raise clarification()
    signature = matches[0][1]
    if (
        signature
        and len({other[2] for _, other in candidates if other and other[:2] == signature[:2]}) > 1
    ):
        raise clarification()
    return signature, correction, matches[0][0]


def clarification():
    return AppError("NEEDS_CLARIFICATION", "请明确是否长期保存、偏好的准确内容和适用范围。", 422)
