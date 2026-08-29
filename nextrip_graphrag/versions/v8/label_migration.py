from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .canonical_importer import ensure_canonical_v8_schema
from .observation_publisher import ensure_v8_observation_schema


KB_VERSION = "v8"
LEGACY_LABEL_PREFIX = "V8"
LEGACY_SCHEMA_PREFIX = "v8_"


class LabelMigrationError(RuntimeError):
    """Raised when the live graph cannot be relabeled without data loss."""


class LabelMigrationStore(Protocol):
    def run(self, query: str, **params: Any) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class LabelMapping:
    legacy: str
    replacements: tuple[str, ...]


@dataclass(frozen=True)
class SchemaSpec:
    name: str
    kind: str
    label: str
    properties: tuple[str, ...]


LABEL_MAPPINGS = (
    LabelMapping("V8Entity", ("Entity",)),
    LabelMapping("V8CanonicalPlace", ("CanonicalPlace",)),
    LabelMapping("V8Place", ("Place",)),
    LabelMapping("V8City", ("CanonicalCity",)),
    LabelMapping("V8PlaceVersion", ("PlaceVersion",)),
    LabelMapping(
        "V8SourceProvenance",
        ("CanonicalSourceProvenance", "SourceProvenance"),
    ),
    LabelMapping("V8DatasetRelease", ("DatasetRelease",)),
    LabelMapping("V8TravelCatalog", ("CanonicalTravelCatalog",)),
    LabelMapping("V8PlaceType", ("CanonicalPlaceType",)),
    LabelMapping("V8Category", ("CanonicalCategory",)),
    LabelMapping("V8CanonicalConcept", ("CanonicalConcept",)),
    LabelMapping("V8Concept", ("Concept",)),
    LabelMapping("V8Document", ("CanonicalDocument",)),
    LabelMapping("V8TextUnit", ("CanonicalTextUnit",)),
    LabelMapping("V8Fact", ("CanonicalFact",)),
    LabelMapping("V8Claim", ("CanonicalClaim",)),
    LabelMapping("V8ArchivedCatalog", ("ArchivedCatalog",)),
    LabelMapping("V8ArchivedCity", ("ArchivedCity",)),
    LabelMapping("V8ArchivedPlaceType", ("ArchivedPlaceType",)),
    LabelMapping("V8ArchivedCategory", ("ArchivedCategory",)),
    LabelMapping("V8ArchivedConcept", ("ArchivedConcept",)),
    LabelMapping("V8ArchivedDocument", ("ArchivedDocument",)),
    LabelMapping("V8ArchivedTextUnit", ("ArchivedTextUnit",)),
    LabelMapping("V8ArchivedFact", ("ArchivedFact",)),
    LabelMapping("V8ArchivedClaim", ("ArchivedClaim",)),
    LabelMapping("V8ArchivedPlace", ("ArchivedPlace",)),
    LabelMapping("V8Observation", ("Observation",)),
    LabelMapping("V8HotelPriceObservation", ("HotelPriceObservation",)),
    LabelMapping(
        "V8HotelAvailabilityObservation",
        ("HotelAvailabilityObservation",),
    ),
    LabelMapping("V8OpeningStatusObservation", ("OpeningStatusObservation",)),
    LabelMapping("V8MenuSnapshot", ("MenuSnapshot",)),
    LabelMapping("V8MenuItem", ("MenuItem",)),
)


LEGACY_CONSTRAINTS = (
    "v8_canonical_place_id",
    "v8_canonical_city_id",
    "v8_canonical_place_version_id",
    "v8_source_provenance_id",
    "v8_dataset_release_id",
    "v8_travel_catalog_id",
    "v8_place_type_id",
    "v8_category_id",
    "v8_concept_id",
    "v8_document_id",
    "v8_text_unit_id",
    "v8_fact_id",
    "v8_claim_id",
    "v8_observation_id",
    "v8_menu_item_id",
)


LEGACY_INDEXES = (
    "v8_place_city",
    "v8_place_entity_type",
    "v8_place_version_hash",
    "v8_fact_predicate",
    "v8_place_fulltext",
    "v8_concept_fulltext",
    "v8_place_embedding",
    "v8_concept_embedding",
    "v8_observation_place",
    "v8_observation_observed_at",
    "v8_entity_id",
)


