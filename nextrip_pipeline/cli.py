from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from nextrip_pipeline.canonical.evidence import (
    DuplicateEvidenceAudit,
    DuplicateEvidenceAuditor,
)
from nextrip_pipeline.canonical.detail import (
    CandidateDetailStage,
    CandidateDetailStageWriter,
    GoogleMapsCandidateDetail,
)
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDatasetWriter,
    materialize_canonical_active_dataset,
)
from nextrip_pipeline.canonical.discovery import (
    GoogleMapsCandidateDiscovery,
    GoogleMapsCandidateStageWriter,
)
from nextrip_pipeline.canonical.id_allocator import MonotonicPlaceIdAllocator
from nextrip_pipeline.canonical.manifest import CanonicalIdentityManifestWriter
from nextrip_pipeline.canonical.manifest import read_canonical_identity_manifest
from nextrip_pipeline.canonical.master import (
    build_manifest_from_master,
    load_duplicate_identity_decisions,
    load_verified_master,
)
from nextrip_pipeline.canonical.projection import (
    ApprovedReplacement,
    ApprovedReplacementWriter,
    ReplacementApprovalMethod,
    build_existing_identity_projection,
    load_approved_replacements,
)
from nextrip_pipeline.canonical.replacement_batch import (
    CanonicalReplacementProposalBatch,
    CanonicalReplacementProposalBatchRunner,
    CanonicalReplacementProposalBatchWriter,
)
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetNotReadyError,
    CanonicalDatasetReadinessWriter,
    evaluate_canonical_dataset_readiness,
    require_canonical_dataset_publish_ready,
)
from nextrip_pipeline.canonical.review_correction import (
    apply_review_corrections,
    read_canonical_review_correction_overlay,
)
from nextrip_pipeline.canonical.resolver import CanonicalIdentityResolver
from nextrip_pipeline.crawl import (
    GoogleMapsRegistryBuilder,
    GoogleMapsRegistryWriter,
    MasterDataValidationError,
    MenuImageDownloader,
    PlaywrightBrowserClient,
    RawJsonWriter,
    SourceRegistry,
    SourceRegistryError,
    TrivagoHotelRegistry,
    TrivagoRegistryBuilder,
    TrivagoRegistryWriter,
)
from nextrip_pipeline.crawl.adapters import (
    GoogleMapsPlaceAdapter,
    TrivagoMcpDiscoveryAdapter,
    TrivagoMcpPriceAdapter,
    TrivagoPriceRequest,
)
from nextrip_pipeline.decision_gate import (
    GoogleMapsDecisionWriter,
    HotelPriceDecisionWriter,
    MenuDecisionWriter,
)
from nextrip_pipeline.jobs import (
    GoogleMapsBatchMode,
    GoogleMapsBatchRunner,
    GoogleMapsBatchSummaryWriter,
    GoogleMapsQualityReprocessor,
    GoogleMapsReprocessSummaryWriter,
    MENU_ENTITY_TYPES,
    GoogleMapsOpeningJob,
    GoogleMapsRefreshPipeline,
    HotelPriceRefreshPipeline,
    MasterPlaceBootstrapSummaryWriter,
    TrivagoHotelPriceJob,
    TrivagoBatchSummaryWriter,
    TrivagoMcpBatchRunner,
    TrivagoPriceBatchContext,
    TrivagoStayAvailabilityBatchRunner,
    TrivagoStayAvailabilityResultWriter,
    TrivagoStayAvailabilityRunner,
    TrivagoStayBatchSummaryWriter,
    VerifiedMasterCurrentPlaceWriter,
    VerifiedMasterPlaceBootstrapper,
    load_google_maps_manifest,
    MenuRefreshPipeline,
)
from nextrip_pipeline.preprocessing import (
    HotelPriceNormalizationError,
    GoogleMapsNormalizationError,
    NormalizedGoogleMapsWriter,
    NormalizedHotelPriceWriter,
    TrivagoMcpPriceNormalizer,
    MenuOcrCache,
    MenuOcrDependencyError,
    NormalizedMenuWriter,
    RapidOcrEngine,
)
from nextrip_pipeline.publishing import (
    CurrentHotelAvailabilityWriter,
    CurrentHotelPriceWriter,
    CurrentMenuWriter,
    CurrentPlaceWriter,
    GoogleMapsMenuSourceIndex,
)
from nextrip_pipeline.crawl.adapters.google_maps_discovery import (
    GoogleMapsCandidateDiscoveryAdapter,
)
from nextrip_pipeline.quality import (
    CurrentGoogleMapsMappingWriter,
    CurrentTrivagoMappingWriter,
    GoogleMapsMappingResolutionWriter,
    GoogleMapsMappingResolver,
    LLMReviewQueueConfig,
    LLMReviewRequestWriter,
    TrivagoDiscoveryAuditWriter,
)
from nextrip_pipeline.review import MenuReviewQueue
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    GoogleMapsPlaceObservation,
    NormalizedMenu,
    Occupancy,
    SourceRecord,
)
from nextrip_pipeline.validators import (
    HotelPriceValidatorOrchestrator,
    GoogleMapsValidationWriter,
    GoogleMapsValidatorOrchestrator,
    ValidationResultWriter,
    MenuValidationWriter,
    MenuValidatorOrchestrator,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextrip-pipeline",
        description="NexTrip automated data pipeline.",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "health",
        help="Check that the pipeline package is available.",
    )

    validate_sources = subparsers.add_parser(
        "validate-sources",
        help="Validate a JSON source registry before a crawl run.",
    )
    validate_sources.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the source registry JSON file.",
    )

    google_maps = subparsers.add_parser(
        "crawl-google-maps",
        help="Capture one Google Maps place page through Playwright.",
    )
    _add_browser_arguments(google_maps)
    google_maps.add_argument("--mapping", type=Path, required=True)

    hotel_prices = subparsers.add_parser(
        "crawl-hotel-prices",
        help="Capture one live hotel search through the official Trivago MCP.",
    )
    hotel_prices.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    hotel_prices.add_argument("--mapping", type=Path, required=True)
    hotel_prices.add_argument("--hotel-name", required=True)
    hotel_prices.add_argument("--destination", required=True)
    hotel_prices.add_argument("--check-in", required=True)
    hotel_prices.add_argument("--check-out", required=True)
    hotel_prices.add_argument("--adults", type=int, default=2)
    hotel_prices.add_argument("--rooms", type=int, default=1)
    hotel_prices.add_argument("--children", type=int, default=0)
    hotel_prices.add_argument(
        "--children-ages",
        type=int,
        nargs="*",
        default=[],
        help="Ages for every child, for example: --children-ages 6 10",
    )

    normalize_price = subparsers.add_parser(
        "normalize-hotel-price",
        help="Normalize one Trivago MCP SourceRecord into hotel price observations.",
    )
    normalize_price.add_argument("--input", type=Path, required=True)
    normalize_price.add_argument("--mapping", type=Path, required=True)
    normalize_price.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/normalized"),
    )

    refresh_price = subparsers.add_parser(
        "refresh-hotel-price",
        help="Run the complete hotel price pipeline and update current price.",
    )
    refresh_price.add_argument("--mapping", type=Path, required=True)
    refresh_price.add_argument("--hotel-name", required=True)
    refresh_price.add_argument("--destination", required=True)
    refresh_price.add_argument("--check-in", required=True)
    refresh_price.add_argument("--check-out", required=True)
    refresh_price.add_argument("--adults", type=int, default=2)
    refresh_price.add_argument("--rooms", type=int, default=1)
    refresh_price.add_argument("--children", type=int, default=0)
    refresh_price.add_argument("--children-ages", type=int, nargs="*", default=[])
    refresh_price.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    refresh_price.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    refresh_price.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    refresh_price.add_argument(
        "--decision-dir", type=Path, default=Path("data/decisions")
    )
    refresh_price.add_argument(
        "--current-dir", type=Path, default=Path("data/current/hotel_price")
    )

    build_trivago_registry = subparsers.add_parser(
        "build-trivago-registry",
        help="Build Trivago MCP search targets from verified hotel master data.",
    )
    build_trivago_registry.add_argument(
        "--master-file",
        type=Path,
        default=Path("travel_data_verified/hotel_final.json"),
    )
    build_trivago_registry.add_argument(
        "--override",
        type=Path,
        action="append",
        default=[],
        help="Confirmed Trivago mapping override; repeat as needed.",
    )
    build_trivago_registry.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/trivago_mappings"),
    )
    build_trivago_registry.add_argument(
        "--output",
        type=Path,
        default=Path("config/generated/trivago-hotel-registry.json"),
    )
    build_trivago_registry.add_argument(
        "--report",
        type=Path,
        default=Path("config/generated/trivago-registry-report.json"),
    )

    batch_trivago = subparsers.add_parser(
        "batch-trivago-prices",
        help="Discover Trivago IDs and refresh contextual hotel prices through MCP.",
    )
    batch_trivago.add_argument(
        "--registry",
        type=Path,
        default=Path("config/generated/trivago-hotel-registry.json"),
    )
    batch_trivago.add_argument("--check-in", type=date.fromisoformat)
    batch_trivago.add_argument("--check-out", type=date.fromisoformat)
    batch_trivago.add_argument("--check-in-offset-days", type=int, default=1)
    batch_trivago.add_argument("--stay-nights", type=int, default=1)
    batch_trivago.add_argument("--adults", type=int, default=2)
    batch_trivago.add_argument("--rooms", type=int, default=1)
    batch_trivago.add_argument("--children", type=int, default=0)
    batch_trivago.add_argument("--children-ages", type=int, nargs="*", default=[])
    batch_trivago.add_argument("--currency", default="VND")
    batch_trivago.add_argument("--max-requests", type=int)
    batch_trivago.add_argument("--offset", type=int, default=0)
    batch_trivago.add_argument("--entity-id", action="append", default=[])
    batch_trivago.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    batch_trivago.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    batch_trivago.add_argument(
        "--quality-dir", type=Path, default=Path("data/quality/trivago_mapping")
    )
    batch_trivago.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/trivago_mappings"),
    )
    batch_trivago.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    batch_trivago.add_argument(
        "--decision-dir", type=Path, default=Path("data/decisions")
    )
    batch_trivago.add_argument(
        "--current-price-dir",
        type=Path,
        default=Path("data/current/hotel_price"),
    )
    batch_trivago.add_argument(
        "--summary-dir", type=Path, default=Path("data/runs/trivago_mcp")
    )

    batch_availability = subparsers.add_parser(
        "batch-trivago-availability",
        help=(
            "Refresh exact hotel stays through Trivago MCP and safely try "
            "later check-in dates only after verified unavailability."
        ),
    )
    batch_availability.add_argument(
        "--registry",
        type=Path,
        default=Path("config/generated/trivago-hotel-registry.json"),
    )
    batch_availability.add_argument("--check-in", type=date.fromisoformat)
    batch_availability.add_argument(
        "--check-out",
        type=date.fromisoformat,
        help="Exact departure date; omit to derive it from --stay-nights.",
    )
    batch_availability.add_argument("--check-in-offset-days", type=int, default=1)
    batch_availability.add_argument(
        "--stay-nights",
        type=int,
        default=1,
        help="Full-stay duration used when --check-out is omitted.",
    )
    batch_availability.add_argument("--lookahead-days", type=int, default=1)
    batch_availability.add_argument("--adults", type=int, default=2)
    batch_availability.add_argument("--rooms", type=int, default=1)
    batch_availability.add_argument("--children", type=int, default=0)
    batch_availability.add_argument(
        "--children-ages", type=int, nargs="*", default=[]
    )
    batch_availability.add_argument("--currency", default="VND")
    batch_availability.add_argument("--max-requests", type=int)
    batch_availability.add_argument("--offset", type=int, default=0)
    batch_availability.add_argument("--entity-id", action="append", default=[])
    batch_availability.add_argument(
        "--raw-dir", type=Path, default=Path("data/raw")
    )
    batch_availability.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    batch_availability.add_argument(
        "--quality-dir", type=Path, default=Path("data/quality/trivago_mapping")
    )
    batch_availability.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/trivago_mappings"),
    )
    batch_availability.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    batch_availability.add_argument(
        "--decision-dir", type=Path, default=Path("data/decisions")
    )
    batch_availability.add_argument(
        "--current-price-dir",
        type=Path,
        default=Path("data/current/hotel_price"),
    )
    batch_availability.add_argument(
        "--current-availability-dir",
        type=Path,
        default=Path("data/current/hotel_availability"),
    )
    batch_availability.add_argument(
        "--stay-result-dir",
        type=Path,
        default=Path("data/runs/trivago_stay"),
    )
    batch_availability.add_argument(
        "--summary-dir",
        type=Path,
        default=Path("data/runs/trivago_availability_batch"),
    )

    refresh_maps = subparsers.add_parser(
        "refresh-google-place",
        help="Run the complete Google Maps place/status pipeline.",
    )
    _add_browser_arguments(refresh_maps)
    refresh_maps.add_argument("--mapping", type=Path, required=True)
    refresh_maps.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    refresh_maps.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    refresh_maps.add_argument(
        "--decision-dir", type=Path, default=Path("data/decisions")
    )
    _add_google_quality_arguments(refresh_maps)

    bootstrap_master = subparsers.add_parser(
        "bootstrap-master-places",
        help=(
            "Seed current place JSON from verified master data without "
            "crawling or review."
        ),
    )
    bootstrap_master.add_argument(
        "--master-dir", type=Path, default=Path("travel_data_verified")
    )
    bootstrap_master.add_argument(
        "--current-place-dir", type=Path, default=Path("data/current/place")
    )
    bootstrap_master.add_argument(
        "--summary-dir",
        type=Path,
        default=Path("data/runs/master_place_bootstrap"),
    )

    refresh_menu = subparsers.add_parser(
        "refresh-place-menu",
        help="Download one mapped Google menu image and extract menu items locally.",
    )
    refresh_menu.add_argument("--mapping", type=Path, required=True)
    refresh_menu.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    refresh_menu.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    refresh_menu.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    refresh_menu.add_argument(
        "--decision-dir", type=Path, default=Path("data/decisions")
    )
    refresh_menu.add_argument(
        "--cache-dir", type=Path, default=Path("data/cache/menu_ocr")
    )
    refresh_menu.add_argument(
        "--review-dir", type=Path, default=Path("data/review/menu")
    )

    list_menu_reviews = subparsers.add_parser(
        "list-menu-reviews", help="List unresolved menu review tasks."
    )
    list_menu_reviews.add_argument(
        "--review-dir", type=Path, default=Path("data/review/menu")
    )

    export_menu_review = subparsers.add_parser(
        "export-menu-review", help="Export a review candidate for manual editing."
    )
    export_menu_review.add_argument("--review-id", required=True)
    export_menu_review.add_argument("--output", type=Path, required=True)
    export_menu_review.add_argument(
        "--review-dir", type=Path, default=Path("data/review/menu")
    )

    approve_menu = subparsers.add_parser(
        "approve-menu", help="Approve an edited menu and publish it as current."
    )
    approve_menu.add_argument("--review-id", required=True)
    approve_menu.add_argument("--input", type=Path, required=True)
    approve_menu.add_argument("--reviewer", required=True)
    approve_menu.add_argument(
        "--review-dir", type=Path, default=Path("data/review/menu")
    )
    approve_menu.add_argument(
        "--current-dir", type=Path, default=Path("data/current/menu")
    )

    reject_menu = subparsers.add_parser(
        "reject-menu", help="Reject a menu review task with an audit reason."
    )
    reject_menu.add_argument("--review-id", required=True)
    reject_menu.add_argument("--reviewer", required=True)
    reject_menu.add_argument("--reason", required=True)
    reject_menu.add_argument(
        "--review-dir", type=Path, default=Path("data/review/menu")
    )

    batch_maps = subparsers.add_parser(
        "batch-google-maps",
        help="Run a bounded Google Maps place or menu batch from a manifest.",
    )
    _add_browser_arguments(batch_maps)
    batch_maps.add_argument("--manifest", type=Path, required=True)
    batch_maps.add_argument(
        "--mode", choices=[item.value for item in GoogleMapsBatchMode], required=True
    )
    batch_maps.add_argument("--max-requests", type=int, default=32)
    batch_maps.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Manual start offset; omit to use automatic daily rotation.",
    )
    batch_maps.add_argument(
        "--exclude-entity-id",
        action="append",
        default=[],
        help="Entity ID to skip; repeat this argument as needed.",
    )
    batch_maps.add_argument(
        "--entity-id",
        action="append",
        default=[],
        help=(
            "Only process the selected entity ID; repeat this argument as "
            "needed. Intended for targeted canonical review."
        ),
    )
    batch_maps.add_argument(
        "--entity-type",
        action="append",
        choices=[item.value for item in EntityType],
        default=[],
        help="Only process selected entity types; repeat this argument as needed.",
    )
    batch_maps.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    batch_maps.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    batch_maps.add_argument("--decision-dir", type=Path, default=Path("data/decisions"))
    batch_maps.add_argument("--review-dir", type=Path, default=Path("data/review/menu"))
    batch_maps.add_argument(
        "--cache-dir", type=Path, default=Path("data/cache/menu_ocr")
    )
    batch_maps.add_argument(
        "--summary-dir", type=Path, default=Path("data/runs/google_maps")
    )
    batch_maps.add_argument(
        "--menu-source-dir",
        type=Path,
        default=Path("data/current/google_maps_menu_sources"),
    )
    _add_google_quality_arguments(batch_maps)

    reprocess_maps = subparsers.add_parser(
        "reprocess-google-maps",
        help="Re-run Google Maps quality gates from normalized JSON without crawling.",
    )
    reprocess_maps.add_argument("--manifest", type=Path, required=True)
    reprocess_maps.add_argument(
        "--observation-root",
        type=Path,
        default=Path("data/normalized"),
    )
    reprocess_maps.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    reprocess_maps.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    reprocess_maps.add_argument(
        "--decision-dir", type=Path, default=Path("data/decisions")
    )
    reprocess_maps.add_argument(
        "--summary-dir",
        type=Path,
        default=Path("data/runs/google_maps_reprocess"),
    )
    reprocess_maps.add_argument(
        "--entity-type",
        action="append",
        choices=[item.value for item in EntityType],
        default=[],
        help=(
            "Only reprocess selected entity types; repeat as needed. "
            "Defaults to attraction, cafe, nightlife, and restaurant."
        ),
    )
    _add_google_quality_arguments(reprocess_maps)

    build_maps_registry = subparsers.add_parser(
        "build-google-maps-registry",
        help="Generate Google Maps mappings from all verified master data.",
    )
    build_maps_registry.add_argument(
        "--master-dir", type=Path, default=Path("travel_data_verified")
    )
    build_maps_registry.add_argument(
        "--output",
        type=Path,
        default=Path("config/generated/google-maps-mapping-registry.json"),
    )
    build_maps_registry.add_argument(
        "--report",
        type=Path,
        default=Path("config/generated/google-maps-registry-report.json"),
    )
    build_maps_registry.add_argument(
        "--override",
        type=Path,
        action="append",
        default=[],
        help="Hand-verified mapping override; repeat this argument as needed.",
    )

    audit_canonical = subparsers.add_parser(
        "audit-canonical-identities",
        help=(
            "Audit name-based duplicate candidates against master data and "
            "latest Google Maps observations without changing master data."
        ),
    )
    audit_canonical.add_argument(
        "--candidate-groups",
        type=Path,
        default=Path("config/generated/google-maps-registry-report.json"),
    )
    audit_canonical.add_argument(
        "--master-dir", type=Path, default=Path("travel_data_verified")
    )
    audit_canonical.add_argument(
        "--observation-root",
        type=Path,
        default=Path("data/normalized/entity=opening_status"),
    )
    audit_canonical.add_argument(
        "--output",
        type=Path,
        default=Path("data/reports/canonical/duplicate-evidence.json"),
    )

    build_canonical = subparsers.add_parser(
        "build-canonical-manifest",
        help=(
            "Build the canonical ID/alias/tombstone/vacancy manifest from "
            "verified master data and explicit duplicate decisions."
        ),
    )
    build_canonical.add_argument(
        "--master-dir", type=Path, default=Path("travel_data_verified")
    )
    build_canonical.add_argument(
        "--decisions",
        type=Path,
        default=Path("config/canonical-identity-decisions.json"),
    )
    build_canonical.add_argument("--previous-manifest", type=Path, default=None)
    build_canonical.add_argument(
        "--approved-replacement-dir",
        type=Path,
        default=Path("data/canonical/replacements"),
        help="Immutable approved replacement overlay; verified master is unchanged.",
    )
    build_canonical.add_argument(
        "--output",
        type=Path,
        default=Path("config/generated/canonical-identity-manifest.json"),
    )
    build_canonical.add_argument(
        "--report",
        type=Path,
        default=Path("data/reports/canonical/manifest-build.json"),
    )
    build_canonical.add_argument(
        "--ignore-master-duplicate-tags",
        action="store_true",
        help="Do not apply explicit duplicate_record/duplicate_of_* master tags.",
    )

    propose_replacements = subparsers.add_parser(
        "propose-canonical-replacements",
        help=(
            "Discover, detail-check, and propose globally distinct Google Maps "
            "places for open canonical quota vacancies."
        ),
    )
    _add_browser_arguments(propose_replacements)
    propose_replacements.add_argument(
        "--manifest",
        type=Path,
        default=Path("config/generated/canonical-identity-manifest.json"),
    )
    propose_replacements.add_argument(
        "--master-dir", type=Path, default=Path("travel_data_verified")
    )
    propose_replacements.add_argument(
        "--current-place-dir",
        type=Path,
        default=Path("data/current/place"),
    )
    propose_replacements.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/google_maps_mappings"),
    )
    propose_replacements.add_argument(
        "--approved-replacement-dir",
        type=Path,
        default=Path("data/canonical/replacements"),
    )
    propose_replacements.add_argument(
        "--stage-dir",
        type=Path,
        default=Path("data/review/canonical/discovery"),
    )
    propose_replacements.add_argument(
        "--detail-dir",
        type=Path,
        default=Path("data/review/canonical/detail"),
    )
    propose_replacements.add_argument(
        "--summary-dir",
        type=Path,
        default=Path("data/runs/canonical_replacements"),
    )
    propose_replacements.add_argument("--result-limit", type=int, default=30)
    propose_replacements.add_argument(
        "--max-detail-candidates", type=int, default=20
    )
    propose_replacements.add_argument("--max-vacancies", type=int, default=100)
    propose_replacements.add_argument(
        "--entity-type",
        action="append",
        choices=[item.value for item in EntityType],
        default=[],
    )
    propose_replacements.add_argument(
        "--city-id",
        action="append",
        choices=["city_da_nang", "city_quy_nhon"],
        default=[],
    )
    propose_replacements.add_argument(
        "--vacancy-id",
        action="append",
        default=[],
        help="Only process selected vacancy IDs; repeat as needed.",
    )
    propose_replacements.add_argument(
        "--search-term",
        default=None,
        help=(
            "Override the Maps query term. Requires exactly one --entity-type; "
            "the city name is appended automatically."
        ),
    )

    apply_replacements = subparsers.add_parser(
        "apply-canonical-replacements",
        help=(
            "Materialize selected PASS proposals as an immutable replacement "
            "overlay; original verified master files are never changed."
        ),
    )
    apply_replacements.add_argument("--batch", type=Path, required=True)
    apply_replacements.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/canonical/replacements"),
    )
    apply_replacements.add_argument(
        "--proposal-id",
        action="append",
        default=[],
        help="Apply selected proposal IDs; omit to apply every PASS proposal.",
    )
    apply_replacements.add_argument(
        "--method",
        choices=[item.value for item in ReplacementApprovalMethod],
        default=ReplacementApprovalMethod.DETERMINISTIC.value,
    )
    apply_replacements.add_argument(
        "--reviewer",
        default="canonical-deterministic-gate",
        help="Human reviewer or deterministic policy identity recorded in provenance.",
    )

    materialize_canonical = subparsers.add_parser(
        "materialize-canonical-dataset",
        help=(
            "Materialize one immutable active record per canonical identity "
            "and fail the Neo4j V8 gate while duplicate evidence is unresolved."
        ),
    )
    materialize_canonical.add_argument(
        "--master-dir", type=Path, default=Path("travel_data_verified")
    )
    materialize_canonical.add_argument(
        "--manifest",
        type=Path,
        default=Path("config/generated/canonical-identity-manifest.json"),
    )
    materialize_canonical.add_argument(
        "--decisions",
        type=Path,
        default=Path("config/canonical-identity-decisions.json"),
        help="Explicit merge/distinct decision document used by the readiness gate.",
    )
    materialize_canonical.add_argument(
        "--approved-replacement-dir",
        type=Path,
        default=Path("data/canonical/replacements"),
    )
    materialize_canonical.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/canonical/datasets"),
    )
    materialize_canonical.add_argument(
        "--duplicate-evidence",
        type=Path,
        default=Path("data/reports/canonical/duplicate-evidence.json"),
    )
    materialize_canonical.add_argument(
        "--review-correction",
        type=Path,
        default=None,
        help=(
            "Optional immutable Google-backed field overlay for unresolved "
            "REVIEW identities; this never resolves or merges a review group."
        ),
    )
    materialize_canonical.add_argument(
        "--readiness-output-dir",
        type=Path,
        default=Path("data/canonical/readiness"),
    )
    materialize_canonical.add_argument(
        "--allow-unresolved-review",
        action="store_true",
        help=(
            "Return success for a shadow dataset even when readiness is false; "
            "never use this flag on the Neo4j V8 publish task."
        ),
    )
    return parser


