from __future__ import annotations

import re
from typing import Any

from ..v2.query_planner import SYSTEM_INSTRUCTION as BASE_SYSTEM_INSTRUCTION
from ..v2.query_planner import _city, _entity_types, _requested_limit
from ..v2.schemas import QueryIntent, QueryOperation
from .schemas import V3Filters, V3QueryPlan, V3RetrievalTask


SYSTEM_INSTRUCTION = BASE_SYSTEM_INSTRUCTION + """
V3 hỗ trợ thêm typed filters: star_rating, amenity, cuisine, dish, feature,
venue_type, ambience, tag, category, near_subject, open_24h và budget_max.
Không được biến câu hỏi thuộc tính như 'bao nhiêu sao' thành aggregate count.
Không sinh Cypher. Nếu domain là flight, taxi, train, bus hoặc weather lịch sử,
hãy trả unsupported thay vì đoán từ Place graph."""


def plan_query(query: str, gemini: Any | None = None) -> tuple[V3QueryPlan, str, str | None]:
    deterministic = deterministic_plan(query)
    if deterministic.intent != QueryIntent.UNSUPPORTED and deterministic.confidence >= 0.92:
        return deterministic, "deterministic_fastpath", None
    if gemini is not None:
        try:
            plan = gemini.generate_structured(
                SYSTEM_INSTRUCTION,
                f"Lập V3 query plan có cấu trúc cho câu hỏi:\n{query}",
                V3QueryPlan,
            )
            return plan, "gemini", None
        except Exception as exc:
            return deterministic, "deterministic_fallback", exc.__class__.__name__
    return deterministic, "deterministic", None


def deterministic_plan(query: str) -> V3QueryPlan:
    normalized = " ".join(query.strip().split())
    lowered = normalized.casefold()
    city = _city(lowered)
    entity_types = _entity_types(lowered)
    limit = _requested_limit(lowered)
    filters = _filters(lowered)
    if not entity_types:
        if filters.cuisine or filters.dish:
            entity_types = ["restaurant"]
        elif filters.venue_type:
            entity_types = ["nightlife"]
        elif filters.category in {"rooftop_cafe", "work_cafe"}:
            entity_types = ["cafe"]

    if _is_transport_or_external(lowered):
        return V3QueryPlan(
            intent=QueryIntent.UNSUPPORTED,
            clarification_needed=False,
            confidence=0.98,
        )

    detail = _detail_plan(normalized, lowered, city, entity_types, limit)
    if detail:
        return detail

    if _is_true_count(lowered, entity_types):
        return V3QueryPlan(
            intent=QueryIntent.AGGREGATE_COUNT,
            city=city,
            tasks=[
                V3RetrievalTask(
                    operation=QueryOperation.COUNT,
                    entity_types=[entity_type],
                    limit=1,
                )
                for entity_type in entity_types
            ],
            clarification_needed=city is None or not entity_types,
            confidence=0.98,
        )

    if any(word in lowered for word in ("ngon nhất", "đẹp nhất", "nổi tiếng", "gợi ý", "nên")):
        return V3QueryPlan(
            intent=QueryIntent.RECOMMENDATION,
            city=city,
            tasks=[
                V3RetrievalTask(
                    operation=QueryOperation.RECOMMEND,
                    entity_types=entity_types,
                    filters=filters,
                    terms=_terms(lowered),
                    limit=limit,
                )
            ],
            clarification_needed=city is None,
            confidence=0.93,
        )

    if _is_list_query(lowered, entity_types) or entity_types and _has_active_filters(filters):
        return V3QueryPlan(
            intent=QueryIntent.ENTITY_LIST,
            city=city,
            tasks=[
                V3RetrievalTask(
                    operation=QueryOperation.FILTER,
                    entity_types=entity_types,
                    filters=filters,
                    terms=_terms(lowered),
                    limit=limit,
                )
            ],
            clarification_needed=(city is None and filters.near_subject is None) or not entity_types,
            confidence=0.95,
        )

    return V3QueryPlan(
        intent=QueryIntent.UNSUPPORTED,
        clarification_needed=False,
        confidence=0.4,
    )


def _detail_plan(
    query: str,
    lowered: str,
    city: str | None,
    entity_types: list[str],
    limit: int,
) -> V3QueryPlan | None:
    predicate_patterns = (
        (("bao nhiêu sao", "mấy sao"), ["star_rating"]),
        (("giá phòng",), ["price_min", "price_max", "price"]),
        (("giá vé", "vé vào cửa"), ["price_min", "price_max", "price"]),
        (("địa chỉ", "ở đâu", "nằm ở"), ["address", "location"]),
        (("giờ mở cửa", "mở cửa lúc", "mở cửa mấy giờ"), ["opening_hours"]),
        (("tiện ích", "hồ bơi"), ["amenities", "features"]),
        (("số điện thoại", "liên hệ"), ["phone"]),
        (("check-in", "check in"), ["check_in_time"]),
        (("check-out", "check out"), ["check_out_time"]),
        (("phục vụ món",), ["cuisine", "signature_dishes"]),
        (("mất bao lâu", "thời gian tham quan"), ["duration"]),
        (("cách trung tâm",), ["distance_to_city_center_geo"]),
        (("bao xa", "khoảng cách"), ["distance_to_city_center_geo"]),
        (("ở thành phố nào",), ["city"]),
        (("độ cao",), ["altitude"]),
        (("có gì đặc biệt",), ["highlights", "description"]),
        (("hoạt động gì", "có những hoạt động"), ["features", "highlights", "description"]),
        (("được xây dựng", "từ khi nào"), ["construction_period"]),
        (("unesco",), ["unesco_status"]),
        (("đặt tour",), ["booking_advice"]),
        (("cáp treo",), ["cable_car"]),
        (("chi nhánh",), ["branch_info", "address"]),
        (("có hoạt động",), ["availability_status"]),
    )
    for markers, predicates in predicate_patterns:
        if any(marker in lowered for marker in markers):
            subject = _subject(query, markers)
            subject = _without_city_suffix(subject, city)
            if not subject or _looks_generic(subject):
                return None
            return V3QueryPlan(
                intent=QueryIntent.ENTITY_DETAIL,
                city=city,
                subjects=[subject],
                tasks=[
                    V3RetrievalTask(
                        operation=QueryOperation.LOOKUP,
                        entity_types=entity_types,
                        predicates=predicates,
                        filters=_filters(lowered),
                        limit=limit,
                    )
                ],
                confidence=0.96,
            )
    return None


