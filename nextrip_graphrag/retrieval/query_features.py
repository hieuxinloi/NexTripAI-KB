from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GraphFilters:
    categories: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    indoor_or_all_weather: bool = False

    @property
    def active(self) -> bool:
        return bool(self.categories or self.terms or self.indoor_or_all_weather)


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    return plain.replace("đ", "d")


def extract_graph_filters(query: str) -> GraphFilters:
    text = _normalize(query)
    categories: list[str] = []
    terms: list[str] = []

    if "hai san" in text:
        categories.append("seafood")
    if "rooftop" in text or "san thuong" in text:
        categories.extend(["rooftop_cafe", "rooftop_bar"])
    if "lam viec" in text or "work" in text:
        terms.append("work")

    rainy = any(phrase in text for phrase in ("troi mua", "khi mua", "ngay mua", "indoor", "trong nha"))
    return GraphFilters(
        categories=categories,
        terms=terms,
        indoor_or_all_weather=rainy,
    )
