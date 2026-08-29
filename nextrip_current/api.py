from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from nextrip_pipeline.schemas import NexTripModel
from nextrip_traffic.models import (
    TrafficRouteRequest,
    TrafficRouteResponse,
    TransportRecommendationRequest,
    TransportRecommendationResponse,
)

from .config import CurrentDataSettings
from .errors import (
    CurrentDataCorruptError,
    CurrentDataUnavailableError,
    CurrentPlaceNotFoundError,
    HotelRefreshError,
    HotelRefreshUnavailableError,
    TrafficIntegrationError,
    TrafficIntegrationUnavailableError,
)
from .models import (
    CurrentPlaceEnvelope,
    HotelAvailabilitySearchRequest,
    HotelAvailabilitySearchResponse,
    HotelOfferSearchRequest,
    HotelOfferSearchResponse,
    PlaceBatchRequest,
    PlaceBatchResponse,
    TripContextRequest,
    TripContextResponse,
)
from .runtime import CurrentDataServiceFactory, build_current_data_service
from .service import CurrentDataService


class ErrorDetail(NexTripModel):
    code: str
    message: str


class ErrorResponse(NexTripModel):
    error: ErrorDetail


class CurrentReadinessResponse(NexTripModel):
    status: str
    service: str = "nextrip-current"
    place_count: int = Field(ge=0)
    hotel_offer_count: int = Field(ge=0)
    trivago_mapping_count: int = Field(ge=0)
    issues: list[str] = Field(default_factory=list)


class _AuthenticationError(PermissionError):
    pass


class _AuthenticationConfigurationError(RuntimeError):
    pass


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def _factory(request: Request) -> CurrentDataServiceFactory:
    return request.app.state.current_data_service_factory


def get_service(request: Request) -> CurrentDataService:
    return _factory(request).get()


def require_internal_api_key(
    request: Request,
    provided_key: Annotated[
        str | None,
        Header(alias="X-NexTrip-Current-Key"),
    ] = None,
) -> None:
    expected: SecretStr | None = request.app.state.current_data_api_key
    if expected is None:
        raise _AuthenticationConfigurationError(
            "Current Data API key is not configured"
        )
    expected_value = expected.get_secret_value()
    if provided_key is None or not secrets.compare_digest(
        provided_key.encode("utf-8"), expected_value.encode("utf-8")
    ):
        raise _AuthenticationError("invalid or missing Current Data API key")


ServiceDependency = Annotated[CurrentDataService, Depends(get_service)]


