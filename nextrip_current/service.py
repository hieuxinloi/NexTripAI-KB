from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Mapping, Sequence
from typing import Protocol

from nextrip_pipeline.publishing.current_price import CurrentHotelPriceSnapshot
from nextrip_pipeline.publishing.current_availability import (
    CurrentHotelAvailabilitySnapshot,
)
from nextrip_pipeline.schemas import (
    CurrentPlaceSnapshot,
    ExternalEntityMapping,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
)
from nextrip_traffic.models import (
    TrafficRouteRequest,
    TrafficRouteResponse,
    TransportRecommendationRequest,
    TransportRecommendationResponse,
)

from .errors import (
    CurrentPlaceNotFoundError,
    HotelRefreshError,
    HotelRefreshUnavailableError,
    TrafficIntegrationUnavailableError,
)
from .models import (
    CurrentHotelOffer,
    CurrentLookupStatus,
    CurrentPlaceEnvelope,
    HotelAvailabilitySearchRequest,
    HotelAvailabilityResult,
    HotelAvailabilitySearchResponse,
    HotelIdentity,
    HotelNameSource,
    HotelOfferProvenance,
    HotelOfferResult,
    HotelOfferSearchRequest,
    HotelOfferSearchResponse,
    HotelStayWindowResult,
    PlaceBatchItem,
    PlaceBatchRequest,
    PlaceBatchResponse,
    RepositoryReadiness,
    TripContextRequest,
    TripContextResponse,
    TripContextRouteResult,
)
from .repository import CurrentDataRepository
from .traffic import TrafficHttpClient


class HotelPriceRefresher(Protocol):
    def refresh(self, hotel_id: str, request: HotelOfferSearchRequest) -> None: ...


