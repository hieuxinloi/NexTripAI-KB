from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from nextrip_pipeline.schemas import (
    AccessPointRecord,
    AccessPointType,
    NexTripModel,
    RoutingProvider,
)

from .access_points import (
    AccessPointNotFoundError as RegistryAccessPointNotFoundError,
)
from .errors import (
    AccessPointNotFoundError,
    InvalidTrafficRequestError,
    RouteNotFoundError,
    RoutingProviderAuthenticationError,
    RoutingProviderError,
    RoutingProviderRateLimitError,
    TrafficConfigurationError,
    TrafficError,
    TrafficValidationError,
    UnsupportedTransportModeError,
)
from .models import (
    TrafficMatrixRequest,
    TrafficMatrixResponse,
    TrafficRouteRequest,
    TrafficRouteResponse,
    TransportRecommendationRequest,
    TransportRecommendationResponse,
)
from .providers import ProviderHealth
from .runtime import TrafficRuntime, TrafficRuntimeFactory


class ErrorDetail(NexTripModel):
    code: str
    message: str


class ErrorResponse(NexTripModel):
    error: ErrorDetail


class ReadinessResponse(NexTripModel):
    status: str
    service: str = "nextrip-traffic"
    access_points: int
    valhalla: ProviderHealth | None = None


class _CachedResourceNotFoundError(LookupError):
    def __init__(self, *, resource: str, identifier: str) -> None:
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"unknown {resource}: {identifier}")


class _InternalAuthenticationError(PermissionError):
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


def _factory(request: Request) -> TrafficRuntimeFactory:
    return request.app.state.traffic_runtime_factory


def get_runtime(request: Request) -> TrafficRuntime:
    """FastAPI dependency that initializes traffic only when first required."""

    return _factory(request).get()


def require_internal_api_key(
    request: Request,
    provided_key: Annotated[
        str | None,
        Header(alias="X-NexTrip-Traffic-Key"),
    ] = None,
) -> None:
    """Authorize internal endpoints only when TRAFFIC_API_KEY is configured."""

    expected_key: str | None = request.app.state.traffic_api_key
    if expected_key is None:
        return
    if provided_key is None or not secrets.compare_digest(
        provided_key.encode("utf-8"),
        expected_key.encode("utf-8"),
    ):
        raise _InternalAuthenticationError("invalid or missing traffic API key")


RuntimeDependency = Annotated[TrafficRuntime, Depends(get_runtime)]


