from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from nextrip_current.api import create_app
from nextrip_current.config import CurrentDataSettings
from nextrip_current.errors import (
    HotelRefreshUnavailableError,
    TrafficIntegrationError,
)
from nextrip_current.models import (
    CurrentLookupStatus,
    HotelNameSource,
    HotelOfferSearchRequest,
    PlaceBatchRequest,
)
from nextrip_current.repository import CurrentDataRepository
from nextrip_current.runtime import CurrentDataServiceFactory
from nextrip_current.service import CurrentDataService
from nextrip_current.traffic import TrafficHttpClient
from nextrip_pipeline.publishing.current_price import CurrentHotelPriceSnapshot
from nextrip_pipeline.schemas import (
    CurrentPlaceProvenance,
    CurrentPlaceSnapshot,
    EntityType,
    ExternalEntityMapping,
    HotelPriceObservation,
    MappingStatus,
    Occupancy,
    OfferAvailability,
    RecordSubjectType,
    VerificationStatus,
)
from nextrip_traffic.models import TrafficRouteRequest
from tests.canonical_dataset_support import (
    CanonicalTestPlace,
    write_canonical_dataset,
)


NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    roots = (
        tmp_path / "place",
        tmp_path / "hotel_price",
        tmp_path / "trivago_mappings",
    )
    for root in roots:
        root.mkdir(parents=True)
    return roots


def _place(place_id: str = "hotel_1", name: str = "Master Hotel"):
    return CurrentPlaceSnapshot(
        place_id=place_id,
        entity_type=EntityType.HOTEL,
        city="Đà Nẵng",
        city_id="city_da_nang",
        name=name,
        provenance=CurrentPlaceProvenance(
            run_id="master-run",
            source_record_id=place_id,
            source_id="verified-master-data",
            verification_status=VerificationStatus.HUMAN_VERIFIED,
            observed_at=NOW - timedelta(days=1),
        ),
        updated_at=NOW - timedelta(days=1),
    )


def _write_place(root: Path, place_id: str = "hotel_1", name="Master Hotel"):
    snapshot = _place(place_id, name)
    (root / f"{place_id}.json").write_text(
        snapshot.model_dump_json(indent=2), encoding="utf-8"
    )
    return snapshot


def _mapping(hotel_id: str = "hotel_1"):
    return ExternalEntityMapping(
        mapping_id=f"trivago-mcp-{hotel_id}",
        entity_id=hotel_id,
        entity_type=EntityType.HOTEL,
        subject_type=RecordSubjectType.PLACE,
        source_id="trivago-mcp",
        external_id="external-1",
        external_url="https://www.trivago.vn/hotel/external-1",
        status=MappingStatus.CONFIRMED,
        matched_at=NOW - timedelta(days=2),
        verified_at=NOW - timedelta(days=1),
        attributes={
            "trivago_name": "Trivago Display Hotel",
            "hotel_name": "Fallback Hotel Name",
            "master_name": "Mapped Master Name",
        },
    )


def _write_mapping(root: Path, hotel_id="hotel_1"):
    mapping = _mapping(hotel_id)
    (root / f"{hotel_id}.json").write_text(
        mapping.model_dump_json(indent=2), encoding="utf-8"
    )
    return mapping


def _price_snapshot(
    *,
    hotel_id="hotel_1",
    check_in=date(2026, 8, 21),
    check_out=date(2026, 8, 22),
    observed_at=NOW - timedelta(minutes=10),
    stale_after=NOW + timedelta(hours=4),
    seller="Booking.com",
    occupancy=Occupancy(adults=2, children=0, rooms=1),
    children_ages=None,
    mapping_id="trivago-mcp-hotel_1",
    external_id="external-1",
):
    effective_children_ages = [] if children_ages is None else list(children_ages)
    observation = HotelPriceObservation(
        observation_id=f"observation-{hotel_id}-{observed_at.timestamp()}",
        run_id="price-run",
        hotel_id=hotel_id,
        offer_key=f"offer-{hotel_id}",
        source_record_id="source-record-1",
        source_id="trivago-mcp",
        mapping_id=mapping_id,
        external_id=external_id,
        seller=seller,
        room_type="Deluxe",
        check_in=check_in,
        check_out=check_out,
        occupancy=occupancy,
        children_ages=effective_children_ages,
        currency="VND",
        amount=Decimal("1800000"),
        nightly_amount=Decimal("1800000"),
        total_amount=Decimal("1800000"),
        availability=OfferAvailability.AVAILABLE,
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    return CurrentHotelPriceSnapshot(
        hotel_id=hotel_id,
        observation_id=observation.observation_id,
        decision_id=f"{observation.observation_id}:decision",
        observation=observation,
        updated_at=observed_at,
        stale_after=stale_after,
    )


def _write_price(root: Path, snapshot: CurrentHotelPriceSnapshot):
    path = root / f"{snapshot.observation_id}.json"
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return path


def _service(tmp_path: Path, *, refresher=None):
    place_root, price_root, mapping_root = _roots(tmp_path)
    canonical_dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [
            CanonicalTestPlace(
                place_id="hotel_1",
                name="Master Hotel",
                latitude=16.06,
                longitude=108.22,
                entity_type=EntityType.HOTEL,
            )
        ],
        generated_at=NOW - timedelta(days=1),
    )
    repository = CurrentDataRepository(
        canonical_dataset_path=canonical_dataset,
        hotel_price_root=price_root,
        trivago_mapping_root=mapping_root,
    )
    return (
        CurrentDataService(repository, hotel_refresher=refresher, clock=lambda: NOW),
        place_root,
        price_root,
        mapping_root,
    )


