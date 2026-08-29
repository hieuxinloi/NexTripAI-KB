from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nextrip_pipeline.schemas import (
    AccessPointRecord,
    AccessPointType,
    CityRecord,
    DailyOpeningSchedule,
    DailyOpeningStatus,
    EntityType,
    ExternalEntityMapping,
    GeoPoint,
    OfferAvailability,
    OpeningInterval,
    OpeningStatusObservation,
    MappingStatus,
    Occupancy,
    PlaceRecord,
    PriceObservation,
    ProviderRole,
    RouteObservation,
    RouteMatrixCell,
    RouteMatrixResult,
    RoutingProvider,
    RecordSubjectType,
    SourceRecord,
    SuggestedAction,
    TransportMode,
    TrafficBasis,
    ValidationResult,
    ValidationStatus,
    Weekday,
    WeeklyOpeningScheduleObservation,
)


UTC = timezone.utc


def test_source_record_requires_aware_timestamp_and_sha256() -> None:
    record = SourceRecord(
        source_record_id="source-record-1",
        run_id="run-1",
        source_id="hotel-official",
        entity_type=EntityType.HOTEL,
        crawled_at=datetime(2026, 8, 18, 5, tzinfo=UTC),
        raw_payload={"name": "Example Hotel"},
        content_hash="a" * 64,
        parser_version="1.0.0",
        source_url="https://example.com/hotel",
        http_status=200,
    )

    assert record.entity_type is EntityType.HOTEL
    assert str(record.source_url) == "https://example.com/hotel"

    invalid_payload = record.model_dump()
    invalid_payload["crawled_at"] = datetime(2026, 8, 18, 5)

    with pytest.raises(ValidationError):
        SourceRecord.model_validate(invalid_payload)


def test_source_record_supports_non_place_observations() -> None:
    record = SourceRecord(
        source_record_id="route-source-1",
        run_id="run-1",
        source_id="valhalla",
        subject_type=RecordSubjectType.ROUTE,
        subject_id="route-1",
        crawled_at=datetime(2026, 8, 18, 5, tzinfo=UTC),
        raw_payload={"trip": {"status": 0}},
        content_hash="b" * 64,
        parser_version="1.0.0",
    )

    assert record.entity_type is None
    assert record.subject_type is RecordSubjectType.ROUTE


def test_confirmed_external_mapping_requires_verification_time() -> None:
    with pytest.raises(ValidationError, match="requires verified_at"):
        ExternalEntityMapping(
            mapping_id="mapping-1",
            entity_id="hotel-1",
            entity_type=EntityType.HOTEL,
            source_id="agoda-demand-api",
            external_id="12157",
            status=MappingStatus.CONFIRMED,
            matched_at=datetime(2026, 8, 18, 5, tzinfo=UTC),
        )


def test_geo_point_rejects_invalid_coordinates() -> None:
    with pytest.raises(ValidationError):
        GeoPoint(latitude=91, longitude=108.2)


def test_place_supports_multiple_profiles_without_duplicate_identity() -> None:
    place = PlaceRecord(
        place_id="place-1",
        primary_type=EntityType.RESTAURANT,
        place_types=[EntityType.RESTAURANT, EntityType.NIGHTLIFE],
        name="Example Venue",
        normalized_name="example venue",
        source_record_ids=["source-1"],
        normalized_at=datetime(2026, 8, 18, 5, tzinfo=UTC),
    )

    assert place.entity_type is EntityType.RESTAURANT
    assert place.place_types == [EntityType.RESTAURANT, EntityType.NIGHTLIFE]


def test_city_and_access_point_are_explicit_routing_entities() -> None:
    updated_at = datetime(2026, 8, 18, 5, tzinfo=UTC)
    city = CityRecord(
        city_id="city-quy-nhon",
        name="Quy Nhơn",
        normalized_name="quy nhon",
        center=GeoPoint(latitude=13.782, longitude=109.219),
        routing_access_point_id="access-quy-nhon-center",
        updated_at=updated_at,
    )
    access_point = AccessPointRecord(
        access_point_id=city.routing_access_point_id,
        owner_entity_id=city.city_id,
        access_type=AccessPointType.CITY_CENTER,
        location=city.center,
        supported_modes=[TransportMode.DRIVE, TransportMode.TWO_WHEELER],
        updated_at=updated_at,
    )

    assert access_point.owner_entity_id == city.city_id


def test_validation_result_rejects_score_above_one() -> None:
    with pytest.raises(ValidationError):
        ValidationResult(
            validation_id="validation-1",
            run_id="run-1",
            record_id="place-1",
            validator="CoordinateValidator",
            validator_version="1.0.0",
            status=ValidationStatus.PASS,
            score=1.1,
            suggested_action=SuggestedAction.AUTO_ACCEPT,
            validated_at=datetime(2026, 8, 18, 5, tzinfo=UTC),
        )


