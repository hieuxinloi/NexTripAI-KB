from __future__ import annotations

import httpx
from pydantic import SecretStr, ValidationError

from nextrip_traffic.models import (
    TrafficRouteRequest,
    TrafficRouteResponse,
    TransportRecommendationRequest,
    TransportRecommendationResponse,
)

from .errors import TrafficIntegrationError


class TrafficHttpClient:
    """Typed internal client; no Current Data HTTP route exposes it yet."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | None,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
        )

    def route(self, request: TrafficRouteRequest) -> TrafficRouteResponse:
        return self._post("/routes", request, TrafficRouteResponse)

    def recommend_transport(
        self, request: TransportRecommendationRequest
    ) -> TransportRecommendationResponse:
        return self._post(
            "/transport-recommendations",
            request,
            TransportRecommendationResponse,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _post(self, path, request, response_type):
        headers = {}
        if self._api_key is not None:
            headers["X-NexTrip-Traffic-Key"] = self._api_key.get_secret_value()
        try:
            response = self._client.post(
                path,
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            response.raise_for_status()
            return response_type.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise TrafficIntegrationError(
                f"traffic service request failed for {path}: {type(error).__name__}"
            ) from error
