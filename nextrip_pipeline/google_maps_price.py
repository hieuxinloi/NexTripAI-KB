from __future__ import annotations

import re


_AMOUNT = r"\d+(?:[.,]\d{3})*(?:[.,]\d{1,2})?"
_CURRENCY = r"(?:[$₫€£]|VND|VNĐ|đ)"
_SEPARATOR = r"\s*(?:-|\N{EN DASH}|\N{EM DASH}|to|đến)\s*"
_MONEY = (
    rf"(?:{_CURRENCY}\s*{_AMOUNT}(?:{_SEPARATOR}{_CURRENCY}?\s*{_AMOUNT})?"
    rf"|{_AMOUNT}(?:{_SEPARATOR}{_AMOUNT})?\s*{_CURRENCY})"
)
_PRICE_PREFIX = r"(?:price(?:\s+(?:range|level))?|mức giá|khoảng giá|giá)"
_BOUND_PREFIX = r"(?:trên|từ|dưới|đến|over|from|under|up to)"
_UNIT_SUFFIX = (
    r"(?:trở lên|trở xuống|mỗi người|/người|mỗi khách|/khách|per person|per guest)"
)
_REPORT_SUFFIX = r"(?:\d+\s*(?:người|people)\s*(?:đã báo cáo|reported))"
_NUMERIC_PRICE = re.compile(
    rf"^(?:{_PRICE_PREFIX}\s*[:,]?\s*)?"
    rf"(?:{_BOUND_PREFIX}\s+)?"
    rf"{_MONEY}"
    rf"(?:\s*{_UNIT_SUFFIX})?"
    rf"(?:\s*,\s*{_REPORT_SUFFIX})?$",
    flags=re.IGNORECASE,
)

_PRICE_LEVELS = (
    ("very expensive", 4),
    ("inexpensive", 1),
    ("affordable", 1),
    ("moderate", 2),
    ("expensive", 3),
    ("rất đắt", 4),
    ("không đắt", 1),
    ("bình dân", 1),
    ("vừa phải", 2),
    ("trung bình", 2),
    ("đắt", 3),
)
_LEVEL_WORDS = "|".join(
    re.escape(label)
    for label, _ in sorted(_PRICE_LEVELS, key=lambda item: -len(item[0]))
)
_TIER_PRICE = re.compile(
    rf"^(?:{_PRICE_PREFIX}\s*[:,]?\s*)?"
    rf"(?:[$₫€£]{{1,4}}|{_LEVEL_WORDS})$",
    flags=re.IGNORECASE,
)


def google_maps_price_evidence_text(value: object) -> str | None:
    """Normalize only a complete Google price amount/range/tier label.

    The grammar is deliberately anchored. Labels for hotel booking modules,
    sponsored tours, review text, or names merely beginning with ``Giá`` do
    not become price evidence just because they contain a price-like fragment.
    """

    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    if _NUMERIC_PRICE.fullmatch(text) or _TIER_PRICE.fullmatch(text):
        return text
    return None


def google_maps_price_level(value: object) -> int | None:
    """Return only an explicit affordability tier, never infer from an amount."""

    text = google_maps_price_evidence_text(value)
    if text is None or re.search(r"\d", text):
        return None
    symbol_groups = re.findall(r"[$₫€£]+", text)
    if symbol_groups:
        return min(len(max(symbol_groups, key=len)), 4)
    lowered = text.casefold()
    return next(
        (level for label, level in _PRICE_LEVELS if label in lowered),
        None,
    )