def _request(**updates):
    values = {
        "hotel_ids": ["hotel_1"],
        "check_in": date(2026, 8, 21),
        "check_out": date(2026, 8, 22),
        "occupancy": Occupancy(adults=2, children=0, rooms=1),
        "currency": "VND",
    }
    values.update(updates)
    return HotelOfferSearchRequest(**values)


def test_repository_readiness_cache_avoids_repeated_artifact_scan(tmp_path):
    service, _, price_root, mapping_root = _service(tmp_path)
    repository = service.repository

    first = repository.readiness()
    price_root.rmdir()
    cached = repository.readiness()
    uncached = CurrentDataRepository(
        canonical_dataset_path=repository.canonical_dataset_path,
        hotel_price_root=price_root,
        hotel_availability_root=repository.hotel_availability_root,
        trivago_mapping_root=mapping_root,
        readiness_cache_seconds=0,
    ).readiness()

    assert first.ready is True
    assert cached == first
    assert uncached.ready is False
    assert "current_hotel_price_root is not available" in uncached.issues


def test_place_get_and_batch_keep_missing_explicit(tmp_path):
    service, place_root, _, _ = _service(tmp_path)
    _write_place(place_root)

    current = service.get_place("hotel_1")
    batch = service.get_places(PlaceBatchRequest(place_ids=["hotel_1", "missing"]))

    assert current.place.name == "Master Hotel"
    assert current.stale is False
    assert [item.status for item in batch.items] == [
        CurrentLookupStatus.AVAILABLE,
        CurrentLookupStatus.MISSING,
    ]
    assert batch.items[1].current is None


def test_hotel_search_requires_exact_context_and_combines_confirmed_name(tmp_path):
    service, place_root, price_root, mapping_root = _service(tmp_path)
    _write_place(place_root)
    _write_mapping(mapping_root)
    _write_price(price_root, _price_snapshot())

    response = service.search_hotel_offers(_request())
    current_place = service.get_place("hotel_1")
    result = response.results[0]
    offer = result.offers[0]

    assert result.status is CurrentLookupStatus.AVAILABLE
    assert current_place.place.name == "Trivago Display Hotel"
    assert current_place.master_name == "Master Hotel"
    assert current_place.name_source is HotelNameSource.TRIVAGO_NAME
    assert "Master Hotel" in current_place.aliases
    assert result.identity.display_name == "Trivago Display Hotel"
    assert result.identity.master_name == "Master Hotel"
    assert result.identity.name_source is HotelNameSource.TRIVAGO_NAME
    assert result.identity.aliases == [
        "Master Hotel",
        "Mapped Master Name",
        "Fallback Hotel Name",
    ]
    assert offer.amount == Decimal("1800000")
    assert offer.stale is False
    assert offer.provenance.source_record_id == "source-record-1"
    assert offer.provenance.decision_id.endswith(":decision")
    assert str(result.identity.external_url).startswith("https://www.trivago.vn/")
    assert offer.provenance.external_id == "external-1"

    missing = service.search_hotel_offers(
        _request(check_in=date(2026, 9, 1), check_out=date(2026, 9, 2))
    )
    assert missing.results[0].status is CurrentLookupStatus.MISSING
    assert missing.results[0].offers == []