NEUTRAL_CONSTRAINTS = (
    SchemaSpec("canonical_place_id", "UNIQUENESS", "CanonicalPlace", ("id",)),
    SchemaSpec("canonical_city_id", "UNIQUENESS", "CanonicalCity", ("id",)),
    SchemaSpec("place_version_id", "UNIQUENESS", "PlaceVersion", ("id",)),
    SchemaSpec(
        "canonical_source_provenance_id",
        "UNIQUENESS",
        "CanonicalSourceProvenance",
        ("id",),
    ),
    SchemaSpec("dataset_release_id", "UNIQUENESS", "DatasetRelease", ("id",)),
    SchemaSpec(
        "canonical_travel_catalog_id",
        "UNIQUENESS",
        "CanonicalTravelCatalog",
        ("id",),
    ),
    SchemaSpec(
        "canonical_place_type_id",
        "UNIQUENESS",
        "CanonicalPlaceType",
        ("id",),
    ),
    SchemaSpec(
        "canonical_category_id",
        "UNIQUENESS",
        "CanonicalCategory",
        ("id",),
    ),
    SchemaSpec(
        "canonical_concept_id", "UNIQUENESS", "CanonicalConcept", ("id",)
    ),
    SchemaSpec(
        "canonical_document_id", "UNIQUENESS", "CanonicalDocument", ("id",)
    ),
    SchemaSpec(
        "canonical_text_unit_id", "UNIQUENESS", "CanonicalTextUnit", ("id",)
    ),
    SchemaSpec("canonical_fact_id", "UNIQUENESS", "CanonicalFact", ("id",)),
    SchemaSpec("canonical_claim_id", "UNIQUENESS", "CanonicalClaim", ("id",)),
    SchemaSpec("observation_id", "UNIQUENESS", "Observation", ("id",)),
    SchemaSpec("menu_item_id", "UNIQUENESS", "MenuItem", ("id",)),
)


NEUTRAL_INDEXES = (
    SchemaSpec("entity_id", "RANGE", "Entity", ("id",)),
    SchemaSpec("place_city", "RANGE", "Place", ("city",)),
    SchemaSpec("place_entity_type", "RANGE", "Place", ("entity_type",)),
    SchemaSpec("place_version_hash", "RANGE", "PlaceVersion", ("record_hash",)),
    SchemaSpec(
        "canonical_fact_predicate", "RANGE", "CanonicalFact", ("predicate",)
    ),
    SchemaSpec(
        "place_fulltext",
        "FULLTEXT",
        "Place",
        ("name", "aliases", "entity_profile"),
    ),
    SchemaSpec(
        "concept_fulltext",
        "FULLTEXT",
        "Concept",
        ("name", "canonical_name"),
    ),
    SchemaSpec("place_embedding", "VECTOR", "Place", ("embedding",)),
    SchemaSpec("concept_embedding", "VECTOR", "Concept", ("embedding",)),
    SchemaSpec("observation_place", "RANGE", "Observation", ("place_id",)),
    SchemaSpec(
        "observation_observed_at", "RANGE", "Observation", ("observed_at",)
    ),
)