def _add_browser_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--artifact-dir", type=Path, default=Path("data/crawl_artifacts")
    )
    parser.add_argument("--headed", action="store_true")


def _add_google_quality_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--quality-dir",
        type=Path,
        default=Path("data/quality/google_maps_mapping"),
    )
    parser.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/google_maps_mappings"),
    )
    parser.add_argument(
        "--mapping-review-dir",
        type=Path,
        default=Path("data/review/google_maps_mapping"),
    )
    parser.add_argument(
        "--current-place-dir",
        type=Path,
        default=Path("data/current/place"),
    )
    parser.add_argument("--llm-max-requests", type=int, default=25)
    parser.add_argument("--llm-max-tokens", type=int, default=25_000)
    parser.add_argument(
        "--disable-llm-review-queue",
        action="store_true",
        help="Run deterministic quality gates without adding LLM review requests.",
    )


def _load_mapping(path: Path) -> ExternalEntityMapping:
    return ExternalEntityMapping.model_validate_json(path.read_text(encoding="utf-8"))


def _load_source_record(path: Path) -> SourceRecord:
    return SourceRecord.model_validate_json(path.read_text(encoding="utf-8"))


def _load_normalized_menu(path: Path) -> NormalizedMenu:
    return NormalizedMenu.model_validate_json(path.read_text(encoding="utf-8"))


