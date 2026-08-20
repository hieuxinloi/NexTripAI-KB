"""Optional MCP facade for the current-data service.

The module deliberately imports the MCP SDK lazily.  Importing
``nextrip_current`` (or an HTTP API built on it) therefore does not require the
optional ``mcp`` dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from nextrip_current.models import (
    HotelAvailabilitySearchRequest,
    HotelOfferSearchRequest,
    PlaceBatchRequest,
)
from nextrip_traffic.models import (
    TrafficRouteRequest,
    TransportRecommendationRequest,
)

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from nextrip_current.service import CurrentDataService


MCP_SERVER_NAME = "NexTrip Current Data"
MCP_INSTALL_HINT = 'Install the MCP extra with: pip install -e ".[mcp]"'


class MissingMCPDependencyError(RuntimeError):
    """Raised when the optional MCP SDK is needed but not installed."""


def _load_mcp_server_type() -> type[MCPServer]:
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise MissingMCPDependencyError(
            "The MCP facade requires the official MCP Python SDK v2. "
            f"{MCP_INSTALL_HINT}"
        ) from exc
    return MCPServer


def _as_structured_dict(value: Any) -> dict[str, Any]:
    """Convert a service response to MCP structured content without reshaping it."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="json")

    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Current-data service response keys must be strings")
        return dict(value)

    # MCP structured content must be a JSON object.  A collection-returning
    # service keeps its value intact under a single neutral envelope.
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"result": list(value)}

    raise TypeError(
        "Current-data service methods must return a mapping, Pydantic model, "
        "or sequence"
    )


def create_mcp_server(
    service: CurrentDataService,
    *,
    server_factory: Callable[[str], Any] | None = None,
) -> MCPServer:
    """Create an MCP server whose tools delegate directly to ``service``.

    ``server_factory`` is an intentionally small injection seam.  Unit tests can
    validate the facade without installing the optional MCP SDK.
    """

    factory = server_factory or _load_mcp_server_type()
    server = factory(MCP_SERVER_NAME)

    @server.tool(
        name="get_current_place",
        description="Get the current canonical record for one registry place_id.",
        structured_output=True,
    )
    def get_current_place(place_id: str) -> dict[str, Any]:
        return _as_structured_dict(service.get_place(place_id))

    @server.tool(
        name="get_current_places",
        description="Get current canonical records for registry place_ids.",
        structured_output=True,
    )
    def get_current_places(place_ids: list[str]) -> dict[str, Any]:
        request = PlaceBatchRequest(place_ids=place_ids)
        return _as_structured_dict(service.get_places(request))

    @server.tool(
        name="search_current_hotel_offers",
        description="Search current contextual hotel offers.",
        structured_output=True,
    )
    def search_current_hotel_offers(
        request: HotelOfferSearchRequest,
    ) -> dict[str, Any]:
        validated = HotelOfferSearchRequest.model_validate(request)
        return _as_structured_dict(service.search_hotel_offers(validated))

    @server.tool(
        name="search_hotel_availability",
        description=(
            "Search an exact hotel stay using check_out, stay_nights, or "
            "inclusive stay_days; only after confirmed unavailability, inspect "
            "later check-in dates up to lookahead_days. Missing data refreshes "
            "by default."
        ),
        structured_output=True,
    )
    def search_hotel_availability(
        request: HotelAvailabilitySearchRequest,
    ) -> dict[str, Any]:
        validated = HotelAvailabilitySearchRequest.model_validate(request)
        return _as_structured_dict(service.search_hotel_availability(validated))

    @server.tool(
        name="calculate_route",
        description="Calculate a route using the current traffic service.",
        structured_output=True,
    )
    def calculate_route(payload: TrafficRouteRequest) -> dict[str, Any]:
        request = TrafficRouteRequest.model_validate(payload)
        return _as_structured_dict(service.route(request))

    @server.tool(
        name="recommend_transport",
        description="Rank eligible transport modes for a trip request.",
        structured_output=True,
    )
    def recommend_transport(
        payload: TransportRecommendationRequest,
    ) -> dict[str, Any]:
        request = TransportRecommendationRequest.model_validate(payload)
        return _as_structured_dict(service.recommend_transport(request))

    return server


def _build_service() -> CurrentDataService:
    from nextrip_current.runtime import build_current_data_service

    return build_current_data_service()


def _close_service(service: Any) -> None:
    close = getattr(service, "close", None)
    if not callable(close):
        return

    result = close()
    if inspect.isawaitable(result):
        asyncio.run(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextrip-current-mcp",
        description="Serve NexTrip current data through the Model Context Protocol.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        server_type = _load_mcp_server_type()
    except MissingMCPDependencyError as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")

    service = _build_service()
    try:
        server = create_mcp_server(service, server_factory=server_type)
        server.run(transport=args.transport)
    finally:
        _close_service(service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