def create_app(
    *,
    runtime_factory: TrafficRuntimeFactory | None = None,
    internal_api_key: str | None = None,
) -> FastAPI:
    factory = runtime_factory or TrafficRuntimeFactory()
    configured_key = internal_api_key
    if configured_key is None:
        configured_key = os.getenv("TRAFFIC_API_KEY") or None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        factory.close()

    application = FastAPI(
        title="NexTrip Traffic Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.traffic_runtime_factory = factory
    application.state.traffic_api_key = configured_key
    _register_exception_handlers(application)
    _register_routes(application)
    return application


def _register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(_InternalAuthenticationError)
    async def internal_authentication_failed(
        _request: Request,
        error: _InternalAuthenticationError,
    ) -> JSONResponse:
        return _error_response(
            status_code=401,
            code="traffic_api_key_invalid",
            message=str(error),
            headers={"WWW-Authenticate": "ApiKey"},
        )

    @application.exception_handler(RegistryAccessPointNotFoundError)
    @application.exception_handler(AccessPointNotFoundError)
    async def access_point_not_found(
        _request: Request,
        error: RegistryAccessPointNotFoundError | AccessPointNotFoundError,
    ) -> JSONResponse:
        return _error_response(
            status_code=404,
            code="access_point_not_found",
            message=str(error),
        )

    @application.exception_handler(_CachedResourceNotFoundError)
    async def cached_resource_not_found(
        _request: Request,
        error: _CachedResourceNotFoundError,
    ) -> JSONResponse:
        return _error_response(
            status_code=404,
            code=f"{error.resource}_not_found",
            message=str(error),
        )

    @application.exception_handler(RouteNotFoundError)
    async def route_not_found(
        _request: Request,
        error: RouteNotFoundError,
    ) -> JSONResponse:
        return _error_response(
            status_code=404,
            code="route_not_found",
            message=str(error),
        )

    @application.exception_handler(UnsupportedTransportModeError)
    @application.exception_handler(InvalidTrafficRequestError)
    async def unsupported_mode(
        _request: Request,
        error: UnsupportedTransportModeError | InvalidTrafficRequestError,
    ) -> JSONResponse:
        return _error_response(
            status_code=422,
            code=(
                "unsupported_transport_mode"
                if isinstance(error, UnsupportedTransportModeError)
                else "invalid_traffic_request"
            ),
            message=str(error),
        )

    @application.exception_handler(RoutingProviderAuthenticationError)
    async def provider_authentication_failed(
        _request: Request,
        error: RoutingProviderAuthenticationError,
    ) -> JSONResponse:
        return _error_response(
            status_code=502,
            code="provider_authentication_failed",
            message=str(error),
        )

    @application.exception_handler(RoutingProviderRateLimitError)
    async def provider_rate_limited(
        _request: Request,
        error: RoutingProviderRateLimitError,
    ) -> JSONResponse:
        return _error_response(
            status_code=503,
            code="provider_rate_limited",
            message=str(error),
        )

    @application.exception_handler(TrafficValidationError)
    async def invalid_provider_result(
        _request: Request,
        error: TrafficValidationError,
    ) -> JSONResponse:
        return _error_response(
            status_code=502,
            code="invalid_provider_result",
            message=str(error),
        )

    @application.exception_handler(RoutingProviderError)
    async def provider_failed(
        _request: Request,
        error: RoutingProviderError,
    ) -> JSONResponse:
        return _error_response(
            status_code=502,
            code="routing_provider_failed",
            message=str(error),
        )

    @application.exception_handler(TrafficConfigurationError)
    async def configuration_failed(
        _request: Request,
        error: TrafficConfigurationError,
    ) -> JSONResponse:
        return _error_response(
            status_code=503,
            code="traffic_configuration_error",
            message=str(error),
        )

    @application.exception_handler(TrafficError)
    async def traffic_failed(
        _request: Request,
        error: TrafficError,
    ) -> JSONResponse:
        return _error_response(
            status_code=500,
            code="traffic_error",
            message=str(error),
        )


def _register_routes(application: FastAPI) -> None:
    protected = [Depends(require_internal_api_key)]

    @application.get("/health")
    def health() -> dict[str, str]:
        # Deliberately does not resolve RuntimeDependency: liveness remains
        # available with no HERE credential or running Valhalla container.
        return {
            "status": "ok",
            "service": "nextrip-traffic",
        }

    @application.get(
        "/ready",
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse}},
    )
    def readiness(runtime: RuntimeDependency) -> ReadinessResponse | JSONResponse:
        access_point_count = len(runtime.registry)
        health = [
            ProviderHealth.model_validate(value)
            for value in runtime.service.provider_health()
        ]
        valhalla = next(
            (
                status
                for status in health
                if status.provider == RoutingProvider.VALHALLA
            ),
            None,
        )
        ready = (
            access_point_count > 0
            and valhalla is not None
            and valhalla.available is True
        )
        payload = ReadinessResponse(
            status="ready" if ready else "not_ready",
            access_points=access_point_count,
            valhalla=valhalla,
        )
        if ready:
            return payload
        return JSONResponse(
            status_code=503,
            content=payload.model_dump(mode="json"),
        )

    @application.post(
        "/routes",
        response_model=TrafficRouteResponse,
        responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def create_route(
        payload: TrafficRouteRequest,
        runtime: RuntimeDependency,
    ) -> TrafficRouteResponse:
        return runtime.service.route(payload)

    @application.post(
        "/transport-recommendations",
        response_model=TransportRecommendationResponse,
        responses={
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
        dependencies=protected,
    )
    def recommend_transport(
        payload: TransportRecommendationRequest,
        runtime: RuntimeDependency,
    ) -> TransportRecommendationResponse:
        return runtime.service.recommend_transport(payload)

    @application.get(
        "/routes/{observation_id}",
        response_model=TrafficRouteResponse,
        responses={404: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def get_route(
        observation_id: str,
        runtime: RuntimeDependency,
        allow_stale: bool = Query(default=False),
    ) -> TrafficRouteResponse:
        result = runtime.service.get_route(observation_id, allow_stale=allow_stale)
        if result is None:
            raise _CachedResourceNotFoundError(
                resource="route",
                identifier=observation_id,
            )
        return result

    @application.post(
        "/matrix",
        response_model=TrafficMatrixResponse,
        responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def create_matrix(
        payload: TrafficMatrixRequest,
        runtime: RuntimeDependency,
    ) -> TrafficMatrixResponse:
        return runtime.service.matrix(payload)

    @application.get(
        "/matrix/{matrix_id}",
        response_model=TrafficMatrixResponse,
        responses={404: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def get_matrix(
        matrix_id: str,
        runtime: RuntimeDependency,
        allow_stale: bool = Query(default=False),
    ) -> TrafficMatrixResponse:
        result = runtime.service.get_matrix(matrix_id, allow_stale=allow_stale)
        if result is None:
            raise _CachedResourceNotFoundError(
                resource="matrix",
                identifier=matrix_id,
            )
        return result

    @application.get(
        "/access-points",
        response_model=list[AccessPointRecord],
        dependencies=protected,
    )
    def list_access_points(
        runtime: RuntimeDependency,
        access_type: AccessPointType | None = Query(default=None),
        owner_entity_id: str | None = Query(default=None),
    ) -> list[AccessPointRecord]:
        return runtime.registry.list(
            access_type=access_type,
            owner_entity_id=owner_entity_id,
        )

    @application.get("/access-points/stats", dependencies=protected)
    def access_point_stats(runtime: RuntimeDependency) -> dict[str, object]:
        return runtime.registry.stats()

    @application.get(
        "/access-points/{identifier}",
        response_model=AccessPointRecord,
        responses={404: {"model": ErrorResponse}},
        dependencies=protected,
    )
    def get_access_point(
        identifier: str,
        runtime: RuntimeDependency,
    ) -> AccessPointRecord:
        return runtime.registry.resolve(identifier)

    def provider_health(runtime: RuntimeDependency) -> list[ProviderHealth]:
        return [
            ProviderHealth.model_validate(value)
            for value in runtime.service.provider_health()
        ]

    application.add_api_route(
        "/providers/health",
        provider_health,
        methods=["GET"],
        response_model=list[ProviderHealth],
        dependencies=protected,
    )
    application.add_api_route(
        "/health/providers",
        provider_health,
        methods=["GET"],
        response_model=list[ProviderHealth],
        include_in_schema=False,
        dependencies=protected,
    )


app = create_app()