def _subject(query: str, markers: tuple[str, ...]) -> str:
    subject = query.strip().rstrip("?.!")
    prefixes = (
        r"^(?:khoảng cách\s+)?từ\s+.+?\s+đến\s+",
        r"^(?:có\s+)?cáp treo\s+(?:ở|tại)\s+",
        r"^(?:giá phòng|giá vé(?: vào cửa)?|địa chỉ|giờ mở cửa|số điện thoại(?: liên hệ)?|thời gian tham quan)\s+",
        r"^(?:khách sạn|nhà hàng|quán|đảo|bãi biển)\s+",
    )
    for prefix in prefixes:
        subject = re.sub(prefix, "", subject, flags=re.IGNORECASE)
    cuts = (
        r"\s+(?:có\s+)?bao nhiêu sao.*$",
        r"\s+(?:có\s+)?mấy sao.*$",
        r"\s+có những tiện ích.*$",
        r"\s+có hồ bơi.*$",
        r"\s+phục vụ món gì.*$",
        r"\s+có địa chỉ.*$",
        r"\s+(?:nằm\s+)?ở đâu.*$",
        r"\s+mở cửa.*$",
        r"\s+được xây dựng.*$",
        r"\s+có cần đặt tour.*$",
        r"\s+có chi nhánh.*$",
        r"\s+có cáp treo.*$",
        r"\s+cách .*?$",
        r"\s+mất bao lâu.*$",
        r"\s+có những hoạt động.*$",
        r"\s+ở thành phố nào.*$",
        r"\s+có độ cao.*$",
        r"\s+độ cao.*$",
        r"\s+có gì đặc biệt.*$",
        r"\s+có được unesco.*$",
        r"\s+không$",
    )
    for cut in cuts:
        reduced = re.sub(cut, "", subject, flags=re.IGNORECASE)
        if reduced != subject:
            subject = reduced
            break
    return subject.strip()


def _without_city_suffix(subject: str, city: str | None) -> str:
    if city is None:
        return subject
    return re.sub(rf"\s+{re.escape(city)}$", "", subject, flags=re.IGNORECASE).strip()


def _filters(query: str) -> V3Filters:
    star = re.search(r"\b([1-5])\s*sao\b", query)
    return V3Filters(
        star_rating=int(star.group(1)) if star else None,
        amenity="pool" if "hồ bơi" in query else None,
        cuisine="seafood" if "hải sản" in query else "vegetarian" if "chay" in query else None,
        dish="bánh xèo" if "bánh xèo" in query else None,
        venue_type="pub" if "pub" in query else "bar" if re.search(r"\bbar\b", query) else None,
        category="rooftop_cafe" if "rooftop" in query and "cafe" in query else "work_cafe" if "làm việc" in query else None,
        ambience="sea_view" if "view biển" in query else None,
        tag="sea_view" if "view biển" in query else None,
        near_subject=_near_subject(query),
        open_24h=True if "24/7" in query or "24h" in query else None,
    )


def _terms(query: str) -> list[str]:
    terms = []
    for value in ("hải sản", "chay", "bánh xèo", "view biển", "rooftop", "yên tĩnh", "làm việc"):
        if value in query:
            terms.append(value)
    return terms


def _near_subject(query: str) -> str | None:
    match = re.search(r"gần\s+(.+?)(?:\?|$)", query, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _is_true_count(query: str, entity_types: list[str]) -> bool:
    return "bao nhiêu" in query and bool(entity_types) and not any(
        marker in query for marker in ("bao nhiêu sao", "giá bao nhiêu", "mất bao nhiêu")
    )


def _is_list_query(query: str, entity_types: list[str]) -> bool:
    return bool(entity_types) and any(
        marker in query
        for marker in ("nào", "danh sách", "liệt kê", "có pub", "có quán", "đặc sản")
    )


def _has_active_filters(filters: V3Filters) -> bool:
    return any(value is not None for value in filters.model_dump().values())


def _is_transport_or_external(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "sân bay",
            "máy bay",
            "tàu hỏa",
            "taxi",
            "xe bus",
            "thuê xe máy",
            "grab",
            "thời gian bay",
        )
    ) or bool(
        re.search(
            r"khoảng cách.*(?:đà nẵng.*quy nhơn|quy nhơn.*đà nẵng)",
            query,
        )
    )


def _looks_generic(subject: str) -> bool:
    lowered = subject.casefold()
    return any(
        lowered.startswith(prefix)
        for prefix in ("check-in khách sạn thường", "giá trung bình", "khoảng cách đà nẵng")
    )