def inspect_generic_label_migration(store: LabelMigrationStore) -> dict[str, Any]:
    """Return a read-only snapshot of legacy labels and schema objects."""

    totals = _graph_totals(store)
    ownership = _single_row(
        store.run(
            """
            MATCH (node)
            RETURN count(node) AS total_nodes,
                   count(CASE
                     WHEN node.kb_version = $kb_version THEN 1
                   END) AS owned_nodes
            """,
            kb_version=KB_VERSION,
        ),
        "inspect database ownership",
    )
    label_counts = {
        str(row["label"]): int(row["count"])
        for row in store.run(
            """
            MATCH (node)
            UNWIND labels(node) AS label
            RETURN label, count(*) AS count
            ORDER BY label
            """
        )
    }
    constraint_rows = _constraint_rows(store)
    index_rows = _index_rows(store)
    constraint_names = {str(row["name"]) for row in constraint_rows}
    index_names = {str(row["name"]) for row in index_rows}
    known_labels = {mapping.legacy for mapping in LABEL_MAPPINGS}
    prefixed_labels = {
        label: count
        for label, count in label_counts.items()
        if label.startswith(LEGACY_LABEL_PREFIX)
    }
    prefixed_constraints = {
        name for name in constraint_names if name.lower().startswith(LEGACY_SCHEMA_PREFIX)
    }
    prefixed_indexes = {
        name for name in index_names if name.lower().startswith(LEGACY_SCHEMA_PREFIX)
    }
    prefixed_target_constraints = {
        str(row["name"])
        for row in constraint_rows
        if _targets_prefixed_label(row)
    }
    prefixed_target_indexes = {
        str(row["name"])
        for row in index_rows
        if _targets_prefixed_label(row)
    }
    return {
        "nodes": totals["nodes"],
        "relationships": totals["relationships"],
        "owned_v8_nodes": int(ownership.get("owned_nodes", 0)),
        "legacy_label_counts": {
            mapping.legacy: label_counts.get(mapping.legacy, 0)
            for mapping in LABEL_MAPPINGS
            if label_counts.get(mapping.legacy, 0)
        },
        "unknown_legacy_label_counts": {
            label: count
            for label, count in sorted(prefixed_labels.items())
            if label not in known_labels
        },
        "replacement_label_counts": {
            label: label_counts.get(label, 0)
            for label in sorted(
                {
                    label
                    for mapping in LABEL_MAPPINGS
                    for label in mapping.replacements
                }
            )
            if label_counts.get(label, 0)
        },
        "legacy_constraints": sorted(constraint_names.intersection(LEGACY_CONSTRAINTS)),
        "legacy_indexes": sorted(index_names.intersection(LEGACY_INDEXES)),
        "unknown_legacy_constraints": sorted(
            prefixed_constraints.union(prefixed_target_constraints).difference(
                LEGACY_CONSTRAINTS
            )
        ),
        "unknown_legacy_indexes": sorted(
            prefixed_indexes.union(prefixed_target_indexes)
            .difference(LEGACY_INDEXES)
            .difference(LEGACY_CONSTRAINTS)
        ),
    }


def preflight_generic_label_migration(
    store: LabelMigrationStore,
) -> dict[str, Any]:
    """Run every read-only cutover gate and return its immutable fingerprint."""

    report = inspect_generic_label_migration(store)
    _require_known_legacy_surface(report)
    _require_isolated_database(report)
    _require_owned_legacy_nodes(store)
    canonical = _canonical_snapshot(store)
    dimension = int(canonical["embedding_dimension"])
    vector_sources = _validate_vector_sources(
        store,
        embedding_dimension=dimension,
    )
    _require_no_neutral_schema_collisions(
        store,
        embedding_dimension=dimension,
    )
    return {
        "status": "ready",
        "inventory": report,
        "canonical": canonical,
        "vector_sources": vector_sources,
    }


