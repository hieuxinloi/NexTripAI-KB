from __future__ import annotations

import pytest

from nextrip_graphrag.normalizer import read_processed
from nextrip_graphrag.versions.v3.graph_store import _facet_is_supported
from nextrip_graphrag.versions.v3.ontology import claims_open_24h, extract_facts
from nextrip_graphrag.versions.v3.query_planner import deterministic_plan
from nextrip_graphrag.versions.v3.retrieval import V3RetrievalService
from nextrip_graphrag.versions.v3.schemas import V3Filters
from nextrip_graphrag.versions.v2.retrieval import _fulltext_query


@pytest.mark.parametrize(
    ("query", "intent", "predicates"),
    [
        ("Khách sạn Novotel Đà Nẵng có bao nhiêu sao?", "entity_detail", ["star_rating"]),
        ("Giá phòng khách sạn InterContinental Đà Nẵng?", "entity_detail", ["price_min", "price_max", "price"]),
        ("Giờ mở cửa nhà hàng Bé Mặn?", "entity_detail", ["opening_hours"]),
        ("Ngũ Hành Sơn mở cửa lúc mấy giờ?", "entity_detail", ["opening_hours"]),
        ("Thời gian tham quan Ngũ Hành Sơn mất bao lâu?", "entity_detail", ["duration"]),
        ("Giờ mở cửa Sky36 Bar?", "entity_detail", ["opening_hours"]),
        ("Cầu Rồng có gì đặc biệt?", "entity_detail", ["highlights", "description"]),
        ("Ghềnh Ráng Tiên Sa ở thành phố nào?", "entity_detail", ["city"]),
        ("Bà Nà Hills có độ cao bao nhiêu?", "entity_detail", ["altitude"]),
    ],
)
def test_v3_detail_plans(query: str, intent: str, predicates: list[str]) -> None:
    plan = deterministic_plan(query)

    assert plan.intent == intent
    assert plan.tasks[0].predicates == predicates


def test_v3_near_filter_plan() -> None:
    plan = deterministic_plan("Khách sạn nào gần bãi biển Mỹ Khê?")

    assert plan.intent == "entity_list"
    assert plan.tasks[0].entity_types == ["hotel"]
    assert plan.tasks[0].filters.near_subject == "bãi biển mỹ khê"


def test_v3_faceted_list_plans() -> None:
    five_star = deterministic_plan("Có khách sạn 5 sao nào ở Quy Nhơn?")
    vegetarian = deterministic_plan("Có nhà hàng chay nào ở Quy Nhơn?")
    rooftop = deterministic_plan("Có quán cafe rooftop nào ở Quy Nhơn?")
    pub = deterministic_plan("Có pub nào ở Quy Nhơn không?")

    assert five_star.tasks[0].filters.star_rating == 5
    assert vegetarian.tasks[0].filters.cuisine == "vegetarian"
    assert rooftop.tasks[0].filters.category == "rooftop_cafe"
    assert pub.tasks[0].filters.venue_type == "pub"


def test_v3_recommendation_uses_recommend_operation() -> None:
    plan = deterministic_plan("Gợi ý quán cafe yên tĩnh để làm việc ở Đà Nẵng")

    assert plan.intent == "recommendation"
    assert plan.tasks[0].operation == "recommend"
    assert plan.tasks[0].filters.category == "work_cafe"


def test_v3_rain_recommendation_requires_verified_indoor_places() -> None:
    plan = deterministic_plan("Trời mưa thì nên đi đâu ở Quy Nhơn?")

    assert plan.intent == "recommendation"
    assert plan.tasks[0].entity_types == ["attraction"]
    assert plan.tasks[0].filters.indoor is True
    assert plan.tasks[0].filters.weather == "rain"


def test_v3_rain_filter_compiles_to_indoor_or_all_weather_cypher() -> None:
    class CapturingStore:
        def __init__(self) -> None:
            self.query = ""

        def run(self, query, **params):
            self.query = query
            return []

    store = CapturingStore()
    V3RetrievalService(store)._filter_v3(
        "Quy Nhơn",
        ["attraction"],
        V3Filters(indoor=True, weather="rain"),
        5,
    )

    assert "place.is_indoor = true" in store.query
    assert "'all_weather' IN coalesce(place.weather_suitable, [])" in store.query


@pytest.mark.parametrize(
    "query",
    [
        "Sân bay Đà Nẵng cách trung tâm bao xa?",
        "Có thể thuê xe máy ở đâu tại Đà Nẵng?",
        "Khoảng cách Đà Nẵng - Quy Nhơn?",
        "Grab có hoạt động ở Quy Nhơn không?",
    ],
)
def test_v3_transport_queries_do_not_fuzzy_anchor_to_places(query: str) -> None:
    assert deterministic_plan(query).intent == "unsupported"


def test_v3_normalizes_entity_subjects_before_anchor_search() -> None:
    tower = deterministic_plan("Tháp Đôi Quy Nhơn được xây dựng từ khi nào?")
    restaurant = deterministic_plan("Nhà hàng Trần có địa chỉ ở đâu?")
    cable_car = deterministic_plan("Có cáp treo ở Bà Nà Hills không?")
    distance = deterministic_plan("Khoảng cách từ trung tâm Đà Nẵng đến Bà Nà?")

    assert tower.subjects == ["Tháp Đôi"]
    assert restaurant.subjects == ["Trần"]
    assert cable_car.subjects == ["Bà Nà Hills"]
    assert distance.subjects == ["Bà Nà"]
    assert tower.tasks[0].predicates == ["construction_period"]
    assert cable_car.tasks[0].predicates == ["cable_car"]
    assert distance.tasks[0].predicates == ["distance_to_city_center_geo"]


def test_v3_derives_city_center_distance_from_verified_coordinates() -> None:
    places = {place["id"]: place for place in read_processed("processed_verified")["places"]}
    facts = {fact["predicate"]: fact for fact in extract_facts(places["attr_dn_067"])}

    assert facts["distance_to_city_center_geo"]["unit"] == "km"
    assert 20 < facts["distance_to_city_center_geo"]["value"] < 30


def test_fulltext_anchor_sanitizes_lucene_operators() -> None:
    assert _fulltext_query("Sài Gòn - Đà Nẵng?") == "Sài Gòn Đà Nẵng"


def test_v3_ontology_promotes_existing_properties_to_facts() -> None:
    places = {place["id"]: place for place in read_processed("processed_verified")["places"]}
    hotel_facts = {fact["predicate"] for fact in extract_facts(places["hotel_qn_001"])}
    restaurant_facts = {fact["predicate"] for fact in extract_facts(places["rest_dn_033"])}

    assert {"check_in_time", "check_out_time", "distance_to_beach", "amenities"} <= hotel_facts
    assert {"cuisine", "signature_dishes", "serves"} <= restaurant_facts


def test_v3_only_promotes_supported_dish_claims_to_facets() -> None:
    places = {place["id"]: place for place in read_processed("processed_verified")["places"]}

    assert not _facet_is_supported(places["rest_qn_021"], "signature_dishes", "Bánh xèo")
    assert _facet_is_supported(places["rest_qn_056"], "signature_dishes", "bánh xèo")


def test_v3_distinguishes_opening_claim_from_always_on_amenity() -> None:
    places = {place["id"]: place for place in read_processed("processed_verified")["places"]}

    assert claims_open_24h(places["cafe_dn_061"]["props"])
    assert claims_open_24h(places["cafe_dn_062"]["props"])
    assert not claims_open_24h(places["cafe_dn_063"]["props"])