class CurrentDataService:
    """Application service shared by HTTP and future MCP adapters."""

    def __init__(
        self,
        repository: CurrentDataRepository,
        *,
        hotel_refresher: HotelPriceRefresher | None = None,
        traffic_client: TrafficHttpClient | None = None,
        clock=None,
    ) -> None:
        self.repository = repository
        self.hotel_refresher = hotel_refresher
        self.traffic_client = traffic_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        refresher_close = getattr(self.hotel_refresher, "close", None)
        try:
            if callable(refresher_close):
                refresher_close()
        finally:
            if self.traffic_client is not None:
                self.traffic_client.close()

    def readiness(self) -> RepositoryReadiness:
        return self.repository.readiness()

    def get_place(self, place_id: str) -> CurrentPlaceEnvelope:
        evaluated_at = self._now()
        place = self.repository.get_place(place_id)
        if place is None:
            raise CurrentPlaceNotFoundError(place_id)
        return self._place_envelope(place, evaluated_at)

    def get_places(
        self,
        request: PlaceBatchRequest | Mapping[str, object] | Sequence[str],
    ) -> PlaceBatchResponse:
        if isinstance(request, Sequence) and not isinstance(request, (str, bytes)):
            request = PlaceBatchRequest(place_ids=list(request))
        else:
            request = PlaceBatchRequest.model_validate(request)
        evaluated_at = self._now()
        places = self.repository.get_places(request.place_ids)
        items = []
        for place_id in request.place_ids:
            place = places[place_id]
            if place is None:
                items.append(
                    PlaceBatchItem(
                        place_id=place_id,
                        status=CurrentLookupStatus.MISSING,
                    )
                )
                continue
            envelope = self._place_envelope(place, evaluated_at)
            items.append(
                PlaceBatchItem(
                    place_id=place_id,
                    status=(
                        CurrentLookupStatus.STALE
                        if envelope.stale
                        else CurrentLookupStatus.AVAILABLE
                    ),
                    current=envelope,
                )
            )
        return PlaceBatchResponse(evaluated_at=evaluated_at, items=items)

    def search_hotel_offers(
        self, request: HotelOfferSearchRequest | Mapping[str, object]
    ) -> HotelOfferSearchResponse:
        request = HotelOfferSearchRequest.model_validate(request)
        response = self._search_hotel_offers(request)
        if not request.refresh_if_missing:
            return response
        result = response.results[0]
        if result.status is CurrentLookupStatus.AVAILABLE:
            return response
        if self.hotel_refresher is None:
            raise HotelRefreshUnavailableError(
                "hotel refresh is disabled or no refresher is configured"
            )
        hotel_id = request.hotel_ids[0]
        try:
            self.hotel_refresher.refresh(hotel_id, request)
        except Exception as error:
            raise HotelRefreshError(
                f"hotel refresh failed for {hotel_id}: {type(error).__name__}"
            ) from error
        refreshed = self._search_hotel_offers(request)
        refreshed_result = refreshed.results[0].model_copy(
            update={"refresh_attempted": True}
        )
        return refreshed.model_copy(update={"results": [refreshed_result]})

    def search_hotel_availability(
        self, request: HotelAvailabilitySearchRequest | Mapping[str, object]
    ) -> HotelAvailabilitySearchResponse:
        """Read or refresh availability for a complete stay and safe fallbacks."""

        request = HotelAvailabilitySearchRequest.model_validate(request)
        response = self._search_hotel_availability(request)
        if not request.refresh_if_missing or self._availability_is_complete(response):
            return response
        if self.hotel_refresher is None:
            raise HotelRefreshUnavailableError(
                "hotel refresh is disabled or no refresher is configured"
            )
        hotel_id = request.hotel_ids[0]
        try:
            self.hotel_refresher.refresh(hotel_id, request)
        except Exception as error:
            raise HotelRefreshError(
                f"hotel refresh failed for {hotel_id}: {type(error).__name__}"
            ) from error

        refreshed = self._search_hotel_availability(request)
        refreshed_results = [
            result.model_copy(
                update={
                    "windows": [
                        window.model_copy(update={"refresh_attempted": True})
                        for window in result.windows
                    ]
                }
            )
            for result in refreshed.results
        ]
        return refreshed.model_copy(update={"results": refreshed_results})

    def route(
        self, request: TrafficRouteRequest | Mapping[str, object]
    ) -> TrafficRouteResponse:
        request = TrafficRouteRequest.model_validate(request)
        if self.traffic_client is None:
            raise TrafficIntegrationUnavailableError(
                "traffic API integration is not configured"
            )
        return self.traffic_client.route(request)

    def recommend_transport(
        self, request: TransportRecommendationRequest | Mapping[str, object]
    ) -> TransportRecommendationResponse:
        request = TransportRecommendationRequest.model_validate(request)
        if self.traffic_client is None:
            raise TrafficIntegrationUnavailableError(
                "traffic API integration is not configured"
            )
        return self.traffic_client.recommend_transport(request)

    def build_trip_context(
        self, request: TripContextRequest | Mapping[str, object]
    ) -> TripContextResponse:
        request = TripContextRequest.model_validate(request)
        places = self.get_places(request.place_ids)
        hotel_availability = None
        hotel_error_code = None
        if request.hotel_search is not None:
            try:
                hotel_availability = self.search_hotel_availability(
                    request.hotel_search
                )
            except Exception as error:
                hotel_error_code = type(error).__name__
        routes = []
        for leg in request.route_legs:
            try:
                recommendation = self.recommend_transport(
                    TransportRecommendationRequest(
                        origin_id=leg.origin_id,
                        destination_id=leg.destination_id,
                        departure_time=leg.departure_time,
                    )
                )
            except Exception as error:
                routes.append(
                    TripContextRouteResult(
                        origin_id=leg.origin_id,
                        destination_id=leg.destination_id,
                        status="unavailable",
                        error_code=type(error).__name__,
                    )
                )
                continue
            routes.append(
                TripContextRouteResult(
                    origin_id=leg.origin_id,
                    destination_id=leg.destination_id,
                    status="available",
                    recommendation=recommendation,
                )
            )
        return TripContextResponse(
            evaluated_at=self._now(),
            places=places,
            hotel_availability=hotel_availability,
            hotel_error_code=hotel_error_code,
            routes=routes,
        )

    def _search_hotel_offers(
        self, request: HotelOfferSearchRequest
    ) -> HotelOfferSearchResponse:
        evaluated_at = self._now()
        snapshots = self.repository.find_exact_hotel_offers(
            hotel_ids=request.hotel_ids,
            check_in=request.check_in,
            check_out=request.check_out,
            occupancy=request.occupancy,
            children_ages=request.children_ages,
            currency=request.currency,
            seller=request.seller,
        )
        results = []
        for hotel_id in request.hotel_ids:
            values = snapshots.get(hotel_id, [])
            stale_values = [value for value in values if value.is_stale(evaluated_at)]
            fresh_values = [
                value for value in values if not value.is_stale(evaluated_at)
            ]
            visible = values if request.include_stale else fresh_values
            status = (
                CurrentLookupStatus.AVAILABLE
                if fresh_values
                else CurrentLookupStatus.STALE
                if stale_values
                else CurrentLookupStatus.MISSING
            )
            mapping = self.repository.get_confirmed_trivago_mapping(hotel_id)
            place = self.repository.get_place(hotel_id)
            results.append(
                HotelOfferResult(
                    hotel_id=hotel_id,
                    status=status,
                    identity=self._hotel_identity(hotel_id, place, mapping),
                    offers=[
                        self._hotel_offer(value, evaluated_at, mapping)
                        for value in visible
                    ],
                    stale_offer_count=len(stale_values),
                    latest_observed_at=(
                        values[0].observation.observed_at if values else None
                    ),
                    latest_stale_after=(
                        max(value.stale_after for value in values) if values else None
                    ),
                )
            )
        return HotelOfferSearchResponse(
            check_in=request.check_in,
            check_out=request.check_out,
            occupancy=request.occupancy,
            children_ages=request.children_ages,
            currency=request.currency,
            seller=request.seller,
            include_stale=request.include_stale,
            evaluated_at=evaluated_at,
            results=results,
        )

    def _search_hotel_availability(
        self, request: HotelOfferSearchRequest
    ) -> HotelAvailabilitySearchResponse:
        evaluated_at = self._now()
        results: list[HotelAvailabilityResult] = []
        for hotel_id in request.hotel_ids:
            mapping = self.repository.get_confirmed_trivago_mapping(hotel_id)
            place = self.repository.get_place(hotel_id)
            windows: list[HotelStayWindowResult] = []
            selected_window_index = None
            for offset in range(request.lookahead_days + 1):
                check_in = request.check_in + timedelta(days=offset)
                check_out = check_in + timedelta(days=request.stay_nights)
                prices = self.repository.find_exact_hotel_offers(
                    hotel_ids=[hotel_id],
                    check_in=check_in,
                    check_out=check_out,
                    occupancy=request.occupancy,
                    children_ages=request.children_ages,
                    currency=request.currency,
                    seller=request.seller,
                )[hotel_id]
                availability = self.repository.find_exact_hotel_availability(
                    hotel_ids=[hotel_id],
                    check_in=check_in,
                    check_out=check_out,
                    occupancy=request.occupancy,
                    children_ages=request.children_ages,
                    currency=request.currency,
                )[hotel_id]
                window = self._hotel_stay_window(
                    hotel_id=hotel_id,
                    requested_check_in=request.check_in,
                    fallback_offset_days=offset,
                    stay_nights=request.stay_nights,
                    price_snapshots=prices,
                    availability_snapshot=availability,
                    evaluated_at=evaluated_at,
                    include_stale=request.include_stale,
                    mapping=mapping,
                )
                windows.append(window)
                if (
                    window.lookup_status is CurrentLookupStatus.AVAILABLE
                    and window.availability is HotelAvailabilityStatus.AVAILABLE
                ):
                    selected_window_index = offset
                    break
                if not (
                    window.lookup_status is CurrentLookupStatus.AVAILABLE
                    and window.availability is HotelAvailabilityStatus.UNAVAILABLE
                ):
                    # A later date is only queried after current, trusted
                    # unavailable evidence. Missing/stale/technical failure is
                    # never converted into a claim that the hotel is sold out.
                    break
            results.append(
                HotelAvailabilityResult(
                    hotel_id=hotel_id,
                    identity=self._hotel_identity(hotel_id, place, mapping),
                    windows=windows,
                    selected_window_index=selected_window_index,
                )
            )
        return HotelAvailabilitySearchResponse(
            check_in=request.check_in,
            check_out=request.check_out,
            stay_nights=request.stay_nights,
            stay_days=request.stay_days,
            lookahead_days=request.lookahead_days,
            occupancy=request.occupancy,
            children_ages=request.children_ages,
            currency=request.currency,
            seller=request.seller,
            include_stale=request.include_stale,
            evaluated_at=evaluated_at,
            results=results,
        )

    def _hotel_stay_window(
        self,
        *,
        hotel_id: str,
        requested_check_in,
        fallback_offset_days: int,
        stay_nights: int,
        price_snapshots: list[CurrentHotelPriceSnapshot],
        availability_snapshot: CurrentHotelAvailabilitySnapshot | None,
        evaluated_at: datetime,
        include_stale: bool,
        mapping: ExternalEntityMapping | None,
    ) -> HotelStayWindowResult:
        check_in = requested_check_in + timedelta(days=fallback_offset_days)
        check_out = check_in + timedelta(days=stay_nights)
        price_snapshots = [
            snapshot
            for snapshot in price_snapshots
            if self._price_matches_current_mapping(snapshot, mapping)
        ]
        stale_prices = [
            snapshot for snapshot in price_snapshots if snapshot.is_stale(evaluated_at)
        ]
        fresh_prices = [
            snapshot
            for snapshot in price_snapshots
            if not snapshot.is_stale(evaluated_at)
        ]

        # The price-only scheduler and the availability runner can update the
        # same context independently. Always prefer the newest evidence: an
        # older unavailable/unknown snapshot must not hide a newer priced
        # offer, while newer availability still suppresses an older price.
        if availability_snapshot is not None and price_snapshots:
            newest_price = max(
                price_snapshots,
                key=lambda snapshot: (
                    snapshot.observation.observed_at,
                    snapshot.updated_at,
                ),
            )
            if (
                newest_price.observation.observed_at
                > availability_snapshot.observation.observed_at
            ):
                availability_snapshot = None

        if availability_snapshot is not None:
            availability_is_stale = availability_snapshot.is_stale(evaluated_at)
            lookup_status = (
                CurrentLookupStatus.STALE
                if availability_is_stale
                else CurrentLookupStatus.AVAILABLE
            )
            availability = availability_snapshot.observation.status
            reason = availability_snapshot.observation.reason
            referenced = set(availability_snapshot.observation.price_observation_ids)
            candidates = [
                snapshot
                for snapshot in price_snapshots
                if snapshot.observation_id in referenced
                and self._price_matches_availability_mapping(
                    snapshot,
                    availability_snapshot,
                )
                and (include_stale or not snapshot.is_stale(evaluated_at))
            ]
            if not self._availability_matches_current_mapping(
                availability_snapshot,
                mapping,
            ):
                availability = HotelAvailabilityStatus.UNKNOWN
                reason = HotelAvailabilityReason.MAPPING_UNRESOLVED
                candidates = []
                if not availability_is_stale:
                    lookup_status = CurrentLookupStatus.MISSING
            elif availability is not HotelAvailabilityStatus.AVAILABLE:
                candidates = []
            elif not candidates:
                # Availability cannot be presented as bookable when its exact
                # price evidence is absent or hidden because it is stale.
                availability = HotelAvailabilityStatus.UNKNOWN
                reason = HotelAvailabilityReason.NO_PRICE
                if not availability_is_stale:
                    lookup_status = CurrentLookupStatus.MISSING
            if availability_is_stale and not include_stale:
                availability = HotelAvailabilityStatus.UNKNOWN
                reason = None
                candidates = []
            offers = [
                self._hotel_offer(snapshot, evaluated_at, mapping)
                for snapshot in candidates
            ]
            latest_observed_at = availability_snapshot.observation.observed_at
            latest_stale_after = availability_snapshot.stale_after
            availability_observation_id = availability_snapshot.observation_id
        elif fresh_prices:
            lookup_status = CurrentLookupStatus.AVAILABLE
            availability = HotelAvailabilityStatus.AVAILABLE
            reason = HotelAvailabilityReason.OFFER_FOUND
            offers = [
                self._hotel_offer(snapshot, evaluated_at, mapping)
                for snapshot in fresh_prices
            ]
            latest_observed_at = max(
                snapshot.observation.observed_at for snapshot in fresh_prices
            )
            latest_stale_after = max(snapshot.stale_after for snapshot in fresh_prices)
            availability_observation_id = None
        elif stale_prices:
            lookup_status = CurrentLookupStatus.STALE
            visible = stale_prices if include_stale else []
            availability = (
                HotelAvailabilityStatus.AVAILABLE
                if visible
                else HotelAvailabilityStatus.UNKNOWN
            )
            reason = (
                HotelAvailabilityReason.OFFER_FOUND
                if visible
                else HotelAvailabilityReason.NO_PRICE
            )
            offers = [
                self._hotel_offer(snapshot, evaluated_at, mapping)
                for snapshot in visible
            ]
            latest_observed_at = max(
                snapshot.observation.observed_at for snapshot in stale_prices
            )
            latest_stale_after = max(snapshot.stale_after for snapshot in stale_prices)
            availability_observation_id = None
        else:
            lookup_status = CurrentLookupStatus.MISSING
            availability = HotelAvailabilityStatus.UNKNOWN
            reason = None
            offers = []
            latest_observed_at = None
            latest_stale_after = None
            availability_observation_id = None

        return HotelStayWindowResult(
            requested_check_in=requested_check_in,
            fallback_offset_days=fallback_offset_days,
            check_in=check_in,
            check_out=check_out,
            stay_nights=stay_nights,
            lookup_status=lookup_status,
            availability=availability,
            reason=reason,
            offers=offers,
            stale_offer_count=len(stale_prices),
            latest_observed_at=latest_observed_at,
            latest_stale_after=latest_stale_after,
            availability_observation_id=availability_observation_id,
        )

    @staticmethod
    def _availability_is_complete(
        response: HotelAvailabilitySearchResponse,
    ) -> bool:
        result = response.results[0]
        if result.selected_window_index is not None:
            return True
        last = result.windows[-1]
        if last.lookup_status is not CurrentLookupStatus.AVAILABLE:
            return False
        if last.availability is HotelAvailabilityStatus.UNKNOWN:
            return True
        return len(result.windows) == response.lookahead_days + 1

    @staticmethod
    def _price_matches_current_mapping(
        snapshot: CurrentHotelPriceSnapshot,
        mapping: ExternalEntityMapping | None,
    ) -> bool:
        observation = snapshot.observation
        if observation.mapping_id is None or mapping is None:
            return True
        return (observation.mapping_id, observation.external_id) == (
            mapping.mapping_id,
            mapping.external_id,
        )

    @staticmethod
    def _availability_matches_current_mapping(
        snapshot: CurrentHotelAvailabilitySnapshot,
        mapping: ExternalEntityMapping | None,
    ) -> bool:
        observation = snapshot.observation
        if observation.mapping_id is None or mapping is None:
            return True
        return (observation.mapping_id, observation.external_id) == (
            mapping.mapping_id,
            mapping.external_id,
        )

    @staticmethod
    def _price_matches_availability_mapping(
        price: CurrentHotelPriceSnapshot,
        availability: CurrentHotelAvailabilitySnapshot,
    ) -> bool:
        return (
            price.observation.mapping_id,
            price.observation.external_id,
        ) == (
            availability.observation.mapping_id,
            availability.observation.external_id,
        )

    def _place_envelope(
        self, place: CurrentPlaceSnapshot, evaluated_at: datetime
    ) -> CurrentPlaceEnvelope:
        mapping = (
            self.repository.get_confirmed_trivago_mapping(place.place_id)
            if place.entity_type.value == "hotel"
            else None
        )
        identity = self._hotel_identity(place.place_id, place, mapping)
        display_name = identity.display_name or place.name
        exposed_place = (
            place
            if display_name == place.name
            else place.model_copy(update={"name": display_name})
        )
        return CurrentPlaceEnvelope(
            place=exposed_place,
            master_name=place.name,
            aliases=identity.aliases,
            name_source=identity.name_source,
            stale=place.is_stale(evaluated_at),
            evaluated_at=evaluated_at,
        )

    @staticmethod
    def _hotel_identity(
        hotel_id: str,
        place: CurrentPlaceSnapshot | None,
        mapping: ExternalEntityMapping | None,
    ) -> HotelIdentity:
        master_name = place.name if place is not None else None
        mapping_id = None
        external_id = None
        external_url = None
        trivago_name = None
        hotel_name = None
        mapped_master_name = None
        if mapping is not None:
            mapping_id = mapping.mapping_id
            external_id = mapping.external_id
            external_url = mapping.external_url
            trivago_name = CurrentDataService._clean_name(
                mapping.attributes.get("trivago_name")
            )
            hotel_name = CurrentDataService._clean_name(
                mapping.attributes.get("hotel_name")
            )
            mapped_master_name = CurrentDataService._clean_name(
                mapping.attributes.get("master_name")
            )
        if trivago_name:
            display_name = trivago_name
            source = HotelNameSource.TRIVAGO_NAME
        elif hotel_name:
            display_name = hotel_name
            source = HotelNameSource.HOTEL_NAME
        elif master_name:
            display_name = master_name
            source = HotelNameSource.CURRENT_PLACE
        else:
            display_name = None
            source = HotelNameSource.UNKNOWN

        aliases = []
        seen = {" ".join((display_name or "").split()).casefold()}
        for candidate in (master_name, mapped_master_name, hotel_name, trivago_name):
            if not candidate:
                continue
            normalized = " ".join(candidate.split()).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            aliases.append(candidate)
        return HotelIdentity(
            hotel_id=hotel_id,
            display_name=display_name,
            master_name=master_name,
            aliases=aliases,
            name_source=source,
            mapping_id=mapping_id,
            external_id=external_id,
            external_url=external_url,
        )

    @staticmethod
    def _hotel_offer(
        snapshot: CurrentHotelPriceSnapshot,
        evaluated_at: datetime,
        mapping: ExternalEntityMapping | None,
    ) -> CurrentHotelOffer:
        observation = snapshot.observation
        captured_mapping = (
            mapping
            if mapping is not None
            and (observation.mapping_id, observation.external_id)
            == (mapping.mapping_id, mapping.external_id)
            else None
        )
        return CurrentHotelOffer(
            hotel_id=snapshot.hotel_id,
            offer_key=observation.offer_key,
            seller=observation.seller,
            room_type=observation.room_type,
            check_in=observation.check_in,
            check_out=observation.check_out,
            occupancy=observation.occupancy,
            children_ages=observation.children_ages,
            currency=observation.currency,
            amount=observation.amount,
            nightly_amount=observation.nightly_amount,
            total_amount=observation.total_amount,
            tax_amount=observation.tax_amount,
            fee_amount=observation.fee_amount,
            min_amount=observation.min_amount,
            max_amount=observation.max_amount,
            tax_included=observation.tax_included,
            meal_plan=observation.meal_plan,
            cancellation_policy=observation.cancellation_policy,
            refundable=observation.refundable,
            booking_url=observation.booking_url,
            availability=observation.availability,
            observed_at=observation.observed_at,
            stale_after=snapshot.stale_after,
            stale=snapshot.is_stale(evaluated_at),
            provenance=HotelOfferProvenance(
                run_id=observation.run_id,
                source_id=observation.source_id,
                source_record_id=observation.source_record_id,
                observation_id=snapshot.observation_id,
                decision_id=snapshot.decision_id,
                verification_status=observation.verification_status,
                mapping_id=observation.mapping_id,
                external_id=observation.external_id,
                external_url=(
                    captured_mapping.external_url
                    if captured_mapping is not None
                    else None
                ),
            ),
        )

    @staticmethod
    def _clean_name(value) -> str | None:
        if not isinstance(value, str):
            return None
        return " ".join(value.split()) or None

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Current Data clock must return an aware datetime")
        return value