def apply_generic_label_migration(
    store: LabelMigrationStore,
    *,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Relabel one canonical V8 database without changing nodes or edges.

    The operation is deliberately resumable. Replacement labels and neutral
    schema are installed before any legacy label or schema object is removed.
    Every mutation is bounded to keep Aura transaction memory predictable.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    preflight = preflight_generic_label_migration(store)
    before = preflight["inventory"]
    canonical_before = preflight["canonical"]
    embedding_dimension = int(canonical_before["embedding_dimension"])

    added: dict[str, int] = {}
    for mapping in LABEL_MAPPINGS:
        if mapping.legacy not in before["legacy_label_counts"]:
            continue
        count = _add_replacement_labels(store, mapping, batch_size=batch_size)
        if count:
            added[mapping.legacy] = count

    ensure_canonical_v8_schema(
        store,
        embedding_dimension=embedding_dimension,
    )
    ensure_v8_observation_schema(store)
    store.run("CALL db.awaitIndexes(120)")
    _require_neutral_schema(
        store,
        embedding_dimension=embedding_dimension,
    )
    _require_complete_replacement_coverage(
        store,
        legacy_labels=set(before["legacy_label_counts"]),
    )

    removed: dict[str, int] = {}
    for mapping in LABEL_MAPPINGS:
        if mapping.legacy not in before["legacy_label_counts"]:
            continue
        count = _remove_legacy_label(store, mapping.legacy, batch_size=batch_size)
        if count:
            removed[mapping.legacy] = count

    for name in LEGACY_CONSTRAINTS:
        store.run(f"DROP CONSTRAINT `{name}` IF EXISTS")
    for name in LEGACY_INDEXES:
        store.run(f"DROP INDEX `{name}` IF EXISTS")
    store.run("CALL db.awaitIndexes(120)")

    after = inspect_generic_label_migration(store)
    if before["nodes"] != after["nodes"]:
        raise LabelMigrationError("node count changed during graph-label migration")
    if before["relationships"] != after["relationships"]:
        raise LabelMigrationError(
            "relationship count changed during graph-label migration"
        )
    if after["legacy_label_counts"] or after["unknown_legacy_label_counts"]:
        raise LabelMigrationError("legacy V8-prefixed node labels remain")
    if (
        after["legacy_constraints"]
        or after["legacy_indexes"]
        or after["unknown_legacy_constraints"]
        or after["unknown_legacy_indexes"]
    ):
        raise LabelMigrationError("legacy V8-prefixed schema objects remain")

    canonical_after = _canonical_snapshot(store)
    if canonical_after != canonical_before:
        raise LabelMigrationError(
            "canonical release fingerprint changed during graph-label migration"
        )
    active_places = _single_row(
        store.run(
            """
            MATCH (place:Place {kb_version: $kb_version})
            RETURN count(place) AS count
            """,
            kb_version=KB_VERSION,
        ),
        "count migrated active places",
    )

    return {
        "status": "migrated",
        "batch_size": batch_size,
        "before": before,
        "after": after,
        "added_label_counts": added,
        "removed_label_counts": removed,
        "active_release_count": 1,
        "active_release_id": canonical_after["release_id"],
        "embedding_dimension": canonical_after["embedding_dimension"],
        "active_place_count": int(active_places["count"]),
    }


def _require_known_legacy_surface(report: dict[str, Any]) -> None:
    unknown = {
        "labels": report["unknown_legacy_label_counts"],
        "constraints": report["unknown_legacy_constraints"],
        "indexes": report["unknown_legacy_indexes"],
    }
    if any(unknown.values()):
        raise LabelMigrationError(
            f"unknown V8-prefixed labels or schema require explicit mapping: {unknown}"
        )


def _require_isolated_database(report: dict[str, Any]) -> None:
    if report["owned_v8_nodes"] != report["nodes"]:
        raise LabelMigrationError(
            "NEO4J_V8_* must point to an isolated database whose nodes all "
            "belong to kb_version=v8"
        )


def _canonical_snapshot(store: LabelMigrationStore) -> dict[str, Any]:
    header = _single_row(
        store.run(
            """
            MATCH (catalog {
              id: 'v8:canonical-travel-catalog',
              kb_version: $kb_version,
              status: 'ready'
            })-[:CURRENT_RELEASE]->(release {
              kb_version: $kb_version,
              status: 'active'
            })
            RETURN release.id AS release_id,
                   release.release_hash AS release_hash,
                   release.dataset_id AS dataset_id,
                   release.dataset_hash AS dataset_hash,
                   release.place_count AS release_place_count,
                   release.city_count AS release_city_count,
                   release.document_count AS release_document_count,
                   release.text_unit_count AS release_text_unit_count,
                   release.fact_count AS release_fact_count,
                   release.claim_count AS release_claim_count,
                   release.manifest_json AS manifest_json,
                   catalog.current_release_id AS catalog_release_id,
                   catalog.embedding_dimension AS embedding_dimension,
                   catalog.static_graph_ready AS static_graph_ready,
                   catalog.source_place_count AS catalog_place_count,
                   catalog.source_city_count AS catalog_city_count,
                   catalog.source_document_count AS catalog_document_count,
                   catalog.source_text_unit_count AS catalog_text_unit_count,
                   catalog.source_fact_count AS catalog_fact_count,
                   catalog.source_claim_count AS catalog_claim_count
            """,
            kb_version=KB_VERSION,
        ),
        "validate ready catalog and active release",
    )
    release_id = str(header.get("release_id") or "")
    try:
        manifest = json.loads(str(header.get("manifest_json") or ""))
    except (TypeError, ValueError) as exc:
        raise LabelMigrationError("active release manifest_json is invalid") from exc
    if not isinstance(manifest, dict):
        raise LabelMigrationError("active release manifest_json must be an object")

    dimension = _positive_int(header.get("embedding_dimension"), "catalog dimension")
    manifest_dimension = _positive_int(
        manifest.get("embedding_dimension"),
        "release manifest dimension",
    )
    if dimension != manifest_dimension:
        raise LabelMigrationError(
            "catalog and release manifest embedding dimensions do not match"
        )
    if (
        not release_id
        or str(header.get("catalog_release_id") or "") != release_id
        or header.get("static_graph_ready") is not True
        or str(manifest.get("release_id") or "") != release_id
        or str(manifest.get("release_hash") or "")
        != str(header.get("release_hash") or "")
        or str(manifest.get("dataset_hash") or "")
        != str(header.get("dataset_hash") or "")
    ):
        raise LabelMigrationError(
            "ready catalog, active release, and immutable manifest disagree"
        )

    counts = _single_row(
        store.run(
            """
            MATCH (catalog {
              id: 'v8:canonical-travel-catalog',
              kb_version: $kb_version
            })-[:CURRENT_RELEASE]->(release {
              id: $release_id,
              kb_version: $kb_version
            })
            OPTIONAL MATCH (place {kb_version: $kb_version})
            WHERE place:V8Place OR place:Place
            WITH catalog, release, place
            ORDER BY place.id
            RETURN collect(DISTINCT place.id) AS place_ids,
                   count(DISTINCT place) AS place_count,
                   sum(count {
                     (place)-[:CURRENT_VERSION]->()
                   }) AS current_version_count,
                   sum(count {
                     (place)<-[:SOURCE_FOR]-()
                   }) AS document_count,
                   sum(count {
                     (place)<-[:MENTIONS]-()
                   }) AS text_unit_count,
                   sum(count {
                     (place)-[:HAS_FACT]->()
                   }) AS fact_count,
                   sum(count {
                     (place)-[:HAS_FACT]->()-[:SUPPORTED_BY]->()
                   }) AS fact_evidence_count,
                   sum(count {
                     (place)<-[:ABOUT]-()
                   }) AS claim_count,
                   sum(count {
                     (place)<-[:ABOUT]-()-[:OBJECT]->()
                   }) AS claim_object_count,
                   sum(count {
                     (place)<-[:ABOUT]-()-[:SUPPORTED_BY]->()
                   }) AS claim_evidence_count,
                   count { (catalog)-[:HAS_CITY]->() } AS city_count
            """,
            kb_version=KB_VERSION,
            release_id=release_id,
        ),
        "fingerprint active canonical graph",
    )
    place_ids = sorted(str(value) for value in counts.get("place_ids", []))
    graph_counts = {
        key: int(counts.get(key, 0) or 0)
        for key in (
            "place_count",
            "current_version_count",
            "document_count",
            "text_unit_count",
            "fact_count",
            "fact_evidence_count",
            "claim_count",
            "claim_object_count",
            "claim_evidence_count",
            "city_count",
        )
    }
    declared = {
        "place_count": _nonnegative_int(
            header.get("release_place_count"), "release place_count"
        ),
        "city_count": _nonnegative_int(
            header.get("release_city_count"), "release city_count"
        ),
        "document_count": _nonnegative_int(
            header.get("release_document_count"), "release document_count"
        ),
        "text_unit_count": _nonnegative_int(
            header.get("release_text_unit_count"), "release text_unit_count"
        ),
        "fact_count": _nonnegative_int(
            header.get("release_fact_count"), "release fact_count"
        ),
        "claim_count": _nonnegative_int(
            header.get("release_claim_count"), "release claim_count"
        ),
    }
    catalog_declared = {
        key: _nonnegative_int(header.get(f"catalog_{key}"), f"catalog {key}")
        for key in declared
    }
    manifest_declared = {
        key: _nonnegative_int(manifest.get(key), f"manifest {key}")
        for key in declared
    }
    expected_graph = {
        **declared,
        "current_version_count": declared["place_count"],
        "fact_evidence_count": declared["fact_count"],
        "claim_object_count": declared["claim_count"],
        "claim_evidence_count": declared["claim_count"],
    }
    if (
        declared != catalog_declared
        or declared != manifest_declared
        or graph_counts != expected_graph
    ):
        raise LabelMigrationError(
            "canonical graph counts disagree with catalog or release manifest"
        )

    place_id_hash = hashlib.sha256("\n".join(place_ids).encode()).hexdigest()
    return {
        "release_id": release_id,
        "release_hash": str(header.get("release_hash") or ""),
        "dataset_id": str(header.get("dataset_id") or ""),
        "dataset_hash": str(header.get("dataset_hash") or ""),
        "embedding_dimension": dimension,
        "place_id_hash": place_id_hash,
        **graph_counts,
    }


def _validate_vector_sources(
    store: LabelMigrationStore,
    *,
    embedding_dimension: int,
) -> dict[str, Any]:
    rows = {str(row["name"]): row for row in _index_rows(store)}
    domains = {
        "place": (
            ("v8_place_embedding", "V8Place"),
            ("place_embedding", "Place"),
        ),
        "concept": (
            ("v8_concept_embedding", "V8Concept"),
            ("concept_embedding", "Concept"),
        ),
    }
    report: dict[str, Any] = {}
    for domain, candidates in domains.items():
        present: list[str] = []
        for name, label in candidates:
            row = rows.get(name)
            if row is None:
                continue
            present.append(name)
            spec = SchemaSpec(name, "VECTOR", label, ("embedding",))
            error = _schema_spec_error(row, spec, require_online=True)
            if error:
                raise LabelMigrationError(f"invalid vector source: {error}")
            _require_vector_options(row, embedding_dimension=embedding_dimension)
        if not present:
            raise LabelMigrationError(
                f"no existing vector index can validate the {domain} dimension"
            )
        dimensions = _single_row(
            store.run(
                f"""
                MATCH (node)
                WHERE node.kb_version = $kb_version
                  AND (node:`{candidates[0][1]}` OR node:`{candidates[1][1]}`)
                  AND node.embedding IS NOT NULL
                RETURN count(node) AS embedded_count,
                       collect(DISTINCT size(node.embedding)) AS dimensions
                """,
                kb_version=KB_VERSION,
            ),
            f"validate {domain} embedding values",
        )
        embedded_count = int(dimensions.get("embedded_count", 0))
        actual_dimensions = sorted(
            int(value) for value in dimensions.get("dimensions", [])
        )
        if embedded_count and actual_dimensions != [embedding_dimension]:
            raise LabelMigrationError(
                f"{domain} embeddings have dimensions {actual_dimensions}; "
                f"expected [{embedding_dimension}]"
            )
        report[domain] = {
            "indexes": present,
            "embedded_nodes": embedded_count,
            "embedding_dimensions": actual_dimensions,
        }
    return report


def _require_no_neutral_schema_collisions(
    store: LabelMigrationStore,
    *,
    embedding_dimension: int,
) -> None:
    constraints = {str(row["name"]): row for row in _constraint_rows(store)}
    indexes = {str(row["name"]): row for row in _index_rows(store)}
    errors: list[str] = []
    for spec in NEUTRAL_CONSTRAINTS:
        row = constraints.get(spec.name)
        if row is not None:
            error = _schema_spec_error(row, spec)
            if error:
                errors.append(error)
    for spec in NEUTRAL_INDEXES:
        row = indexes.get(spec.name)
        if row is None:
            continue
        error = _schema_spec_error(row, spec, require_online=True)
        if error:
            errors.append(error)
        elif spec.kind == "VECTOR":
            try:
                _require_vector_options(
                    row,
                    embedding_dimension=embedding_dimension,
                )
            except LabelMigrationError as exc:
                errors.append(str(exc))
    if errors:
        raise LabelMigrationError(
            "neutral schema name collision before migration: " + "; ".join(errors)
        )


def _require_neutral_schema(
    store: LabelMigrationStore,
    *,
    embedding_dimension: int,
) -> None:
    constraint_rows = _constraint_rows(store)
    index_rows = _index_rows(store)
    constraints = {str(row["name"]): row for row in constraint_rows}
    indexes = {str(row["name"]): row for row in index_rows}
    errors: list[str] = []

    for spec in NEUTRAL_CONSTRAINTS:
        row = constraints.get(spec.name)
        error = _schema_spec_error(row, spec)
        if error:
            errors.append(error)

    for spec in NEUTRAL_INDEXES:
        row = indexes.get(spec.name)
        error = _schema_spec_error(row, spec, require_online=True)
        if error:
            errors.append(error)
            continue
        if spec.kind == "VECTOR" and row is not None:
            try:
                _require_vector_options(
                    row,
                    embedding_dimension=embedding_dimension,
                )
            except LabelMigrationError as exc:
                errors.append(
                    str(exc)
                )

    if errors:
        raise LabelMigrationError(
            "neutral schema validation failed before cutover: " + "; ".join(errors)
        )


def _require_vector_options(
    row: dict[str, Any],
    *,
    embedding_dimension: int,
) -> None:
    options = row.get("options") or {}
    config = options.get("indexConfig") or {}
    dimension = config.get("vector.dimensions")
    similarity = str(config.get("vector.similarity_function") or "").lower()
    if dimension is None or int(dimension) != embedding_dimension:
        raise LabelMigrationError(
            f"{row.get('name')} has vector dimension {dimension!r}; "
            f"expected {embedding_dimension}"
        )
    if similarity != "cosine":
        raise LabelMigrationError(
            f"{row.get('name')} has vector similarity {similarity!r}; "
            "expected 'cosine'"
        )


def _schema_spec_error(
    row: dict[str, Any] | None,
    spec: SchemaSpec,
    *,
    require_online: bool = False,
) -> str | None:
    if row is None:
        return f"missing {spec.kind} schema object {spec.name}"
    actual = (
        _normalize_schema_kind(row.get("type")),
        str(row.get("entityType") or ""),
        tuple(str(value) for value in (row.get("labelsOrTypes") or [])),
        tuple(str(value) for value in (row.get("properties") or [])),
    )
    expected = (spec.kind, "NODE", (spec.label,), spec.properties)
    if actual != expected:
        return f"{spec.name} has definition {actual!r}; expected {expected!r}"
    if require_online and str(row.get("state") or "") != "ONLINE":
        return f"{spec.name} is not ONLINE"
    return None


def _constraint_rows(store: LabelMigrationStore) -> list[dict[str, Any]]:
    return store.run(
        """
        SHOW CONSTRAINTS
        YIELD name, type, entityType, labelsOrTypes, properties
        RETURN name, type, entityType, labelsOrTypes, properties
        """
    )


def _index_rows(store: LabelMigrationStore) -> list[dict[str, Any]]:
    return store.run(
        """
        SHOW INDEXES
        YIELD name, state, type, entityType, labelsOrTypes, properties,
              options, owningConstraint, failureMessage
        RETURN name, state, type, entityType, labelsOrTypes, properties,
               options, owningConstraint, failureMessage
        """
    )


def _targets_prefixed_label(row: dict[str, Any]) -> bool:
    if str(row.get("entityType") or "") != "NODE":
        return False
    return any(
        str(label).startswith(LEGACY_LABEL_PREFIX)
        for label in (row.get("labelsOrTypes") or [])
    )


def _normalize_schema_kind(value: Any) -> str:
    kind = str(value or "")
    if kind == "NODE_PROPERTY_UNIQUENESS":
        return "UNIQUENESS"
    return kind


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed < 1:
        raise LabelMigrationError(f"{field} must be positive")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LabelMigrationError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise LabelMigrationError(f"{field} must not be negative")
    return parsed


def _require_owned_legacy_nodes(store: LabelMigrationStore) -> None:
    row = _single_row(
        store.run(
            """
            MATCH (node)
            WHERE any(
              label IN labels(node)
              WHERE label STARTS WITH $label_prefix
            )
            RETURN count(node) AS total,
                   count(CASE
                     WHEN node.kb_version = $kb_version THEN 1
                   END) AS owned
            """,
            label_prefix=LEGACY_LABEL_PREFIX,
            kb_version=KB_VERSION,
        ),
        "validate legacy-label ownership",
    )
    if int(row.get("total", 0)) != int(row.get("owned", 0)):
        raise LabelMigrationError(
            "V8-prefixed labels include nodes not owned by kb_version=v8"
        )


def _add_replacement_labels(
    store: LabelMigrationStore,
    mapping: LabelMapping,
    *,
    batch_size: int,
) -> int:
    label_clause = "".join(f":`{label}`" for label in mapping.replacements)
    missing_clause = " OR ".join(
        f"NOT (node:`{label}`)" for label in mapping.replacements
    )
    total = 0
    while True:
        row = _single_row(
            store.run(
                f"""
                MATCH (node:`{mapping.legacy}`)
                WHERE node.kb_version = $kb_version
                  AND ({missing_clause})
                WITH node LIMIT $batch_size
                SET node{label_clause}
                RETURN count(node) AS processed
                """,
                kb_version=KB_VERSION,
                batch_size=batch_size,
            ),
            f"add replacement labels for {mapping.legacy}",
        )
        processed = int(row.get("processed", 0))
        total += processed
        if processed == 0:
            return total


def _require_complete_replacement_coverage(
    store: LabelMigrationStore,
    *,
    legacy_labels: set[str],
) -> None:
    for mapping in LABEL_MAPPINGS:
        if mapping.legacy not in legacy_labels:
            continue
        missing_clause = " OR ".join(
            f"NOT (node:`{label}`)" for label in mapping.replacements
        )
        row = _single_row(
            store.run(
                f"""
                MATCH (node:`{mapping.legacy}`)
                WHERE node.kb_version = $kb_version
                  AND ({missing_clause})
                RETURN count(node) AS missing
                """,
                kb_version=KB_VERSION,
            ),
            f"validate replacement labels for {mapping.legacy}",
        )
        if int(row.get("missing", 0)):
            raise LabelMigrationError(
                f"replacement coverage is incomplete for {mapping.legacy}"
            )


def _remove_legacy_label(
    store: LabelMigrationStore,
    label: str,
    *,
    batch_size: int,
) -> int:
    total = 0
    while True:
        row = _single_row(
            store.run(
                f"""
                MATCH (node:`{label}`)
                WHERE node.kb_version = $kb_version
                WITH node LIMIT $batch_size
                REMOVE node:`{label}`
                RETURN count(node) AS processed
                """,
                kb_version=KB_VERSION,
                batch_size=batch_size,
            ),
            f"remove legacy label {label}",
        )
        processed = int(row.get("processed", 0))
        total += processed
        if processed == 0:
            return total


def _graph_totals(store: LabelMigrationStore) -> dict[str, int]:
    nodes = _single_row(
        store.run("MATCH (node) RETURN count(node) AS count"),
        "count graph nodes",
    )
    relationships = _single_row(
        store.run("MATCH ()-[relationship]->() RETURN count(relationship) AS count"),
        "count graph relationships",
    )
    return {
        "nodes": int(nodes.get("count", 0)),
        "relationships": int(relationships.get("count", 0)),
    }


def _single_row(rows: list[dict[str, Any]], operation: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise LabelMigrationError(
            f"{operation} expected one row, received {len(rows)}"
        )
    return dict(rows[0])


__all__ = [
    "LABEL_MAPPINGS",
    "LEGACY_CONSTRAINTS",
    "LEGACY_INDEXES",
    "NEUTRAL_CONSTRAINTS",
    "NEUTRAL_INDEXES",
    "LabelMapping",
    "LabelMigrationError",
    "SchemaSpec",
    "apply_generic_label_migration",
    "inspect_generic_label_migration",
    "preflight_generic_label_migration",
]