def create_app(
    *,
    settings: CurrentDataSettings | None = None,
    service_factory: CurrentDataServiceFactory | None = None,
    internal_api_key: str | SecretStr | None = None,
) -> FastAPI:
    resolved = settings or CurrentDataSettings.from_env()
    factory = service_factory or CurrentDataServiceFactory(
        lambda: build_current_data_service(resolved)
    )
    if isinstance(internal_api_key, str):
        configured_key = SecretStr(internal_api_key)
    elif isinstance(internal_api_key, SecretStr):
        configured_key = internal_api_key
    else:
        configured_key = resolved.api_key

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        # Fail closed before accepting traffic and populate the readiness cache.
        # Recursive counts are expensive on a Windows bind mount, so probes must
        # not rediscover every operational JSON artifact on every request.
        factory.get().readiness()
        try:
            yield
        finally:
            factory.close()

    application = FastAPI(
        title="NexTrip Current Data Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.current_data_service_factory = factory
    application.state.current_data_api_key = configured_key
    _register_exception_handlers(application)
    _register_routes(application)
    return application


def _register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(_AuthenticationError)
    async def authentication_failed(
        _request: Request, error: _AuthenticationError
    ) -> JSONResponse:
        return _error_response(
            status_code=401,
            code="current_api_key_invalid",
            message=str(error),
            headers={"WWW-Authenticate": "ApiKey"},
        )

    @application.exception_handler(_AuthenticationConfigurationError)
    async def authentication_not_configured(
        _request: Request, error: _AuthenticationConfigurationError
    ) -> JSONResponse:
        return _error_response(
            status_code=503,
            code="current_api_key_not_configured",
            message=str(error),
        )

    @application.exception_handler(CurrentPlaceNotFoundError)
    async def place_not_found(
        _request: Request, error: CurrentPlaceNotFoundError
    ) -> JSONResponse:
        return _error_response(
            status_code=404,
            code="current_place_not_found",
            message=str(error),
        )

    @application.exception_handler(HotelRefreshUnavailableError)
    async def refresh_unavailable(
        _request: Request, error: HotelRefreshUnavailableError
    ) -> JSONResponse:
        return _error_response(
            status_code=503,
            code="refresh_unavailable",
            message=str(error),
        )

    @application.exception_handler(HotelRefreshError)
    async def refresh_failed(
        _request: Request, error: HotelRefreshError
    ) -> JSONResponse:
        return _error_response(
            status_code=502,
            code="hotel_refresh_failed",
            message=str(error),
        )

    @application.exception_handler(CurrentDataUnavailableError)
    async def current_data_unavailable(
        _request: Request, error: CurrentDataUnavailableError
    ) -> JSONResponse:
        return _error_response(
            status_code=503,
            code="current_data_unavailable",
            message=str(error),
        )

    @application.exception_handler(CurrentDataCorruptError)
    async def current_data_corrupt(
        _request: Request, error: CurrentDataCorruptError
    ) -> JSONResponse:
        return _error_response(
            status_code=500,
            code="current_data_corrupt",
            message=str(error),
        )

    @application.exception_handler(TrafficIntegrationUnavailableError)
    async def traffic_unavailable(
        _request: Request, error: TrafficIntegrationUnavailableError
    ) -> JSONResponse:
        return _error_response(
            status_code=503,
            code="traffic_integration_unavailable",
            message=str(error),
        )

    @application.exception_handler(TrafficIntegrationError)
    async def traffic_failed(
        _request: Request, error: TrafficIntegrationError
    ) -> JSONResponse:
        return _error_response(
            status_code=502,
            code="traffic_integration_failed",
            message=str(error),
        )


def _register_routes(application: FastAPI) -> None:
    protected = [Depends(require_internal_api_key)]

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "nextrip-current"}

    @application.get(
        "/ready",
        response_model=CurrentReadinessResponse,
        responses={503: {"model": CurrentReadinessResponse}},
    )
    def readiness(service: ServiceDependency):
        repository = service.readiness()
        payload = CurrentReadinessResponse(
            status="ready" if repository.ready else "not_ready",
            place_count=repository.place_count,
            hotel_offer_count=repository.hotel_offer_count,
            trivago_mapping_count=repository.trivago_mapping_count,
            issues=repository.issues,
        )
        if repository.ready:
            return payload
        return JSONResponse(
            status_code=503,
            content=payload.model_dump(mode="json"),
        )

    @application.get(
        "/api/current/places/{place_id}",
        response_model=CurrentPlaceEnvelope,
        responses={404: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def get_current_place(
        place_id: str, service: ServiceDependency
    ) -> CurrentPlaceEnvelope:
        return service.get_place(place_id)

    @application.post(
        "/api/current/places/batch",
        response_model=PlaceBatchResponse,
        dependencies=protected,
    )
    def get_current_places(
        payload: PlaceBatchRequest, service: ServiceDependency
    ) -> PlaceBatchResponse:
        return service.get_places(payload)

    @application.post(
        "/api/current/hotel-offers/search",
        response_model=HotelOfferSearchResponse,
        responses={
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        dependencies=protected,
    )
    def search_hotel_offers(
        payload: HotelOfferSearchRequest, service: ServiceDependency
    ) -> HotelOfferSearchResponse:
        return service.search_hotel_offers(payload)

    @application.post(
        "/api/current/hotel-availability/search",
        response_model=HotelAvailabilitySearchResponse,
        responses={
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        dependencies=protected,
    )
    def search_hotel_availability(
        payload: HotelAvailabilitySearchRequest, service: ServiceDependency
    ) -> HotelAvailabilitySearchResponse:
        return service.search_hotel_availability(payload)

    @application.post(
        "/api/current/traffic/routes",
        response_model=TrafficRouteResponse,
        responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def create_traffic_route(
        payload: TrafficRouteRequest, service: ServiceDependency
    ) -> TrafficRouteResponse:
        return service.route(payload)

    @application.post(
        "/api/current/traffic/recommendations",
        response_model=TransportRecommendationResponse,
        responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def create_transport_recommendation(
        payload: TransportRecommendationRequest,
        service: ServiceDependency,
    ) -> TransportRecommendationResponse:
        return service.recommend_transport(payload)

    @application.post(
        "/api/current/trip-context",
        response_model=TripContextResponse,
        responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def build_trip_context(
        payload: TripContextRequest,
        service: ServiceDependency,
    ) -> TripContextResponse:
        return service.build_trip_context(payload)


app = create_app()
