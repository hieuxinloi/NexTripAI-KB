from __future__ import annotations

from importlib import import_module
from typing import Any

from .models import KBVersionManifest


def kb_version_manifests() -> dict[str, KBVersionManifest]:
    return {
        "v1": KBVersionManifest(
            kb_version="v1",
            status="baseline",
            dataset="travel_data_verified:692",
            ontology_version="place-term-v1",
            embedding_version="place-search-text-v1",
            retrieval_version="vector-first-keyword-fallback-v1",
            description="Current Place-centric graph with generic Term facets.",
        ),
        "v2": KBVersionManifest(
            kb_version="v2",
            status="experimental",
            dataset="travel_data_verified:692",
            ontology_version="typed-travel-provenance-v2",
            embedding_version="place-profile-evidence-chunk-v2",
            retrieval_version="hybrid-anchor-graph-traversal-v2",
            description="Typed travel ontology, provenance chunks and graph-driven retrieval.",
        ),
        "v3": KBVersionManifest(
            kb_version="v3",
            status="experimental",
            dataset="travel_data_verified:692",
            ontology_version="resolved-claims-v3",
            embedding_version="multi-vector-profile-v3",
            retrieval_version="query-adaptive-graph-first-v3",
            description="Expanded typed facets, geo-near edges and graph-first constraint retrieval.",
        ),
        "v4": KBVersionManifest(
            kb_version="v4",
            status="experimental",
            dataset="travel_data_verified:692",
            ontology_version="domain-subgraphs-evidence-claims-v4",
            embedding_version="place-profile-v4",
            retrieval_version="ontology-guided-query-adaptive-v4",
            description="Seven connected domain subgraphs with evidence-aware claims and adaptive retrieval.",
        ),
        "v5": KBVersionManifest(
            kb_version="v5",
            status="experimental",
            dataset="travel_data_verified:692",
            ontology_version="typed-target-geo-provenance-v5",
            embedding_version="place-concept-evidence-v5",
            retrieval_version="deterministic-resilient-typed-router-v5.1",
            description="V5.1 adds fail-fast deterministic planning, grounded category filters and direct NEAR-distance retrieval.",
        ),
        "v6": KBVersionManifest(
            kb_version="v6",
            status="experimental",
            dataset="travel_data_verified:692",
            ontology_version="typed-target-geo-provenance-v5",
            embedding_version="place-concept-evidence-v5",
            retrieval_version="stateful-grounded-itinerary-router-v6",
            description="V6 reuses the verified V5 graph and adds explicit conversation state, grounded scheduling and auditable preference relaxation.",
        ),
        "v7": KBVersionManifest(
            kb_version="v7",
            status="experimental",
            dataset="travel_data_verified:692",
            ontology_version="typed-target-geo-provenance-v5",
            embedding_version="place-concept-evidence-v5",
            retrieval_version="llm-semantic-grounded-hybrid-rrf-v7.1",
            description="V7.1 adds rank-based full-text/vector fusion to semantic planning and closed-candidate graph entity linking.",
        ),
        "v8": KBVersionManifest(
            kb_version="v8",
            status="experimental",
            dataset="travel_data_verified:692",
            ontology_version="typed-target-geo-provenance-v5-with-evidence-chunks",
            embedding_version="place-concept-evidence-v5",
            retrieval_version="semantic-tolerant-stateful-hybrid-evidence-v8",
            description="V8 combines tolerant semantic planning, closed-world entity grounding, V6 conversation/itinerary state, and multi-chunk claim evidence.",
        ),
    }


def version_graph_store_class(version: str) -> type[Any]:
    normalized = _validated_typed_version(version)
    module = import_module(f"{__package__}.{normalized}.graph_store")
    return getattr(module, f"{normalized.upper()}GraphStore")


def version_retrieval_service_class(version: str) -> type[Any]:
    normalized = _validated_typed_version(version)
    module = import_module(f"{__package__}.{normalized}.retrieval")
    return getattr(module, f"{normalized.upper()}RetrievalService")


def _validated_typed_version(version: str) -> str:
    normalized = version.strip().lower()
    if normalized == "v1" or normalized not in kb_version_manifests():
        raise ValueError(f"Unsupported typed Knowledge Base version: {normalized}")
    return normalized
