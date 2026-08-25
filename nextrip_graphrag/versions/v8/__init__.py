"""GraphRAG V8 retrieval and canonical dataset publication.

Retrieval dependencies are loaded lazily so the scheduled observation publisher
can use the Neo4j driver without installing the full GraphRAG/embedding stack in
the Airflow image.
"""

from typing import Any

from .canonical_importer import (
    CanonicalV8ImportPlan,
    CanonicalV8ImportResult,
    CanonicalV8ReleaseManifest,
    CanonicalV8ReleaseManifestWriter,
    apply_canonical_v8_import,
    dry_run_canonical_v8_import,
    ensure_canonical_v8_schema,
    prepare_canonical_v8_import,
)
__all__ = [
    "CanonicalV8ImportPlan",
    "CanonicalV8ImportResult",
    "CanonicalV8ReleaseManifest",
    "CanonicalV8ReleaseManifestWriter",
    "V8RetrievalService",
    "apply_canonical_v8_import",
    "dry_run_canonical_v8_import",
    "ensure_canonical_v8_schema",
    "prepare_canonical_v8_import",
]


def __getattr__(name: str) -> Any:
    if name == "V8RetrievalService":
        from .retrieval import V8RetrievalService

        return V8RetrievalService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
