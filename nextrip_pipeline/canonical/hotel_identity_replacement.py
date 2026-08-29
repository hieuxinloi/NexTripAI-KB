from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.preprocessing.hotel_price import parse_price_amount
from nextrip_pipeline.schemas import (
    EntityType,
    NexTripModel,
    RecordSubjectType,
    SourceRecord,
)


_SHA256 = r"^[a-f0-9]{64}$"
_HOTEL_ID = r"^hotel_(?:dn|qn)_[0-9]{3,}$"
_PROPERTY_ID = re.compile(r"(?:[?&;]|^)search=100-(\d+)(?:;|&|$)")
_DATE_INTERVAL = re.compile(r"(?:[?&;]|^)dr-(\d{8})-(\d{8})(?:;|&|$)")
_TRIVAGO_MCP_HOST = "mcp.trivago.com"
_TRIVAGO_ACCOMMODATION_HOSTS = frozenset({"trivago.vn", "www.trivago.vn"})
_REPLACEMENT_HISTORY_KEY = "hotel_identity_replacement_batches"


class HotelIdentityReplacementError(ValueError):
    """Raised when a same-ID hotel identity replacement is not safe."""


class ReplacementCoordinates(NexTripModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @model_validator(mode="before")
    @classmethod
    def accept_master_coordinate_names(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        if "latitude" not in normalized and "lat" in normalized:
            normalized["latitude"] = normalized.pop("lat")
        if "longitude" not in normalized and "lng" in normalized:
            normalized["longitude"] = normalized.pop("lng")
        return normalized


class ProposalArtifact(NexTripModel):
    path: str = Field(min_length=1)
    file_sha256: str = Field(pattern=_SHA256)


class ReviewedLocationEvidence(NexTripModel):
    """Human-reviewed location evidence used when provider coordinates are wrong."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    target_id: str = Field(pattern=_HOTEL_ID)
    new_name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    reviewed_coordinates: ReplacementCoordinates
    provider_coordinates: ReplacementCoordinates
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    reason: str = Field(min_length=1)
    supporting_urls: list[HttpUrl] = Field(min_length=1)


class HotelIdentityReplacementProposal(NexTripModel):
    target_id: str = Field(pattern=_HOTEL_ID)
    old_name: str = Field(min_length=1)
    new_name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    address: str = Field(min_length=1)
    coordinates: ReplacementCoordinates
    category: str = Field(min_length=1)
    star_rating: int | None = Field(default=None, ge=0, le=5)
    review_rating: float | None = Field(default=None, ge=0, le=10)
    review_count: int | None = Field(default=None, ge=0)
    amenities: list[str] = Field(default_factory=list)
    external_id: str = Field(min_length=1)
    property_id: str = Field(pattern=r"^\d+$")
    accommodation_url: str = Field(min_length=1)
    main_image: str | None = None
    seller: str = Field(min_length=1)
    nightly_amount: int = Field(gt=0)
    total_amount: int = Field(gt=0)
    currency: Literal["VND"] = "VND"
    check_in: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    check_out: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    adults: int = Field(ge=1)
    rooms: int = Field(ge=1)
    raw_path: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=_SHA256)
    location_evidence: ProposalArtifact | None = None
    selection_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_context(self) -> HotelIdentityReplacementProposal:
        if self.old_name.casefold() == self.new_name.casefold():
            raise ValueError("replacement must change the hotel identity")
        try:
            check_in = date.fromisoformat(self.check_in)
            check_out = date.fromisoformat(self.check_out)
        except ValueError as error:
            raise ValueError(
                "replacement dates must be valid calendar dates"
            ) from error
        if check_out <= check_in:
            raise ValueError("replacement check_out must be after check_in")
        if self.total_amount < self.nightly_amount:
            raise ValueError("replacement total amount cannot be below nightly amount")
        if _property_id(self.accommodation_url) != self.property_id:
            raise ValueError("property_id does not match accommodation_url")
        return self


class HotelIdentityReplacementConfig(NexTripModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    approved_by: str = Field(min_length=1)
    approved_at: AwareDatetime
    reason: str = Field(min_length=1)
    proposal_artifacts: list[ProposalArtifact] = Field(min_length=1)
    expected_target_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> HotelIdentityReplacementConfig:
        paths = [item.path for item in self.proposal_artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("proposal artifact paths must be unique")
        if self.expected_target_ids != sorted(set(self.expected_target_ids)):
            raise ValueError("expected_target_ids must be sorted and unique")
        if any(
            re.fullmatch(_HOTEL_ID, item) is None for item in self.expected_target_ids
        ):
            raise ValueError("expected_target_ids contains an invalid hotel ID")
        return self


class ValidatedHotelReplacement(NexTripModel):
    proposal: HotelIdentityReplacementProposal
    source_record_id: str
    crawled_at: AwareDatetime
    distance: str | None = None
    location_evidence: ReviewedLocationEvidence | None = None


class HotelReplacementRunResult(NexTripModel):
    mode: Literal["check", "apply"]
    batch_id: str
    batch_hash: str = Field(pattern=_SHA256)
    target_count: int = Field(ge=1)
    target_ids: list[str]
    old_names: dict[str, str]
    new_names: dict[str, str]
    master_path: str
    master_before_sha256: str = Field(pattern=_SHA256)
    master_after_sha256: str = Field(pattern=_SHA256)
    removed_active_path_count: int = Field(ge=0)
    removed_traffic_cache_entries: int = Field(ge=0)
    removed_search_review_overrides: list[str]
    audit_path: str | None = None


def load_and_validate_replacements(
    *,
    repository_root: str | Path,
    config_path: str | Path,
    master_path: str | Path,
    current_mapping_directory: str | Path,
) -> tuple[
    HotelIdentityReplacementConfig,
    list[ValidatedHotelReplacement],
    dict[str, Any],
    bytes,
]:
    root = Path(repository_root).resolve()
    config_destination = _resolve_path(root, config_path)
    config = HotelIdentityReplacementConfig.model_validate_json(
        config_destination.read_bytes()
    )

    proposals: list[HotelIdentityReplacementProposal] = []
    for artifact in config.proposal_artifacts:
        proposal_path = _resolve_path(root, artifact.path)
        raw_bytes = proposal_path.read_bytes()
        if _sha256(raw_bytes) != artifact.file_sha256:
            raise HotelIdentityReplacementError(
                f"proposal artifact hash mismatch: {artifact.path}"
            )
        payload = json.loads(raw_bytes)
        if not isinstance(payload, list):
            raise HotelIdentityReplacementError(
                f"proposal artifact must contain a JSON list: {artifact.path}"
            )
        proposals.extend(
            HotelIdentityReplacementProposal.model_validate(item) for item in payload
        )

    target_ids = [item.target_id for item in proposals]
    if sorted(target_ids) != config.expected_target_ids:
        raise HotelIdentityReplacementError(
            "proposal target IDs do not match the approved expected_target_ids"
        )
    _require_unique("target_id", target_ids)
    _require_unique("external_id", [item.external_id for item in proposals])
    _require_unique("property_id", [item.property_id for item in proposals])
    _require_unique(
        "normalized new_name", [_normalize_text(item.new_name) for item in proposals]
    )
    _require_unique(
        "normalized address", [_normalize_text(item.address) for item in proposals]
    )

    master_destination = _resolve_path(root, master_path)
    master_bytes = master_destination.read_bytes()
    try:
        master_document = json.loads(master_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementError(
            f"cannot read hotel master: {error}"
        ) from error
    records = master_document.get("data") if isinstance(master_document, dict) else None
    if not isinstance(records, list) or not all(
        isinstance(item, dict) for item in records
    ):
        raise HotelIdentityReplacementError(
            "hotel master must contain an object data list"
        )

    record_by_id = {str(item.get("id")): item for item in records}
    if len(record_by_id) != len(records):
        raise HotelIdentityReplacementError("hotel master contains duplicate IDs")

    target_set = set(target_ids)
    validated: list[ValidatedHotelReplacement] = []
    for proposal in proposals:
        current = record_by_id.get(proposal.target_id)
        if current is None:
            raise HotelIdentityReplacementError(
                f"replacement target is absent from hotel master: {proposal.target_id}"
            )
        replacement_marker = current.get("identity_replacement")
        current_name = str(current.get("name") or "")
        already_replaced = (
            isinstance(replacement_marker, Mapping)
            and str(replacement_marker.get("external_id") or "") == proposal.external_id
            and _normalize_text(current_name) == _normalize_text(proposal.new_name)
        )
        if not already_replaced and current_name != proposal.old_name:
            raise HotelIdentityReplacementError(
                f"old_name mismatch for {proposal.target_id}: "
                f"master={current_name!r}, approved={proposal.old_name!r}"
            )
        if str(current.get("city") or "") != proposal.city:
            raise HotelIdentityReplacementError(
                f"replacement cannot move cities: {proposal.target_id}"
            )
        validated.append(_validate_raw_evidence(root, proposal))

    _validate_master_collisions(records, proposals, target_set)
    mapping_directory = _resolve_path(root, current_mapping_directory)
    _validate_mapping_collisions(mapping_directory, proposals, target_set)
    return config, validated, master_document, master_bytes


def replace_hotel_identities(
    *,
    master_path: str | Path,
    repository_root: str | Path = ".",
    config_path: str | Path = "config/hotel-identity-replacements.json",
    current_root: str | Path = "data/current",
    search_review_path: str | Path = "config/trivago-search-review.json",
    audit_root: str | Path = "data/audit/hotel_identity_replacements",
    apply: bool = False,
    now: datetime | None = None,
) -> HotelReplacementRunResult:
    root = Path(repository_root).resolve()
    master_destination = _resolve_path(root, master_path)
    current_destination = _resolve_path(root, current_root)
    config, validated, master_document, master_before = load_and_validate_replacements(
        repository_root=root,
        config_path=config_path,
        master_path=master_path,
        current_mapping_directory=current_destination / "trivago_mappings",
    )
    proposals = [item.proposal for item in validated]
    proposal_by_id = {item.target_id: item for item in proposals}
    validated_by_id = {item.proposal.target_id: item for item in validated}
    batch_payload = {
        "schema_version": config.schema_version,
        "approved_by": config.approved_by,
        "approved_at": config.approved_at.isoformat(),
        "reason": config.reason,
        "proposal_artifacts": [
            item.model_dump(mode="json") for item in config.proposal_artifacts
        ],
        "proposals": [
            item.model_dump(mode="json", exclude_none=True)
            for item in sorted(proposals, key=lambda value: value.target_id)
        ],
    }
    batch_hash = _stable_sha256(batch_payload)
    batch_id = f"hotel-identity-replacement-{batch_hash[:20]}"

    audit_directory = _resolve_path(root, audit_root)
    audit_path = audit_directory / f"batch={batch_hash[:20]}.json"
    intent_path = audit_directory / f"intent={batch_hash[:20]}.json"
    marker_states = [
        (
            isinstance(record.get("identity_replacement"), Mapping)
            and record["identity_replacement"].get("batch_hash") == batch_hash
        )
        for record in master_document["data"]
        if str(record.get("id")) in proposal_by_id
    ]
    if any(marker_states) and not all(marker_states):
        raise HotelIdentityReplacementError(
            "hotel master contains a partially applied replacement batch"
        )

    records = master_document["data"]
    recovery_preimage: tuple[dict[str, Any], bytes] | None = None
    recovery_source: Literal["intent", "git"] | None = None
    if marker_states and all(marker_states) and not audit_path.is_file():
        if intent_path.is_file():
            recovery_preimage = _recover_master_preimage_from_intent(
                root=root,
                intent_path=intent_path,
                master_path=master_destination,
                batch_id=batch_id,
                batch_hash=batch_hash,
                proposals=proposals,
            )
            recovery_source = "intent"
        else:
            recovery_preimage = _recover_master_preimage_from_git(
                root=root,
                master_path=master_destination,
                proposals=proposals,
            )
            recovery_source = "git"
    old_master_document = (
        recovery_preimage[0] if recovery_preimage is not None else master_document
    )
    audit_master_before = (
        recovery_preimage[1] if recovery_preimage is not None else master_before
    )
    old_record_by_id = {
        str(record["id"]): json.loads(json.dumps(record, ensure_ascii=False))
        for record in old_master_document["data"]
        if str(record.get("id")) in proposal_by_id
    }
    replacement_records = {
        place_id: _build_master_record(
            validated_by_id[place_id],
            config=config,
            batch_id=batch_id,
            batch_hash=batch_hash,
        )
        for place_id in proposal_by_id
    }
    replaced_records = [
        replacement_records.get(str(record.get("id")), record) for record in records
    ]
    completed_retry = (
        bool(marker_states) and all(marker_states) and audit_path.is_file()
    )
    updated_metadata = dict(master_document.get("metadata", {}))
    if not completed_retry:
        updated_metadata = _append_replacement_batch_metadata(
            updated_metadata,
            records=records,
            batch_id=batch_id,
            batch_hash=batch_hash,
            approved_at=config.approved_at.isoformat(),
            replacement_count=len(proposals),
        )
    updated_master = {
        **master_document,
        "metadata": updated_metadata,
        "data": replaced_records,
    }
    newline = "\r\n" if b"\r\n" in master_before else "\n"
    master_after = _json_bytes(updated_master, newline=newline)

    base_result = {
        "mode": "apply" if apply else "check",
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "target_count": len(proposals),
        "target_ids": sorted(proposal_by_id),
        "old_names": {
            place_id: proposal_by_id[place_id].old_name
            for place_id in sorted(proposal_by_id)
        },
        "new_names": {
            place_id: proposal_by_id[place_id].new_name
            for place_id in sorted(proposal_by_id)
        },
        "master_path": _relative_text(root, master_destination),
        "master_before_sha256": _sha256(audit_master_before),
        "master_after_sha256": _sha256(master_after),
        "removed_active_path_count": 0,
        "removed_traffic_cache_entries": 0,
        "removed_search_review_overrides": [],
        "audit_path": None,
    }
    if not apply:
        return HotelReplacementRunResult.model_validate(base_result)

    if marker_states and all(marker_states):
        if audit_path.is_file():
            _validate_completed_master(
                master_document,
                replacement_records=replacement_records,
                batch_id=batch_id,
                batch_hash=batch_hash,
                replacement_count=len(proposals),
            )
            completed_audit = _validate_completed_audit(
                audit_path,
                root=root,
                batch_id=batch_id,
                batch_hash=batch_hash,
                master_path=_relative_text(root, master_destination),
                config=config,
                proposals=proposals,
                replacement_records=replacement_records,
            )
            _validate_completed_projections(
                current_root=current_destination,
                proposals=proposals,
                completed_audit=completed_audit,
            )
            # A verified completed retry must not delete freshly rebuilt current
            # projections that now legitimately belong to the replacement hotels.
            return HotelReplacementRunResult.model_validate(
                {
                    **base_result,
                    "audit_path": _relative_text(root, audit_path),
                }
            )
        if master_before != master_after:
            raise HotelIdentityReplacementError(
                "partially applied master differs from the reviewed replacement output"
            )
    else:
        intent_document = _build_replacement_intent(
            batch_id=batch_id,
            batch_hash=batch_hash,
            approved_at=config.approved_at.isoformat(),
            master_path=_relative_text(root, master_destination),
            master_preimage=master_before,
            proposals=proposals,
        )
        # Persist the exact preimage before the first mutable write. If the
        # process stops after replacing master but before writing its completion
        # audit, a retry no longer depends on the repository's Git HEAD.
        _write_immutable_json(intent_path, intent_document)
        _atomic_replace_bytes(master_destination, master_after)
    removed_overrides = _remove_search_review_overrides(
        _resolve_path(root, search_review_path), set(proposal_by_id)
    )
    removed_paths = _remove_active_projections(
        root=root,
        current_root=current_destination,
        target_ids=set(proposal_by_id),
    )
    removed_cache_entries = _remove_traffic_cache_entries(
        current_destination / "traffic" / "cache.sqlite3", set(proposal_by_id)
    )

    applied_at = now or datetime.now(timezone.utc)
    if applied_at.tzinfo is None or applied_at.utcoffset() is None:
        raise HotelIdentityReplacementError("now must be timezone-aware")
    audit_values: dict[str, Any] = {
        "schema_version": "1.0.0",
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "approved_by": config.approved_by,
        "approved_at": config.approved_at.isoformat(),
        "applied_at": applied_at.isoformat(),
        "reason": config.reason,
        "master_path": _relative_text(root, master_destination),
        "master_before_sha256": _sha256(audit_master_before),
        "master_after_sha256": _sha256(master_after),
        "replacements": [
            {
                "target_id": proposal.target_id,
                "old_identity": _identity_audit(old_record_by_id[proposal.target_id]),
                "new_identity": _identity_audit(
                    replacement_records[proposal.target_id]
                ),
                "external_id": proposal.external_id,
                "property_id": proposal.property_id,
                "raw_path": proposal.raw_path,
                "raw_sha256": proposal.raw_sha256,
                "location_evidence": (
                    proposal.location_evidence.model_dump(mode="json")
                    if proposal.location_evidence is not None
                    else None
                ),
                "old_record_sha256": _stable_sha256(
                    old_record_by_id[proposal.target_id]
                ),
                "new_record_sha256": _stable_sha256(
                    replacement_records[proposal.target_id]
                ),
            }
            for proposal in sorted(proposals, key=lambda value: value.target_id)
        ],
        "removed_active_paths": removed_paths,
        "removed_traffic_cache_entries": removed_cache_entries,
        "removed_search_review_overrides": removed_overrides,
        "recovered_after_partial_apply": recovery_preimage is not None,
        "recovery_source": recovery_source,
        "intent_path": (
            _relative_text(root, intent_path) if intent_path.is_file() else None
        ),
        "intent_sha256": (
            _sha256(intent_path.read_bytes()) if intent_path.is_file() else None
        ),
        "history_policy": (
            "Immutable raw, normalized, quality, decision, and run artifacts are "
            "retained for audit only; all ID-addressable current projections were "
            "invalidated before rebuilding the new identities."
        ),
    }
    audit_hash = _stable_sha256(audit_values)
    audit_document = {
        **audit_values,
        "audit_id": f"hotel-identity-replacement-audit-{audit_hash[:20]}",
        "audit_hash": audit_hash,
    }
    _write_immutable_json(audit_path, audit_document)
    return HotelReplacementRunResult.model_validate(
        {
            **base_result,
            "removed_active_path_count": len(removed_paths),
            "removed_traffic_cache_entries": removed_cache_entries,
            "removed_search_review_overrides": removed_overrides,
            "audit_path": _relative_text(root, audit_path),
        }
    )


def _validate_raw_evidence(
    root: Path, proposal: HotelIdentityReplacementProposal
) -> ValidatedHotelReplacement:
    raw_path = _resolve_path(root, proposal.raw_path)
    raw_bytes = raw_path.read_bytes()
    if _sha256(raw_bytes) != proposal.raw_sha256:
        raise HotelIdentityReplacementError(
            f"raw evidence hash mismatch for {proposal.target_id}"
        )
    try:
        source = SourceRecord.model_validate_json(raw_bytes)
    except ValueError as error:
        raise HotelIdentityReplacementError(
            f"invalid SourceRecord evidence for {proposal.target_id}: {error}"
        ) from error
    actual_content_hash = compute_content_hash(source.raw_payload)
    if source.content_hash.casefold() != actual_content_hash:
        raise HotelIdentityReplacementError(
            f"raw SourceRecord content_hash mismatch for {proposal.target_id}"
        )
    if (
        source.source_id != "trivago-mcp"
        or source.subject_type is not RecordSubjectType.HOTEL_PRICE
        or source.entity_type is not EntityType.HOTEL
        or source.subject_id != proposal.target_id
    ):
        raise HotelIdentityReplacementError(
            f"raw evidence ownership mismatch for {proposal.target_id}"
        )
    if source.http_status != 200:
        raise HotelIdentityReplacementError(
            f"raw evidence HTTP status is not 200 for {proposal.target_id}"
        )
    source_url = urlsplit(str(source.source_url or ""))
    try:
        source_port = source_url.port
    except ValueError as error:
        raise HotelIdentityReplacementError(
            f"raw evidence has an invalid MCP URL for {proposal.target_id}"
        ) from error
    if (
        source_url.scheme.casefold() != "https"
        or (source_url.hostname or "").casefold() != _TRIVAGO_MCP_HOST
        or source_port not in (None, 443)
        or source_url.username is not None
        or source_url.password is not None
        or source_url.path.rstrip("/") != "/mcp"
    ):
        raise HotelIdentityReplacementError(
            f"raw evidence is not from the Trivago MCP endpoint: {proposal.target_id}"
        )

    request = _mapping(source.raw_payload.get("request"), "raw_payload.request")
    if request.get("tool") != "trivago-accommodation-search":
        raise HotelIdentityReplacementError(
            f"raw evidence tool mismatch for {proposal.target_id}"
        )
    if request.get("entity_id") != proposal.target_id:
        raise HotelIdentityReplacementError(
            f"raw evidence request entity mismatch for {proposal.target_id}"
        )
    if request.get("search_strategy") != "name":
        raise HotelIdentityReplacementError(
            f"raw evidence is not an exact-name search for {proposal.target_id}"
        )
    arguments = _mapping(request.get("arguments"), "request.arguments")
    expected_arguments = {
        "arrival": proposal.check_in,
        "departure": proposal.check_out,
        "adults": proposal.adults,
        "rooms": proposal.rooms,
        "children": 0,
        "country": "VN",
        "currency": proposal.currency,
    }
    for key, expected in expected_arguments.items():
        if arguments.get(key) != expected:
            raise HotelIdentityReplacementError(
                f"raw evidence {key} mismatch for {proposal.target_id}"
            )
    query = str(arguments.get("query") or "")
    query_key = _normalize_text(query)
    name_key = _normalize_text(proposal.new_name)
    city_key = _normalize_text(proposal.city)
    if (
        not (query_key == name_key or query_key.startswith(f"{name_key} "))
        or city_key not in query_key
    ):
        raise HotelIdentityReplacementError(
            f"raw evidence query does not target the replacement for {proposal.target_id}"
        )

    response = _mapping(source.raw_payload.get("response"), "raw_payload.response")
    result = _mapping(response.get("result"), "response.result")
    structured = _mapping(result.get("structuredContent"), "result.structuredContent")
    accommodations = structured.get("accommodations")
    if not isinstance(accommodations, list) or not all(
        isinstance(item, Mapping) for item in accommodations
    ):
        raise HotelIdentityReplacementError(
            f"raw evidence accommodations are invalid for {proposal.target_id}"
        )
    matches = [
        dict(item)
        for item in accommodations
        if str(item.get("accommodation_id") or "") == proposal.external_id
    ]
    if len(matches) != 1 or len(accommodations) != 1:
        raise HotelIdentityReplacementError(
            f"exact evidence must contain exactly one candidate for {proposal.target_id}"
        )
    candidate = matches[0]
    comparisons = {
        "accommodation_name": proposal.new_name,
        "accommodation_url": proposal.accommodation_url,
        "advertisers": proposal.seller,
        "arrival": proposal.check_in,
        "departure": proposal.check_out,
        "currency": proposal.currency,
    }
    for key, expected in comparisons.items():
        actual = str(candidate.get(key) or "").replace("\\u0026", "&")
        if key == "accommodation_name":
            matches_expected = _normalize_text(actual) == _normalize_text(expected)
        else:
            matches_expected = actual == expected
        if not matches_expected:
            raise HotelIdentityReplacementError(
                f"raw candidate {key} mismatch for {proposal.target_id}"
            )
    _validate_trivago_accommodation_url(proposal)
    nightly = int(parse_price_amount(candidate.get("price_per_night"), "VND"))
    total = int(parse_price_amount(candidate.get("price_per_stay"), "VND"))
    if (nightly, total) != (proposal.nightly_amount, proposal.total_amount):
        raise HotelIdentityReplacementError(
            f"raw candidate price mismatch for {proposal.target_id}"
        )
    location_evidence = _validate_location_evidence(root, proposal, candidate)
    _require_optional_number_match(
        candidate.get("hotel_rating"),
        proposal.star_rating,
        "hotel_rating",
        proposal.target_id,
    )
    _require_optional_number_match(
        candidate.get("review_rating"),
        proposal.review_rating,
        "review_rating",
        proposal.target_id,
    )
    raw_review_count = candidate.get("review_count")
    parsed_review_count = (
        int(re.sub(r"[^0-9]", "", str(raw_review_count)))
        if raw_review_count not in (None, "")
        else None
    )
    if parsed_review_count != proposal.review_count:
        raise HotelIdentityReplacementError(
            f"raw candidate review_count mismatch for {proposal.target_id}"
        )
    raw_image = str(candidate.get("main_image") or "") or None
    if raw_image != proposal.main_image:
        raise HotelIdentityReplacementError(
            f"raw candidate main_image mismatch for {proposal.target_id}"
        )
    raw_amenities = _normalize_text(str(candidate.get("top_amenities") or ""))
    if any(_normalize_text(value) not in raw_amenities for value in proposal.amenities):
        raise HotelIdentityReplacementError(
            f"proposal amenities are not supported by raw evidence: {proposal.target_id}"
        )
    country_city = _normalize_text(str(candidate.get("country_city") or ""))
    if _normalize_text(proposal.city) not in country_city:
        raise HotelIdentityReplacementError(
            f"raw candidate city mismatch for {proposal.target_id}"
        )
    return ValidatedHotelReplacement(
        proposal=proposal,
        source_record_id=source.source_record_id,
        crawled_at=source.crawled_at,
        distance=str(candidate.get("distance") or "") or None,
        location_evidence=location_evidence,
    )


def _validate_location_evidence(
    root: Path,
    proposal: HotelIdentityReplacementProposal,
    candidate: Mapping[str, Any],
) -> ReviewedLocationEvidence | None:
    reference = proposal.location_evidence
    if reference is None:
        _require_float_match(candidate.get("latitude"), proposal.coordinates.latitude)
        _require_float_match(candidate.get("longitude"), proposal.coordinates.longitude)
        return None

    evidence_path = _resolve_path(root, reference.path)
    evidence_bytes = evidence_path.read_bytes()
    if _sha256(evidence_bytes) != reference.file_sha256:
        raise HotelIdentityReplacementError(
            f"location evidence hash mismatch for {proposal.target_id}"
        )
    try:
        evidence = ReviewedLocationEvidence.model_validate_json(evidence_bytes)
    except ValueError as error:
        raise HotelIdentityReplacementError(
            f"invalid location evidence for {proposal.target_id}: {error}"
        ) from error

    if evidence.target_id != proposal.target_id or _normalize_text(
        evidence.new_name
    ) != _normalize_text(proposal.new_name):
        raise HotelIdentityReplacementError(
            f"location evidence ownership mismatch for {proposal.target_id}"
        )
    if evidence.address != proposal.address:
        raise HotelIdentityReplacementError(
            f"location evidence address mismatch for {proposal.target_id}"
        )
    _require_float_match(
        proposal.coordinates.latitude,
        evidence.reviewed_coordinates.latitude,
    )
    _require_float_match(
        proposal.coordinates.longitude,
        evidence.reviewed_coordinates.longitude,
    )
    _require_float_match(
        candidate.get("latitude"),
        evidence.provider_coordinates.latitude,
    )
    _require_float_match(
        candidate.get("longitude"),
        evidence.provider_coordinates.longitude,
    )
    return evidence


def _validate_master_collisions(
    records: Sequence[dict[str, Any]],
    proposals: Sequence[HotelIdentityReplacementProposal],
    target_ids: set[str],
) -> None:
    retained = [item for item in records if str(item.get("id")) not in target_ids]
    name_owner = {
        _normalize_text(str(item.get("name") or "")): str(item.get("id"))
        for item in retained
        if str(item.get("name") or "").strip()
    }
    address_owner = {
        _normalize_text(str(item.get("address") or "")): str(item.get("id"))
        for item in retained
        if str(item.get("address") or "").strip()
    }
    retained_locations = [
        (str(item.get("id")), _coordinates(item.get("coordinates")))
        for item in retained
    ]
    for proposal in proposals:
        owner = name_owner.get(_normalize_text(proposal.new_name))
        if owner:
            raise HotelIdentityReplacementError(
                f"replacement name already belongs to {owner}: {proposal.new_name}"
            )
        owner = address_owner.get(_normalize_text(proposal.address))
        if owner:
            raise HotelIdentityReplacementError(
                f"replacement address already belongs to {owner}: {proposal.address}"
            )
        for owner_id, location in retained_locations:
            if location is None:
                continue
            distance = _haversine_metres(
                proposal.coordinates.latitude,
                proposal.coordinates.longitude,
                location[0],
                location[1],
            )
            if distance < 5:
                raise HotelIdentityReplacementError(
                    f"replacement coordinates collide with {owner_id}: "
                    f"{proposal.target_id} ({distance:.1f}m)"
                )
    for left_index, left in enumerate(proposals):
        for right in proposals[left_index + 1 :]:
            distance = _haversine_metres(
                left.coordinates.latitude,
                left.coordinates.longitude,
                right.coordinates.latitude,
                right.coordinates.longitude,
            )
            if distance < 5:
                raise HotelIdentityReplacementError(
                    f"replacement coordinates collide: {left.target_id}, "
                    f"{right.target_id} ({distance:.1f}m)"
                )


def _validate_mapping_collisions(
    mapping_directory: Path,
    proposals: Sequence[HotelIdentityReplacementProposal],
    target_ids: set[str],
) -> None:
    external_owner: dict[str, str] = {}
    property_owner: dict[str, str] = {}
    if mapping_directory.exists():
        for path in sorted(mapping_directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            entity_id = str(payload.get("entity_id") or "")
            if entity_id in target_ids:
                continue
            external_id = str(payload.get("external_id") or "")
            if external_id:
                external_owner[external_id] = entity_id
            property_id = _property_id(str(payload.get("external_url") or ""))
            if property_id:
                property_owner[property_id] = entity_id
    for proposal in proposals:
        if proposal.external_id in external_owner:
            raise HotelIdentityReplacementError(
                f"replacement external_id belongs to {external_owner[proposal.external_id]}"
            )
        if proposal.property_id in property_owner:
            raise HotelIdentityReplacementError(
                f"replacement property_id belongs to {property_owner[proposal.property_id]}"
            )


def _build_master_record(
    item: ValidatedHotelReplacement,
    *,
    config: HotelIdentityReplacementConfig,
    batch_id: str,
    batch_hash: str,
) -> dict[str, Any]:
    proposal = item.proposal
    observed_date = item.crawled_at.date().isoformat()
    amount = proposal.nightly_amount
    star_text = f" {proposal.star_rating} sao" if proposal.star_rating else ""
    description = (
        f"Cơ sở lưu trú{star_text} tại {proposal.city}, được xác minh bằng "
        "exact listing và giá phòng trực tiếp từ Trivago. Giá theo ngày lưu trú "
        "được cập nhật qua pipeline observation, không dùng làm giá cố định."
    )
    tags = _amenity_tags(proposal)
    verified_sources: list[dict[str, Any]] = [
        {
            "name": "Trivago MCP exact listing with live price",
            "url": proposal.accommodation_url,
            "raw_path": proposal.raw_path,
            "raw_sha256": proposal.raw_sha256,
        }
    ]
    if item.location_evidence is not None:
        assert proposal.location_evidence is not None
        verified_sources.append(
            {
                "name": "Human-reviewed independent location evidence",
                "url": str(item.location_evidence.supporting_urls[0]),
                "evidence_path": proposal.location_evidence.path,
                "evidence_sha256": proposal.location_evidence.file_sha256,
                "reviewer": item.location_evidence.reviewer,
                "reviewed_at": item.location_evidence.reviewed_at.isoformat(),
            }
        )
    return {
        "id": proposal.target_id,
        "entity_type": "hotel",
        "name": proposal.new_name,
        "city": proposal.city,
        "district": None,
        "address": proposal.address,
        "coordinates": {
            "lat": proposal.coordinates.latitude,
            "lng": proposal.coordinates.longitude,
        },
        "category": proposal.category,
        "sub_category": [],
        "description": description,
        "highlights": [
            value
            for value in (
                f"Khách sạn {proposal.star_rating} sao"
                if proposal.star_rating
                else None,
                f"Đánh giá Trivago {proposal.review_rating}/10"
                if proposal.review_rating is not None
                else None,
            )
            if value is not None
        ],
        "price_range": f"{amount:,}-{amount:,} VND/đêm (quan sát {observed_date})",
        "rating": proposal.review_rating,
        "review_count": proposal.review_count,
        "opening_hours": None,
        "suitable_for": [],
        "transportation_options": {
            "grab": True,
            "taxi": True,
            "motorbike": True,
            "bus_routes": [],
            "parking": None,
            "distance_from_center": item.distance,
            "travel_time_from_center": None,
            "how_to_get_there": None,
        },
        "tags": tags,
        "images": [proposal.main_image] if proposal.main_image else [],
        "source": {
            "url": proposal.accommodation_url,
            "crawled_at": item.crawled_at.isoformat(),
            "source_name": "Trivago MCP",
        },
        "embedding_text": " | ".join(
            [
                proposal.new_name,
                proposal.city,
                proposal.category,
                proposal.address,
                description,
            ]
        ),
        "last_updated": observed_date,
        "star_rating": proposal.star_rating,
        "price_per_night": {
            "min": amount,
            "max": amount,
            "currency": proposal.currency,
            "observed_for_check_in": proposal.check_in,
            "observed_for_check_out": proposal.check_out,
        },
        "room_types": [],
        "amenities": proposal.amenities,
        "check_in_time": None,
        "check_out_time": None,
        "hotel_style": ["resort"] if proposal.category.casefold() == "resort" else [],
        "booking_links": {"trivago": proposal.accommodation_url},
        "distance_to_center": item.distance,
        "distance_to_beach": None,
        "last_crawled": observed_date,
        "verification_status": "human_verified_replacement",
        "verified_sources": verified_sources,
        "last_verified": observed_date,
        "provider_identity": {
            "source_id": "trivago-mcp",
            "external_id": proposal.external_id,
            "property_id": proposal.property_id,
            "provider_name": proposal.new_name,
        },
        "initial_price_observation": {
            "check_in": proposal.check_in,
            "check_out": proposal.check_out,
            "adults": proposal.adults,
            "rooms": proposal.rooms,
            "seller": proposal.seller,
            "currency": proposal.currency,
            "nightly_amount": proposal.nightly_amount,
            "total_amount": proposal.total_amount,
            "observed_at": item.crawled_at.isoformat(),
            "source_record_id": item.source_record_id,
        },
        "identity_replacement": {
            "batch_id": batch_id,
            "batch_hash": batch_hash,
            "approved_by": config.approved_by,
            "approved_at": config.approved_at.isoformat(),
            "external_id": proposal.external_id,
            "property_id": proposal.property_id,
        },
    }


def _read_replacement_batch_history(
    metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_history = metadata.get(_REPLACEMENT_HISTORY_KEY)
    if raw_history is None:
        return []
    if not isinstance(raw_history, list):
        raise HotelIdentityReplacementError(
            "hotel identity replacement batch history must be a list"
        )

    history: list[dict[str, Any]] = []
    batch_ids: set[str] = set()
    batch_hashes: set[str] = set()
    for index, raw_entry in enumerate(raw_history):
        if not isinstance(raw_entry, Mapping):
            raise HotelIdentityReplacementError(
                f"hotel identity replacement batch history entry {index} is invalid"
            )
        entry = dict(raw_entry)
        batch_id = str(entry.get("batch_id") or "")
        batch_hash = str(entry.get("batch_hash") or "")
        replacement_count = entry.get("replacement_count")
        approved_at = str(entry.get("approved_at") or "")
        if (
            not batch_id
            or re.fullmatch(_SHA256, batch_hash) is None
            or not isinstance(replacement_count, int)
            or isinstance(replacement_count, bool)
            or replacement_count < 1
            or not approved_at
        ):
            raise HotelIdentityReplacementError(
                f"hotel identity replacement batch history entry {index} is invalid"
            )
        if batch_id in batch_ids or batch_hash in batch_hashes:
            raise HotelIdentityReplacementError(
                "hotel identity replacement batch history contains duplicates"
            )
        batch_ids.add(batch_id)
        batch_hashes.add(batch_hash)
        history.append(entry)
    return history


def _append_replacement_batch_metadata(
    metadata: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
    batch_id: str,
    batch_hash: str,
    approved_at: str,
    replacement_count: int,
) -> dict[str, Any]:
    """Append a batch without erasing the audit identity of earlier batches."""

    updated = dict(metadata)
    history = _read_replacement_batch_history(metadata)

    # Masters created before append-only history only expose the latest batch in
    # top-level metadata. Promote that batch using its record-level marker before
    # appending a new batch, so the earlier immutable audit remains discoverable.
    legacy_batch_id = str(metadata.get("hotel_identity_replacement_batch_id") or "")
    if legacy_batch_id and not any(
        entry["batch_id"] == legacy_batch_id for entry in history
    ):
        legacy_hashes = {
            str(marker.get("batch_hash"))
            for record in records
            if isinstance((marker := record.get("identity_replacement")), Mapping)
            and marker.get("batch_id") == legacy_batch_id
            and re.fullmatch(_SHA256, str(marker.get("batch_hash") or "")) is not None
        }
        legacy_count = metadata.get("hotel_identity_replacement_count")
        legacy_approved_at = str(metadata.get("hotel_identity_replacements_at") or "")
        if (
            len(legacy_hashes) != 1
            or not isinstance(legacy_count, int)
            or isinstance(legacy_count, bool)
            or legacy_count < 1
            or not legacy_approved_at
        ):
            raise HotelIdentityReplacementError(
                "cannot promote legacy hotel replacement metadata into append-only "
                "batch history"
            )
        history.append(
            {
                "batch_id": legacy_batch_id,
                "batch_hash": next(iter(legacy_hashes)),
                "approved_at": legacy_approved_at,
                "replacement_count": legacy_count,
            }
        )

    current_entry = {
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "approved_at": approved_at,
        "replacement_count": replacement_count,
    }
    matching_entries = [
        entry
        for entry in history
        if entry["batch_id"] == batch_id or entry["batch_hash"] == batch_hash
    ]
    if matching_entries:
        if len(matching_entries) != 1 or matching_entries[0] != current_entry:
            raise HotelIdentityReplacementError(
                "hotel identity replacement batch history conflicts with this batch"
            )
    else:
        history.append(current_entry)

    updated.update(
        {
            "hotel_identity_replacements_at": approved_at,
            "hotel_identity_replacement_batch_id": batch_id,
            "hotel_identity_replacement_count": replacement_count,
            _REPLACEMENT_HISTORY_KEY: history,
        }
    )
    return updated


def _validate_completed_master(
    document: Mapping[str, Any],
    *,
    replacement_records: Mapping[str, dict[str, Any]],
    batch_id: str,
    batch_hash: str,
    replacement_count: int,
) -> None:
    records = document.get("data")
    if not isinstance(records, list):
        raise HotelIdentityReplacementError(
            "completed replacement master has no data list"
        )
    record_by_id = {
        str(item.get("id")): item for item in records if isinstance(item, Mapping)
    }
    for target_id, expected in replacement_records.items():
        if record_by_id.get(target_id) != expected:
            raise HotelIdentityReplacementError(
                f"completed replacement master drift for {target_id}"
            )
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        raise HotelIdentityReplacementError(
            "completed replacement master has no metadata"
        )
    history = _read_replacement_batch_history(metadata)
    history_match = any(
        entry["batch_id"] == batch_id
        and entry["batch_hash"] == batch_hash
        and entry["replacement_count"] == replacement_count
        for entry in history
    )
    # Backward compatibility for the already-applied first batch, whose master
    # predates append-only history but still has exact record-level batch hashes.
    legacy_latest_match = (
        metadata.get("hotel_identity_replacement_batch_id") == batch_id
        and metadata.get("hotel_identity_replacement_count") == replacement_count
    )
    if not history_match and not legacy_latest_match:
        raise HotelIdentityReplacementError(
            "completed replacement master metadata has no matching batch history"
        )
    if any(
        expected.get("identity_replacement", {}).get("batch_hash") != batch_hash
        for expected in replacement_records.values()
    ):
        raise HotelIdentityReplacementError(
            "completed replacement master has an unexpected batch hash"
        )


def _validate_completed_audit(
    path: Path,
    *,
    root: Path,
    batch_id: str,
    batch_hash: str,
    master_path: str,
    config: HotelIdentityReplacementConfig,
    proposals: Sequence[HotelIdentityReplacementProposal],
    replacement_records: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementError(
            f"completed replacement audit is unreadable: {error}"
        ) from error
    if not isinstance(document, dict):
        raise HotelIdentityReplacementError(
            "completed replacement audit must be a JSON object"
        )
    audit_hash = document.get("audit_hash")
    audit_values = {
        key: value
        for key, value in document.items()
        if key not in {"audit_id", "audit_hash"}
    }
    expected_audit_hash = _stable_sha256(audit_values)
    if audit_hash != expected_audit_hash:
        raise HotelIdentityReplacementError("completed replacement audit_hash mismatch")
    if document.get("audit_id") != (
        f"hotel-identity-replacement-audit-{expected_audit_hash[:20]}"
    ):
        raise HotelIdentityReplacementError("completed replacement audit_id mismatch")
    expected_header = {
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "approved_by": config.approved_by,
        "approved_at": config.approved_at.isoformat(),
        "reason": config.reason,
        "master_path": master_path,
    }
    for key, expected in expected_header.items():
        if document.get(key) != expected:
            raise HotelIdentityReplacementError(
                f"completed replacement audit mismatch: {key}"
            )
    for key in ("master_before_sha256", "master_after_sha256"):
        if re.fullmatch(_SHA256, str(document.get(key) or "")) is None:
            raise HotelIdentityReplacementError(
                f"completed replacement audit has an invalid {key}"
            )
    intent_reference = document.get("intent_path")
    intent_sha256 = document.get("intent_sha256")
    if intent_reference is not None or intent_sha256 is not None:
        if (
            not isinstance(intent_reference, str)
            or re.fullmatch(_SHA256, str(intent_sha256 or "")) is None
        ):
            raise HotelIdentityReplacementError(
                "completed replacement audit has an invalid intent reference"
            )
        referenced_intent = _resolve_path(root, intent_reference)
        if (
            not referenced_intent.is_file()
            or _sha256(referenced_intent.read_bytes()) != intent_sha256
        ):
            raise HotelIdentityReplacementError(
                "completed replacement intent sidecar is missing or has drifted"
            )
    try:
        applied_at = datetime.fromisoformat(str(document.get("applied_at")))
    except ValueError as error:
        raise HotelIdentityReplacementError(
            "completed replacement audit has an invalid applied_at"
        ) from error
    if applied_at.tzinfo is None or applied_at.utcoffset() is None:
        raise HotelIdentityReplacementError(
            "completed replacement audit applied_at must be timezone-aware"
        )

    replacements = document.get("replacements")
    if not isinstance(replacements, list) or not all(
        isinstance(item, Mapping) for item in replacements
    ):
        raise HotelIdentityReplacementError(
            "completed replacement audit has invalid replacements"
        )
    replacement_by_id = {str(item.get("target_id")): item for item in replacements}
    if set(replacement_by_id) != set(replacement_records) or len(replacements) != len(
        replacement_records
    ):
        raise HotelIdentityReplacementError(
            "completed replacement audit target set mismatch"
        )
    for proposal in proposals:
        item = replacement_by_id[proposal.target_id]
        expected_values = {
            "external_id": proposal.external_id,
            "property_id": proposal.property_id,
            "raw_path": proposal.raw_path,
            "raw_sha256": proposal.raw_sha256,
            "location_evidence": (
                proposal.location_evidence.model_dump(mode="json")
                if proposal.location_evidence is not None
                else None
            ),
            "new_identity": _identity_audit(replacement_records[proposal.target_id]),
            "new_record_sha256": _stable_sha256(
                replacement_records[proposal.target_id]
            ),
        }
        for key, expected in expected_values.items():
            if item.get(key) != expected:
                raise HotelIdentityReplacementError(
                    f"completed replacement audit drift for {proposal.target_id}: {key}"
                )
        old_identity = item.get("old_identity")
        if (
            not isinstance(old_identity, Mapping)
            or old_identity.get("name") != proposal.old_name
        ):
            raise HotelIdentityReplacementError(
                f"completed replacement audit old identity mismatch for "
                f"{proposal.target_id}"
            )
    return document


def _validate_completed_projections(
    *,
    current_root: Path,
    proposals: Sequence[HotelIdentityReplacementProposal],
    completed_audit: Mapping[str, Any],
) -> None:
    audit_replacements = {
        str(item.get("target_id")): item
        for item in completed_audit.get("replacements", [])
        if isinstance(item, Mapping)
    }
    for proposal in proposals:
        old_identity = _mapping(
            audit_replacements[proposal.target_id].get("old_identity"),
            f"audit old_identity for {proposal.target_id}",
        )
        old_name = _normalize_text(str(old_identity.get("name") or ""))

        mapping_path = (
            current_root / "trivago_mappings" / (f"{proposal.target_id}.json")
        )
        if mapping_path.is_file():
            mapping = _read_projection(mapping_path)
            if (
                mapping.get("entity_id") != proposal.target_id
                or mapping.get("source_id") != "trivago-mcp"
                or mapping.get("external_id") != proposal.external_id
                or _property_id(str(mapping.get("external_url") or ""))
                != proposal.property_id
            ):
                raise HotelIdentityReplacementError(
                    f"completed replacement has a stale Trivago mapping: "
                    f"{proposal.target_id}"
                )
            attributes = mapping.get("attributes")
            if isinstance(attributes, Mapping):
                for key in ("hotel_name", "master_name", "trivago_name"):
                    value = attributes.get(key)
                    if value is not None and _normalize_text(str(value)) != (
                        _normalize_text(proposal.new_name)
                    ):
                        raise HotelIdentityReplacementError(
                            f"completed replacement Trivago {key} drift: "
                            f"{proposal.target_id}"
                        )

        google_path = (
            current_root / "google_maps_mappings" / (f"{proposal.target_id}.json")
        )
        if google_path.is_file():
            mapping = _read_projection(google_path)
            attributes = _mapping(
                mapping.get("attributes"),
                f"Google Maps attributes for {proposal.target_id}",
            )
            if mapping.get("entity_id") != proposal.target_id or _normalize_text(
                str(attributes.get("master_name") or "")
            ) != _normalize_text(proposal.new_name):
                raise HotelIdentityReplacementError(
                    f"completed replacement has a stale Google Maps mapping: "
                    f"{proposal.target_id}"
                )

        place_path = current_root / "place" / f"{proposal.target_id}.json"
        if place_path.is_file():
            place = _read_projection(place_path)
            place_name = _normalize_text(str(place.get("name") or ""))
            if place.get("place_id") != proposal.target_id or (
                old_name and place_name == old_name
            ):
                raise HotelIdentityReplacementError(
                    f"completed replacement has a stale place projection: "
                    f"{proposal.target_id}"
                )
            provenance = place.get("provenance")
            if (
                isinstance(provenance, Mapping)
                and provenance.get("source_id") == "verified-master-data"
                and place_name != _normalize_text(proposal.new_name)
            ):
                raise HotelIdentityReplacementError(
                    f"completed replacement master place drift: {proposal.target_id}"
                )

        for directory in (
            current_root / "hotel_availability" / f"hotel={proposal.target_id}",
            current_root / "hotel_price" / f"hotel={proposal.target_id}",
        ):
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.json")):
                observation = _read_projection(path)
                hotel_ids = _nested_values(observation, "hotel_id")
                external_ids = _nested_values(observation, "external_id")
                if any(value != proposal.target_id for value in hotel_ids) or any(
                    value != proposal.external_id for value in external_ids
                ):
                    raise HotelIdentityReplacementError(
                        f"completed replacement has a stale observation: {path}"
                    )


def _read_projection(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementError(
            f"completed replacement projection is unreadable: {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HotelIdentityReplacementError(
            f"completed replacement projection must be an object: {path}"
        )
    return value


def _nested_values(value: object, key: str) -> list[object]:
    if isinstance(value, Mapping):
        found = [item for name, item in value.items() if name == key]
        for item in value.values():
            found.extend(_nested_values(item, key))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_nested_values(item, key))
        return found
    return []


def _remove_search_review_overrides(path: Path, target_ids: set[str]) -> list[str]:
    if not path.exists():
        return []
    raw_bytes = path.read_bytes()
    document = json.loads(raw_bytes.decode("utf-8-sig"))
    overrides = document.get("overrides") if isinstance(document, dict) else None
    if not isinstance(overrides, list):
        raise HotelIdentityReplacementError(
            "Trivago search-review config must contain an overrides list"
        )
    removed = sorted(
        str(item.get("entity_id"))
        for item in overrides
        if isinstance(item, Mapping) and str(item.get("entity_id")) in target_ids
    )
    if not removed:
        return []
    document["overrides"] = [
        item
        for item in overrides
        if not isinstance(item, Mapping) or str(item.get("entity_id")) not in target_ids
    ]
    newline = "\r\n" if b"\r\n" in raw_bytes else "\n"
    _atomic_replace_bytes(path, _json_bytes(document, newline=newline))
    return removed


def _remove_active_projections(
    *, root: Path, current_root: Path, target_ids: set[str]
) -> list[dict[str, Any]]:
    intended_root = current_root.resolve()
    if not intended_root.is_relative_to(root):
        raise HotelIdentityReplacementError("current root must be inside repository")
    targets: list[Path] = []
    for place_id in sorted(target_ids):
        targets.extend(
            [
                current_root / "trivago_mappings" / f"{place_id}.json",
                current_root / "google_maps_mappings" / f"{place_id}.json",
                current_root / "place" / f"{place_id}.json",
                current_root / "hotel_availability" / f"hotel={place_id}",
                current_root / "hotel_price" / f"hotel={place_id}",
            ]
        )
    removed: list[dict[str, Any]] = []
    for target in targets:
        resolved = target.resolve()
        if not resolved.is_relative_to(intended_root) or resolved == intended_root:
            raise HotelIdentityReplacementError(
                f"unsafe current projection target: {target}"
            )
        if not target.exists():
            continue
        snapshot = _path_snapshot(root, target)
        if target.is_dir():
            _remove_tree(target)
        else:
            try:
                target.chmod(stat.S_IWRITE)
            except OSError:
                pass
            target.unlink()
        removed.append(snapshot)
    return removed


def _remove_tree(path: Path) -> None:
    """Remove an exact current-data directory, including OneDrive read-only nodes."""

    def make_writable(value: Path) -> None:
        try:
            # Replacing the complete mode with S_IWRITE removes directory search
            # bits on POSIX. Preserve the existing mode and only add owner write.
            value.chmod(value.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass

    # OneDrive commonly marks hydrated directories as read-only reparse points.
    # Clear that attribute only inside the already validated exact target tree.
    for item in sorted(
        path.rglob("*"), key=lambda value: len(value.parts), reverse=True
    ):
        make_writable(item)
    make_writable(path)
    shutil.rmtree(path)


def _remove_traffic_cache_entries(path: Path, target_ids: set[str]) -> int:
    if not path.exists():
        return 0
    connection = sqlite3.connect(path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='traffic_cache'"
        ).fetchone()
        if table is None:
            return 0
        rows = connection.execute(
            "SELECT cache_key, payload_json FROM traffic_cache"
        ).fetchall()
        keys = []
        for cache_key, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                continue
            if _contains_exact_value(payload, target_ids):
                keys.append(cache_key)
        if keys:
            with connection:
                connection.executemany(
                    "DELETE FROM traffic_cache WHERE cache_key = ?",
                    [(key,) for key in keys],
                )
        return len(keys)
    finally:
        connection.close()


def _path_snapshot(root: Path, path: Path) -> dict[str, Any]:
    files = (
        [path]
        if path.is_file()
        else sorted(item for item in path.rglob("*") if item.is_file())
    )
    digest = hashlib.sha256()
    for item in files:
        relative = item.relative_to(path) if path.is_dir() else Path(item.name)
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(item.read_bytes())))
    return {
        "path": _relative_text(root, path),
        "kind": "directory" if path.is_dir() else "file",
        "file_count": len(files),
        "content_sha256": digest.hexdigest(),
    }


def _identity_audit(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "name": record.get("name"),
        "city": record.get("city"),
        "address": record.get("address"),
        "coordinates": record.get("coordinates"),
        "category": record.get("category"),
    }


def _build_replacement_intent(
    *,
    batch_id: str,
    batch_hash: str,
    approved_at: str,
    master_path: str,
    master_preimage: bytes,
    proposals: Sequence[HotelIdentityReplacementProposal],
) -> dict[str, Any]:
    intent_values: dict[str, Any] = {
        "schema_version": "1.0.0",
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "approved_at": approved_at,
        "master_path": master_path,
        "master_preimage_sha256": _sha256(master_preimage),
        "master_preimage_base64": base64.b64encode(master_preimage).decode("ascii"),
        "targets": [
            {"target_id": proposal.target_id, "old_name": proposal.old_name}
            for proposal in sorted(proposals, key=lambda value: value.target_id)
        ],
    }
    intent_hash = _stable_sha256(intent_values)
    return {
        **intent_values,
        "intent_id": f"hotel-identity-replacement-intent-{intent_hash[:20]}",
        "intent_hash": intent_hash,
    }


def _recover_master_preimage_from_intent(
    *,
    root: Path,
    intent_path: Path,
    master_path: Path,
    batch_id: str,
    batch_hash: str,
    proposals: Sequence[HotelIdentityReplacementProposal],
) -> tuple[dict[str, Any], bytes]:
    """Recover the exact preimage pinned before this batch first mutated master."""

    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementError(
            f"replacement intent sidecar is unreadable: {error}"
        ) from error
    if not isinstance(intent, dict):
        raise HotelIdentityReplacementError(
            "replacement intent sidecar must be a JSON object"
        )
    intent_hash = str(intent.get("intent_hash") or "")
    intent_values = {
        key: value
        for key, value in intent.items()
        if key not in {"intent_id", "intent_hash"}
    }
    expected_intent_hash = _stable_sha256(intent_values)
    if intent_hash != expected_intent_hash:
        raise HotelIdentityReplacementError(
            "replacement intent sidecar intent_hash mismatch"
        )
    if intent.get("intent_id") != (
        f"hotel-identity-replacement-intent-{expected_intent_hash[:20]}"
    ):
        raise HotelIdentityReplacementError(
            "replacement intent sidecar intent_id mismatch"
        )
    expected_header = {
        "schema_version": "1.0.0",
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "master_path": _relative_text(root, master_path),
        "targets": [
            {"target_id": proposal.target_id, "old_name": proposal.old_name}
            for proposal in sorted(proposals, key=lambda value: value.target_id)
        ],
    }
    for key, expected in expected_header.items():
        if intent.get(key) != expected:
            raise HotelIdentityReplacementError(
                f"replacement intent sidecar mismatch: {key}"
            )
    encoded_preimage = intent.get("master_preimage_base64")
    if not isinstance(encoded_preimage, str):
        raise HotelIdentityReplacementError(
            "replacement intent sidecar has no master preimage"
        )
    try:
        preimage_bytes = base64.b64decode(encoded_preimage, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HotelIdentityReplacementError(
            "replacement intent sidecar master preimage is invalid"
        ) from error
    if _sha256(preimage_bytes) != intent.get("master_preimage_sha256"):
        raise HotelIdentityReplacementError(
            "replacement intent sidecar master preimage hash mismatch"
        )
    try:
        document = json.loads(preimage_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementError(
            f"replacement intent master preimage is invalid: {error}"
        ) from error
    records = document.get("data") if isinstance(document, dict) else None
    if not isinstance(records, list) or not all(
        isinstance(item, dict) for item in records
    ):
        raise HotelIdentityReplacementError(
            "replacement intent master preimage has no valid data list"
        )
    by_id = {str(item.get("id")): item for item in records}
    for proposal in proposals:
        record = by_id.get(proposal.target_id)
        if record is None or str(record.get("name") or "") != proposal.old_name:
            raise HotelIdentityReplacementError(
                "replacement intent preimage does not match approved old identity: "
                f"{proposal.target_id}"
            )
    return document, preimage_bytes


def _recover_master_preimage_from_git(
    *,
    root: Path,
    master_path: Path,
    proposals: Sequence[HotelIdentityReplacementProposal],
) -> tuple[dict[str, Any], bytes]:
    """Recover a content-pinned preimage after an interrupted first apply.

    This path is used only when every replacement marker is already in master
    but the immutable completion audit is absent. It never writes through Git.
    """

    relative = _relative_text(root, master_path)
    completed = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{relative}"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise HotelIdentityReplacementError(
            "replacement master is active but its audit is missing, and the Git "
            f"preimage cannot be recovered: {detail or relative}"
        )
    try:
        document = json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementError(
            f"Git master preimage is invalid: {error}"
        ) from error
    records = document.get("data") if isinstance(document, dict) else None
    if not isinstance(records, list) or not all(
        isinstance(item, dict) for item in records
    ):
        raise HotelIdentityReplacementError(
            "Git master preimage has no valid data list"
        )
    by_id = {str(item.get("id")): item for item in records}
    for proposal in proposals:
        record = by_id.get(proposal.target_id)
        if record is None or str(record.get("name") or "") != proposal.old_name:
            raise HotelIdentityReplacementError(
                f"Git preimage does not match approved old identity: {proposal.target_id}"
            )
    current_bytes = master_path.read_bytes()
    if b"\r\n" in current_bytes:
        preimage_bytes = completed.stdout.replace(b"\r\n", b"\n").replace(
            b"\n", b"\r\n"
        )
    else:
        preimage_bytes = completed.stdout.replace(b"\r\n", b"\n")
    return document, preimage_bytes


def _amenity_tags(proposal: HotelIdentityReplacementProposal) -> list[str]:
    values: set[str] = set()
    text = _normalize_text(" ".join(proposal.amenities))
    for token, tag in (
        ("wifi", "wifi"),
        ("be boi", "pool"),
        ("spa", "spa"),
        ("bai do xe", "parking"),
        ("nha hang", "restaurant"),
        ("bar", "bar"),
        ("gym", "gym"),
    ):
        if token in text:
            values.add(tag)
    if proposal.category.casefold() == "resort":
        values.add("resort")
    return sorted(values)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HotelIdentityReplacementError(f"{label} must be an object")
    return value


def _coordinates(value: object) -> tuple[float, float] | None:
    if not isinstance(value, Mapping):
        return None
    latitude = value.get("lat", value.get("latitude"))
    longitude = value.get("lng", value.get("longitude"))
    if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
        return None
    if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
        return None
    return float(latitude), float(longitude)


def _require_float_match(actual: object, expected: float) -> None:
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        raise HotelIdentityReplacementError("raw candidate coordinate is missing")
    if not math.isclose(float(actual), expected, rel_tol=0, abs_tol=1e-7):
        raise HotelIdentityReplacementError("raw candidate coordinate mismatch")


def _require_optional_number_match(
    actual: object, expected: int | float | None, field_name: str, target_id: str
) -> None:
    if actual in (None, "") and expected is None:
        return
    try:
        actual_number = float(actual)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise HotelIdentityReplacementError(
            f"raw candidate {field_name} is invalid for {target_id}"
        ) from error
    if expected is None or not math.isclose(
        actual_number, float(expected), abs_tol=1e-7
    ):
        raise HotelIdentityReplacementError(
            f"raw candidate {field_name} mismatch for {target_id}"
        )


def _haversine_metres(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    earth_radius = 6_371_000.0
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius * math.asin(math.sqrt(value))


def _property_id(url: str) -> str | None:
    normalized = url.replace("\\u0026", "&")
    match = _PROPERTY_ID.search(urlsplit(normalized).query)
    return match.group(1) if match else None


def _validate_trivago_accommodation_url(
    proposal: HotelIdentityReplacementProposal,
) -> None:
    normalized = proposal.accommodation_url.replace("\\u0026", "&")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as error:
        raise HotelIdentityReplacementError(
            f"invalid Trivago accommodation URL for {proposal.target_id}"
        ) from error
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in _TRIVAGO_ACCOMMODATION_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HotelIdentityReplacementError(
            f"candidate URL is not an official Trivago URL for {proposal.target_id}"
        )
    if _property_id(normalized) != proposal.property_id:
        raise HotelIdentityReplacementError(
            f"raw property_id mismatch for {proposal.target_id}"
        )
    interval = _DATE_INTERVAL.search(parsed.query)
    expected_interval = (
        proposal.check_in.replace("-", ""),
        proposal.check_out.replace("-", ""),
    )
    if interval is None or interval.groups() != expected_interval:
        raise HotelIdentityReplacementError(
            f"candidate URL date interval mismatch for {proposal.target_id}"
        )


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_like = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).replace("đ", "d")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_like).split())


def _require_unique(label: str, values: Sequence[str]) -> None:
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        raise HotelIdentityReplacementError(
            f"duplicate replacement {label}: {', '.join(duplicates)}"
        )


def _resolve_path(root: Path, value: str | Path) -> Path:
    candidate = Path(value)
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    )
    if not resolved.is_relative_to(root):
        raise HotelIdentityReplacementError(f"path escapes repository: {value}")
    return resolved


def _relative_text(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return _sha256(payload)


def _json_bytes(value: object, *, newline: str = "\n") -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if newline != "\n":
        text = text.replace("\n", newline)
    return text.encode("utf-8")


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_immutable_json(path: Path, value: object) -> None:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"immutable replacement audit differs: {path}")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "wb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())


def _contains_exact_value(value: object, targets: set[str]) -> bool:
    if isinstance(value, str):
        return value in targets
    if isinstance(value, Mapping):
        return any(_contains_exact_value(item, targets) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_value(item, targets) for item in value)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or apply reviewed same-ID hotel identity replacements. "
            "Without --apply, no file is changed."
        )
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument(
        "--config", type=Path, default=Path("config/hotel-identity-replacements.json")
    )
    parser.add_argument(
        "--master",
        type=Path,
        required=True,
        help="Explicit legacy hotel corpus for this historical migration tool.",
    )
    parser.add_argument("--current-root", type=Path, default=Path("data/current"))
    parser.add_argument(
        "--search-review",
        type=Path,
        default=Path("config/trivago-search-review.json"),
    )
    parser.add_argument(
        "--audit-root",
        type=Path,
        default=Path("data/audit/hotel_identity_replacements"),
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = replace_hotel_identities(
            repository_root=arguments.repository_root,
            config_path=arguments.config,
            master_path=arguments.master,
            current_root=arguments.current_root,
            search_review_path=arguments.search_review,
            audit_root=arguments.audit_root,
            apply=arguments.apply,
        )
    except (HotelIdentityReplacementError, OSError, ValueError) as error:
        print(f"Cannot replace hotel identities: {error}")
        return 1
    # Keep the CLI safe in the default Windows PowerShell code page while the
    # persisted artifacts themselves remain real UTF-8 Vietnamese text.
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
