from __future__ import annotations

import re
from typing import Any

from .schemas import (
    HardConstraints,
    QueryIntent,
    QueryOperation,
    QueryPlan,
    RetrievalTask,
)


SYSTEM_INSTRUCTION = """Bạn là query planner cho Knowledge Graph du lịch NexTripAI.
Chỉ lập kế hoạch truy xuất, không trả lời câu hỏi và tuyệt đối không sinh Cypher.
Chỉ dùng city Đà Nẵng hoặc Quy Nhơn; entity type attraction, cafe, hotel,
nightlife, restaurant; predicate thuộc schema được cung cấp. Hard constraint phải
được biểu diễn trong task, không được giấu trong terms."""


def plan_query(query: str, gemini: Any | None = None) -> tuple[QueryPlan, str, str | None]:
    deterministic = deterministic_plan(query)
    if not deterministic.clarification_needed and deterministic.confidence >= 0.94:
        return deterministic, "deterministic_fastpath", None
    if gemini is not None:
        try:
            plan = gemini.generate_structured(
                SYSTEM_INSTRUCTION,
                f"Lập query plan có cấu trúc cho câu hỏi sau:\n{query}",
                QueryPlan,
            )
            return plan, "gemini", None
        except Exception as exc:
            return deterministic, "deterministic_fallback", exc.__class__.__name__
    return deterministic, "deterministic", None


def deterministic_plan(query: str) -> QueryPlan:
    normalized = " ".join(query.strip().split())
    lowered = normalized.casefold()
    city = _city(lowered)
    entity_types = _entity_types(lowered)
    limit = _requested_limit(lowered)

    if _is_count(lowered):
        return QueryPlan(
            intent=QueryIntent.AGGREGATE_COUNT,
            city=city,
            tasks=[
                RetrievalTask(
                    operation=QueryOperation.COUNT,
                    entity_types=[entity_type],
                    limit=1,
                )
                for entity_type in entity_types
            ],
            clarification_needed=city is None or not entity_types,
            confidence=0.98,
        )

    if _is_recommendation(lowered):
        duration = _duration_days(lowered)
        if duration and not entity_types:
            entity_types = ["attraction", "restaurant", "cafe"]
        rainy = "mưa" in lowered
        return QueryPlan(
            intent=QueryIntent.RECOMMENDATION,
            city=city,
            duration_days=duration,
            tasks=[
                RetrievalTask(
                    operation=QueryOperation.RECOMMEND,
                    entity_types=entity_types,
                    hard_constraints=HardConstraints(
                        indoor=True if rainy else None,
                        weather="rain" if rainy else None,
                    ),
                    limit=limit,
                )
            ],
            clarification_needed=city is None,
            confidence=0.9,
        )

    predicates = _detail_predicates(lowered)
    if predicates:
        subject = _extract_subject(normalized)
        return QueryPlan(
            intent=QueryIntent.ENTITY_DETAIL,
            city=city,
            subjects=[subject] if subject else [],
            tasks=[
                RetrievalTask(
                    operation=QueryOperation.LOOKUP,
                    entity_types=entity_types,
                    predicates=predicates,
                    limit=limit,
                )
            ],
            clarification_needed=not bool(subject),
            confidence=0.95,
        )

    if _is_list(lowered):
        return QueryPlan(
            intent=QueryIntent.ENTITY_LIST,
            city=city,
            tasks=[
                RetrievalTask(
                    operation=QueryOperation.FILTER,
                    entity_types=entity_types,
                    limit=limit,
                )
            ],
            clarification_needed=city is None or not entity_types,
            confidence=0.94,
        )

    return QueryPlan(
        intent=QueryIntent.UNSUPPORTED,
        clarification_needed=True,
        confidence=0.35,
    )


def _city(query: str) -> str | None:
    if "đà nẵng" in query or "da nang" in query or "danang" in query:
        return "Đà Nẵng"
    if any(value in query for value in ("quy nhơn", "quy nhon", "qui nhơn", "qui nhon")):
        return "Quy Nhơn"
    return None


def _entity_types(query: str) -> list[str]:
    patterns = (
        ("hotel", ("khách sạn", "hotel")),
        ("restaurant", ("nhà hàng", "quán ăn", "restaurant")),
        ("cafe", ("cà phê", "cafe", "coffee")),
        ("nightlife", ("nightlife", "bar", "pub", "quán đêm")),
        ("attraction", ("điểm tham quan", "địa điểm tham quan", "điểm du lịch")),
    )
    matches = []
    for entity_type, words in patterns:
        positions = [query.find(word) for word in words if word in query]
        if positions:
            matches.append((min(positions), entity_type))
    return [entity_type for _, entity_type in sorted(matches)]


def _is_count(query: str) -> bool:
    return "bao nhiêu" in query and bool(_entity_types(query))


def _is_list(query: str) -> bool:
    return any(word in query for word in ("liệt kê", "danh sách", "những")) and bool(
        _entity_types(query)
    )


def _is_recommendation(query: str) -> bool:
    return any(word in query for word in ("gợi ý", "nên", "cần làm gì", "đi đâu", "chơi ở đâu"))


def _detail_predicates(query: str) -> list[str]:
    if "độ cao" in query or "cao bao nhiêu" in query:
        return ["altitude"]
    if "thành phố nào" in query:
        return ["city"]
    if "ở đâu" in query or "nằm ở" in query or "địa chỉ" in query:
        return ["address", "location"]
    if "có gì đặc biệt" in query or "thông tin" in query:
        return ["description"]
    if "mấy giờ" in query or "giờ mở cửa" in query:
        return ["opening_hours"]
    if "đánh giá" in query or "rating" in query:
        return ["rating", "review_count"]
    return []


def _extract_subject(query: str) -> str:
    patterns = (
        r"\s+ở\s+(?:quy nhơn|quy nhon|qui nhơn|qui nhon|đà nẵng|da nang|danang)\s+có địa chỉ.*$",
        r"\s+(?:nằm\s+)?ở đâu.*$",
        r"\s+ở thành phố nào.*$",
        r"\s+có độ cao bao nhiêu.*$",
        r"\s+cao bao nhiêu.*$",
        r"\s+có gì đặc biệt.*$",
        r"\s+(?:có\s+)?giờ mở cửa.*$",
        r"\s+(?:có\s+)?đánh giá.*$",
        r"\s+có địa chỉ.*$",
    )
    subject = query.strip().rstrip("?.!")
    for pattern in patterns:
        reduced = re.sub(pattern, "", subject, flags=re.IGNORECASE)
        if reduced != subject:
            return reduced.strip()
    return subject


def _requested_limit(query: str) -> int:
    match = re.search(r"\b(\d{1,2})\b", query)
    return min(max(int(match.group(1)), 1), 30) if match else 5


def _duration_days(query: str) -> int | None:
    match = re.search(r"\b(\d{1,2})\s*ngày\b", query)
    return int(match.group(1)) if match else None