def test_available_price_requires_valid_stay_and_amount() -> None:
    observation = PriceObservation(
        observation_id="price-1",
        run_id="run-1",
        hotel_id="hotel-1",
        offer_key="deluxe|2-adults|breakfast",
        source_record_id="source-record-1",
        room_type="Deluxe",
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 21),
        occupancy=Occupancy(adults=2, rooms=1),
        amount=Decimal("1350000"),
        availability=OfferAvailability.AVAILABLE,
        observed_at=datetime(2026, 8, 18, 5, tzinfo=UTC),
    )

    assert observation.currency == "VND"
    assert observation.amount == Decimal("1350000")

    with pytest.raises(ValidationError):
        PriceObservation(
            observation_id="price-2",
            run_id="run-1",
            hotel_id="hotel-1",
            offer_key="deluxe|2-adults|breakfast",
            source_record_id="source-record-1",
            room_type="Deluxe",
            check_in=date(2026, 8, 21),
            check_out=date(2026, 8, 20),
            occupancy=Occupancy(adults=2, rooms=1),
            availability=OfferAvailability.AVAILABLE,
            observed_at=datetime(2026, 8, 18, 5, tzinfo=UTC),
        )


def test_hotel_occupancy_has_overrideable_defaults() -> None:
    default_occupancy = Occupancy()
    occupancy = Occupancy(adults=1, children=1, rooms=1)

    assert default_occupancy.adults == 2
    assert default_occupancy.children == 0
    assert default_occupancy.rooms == 1
    assert occupancy.adults == 1
    assert occupancy.children == 1


def test_open_today_requires_hours_or_24_hour_flag() -> None:
    observation = OpeningStatusObservation(
        observation_id="opening-1",
        run_id="run-1",
        place_id="place-1",
        source_record_ids=["source-record-1"],
        local_date=date(2026, 8, 18),
        status=DailyOpeningStatus.OPEN_TODAY,
        opening_intervals=[
            OpeningInterval(opens_at=time(7), closes_at=time(22)),
        ],
        observed_at=datetime(2026, 8, 18, 1, tzinfo=UTC),
    )

    assert observation.timezone == "Asia/Ho_Chi_Minh"

    with pytest.raises(ValidationError):
        OpeningStatusObservation(
            observation_id="opening-2",
            run_id="run-1",
            place_id="place-1",
            source_record_ids=["source-record-1"],
            local_date=date(2026, 8, 18),
            status=DailyOpeningStatus.OPEN_TODAY,
            observed_at=datetime(2026, 8, 18, 1, tzinfo=UTC),
        )


def test_weekly_schedule_rejects_duplicate_days() -> None:
    with pytest.raises(ValidationError, match="duplicate days"):
        WeeklyOpeningScheduleObservation(
            observation_id="schedule-1",
            run_id="run-1",
            place_id="place-1",
            source_record_ids=["source-1"],
            days=[
                DailyOpeningSchedule(day=Weekday.MONDAY, closed=True),
                DailyOpeningSchedule(day=Weekday.MONDAY, open_24_hours=True),
            ],
            observed_at=datetime(2026, 8, 18, 1, tzinfo=UTC),
        )


def test_route_observation_requires_geometry_and_fallback_reason() -> None:
    observed_at = datetime(2026, 8, 18, 10, tzinfo=UTC)
    route = RouteObservation(
        observation_id="route-1",
        request_id="request-1",
        origin_access_point_id="access-1",
        destination_access_point_id="access-2",
        mode=TransportMode.TWO_WHEELER,
        provider=RoutingProvider.HERE,
        provider_role=ProviderRole.PRIMARY,
        distance_meters=7200,
        duration_seconds=1080,
        encoded_polyline="encoded-route",
        departure_time=observed_at,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(minutes=10),
    )

    assert route.provider is RoutingProvider.HERE

    with pytest.raises(ValidationError):
        RouteObservation(
            observation_id="route-2",
            request_id="request-2",
            origin_access_point_id="access-1",
            destination_access_point_id="access-2",
            mode=TransportMode.DRIVE,
            provider=RoutingProvider.GOOGLE,
            provider_role=ProviderRole.FALLBACK,
            distance_meters=8100,
            duration_seconds=1500,
            encoded_polyline="encoded-route",
            departure_time=observed_at,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=10),
        )


def test_valhalla_route_matrix_models_road_network_costs() -> None:
    observed_at = datetime(2026, 8, 18, 10, tzinfo=UTC)
    matrix = RouteMatrixResult(
        matrix_id="matrix-1",
        request_id="request-matrix-1",
        provider=RoutingProvider.VALHALLA,
        provider_role=ProviderRole.PRIMARY,
        mode=TransportMode.DRIVE,
        origin_access_point_ids=["quy-nhon-center"],
        destination_access_point_ids=["da-nang-center"],
        cells=[
            RouteMatrixCell(
                origin_index=0,
                destination_index=0,
                distance_meters=322000,
                duration_seconds=19800,
            )
        ],
        traffic_basis=TrafficBasis.FREE_FLOW,
        departure_time=observed_at,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=24),
    )

    assert matrix.cells[0].distance_meters == 322000