def test_offer_provenance_uses_captured_mapping_not_current_mapping(tmp_path):
    service, place_root, price_root, mapping_root = _service(tmp_path)
    _write_place(place_root)
    _write_mapping(mapping_root)
    _write_price(
        price_root,
        _price_snapshot(
            mapping_id="mapping-at-crawl-time",
            external_id="external-at-crawl-time",
        ),
    )

    offer = service.search_hotel_offers(_request()).results[0].offers[0]

    assert offer.provenance.mapping_id == "mapping-at-crawl-time"
    assert offer.provenance.external_id == "external-at-crawl-time"
    assert offer.provenance.external_url is None


def test_legacy_offer_is_read_without_inventing_mapping_provenance(tmp_path):
    service, place_root, price_root, mapping_root = _service(tmp_path)
    _write_place(place_root)
    _write_mapping(mapping_root)
    _write_price(
        price_root,
        _price_snapshot(mapping_id=None, external_id=None),
    )

    provenance = service.search_hotel_offers(_request()).results[0].offers[0].provenance

    assert provenance.mapping_id is None
    assert provenance.external_id is None
    assert provenance.external_url is None


def test_stale_offer_is_explicit_and_hidden_unless_requested(tmp_path):
    service, place_root, price_root, _ = _service(tmp_path)
    _write_place(place_root)
    _write_price(
        price_root,
        _price_snapshot(stale_after=NOW - timedelta(seconds=1)),
    )

    hidden = service.search_hotel_offers(_request())
    included = service.search_hotel_offers(_request(include_stale=True))

    assert hidden.results[0].status is CurrentLookupStatus.STALE
    assert hidden.results[0].offers == []
    assert hidden.results[0].stale_offer_count == 1
    assert hidden.results[0].latest_stale_after == NOW - timedelta(seconds=1)
    assert included.results[0].offers[0].stale is True


def test_children_ages_and_refresh_cardinality_are_validated():
    with pytest.raises(ValidationError, match="children_ages"):
        _request(
            occupancy=Occupancy(adults=2, children=1, rooms=1),
            children_ages=[],
        )
    with pytest.raises(ValidationError, match="exactly one hotel_id"):
        _request(hotel_ids=["hotel_1", "hotel_2"], refresh_if_missing=True)


def test_hotel_search_matches_children_ages_as_part_of_exact_context(tmp_path):
    service, place_root, price_root, _ = _service(tmp_path)
    _write_place(place_root)
    child_occupancy = Occupancy(adults=2, children=1, rooms=1)
    _write_price(
        price_root,
        _price_snapshot(
            occupancy=child_occupancy,
            children_ages=[7],
        ),
    )

    exact = service.search_hotel_offers(
        _request(occupancy=child_occupancy, children_ages=[7])
    )
    different_age = service.search_hotel_offers(
        _request(occupancy=child_occupancy, children_ages=[8])
    )

    assert exact.results[0].status is CurrentLookupStatus.AVAILABLE
    assert exact.results[0].offers[0].children_ages == [7]
    assert different_age.results[0].status is CurrentLookupStatus.MISSING


def test_refresh_missing_once_then_rereads_current_projection(tmp_path):
    place_root, price_root, mapping_root = _roots(tmp_path)
    _write_place(place_root)
    canonical_dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [
            CanonicalTestPlace(
                place_id="hotel_1",
                name="Master Hotel",
                latitude=16.06,
                longitude=108.22,
                entity_type=EntityType.HOTEL,
            )
        ],
    )
    calls = []

    class Refresher:
        def refresh(self, hotel_id, request):
            calls.append((hotel_id, request.check_in))
            _write_price(price_root, _price_snapshot(hotel_id=hotel_id))

    service = CurrentDataService(
        CurrentDataRepository(
            canonical_dataset_path=canonical_dataset,
            hotel_price_root=price_root,
            trivago_mapping_root=mapping_root,
        ),
        hotel_refresher=Refresher(),
        clock=lambda: NOW,
    )
    result = service.search_hotel_offers(_request(refresh_if_missing=True)).results[0]

    assert calls == [("hotel_1", date(2026, 8, 21))]
    assert result.status is CurrentLookupStatus.AVAILABLE
    assert result.refresh_attempted is True
    assert len(result.offers) == 1


def test_refresh_request_fails_explicitly_when_refresher_disabled(tmp_path):
    service, place_root, _, _ = _service(tmp_path)
    _write_place(place_root)
    with pytest.raises(HotelRefreshUnavailableError):
        service.search_hotel_offers(_request(refresh_if_missing=True))


