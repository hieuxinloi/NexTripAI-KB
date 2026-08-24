from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError

from nextrip_pipeline.canonical.dataset import read_canonical_active_dataset
from nextrip_pipeline.canonical.place_projection import (
    project_canonical_dataset_places,
)
from nextrip_pipeline.publishing.current_availability import (
    CurrentHotelAvailabilitySnapshot,
)
from nextrip_pipeline.publishing.current_price import CurrentHotelPriceSnapshot
from nextrip_pipeline.schemas import (
    CurrentPlaceSnapshot,
    ExternalEntityMapping,
    MappingStatus,
    Occupancy,
)

from .errors import CurrentDataCorruptError
from .models import RepositoryReadiness


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


class CurrentDataRepository:
    """Read canonical places and append-only operational observation stores."""

    def __init__(
        self,
        *,
        canonical_dataset_path: str | Path,
        hotel_price_root: str | Path,
        trivago_mapping_root: str | Path,
        hotel_availability_root: str | Path | None = None,
    ) -> None:
        self.canonical_dataset_path = Path(canonical_dataset_path)
        try:
            dataset = read_canonical_active_dataset(self.canonical_dataset_path)
            self._canonical_places = project_canonical_dataset_places(dataset)
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise CurrentDataCorruptError(
                "invalid canonical place dataset at "
                f"{self.canonical_dataset_path}: {error}"
            ) from error
        self.hotel_price_root = Path(hotel_price_root)
        self.trivago_mapping_root = Path(trivago_mapping_root)
        self.hotel_availability_root = (
            Path(hotel_availability_root)
            if hotel_availability_root is not None
            else self.hotel_price_root.parent / "hotel_availability"
        )

    def get_place(self, place_id: str) -> CurrentPlaceSnapshot | None:
        place = self._canonical_places.get(place_id)
        return place.model_copy(deep=True) if place is not None else None

    def get_places(
        self, place_ids: Iterable[str]
    ) -> dict[str, CurrentPlaceSnapshot | None]:
        return {place_id: self.get_place(place_id) for place_id in place_ids}

    def get_confirmed_trivago_mapping(
        self, hotel_id: str
    ) -> ExternalEntityMapping | None:
        path = self.trivago_mapping_root / f"{quote(hotel_id, safe='-_.')}.json"
        if not path.is_file():
            return None
        try:
            mapping = ExternalEntityMapping.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise CurrentDataCorruptError(
                f"invalid current Trivago mapping for {hotel_id}: {error}"
            ) from error
        if (
            mapping.entity_id != hotel_id
            or mapping.source_id != "trivago-mcp"
            or mapping.status is not MappingStatus.CONFIRMED
        ):
            return None
        return mapping

    def find_exact_hotel_offers(
        self,
        *,
        hotel_ids: Iterable[str],
        check_in: date,
        check_out: date,
        occupancy: Occupancy,
        children_ages: list[int],
        currency: str | None = None,
        seller: str | None = None,
    ) -> dict[str, list[CurrentHotelPriceSnapshot]]:
        requested = set(hotel_ids)
        results = {hotel_id: [] for hotel_id in requested}
        if not self.hotel_price_root.is_dir():
            return results

        # Legacy and contextual paths may temporarily coexist. Retain only the
        # newest snapshot for each exact seller/offer identity.
        deduplicated: dict[tuple[object, ...], CurrentHotelPriceSnapshot] = {}
        for path in self.hotel_price_root.rglob("*.json"):
            if path.name.startswith("."):
                continue
            snapshot = self._read_price(path)
            observation = snapshot.observation
            if (
                snapshot.hotel_id not in requested
                or observation.hotel_id != snapshot.hotel_id
                or observation.check_in != check_in
                or observation.check_out != check_out
                or observation.occupancy != occupancy
                or observation.children_ages != sorted(children_ages)
                or (currency is not None and observation.currency != currency)
                or (
                    seller is not None
                    and _normalized_text(observation.seller) != _normalized_text(seller)
                )
            ):
                continue
            identity = (
                snapshot.hotel_id,
                observation.check_in,
                observation.check_out,
                observation.occupancy.adults,
                observation.occupancy.children,
                observation.occupancy.rooms,
                tuple(observation.children_ages),
                observation.currency,
                _normalized_text(observation.source_id),
                _normalized_text(observation.seller),
                observation.offer_key,
            )
            current = deduplicated.get(identity)
            if current is None or (
                snapshot.observation.observed_at,
                snapshot.updated_at,
            ) > (
                current.observation.observed_at,
                current.updated_at,
            ):
                deduplicated[identity] = snapshot

        for snapshot in deduplicated.values():
            results[snapshot.hotel_id].append(snapshot)
        for values in results.values():
            values.sort(
                key=lambda item: (
                    item.observation.observed_at,
                    item.updated_at,
                ),
                reverse=True,
            )
        return results

    def find_exact_hotel_availability(
        self,
        *,
        hotel_ids: Iterable[str],
        check_in: date,
        check_out: date,
        occupancy: Occupancy,
        children_ages: list[int],
        currency: str | None = None,
        source_id: str | None = "trivago-mcp",
    ) -> dict[str, CurrentHotelAvailabilitySnapshot | None]:
        """Return the newest availability evidence for one exact stay context."""

        requested = set(hotel_ids)
        results: dict[str, CurrentHotelAvailabilitySnapshot | None] = {
            hotel_id: None for hotel_id in requested
        }
        if not self.hotel_availability_root.is_dir():
            return results

        normalized_ages = sorted(children_ages)
        for path in self.hotel_availability_root.rglob("*.json"):
            if path.name.startswith("."):
                continue
            snapshot = self._read_availability(path)
            observation = snapshot.observation
            if (
                snapshot.hotel_id not in requested
                or observation.hotel_id != snapshot.hotel_id
                or observation.check_in != check_in
                or observation.check_out != check_out
                or observation.occupancy != occupancy
                or observation.children_ages != normalized_ages
                or (currency is not None and observation.currency != currency)
                or (
                    source_id is not None
                    and _normalized_text(observation.source_id)
                    != _normalized_text(source_id)
                )
            ):
                continue
            current = results[snapshot.hotel_id]
            if current is None or (
                observation.observed_at,
                snapshot.updated_at,
            ) > (
                current.observation.observed_at,
                current.updated_at,
            ):
                results[snapshot.hotel_id] = snapshot
        return results

    def readiness(self) -> RepositoryReadiness:
        roots: dict[str, Path] = {
            "current_hotel_price_root": self.hotel_price_root,
            "current_trivago_mapping_root": self.trivago_mapping_root,
        }
        issues = []
        counts: dict[str, int] = {}
        counts["canonical_dataset"] = len(self._canonical_places)
        if not self.canonical_dataset_path.is_file():
            issues.append("canonical_dataset is not available")
        for label, root in roots.items():
            if not root.is_dir():
                issues.append(f"{label} is not available")
                counts[label] = 0
                continue
            try:
                counts[label] = sum(
                    1 for path in root.rglob("*.json") if not path.name.startswith(".")
                )
            except OSError as error:
                issues.append(f"{label} is unreadable ({type(error).__name__})")
                counts[label] = 0
        place_count = counts["canonical_dataset"]
        if place_count == 0:
            issues.append("canonical dataset contains no places")
        return RepositoryReadiness(
            ready=not issues,
            place_count=place_count,
            hotel_offer_count=counts.get("current_hotel_price_root", 0),
            trivago_mapping_count=counts.get("current_trivago_mapping_root", 0),
            issues=issues,
        )

    @staticmethod
    def _read_price(path: Path) -> CurrentHotelPriceSnapshot:
        try:
            return CurrentHotelPriceSnapshot.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise CurrentDataCorruptError(
                f"invalid current hotel offer at {path}: {error}"
            ) from error

    @staticmethod
    def _read_availability(path: Path) -> CurrentHotelAvailabilitySnapshot:
        try:
            return CurrentHotelAvailabilitySnapshot.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise CurrentDataCorruptError(
                f"invalid current hotel availability at {path}: {error}"
            ) from error
