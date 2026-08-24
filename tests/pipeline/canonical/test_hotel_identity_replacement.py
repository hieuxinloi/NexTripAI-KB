from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical import hotel_identity_replacement as replacement_module
from nextrip_pipeline.canonical.hotel_identity_replacement import (
    HotelIdentityReplacementError,
    replace_hotel_identities,
)
from nextrip_pipeline.crawl.raw_writer import compute_content_hash


TARGET_ID = "hotel_dn_001"
LEGACY_MASTER_RELATIVE_PATH = Path("legacy-import/hotel_final.json")
TRIVAGO_URL = (
    "https://www.trivago.vn/vi/lm/new-hotel-da-nang?"
    "currencyCode=VND&search=100-987654;dr-20260824-20260825;rc-1-2"
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_master_path(root: Path) -> Path:
    return root / LEGACY_MASTER_RELATIVE_PATH


def _create_repository(root: Path) -> None:
    master = {
        "metadata": {
            "entity_type": "hotel",
            "total_count": 2,
            "da_nang_count": 2,
            "quy_nhon_count": 0,
        },
        "data": [
            {
                "id": TARGET_ID,
                "entity_type": "hotel",
                "name": "Old Hotel",
                "city": "Đà Nẵng",
                "address": "1 Old Street",
                "coordinates": {"lat": 16.0, "lng": 108.0},
                "category": "hotel",
                "tags": [],
            },
            {
                "id": "hotel_dn_002",
                "entity_type": "hotel",
                "name": "Retained Hotel",
                "city": "Đà Nẵng",
                "address": "2 Retained Street",
                "coordinates": {"lat": 16.2, "lng": 108.2},
                "category": "hotel",
                "tags": [],
            },
        ],
    }
    _write_json(_legacy_master_path(root), master)

    raw = {
        "source_record_id": "trivago-mcp-test",
        "run_id": "replacement-test",
        "source_id": "trivago-mcp",
        "entity_type": "hotel",
        "subject_type": "hotel_price",
        "subject_id": TARGET_ID,
        "crawled_at": "2026-08-23T02:00:00Z",
        "raw_payload": {
            "request": {
                "entity_id": TARGET_ID,
                "search_strategy": "name",
                "tool": "trivago-accommodation-search",
                "arguments": {
                    "query": "New Hotel, Đà Nẵng, Việt Nam",
                    "arrival": "2026-08-24",
                    "departure": "2026-08-25",
                    "adults": 2,
                    "children": 0,
                    "rooms": 1,
                    "country": "VN",
                    "currency": "VND",
                },
            },
            "response": {
                "result": {
                    "structuredContent": {
                        "accommodations": [
                            {
                                "accommodation_id": "new-external-id",
                                "accommodation_name": "New Hotel",
                                "accommodation_url": TRIVAGO_URL,
                                "advertisers": "Booking.com",
                                "arrival": "2026-08-24",
                                "departure": "2026-08-25",
                                "currency": "VND",
                                "country_city": "Đà Nẵng, Việt Nam",
                                "price_per_night": "500.000 đ",
                                "price_per_stay": "500.000 đ",
                                "hotel_rating": 3,
                                "review_rating": "8.5",
                                "review_count": "100",
                                "top_amenities": "WiFi, Bãi đỗ xe",
                                "latitude": 16.1,
                                "longitude": 108.1,
                                "distance": "cách trung tâm 1 km",
                                "main_image": "https://example.com/new-hotel.jpg",
                            }
                        ]
                    }
                }
            },
        },
        "content_hash": "0" * 64,
        "parser_version": "test",
        "source_url": "https://mcp.trivago.com/mcp",
        "http_status": 200,
        "content_type": "application/json",
    }
    raw["content_hash"] = compute_content_hash(raw["raw_payload"])
    raw_path = root / "data/raw/replacement.json"
    _write_json(raw_path, raw)

    proposal = [
        {
            "target_id": TARGET_ID,
            "old_name": "Old Hotel",
            "new_name": "New Hotel",
            "city": "Đà Nẵng",
            "address": "100 New Street",
            "coordinates": {"lat": 16.1, "lng": 108.1},
            "category": "hotel",
            "star_rating": 3,
            "review_rating": 8.5,
            "review_count": 100,
            "amenities": ["WiFi", "Bãi đỗ xe"],
            "external_id": "new-external-id",
            "property_id": "987654",
            "accommodation_url": TRIVAGO_URL,
            "main_image": "https://example.com/new-hotel.jpg",
            "seller": "Booking.com",
            "nightly_amount": 500000,
            "total_amount": 500000,
            "currency": "VND",
            "check_in": "2026-08-24",
            "check_out": "2026-08-25",
            "adults": 2,
            "rooms": 1,
            "raw_path": "data/raw/replacement.json",
            "raw_sha256": _sha256(raw_path),
            "selection_reason": "Reviewed equivalent with a complete live offer.",
        }
    ]
    proposal_path = root / "data/review/proposals.json"
    _write_json(proposal_path, proposal)
    config = {
        "schema_version": "1.0.0",
        "approved_by": "reviewer",
        "approved_at": "2026-08-23T10:00:00+07:00",
        "reason": "Test replacement",
        "proposal_artifacts": [
            {
                "path": "data/review/proposals.json",
                "file_sha256": _sha256(proposal_path),
            }
        ],
        "expected_target_ids": [TARGET_ID],
    }
    _write_json(root / "config/hotel-identity-replacements.json", config)
    _write_json(
        root / "config/trivago-search-review.json",
        {
            "schema_version": "1.0.0",
            "overrides": [
                {"entity_id": TARGET_ID},
                {"entity_id": "hotel_dn_002"},
            ],
        },
    )

    for relative in (
        f"data/current/trivago_mappings/{TARGET_ID}.json",
        f"data/current/google_maps_mappings/{TARGET_ID}.json",
        f"data/current/place/{TARGET_ID}.json",
        f"data/current/hotel_availability/hotel={TARGET_ID}/context.json",
        f"data/current/hotel_price/hotel={TARGET_ID}/context.json",
    ):
        _write_json(root / relative, {"entity_id": TARGET_ID})
    _write_json(
        root / "data/current/trivago_mappings/hotel_dn_002.json",
        {
            "entity_id": "hotel_dn_002",
            "external_id": "retained-external-id",
            "external_url": "https://www.trivago.vn/x?search=100-123456",
        },
    )

    cache_path = root / "data/current/traffic/cache.sqlite3"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(cache_path)
    with connection:
        connection.execute(
            "CREATE TABLE traffic_cache (cache_key TEXT PRIMARY KEY, payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO traffic_cache VALUES (?, ?)",
            ("remove", json.dumps({"origin": TARGET_ID})),
        )
        connection.execute(
            "INSERT INTO traffic_cache VALUES (?, ?)",
            ("retain", json.dumps({"origin": "hotel_dn_002"})),
        )
    connection.close()


def _mutate_and_repin_evidence(root: Path, case: str) -> None:
    raw_path = root / "data/raw/replacement.json"
    proposal_path = root / "data/review/proposals.json"
    config_path = root / "config/hotel-identity-replacements.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    proposals = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal = proposals[0]
    request = raw["raw_payload"]["request"]
    arguments = request["arguments"]
    candidate = raw["raw_payload"]["response"]["result"]["structuredContent"][
        "accommodations"
    ][0]

    if case == "content_hash":
        raw["content_hash"] = "f" * 64
    elif case == "subject_type":
        raw["subject_type"] = "rating"
    elif case == "mcp_host":
        raw["source_url"] = "https://example.com/mcp"
    elif case == "http_status":
        raw["http_status"] = 201
    elif case == "tool":
        request["tool"] = "different-tool"
    elif case == "query":
        arguments["query"] = "Unrelated Hotel, Đà Nẵng, Việt Nam"
    elif case == "country":
        arguments["country"] = "US"
    elif case == "children":
        arguments["children"] = 1
    elif case == "candidate_host":
        bad_url = TRIVAGO_URL.replace("www.trivago.vn", "example.com")
        proposal["accommodation_url"] = bad_url
        candidate["accommodation_url"] = bad_url
    elif case == "url_interval":
        bad_url = TRIVAGO_URL.replace("dr-20260824-20260825", "dr-20260825-20260826")
        proposal["accommodation_url"] = bad_url
        candidate["accommodation_url"] = bad_url
    elif case == "calendar_date":
        bad_url = TRIVAGO_URL.replace("dr-20260824-20260825", "dr-20260230-20260301")
        proposal["check_in"] = "2026-02-30"
        proposal["check_out"] = "2026-03-01"
        proposal["accommodation_url"] = bad_url
        arguments["arrival"] = "2026-02-30"
        arguments["departure"] = "2026-03-01"
        candidate["arrival"] = "2026-02-30"
        candidate["departure"] = "2026-03-01"
        candidate["accommodation_url"] = bad_url
    else:  # pragma: no cover - protects the test helper itself
        raise AssertionError(f"unknown evidence mutation: {case}")

    if case != "content_hash":
        raw["content_hash"] = compute_content_hash(raw["raw_payload"])
    _write_json(raw_path, raw)
    proposal["raw_sha256"] = _sha256(raw_path)
    _write_json(proposal_path, proposals)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["proposal_artifacts"][0]["file_sha256"] = _sha256(proposal_path)
    _write_json(config_path, config)


def _configure_second_replacement(root: Path) -> Path:
    master = json.loads(_legacy_master_path(root).read_text(encoding="utf-8"))
    target = next(item for item in master["data"] if item["id"] == "hotel_dn_002")
    city = target["city"]
    second_url = (
        "https://www.trivago.vn/vi/lm/second-hotel-da-nang?"
        "currencyCode=VND&search=100-765432;dr-20260824-20260825;rc-1-2"
    )
    raw = json.loads((root / "data/raw/replacement.json").read_text(encoding="utf-8"))
    raw.update(
        {
            "source_record_id": "trivago-mcp-test-second",
            "run_id": "replacement-test-second",
            "subject_id": "hotel_dn_002",
        }
    )
    request = raw["raw_payload"]["request"]
    request["entity_id"] = "hotel_dn_002"
    request["arguments"]["query"] = f"Second Hotel, {city}, Viá»‡t Nam"
    candidate = raw["raw_payload"]["response"]["result"]["structuredContent"][
        "accommodations"
    ][0]
    candidate.update(
        {
            "accommodation_id": "second-external-id",
            "accommodation_name": "Second Hotel",
            "accommodation_url": second_url,
            "price_per_night": "600.000 Ä‘",
            "price_per_stay": "600.000 Ä‘",
            "latitude": 16.3,
            "longitude": 108.3,
            "main_image": "https://example.com/second-hotel.jpg",
            "country_city": f"{city}, Viá»‡t Nam",
        }
    )
    raw["content_hash"] = compute_content_hash(raw["raw_payload"])
    raw_path = root / "data/raw/replacement-second.json"
    _write_json(raw_path, raw)

    proposals = [
        {
            "target_id": "hotel_dn_002",
            "old_name": "Retained Hotel",
            "new_name": "Second Hotel",
            "city": city,
            "address": "200 Second Street",
            "coordinates": {"lat": 16.3, "lng": 108.3},
            "category": "hotel",
            "star_rating": 3,
            "review_rating": 8.5,
            "review_count": 100,
            "amenities": ["WiFi"],
            "external_id": "second-external-id",
            "property_id": "765432",
            "accommodation_url": second_url,
            "main_image": "https://example.com/second-hotel.jpg",
            "seller": "Booking.com",
            "nightly_amount": 600000,
            "total_amount": 600000,
            "currency": "VND",
            "check_in": "2026-08-24",
            "check_out": "2026-08-25",
            "adults": 2,
            "rooms": 1,
            "raw_path": "data/raw/replacement-second.json",
            "raw_sha256": _sha256(raw_path),
            "selection_reason": "Reviewed second equivalent with a live offer.",
        }
    ]
    proposal_path = root / "data/review/proposals-second.json"
    _write_json(proposal_path, proposals)
    config_path = root / "config/hotel-identity-replacements-second.json"
    _write_json(
        config_path,
        {
            "schema_version": "1.0.0",
            "approved_by": "reviewer",
            "approved_at": "2026-08-24T10:00:00+07:00",
            "reason": "Test second replacement",
            "proposal_artifacts": [
                {
                    "path": "data/review/proposals-second.json",
                    "file_sha256": _sha256(proposal_path),
                }
            ],
            "expected_target_ids": ["hotel_dn_002"],
        },
    )
    return config_path


def _configure_reviewed_location_override(root: Path) -> Path:
    raw_path = root / "data/raw/replacement.json"
    proposal_path = root / "data/review/proposals.json"
    config_path = root / "config/hotel-identity-replacements.json"

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    candidate = raw["raw_payload"]["response"]["result"]["structuredContent"][
        "accommodations"
    ][0]
    candidate["latitude"] = 16.4
    candidate["longitude"] = 108.4
    raw["content_hash"] = compute_content_hash(raw["raw_payload"])
    _write_json(raw_path, raw)

    evidence_path = root / "data/review/location-evidence.json"
    _write_json(
        evidence_path,
        {
            "schema_version": "1.0.0",
            "target_id": TARGET_ID,
            "new_name": "New Hotel",
            "address": "100 New Street",
            "reviewed_coordinates": {"latitude": 16.1, "longitude": 108.1},
            "provider_coordinates": {"latitude": 16.4, "longitude": 108.4},
            "reviewer": "location-reviewer",
            "reviewed_at": "2026-08-23T10:30:00+07:00",
            "reason": "Independent sources agree that provider coordinates are wrong.",
            "supporting_urls": [
                "https://www.google.com/maps/place/New+Hotel",
                "https://www.booking.com/hotel/vn/new-hotel.html",
            ],
        },
    )

    proposals = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposals[0]["raw_sha256"] = _sha256(raw_path)
    proposals[0]["location_evidence"] = {
        "path": "data/review/location-evidence.json",
        "file_sha256": _sha256(evidence_path),
    }
    _write_json(proposal_path, proposals)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["proposal_artifacts"][0]["file_sha256"] = _sha256(proposal_path)
    _write_json(config_path, config)
    return evidence_path


def test_replacement_requires_explicit_legacy_master(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="master_path"):
        replace_hotel_identities(repository_root=tmp_path)  # type: ignore[call-arg]

    with pytest.raises(SystemExit, match="2"):
        replacement_module.build_parser().parse_args([])


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("content_hash", "content_hash mismatch"),
        ("subject_type", "ownership mismatch"),
        ("mcp_host", "not from the Trivago MCP endpoint"),
        ("http_status", "HTTP status is not 200"),
        ("tool", "tool mismatch"),
        ("query", "query does not target"),
        ("country", "country mismatch"),
        ("children", "children mismatch"),
        ("candidate_host", "not an official Trivago URL"),
        ("url_interval", "date interval mismatch"),
        ("calendar_date", "valid calendar dates"),
    ],
)
def test_rejects_untrusted_or_wrong_context_evidence(
    tmp_path: Path, case: str, message: str
) -> None:
    _create_repository(tmp_path)
    _mutate_and_repin_evidence(tmp_path, case)

    with pytest.raises(ValueError, match=message):
        replace_hotel_identities(
            repository_root=tmp_path,
            master_path=_legacy_master_path(tmp_path),
        )


