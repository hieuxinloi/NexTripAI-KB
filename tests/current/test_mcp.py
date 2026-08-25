from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nextrip_current import mcp


class FakeMCPServer:
    instances: list[FakeMCPServer] = []

    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: dict[str, Any] = {}
        self.transport: str | None = None
        self.instances.append(self)

    def tool(self, *, name: str, **_: Any) -> Any:
        def register(function: Any) -> Any:
            self.tools[name] = function
            return function

        return register

    def run(self, *, transport: str) -> None:
        self.transport = transport


class DumpableResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.payload


class FakeCurrentDataService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    def get_place(self, place_id: str) -> DumpableResponse:
        self.calls.append(("get_place", place_id))
        return DumpableResponse({"place_id": place_id, "name": "Ky Co"})

    def get_places(self, request: Any) -> list[dict[str, str]]:
        self.calls.append(("get_places", request))
        return [{"place_id": place_id} for place_id in request.place_ids]

    def search_hotel_offers(self, request: Any) -> dict[str, Any]:
        self.calls.append(("search_hotel_offers", request))
        return {"offers": [], "context": request.model_dump(mode="json")}

    def search_hotel_availability(self, request: Any) -> dict[str, Any]:
        self.calls.append(("search_hotel_availability", request))
        return {"windows": [], "context": request.model_dump(mode="json")}

    def route(self, request: Any) -> dict[str, Any]:
        self.calls.append(("route", request))
        return {"distance_m": 1200, "request": request.model_dump(mode="json")}

    def recommend_transport(self, request: Any) -> dict[str, Any]:
        self.calls.append(("recommend_transport", request))
        return {
            "recommended_mode": "walk",
            "request": request.model_dump(mode="json"),
        }

    def build_trip_context(self, request: Any) -> dict[str, Any]:
        self.calls.append(("build_trip_context", request))
        return {
            "place_ids": request.place_ids,
            "route_count": len(request.route_legs),
        }

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def clear_fake_servers() -> None:
    FakeMCPServer.instances.clear()