def _load_trivago_registry(path: Path) -> TrivagoHotelRegistry:
    return TrivagoHotelRegistry.model_validate_json(path.read_text(encoding="utf-8"))


def _price_request(arguments: argparse.Namespace) -> TrivagoPriceRequest:
    return TrivagoPriceRequest(
        hotel_name=arguments.hotel_name,
        destination=arguments.destination,
        check_in=arguments.check_in,
        check_out=arguments.check_out,
        occupancy=Occupancy(
            adults=arguments.adults,
            children=arguments.children,
            rooms=arguments.rooms,
        ),
        children_ages=arguments.children_ages,
    )


def _new_price_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"hotel-price-{timestamp}-{uuid4().hex[:8]}"


def _new_maps_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"opening-status-{timestamp}-{uuid4().hex[:8]}"


def _new_menu_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"menu-{timestamp}-{uuid4().hex[:8]}"


def _new_google_batch_run_id(mode: GoogleMapsBatchMode) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"google-maps-{mode.value}-{timestamp}-{uuid4().hex[:8]}"


def _new_google_reprocess_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"google-maps-reprocess-{timestamp}-{uuid4().hex[:8]}"


def _new_master_bootstrap_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"master-place-bootstrap-{timestamp}-{uuid4().hex[:8]}"