def test_http_contract_auth_readiness_batch_limit_and_search(tmp_path):
    service, place_root, price_root, mapping_root = _service(tmp_path)
    _write_place(place_root)
    _write_mapping(mapping_root)
    _write_price(price_root, _price_snapshot())
    app = create_app(
        settings=CurrentDataSettings(
            kb_root=tmp_path,
            canonical_dataset_path=tmp_path / "canonical.json",
        ),
        service_factory=CurrentDataServiceFactory(lambda: service),
        internal_api_key="current-secret",
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").json()["status"] == "ready"
        assert client.get("/api/current/places/hotel_1").status_code == 401
        headers = {"X-NexTrip-Current-Key": "current-secret"}
        place_response = client.get("/api/current/places/hotel_1", headers=headers)
        missing_response = client.get("/api/current/places/missing", headers=headers)
        batch_too_large = client.post(
            "/api/current/places/batch",
            headers=headers,
            json={"place_ids": [f"place-{index}" for index in range(101)]},
        )
        offer_response = client.post(
            "/api/current/hotel-offers/search",
            headers=headers,
            json={
                "hotel_ids": ["hotel_1"],
                "check_in": "2026-08-21",
                "check_out": "2026-08-22",
                "occupancy": {"adults": 2, "children": 0, "rooms": 1},
                "children_ages": [],
                "currency": "VND",
            },
        )
        traffic_response = client.post(
            "/api/current/traffic/routes",
            headers=headers,
            json={"origin_id": "a", "destination_id": "b"},
        )

    assert place_response.status_code == 200
    assert missing_response.status_code == 404
    assert batch_too_large.status_code == 422
    assert offer_response.status_code == 200
    assert offer_response.json()["results"][0]["status"] == "available"
    assert traffic_response.status_code == 503
    assert traffic_response.json()["error"]["code"] == "traffic_integration_unavailable"


def test_traffic_client_sends_secret_header_and_parses_typed_response():
    captured = {}

    def handler(request: httpx.Request):
        captured["key"] = request.headers.get("X-NexTrip-Traffic-Key")
        return httpx.Response(200, json={})

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://traffic"
    )
    client = TrafficHttpClient(
        base_url="http://traffic",
        api_key=SecretStr("traffic-secret"),
        client=http_client,
    )
    request = TrafficRouteRequest(origin_id="a", destination_id="b")

    # The deliberately empty provider response proves typed validation happens;
    # the key must still be delivered only through the internal request header.
    with pytest.raises(TrafficIntegrationError):
        client.route(request)
    assert captured["key"] == "traffic-secret"
    assert "traffic-secret" not in repr(client.__dict__)


def test_runtime_refresh_gate_drops_refresher_when_disabled(tmp_path):
    from nextrip_current.runtime import build_current_data_service

    _, price_root, mapping_root = _roots(tmp_path)
    canonical_dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [
            CanonicalTestPlace(
                place_id="hotel_1",
                name="Master Hotel",
                latitude=16.06,
                longitude=108.22,
                entity_type=EntityType.HOTEL,
            )
        ],
    )

    class Refresher:
        def refresh(self, hotel_id, request):
            raise AssertionError("must not be wired while disabled")

    service = build_current_data_service(
        CurrentDataSettings(
            kb_root=tmp_path,
            canonical_dataset_path=canonical_dataset,
            current_hotel_price_root=price_root,
            current_trivago_mapping_root=mapping_root,
            trivago_refresh_enabled=False,
        ),
        hotel_refresher=Refresher(),
    )
    assert service.hotel_refresher is None


def test_runtime_keeps_enabled_refresher_and_service_closes_it(tmp_path):
    from nextrip_current.runtime import build_current_data_service

    _, price_root, mapping_root = _roots(tmp_path)
    canonical_dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [
            CanonicalTestPlace(
                place_id="hotel_1",
                name="Master Hotel",
                latitude=16.06,
                longitude=108.22,
                entity_type=EntityType.HOTEL,
            )
        ],
    )

    class Refresher:
        closed = False

        def refresh(self, hotel_id, request):
            raise AssertionError("not used")

        def close(self):
            self.closed = True

    refresher = Refresher()
    service = build_current_data_service(
        CurrentDataSettings(
            kb_root=tmp_path,
            canonical_dataset_path=canonical_dataset,
            current_hotel_price_root=price_root,
            current_trivago_mapping_root=mapping_root,
            trivago_refresh_enabled=True,
        ),
        hotel_refresher=refresher,
    )
    assert service.hotel_refresher is refresher
    service.close()
    assert refresher.closed is True
