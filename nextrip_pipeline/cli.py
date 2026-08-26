from __future__ import annotations

import argparse
import json
import os
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
from nextrip_pipeline.canonical.completeness import (
    ArtifactInputDigest,
    CanonicalCompletenessWriter,
    CanonicalHotelStayContext,
    CanonicalSourcePolicy,
    CompletenessArtifactKind,
    CompletenessSeverity,
    build_canonical_completeness_audit,
    digest_artifact_file,
    load_artifact_directory,
    read_canonical_completeness_audit,
)
from nextrip_pipeline.canonical.crawl_backlog import (
    CanonicalCrawlBacklog,
    CanonicalCrawlBacklogWriter,
    CanonicalCrawlExecutionClaimWriter,
    CanonicalCrawlJob,
    CanonicalCrawlRequiredField,
    CanonicalCrawlTask,
    build_canonical_crawl_backlog,
    build_canonical_crawl_execution_claim,
    read_canonical_crawl_backlog,
)
from nextrip_pipeline.canonical.detail import (
    CandidateDetailStage,
    CandidateDetailStageWriter,
    GoogleMapsCandidateDetail,
)
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDatasetWriter,
    materialize_canonical_active_dataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.google_maps_refresh import (
    CanonicalGoogleMapsPatchWriter,
    CanonicalMenuCollectionBacklogWriter,
    GOOGLE_MAPS_CANONICAL_ENTITY_TYPES,
    apply_google_maps_canonical_refresh_patch,
    build_canonical_menu_collection_backlog,
    build_google_maps_canonical_refresh_patch,
)
from nextrip_pipeline.canonical.discovery import (
    GoogleMapsCandidateDiscovery,
    GoogleMapsCandidateStageWriter,
)
from nextrip_pipeline.canonical.id_allocator import MonotonicPlaceIdAllocator
from nextrip_pipeline.canonical.invalidation import (
    CanonicalInvalidationReason,
    CanonicalInvalidationWriter,
    approve_canonical_invalidation,
)
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
    apply_review_corrections_to_identity_projection,
    build_existing_identity_projection,
    load_approved_replacements,
)
from nextrip_pipeline.canonical.candidate import ExistingCanonicalIdentity
from nextrip_pipeline.canonical.replacement_batch import (
    CanonicalReplacementProposal,
    CanonicalReplacementProposalBatch,
    CanonicalReplacementProposalBatchRunner,
    CanonicalReplacementProposalBatchWriter,
)
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetNotReadyError,
    CanonicalDatasetReadinessWriter,
    evaluate_canonical_dataset_readiness,
    read_canonical_dataset_readiness,
    require_canonical_dataset_publish_ready,
)
from nextrip_pipeline.canonical.replacement_enrichment import (
    CanonicalReplacementEnrichmentWriter,
    apply_replacement_enrichments,
    build_canonical_replacement_enrichment_overlay,
    read_canonical_replacement_enrichment_overlay,
)
from nextrip_pipeline.canonical.review_correction import (
    apply_review_corrections,
    read_canonical_review_correction_overlay,
)
from nextrip_pipeline.canonical.resolver import CanonicalIdentityResolver
from nextrip_pipeline.crawl import (
    GoogleMapsBatchManifestDocument,
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
    TrivagoSearchReviewConfig,
)
from nextrip_pipeline.crawl.adapters import (
    GoogleMapsPlaceAdapter,
    TrivagoMcpDiscoveryAdapter,
    TrivagoMcpPriceAdapter,
    TrivagoPriceRequest,
)
from nextrip_pipeline.decision_gate import (
    GoogleMapsDecisionGate,
    GoogleMapsDecisionPolicy,
    GoogleMapsDecisionWriter,
    HotelPriceDecisionWriter,
    MenuDecisionWriter,
)
from nextrip_pipeline.jobs import (
    GoogleMapsBatchItemStatus,
    GoogleMapsBatchMode,
    GoogleMapsBatchRunner,
    GoogleMapsBatchSummaryWriter,
    GoogleMapsQualityReprocessor,
    GoogleMapsReprocessSummaryWriter,
    MENU_ENTITY_TYPES,
    GoogleMapsOpeningJob,
    GoogleMapsRefreshPipeline,
    HotelPriceRefreshPipeline,
    TrivagoHotelPriceJob,
    TrivagoBatchSummaryWriter,
    TrivagoMcpBatchRunner,
    TrivagoPriceBatchContext,
    TrivagoStayAvailabilityBatchRunner,
    TrivagoStayAvailabilityResultWriter,
    TrivagoStayAvailabilityRunner,
    TrivagoStayBatchSummaryWriter,
    google_maps_batch_requires_retry,
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
    AcceptedObservationStore,
    CurrentHotelAvailabilitySnapshot,
    CurrentHotelAvailabilityWriter,
    CurrentHotelPriceSnapshot,
    CurrentHotelPriceWriter,
    CurrentMenuWriter,
    GoogleMapsMenuSourceEntry,
    GoogleMapsMenuSourceIndex,
)
from nextrip_pipeline.crawl.adapters.google_maps_discovery import (
    GoogleMapsCandidateDiscoveryAdapter,
)
from nextrip_pipeline.quality import (
    CurrentGoogleMapsMappingWriter,
    CurrentTrivagoMappingWriter,
    GoogleMapsMappingApprovalError,
    GoogleMapsMappingApprovalWriter,
    GoogleMapsMappingResolutionWriter,
    GoogleMapsMappingResolver,
    LLMReviewQueueConfig,
    LLMReviewRequestWriter,
    OpeningStatusApprovalError,
    OpeningStatusReviewApprovalWriter,
    TrivagoDiscoveryAuditWriter,
    TrivagoMappingApprovalWriter,
    approve_google_maps_mapping,
    approve_trivago_review,
    build_opening_status_review_approvals,
)
from nextrip_pipeline.canonical.hotel_identity_patch import (
    CanonicalHotelIdentityPatchWriter,
    apply_canonical_hotel_identity_patch,
    build_canonical_hotel_identity_patch,
)
from nextrip_pipeline.canonical.active_pointer import (
    ACTIVE_DATASET_POINTER_FILENAME,
    CanonicalActivePointerError,
    promote_canonical_active_dataset,
    resolve_active_canonical_dataset,
)
from nextrip_pipeline.review import MenuReviewQueue
from nextrip_pipeline.review.trivago_mapping import (
    TrivagoReviewBatchBuilder,
    TrivagoReviewBatchWriter,
)
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
        help="Build Trivago MCP search targets from the canonical dataset.",
    )
    build_trivago_registry.add_argument(
        "--canonical-dataset",
        type=Path,
        required=True,
        help=(
            "Required content-addressed canonical active dataset. This is the "
            "only source used to select active hotels."
        ),
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
        "--search-review-config",
        type=Path,
        default=Path("config/trivago-search-review.json"),
        help=(
            "Reviewed aliases and expected provider identities. The expected "
            "identity is only confirmed after fresh entity-owned evidence."
        ),
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

    approve_trivago_mapping = subparsers.add_parser(
        "approve-trivago-mapping",
        help="Human-approve one source-pinned Trivago REVIEW resolution.",
    )
    approve_trivago_mapping.add_argument(
        "--resolution",
        type=Path,
        required=True,
        help="Immutable Trivago REVIEW resolution JSON to approve.",
    )
    approve_trivago_mapping.add_argument(
        "--registry",
        type=Path,
        default=Path("config/generated/trivago-hotel-registry.json"),
    )
    approve_trivago_mapping.add_argument("--reviewer", required=True)
    approve_trivago_mapping.add_argument(
        "--allow-external-id-change",
        action="store_true",
        help=(
            "Explicitly authorize replacing an already-confirmed Trivago "
            "external ID after human identity verification."
        ),
    )
    approve_trivago_mapping.add_argument(
        "--approval-dir",
        type=Path,
        default=Path("data/approvals/trivago_mapping"),
    )
    approve_trivago_mapping.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/trivago_mappings"),
    )

    approve_google_maps_mapping_parser = subparsers.add_parser(
        "approve-google-maps-mapping",
        help=(
            "Human-approve one source-pinned Google Maps identity resolution "
            "and decision."
        ),
    )
    approve_google_maps_mapping_parser.add_argument(
        "--canonical-dataset",
        type=Path,
        required=True,
    )
    approve_google_maps_mapping_parser.add_argument(
        "--observation",
        type=Path,
        required=True,
    )
    approve_google_maps_mapping_parser.add_argument(
        "--resolution",
        type=Path,
        required=True,
    )
    approve_google_maps_mapping_parser.add_argument(
        "--decision",
        type=Path,
        required=True,
    )
    approve_google_maps_mapping_parser.add_argument("--reviewer", required=True)
    approve_google_maps_mapping_parser.add_argument(
        "--approval-output-dir",
        type=Path,
        default=Path("data/approvals/google_maps_mapping"),
    )
    approve_google_maps_mapping_parser.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/google_maps_mappings"),
    )

    approve_opening_reviews = subparsers.add_parser(
        "approve-opening-status-reviews",
        help=(
            "Human-approve pending opening observations pinned to one "
            "canonical dataset; use explicit --all-pending with --expected-count "
            "for a bulk approval."
        ),
    )
    approve_opening_reviews.add_argument(
        "--canonical-dataset",
        type=Path,
        required=True,
    )
    approve_opening_reviews.add_argument("--reviewer", required=True)
    approve_opening_reviews.add_argument(
        "--place-id",
        action="append",
        default=[],
        help="Approve one pending place; repeat as needed.",
    )
    approve_opening_reviews.add_argument(
        "--all-pending",
        action="store_true",
        help="Explicitly authorize approving every pending opening review.",
    )
    approve_opening_reviews.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help="Fail unless the selected pending review count exactly matches this value.",
    )
    approve_opening_reviews.add_argument(
        "--reason",
        default="human_approved_pending_opening_review",
    )
    approve_opening_reviews.add_argument(
        "--approval-output-dir",
        type=Path,
        default=Path("data/approvals/opening_status"),
    )

    build_trivago_review = subparsers.add_parser(
        "build-trivago-review-batch",
        help=(
            "Build a source-pinned review queue for unresolved Trivago hotel "
            "identities. This command never approves a mapping."
        ),
    )
    build_trivago_review.add_argument(
        "--batch-summary",
        type=Path,
        required=True,
        help="Immutable batch-trivago-availability summary to review.",
    )
    build_trivago_review.add_argument(
        "--registry",
        type=Path,
        default=Path("config/generated/trivago-hotel-registry.json"),
    )
    build_trivago_review.add_argument(
        "--current-mapping-dir",
        type=Path,
        default=Path("data/current/trivago_mappings"),
    )
    build_trivago_review.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/review/trivago_mapping"),
    )
    build_trivago_review.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("."),
        help="Root used to resolve artifact paths recorded by the batch.",
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
    batch_availability.add_argument(
        "--backlog",
        type=Path,
        default=None,
        help=(
            "Run only automatic, due Trivago tasks from this immutable "
            "canonical crawl backlog."
        ),
    )
    batch_availability.add_argument(
        "--claim-dir",
        type=Path,
        default=Path("data/runs/canonical_crawl_claims"),
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
    batch_availability.add_argument(
        "--identity-retry-limit",
        type=int,
        choices=range(0, TrivagoStayAvailabilityRunner.max_identity_retry_limit + 1),
        default=2,
        metavar="0..4",
        help=(
            "Maximum additional registry-pinned identity searches per stay "
            "window (default: 2)."
        ),
    )
    batch_availability.add_argument(
        "--include-radius-identity-retry",
        action="store_true",
        help=(
            "Reserve the final identity retry for a coordinate-radius search; "
            "radius evidence alone never auto-confirms a hotel."
        ),
    )
    batch_availability.add_argument(
        "--include-identity-discovery",
        action="store_true",
        help=(
            "Explicitly include unresolved/review registry identities in this "
            "batch. Scheduled five-hour price refreshes omit this option and "
            "only call confirmed identities."
        ),
    )
    batch_availability.add_argument("--adults", type=int, default=2)
    batch_availability.add_argument("--rooms", type=int, default=1)
    batch_availability.add_argument("--children", type=int, default=0)
    batch_availability.add_argument("--children-ages", type=int, nargs="*", default=[])
    batch_availability.add_argument("--currency", default="VND")
    batch_availability.add_argument("--max-requests", type=int)
    batch_availability.add_argument("--offset", type=int, default=0)
    batch_availability.add_argument("--entity-id", action="append", default=[])
    batch_availability.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
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
        "--accepted-observation-dir",
        type=Path,
        default=Path("data/observations"),
        help=(
            "Append-only JSON history written after deterministic quality "
            "acceptance and before current/Neo4j publication."
        ),
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
        "--force-search",
        action="store_true",
        help=(
            "Ignore a stored Google Maps place URL and recrawl from the mapping "
            "search query and master-coordinate viewport."
        ),
    )
    refresh_maps.add_argument(
        "--search-query-mode",
        choices=["registry", "name"],
        default="registry",
        help=(
            "Select the registry query or retry with the canonical place name; "
            "the default remains registry."
        ),
    )
    refresh_maps.add_argument(
        "--normalized-dir", type=Path, default=Path("data/normalized")
    )
    refresh_maps.add_argument(
        "--validation-dir", type=Path, default=Path("data/validation")
    )
    refresh_maps.add_argument(
        "--decision-dir", type=Path, default=Path("data/decisions")
    )
    refresh_maps.add_argument(
        "--accepted-observation-dir",
        type=Path,
        default=Path("data/observations"),
    )
    _add_google_quality_arguments(refresh_maps)

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
        "--force-search",
        action="store_true",
        help=(
            "Ignore stored Google Maps place URLs and recrawl from mapping "
            "search queries and master-coordinate viewports."
        ),
    )
    batch_maps.add_argument(
        "--search-query-mode",
        choices=["registry", "name"],
        default="registry",
        help=(
            "Select registry queries or retry with canonical place names; the "
            "default remains registry."
        ),
    )
    batch_maps.add_argument(
        "--canonical-dataset",
        type=Path,
        default=(
            Path(value) if (value := os.getenv("NEXTRIP_CANONICAL_DATASET")) else None
        ),
        help=(
            "Canonical dataset pinned by a v1.1 crawl backlog. Required when "
            "--backlog has automatic work. Defaults to NEXTRIP_CANONICAL_DATASET."
        ),
    )
    batch_maps.add_argument(
        "--backlog",
        type=Path,
        default=None,
        help=(
            "Run only automatic, due tasks for the selected mode from this "
            "immutable canonical crawl backlog."
        ),
    )
    batch_maps.add_argument(
        "--claim-dir",
        type=Path,
        default=Path("data/runs/canonical_crawl_claims"),
    )
    batch_maps.add_argument(
        "--mode", choices=[item.value for item in GoogleMapsBatchMode], required=True
    )
    batch_maps.add_argument("--max-requests", type=int, default=32)
    batch_maps.add_argument(
        "--max-no-update-ratio",
        type=float,
        default=float(os.getenv("NEXTRIP_MAPS_MAX_NO_UPDATE_RATIO", "0.20")),
        help=(
            "Maximum tolerated fraction of isolated NO_UPDATE items for a "
            "trusted scheduled crawl. At least one isolated item is always "
            "tolerated; exceeding the guard makes Airflow retry the task."
        ),
    )
    batch_maps.add_argument(
        "--run-id",
        default=None,
        help=(
            "Optional caller-owned immutable run ID. Airflow uses this to pin "
            "the canonical patch to exactly the crawl attempts in one cycle."
        ),
    )
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
    batch_maps.add_argument(
        "--current-menu-dir",
        type=Path,
        default=Path("data/current/menu"),
    )
    batch_maps.add_argument(
        "--accepted-observation-dir",
        type=Path,
        default=Path("data/observations"),
    )
    _add_google_quality_arguments(batch_maps)

    reprocess_maps = subparsers.add_parser(
        "reprocess-google-maps",
        help="Re-run Google Maps quality gates from normalized JSON without crawling.",
    )
    reprocess_maps.add_argument("--manifest", type=Path, required=True)
    reprocess_maps.add_argument(
        "--observation",
        type=Path,
        action="append",
        default=[],
        help=(
            "Reprocess one exact immutable observation; repeat as needed. "
            "When supplied, the default observation root is not scanned."
        ),
    )
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
    reprocess_maps.add_argument(
        "--entity-id",
        action="append",
        default=[],
        help="Only reprocess selected entity IDs; repeat as needed.",
    )
    _add_google_quality_arguments(reprocess_maps)

    build_maps_registry = subparsers.add_parser(
        "build-google-maps-registry",
        help="Generate active Google Maps mappings from the canonical dataset.",
    )
    build_maps_registry.add_argument(
        "--canonical-dataset",
        type=Path,
        required=True,
        help=(
            "Required immutable canonical active dataset. This is the only "
            "source used to select active non-hotel places."
        ),
    )
    build_maps_registry.add_argument(
        "--base-manifest",
        type=Path,
        default=None,
        help=(
            "Optional derived Maps manifest whose active mappings are deliberately "
            "reused when --canonical-dataset is supplied. Omit this argument to "
            "derive every mapping from the pinned canonical dataset only. A missing "
            "explicit file bootstraps an empty derived registry; an invalid existing "
            "file fails closed."
        ),
    )
    build_maps_registry.add_argument(
        "--output",
        type=Path,
        default=Path("config/generated/canonical-google-maps-mapping-registry.json"),
    )
    build_maps_registry.add_argument(
        "--report",
        type=Path,
        default=Path("config/generated/canonical-google-maps-registry-report.json"),
    )
    build_maps_registry.add_argument(
        "--batch-manifest-output",
        type=Path,
        default=None,
        help=(
            "Optional active-only batch manifest pointing at the generated "
            "canonical registry."
        ),
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
        default=Path("config/generated/canonical-google-maps-registry-report.json"),
    )
    audit_canonical.add_argument(
        "--master-dir",
        type=Path,
        required=True,
        help="Explicit legacy import directory; never used as serving authority.",
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
        "--master-dir",
        type=Path,
        required=True,
        help="Explicit legacy import directory used only for one-time migration.",
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
        "--approved-invalidation-dir",
        type=Path,
        default=Path("data/canonical/invalidations"),
        help=(
            "Immutable human-approved canonical invalidations applied against "
            "the exact previous manifest."
        ),
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

    approve_invalidation = subparsers.add_parser(
        "approve-canonical-invalidation",
        help=(
            "Create one immutable, source-pinned human approval that quarantines "
            "an ineligible active canonical identity on the next manifest build."
        ),
    )
    approve_invalidation.add_argument(
        "--manifest",
        type=Path,
        default=Path("config/generated/canonical-identity-manifest.json"),
    )
    approve_invalidation.add_argument("--place-id", required=True)
    approve_invalidation.add_argument(
        "--observation",
        type=Path,
        required=True,
        help="Normalized Google Maps place observation used as pinned evidence.",
    )
    approve_invalidation.add_argument(
        "--identity-source",
        type=Path,
        required=True,
        help=(
            "Verified master JSON containing the observed place; exact identity "
            "facts are pinned into the approval."
        ),
    )
    approve_invalidation.add_argument(
        "--reason",
        choices=[item.value for item in CanonicalInvalidationReason],
        default=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE.value,
    )
    approve_invalidation.add_argument(
        "--reviewer",
        required=True,
        help="Human reviewer identity recorded in immutable provenance.",
    )
    approve_invalidation.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/canonical/invalidations"),
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
        "--master-dir",
        type=Path,
        required=True,
        help="Explicit legacy import corpus for this historical vacancy workflow.",
    )
    propose_replacements.add_argument(
        "--current-place-dir",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
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
        "--review-correction",
        type=Path,
        default=None,
        help=(
            "Source-backed REVIEW correction overlay used by the duplicate "
            "gate; it never changes identity decisions."
        ),
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
    propose_replacements.add_argument("--max-detail-candidates", type=int, default=20)
    propose_replacements.add_argument("--max-vacancies", type=int, default=100)
    propose_replacements.add_argument(
        "--entity-type",
        action="append",
        choices=[item.value for item in EntityType],
        default=[],
    )
    propose_replacements.add_argument(
        "--candidate-entity-type",
        choices=[EntityType.CAFE.value, EntityType.RESTAURANT.value],
        default=None,
        help=(
            "Use the actual cafe/restaurant type for candidates that fill "
            "nightlife vacancies while preserving only the total place count."
        ),
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
        action="append",
        default=[],
        help=(
            "Override the Maps query term; repeat to union multiple pools. "
            "Requires exactly one --entity-type and --city-id."
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

    build_replacement_enrichment = subparsers.add_parser(
        "build-canonical-replacement-enrichment",
        help=(
            "Recover address, Google category, and one eligible cover from the "
            "immutable raw records pinned by every approved replacement."
        ),
    )
    build_replacement_enrichment.add_argument(
        "--approved-replacement-dir",
        type=Path,
        default=Path("data/canonical/replacements"),
    )
    build_replacement_enrichment.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("data/raw"),
    )
    build_replacement_enrichment.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/canonical/replacement-enrichments"),
    )

    materialize_canonical = subparsers.add_parser(
        "materialize-canonical-dataset",
        help=(
            "Materialize one immutable active record per canonical identity "
            "and fail the Neo4j V8 gate while duplicate evidence is unresolved."
        ),
    )
    materialize_canonical.add_argument(
        "--master-dir",
        type=Path,
        required=True,
        help="Explicit legacy import directory used only for one-time migration.",
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
        "--replacement-enrichment",
        type=Path,
        default=None,
        help=(
            "Optional complete, immutable source-backed field overlay for all "
            "approved replacements."
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

    apply_google_refresh = subparsers.add_parser(
        "apply-google-maps-canonical-refresh",
        help=(
            "Build one source-pinned Google Maps patch and materialize a new "
            "immutable canonical dataset without writing data/current/place."
        ),
    )
    apply_google_refresh.add_argument(
        "--canonical-dataset",
        type=Path,
        required=True,
    )
    apply_google_refresh.add_argument(
        "--observation-root",
        type=Path,
        default=Path("data/normalized"),
    )
    apply_google_refresh.add_argument(
        "--decision-root",
        type=Path,
        default=Path("data/decisions"),
    )

    promote_canonical = subparsers.add_parser(
        "promote-canonical-dataset",
        help=(
            "Validate a canonical dataset/readiness pair, write an immutable "
            "promotion audit, and atomically update the active pointer."
        ),
    )
    promote_canonical.add_argument(
        "--canonical-root",
        type=Path,
        default=Path("data/canonical"),
    )
    promote_canonical.add_argument("--dataset", type=Path, required=True)
    promote_canonical.add_argument("--readiness", type=Path, required=True)

    resolve_canonical = subparsers.add_parser(
        "resolve-active-canonical-dataset",
        help="Resolve and integrity-check the active canonical dataset pointer.",
    )
    resolve_canonical.add_argument(
        "--pointer",
        type=Path,
        default=Path("data/canonical") / ACTIVE_DATASET_POINTER_FILENAME,
    )
    apply_google_refresh.add_argument(
        "--mapping-approval-root",
        type=Path,
        default=None,
        help=(
            "Optional immutable Google Maps mapping approvals that may publish "
            "their exact reviewed REVIEW/QUARANTINE evidence."
        ),
    )
    apply_google_refresh.add_argument(
        "--weekly-schedule-review",
        type=Path,
        default=None,
        help=(
            "Optional reviewed seven-day schedules pinned to exact Google "
            "observations and human mapping approvals."
        ),
    )
    build_maps_registry.add_argument(
        "--resolved-mapping-dir",
        type=Path,
        default=None,
        help=(
            "Optional current confirmed/rejected mapping overlay referenced by "
            "the generated batch manifest. Canonical place data remains the "
            "only content authority; this directory stores provider identity."
        ),
    )
    apply_google_refresh.add_argument(
        "--entity-type",
        action="append",
        choices=[item.value for item in GOOGLE_MAPS_CANONICAL_ENTITY_TYPES],
        default=[],
        help=(
            "Entity type to patch; repeat as needed. Defaults to attraction, "
            "cafe, nightlife, and restaurant."
        ),
    )
    apply_google_refresh.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="Only use evidence from these Google batch run IDs.",
    )
    apply_google_refresh.add_argument(
        "--patch-output-dir",
        type=Path,
        default=Path("data/canonical/google-maps-patches"),
    )
    apply_google_refresh.add_argument(
        "--dataset-output-dir",
        type=Path,
        default=Path("data/canonical/datasets"),
    )
    apply_google_refresh.add_argument(
        "--manifest",
        type=Path,
        default=Path("config/generated/canonical-identity-manifest.json"),
    )
    apply_google_refresh.add_argument(
        "--identity-decisions",
        type=Path,
        default=Path("config/canonical-identity-decisions.json"),
    )
    apply_google_refresh.add_argument(
        "--duplicate-evidence",
        type=Path,
        default=Path("data/reports/canonical/duplicate-evidence.json"),
    )
    apply_google_refresh.add_argument(
        "--readiness-output-dir",
        type=Path,
        default=Path("data/canonical/readiness"),
    )
    apply_google_refresh.add_argument(
        "--menu-review-output-dir",
        type=Path,
        default=Path("data/review/menu/canonical-backlogs"),
    )
    apply_google_refresh.add_argument(
        "--require-complete",
        action="store_true",
        help="Fail instead of materializing when any requested place is deferred.",
    )
    apply_google_refresh.add_argument(
        "--skip-menu-backlog",
        action="store_true",
        help="Do not generate the cafe/restaurant manual menu backlog.",
    )

    apply_hotel_identity = subparsers.add_parser(
        "apply-hotel-identity-patch",
        help=(
            "Apply reviewed Trivago/Google hotel rename or same-ID replacement "
            "corrections to one immutable canonical dataset."
        ),
    )
    apply_hotel_identity.add_argument("--canonical-dataset", type=Path, required=True)
    apply_hotel_identity.add_argument("--corrections", type=Path, required=True)
    apply_hotel_identity.add_argument("--evidence-root", type=Path, default=Path("."))
    apply_hotel_identity.add_argument(
        "--patch-output-dir",
        type=Path,
        default=Path("data/canonical/hotel-identity-patches"),
    )
    apply_hotel_identity.add_argument(
        "--dataset-output-dir",
        type=Path,
        default=Path("data/canonical/datasets"),
    )
    apply_hotel_identity.add_argument(
        "--manifest",
        type=Path,
        default=Path("config/generated/canonical-identity-manifest.json"),
    )
    apply_hotel_identity.add_argument(
        "--identity-decisions",
        type=Path,
        default=Path("config/canonical-identity-decisions.json"),
    )
    apply_hotel_identity.add_argument(
        "--duplicate-evidence",
        type=Path,
        default=Path("data/reports/canonical/duplicate-evidence.json"),
    )
    apply_hotel_identity.add_argument(
        "--readiness-output-dir",
        type=Path,
        default=Path("data/canonical/readiness"),
    )

    audit_completeness = subparsers.add_parser(
        "audit-canonical-completeness",
        help=(
            "Audit static, live-context, and deferred data coverage for one "
            "immutable canonical dataset without crawling."
        ),
    )
    audit_completeness.add_argument("--dataset", type=Path, required=True)
    audit_completeness.add_argument("--readiness", type=Path, required=True)
    audit_completeness.add_argument(
        "--as-of",
        type=_aware_datetime_argument,
        default=None,
        help="Timezone-aware audit instant, for example 2026-08-21T12:00:00Z.",
    )
    audit_completeness.add_argument("--check-in", type=date.fromisoformat)
    audit_completeness.add_argument("--check-out", type=date.fromisoformat)
    audit_completeness.add_argument("--adults", type=int, default=2)
    audit_completeness.add_argument("--rooms", type=int, default=1)
    audit_completeness.add_argument("--children", type=int, default=0)
    audit_completeness.add_argument("--children-ages", type=int, nargs="*", default=[])
    audit_completeness.add_argument("--currency", default="VND")
    audit_completeness.add_argument(
        "--google-manifest",
        type=Path,
        default=Path("config/generated/canonical-google-maps-batch-manifest.json"),
    )
    audit_completeness.add_argument(
        "--google-registry",
        type=Path,
        default=Path("config/generated/canonical-google-maps-mapping-registry.json"),
    )
    audit_completeness.add_argument(
        "--trivago-registry",
        type=Path,
        default=Path("config/generated/trivago-hotel-registry.json"),
    )
    audit_completeness.add_argument(
        "--trivago-mapping-dir",
        type=Path,
        default=Path("data/current/trivago_mappings"),
    )
    audit_completeness.add_argument(
        "--hotel-availability-dir",
        type=Path,
        default=Path("data/current/hotel_availability"),
    )
    audit_completeness.add_argument(
        "--hotel-price-dir",
        type=Path,
        default=Path("data/current/hotel_price"),
    )
    audit_completeness.add_argument(
        "--current-menu-dir",
        type=Path,
        default=Path("data/current/menu"),
    )
    audit_completeness.add_argument(
        "--menu-source-dir",
        type=Path,
        default=Path("data/current/google_maps_menu_sources"),
    )
    audit_completeness.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/canonical/completeness"),
    )

    build_backlog = subparsers.add_parser(
        "build-canonical-crawl-backlog",
        help=(
            "Coalesce a canonical completeness report into deterministic, "
            "provider-specific scheduled crawl tasks."
        ),
    )
    build_backlog.add_argument("--completeness-report", type=Path, required=True)
    build_backlog.add_argument(
        "--sources", type=Path, default=Path("config/sources.json")
    )
    build_backlog.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/canonical/crawl-backlogs"),
    )
    return parser