def _new_trivago_batch_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"trivago-mcp-{timestamp}-{uuid4().hex[:8]}"


def _new_trivago_availability_batch_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"trivago-availability-{timestamp}-{uuid4().hex[:8]}"


def _trivago_batch_dates(arguments: argparse.Namespace) -> tuple[date, date]:
    if (arguments.check_in is None) != (arguments.check_out is None):
        raise ValueError("--check-in and --check-out must be provided together")
    if arguments.check_in is not None:
        return arguments.check_in, arguments.check_out
    if arguments.check_in_offset_days < 0:
        raise ValueError("--check-in-offset-days cannot be negative")
    if arguments.stay_nights < 1:
        raise ValueError("--stay-nights must be positive")
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    check_in = today + timedelta(days=arguments.check_in_offset_days)
    return check_in, check_in + timedelta(days=arguments.stay_nights)


def _trivago_availability_dates(
    arguments: argparse.Namespace,
) -> tuple[date, date]:
    if arguments.stay_nights < 1:
        raise ValueError("--stay-nights must be positive")
    if arguments.check_out is not None and arguments.check_in is None:
        raise ValueError("--check-out requires --check-in")
    if arguments.check_in is not None:
        return (
            arguments.check_in,
            arguments.check_out
            or arguments.check_in + timedelta(days=arguments.stay_nights),
        )
    if arguments.check_in_offset_days < 0:
        raise ValueError("--check-in-offset-days cannot be negative")
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    check_in = today + timedelta(days=arguments.check_in_offset_days)
    return check_in, check_in + timedelta(days=arguments.stay_nights)