def test_reviewed_location_evidence_overrides_wrong_provider_coordinates(
    tmp_path: Path,
) -> None:
    _create_repository(tmp_path)
    _configure_reviewed_location_override(tmp_path)

    replace_hotel_identities(
        repository_root=tmp_path,
        master_path=_legacy_master_path(tmp_path),
        apply=True,
        now=datetime(2026, 8, 23, 4, tzinfo=timezone.utc),
    )

    master = json.loads(_legacy_master_path(tmp_path).read_text(encoding="utf-8"))
    replacement = next(item for item in master["data"] if item["id"] == TARGET_ID)
    assert replacement["coordinates"] == {"lat": 16.1, "lng": 108.1}
    assert len(replacement["verified_sources"]) == 2
    assert replacement["verified_sources"][1]["reviewer"] == "location-reviewer"


def test_reviewed_location_evidence_is_content_pinned(tmp_path: Path) -> None:
    _create_repository(tmp_path)
    evidence_path = _configure_reviewed_location_override(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["address"] = "Tampered address"
    _write_json(evidence_path, evidence)

    with pytest.raises(HotelIdentityReplacementError, match="location evidence hash"):
        replace_hotel_identities(
            repository_root=tmp_path,
            master_path=_legacy_master_path(tmp_path),
        )


def test_same_id_replacement_is_validated_applied_and_idempotent(
    tmp_path: Path,
) -> None:
    _create_repository(tmp_path)
    master_path = _legacy_master_path(tmp_path)
    before = master_path.read_bytes()

    checked = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
    )

    assert checked.mode == "check"
    assert checked.target_ids == [TARGET_ID]
    assert master_path.read_bytes() == before
    assert (tmp_path / f"data/current/place/{TARGET_ID}.json").is_file()

    applied = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )

    assert applied.mode == "apply"
    assert applied.removed_active_path_count == 5
    assert applied.removed_traffic_cache_entries == 1
    assert applied.removed_search_review_overrides == [TARGET_ID]
    assert applied.audit_path is not None
    assert (tmp_path / applied.audit_path).is_file()

    document = json.loads(master_path.read_text(encoding="utf-8"))
    replacement = next(item for item in document["data"] if item["id"] == TARGET_ID)
    assert replacement["name"] == "New Hotel"
    assert replacement["provider_identity"]["external_id"] == "new-external-id"
    assert "Old Hotel" not in json.dumps(replacement, ensure_ascii=False)
    assert not (tmp_path / f"data/current/place/{TARGET_ID}.json").exists()
    assert (tmp_path / "data/current/trivago_mappings/hotel_dn_002.json").is_file()

    review = json.loads(
        (tmp_path / "config/trivago-search-review.json").read_text(encoding="utf-8")
    )
    assert [item["entity_id"] for item in review["overrides"]] == ["hotel_dn_002"]

    connection = sqlite3.connect(tmp_path / "data/current/traffic/cache.sqlite3")
    keys = [row[0] for row in connection.execute("SELECT cache_key FROM traffic_cache")]
    connection.close()
    assert keys == ["retain"]

    master_after_first_apply = master_path.read_bytes()
    retried = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
        apply=True,
        now=datetime(2026, 8, 24, 3, tzinfo=timezone.utc),
    )
    assert retried.audit_path == applied.audit_path
    assert master_path.read_bytes() == master_after_first_apply


