from __future__ import annotations

from .models import KBVersionManifest


def kb_version_manifests() -> dict[str, KBVersionManifest]:
    return {
        "v1": KBVersionManifest(
            kb_version="v1",
            status="baseline",
            dataset="travel_data_verified:519",
            ontology_version="place-term-v1",
            embedding_version="place-search-text-v1",
            retrieval_version="vector-first-keyword-fallback-v1",
            description="Current Place-centric graph with generic Term facets.",
        ),
        "v2": KBVersionManifest(
            kb_version="v2",
            status="experimental",
            dataset="travel_data_verified:519",
            ontology_version="typed-travel-provenance-v2",
            embedding_version="place-profile-evidence-chunk-v2",
            retrieval_version="hybrid-anchor-graph-traversal-v2",
            description="Typed travel ontology, provenance chunks and graph-driven retrieval.",
        ),
        "v3": KBVersionManifest(
            kb_version="v3",
            status="experimental",
            dataset="travel_data_verified:519",
            ontology_version="resolved-claims-v3",
            embedding_version="multi-vector-profile-v3",
            retrieval_version="query-adaptive-graph-first-v3",
            description="Expanded typed facets, geo-near edges and graph-first constraint retrieval.",
        ),
        "v4": KBVersionManifest(
            kb_version="v4",
            status="planned",
            dataset="travel_data_verified:519",
            ontology_version="geo-temporal-v4",
            embedding_version="geo-context-v4",
            retrieval_version="geo-itinerary-v4",
            description="Generated geo edges and itinerary candidate retrieval.",
        ),
        "v5": KBVersionManifest(
            kb_version="v5",
            status="planned",
            dataset="travel_data_verified:519",
            ontology_version="community-summary-v5",
            embedding_version="local-community-summary-v5",
            retrieval_version="adaptive-local-global-v5",
            description="Community summaries with adaptive local/global GraphRAG.",
        ),
    }