def _google_maps_refresh_pipeline(
    arguments: argparse.Namespace,
    browser: PlaywrightBrowserClient,
) -> GoogleMapsRefreshPipeline:
    return GoogleMapsRefreshPipeline(
        GoogleMapsPlaceAdapter(browser),
        RawJsonWriter(arguments.raw_dir),
        NormalizedGoogleMapsWriter(arguments.normalized_dir),
        GoogleMapsValidationWriter(arguments.validation_dir),
        GoogleMapsDecisionWriter(arguments.decision_dir),
        validator=GoogleMapsValidatorOrchestrator(),
        mapping_resolver=GoogleMapsMappingResolver(),
        resolution_writer=GoogleMapsMappingResolutionWriter(arguments.quality_dir),
        current_mapping_writer=CurrentGoogleMapsMappingWriter(
            arguments.current_mapping_dir
        ),
        llm_review_writer=(
            None
            if arguments.disable_llm_review_queue
            else LLMReviewRequestWriter(
                LLMReviewQueueConfig(
                    root_directory=arguments.mapping_review_dir,
                    max_requests_per_run=arguments.llm_max_requests,
                    max_tokens_per_run=arguments.llm_max_tokens,
                )
            )
        ),
        current_place_writer=CurrentPlaceWriter(arguments.current_place_dir),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "health":
        print("NexTrip data pipeline is ready.")
        return 0

    if arguments.command == "validate-sources":
        try:
            registry = SourceRegistry.load(arguments.config)
        except SourceRegistryError as error:
            print(error, file=sys.stderr)
            return 2

        print(
            f"Source registry is valid: {len(registry.document.sources)} sources, "
            f"{len(registry.enabled_sources())} enabled."
        )
        return 0

    if arguments.command in {"crawl-google-maps", "crawl-hotel-prices"}:
        try:
            mapping = _load_mapping(arguments.mapping)
            writer = RawJsonWriter(arguments.raw_dir)
            if arguments.command == "crawl-google-maps":
                browser = PlaywrightBrowserClient(
                    headless=not arguments.headed,
                    artifact_directory=arguments.artifact_dir,
                )
                adapter = GoogleMapsPlaceAdapter(browser)
                result = GoogleMapsOpeningJob(adapter, writer).run([mapping])
            else:
                adapter = TrivagoMcpPriceAdapter()
                request = _price_request(arguments)
                result = TrivagoHotelPriceJob(adapter, writer).run([(mapping, request)])
        except (OSError, ValidationError, ValueError, RuntimeError) as error:
            print(f"Crawl failed: {error}", file=sys.stderr)
            return 2
        if not result.succeeded:
            print(result.failures[0].error, file=sys.stderr)
            return 1
        print(result.written_paths[0])
        return 0

    if arguments.command == "normalize-hotel-price":
        try:
            source_record = _load_source_record(arguments.input)
            mapping = _load_mapping(arguments.mapping)
            observations = TrivagoMcpPriceNormalizer().normalize(
                source_record,
                mapping,
            )
            writer = NormalizedHotelPriceWriter(arguments.output_dir)
            paths = [writer.write(observation) for observation in observations]
        except (
            OSError,
            ValidationError,
            HotelPriceNormalizationError,
        ) as error:
            print(f"Normalization failed: {error}", file=sys.stderr)
            return 2
        for path in paths:
            print(path)
        return 0

    if arguments.command == "refresh-hotel-price":
        try:
            mapping = _load_mapping(arguments.mapping)
            pipeline = HotelPriceRefreshPipeline(
                TrivagoMcpPriceAdapter(),
                RawJsonWriter(arguments.raw_dir),
                NormalizedHotelPriceWriter(arguments.normalized_dir),
                ValidationResultWriter(arguments.validation_dir),
                HotelPriceDecisionWriter(arguments.decision_dir),
                CurrentHotelPriceWriter(arguments.current_dir),
                validator=HotelPriceValidatorOrchestrator(),
            )
            result = pipeline.run(
                mapping,
                _price_request(arguments),
                run_id=_new_price_run_id(),
            )
        except (
            OSError,
            httpx.HTTPError,
            ValidationError,
            HotelPriceNormalizationError,
            ValueError,
        ) as error:
            print(f"Hotel price refresh failed: {error}", file=sys.stderr)
            return 2
        print(f"raw={result.raw_path}")
        for decision, path in zip(result.decisions, result.decision_paths):
            print(f"decision={decision.status.value} path={path}")
        for path in result.current_paths:
            print(f"current={path}")
        return 0

    if arguments.command == "build-trivago-registry":
        try:
            override_paths = list(arguments.override)
            default_override = Path("config/trivago-mapping.json")
            if not override_paths and default_override.exists():
                override_paths.append(default_override)

            overrides_by_entity = {
                mapping.entity_id: mapping
                for mapping in (_load_mapping(path) for path in override_paths)
            }
            # A mapping discovered by an earlier MCP run is newer than the
            # checked-in bootstrap mapping and therefore takes precedence.
            overrides_by_entity.update(
                {
                    mapping.entity_id: mapping
                    for mapping in CurrentTrivagoMappingWriter(
                        arguments.current_mapping_dir
                    ).all()
                }
            )
            registry, report = TrivagoRegistryBuilder().build(
                arguments.master_file,
                overrides=list(overrides_by_entity.values()),
            )
            registry_path = TrivagoRegistryWriter.write(registry, arguments.output)
            report_path = TrivagoRegistryWriter.write(report, arguments.report)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot build Trivago registry: {error}", file=sys.stderr)
            return 2
        print(f"registry={registry_path}")
        print(f"report={report_path}")
        print(
            f"hotels={report.registry_count} "
            f"confirmed={report.status_counts.get('confirmed', 0)} "
            f"unresolved={report.status_counts.get('unresolved', 0)} "
            f"overrides={report.overrides_applied}"
        )
        return 0

    if arguments.command == "batch-trivago-prices":
        try:
            registry = _load_trivago_registry(arguments.registry)
            check_in, check_out = _trivago_batch_dates(arguments)
            context = TrivagoPriceBatchContext(
                check_in=check_in,
                check_out=check_out,
                occupancy=Occupancy(
                    adults=arguments.adults,
                    children=arguments.children,
                    rooms=arguments.rooms,
                ),
                children_ages=arguments.children_ages,
                currency=arguments.currency.upper(),
            )
            summary, summary_path = TrivagoMcpBatchRunner(
                TrivagoMcpDiscoveryAdapter(),
                RawJsonWriter(arguments.raw_dir),
                NormalizedHotelPriceWriter(arguments.normalized_dir),
                TrivagoDiscoveryAuditWriter(arguments.quality_dir),
                CurrentTrivagoMappingWriter(arguments.current_mapping_dir),
                TrivagoBatchSummaryWriter(arguments.summary_dir),
                validation_writer=ValidationResultWriter(arguments.validation_dir),
                decision_writer=HotelPriceDecisionWriter(arguments.decision_dir),
                current_writer=CurrentHotelPriceWriter(arguments.current_price_dir),
                validator=HotelPriceValidatorOrchestrator(),
                max_requests=arguments.max_requests,
                offset=arguments.offset,
                entity_ids=arguments.entity_id,
            ).run(
                registry,
                context,
                run_id=_new_trivago_batch_run_id(),
            )
        except (
            OSError,
            httpx.HTTPError,
            ValidationError,
            ValueError,
            RuntimeError,
        ) as error:
            print(f"Trivago MCP batch failed: {error}", file=sys.stderr)
            return 2
        print(f"summary={summary_path}")
        print(
            f"selected={summary.selected_count} "
            f"succeeded={summary.succeeded_count} "
            f"unresolved={summary.unresolved_count} "
            f"failed={summary.failed_count}"
        )
        print(
            f"normalized={summary.normalized_observation_count} "
            f"validated={summary.validation_count} "
            f"pass={summary.decision_counts.get('pass', 0)} "
            f"quarantine={summary.decision_counts.get('quarantine', 0)} "
            f"current={summary.published_current_count}"
        )
        for item in summary.items:
            if item.status.value == "failed":
                print(
                    f"failed entity_id={item.entity_id} "
                    f"stage={item.error_stage} error={item.error}",
                    file=sys.stderr,
                )
        return 1 if summary.failed_count else 0

    if arguments.command == "batch-trivago-availability":
        try:
            registry = _load_trivago_registry(arguments.registry)
            check_in, check_out = _trivago_availability_dates(arguments)
            context = TrivagoPriceBatchContext(
                check_in=check_in,
                check_out=check_out,
                occupancy=Occupancy(
                    adults=arguments.adults,
                    children=arguments.children,
                    rooms=arguments.rooms,
                ),
                children_ages=arguments.children_ages,
                currency=arguments.currency.upper(),
            )
            stay_runner = TrivagoStayAvailabilityRunner(
                TrivagoMcpDiscoveryAdapter(),
                RawJsonWriter(arguments.raw_dir),
                NormalizedHotelPriceWriter(arguments.normalized_dir),
                TrivagoDiscoveryAuditWriter(arguments.quality_dir),
                CurrentTrivagoMappingWriter(arguments.current_mapping_dir),
                CurrentHotelAvailabilityWriter(
                    arguments.current_availability_dir
                ),
                validation_writer=ValidationResultWriter(
                    arguments.validation_dir
                ),
                decision_writer=HotelPriceDecisionWriter(arguments.decision_dir),
                current_price_writer=CurrentHotelPriceWriter(
                    arguments.current_price_dir
                ),
                validator=HotelPriceValidatorOrchestrator(),
            )
            summary, summary_path = TrivagoStayAvailabilityBatchRunner(
                stay_runner,
                TrivagoStayAvailabilityResultWriter(
                    arguments.stay_result_dir
                ),
                TrivagoStayBatchSummaryWriter(arguments.summary_dir),
                max_requests=arguments.max_requests,
                offset=arguments.offset,
                entity_ids=arguments.entity_id,
            ).run(
                registry,
                context,
                run_id=_new_trivago_availability_batch_run_id(),
                lookahead_days=arguments.lookahead_days,
            )
        except (
            OSError,
            httpx.HTTPError,
            ValidationError,
            ValueError,
            RuntimeError,
        ) as error:
            print(f"Trivago availability batch failed: {error}", file=sys.stderr)
            return 2
        print(f"summary={summary_path}")
        print(
            f"selected={summary.selected_count} "
            f"completed={summary.completed_count} "
            f"failed={summary.failed_count}"
        )
        print(
            f"available={summary.availability_counts.get('available', 0)} "
            f"unavailable={summary.availability_counts.get('unavailable', 0)} "
            f"unknown={summary.availability_counts.get('unknown', 0)}"
        )
        for item in summary.items:
            if item.status.value == "failed":
                print(
                    f"failed entity_id={item.hotel_id} "
                    f"stage={item.error_stage} error={item.error}",
                    file=sys.stderr,
                )
        return 1 if summary.failed_count else 0

    if arguments.command == "refresh-google-place":
        try:
            mapping = _load_mapping(arguments.mapping)
            browser = PlaywrightBrowserClient(
                headless=not arguments.headed,
                artifact_directory=arguments.artifact_dir,
            )
            pipeline = _google_maps_refresh_pipeline(arguments, browser)
            result = pipeline.run(mapping, run_id=_new_maps_run_id())
        except (
            OSError,
            ValidationError,
            GoogleMapsNormalizationError,
            ValueError,
            RuntimeError,
        ) as error:
            print(f"Google Maps refresh failed: {error}", file=sys.stderr)
            return 2
        print(f"raw={result.raw_path}")
        print(f"normalized={result.normalized_path}")
        if result.decision is not None:
            print(
                f"decision={result.decision.status.value} path={result.decision_path}"
            )
            if result.decision.reason_codes:
                print(f"reason_codes={','.join(result.decision.reason_codes)}")
        if result.mapping_resolution is not None:
            print(f"mapping_resolution={result.mapping_resolution.status.value}")
        if result.current_mapping_path is not None:
            print(f"current_mapping={result.current_mapping_path}")
        if result.current_place_path is not None:
            print(f"current_place={result.current_place_path}")
        if result.llm_review_receipt is not None:
            print(
                "llm_review="
                f"{result.llm_review_receipt.disposition.value}"
            )
        return 0

    if arguments.command == "bootstrap-master-places":
        try:
            summary, summary_path = VerifiedMasterPlaceBootstrapper(
                VerifiedMasterCurrentPlaceWriter(arguments.current_place_dir),
                MasterPlaceBootstrapSummaryWriter(arguments.summary_dir),
            ).run(
                arguments.master_dir,
                run_id=_new_master_bootstrap_run_id(),
            )
        except (OSError, ValidationError, ValueError) as error:
            print(f"Master place bootstrap failed: {error}", file=sys.stderr)
            return 2
        print(f"summary={summary_path}")
        print(
            f"records={summary.source_record_count} seeded={summary.seeded_count} "
            f"skipped_existing={summary.skipped_existing_count} "
            f"failed={summary.failed_count}"
        )
        return 1 if summary.failed_count else 0

    if arguments.command == "reprocess-google-maps":
        try:
            mappings = load_google_maps_manifest(arguments.manifest)
            selected_types = set(arguments.entity_type) or {
                EntityType.ATTRACTION.value,
                EntityType.CAFE.value,
                EntityType.NIGHTLIFE.value,
                EntityType.RESTAURANT.value,
            }
            mappings = [
                mapping
                for mapping in mappings
                if mapping.entity_type is not None
                and mapping.entity_type.value in selected_types
            ]
            processor = GoogleMapsQualityReprocessor(
                NormalizedGoogleMapsWriter(arguments.normalized_dir),
                GoogleMapsMappingResolutionWriter(arguments.quality_dir),
                GoogleMapsValidationWriter(arguments.validation_dir),
                GoogleMapsDecisionWriter(arguments.decision_dir),
                CurrentGoogleMapsMappingWriter(arguments.current_mapping_dir),
                CurrentPlaceWriter(arguments.current_place_dir),
                GoogleMapsReprocessSummaryWriter(arguments.summary_dir),
                llm_review_writer=(
                    None
                    if arguments.disable_llm_review_queue
                    else LLMReviewRequestWriter(
                        LLMReviewQueueConfig(
                            root_directory=arguments.mapping_review_dir,
                            max_requests_per_run=arguments.llm_max_requests,
                            max_tokens_per_run=arguments.llm_max_tokens,
                        )
                    )
                ),
            )
            result = processor.run(
                mappings,
                observation_root=arguments.observation_root,
                run_id=_new_google_reprocess_run_id(),
            )
        except (
            OSError,
            ValidationError,
            ValueError,
            RuntimeError,
        ) as error:
            print(f"Google Maps reprocess failed: {error}", file=sys.stderr)
            return 2
        summary = result.summary
        print(f"summary={result.summary_path}")
        print(
            f"selected={summary.selected_observation_count} "
            f"processed={summary.processed_count} "
            f"succeeded={summary.succeeded_count} failed={summary.failed_count}"
        )
        print(
            f"coordinate_fallback={summary.coordinate_fallback_count} "
            f"published_mappings={summary.published_mapping_count} "
            f"published_places={summary.published_place_count} "
            f"llm_queued={summary.llm_queued_count}"
        )
        print(
            f"missing_mappings={len(summary.missing_mapping_place_ids)} "
            f"missing_observations={len(summary.missing_observation_place_ids)} "
            f"errors={len(summary.errors)}"
        )
        for item in summary.items:
            if item.status.value == "failed":
                print(
                    f"failed place_id={item.place_id} error={item.error}",
                    file=sys.stderr,
                )
        return 1 if summary.failed_count else 0

    if arguments.command == "refresh-place-menu":
        try:
            mapping = _load_mapping(arguments.mapping)
            pipeline = MenuRefreshPipeline(
                MenuImageDownloader(),
                RawJsonWriter(arguments.raw_dir),
                NormalizedMenuWriter(arguments.normalized_dir),
                MenuValidationWriter(arguments.validation_dir),
                MenuDecisionWriter(arguments.decision_dir),
                RapidOcrEngine(),
                MenuOcrCache(arguments.cache_dir),
                validator=MenuValidatorOrchestrator(),
                review_queue=MenuReviewQueue(arguments.review_dir),
            )
            result = pipeline.run(mapping, run_id=_new_menu_run_id())
        except (
            OSError,
            httpx.HTTPError,
            ValidationError,
            MenuOcrDependencyError,
            ValueError,
            RuntimeError,
        ) as error:
            print(f"Menu refresh failed: {error}", file=sys.stderr)
            return 2
        print(f"raw_record={result.raw_record_path}")
        print(f"raw_image={result.raw_image_path}")
        print(f"normalized={result.normalized_path}")
        print(f"ocr_cache_hit={str(result.ocr_cache_hit).lower()}")
        if result.review_task_path is not None:
            print(f"review_task={result.review_task_path}")
            print(f"review_created={str(result.review_created).lower()}")
        if result.decision is not None:
            print(
                f"decision={result.decision.status.value} path={result.decision_path}"
            )
            if result.decision.reason_codes:
                print(f"reason_codes={','.join(result.decision.reason_codes)}")
        return 0

    if arguments.command == "list-menu-reviews":
        try:
            tasks = MenuReviewQueue(arguments.review_dir).list_pending()
        except (OSError, ValidationError) as error:
            print(f"Cannot list menu reviews: {error}", file=sys.stderr)
            return 2
        for task in tasks:
            print(
                f"{task.review_id}\t{task.place_id}\t"
                f"items={len(task.candidate.items)}\t"
                f"reasons={','.join(task.reason_codes)}"
            )
        print(f"pending={len(tasks)}")
        return 0

    if arguments.command == "export-menu-review":
        try:
            path = MenuReviewQueue(arguments.review_dir).export_candidate(
                arguments.review_id, arguments.output
            )
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot export menu review: {error}", file=sys.stderr)
            return 2
        print(path)
        return 0

    if arguments.command == "approve-menu":
        try:
            queue = MenuReviewQueue(arguments.review_dir)
            resolution, resolution_path = queue.approve(
                arguments.review_id,
                _load_normalized_menu(arguments.input),
                reviewer=arguments.reviewer,
            )
            current_path = CurrentMenuWriter(arguments.current_dir).publish(resolution)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot approve menu: {error}", file=sys.stderr)
            return 2
        print(f"resolution={resolution_path}")
        print(f"current={current_path}")
        return 0

    if arguments.command == "reject-menu":
        try:
            _, resolution_path = MenuReviewQueue(arguments.review_dir).reject(
                arguments.review_id,
                reviewer=arguments.reviewer,
                reason=arguments.reason,
            )
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot reject menu: {error}", file=sys.stderr)
            return 2
        print(f"resolution={resolution_path}")
        return 0

    if arguments.command == "batch-google-maps":
        try:
            mode = GoogleMapsBatchMode(arguments.mode)
            mappings = load_google_maps_manifest(arguments.manifest)
            excluded_ids = set(arguments.exclude_entity_id)
            requested_ids = set(arguments.entity_id)
            unknown_ids = requested_ids - {
                mapping.entity_id for mapping in mappings
            }
            if unknown_ids:
                raise ValueError(
                    "unknown Google Maps entity IDs: "
                    + ", ".join(sorted(unknown_ids))
                )
            selected_types = set(arguments.entity_type)
            mappings = [
                mapping
                for mapping in mappings
                if mapping.entity_id not in excluded_ids
                and (
                    not requested_ids
                    or mapping.entity_id in requested_ids
                )
                and (
                    not selected_types
                    or (
                        mapping.entity_type is not None
                        and mapping.entity_type.value in selected_types
                    )
                )
            ]
            if mode is GoogleMapsBatchMode.PLACE:
                browser = PlaywrightBrowserClient(
                    headless=not arguments.headed,
                    artifact_directory=arguments.artifact_dir,
                )
                pipeline = _google_maps_refresh_pipeline(arguments, browser)
                menu_source_index = GoogleMapsMenuSourceIndex(arguments.menu_source_dir)

                def processor(mapping, effective_run_id):
                    result = pipeline.run(mapping, run_id=effective_run_id)
                    observation = GoogleMapsPlaceObservation.model_validate_json(
                        result.normalized_path.read_text(encoding="utf-8")
                    )
                    if mapping.entity_type in MENU_ENTITY_TYPES:
                        menu_source_index.publish(observation)
                    return result

            else:
                menu_source_index = GoogleMapsMenuSourceIndex(arguments.menu_source_dir)
                enriched_mappings = []
                for mapping in mappings:
                    entry = menu_source_index.get(mapping.entity_id)
                    if entry is not None and not mapping.attributes.get(
                        "verified_menu_image_url"
                    ):
                        mapping = mapping.model_copy(
                            update={
                                "attributes": {
                                    **mapping.attributes,
                                    "discovered_menu_image_url": str(entry.image_url),
                                }
                            }
                        )
                    enriched_mappings.append(mapping)
                mappings = enriched_mappings
                pipeline = MenuRefreshPipeline(
                    MenuImageDownloader(),
                    RawJsonWriter(arguments.raw_dir),
                    NormalizedMenuWriter(arguments.normalized_dir),
                    MenuValidationWriter(arguments.validation_dir),
                    MenuDecisionWriter(arguments.decision_dir),
                    RapidOcrEngine(),
                    MenuOcrCache(arguments.cache_dir),
                    validator=MenuValidatorOrchestrator(),
                    review_queue=MenuReviewQueue(arguments.review_dir),
                )

                def processor(mapping, effective_run_id):
                    return pipeline.run(mapping, run_id=effective_run_id)

            run_id = _new_google_batch_run_id(mode)
            summary = GoogleMapsBatchRunner(
                processor,
                mode=mode,
                max_requests=arguments.max_requests,
                offset=arguments.offset,
            ).run(mappings, run_id=run_id)
            summary_path = GoogleMapsBatchSummaryWriter(arguments.summary_dir).write(
                summary
            )
        except (
            OSError,
            httpx.HTTPError,
            ValidationError,
            MenuOcrDependencyError,
            ValueError,
            RuntimeError,
        ) as error:
            print(f"Google Maps batch failed: {error}", file=sys.stderr)
            return 2
        print(f"summary={summary_path}")
        print(
            f"selected={summary.selected_count} succeeded={summary.succeeded_count} "
            f"failed={summary.failed_count}"
        )
        for item in summary.items:
            if item.status == "failed":
                print(
                    f"failed entity_id={item.entity_id} error={item.error}",
                    file=sys.stderr,
                )
        return 1 if summary.failed_count else 0

    if arguments.command == "apply-canonical-replacements":
        try:
            batch = CanonicalReplacementProposalBatch.model_validate_json(
                arguments.batch.read_text(encoding="utf-8")
            )
            proposals = [
                result.proposal
                for result in batch.results
                if result.proposal is not None
            ]
            requested_ids = set(arguments.proposal_id)
            unknown_ids = requested_ids - {
                proposal.proposal_id for proposal in proposals
            }
            if unknown_ids:
                raise ValueError(
                    "unknown proposal IDs: " + ", ".join(sorted(unknown_ids))
                )
            selected = [
                proposal
                for proposal in proposals
                if not requested_ids or proposal.proposal_id in requested_ids
            ]
            if not selected:
                raise ValueError("batch contains no selected PASS proposals")
            approval_method = ReplacementApprovalMethod(arguments.method)
            approvals = []
            approved_at = (
                batch.finished_at
                if approval_method is ReplacementApprovalMethod.DETERMINISTIC
                else datetime.now(timezone.utc)
            )
            for proposal in selected:
                detail_path = Path(proposal.detail_path)
                detail = CandidateDetailStage.model_validate_json(
                    detail_path.read_text(encoding="utf-8")
                )
                if (
                    detail.detail_id != proposal.detail_id
                    or detail.detail_hash != proposal.detail_hash
                    or detail.candidate != proposal.candidate
                    or detail.validation != proposal.validation
                    or detail.vacancy.vacancy_id
                    != proposal.target_vacancy.vacancy_id
                ):
                    raise ValueError(
                        f"proposal/detail mismatch: {proposal.proposal_id}"
                    )
                approvals.append(
                    ApprovedReplacement.from_candidate_detail(
                        detail,
                        allocated_place_id=proposal.proposed_place_id,
                        reviewer=arguments.reviewer,
                        approved_at=approved_at,
                        approval_method=approval_method,
                    )
                )
            written = ApprovedReplacementWriter(arguments.output_dir).write_many(
                approvals
            )
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot apply canonical replacements: {error}", file=sys.stderr)
            return 2
        for path in written:
            print(f"replacement={path}")
        print(
            f"applied={len(written)} method={approval_method.value} "
            f"output={arguments.output_dir}"
        )
        return 0

    if arguments.command == "materialize-canonical-dataset":
        try:
            manifest = read_canonical_identity_manifest(arguments.manifest)
            master = load_verified_master(arguments.master_dir)
            approved_records = load_approved_replacements(
                arguments.approved_replacement_dir
            )
            dataset = materialize_canonical_active_dataset(
                master,
                manifest,
                approved_replacements=approved_records,
            )
            review_correction = None
            if arguments.review_correction is not None:
                review_correction = read_canonical_review_correction_overlay(
                    arguments.review_correction
                )
                dataset = apply_review_corrections(dataset, review_correction)
            evidence_audit = DuplicateEvidenceAudit.model_validate_json(
                arguments.duplicate_evidence.read_bytes()
            )
            identity_decisions = load_duplicate_identity_decisions(
                arguments.decisions
            )
            readiness = evaluate_canonical_dataset_readiness(
                dataset,
                evidence_audit,
                CanonicalIdentityResolver(manifest),
                distinct_decisions=identity_decisions.distinct_decisions,
            )
            dataset_path = CanonicalActiveDatasetWriter(
                arguments.output_dir
            ).write(dataset)
            readiness_path = CanonicalDatasetReadinessWriter(
                arguments.readiness_output_dir
            ).write(readiness)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot materialize canonical dataset: {error}", file=sys.stderr)
            return 2
        print(f"dataset={dataset_path}")
        print(f"readiness={readiness_path}")
        if review_correction is not None:
            print(
                f"review_correction={review_correction.overlay_id} "
                f"corrected={review_correction.correction_count} "
                f"skipped={review_correction.skipped_count}"
            )
        print(
            f"records={dataset.report.canonical_record_count} "
            f"master={dataset.report.master_materialized_count} "
            f"replacements={dataset.report.replacement_materialized_count} "
            f"retired={dataset.report.retired_duplicate_count} "
            f"open_vacancies={dataset.report.open_vacancy_count}"
        )
        print(
            f"publish_ready={str(readiness.publish_ready).lower()} "
            f"resolved_merge={len(readiness.resolved_merge)} "
            f"resolved_distinct={len(readiness.resolved_distinct)} "
            f"unresolved={len(readiness.unresolved_groups)}"
        )
        if not arguments.allow_unresolved_review:
            try:
                require_canonical_dataset_publish_ready(readiness)
            except CanonicalDatasetNotReadyError as error:
                print(
                    "Canonical dataset is shadow-only: " + str(error),
                    file=sys.stderr,
                )
                return 1
        return 0

    if arguments.command == "propose-canonical-replacements":
        try:
            if arguments.search_term and len(arguments.entity_type) != 1:
                raise ValueError(
                    "--search-term requires exactly one --entity-type"
                )
            manifest = read_canonical_identity_manifest(arguments.manifest)
            master = load_verified_master(arguments.master_dir)
            approved_records = load_approved_replacements(
                arguments.approved_replacement_dir
            )
            projection = build_existing_identity_projection(
                master,
                manifest,
                current_place_directory=arguments.current_place_dir,
                current_google_mapping_directory=arguments.current_mapping_dir,
                approved_replacements=approved_records,
            )
            selected_types = set(arguments.entity_type)
            selected_cities = set(arguments.city_id)
            selected_vacancy_ids = set(arguments.vacancy_id)
            vacancies = [
                vacancy
                for vacancy in manifest.vacancies
                if (
                    not selected_types
                    or vacancy.entity_type.value in selected_types
                )
                and (
                    not selected_cities or vacancy.city_id in selected_cities
                )
                and (
                    not selected_vacancy_ids
                    or vacancy.vacancy_id in selected_vacancy_ids
                )
            ]
            unknown_vacancies = selected_vacancy_ids - {
                item.vacancy_id for item in manifest.vacancies
            }
            if unknown_vacancies:
                raise ValueError(
                    "unknown vacancy IDs: " + ", ".join(sorted(unknown_vacancies))
                )
            blocked_ids = {
                item.legacy_place_id for item in master.slots
            } | {item.id for item in approved_records}
            retired_ids = {
                item.retired_place_id for item in manifest.retired_place_ids
            }
            browser = PlaywrightBrowserClient(
                headless=not arguments.headed,
                artifact_directory=arguments.artifact_dir,
            )
            entity_search_terms = None
            if arguments.search_term:
                entity_search_terms = {
                    EntityType(arguments.entity_type[0]): arguments.search_term
                }
            discovery = GoogleMapsCandidateDiscovery(
                GoogleMapsCandidateDiscoveryAdapter(
                    browser,
                    entity_search_terms=entity_search_terms,
                ),
                RawJsonWriter(arguments.raw_dir),
                GoogleMapsCandidateStageWriter(arguments.stage_dir),
            )
            detail = GoogleMapsCandidateDetail(
                GoogleMapsPlaceAdapter(browser),
                RawJsonWriter(arguments.raw_dir),
                CandidateDetailStageWriter(arguments.detail_dir),
            )
            run_id = (
                "canonical-replacements-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                f"{uuid4().hex[:8]}"
            )
            summary, summary_path = CanonicalReplacementProposalBatchRunner(
                discovery,
                detail,
                MonotonicPlaceIdAllocator(
                    existing_ids=blocked_ids,
                    retired_ids=retired_ids,
                ),
                CanonicalReplacementProposalBatchWriter(arguments.summary_dir),
                result_limit=arguments.result_limit,
                max_detail_candidates=arguments.max_detail_candidates,
                max_vacancies=arguments.max_vacancies,
            ).run(vacancies, projection.identities, run_id=run_id)
        except (OSError, ValidationError, ValueError, RuntimeError) as error:
            print(f"Cannot propose canonical replacements: {error}", file=sys.stderr)
            return 2
        print(f"summary={summary_path}")
        print(
            f"vacant={summary.vacant_count} searches={summary.unique_search_count} "
            f"proposed={summary.proposed_count} "
            f"unresolved={summary.unresolved_count} failed={summary.failed_count}"
        )
        for result in summary.results:
            if result.proposal is not None:
                print(
                    f"proposal vacancy={result.vacancy.vacancy_id} "
                    f"place_id={result.proposal.proposed_place_id} "
                    f"name={result.proposal.candidate.name}"
                )
            elif result.error:
                print(
                    f"failed vacancy={result.vacancy.vacancy_id} "
                    f"stage={result.error_stage} error={result.error}",
                    file=sys.stderr,
                )
        return 1 if summary.failed_count else 0

    if arguments.command == "audit-canonical-identities":
        try:
            audit = DuplicateEvidenceAuditor().audit_paths(
                candidate_groups_path=arguments.candidate_groups,
                master_source=arguments.master_dir,
                google_observation_source=arguments.observation_root,
            )
            output_path = GoogleMapsRegistryWriter.write(audit, arguments.output)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot audit canonical identities: {error}", file=sys.stderr)
            return 2
        print(f"audit={output_path}")
        print(
            f"groups={audit.group_count} "
            f"confirmed={audit.status_counts.get('confirmed', 0)} "
            f"review={audit.status_counts.get('review', 0)} "
            f"distinct={audit.status_counts.get('distinct', 0)}"
        )
        print(
            "missing_google_observations="
            f"{len(audit.missing_google_observation_place_ids)}"
        )
        return 0

    if arguments.command == "build-canonical-manifest":
        try:
            manifest, report = build_manifest_from_master(
                arguments.master_dir,
                arguments.decisions,
                previous_manifest_path=arguments.previous_manifest,
                include_tagged_duplicates=(
                    not arguments.ignore_master_duplicate_tags
                ),
                approved_replacement_root=arguments.approved_replacement_dir,
            )
            manifest_path = CanonicalIdentityManifestWriter(
                arguments.output
            ).write(manifest)
            report_path = GoogleMapsRegistryWriter.write(report, arguments.report)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot build canonical manifest: {error}", file=sys.stderr)
            return 2
        print(f"manifest={manifest_path}")
        print(f"report={report_path}")
        print(
            f"canonical={len(manifest.identities)} "
            f"retired={len(manifest.retired_place_ids)} "
            f"vacant={sum(item.status.value == 'vacant' for item in manifest.vacancies)}"
        )
        return 0

    if arguments.command == "build-google-maps-registry":
        try:
            overrides = [_load_mapping(path) for path in arguments.override]
            registry, report = GoogleMapsRegistryBuilder().build(
                arguments.master_dir,
                overrides=overrides,
            )
            registry_path = GoogleMapsRegistryWriter.write(registry, arguments.output)
            report_path = GoogleMapsRegistryWriter.write(report, arguments.report)
        except (
            OSError,
            ValidationError,
            MasterDataValidationError,
            ValueError,
        ) as error:
            print(f"Cannot build Google Maps registry: {error}", file=sys.stderr)
            return 2
        print(f"registry={registry_path}")
        print(f"report={report_path}")
        print(
            f"mappings={report.mapping_count} overrides={report.overrides_applied} "
            f"duplicate_candidates={len(report.duplicate_identity_candidates)}"
        )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