def test_remove_tree_preserves_directory_search_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "current" / "hotel_price" / "hotel=hotel_dn_001"
    nested = target / "context"
    nested.mkdir(parents=True)
    (nested / "observation.json").write_text("{}\n", encoding="utf-8")
    original_rmtree = replacement_module.shutil.rmtree

    def assert_searchable_then_remove(path: Path) -> None:
        assert path.stat().st_mode & stat.S_IXUSR
        original_rmtree(path)

    monkeypatch.setattr(
        replacement_module.shutil,
        "rmtree",
        assert_searchable_then_remove,
    )

    replacement_module._remove_tree(target)

    assert not target.exists()


def test_completed_retry_rejects_master_drift(tmp_path: Path) -> None:
    _create_repository(tmp_path)
    replace_hotel_identities(
        repository_root=tmp_path,
        master_path=_legacy_master_path(tmp_path),
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )
    master_path = _legacy_master_path(tmp_path)
    document = json.loads(master_path.read_text(encoding="utf-8"))
    replacement = next(item for item in document["data"] if item["id"] == TARGET_ID)
    replacement["address"] = "Tampered address"
    _write_json(master_path, document)

    with pytest.raises(
        HotelIdentityReplacementError, match="completed replacement master drift"
    ):
        replace_hotel_identities(
            repository_root=tmp_path,
            master_path=master_path,
            apply=True,
        )


def test_completed_retry_rejects_corrupt_audit(tmp_path: Path) -> None:
    _create_repository(tmp_path)
    applied = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=_legacy_master_path(tmp_path),
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )
    assert applied.audit_path is not None
    audit_path = tmp_path / applied.audit_path
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["reason"] = "tampered"
    _write_json(audit_path, audit)

    with pytest.raises(HotelIdentityReplacementError, match="audit_hash mismatch"):
        replace_hotel_identities(
            repository_root=tmp_path,
            master_path=_legacy_master_path(tmp_path),
            apply=True,
        )


def test_completed_retry_rejects_stale_current_projection(tmp_path: Path) -> None:
    _create_repository(tmp_path)
    replace_hotel_identities(
        repository_root=tmp_path,
        master_path=_legacy_master_path(tmp_path),
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )
    _write_json(
        tmp_path / f"data/current/trivago_mappings/{TARGET_ID}.json",
        {
            "entity_id": TARGET_ID,
            "source_id": "trivago-mcp",
            "external_id": "old-external-id",
            "external_url": "https://www.trivago.vn/x?search=100-111111",
        },
    )

    with pytest.raises(HotelIdentityReplacementError, match="stale Trivago mapping"):
        replace_hotel_identities(
            repository_root=tmp_path,
            master_path=_legacy_master_path(tmp_path),
            apply=True,
        )


def test_sequential_batches_preserve_history_and_allow_older_retry(
    tmp_path: Path,
) -> None:
    _create_repository(tmp_path)
    first = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=_legacy_master_path(tmp_path),
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )
    assert first.audit_path is not None
    first_audit_path = tmp_path / first.audit_path
    first_audit_bytes = first_audit_path.read_bytes()

    second_config = _configure_second_replacement(tmp_path)
    second = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=_legacy_master_path(tmp_path),
        config_path=second_config,
        apply=True,
        now=datetime(2026, 8, 24, 3, tzinfo=timezone.utc),
    )

    master_path = _legacy_master_path(tmp_path)
    master = json.loads(master_path.read_text(encoding="utf-8"))
    history = master["metadata"]["hotel_identity_replacement_batches"]
    assert [item["batch_id"] for item in history] == [first.batch_id, second.batch_id]
    assert [item["batch_hash"] for item in history] == [
        first.batch_hash,
        second.batch_hash,
    ]
    assert master["metadata"]["hotel_identity_replacement_batch_id"] == second.batch_id

    master_before_retry = master_path.read_bytes()
    retried_first = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
        apply=True,
        now=datetime(2026, 8, 25, 3, tzinfo=timezone.utc),
    )

    assert retried_first.audit_path == first.audit_path
    assert master_path.read_bytes() == master_before_retry
    assert first_audit_path.read_bytes() == first_audit_bytes


