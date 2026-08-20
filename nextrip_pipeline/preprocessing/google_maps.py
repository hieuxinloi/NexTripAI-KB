from __future__ import annotations

import html
import math
import os
import re
from datetime import datetime, time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningStatus,
    ExternalEntityMapping,
    GeoPoint,
    GoogleMapsPlaceObservation,
    MappingStatus,
    MenuSourceObservation,
    MenuSourceType,
    OpeningInterval,
    OpeningStatusObservation,
    PlaceMediaAsset,
    PlaceMediaObservation,
    PlaceMediaRole,
    RecordSubjectType,
    SourceRecord,
    VerificationStatus,
    Weekday,
    DailyOpeningSchedule,
    WeeklyOpeningScheduleObservation,
)


class GoogleMapsNormalizationError(ValueError):
    """Raised when a Maps capture cannot be normalized safely."""


class GoogleMapsPlaceNormalizer:
    source_id = "google-maps-web"
    timezone_name = "Asia/Ho_Chi_Minh"

    _coordinate_pattern = re.compile(
        r"!3d(-?\d{1,2}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)"
    )
    _url_coordinate_pattern = re.compile(
        r"/@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)"
    )

    def normalize(
        self,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
    ) -> GoogleMapsPlaceObservation:
        self._validate_provenance(record, mapping)
        page = self._require_dict(record.raw_payload.get("page"), "page")
        raw_html = page.get("html")
        if not isinstance(raw_html, str) or not raw_html:
            raise GoogleMapsNormalizationError("page.html must be non-empty")
        decoded_html = html.unescape(raw_html)
        structured = self._optional_dict(page.get("structured_data"))
        final_url = str(page.get("final_url") or record.source_url or "")
        title = page.get("title")
        name = self._text(structured.get("name")) or self._place_name(title)
        if name is None or name.casefold() == "google maps":
            raise GoogleMapsNormalizationError(
                "Google Maps place detail was not resolved"
            )
        location = self._location(
            decoded_html,
            " ".join(
                filter(
                    None,
                    [final_url, self._text(structured.get("detail_url"))],
                )
            ),
            mapping,
            use_url_coordinates=not bool(
                page.get("used_master_coordinates_for_viewport")
            ),
        )
        business_status, daily_status, open_now, raw_status = self._status(decoded_html)
        verification = (
            VerificationStatus.AUTO_VERIFIED
            if mapping.status is MappingStatus.CONFIRMED
            else VerificationStatus.PENDING_REVIEW
        )
        observed_local = record.crawled_at.astimezone(ZoneInfo(self.timezone_name))
        weekly = self._weekly_schedule(
            structured.get("aria_labels"), record, mapping, verification
        )
        today_schedule = self._today_schedule(weekly, observed_local.weekday())
        if business_status is BusinessStatus.ACTIVE and today_schedule is not None:
            daily_status = (
                DailyOpeningStatus.CLOSED_TODAY
                if today_schedule.closed
                else DailyOpeningStatus.OPEN_TODAY
            )
        opening = OpeningStatusObservation(
            observation_id=f"{record.source_record_id}:opening",
            run_id=record.run_id,
            place_id=mapping.entity_id,
            source_record_ids=[record.source_record_id],
            local_date=observed_local.date(),
            timezone=self.timezone_name,
            status=daily_status,
            opening_intervals=(today_schedule.intervals if today_schedule else []),
            is_24_hours=(today_schedule.open_24_hours if today_schedule else False),
            open_now=open_now,
            raw_status_text=raw_status,
            observed_at=record.crawled_at,
            verification_status=verification,
        )
        raw_menu_images = structured.get("menu_image_urls")
        if not raw_menu_images and mapping.attributes.get("verified_menu_image_url"):
            raw_menu_images = [mapping.attributes["verified_menu_image_url"]]
        menu_source = self._menu_source(
            structured.get("menu_url"),
            raw_menu_images,
            record,
            mapping,
            verification,
        )
        media = self._media(
            structured.get("image_urls"),
            record,
            mapping,
            verification,
            final_url,
        )
        raw_price_text = self._text(structured.get("price_text"))
        return GoogleMapsPlaceObservation(
            observation_id=f"{record.source_record_id}:place-status",
            run_id=record.run_id,
            place_id=mapping.entity_id,
            source_record_id=record.source_record_id,
            source_id=record.source_id,
            source_url=final_url,
            name=name,
            category=self._text(structured.get("category")),
            address=self._clean_address(structured.get("address")),
            phone=self._text(structured.get("phone")),
            website_url=self._http_url(structured.get("website_url")),
            location=location,
            business_status=business_status,
            opening=opening,
            weekly_opening=weekly,
            price_level=self._price_level(raw_price_text),
            raw_price_text=raw_price_text,
            menu_source=menu_source,
            media=media,
            observed_at=record.crawled_at,
            verification_status=verification,
        )

    def _validate_provenance(
        self,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
    ) -> None:
        if record.source_id != self.source_id or mapping.source_id != self.source_id:
            raise GoogleMapsNormalizationError("record and mapping must be Google Maps")
        if record.subject_type is not RecordSubjectType.OPENING_STATUS:
            raise GoogleMapsNormalizationError(
                "record is not an opening-status capture"
            )
        if record.subject_id != mapping.entity_id:
            raise GoogleMapsNormalizationError("record and mapping entity differ")
        if mapping.status in {MappingStatus.REJECTED, MappingStatus.PENDING_REVIEW}:
            raise GoogleMapsNormalizationError(
                "mapping is not eligible for normalization"
            )

    @staticmethod
    def _require_dict(value: object, field: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise GoogleMapsNormalizationError(f"{field} must be an object")
        return value

    @staticmethod
    def _optional_dict(value: object) -> dict[str, object]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _http_url(cls, value: object) -> str | None:
        text = cls._text(value)
        return text if text and text.startswith(("http://", "https://")) else None

    @classmethod
    def _clean_address(cls, value: object) -> str | None:
        text = cls._text(value)
        if text is None:
            return None
        return re.sub(r"^(?:Address|Địa chỉ):\s*", "", text, flags=re.IGNORECASE)

    @staticmethod
    def _place_name(title: object) -> str | None:
        if not isinstance(title, str):
            return None
        value = re.sub(r"\s*-\s*Google Maps\s*$", "", title).strip()
        return value or None

    def _location(
        self,
        raw_html: str,
        final_url: str,
        mapping: ExternalEntityMapping,
        *,
        use_url_coordinates: bool = True,
    ) -> GeoPoint | None:
        pairs = (
            self._url_coordinate_pattern.findall(final_url)
            if use_url_coordinates
            else []
        )
        pairs.extend(self._coordinate_pattern.findall(raw_html))
        coordinates = list(dict.fromkeys((float(a), float(b)) for a, b in pairs))
        coordinates = [
            item
            for item in coordinates
            if -90 <= item[0] <= 90 and -180 <= item[1] <= 180
        ]
        expected = self._expected_coordinates(mapping)
        if not coordinates:
            if expected is None:
                return None
            return GeoPoint(
                latitude=expected[0],
                longitude=expected[1],
                accuracy="verified_master_fallback",
                source="verified-master-data",
            )
        selected = (
            min(
                coordinates,
                key=lambda item: self._distance_km(item, expected),
            )
            if expected
            else coordinates[0]
        )
        return GeoPoint(
            latitude=selected[0],
            longitude=selected[1],
            accuracy="google_maps_place_page",
            source=self.source_id,
        )

    @staticmethod
    def _expected_coordinates(
        mapping: ExternalEntityMapping,
    ) -> tuple[float, float] | None:
        latitude = mapping.attributes.get("master_latitude")
        longitude = mapping.attributes.get("master_longitude")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return float(latitude), float(longitude)
        return None

    @staticmethod
    def _distance_km(
        left: tuple[float, float],
        right: tuple[float, float],
    ) -> float:
        lat1, lon1, lat2, lon2 = map(math.radians, (*left, *right))
        d_lat = lat2 - lat1
        d_lon = lon2 - lon1
        value = math.sin(d_lat / 2) ** 2 + (
            math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
        )
        return 6371.0088 * 2 * math.asin(math.sqrt(value))

    @staticmethod
    def _status(
        raw_html: str,
    ) -> tuple[BusinessStatus, DailyOpeningStatus, bool | None, str | None]:
        permanent = ("Đã đóng cửa vĩnh viễn", "Permanently closed")
        temporary = ("Tạm thời đóng cửa", "Temporarily closed")
        open_terms = ("Đang mở cửa", "Open now")
        closed_now_terms = ("Đã đóng cửa", "Closed now")
        if term := next((item for item in permanent if item in raw_html), None):
            return (
                BusinessStatus.PERMANENTLY_CLOSED,
                DailyOpeningStatus.PERMANENTLY_CLOSED,
                False,
                term,
            )
        if term := next((item for item in temporary if item in raw_html), None):
            return (
                BusinessStatus.TEMPORARILY_CLOSED,
                DailyOpeningStatus.TEMPORARILY_CLOSED,
                False,
                term,
            )
        if term := next((item for item in open_terms if item in raw_html), None):
            return BusinessStatus.ACTIVE, DailyOpeningStatus.OPEN_TODAY, True, term
        if term := next((item for item in closed_now_terms if item in raw_html), None):
            next_open = re.search(
                rf"{re.escape(term)}.{{0,80}}?("
                r"Mở cửa lúc\s+\d{1,2}:\d{2}(?:\s+Thứ\s+\d)?|"
                r"Opens?\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)",
                raw_html,
                flags=re.IGNORECASE,
            )
            raw_status = f"{term} · {next_open.group(1)}" if next_open else term
            # Closed now does not prove that the place is closed for the whole day.
            return BusinessStatus.ACTIVE, DailyOpeningStatus.UNKNOWN, False, raw_status
        return BusinessStatus.ACTIVE, DailyOpeningStatus.UNKNOWN, None, None

    def _weekly_schedule(
        self,
        raw_labels: object,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
        verification: VerificationStatus,
    ) -> WeeklyOpeningScheduleObservation | None:
        if not isinstance(raw_labels, list):
            return None
        day_names = {day.value.capitalize(): day for day in Weekday}
        schedules: dict[Weekday, DailyOpeningSchedule] = {}
        for raw_label in raw_labels:
            label = self._text(raw_label)
            if label is None:
                continue
            match = re.match(
                r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*(.+)$",
                label,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            weekday = day_names[match.group(1).capitalize()]
            details = re.sub(
                r",?\s*(?:Copy open hours|Hide open hours|Suggest.*)$",
                "",
                match.group(2),
                flags=re.IGNORECASE,
            ).strip()
            schedule = self._parse_day_schedule(weekday, details)
            if schedule is not None:
                schedules.setdefault(weekday, schedule)
        if not schedules:
            return None
        ordered = sorted(
            schedules.values(), key=lambda item: list(Weekday).index(item.day)
        )
        return WeeklyOpeningScheduleObservation(
            observation_id=f"{record.source_record_id}:weekly-opening",
            run_id=record.run_id,
            place_id=mapping.entity_id,
            source_record_ids=[record.source_record_id],
            timezone=self.timezone_name,
            days=ordered,
            observed_at=record.crawled_at,
            verification_status=verification,
        )

    def _parse_day_schedule(
        self, weekday: Weekday, details: str
    ) -> DailyOpeningSchedule | None:
        if re.search(r"\bClosed\b", details, flags=re.IGNORECASE):
            return DailyOpeningSchedule(day=weekday, closed=True)
        if re.search(r"Open 24 hours", details, flags=re.IGNORECASE):
            return DailyOpeningSchedule(day=weekday, open_24_hours=True)
        intervals = []
        for opens, closes in re.findall(
            r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s*(?:to|–|-)\s*"
            r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))",
            details,
            flags=re.IGNORECASE,
        ):
            opens_at = self._parse_clock_time(opens)
            closes_at = self._parse_clock_time(closes)
            intervals.append(
                OpeningInterval(
                    opens_at=opens_at,
                    closes_at=closes_at,
                    closes_next_day=closes_at <= opens_at,
                )
            )
        return (
            DailyOpeningSchedule(day=weekday, intervals=intervals)
            if intervals
            else None
        )

    @staticmethod
    def _parse_clock_time(value: str) -> time:
        normalized = re.sub(r"\s+", " ", value.strip().upper())
        pattern = "%I:%M %p" if ":" in normalized else "%I %p"
        return datetime.strptime(normalized, pattern).time()

    @staticmethod
    def _today_schedule(
        weekly: WeeklyOpeningScheduleObservation | None,
        weekday_index: int,
    ) -> DailyOpeningSchedule | None:
        if weekly is None:
            return None
        today = list(Weekday)[weekday_index]
        return next((item for item in weekly.days if item.day is today), None)

    @classmethod
    def _menu_source(
        cls,
        raw_url: object,
        raw_image_urls: object,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
        verification: VerificationStatus,
    ) -> MenuSourceObservation | None:
        menu_url = cls._http_url(raw_url)
        image_urls = cls._menu_image_url_list(raw_image_urls)
        if menu_url is None and not image_urls:
            return None
        source_type = (
            MenuSourceType.GOOGLE_MAPS_LINK
            if menu_url is not None
            else MenuSourceType.IMAGE
        )
        return MenuSourceObservation(
            observation_id=f"{record.source_record_id}:menu-source",
            run_id=record.run_id,
            place_id=mapping.entity_id,
            source_record_id=record.source_record_id,
            source_type=source_type,
            menu_url=menu_url,
            menu_image_urls=image_urls,
            observed_at=record.crawled_at,
            verification_status=verification,
        )

    @classmethod
    def _media(
        cls,
        raw_urls: object,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
        verification: VerificationStatus,
        source_url: str,
    ) -> PlaceMediaObservation | None:
        cover_url = cls._cover_image_url(raw_urls)
        if cover_url is None:
            return None
        assets = [PlaceMediaAsset(url=cover_url, role=PlaceMediaRole.COVER)]
        return PlaceMediaObservation(
            observation_id=f"{record.source_record_id}:media",
            run_id=record.run_id,
            place_id=mapping.entity_id,
            source_record_id=record.source_record_id,
            source_url=source_url,
            assets=assets,
            observed_at=record.crawled_at,
            verification_status=verification,
        )

    @classmethod
    def _url_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(
            dict.fromkeys(filter(None, (cls._http_url(item) for item in value)))
        )

    @classmethod
    def _menu_image_url_list(cls, value: object) -> list[str]:
        urls = cls._url_list(value)
        accepted: list[tuple[int, str]] = []
        for url in urls:
            dimensions = re.search(r"=w(\d+)-h(\d+)(?:-|$)", url)
            if dimensions and (
                int(dimensions.group(1)) < 128 or int(dimensions.group(2)) < 128
            ):
                continue
            area = (
                int(dimensions.group(1)) * int(dimensions.group(2)) if dimensions else 0
            )
            accepted.append((area, url))
        return [max(accepted)[1]] if accepted else []

    @classmethod
    def _cover_image_url(cls, value: object) -> str | None:
        urls = cls._url_list(value)
        for url in urls:
            dimensions = re.search(r"=w(\d+)-h(\d+)(?:-|$)", url)
            if dimensions and (
                int(dimensions.group(1)) < 128 or int(dimensions.group(2)) < 128
            ):
                continue
            return url
        return None

    @staticmethod
    def _price_level(raw_text: str | None) -> int | None:
        if raw_text is None:
            return None
        symbol_groups = re.findall(r"[$₫€£]+", raw_text)
        if symbol_groups:
            return min(len(max(symbol_groups, key=len)), 4)
        lowered = raw_text.casefold()
        labels = {
            "inexpensive": 1,
            "affordable": 1,
            "moderate": 2,
            "expensive": 3,
            "very expensive": 4,
        }
        return next(
            (level for label, level in labels.items() if label in lowered), None
        )


class NormalizedGoogleMapsWriter:
    """Immutable writer for normalized Maps observations."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, observation: GoogleMapsPlaceObservation) -> Path:
        destination = (
            self.root_directory
            / "entity=opening_status"
            / f"source={quote(observation.source_id, safe='-_.')}"
            / f"date={observation.observed_at.date().isoformat()}"
            / f"run={quote(observation.run_id, safe='-_.')}"
            / f"observation={quote(observation.observation_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Normalized observation already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(observation.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination
