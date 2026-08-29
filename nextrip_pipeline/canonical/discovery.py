from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.canonical.candidate import (
    CandidateExternalIdentity,
    CanonicalReplacementCandidate,
)
from nextrip_pipeline.canonical.models import (
    EntityCityVacancy,
    stable_identifier,
    stable_sha256,
)
from nextrip_pipeline.crawl.raw_writer import RawJsonWriter
from nextrip_pipeline.schemas import (
    EntityType,
    NexTripModel,
    RecordSubjectType,
    SourceRecord,
)


_MAX_RESULT_LIMIT = 50
_TRACKING_QUERY_PARAMETERS = {
    "authuser",
    "entry",
    "g_ep",
    "hl",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_GOOGLE_DATA_ID_PATTERN = re.compile(
    r"!1s(?P<token>"
    r"0x[0-9a-f]+:0x[0-9a-f]+"
    r"|ChI[A-Za-z0-9_-]+"
    r"|/g/[A-Za-z0-9_-]+"
    r")(?=!|[?&#]|$)",
    re.IGNORECASE,
)
_GOOGLE_G_PATH_PATTERN = re.compile(
    r"(?:^|/)g/(?P<token>[A-Za-z0-9_-]+)(?:/|$)",
    re.IGNORECASE,
)


class GoogleMapsDiscoveryPayloadError(ValueError):
    """Raised when raw search evidence is unsafe to turn into staged data."""


class CandidateStageAlreadyExistsError(FileExistsError):
    """Raised rather than overwrite an immutable discovery stage."""


class DiscoveryCandidateStatus(StrEnum):
    DISCOVERED = "discovered"


class StagedGoogleMapsCandidate(NexTripModel):
    """Unapproved search result awaiting detail capture and distinctness checks."""

    candidate: CanonicalReplacementCandidate
    source_url: HttpUrl
    result_position: int = Field(ge=1)
    card_text: str | None = None
    status: DiscoveryCandidateStatus = DiscoveryCandidateStatus.DISCOVERED

    @model_validator(mode="after")
    def preserve_external_identity(self) -> StagedGoogleMapsCandidate:
        urls = {
            str(identity.external_url)
            for identity in self.candidate.external_identities
            if identity.external_url is not None
        }
        if str(self.source_url) not in urls:
            raise ValueError("candidate must preserve its Maps source URL")
        return self


def google_maps_candidate_stage_payload(
    *,
    schema_version: str,
    stage_id: str,
    run_id: str,
    source_record_id: str,
    vacancy: EntityCityVacancy,
    query: str,
    result_limit: int,
    candidates: list[StagedGoogleMapsCandidate],
    candidate_entity_type: EntityType | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "stage_id": stage_id,
        "run_id": run_id,
        "source_record_id": source_record_id,
        "vacancy": vacancy.model_dump(mode="json"),
        "query": query,
        "result_limit": result_limit,
        "candidates": [item.model_dump(mode="json") for item in candidates],
    }
    # Preserve hashes for legacy same-type stages while making a cross-type
    # discovery pool explicit and independently auditable.
    if candidate_entity_type is not None:
        payload["candidate_entity_type"] = candidate_entity_type.value
    return payload


class GoogleMapsCandidateStage(NexTripModel):
    """Immutable, non-approved output of one bounded discovery search."""

    schema_version: str = "1.0.0"
    stage_id: str = Field(min_length=1)
    stage_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    discovered_at: AwareDatetime
    vacancy: EntityCityVacancy
    candidate_entity_type: EntityType | None = None
    query: str = Field(min_length=1)
    result_limit: int = Field(ge=1, le=_MAX_RESULT_LIMIT)
    candidates: list[StagedGoogleMapsCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_stage(self) -> GoogleMapsCandidateStage:
        expected_entity_type = self.candidate_entity_type or self.vacancy.entity_type
        _validate_candidate_vacancy_type(expected_entity_type, self.vacancy)
        candidate_keys = [item.candidate.candidate_key for item in self.candidates]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("staged candidate keys must be unique")
        positions = [item.result_position for item in self.candidates]
        if len(positions) != len(set(positions)):
            raise ValueError("staged result positions must be unique")
        if len(self.candidates) > self.result_limit:
            raise ValueError("staged candidates exceed the requested result_limit")
        for item in self.candidates:
            if item.candidate.entity_type is not expected_entity_type:
                raise ValueError(
                    "candidate entity type does not match discovery pool"
                )
            if item.candidate.city_id != self.vacancy.city_id:
                raise ValueError("candidate city does not match vacancy")
        expected_hash = stable_sha256(
            google_maps_candidate_stage_payload(
                schema_version=self.schema_version,
                stage_id=self.stage_id,
                run_id=self.run_id,
                source_record_id=self.source_record_id,
                vacancy=self.vacancy,
                query=self.query,
                result_limit=self.result_limit,
                candidates=self.candidates,
                candidate_entity_type=self.candidate_entity_type,
            )
        )
        if self.stage_hash != expected_hash:
            raise ValueError("stage_hash does not match discovery stage content")
        return self


class GoogleMapsDiscoveryCapture(Protocol):
    def fetch(
        self,
        vacancy: EntityCityVacancy,
        *,
        run_id: str,
        result_limit: int,
        search_term: str | None = None,
        candidate_entity_type: EntityType | None = None,
    ) -> SourceRecord: ...


@dataclass(frozen=True, slots=True)
class GoogleMapsDiscoveryRun:
    source_record: SourceRecord
    raw_path: Path
    stage: GoogleMapsCandidateStage
    stage_path: Path


class GoogleMapsCandidateStageWriter:
    """Persist each discovery stage once; an existing stage is never replaced."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, stage: GoogleMapsCandidateStage) -> Path:
        return (
            self.root_directory
            / _path_segment("city", stage.vacancy.city_id)
            / _path_segment("entity", stage.vacancy.entity_type.value)
            / _path_segment("vacancy", stage.vacancy.vacancy_id)
            / _path_segment("run", stage.run_id)
            / f"{_path_segment('stage', stage.stage_id)}.json"
        )

    def write(self, stage: GoogleMapsCandidateStage) -> Path:
        # Revalidation also verifies stage_hash if an unvalidated model was ever
        # constructed by an integration boundary.
        validated = GoogleMapsCandidateStage.model_validate(
            stage.model_dump(mode="python")
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized = (
            json.dumps(
                validated.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError as error:
            raise CandidateStageAlreadyExistsError(
                f"Candidate stage is immutable and already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        return destination


class GoogleMapsCandidateDiscovery:
    """Raw-first orchestration for one vacancy; no candidate is approved here."""

    def __init__(
        self,
        adapter: GoogleMapsDiscoveryCapture,
        raw_writer: RawJsonWriter,
        stage_writer: GoogleMapsCandidateStageWriter,
    ) -> None:
        self.adapter = adapter
        self.raw_writer = raw_writer
        self.stage_writer = stage_writer

    def run(
        self,
        vacancy: EntityCityVacancy,
        *,
        run_id: str,
        result_limit: int = 20,
        search_term: str | None = None,
        candidate_entity_type: EntityType | None = None,
    ) -> GoogleMapsDiscoveryRun:
        if isinstance(result_limit, bool) or not 1 <= result_limit <= _MAX_RESULT_LIMIT:
            raise ValueError(f"result_limit must be between 1 and {_MAX_RESULT_LIMIT}")
        _validate_candidate_vacancy_type(
            candidate_entity_type or vacancy.entity_type,
            vacancy,
        )
        fetch_kwargs: dict[str, object] = {
            "run_id": run_id,
            "result_limit": result_limit,
            "search_term": search_term,
        }
        if candidate_entity_type is not None:
            fetch_kwargs["candidate_entity_type"] = candidate_entity_type
        source_record = self.adapter.fetch(vacancy, **fetch_kwargs)
        # Evidence is persisted before parsing. A malformed page therefore
        # leaves an audit trail but can never create a partial candidate stage.
        raw_path = self.raw_writer.write(source_record)
        stage = stage_google_maps_candidates(
            source_record,
            vacancy,
            candidate_entity_type=candidate_entity_type,
        )
        stage_path = self.stage_writer.write(stage)
        return GoogleMapsDiscoveryRun(
            source_record=source_record,
            raw_path=raw_path,
            stage=stage,
            stage_path=stage_path,
        )


def stage_google_maps_candidates(
    record: SourceRecord,
    vacancy: EntityCityVacancy,
    *,
    candidate_entity_type: EntityType | None = None,
) -> GoogleMapsCandidateStage:
    """Strictly parse one raw result feed into unique, unapproved candidates."""

    request = _required_object(record.raw_payload, "request")
    effective_entity_type = _candidate_entity_type_from_request(
        request,
        vacancy,
        explicit=candidate_entity_type,
    )
    _validate_record_identity(
        record,
        vacancy,
        candidate_entity_type=effective_entity_type,
    )
    page = _required_object(record.raw_payload, "page")
    structured_data = _required_object(page, "structured_data")
    results = structured_data.get("search_results")
    if not isinstance(results, list):
        raise GoogleMapsDiscoveryPayloadError(
            "page.structured_data.search_results must be a list"
        )

    query = _required_text(request, "query")
    result_limit = _required_integer(request, "result_limit")
    if not 1 <= result_limit <= _MAX_RESULT_LIMIT:
        raise GoogleMapsDiscoveryPayloadError(
            f"request.result_limit must be between 1 and {_MAX_RESULT_LIMIT}"
        )
    _validate_request_vacancy(request, vacancy)
    if len(results) > result_limit:
        raise GoogleMapsDiscoveryPayloadError(
            "search results exceed the audited request.result_limit"
        )

    candidates: list[StagedGoogleMapsCandidate] = []
    seen_url_keys: set[str] = set()
    seen_positions: set[int] = set()
    for index, value in enumerate(results):
        if not isinstance(value, dict):
            raise GoogleMapsDiscoveryPayloadError(
                f"search_results[{index}] must be an object"
            )
        name = _required_text(value, "name", path=f"search_results[{index}]")
        source_url = _required_text(
            value,
            "url",
            path=f"search_results[{index}]",
        )
        url_key, external_id = _google_maps_identity(source_url)
        position = _required_integer(
            value,
            "position",
            path=f"search_results[{index}]",
        )
        if not 1 <= position <= result_limit:
            raise GoogleMapsDiscoveryPayloadError(
                f"search_results[{index}].position is outside result_limit"
            )
        card_text_value = value.get("card_text")
        if card_text_value is not None and not isinstance(card_text_value, str):
            raise GoogleMapsDiscoveryPayloadError(
                f"search_results[{index}].card_text must be a string or null"
            )
        card_text = card_text_value.strip() if card_text_value else None
        if url_key in seen_url_keys:
            continue
        if position in seen_positions:
            raise GoogleMapsDiscoveryPayloadError(
                f"search_results[{index}].position is duplicated"
            )
        seen_url_keys.add(url_key)
        seen_positions.add(position)
        candidate = CanonicalReplacementCandidate(
            candidate_key=stable_identifier("candidate", url_key),
            entity_type=effective_entity_type,
            city_id=vacancy.city_id,
            name=name,
            external_identities=[
                CandidateExternalIdentity(
                    source_id=record.source_id,
                    external_id=external_id,
                    external_url=source_url,
                )
            ],
        )
        candidates.append(
            StagedGoogleMapsCandidate(
                candidate=candidate,
                source_url=source_url,
                result_position=position,
                card_text=card_text,
            )
        )

    stage_id = stable_identifier(
        "candidate-stage",
        record.source_record_id,
        vacancy.vacancy_id,
    )
    candidates.sort(key=lambda item: item.result_position)
    payload = google_maps_candidate_stage_payload(
        schema_version="1.0.0",
        stage_id=stage_id,
        run_id=record.run_id,
        source_record_id=record.source_record_id,
        vacancy=vacancy,
        query=query,
        result_limit=result_limit,
        candidates=candidates,
        candidate_entity_type=(
            effective_entity_type
            if effective_entity_type is not vacancy.entity_type
            else None
        ),
    )
    return GoogleMapsCandidateStage(
        schema_version="1.0.0",
        stage_id=stage_id,
        stage_hash=stable_sha256(payload),
        run_id=record.run_id,
        source_record_id=record.source_record_id,
        discovered_at=record.crawled_at,
        vacancy=vacancy,
        candidate_entity_type=(
            effective_entity_type
            if effective_entity_type is not vacancy.entity_type
            else None
        ),
        query=query,
        result_limit=result_limit,
        candidates=candidates,
    )


def _validate_record_identity(
    record: SourceRecord,
    vacancy: EntityCityVacancy,
    *,
    candidate_entity_type: EntityType,
) -> None:
    if record.source_id != "google-maps-web":
        raise GoogleMapsDiscoveryPayloadError(
            "candidate discovery requires google-maps-web evidence"
        )
    if record.subject_type is not RecordSubjectType.PLACE:
        raise GoogleMapsDiscoveryPayloadError(
            "candidate discovery source record must have place subject_type"
        )
    if record.subject_id != vacancy.vacancy_id:
        raise GoogleMapsDiscoveryPayloadError(
            "source record belongs to another canonical vacancy"
        )
    if record.entity_type is not candidate_entity_type:
        raise GoogleMapsDiscoveryPayloadError(
            "source record entity type does not match candidate discovery pool"
        )


def _validate_request_vacancy(
    request: dict[str, object],
    vacancy: EntityCityVacancy,
) -> None:
    expected = {
        "vacancy_id": vacancy.vacancy_id,
        "retired_place_id": vacancy.retired_place_id,
        "entity_type": vacancy.entity_type.value,
        "city_id": vacancy.city_id,
    }
    for field, expected_value in expected.items():
        if request.get(field) != expected_value:
            raise GoogleMapsDiscoveryPayloadError(
                f"request.{field} does not match canonical vacancy"
            )


def _candidate_entity_type_from_request(
    request: dict[str, object],
    vacancy: EntityCityVacancy,
    *,
    explicit: EntityType | None,
) -> EntityType:
    raw = request.get("candidate_entity_type")
    if raw is None:
        requested = vacancy.entity_type
    elif isinstance(raw, str):
        try:
            requested = EntityType(raw)
        except ValueError as error:
            raise GoogleMapsDiscoveryPayloadError(
                "request.candidate_entity_type is unsupported"
            ) from error
    else:
        raise GoogleMapsDiscoveryPayloadError(
            "request.candidate_entity_type must be a string"
        )
    if explicit is not None and requested is not explicit:
        raise GoogleMapsDiscoveryPayloadError(
            "request.candidate_entity_type does not match the requested pool"
        )
    _validate_candidate_vacancy_type(requested, vacancy)
    return requested


def _validate_candidate_vacancy_type(
    candidate_entity_type: EntityType,
    vacancy: EntityCityVacancy,
) -> None:
    if candidate_entity_type is vacancy.entity_type:
        return
    if (
        vacancy.entity_type is EntityType.NIGHTLIFE
        and candidate_entity_type in {EntityType.CAFE, EntityType.RESTAURANT}
    ):
        return
    raise ValueError(
        "cross-type replacement supports only cafe/restaurant candidates "
        "for nightlife vacancies"
    )


def _required_object(
    value: dict[str, object],
    field: str,
) -> dict[str, object]:
    result = value.get(field)
    if not isinstance(result, dict):
        raise GoogleMapsDiscoveryPayloadError(f"{field} must be an object")
    return result


def _required_text(
    value: dict[str, object],
    field: str,
    *,
    path: str | None = None,
) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        prefix = path or ""
        separator = "." if prefix else ""
        raise GoogleMapsDiscoveryPayloadError(
            f"{prefix}{separator}{field} must be a non-empty string"
        )
    return result.strip()


def _required_integer(
    value: dict[str, object],
    field: str,
    *,
    path: str | None = None,
) -> int:
    result = value.get(field)
    if isinstance(result, bool) or not isinstance(result, int):
        prefix = path or ""
        separator = "." if prefix else ""
        raise GoogleMapsDiscoveryPayloadError(
            f"{prefix}{separator}{field} must be an integer"
        )
    return result


def _google_maps_identity(value: str) -> tuple[str, str | None]:
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise GoogleMapsDiscoveryPayloadError(
            "candidate URL must use HTTP(S)"
        )
    if host not in {"google.com", "www.google.com", "maps.google.com"}:
        raise GoogleMapsDiscoveryPayloadError(
            "candidate URL must use an official Google Maps host"
        )
    if "/maps/place/" not in parsed.path:
        raise GoogleMapsDiscoveryPayloadError(
            "candidate URL must be a Google Maps place URL"
        )
    external_id = _google_maps_external_id(value, parsed.query, parsed.path)
    if external_id is not None:
        # The key remains URL-derived, but stable place identity wins over
        # mutable slug, viewport, locale, and tracking fragments.
        return f"google-place:{external_id}", external_id
    normalized_query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_QUERY_PARAMETERS
            and not key.casefold().startswith("utm_")
        )
    )
    normalized_path = parsed.path.rstrip("/")
    return (
        urlunsplit(
            (
                "https",
                "google.com",
                normalized_path,
                normalized_query,
                "",
            )
        ),
        None,
    )


def _google_maps_external_id(
    source_url: str,
    query: str,
    path: str,
) -> str | None:
    decoded_url = unquote(source_url)
    if match := _GOOGLE_DATA_ID_PATTERN.search(decoded_url):
        return match.group("token")
    if match := _GOOGLE_G_PATH_PATTERN.search(unquote(path)):
        return f"/g/{match.group('token')}"
    query_values = dict(parse_qsl(query, keep_blank_values=False))
    for key in ("query_place_id", "place_id"):
        if value := query_values.get(key):
            return value.removeprefix("place_id:")
    if cid := query_values.get("cid"):
        if cid.isdecimal():
            return f"cid:{cid}"
    return None


def _path_segment(prefix: str, value: str) -> str:
    return f"{prefix}={quote(value, safe='-_.')}"