def test_factory_registers_current_data_and_traffic_tools() -> None:
    service = FakeCurrentDataService()

    server = mcp.create_mcp_server(service, server_factory=FakeMCPServer)

    assert server.name == mcp.MCP_SERVER_NAME
    assert set(server.tools) == {
        "get_current_place",
        "get_current_places",
        "search_current_hotel_offers",
        "search_hotel_availability",
        "calculate_route",
        "recommend_transport",
        "build_trip_context",
    }

    assert server.tools["get_current_place"]("place-1") == {
        "place_id": "place-1",
        "name": "Ky Co",
    }
    assert server.tools["get_current_places"](["place-1", "place-2"]) == {
        "result": [{"place_id": "place-1"}, {"place_id": "place-2"}]
    }

    hotel_request = {
        "hotel_ids": ["hotel-1"],
        "check_in": "2026-09-01",
        "check_out": "2026-09-02",
        "refresh_if_missing": True,
    }
    route_payload = {"origin_id": "place-1", "destination_id": "place-2"}
    recommendation_payload = {**route_payload, "objective": "balanced"}

    assert server.tools["search_current_hotel_offers"](hotel_request) == {
        "offers": [],
        "context": {
            **hotel_request,
            "stay_nights": 1,
            "stay_days": 2,
            "lookahead_days": 1,
            "occupancy": {"adults": 2, "children": 0, "rooms": 1},
            "children_ages": [],
            "currency": None,
            "seller": None,
            "include_stale": False,
        },
    }
    availability_result = server.tools["search_hotel_availability"](hotel_request)
    assert availability_result["windows"] == []
    assert availability_result["context"]["stay_nights"] == 1
    route_result = server.tools["calculate_route"](route_payload)
    assert route_result["distance_m"] == 1200
    assert route_result["request"]["origin_id"] == "place-1"
    assert route_result["request"]["destination_id"] == "place-2"
    recommendation_result = server.tools["recommend_transport"](recommendation_payload)
    assert recommendation_result["recommended_mode"] == "walk"
    assert recommendation_result["request"]["objective"] == "balanced"
    context_result = server.tools["build_trip_context"](
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
    assert context_result == {
        "place_ids": ["place-1", "place-2"],
        "route_count": 1,
    }

    assert service.calls[0] == ("get_place", "place-1")
    assert service.calls[1][0] == "get_places"
    assert service.calls[1][1].place_ids == ["place-1", "place-2"]
    assert service.calls[2][0] == "search_hotel_offers"
    assert service.calls[2][1].refresh_if_missing is True
    assert service.calls[3][0] == "search_hotel_availability"
    assert service.calls[4][0] == "route"
    assert service.calls[4][1].origin_id == "place-1"
    assert service.calls[5][0] == "recommend_transport"
    assert service.calls[5][1].objective.value == "balanced"
    assert service.calls[6][0] == "build_trip_context"


def test_model_validation_happens_before_service_delegation() -> None:
    service = FakeCurrentDataService()
    server = mcp.create_mcp_server(service, server_factory=FakeMCPServer)

    with pytest.raises(ValueError):
        server.tools["calculate_route"](
            {"origin_id": "same-place", "destination_id": "same-place"}
        )

    assert service.calls == []


def test_official_sdk_in_memory_call_returns_structured_content() -> None:
    sdk = pytest.importorskip("mcp")
    sdk_server = pytest.importorskip("mcp.server")
    server = mcp.create_mcp_server(FakeCurrentDataService())

    async def call_tool() -> tuple[Any, Any]:
        async with sdk.Client(server) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "get_current_place",
                {"place_id": "place-1"},
            )
            return tools, result

    assert isinstance(server, sdk_server.MCPServer)
    tools, result = asyncio.run(call_tool())
    assert result.is_error is False
    assert result.structured_content == {
        "place_id": "place-1",
        "name": "Ky Co",
    }
    schemas = {tool.name: tool.input_schema for tool in tools.tools}
    hotel_schema = schemas["search_current_hotel_offers"]
    availability_schema = schemas["search_hotel_availability"]
    route_schema = schemas["calculate_route"]
    recommendation_schema = schemas["recommend_transport"]
    context_schema = schemas["build_trip_context"]
    assert "HotelOfferSearchRequest" in hotel_schema["$defs"]
    assert "HotelAvailabilitySearchRequest" in availability_schema["$defs"]
    assert "TrafficRouteRequest" in route_schema["$defs"]
    assert "TransportRecommendationRequest" in recommendation_schema["$defs"]
    assert "TripContextRequest" in context_schema["$defs"]


def test_scalar_service_response_is_rejected() -> None:
    with pytest.raises(TypeError, match="must return"):
        mcp._as_structured_dict("not structured")


def test_cli_uses_selected_transport_and_closes_service(monkeypatch: Any) -> None:
    service = FakeCurrentDataService()
    monkeypatch.setattr(mcp, "_load_mcp_server_type", lambda: FakeMCPServer)
    monkeypatch.setattr(mcp, "_build_service", lambda: service)

    assert mcp.main(["--transport", "streamable-http"]) == 0

    assert FakeMCPServer.instances[-1].transport == "streamable-http"
    assert service.closed is True


def test_cli_closes_service_when_server_fails(monkeypatch: Any) -> None:
    class FailingMCPServer(FakeMCPServer):
        def run(self, *, transport: str) -> None:
            raise RuntimeError("server stopped")

    service = FakeCurrentDataService()
    monkeypatch.setattr(mcp, "_load_mcp_server_type", lambda: FailingMCPServer)
    monkeypatch.setattr(mcp, "_build_service", lambda: service)

    with pytest.raises(RuntimeError, match="server stopped"):
        mcp.main([])

    assert service.closed is True