def test_interrupted_apply_recovers_from_intent_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_repository(tmp_path)
    master_path = _legacy_master_path(tmp_path)
    master_preimage = master_path.read_bytes()
    original_remove_overrides = replacement_module._remove_search_review_overrides

    def fail_after_master_write(path: Path, target_ids: set[str]) -> list[str]:
        raise OSError("simulated interruption after master write")

    monkeypatch.setattr(
        replacement_module,
        "_remove_search_review_overrides",
        fail_after_master_write,
    )
    with pytest.raises(OSError, match="simulated interruption"):
        replace_hotel_identities(
            repository_root=tmp_path,
            master_path=master_path,
            apply=True,
        )

    intents = list(
        (tmp_path / "data/audit/hotel_identity_replacements").glob("intent=*.json")
    )
    assert len(intents) == 1
    assert master_path.read_bytes() != master_preimage
    assert not list(
        (tmp_path / "data/audit/hotel_identity_replacements").glob("batch=*.json")
    )

    monkeypatch.setattr(
        replacement_module,
        "_remove_search_review_overrides",
        original_remove_overrides,
    )
    recovered = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
        apply=True,
        now=datetime(2026, 8, 23, 4, tzinfo=timezone.utc),
    )

    assert recovered.audit_path is not None
    audit = json.loads((tmp_path / recovered.audit_path).read_text(encoding="utf-8"))
    assert audit["recovered_after_partial_apply"] is True
    assert audit["recovery_source"] == "intent"
    assert audit["master_before_sha256"] == hashlib.sha256(master_preimage).hexdigest()
    assert audit["intent_path"] == intents[0].relative_to(tmp_path).as_posix()
    assert audit["intent_sha256"] == _sha256(intents[0])


def test_completed_retry_accepts_legacy_latest_metadata_without_history(
    tmp_path: Path,
) -> None:
    _create_repository(tmp_path)
    applied = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=_legacy_master_path(tmp_path),
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )
    master_path = _legacy_master_path(tmp_path)
    master = json.loads(master_path.read_text(encoding="utf-8"))
    master["metadata"].pop("hotel_identity_replacement_batches")
    _write_json(master_path, master)
    legacy_master = master_path.read_bytes()

    retried = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
        apply=True,
    )

    assert retried.audit_path == applied.audit_path
    assert master_path.read_bytes() == legacy_master
