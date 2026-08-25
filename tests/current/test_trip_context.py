from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from nextrip_current.models import TripContextRequest
from nextrip_current.service import CurrentDataService


def test_trip_context_validates_every_subrequest_against_place_scope() -> None:
    with pytest.raises(ValueError, match="route leg endpoints"):
        TripContextRequest.model_validate(
            {
                "place_ids": ["place-1"],
                "route_legs": [
                    {
                        "origin_id": "place-1",
                        "destination_id": "place-2",
                        "departure_time": "2026-09-01T08:00:00+07:00",
                    }
                ],
            }
        )


def test_trip_context_keeps_a_failed_route_without_failing_place_results() -> None:
    repository = Mock()
    repository.get_places.return_value = {"place-1": None, "place-2": None}
    service = CurrentDataService(
        repository,
        clock=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    result = service.build_trip_context(
        {
            "place_ids": ["place-1", "place-2"],
            "route_legs": [
                {
                    "origin_id": "place-1",
                    "destination_id": "place-2",
                    "departure_time": "2026-09-01T08:00:00+07:00",
                }
            ],
        }
    )

    assert [item.status.value for item in result.places.items] == ["missing", "missing"]
    assert result.routes[0].status == "unavailable"
    assert result.routes[0].error_code == "TrafficIntegrationUnavailableError"
    assert result.routes[0].recommendation is None


def test_trip_context_keeps_places_when_hotel_lookup_fails() -> None:
    repository = Mock()
    repository.get_places.return_value = {"hotel-1": None}
    service = CurrentDataService(
        repository,
        clock=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    service.search_hotel_availability = Mock(side_effect=RuntimeError("offline"))

    result = service.build_trip_context(
        {
            "place_ids": ["hotel-1"],
            "hotel_search": {
                "hotel_ids": ["hotel-1"],
                "check_in": "2026-09-01",
                "stay_nights": 1,
            },
        }
    )

    assert result.places.items[0].status.value == "missing"
    assert result.hotel_availability is None
    assert result.hotel_error_code == "RuntimeError"