def _aware_datetime_argument(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "expected an ISO-8601 datetime with timezone"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _completeness_stay_context(
    arguments: argparse.Namespace,
    *,
    as_of: datetime,
) -> CanonicalHotelStayContext:
    if (arguments.check_in is None) != (arguments.check_out is None):
        raise ValueError("--check-in and --check-out must be provided together")
    if arguments.check_in is None:
        local_today = as_of.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date()
        check_in = local_today + timedelta(days=1)
        check_out = check_in + timedelta(days=1)
    else:
        check_in = arguments.check_in
        check_out = arguments.check_out
    return CanonicalHotelStayContext(
        check_in=check_in,
        check_out=check_out,
        occupancy=Occupancy(
            adults=arguments.adults,
            children=arguments.children,
            rooms=arguments.rooms,
        ),
        children_ages=sorted(arguments.children_ages),
        currency=arguments.currency.upper(),
    )


def _canonical_source_policies(registry: SourceRegistry) -> list[CanonicalSourcePolicy]:
    relevant_source_ids = {"google-maps-web", "trivago-mcp"}
    return sorted(
        (
            CanonicalSourcePolicy(
                source_id=source.source_id,
                enabled=source.enabled,
                schedule_interval_minutes=(
                    source.schedule_interval_minutes or source.cache_policy.ttl_minutes
                ),
                parser_version=source.parser_version,
            )
            for source in registry.document.sources
            if source.source_id in relevant_source_ids
        ),
        key=lambda item: item.source_id,
    )


def _dispatchable_backlog_tasks(
    path: Path,
    job: CanonicalCrawlJob,
    *,
    evaluated_at: datetime | None = None,
) -> tuple[CanonicalCrawlBacklog, list[CanonicalCrawlTask]]:
    backlog = read_canonical_crawl_backlog(path)
    now = evaluated_at or datetime.now(timezone.utc)
    return (
        backlog,
        [
            task
            for task in backlog.tasks
            if task.job is job and task.automatic and task.due_at <= now
        ],
    )


def _canonical_google_registry_path(manifest_path: Path) -> Path:
    manifest = GoogleMapsBatchManifestDocument.model_validate_json(
        manifest_path.read_bytes()
    )
    registry_path = Path(manifest.registry_file)
    if not registry_path.is_absolute():
        registry_path = manifest_path.parent / registry_path
    return registry_path.resolve()


def _require_backlog_artifact_digest(
    backlog: CanonicalCrawlBacklog,
    actual_digest: ArtifactInputDigest,
) -> None:
    expected = next(
        (item for item in backlog.input_digests if item.kind is actual_digest.kind),
        None,
    )
    if expected is None:
        raise ValueError(
            f"backlog does not pin {actual_digest.kind.value} input artifacts"
        )
    if (
        expected.file_count != actual_digest.file_count
        or expected.input_hash != actual_digest.input_hash
    ):
        raise ValueError(
            f"{actual_digest.kind.value} artifacts changed after backlog creation; "
            "build a new completeness audit and backlog"
        )


def _require_backlog_parser_version(
    backlog: CanonicalCrawlBacklog,
    source_id: str,
    parser_version: str,
) -> None:
    policy = next(
        (item for item in backlog.source_policies if item.source_id == source_id),
        None,
    )
    if policy is None:
        raise ValueError(f"backlog does not pin source policy {source_id}")
    if policy.parser_version != parser_version:
        raise ValueError(
            f"{source_id} parser version changed from "
            f"{policy.parser_version} to {parser_version}; rebuild the backlog"
        )


def _validate_trivago_backlog_context(
    tasks: Sequence[CanonicalCrawlTask],
    context: TrivagoPriceBatchContext,
    *,
    lookahead_days: int,
) -> None:
    expected = {
        **CanonicalHotelStayContext(
            check_in=context.check_in,
            check_out=context.check_out,
            occupancy=context.occupancy,
            children_ages=sorted(context.children_ages),
            currency=context.currency,
        ).model_dump(mode="json"),
        "lookahead_days": lookahead_days,
    }
    mismatches = [task.place_id for task in tasks if task.context != expected]
    if mismatches:
        preview = ", ".join(mismatches[:5])
        suffix = ", ..." if len(mismatches) > 5 else ""
        raise ValueError(
            "Trivago CLI context does not match the pinned backlog context for: "
            f"{preview}{suffix}"
        )


def _relative_artifact_reference(target: Path, manifest: Path) -> str:
    return os.path.relpath(target.resolve(), manifest.parent.resolve()).replace(
        "\\", "/"
    )


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
    parser.add_argument("--llm-max-requests", type=int, default=25)
    parser.add_argument("--llm-max-tokens", type=int, default=25_000)
    parser.add_argument(
        "--disable-llm-review-queue",
        action="store_true",
        help="Run deterministic quality gates without adding LLM review requests.",
    )
    parser.add_argument(
        "--trusted-scheduled-crawl",
        action="store_true",
        help=(
            "Use the unattended Airflow decision policy: deterministic PASS "
            "is published, optional missing-detail warnings may pass, and "
            "identity/location ambiguity is quarantined instead of reviewed."
        ),
    )


def _canonical_promotion_input(path: Path) -> Path:
    """Accept absolute, repository-relative, or canonical-root-relative input."""

    if path.is_absolute() or not path.is_file():
        return path
    return path.resolve()


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
        children_ages=sorted(arguments.children_ages),
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
        GoogleMapsPlaceAdapter(
            browser,
            force_search=arguments.force_search,
            search_query_mode=arguments.search_query_mode,
        ),
        RawJsonWriter(arguments.raw_dir),
        NormalizedGoogleMapsWriter(arguments.normalized_dir),
        GoogleMapsValidationWriter(arguments.validation_dir),
        GoogleMapsDecisionWriter(arguments.decision_dir),
        validator=GoogleMapsValidatorOrchestrator(),
        decision_gate=GoogleMapsDecisionGate(
            policy=(
                GoogleMapsDecisionPolicy.TRUSTED_SCHEDULED
                if arguments.trusted_scheduled_crawl
                else GoogleMapsDecisionPolicy.STANDARD
            )
        ),
        mapping_resolver=GoogleMapsMappingResolver(),
        resolution_writer=GoogleMapsMappingResolutionWriter(arguments.quality_dir),
        current_mapping_writer=CurrentGoogleMapsMappingWriter(
            arguments.current_mapping_dir
        ),
        accepted_observation_store=AcceptedObservationStore(
            arguments.accepted_observation_dir
        ),
        llm_review_writer=(
            None
            if (
                arguments.disable_llm_review_queue
                or arguments.trusted_scheduled_crawl
            )
            else LLMReviewRequestWriter(
                LLMReviewQueueConfig(
                    root_directory=arguments.mapping_review_dir,
                    max_requests_per_run=arguments.llm_max_requests,
                    max_tokens_per_run=arguments.llm_max_tokens,
                )
            )
        ),
        # Place/status fields are promoted only through an immutable canonical
        # patch. Do not create a competing data/current/place source.
        current_place_writer=None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_standard_streams()
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
            search_review_overrides = []
            if arguments.search_review_config.exists():
                search_review_config = TrivagoSearchReviewConfig.model_validate_json(
                    arguments.search_review_config.read_bytes()
                )
                search_review_overrides = search_review_config.overrides
            builder = TrivagoRegistryBuilder()
            dataset = read_canonical_active_dataset(arguments.canonical_dataset)
            registry, report = builder.build_from_canonical_dataset(
                dataset,
                overrides=list(overrides_by_entity.values()),
                search_review_overrides=search_review_overrides,
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
            f"overrides={report.overrides_applied} "
            f"search_review_overrides={report.search_review_overrides_applied}"
        )
        return 0

    if arguments.command == "approve-trivago-mapping":
        try:
            approval, mapping, approval_path, mapping_path = approve_trivago_review(
                arguments.resolution,
                arguments.registry,
                reviewer=arguments.reviewer,
                approval_writer=TrivagoMappingApprovalWriter(arguments.approval_dir),
                mapping_writer=CurrentTrivagoMappingWriter(
                    arguments.current_mapping_dir
                ),
                allow_external_id_change=arguments.allow_external_id_change,
            )
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot approve Trivago mapping: {error}", file=sys.stderr)
            return 2
        print(f"approval={approval_path}")
        print(f"mapping={mapping_path}")
        print(
            f"entity_id={mapping.entity_id} external_id={mapping.external_id} "
            f"approval_id={approval.approval_id}"
        )
        return 0

    if arguments.command == "approve-google-maps-mapping":
        try:
            approval, mapping, approval_path, mapping_path = (
                approve_google_maps_mapping(
                    arguments.canonical_dataset,
                    arguments.observation,
                    arguments.resolution,
                    arguments.decision,
                    reviewer=arguments.reviewer,
                    approval_writer=GoogleMapsMappingApprovalWriter(
                        arguments.approval_output_dir
                    ),
                    mapping_writer=CurrentGoogleMapsMappingWriter(
                        arguments.current_mapping_dir
                    ),
                )
            )
        except (
            OSError,
            ValidationError,
            GoogleMapsMappingApprovalError,
            ValueError,
        ) as error:
            print(f"Cannot approve Google Maps mapping: {error}", file=sys.stderr)
            return 2
        print(f"approval={approval_path}")
        print(f"mapping={mapping_path}")
        print(
            f"place_id={mapping.entity_id} external_id={mapping.external_id} "
            f"approval_id={approval.approval_id}"
        )
        return 0

    if arguments.command == "approve-opening-status-reviews":
        try:
            if arguments.place_id and arguments.all_pending:
                raise OpeningStatusApprovalError(
                    "use either --place-id or --all-pending, not both"
                )
            if not arguments.place_id and not arguments.all_pending:
                raise OpeningStatusApprovalError(
                    "bulk approval requires explicit --all-pending"
                )
            if arguments.all_pending and arguments.expected_count is None:
                raise OpeningStatusApprovalError(
                    "bulk approval requires --expected-count"
                )
            if arguments.expected_count is not None and arguments.expected_count < 1:
                raise OpeningStatusApprovalError("--expected-count must be positive")
            dataset = read_canonical_active_dataset(arguments.canonical_dataset)
            approvals = build_opening_status_review_approvals(
                dataset,
                reviewer=arguments.reviewer,
                place_ids=arguments.place_id,
                reason=arguments.reason,
            )
            if (
                arguments.expected_count is not None
                and len(approvals) != arguments.expected_count
            ):
                raise OpeningStatusApprovalError(
                    "pending opening review count differs from --expected-count: "
                    f"found {len(approvals)}, expected {arguments.expected_count}"
                )
            paths = OpeningStatusReviewApprovalWriter(
                arguments.approval_output_dir
            ).write_many(approvals)
        except (
            OSError,
            ValidationError,
            OpeningStatusApprovalError,
            ValueError,
        ) as error:
            print(f"Cannot approve opening reviews: {error}", file=sys.stderr)
            return 2
        print(f"approval_root={arguments.approval_output_dir}")
        print(
            f"dataset_id={dataset.dataset_id} approved={len(paths)} "
            f"reviewer={arguments.reviewer.strip()}"
        )
        return 0

    if arguments.command == "build-trivago-review-batch":
        try:
            review_batch = TrivagoReviewBatchBuilder().build(
                arguments.batch_summary,
                arguments.registry,
                current_mapping_directory=arguments.current_mapping_dir,
                workspace_root=arguments.workspace_root,
            )
            review_path = TrivagoReviewBatchWriter(arguments.output_dir).write(
                review_batch
            )
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot build Trivago review batch: {error}", file=sys.stderr)
            return 2
        print(f"review_batch={review_path}")
        print(
            f"queue_id={review_batch.queue_id} "
            f"tasks={review_batch.task_count} "
            f"candidates={review_batch.candidate_count}"
        )
        print(
            "approval_candidates="
            f"{review_batch.recommendation_counts.get('approval_candidate', 0)} "
            "reject_selected="
            f"{review_batch.recommendation_counts.get('reject_selected_candidate', 0)} "
            "needs_research="
            f"{review_batch.recommendation_counts.get('needs_research', 0)}"
        )
        for warning in review_batch.warnings:
            print(f"warning={warning}")
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
                children_ages=sorted(arguments.children_ages),
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
                children_ages=sorted(arguments.children_ages),
                currency=arguments.currency.upper(),
            )
            entity_ids = list(arguments.entity_id)
            backlog_tasks: list[CanonicalCrawlTask] = []
            canonical_backlog: CanonicalCrawlBacklog | None = None
            if arguments.backlog is not None:
                canonical_backlog, backlog_tasks = _dispatchable_backlog_tasks(
                    arguments.backlog,
                    CanonicalCrawlJob.TRIVAGO_AVAILABILITY,
                )
                backlog_ids = {task.place_id for task in backlog_tasks}
                requested_ids = set(entity_ids)
                outside_backlog = requested_ids - backlog_ids
                if outside_backlog:
                    raise ValueError(
                        "requested Trivago IDs are not automatic due backlog tasks: "
                        + ", ".join(sorted(outside_backlog))
                    )
                entity_ids = sorted(requested_ids or backlog_ids)
                identity_discovery_ids = sorted(
                    task.place_id
                    for task in backlog_tasks
                    if task.place_id in set(entity_ids)
                    and CanonicalCrawlRequiredField.HOTEL_MAPPING
                    in task.required_fields
                )
                if identity_discovery_ids and not arguments.include_identity_discovery:
                    raise ValueError(
                        "Trivago backlog contains identity-discovery work; rerun "
                        "with --include-identity-discovery for: "
                        + ", ".join(identity_discovery_ids)
                    )
                _validate_trivago_backlog_context(
                    [
                        task
                        for task in backlog_tasks
                        if task.place_id in set(entity_ids)
                    ],
                    context,
                    lookahead_days=arguments.lookahead_days,
                )
                unknown_ids = set(entity_ids) - {
                    entry.entity_id for entry in registry.entries
                }
                if unknown_ids:
                    raise ValueError(
                        "canonical backlog references unknown Trivago IDs: "
                        + ", ".join(sorted(unknown_ids))
                    )
                if not entity_ids:
                    print(
                        "backlog_dispatch=skipped "
                        "job=trivago_availability automatic_due=0"
                    )
                    return 0
                _require_backlog_artifact_digest(
                    canonical_backlog,
                    digest_artifact_file(
                        arguments.registry,
                        kind=CompletenessArtifactKind.TRIVAGO_REGISTRY,
                    ),
                )
                _, current_mapping_digest = load_artifact_directory(
                    arguments.current_mapping_dir,
                    ExternalEntityMapping,
                    kind=CompletenessArtifactKind.TRIVAGO_MAPPING,
                )
                _require_backlog_artifact_digest(
                    canonical_backlog,
                    current_mapping_digest,
                )
                _, availability_digest = load_artifact_directory(
                    arguments.current_availability_dir,
                    CurrentHotelAvailabilitySnapshot,
                    kind=CompletenessArtifactKind.HOTEL_AVAILABILITY,
                )
                _require_backlog_artifact_digest(
                    canonical_backlog,
                    availability_digest,
                )
                _, price_digest = load_artifact_directory(
                    arguments.current_price_dir,
                    CurrentHotelPriceSnapshot,
                    kind=CompletenessArtifactKind.HOTEL_PRICE,
                )
                _require_backlog_artifact_digest(
                    canonical_backlog,
                    price_digest,
                )
            trivago_adapter = TrivagoMcpDiscoveryAdapter()
            if canonical_backlog is not None:
                _require_backlog_parser_version(
                    canonical_backlog,
                    "trivago-mcp",
                    trivago_adapter.parser_version,
                )
            stay_runner = TrivagoStayAvailabilityRunner(
                trivago_adapter,
                RawJsonWriter(arguments.raw_dir),
                NormalizedHotelPriceWriter(arguments.normalized_dir),
                TrivagoDiscoveryAuditWriter(arguments.quality_dir),
                CurrentTrivagoMappingWriter(arguments.current_mapping_dir),
                CurrentHotelAvailabilityWriter(arguments.current_availability_dir),
                validation_writer=ValidationResultWriter(arguments.validation_dir),
                decision_writer=HotelPriceDecisionWriter(arguments.decision_dir),
                current_price_writer=CurrentHotelPriceWriter(
                    arguments.current_price_dir
                ),
                accepted_observation_store=AcceptedObservationStore(
                    arguments.accepted_observation_dir
                ),
                validator=HotelPriceValidatorOrchestrator(),
                identity_retry_limit=arguments.identity_retry_limit,
                include_radius_identity_retry=(arguments.include_radius_identity_retry),
            )
            effective_run_id = _new_trivago_availability_batch_run_id()
            claim_path = None
            if canonical_backlog is not None:
                selected_ids = set(entity_ids)
                selected_tasks = [
                    task for task in backlog_tasks if task.place_id in selected_ids
                ]
                claim = build_canonical_crawl_execution_claim(
                    canonical_backlog,
                    job=CanonicalCrawlJob.TRIVAGO_AVAILABILITY,
                    task_ids=[task.task_id for task in selected_tasks],
                    selection_context={
                        "entity_ids": sorted(selected_ids),
                        "max_requests": arguments.max_requests,
                        "offset": arguments.offset,
                        "lookahead_days": arguments.lookahead_days,
                        "identity_retry_limit": arguments.identity_retry_limit,
                        "include_radius_identity_retry": (
                            arguments.include_radius_identity_retry
                        ),
                        "include_identity_discovery": (
                            arguments.include_identity_discovery
                        ),
                    },
                    claimed_at=datetime.now(timezone.utc),
                )
                claim_path, created = CanonicalCrawlExecutionClaimWriter(
                    arguments.claim_dir
                ).claim(claim)
                if not created:
                    print(
                        "backlog_dispatch=skipped reason=already_claimed "
                        f"claim={claim_path}"
                    )
                    return 0
                effective_run_id = claim.run_id
            summary, summary_path = TrivagoStayAvailabilityBatchRunner(
                stay_runner,
                TrivagoStayAvailabilityResultWriter(arguments.stay_result_dir),
                TrivagoStayBatchSummaryWriter(arguments.summary_dir),
                max_requests=arguments.max_requests,
                offset=arguments.offset,
                entity_ids=entity_ids,
                include_identity_discovery=arguments.include_identity_discovery,
            ).run(
                registry,
                context,
                run_id=effective_run_id,
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
        if claim_path is not None:
            print(f"claim={claim_path}")
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
        for accepted_path in result.accepted_observation_paths:
            print(f"accepted_observation={accepted_path}")
        if result.llm_review_receipt is not None:
            print(f"llm_review={result.llm_review_receipt.disposition.value}")
        return 0

    if arguments.command == "reprocess-google-maps":
        try:
            mappings = load_google_maps_manifest(arguments.manifest)
            selected_types = set(arguments.entity_type) or {
                EntityType.ATTRACTION.value,
                EntityType.CAFE.value,
                EntityType.NIGHTLIFE.value,
                EntityType.RESTAURANT.value,
            }
            selected_ids = set(arguments.entity_id)
            mappings = [
                mapping
                for mapping in mappings
                if mapping.entity_type is not None
                and mapping.entity_type.value in selected_types
                and (not selected_ids or mapping.entity_id in selected_ids)
            ]
            processor = GoogleMapsQualityReprocessor(
                NormalizedGoogleMapsWriter(arguments.normalized_dir),
                GoogleMapsMappingResolutionWriter(arguments.quality_dir),
                GoogleMapsValidationWriter(arguments.validation_dir),
                GoogleMapsDecisionWriter(arguments.decision_dir),
                CurrentGoogleMapsMappingWriter(arguments.current_mapping_dir),
                None,
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
                observation_paths=arguments.observation,
                observation_root=(
                    None if arguments.observation else arguments.observation_root
                ),
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
            canonical_backlog: CanonicalCrawlBacklog | None = None
            if arguments.backlog is not None:
                backlog_job = (
                    CanonicalCrawlJob.GOOGLE_MAPS_PLACE
                    if mode is GoogleMapsBatchMode.PLACE
                    else CanonicalCrawlJob.GOOGLE_MAPS_MENU
                )
                canonical_backlog, backlog_tasks = _dispatchable_backlog_tasks(
                    arguments.backlog,
                    backlog_job,
                )
                backlog_ids = {task.place_id for task in backlog_tasks}
                outside_backlog = requested_ids - backlog_ids
                if outside_backlog:
                    raise ValueError(
                        "requested Google Maps IDs are not automatic due backlog "
                        "tasks: " + ", ".join(sorted(outside_backlog))
                    )
                requested_ids = requested_ids or backlog_ids
                if not requested_ids:
                    print(
                        f"backlog_dispatch=skipped job={backlog_job.value} "
                        "automatic_due=0"
                    )
                    return 0
                if arguments.canonical_dataset is None:
                    raise ValueError(
                        "--canonical-dataset or NEXTRIP_CANONICAL_DATASET is "
                        "required to dispatch a canonical crawl backlog"
                    )
                dispatch_dataset = read_canonical_active_dataset(
                    arguments.canonical_dataset
                )
                if (
                    dispatch_dataset.dataset_id,
                    dispatch_dataset.dataset_hash,
                ) != (
                    canonical_backlog.dataset_id,
                    canonical_backlog.dataset_hash,
                ):
                    raise ValueError(
                        "canonical dataset does not match the crawl backlog "
                        "dataset ID and hash"
                    )
                registry_path = _canonical_google_registry_path(arguments.manifest)
                _require_backlog_artifact_digest(
                    canonical_backlog,
                    digest_artifact_file(
                        registry_path,
                        kind=CompletenessArtifactKind.GOOGLE_REGISTRY,
                    ),
                )
            unknown_ids = requested_ids - {mapping.entity_id for mapping in mappings}
            if unknown_ids:
                raise ValueError(
                    "unknown Google Maps entity IDs: " + ", ".join(sorted(unknown_ids))
                )
            if canonical_backlog is not None:
                wrong_dataset_ids = sorted(
                    mapping.entity_id
                    for mapping in mappings
                    if mapping.entity_id in requested_ids
                    and mapping.attributes.get("canonical_dataset_id")
                    != canonical_backlog.dataset_id
                )
                if wrong_dataset_ids:
                    raise ValueError(
                        "Google Maps mappings belong to another canonical "
                        "dataset: " + ", ".join(wrong_dataset_ids[:5])
                    )
            selected_types = set(arguments.entity_type)
            mappings = [
                mapping
                for mapping in mappings
                if mapping.entity_id not in excluded_ids
                and (not requested_ids or mapping.entity_id in requested_ids)
                and (
                    not selected_types
                    or (
                        mapping.entity_type is not None
                        and mapping.entity_type.value in selected_types
                    )
                )
            ]
            if canonical_backlog is not None and not mappings:
                print(
                    f"backlog_dispatch=skipped job={backlog_job.value} "
                    "selected_after_filters=0"
                )
                return 0
            if mode is GoogleMapsBatchMode.PLACE:
                browser = PlaywrightBrowserClient(
                    headless=not arguments.headed,
                    artifact_directory=arguments.artifact_dir,
                )
                pipeline = _google_maps_refresh_pipeline(arguments, browser)
                if canonical_backlog is not None:
                    _require_backlog_parser_version(
                        canonical_backlog,
                        "google-maps-web",
                        pipeline.adapter.parser_version,
                    )
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

            run_id = arguments.run_id or _new_google_batch_run_id(mode)
            claim_path = None
            if canonical_backlog is not None:
                selected_mapping_ids = {mapping.entity_id for mapping in mappings}
                selected_tasks = [
                    task
                    for task in backlog_tasks
                    if task.place_id in selected_mapping_ids
                ]
                claim = build_canonical_crawl_execution_claim(
                    canonical_backlog,
                    job=backlog_job,
                    task_ids=[task.task_id for task in selected_tasks],
                    selection_context={
                        "entity_ids": sorted(selected_mapping_ids),
                        "entity_types": sorted(selected_types),
                        "excluded_entity_ids": sorted(excluded_ids),
                        "max_requests": arguments.max_requests,
                        "offset": arguments.offset,
                        "rotation_date": (
                            datetime.now(timezone.utc).date().isoformat()
                            if arguments.offset is None
                            else None
                        ),
                    },
                    claimed_at=datetime.now(timezone.utc),
                )
                claim_path, created = CanonicalCrawlExecutionClaimWriter(
                    arguments.claim_dir
                ).claim(claim)
                if not created:
                    print(
                        "backlog_dispatch=skipped reason=already_claimed "
                        f"claim={claim_path}"
                    )
                    return 0
                run_id = claim.run_id
            summary = GoogleMapsBatchRunner(
                processor,
                mode=mode,
                max_requests=arguments.max_requests,
                offset=arguments.offset,
                item_error_status=(
                    GoogleMapsBatchItemStatus.NO_UPDATE
                    if arguments.trusted_scheduled_crawl
                    else GoogleMapsBatchItemStatus.FAILED
                ),
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
            f"no_update={summary.no_update_count} failed={summary.failed_count}"
        )
        for item in summary.items:
            if item.status is GoogleMapsBatchItemStatus.NO_UPDATE:
                print(
                    f"no_update entity_id={item.entity_id} error={item.error}",
                    file=sys.stderr,
                )
            elif item.status is GoogleMapsBatchItemStatus.FAILED:
                print(
                    f"failed entity_id={item.entity_id} error={item.error}",
                    file=sys.stderr,
                )
        retry_required = google_maps_batch_requires_retry(
            summary,
            max_no_update_ratio=arguments.max_no_update_ratio,
        )
        if retry_required:
            print(
                "batch_retry_required "
                f"no_update={summary.no_update_count} "
                f"failed={summary.failed_count} "
                f"max_no_update_ratio={arguments.max_no_update_ratio}",
                file=sys.stderr,
            )
        # Keep this as the final stdout line. Airflow's BashOperator XCom uses
        # it to source-pin the downstream canonical refresh patch.
        print(f"run_id={summary.run_id}")
        return 1 if retry_required else 0

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
                    or detail.vacancy.vacancy_id != proposal.target_vacancy.vacancy_id
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

    if arguments.command == "build-canonical-replacement-enrichment":
        try:
            approved_records = load_approved_replacements(
                arguments.approved_replacement_dir
            )
            overlay = build_canonical_replacement_enrichment_overlay(
                approved_records,
                arguments.raw_dir,
            )
            overlay_path = CanonicalReplacementEnrichmentWriter(
                arguments.output_dir
            ).write(overlay)
        except (OSError, ValidationError, ValueError) as error:
            print(
                f"Cannot build canonical replacement enrichment: {error}",
                file=sys.stderr,
            )
            return 2
        print(f"replacement_enrichment={overlay_path}")
        print(
            f"records={overlay.enrichment_count} "
            f"address={overlay.address_count} "
            f"category={overlay.category_count} "
            f"cover={overlay.cover_count} "
            f"missing_cover={overlay.enrichment_count - overlay.cover_count}"
        )
        return 0

    if arguments.command == "promote-canonical-dataset":
        try:
            pointer = promote_canonical_active_dataset(
                arguments.canonical_root,
                _canonical_promotion_input(arguments.dataset),
                _canonical_promotion_input(arguments.readiness),
            )
            resolved = resolve_active_canonical_dataset(arguments.canonical_root)
        except (
            CanonicalActivePointerError,
            CanonicalDatasetNotReadyError,
            OSError,
            ValidationError,
            ValueError,
        ) as error:
            print(f"Cannot promote canonical dataset: {error}", file=sys.stderr)
            return 2
        print(f"pointer={resolved.pointer_path}")
        print(f"promotion={resolved.promotion_path}")
        print(f"dataset={resolved.dataset_path}")
        print(f"readiness={resolved.readiness_path}")
        print(
            f"dataset_id={pointer.dataset_id} "
            f"promotion_id={pointer.promotion_id}"
        )
        return 0

    if arguments.command == "resolve-active-canonical-dataset":
        try:
            pointer_path = arguments.pointer
            if pointer_path.name != ACTIVE_DATASET_POINTER_FILENAME:
                raise CanonicalActivePointerError(
                    f"pointer filename must be {ACTIVE_DATASET_POINTER_FILENAME}"
                )
            resolved = resolve_active_canonical_dataset(pointer_path.parent)
        except (
            CanonicalActivePointerError,
            CanonicalDatasetNotReadyError,
            OSError,
            ValidationError,
            ValueError,
        ) as error:
            print(f"Cannot resolve canonical dataset: {error}", file=sys.stderr)
            return 2
        print(f"dataset={resolved.dataset_path}")
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
            identity_resolver = CanonicalIdentityResolver(manifest)
            replacement_enrichment = None
            if arguments.replacement_enrichment is not None:
                replacement_enrichment = read_canonical_replacement_enrichment_overlay(
                    arguments.replacement_enrichment
                )
                dataset = apply_replacement_enrichments(
                    dataset,
                    replacement_enrichment,
                )
            review_correction = None
            if arguments.review_correction is not None:
                review_correction = read_canonical_review_correction_overlay(
                    arguments.review_correction
                )
                dataset = apply_review_corrections(
                    dataset,
                    review_correction,
                    resolver=identity_resolver,
                )
            evidence_audit = DuplicateEvidenceAudit.model_validate_json(
                arguments.duplicate_evidence.read_bytes()
            )
            identity_decisions = load_duplicate_identity_decisions(arguments.decisions)
            readiness = evaluate_canonical_dataset_readiness(
                dataset,
                evidence_audit,
                identity_resolver,
                distinct_decisions=identity_decisions.distinct_decisions,
            )
            dataset_path = CanonicalActiveDatasetWriter(arguments.output_dir).write(
                dataset
            )
            readiness_path = CanonicalDatasetReadinessWriter(
                arguments.readiness_output_dir
            ).write(readiness)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot materialize canonical dataset: {error}", file=sys.stderr)
            return 2
        print(f"dataset={dataset_path}")
        print(f"readiness={readiness_path}")
        if replacement_enrichment is not None:
            print(
                f"replacement_enrichment={replacement_enrichment.overlay_id} "
                f"enriched={replacement_enrichment.enrichment_count} "
                f"cover={replacement_enrichment.cover_count}"
            )
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
            f"quarantined={dataset.report.quarantined_identity_count} "
            f"open_vacancies={dataset.report.open_vacancy_count}"
        )
        print(
            f"publish_ready={str(readiness.publish_ready).lower()} "
            f"resolved_merge={len(readiness.resolved_merge)} "
            f"resolved_distinct={len(readiness.resolved_distinct)} "
            f"resolved_quarantined={len(readiness.resolved_quarantined)} "
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

    if arguments.command == "apply-hotel-identity-patch":
        try:
            dataset = read_canonical_active_dataset(arguments.canonical_dataset)
            correction_document = json.loads(
                arguments.corrections.read_text(encoding="utf-8-sig")
            )
            if not isinstance(correction_document, dict):
                raise ValueError("hotel identity corrections must be an object")
            patch = build_canonical_hotel_identity_patch(
                dataset,
                correction_document,
                evidence_root=arguments.evidence_root,
            )
            candidate = apply_canonical_hotel_identity_patch(dataset, patch)
            manifest = read_canonical_identity_manifest(arguments.manifest)
            resolver = CanonicalIdentityResolver(manifest)
            evidence_audit = DuplicateEvidenceAudit.model_validate_json(
                arguments.duplicate_evidence.read_bytes()
            )
            identity_decisions = load_duplicate_identity_decisions(
                arguments.identity_decisions
            )
            readiness = evaluate_canonical_dataset_readiness(
                candidate,
                evidence_audit,
                resolver,
                distinct_decisions=identity_decisions.distinct_decisions,
            )
            require_canonical_dataset_publish_ready(readiness)
            patch_path = CanonicalHotelIdentityPatchWriter(
                arguments.patch_output_dir
            ).write(patch)
            dataset_path = CanonicalActiveDatasetWriter(
                arguments.dataset_output_dir
            ).write(candidate)
            readiness_path = CanonicalDatasetReadinessWriter(
                arguments.readiness_output_dir
            ).write(readiness)
        except (
            CanonicalDatasetNotReadyError,
            json.JSONDecodeError,
            OSError,
            ValidationError,
            ValueError,
        ) as error:
            print(f"Cannot apply hotel identity patch: {error}", file=sys.stderr)
            return 2
        print(f"patch={patch_path}")
        print(f"dataset={dataset_path}")
        print(f"readiness={readiness_path}")
        print(
            f"corrected={len(patch.records)} "
            f"renamed={sum(item.mode.value == 'rename' for item in patch.records)} "
            f"replaced={sum(item.mode.value == 'replace' for item in patch.records)}"
        )
        return 0

    if arguments.command == "apply-google-maps-canonical-refresh":
        try:
            dataset = read_canonical_active_dataset(arguments.canonical_dataset)
            entity_types = (
                [EntityType(value) for value in arguments.entity_type]
                if arguments.entity_type
                else list(GOOGLE_MAPS_CANONICAL_ENTITY_TYPES)
            )
            patch = build_google_maps_canonical_refresh_patch(
                dataset,
                arguments.observation_root,
                arguments.decision_root,
                entity_types=entity_types,
                source_run_ids=arguments.run_id,
                mapping_approval_root=arguments.mapping_approval_root,
                weekly_schedule_review_path=arguments.weekly_schedule_review,
            )
            patch_path = CanonicalGoogleMapsPatchWriter(
                arguments.patch_output_dir
            ).write(patch)
            if arguments.require_complete and not patch.complete:
                raise ValueError(
                    "Google Maps refresh is incomplete: "
                    f"{len(patch.deferred)} of {patch.target_count} places deferred"
                )
            refreshed_dataset = apply_google_maps_canonical_refresh_patch(
                dataset,
                patch,
            )

            manifest = read_canonical_identity_manifest(arguments.manifest)
            identity_resolver = CanonicalIdentityResolver(manifest)
            evidence_audit = DuplicateEvidenceAudit.model_validate_json(
                arguments.duplicate_evidence.read_bytes()
            )
            identity_decisions = load_duplicate_identity_decisions(
                arguments.identity_decisions
            )
            readiness = evaluate_canonical_dataset_readiness(
                refreshed_dataset,
                evidence_audit,
                identity_resolver,
                distinct_decisions=identity_decisions.distinct_decisions,
            )
            require_canonical_dataset_publish_ready(readiness)
            dataset_path = CanonicalActiveDatasetWriter(
                arguments.dataset_output_dir
            ).write(refreshed_dataset)
            readiness_path = CanonicalDatasetReadinessWriter(
                arguments.readiness_output_dir
            ).write(readiness)

            menu_backlog_path = None
            menu_task_count = 0
            if not arguments.skip_menu_backlog:
                menu_backlog = build_canonical_menu_collection_backlog(
                    refreshed_dataset,
                    generated_at=patch.generated_at,
                )
                menu_task_count = len(menu_backlog.tasks)
                menu_backlog_path = CanonicalMenuCollectionBacklogWriter(
                    arguments.menu_review_output_dir
                ).write(menu_backlog)
        except (
            CanonicalDatasetNotReadyError,
            OSError,
            ValidationError,
            ValueError,
        ) as error:
            print(
                f"Cannot apply Google Maps canonical refresh: {error}",
                file=sys.stderr,
            )
            return 2

        dispositions = {
            value: sum(item.disposition.value == value for item in patch.deferred)
            for value in ("missing", "no_update", "review", "quarantine")
        }
        permanent_closed = sum(
            item.business_status.value == "permanently_closed" for item in patch.records
        )
        weekly_schedule = sum(item.weekly_opening is not None for item in patch.records)
        price_evidence = sum(
            item.price_status.value == "observed" for item in patch.records
        )
        print(f"patch={patch_path}")
        print(f"dataset={dataset_path}")
        print(f"readiness={readiness_path}")
        if menu_backlog_path is not None:
            print(f"menu_backlog={menu_backlog_path}")
        print(
            f"target={patch.target_count} refreshed={len(patch.records)} "
            f"human_approved={patch.human_approved_count} "
            f"deferred={len(patch.deferred)} missing={dispositions['missing']} "
            f"no_update={dispositions['no_update']} "
            f"review={dispositions['review']} "
            f"quarantine={dispositions['quarantine']}"
        )
        print(
            f"weekly_schedule={weekly_schedule} "
            f"google_price_evidence={price_evidence} "
            f"permanently_closed={permanent_closed} "
            f"menu_human_tasks={menu_task_count}"
        )
        return 0

    if arguments.command == "audit-canonical-completeness":
        try:
            as_of = arguments.as_of or datetime.now(timezone.utc)
            stay_context = _completeness_stay_context(arguments, as_of=as_of)
            dataset = read_canonical_active_dataset(arguments.dataset)
            readiness = read_canonical_dataset_readiness(arguments.readiness)
            manifest_registry_path = _canonical_google_registry_path(
                arguments.google_manifest
            )
            if manifest_registry_path != arguments.google_registry.resolve():
                raise ValueError(
                    "--google-manifest does not point at --google-registry"
                )
            google_mappings = load_google_maps_manifest(arguments.google_manifest)
            wrong_dataset_mappings = sorted(
                mapping.entity_id
                for mapping in google_mappings
                if mapping.attributes.get("canonical_dataset_id") != dataset.dataset_id
            )
            if wrong_dataset_mappings:
                preview = ", ".join(wrong_dataset_mappings[:5])
                suffix = ", ..." if len(wrong_dataset_mappings) > 5 else ""
                raise ValueError(
                    "Google Maps registry belongs to another canonical dataset: "
                    f"{preview}{suffix}"
                )
            google_registry_digest = digest_artifact_file(
                manifest_registry_path,
                kind=CompletenessArtifactKind.GOOGLE_REGISTRY,
            )
            trivago_registry = _load_trivago_registry(arguments.trivago_registry)
            trivago_registry_digest = digest_artifact_file(
                arguments.trivago_registry,
                kind=CompletenessArtifactKind.TRIVAGO_REGISTRY,
            )
            trivago_mappings, trivago_mapping_digest = load_artifact_directory(
                arguments.trivago_mapping_dir,
                ExternalEntityMapping,
                kind=CompletenessArtifactKind.TRIVAGO_MAPPING,
            )
            availability, availability_digest = load_artifact_directory(
                arguments.hotel_availability_dir,
                CurrentHotelAvailabilitySnapshot,
                kind=CompletenessArtifactKind.HOTEL_AVAILABILITY,
            )
            prices, price_digest = load_artifact_directory(
                arguments.hotel_price_dir,
                CurrentHotelPriceSnapshot,
                kind=CompletenessArtifactKind.HOTEL_PRICE,
            )
            current_menus, current_menu_digest = load_artifact_directory(
                arguments.current_menu_dir,
                NormalizedMenu,
                kind=CompletenessArtifactKind.CURRENT_MENU,
                excluded_directories=("_audit",),
            )
            menu_sources, menu_source_digest = load_artifact_directory(
                arguments.menu_source_dir,
                GoogleMapsMenuSourceEntry,
                kind=CompletenessArtifactKind.MENU_SOURCE,
            )
            audit = build_canonical_completeness_audit(
                dataset,
                readiness,
                as_of=as_of,
                stay_context=stay_context,
                google_mappings=google_mappings,
                trivago_mappings=trivago_mappings,
                trivago_entries=trivago_registry.entries,
                hotel_availability=availability,
                hotel_prices=prices,
                current_menus=current_menus,
                menu_sources=menu_sources,
                input_digests=[
                    google_registry_digest,
                    trivago_registry_digest,
                    trivago_mapping_digest,
                    availability_digest,
                    price_digest,
                    current_menu_digest,
                    menu_source_digest,
                ],
            )
            audit_path = CanonicalCompletenessWriter(arguments.output_dir).write(audit)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot audit canonical completeness: {error}", file=sys.stderr)
            return 2
        severity_counts = {
            severity.value: sum(gap.severity is severity for gap in audit.gaps)
            for severity in CompletenessSeverity
        }
        print(f"completeness={audit_path}")
        print(
            f"records={len(dataset.records)} gaps={len(audit.gaps)} "
            f"blocking={severity_counts['blocking']} "
            f"backfill={severity_counts['backfill']} "
            f"operational={severity_counts['operational']} "
            f"deferred={severity_counts['deferred']}"
        )
        print(
            f"static_ingest_ready={str(audit.static_ingest_ready).lower()} "
            f"operational_fresh={str(audit.operational_fresh).lower()} "
            f"deferred_complete={str(audit.deferred_complete).lower()} "
            f"identity_publish_ready={str(audit.identity_publish_ready).lower()} "
            f"identity_review_places={len(audit.identity_review_place_ids)} "
            f"open_vacancies={audit.open_vacancy_count}"
        )
        return 0 if audit.static_ingest_ready else 1

    if arguments.command == "build-canonical-crawl-backlog":
        try:
            completeness = read_canonical_completeness_audit(
                arguments.completeness_report
            )
            source_registry = SourceRegistry.load(arguments.sources)
            backlog = build_canonical_crawl_backlog(
                completeness,
                _canonical_source_policies(source_registry),
            )
            backlog_path = CanonicalCrawlBacklogWriter(arguments.output_dir).write(
                backlog
            )
        except (OSError, SourceRegistryError, ValidationError, ValueError) as error:
            print(f"Cannot build canonical crawl backlog: {error}", file=sys.stderr)
            return 2
        print(f"backlog={backlog_path}")
        print(
            f"tasks={backlog.task_count} automatic={backlog.automatic_count} "
            f"blocked={backlog.blocked_count} manual={backlog.manual_count}"
        )
        for job, count in backlog.job_counts.items():
            print(f"job={job} count={count}")
        return 0

    if arguments.command == "propose-canonical-replacements":
        try:
            if arguments.candidate_entity_type is not None and (
                arguments.entity_type != [EntityType.NIGHTLIFE.value]
            ):
                raise ValueError(
                    "--candidate-entity-type requires exactly --entity-type nightlife"
                )
            if arguments.search_term and (
                len(arguments.entity_type) != 1 or len(arguments.city_id) != 1
            ):
                raise ValueError(
                    "repeated --search-term requires exactly one --entity-type "
                    "and --city-id"
                )
            manifest = read_canonical_identity_manifest(arguments.manifest)
            master = load_verified_master(arguments.master_dir)
            approved_records = load_approved_replacements(
                arguments.approved_replacement_dir
            )
            prior_proposals: list[CanonicalReplacementProposal] = []
            for prior_path in arguments.summary_dir.glob("run=*.json"):
                try:
                    prior_batch = CanonicalReplacementProposalBatch.model_validate_json(
                        prior_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValidationError, ValueError):
                    continue
                prior_proposals.extend(
                    result.proposal
                    for result in prior_batch.results
                    if result.proposal is not None
                )
            projection = build_existing_identity_projection(
                master,
                manifest,
                current_place_directory=arguments.current_place_dir,
                current_google_mapping_directory=arguments.current_mapping_dir,
                approved_replacements=approved_records,
            )
            review_correction = None
            if arguments.review_correction is not None:
                review_correction = read_canonical_review_correction_overlay(
                    arguments.review_correction
                )
                projection = apply_review_corrections_to_identity_projection(
                    projection,
                    review_correction,
                )
            prior_ids = {item.proposed_place_id for item in prior_proposals}
            projection.identities.extend(
                ExistingCanonicalIdentity(
                    place_id=item.proposed_place_id,
                    entity_type=item.candidate.entity_type,
                    city_id=item.candidate.city_id,
                    name=item.candidate.name,
                    phone=item.candidate.phone,
                    website_url=item.candidate.website_url,
                    location=item.candidate.location,
                    external_identities=item.candidate.external_identities,
                )
                for item in prior_proposals
                if item.proposed_place_id
                not in {x.place_id for x in projection.identities}
            )
            selected_types = set(arguments.entity_type)
            selected_cities = set(arguments.city_id)
            selected_vacancy_ids = set(arguments.vacancy_id)
            vacancies = [
                vacancy
                for vacancy in manifest.vacancies
                if (not selected_types or vacancy.entity_type.value in selected_types)
                and (not selected_cities or vacancy.city_id in selected_cities)
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
            blocked_ids = (
                {item.legacy_place_id for item in master.slots}
                | {item.id for item in approved_records}
                | prior_ids
            )
            retired_ids = {item.retired_place_id for item in manifest.retired_place_ids}
            quarantined_ids = {
                legacy_id
                for item in manifest.quarantined_identities
                for legacy_id in item.identity.legacy_place_ids
            }
            browser = PlaywrightBrowserClient(
                headless=not arguments.headed,
                artifact_directory=arguments.artifact_dir,
            )
            entity_search_terms = None
            if arguments.search_term:
                entity_search_terms = {
                    EntityType(
                        arguments.candidate_entity_type or arguments.entity_type[0]
                    ): arguments.search_term[0]
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
                    quarantined_ids=quarantined_ids,
                ),
                CanonicalReplacementProposalBatchWriter(arguments.summary_dir),
                result_limit=arguments.result_limit,
                max_detail_candidates=arguments.max_detail_candidates,
                max_vacancies=arguments.max_vacancies,
                candidate_entity_type=(
                    EntityType(arguments.candidate_entity_type)
                    if arguments.candidate_entity_type is not None
                    else None
                ),
                search_terms=(
                    {
                        (
                            arguments.city_id[0],
                            EntityType(arguments.entity_type[0]),
                        ): tuple(arguments.search_term)
                    }
                    if arguments.search_term
                    else None
                ),
            ).run(
                vacancies,
                projection.identities,
                run_id=run_id,
                identity_projection_hash=projection.projection_hash,
                review_correction_overlay_id=(
                    review_correction.overlay_id
                    if review_correction is not None
                    else None
                ),
                review_correction_overlay_hash=(
                    review_correction.overlay_hash
                    if review_correction is not None
                    else None
                ),
            )
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
                    f"entity_type={result.proposal.candidate.entity_type.value} "
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

    if arguments.command == "approve-canonical-invalidation":
        try:
            manifest = read_canonical_identity_manifest(arguments.manifest)
            approval = approve_canonical_invalidation(
                manifest,
                place_id=arguments.place_id,
                observation_path=arguments.observation,
                reason=CanonicalInvalidationReason(arguments.reason),
                reviewer=arguments.reviewer,
                identity_source_path=arguments.identity_source,
            )
            approval_path = CanonicalInvalidationWriter(arguments.output_dir).write(
                approval
            )
        except (OSError, ValidationError, ValueError) as error:
            print(
                f"Cannot approve canonical invalidation: {error}",
                file=sys.stderr,
            )
            return 2
        print(f"invalidation={approval_path}")
        print(
            f"approval_id={approval.approval_id} "
            f"approval_hash={approval.approval_hash} "
            f"place_id={approval.canonical_place_id} "
            f"reason={approval.reason.value}"
        )
        return 0

    if arguments.command == "build-canonical-manifest":
        try:
            manifest, report = build_manifest_from_master(
                arguments.master_dir,
                arguments.decisions,
                previous_manifest_path=arguments.previous_manifest,
                include_tagged_duplicates=(not arguments.ignore_master_duplicate_tags),
                approved_replacement_root=arguments.approved_replacement_dir,
                approved_invalidation_root=arguments.approved_invalidation_dir,
            )
            manifest_path = CanonicalIdentityManifestWriter(arguments.output).write(
                manifest
            )
            report_path = GoogleMapsRegistryWriter.write(report, arguments.report)
        except (OSError, ValidationError, ValueError) as error:
            print(f"Cannot build canonical manifest: {error}", file=sys.stderr)
            return 2
        print(f"manifest={manifest_path}")
        print(f"report={report_path}")
        print(
            f"canonical={len(manifest.identities)} "
            f"retired={len(manifest.retired_place_ids)} "
            f"vacant={sum(item.status.value == 'vacant' for item in manifest.vacancies)} "
            f"quarantined={len(manifest.quarantined_identities)}"
        )
        return 0

    if arguments.command == "build-google-maps-registry":
        try:
            overrides = [_load_mapping(path) for path in arguments.override]
            dataset = read_canonical_active_dataset(arguments.canonical_dataset)
            base_mappings = (
                load_google_maps_manifest(arguments.base_manifest)
                if arguments.base_manifest is not None
                and arguments.base_manifest.is_file()
                else []
            )
            registry, report = GoogleMapsRegistryBuilder().build_from_canonical_dataset(
                dataset,
                base_mappings=base_mappings,
                overrides=overrides,
            )
            registry_path = GoogleMapsRegistryWriter.write(registry, arguments.output)
            report_path = GoogleMapsRegistryWriter.write(report, arguments.report)
            batch_manifest_path = None
            if arguments.batch_manifest_output is not None:
                manifest = GoogleMapsBatchManifestDocument(
                    registry_file=_relative_artifact_reference(
                        registry_path,
                        arguments.batch_manifest_output,
                    ),
                    resolved_mapping_dir=(
                        _relative_artifact_reference(
                            arguments.resolved_mapping_dir,
                            arguments.batch_manifest_output,
                        )
                        if arguments.resolved_mapping_dir is not None
                        else None
                    ),
                )
                batch_manifest_path = GoogleMapsRegistryWriter.write(
                    manifest,
                    arguments.batch_manifest_output,
                )
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
        if batch_manifest_path is not None:
            print(f"batch_manifest={batch_manifest_path}")
        print(
            f"mappings={report.mapping_count} overrides={report.overrides_applied} "
            f"reused={report.reused_mapping_count} "
            f"duplicate_candidates={len(report.duplicate_identity_candidates)}"
        )
        return 0

    parser.print_help()
    return 0


def _configure_utf8_standard_streams() -> None:
    """Keep Vietnamese CLI output lossless on legacy Windows code pages."""

    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if isinstance(encoding, str) and encoding.replace("-", "").casefold() == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            # In-memory/captured streams may be immutable; their owner is
            # responsible for selecting an encoding.
            continue


if __name__ == "__main__":
    raise SystemExit(main())
