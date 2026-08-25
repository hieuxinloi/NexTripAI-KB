from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from .config import DEFAULT_TYPED_QUERY_TOP_K, Settings
from .evaluation import OfflinePlanner, run_benchmark, run_v3_benchmark
from .evaluation.l1_audit import run_l1_audit
from .evaluation.v2_runner import run_v2_benchmark
from .enrichment import (
    build_article_text_units,
    build_source_artifacts,
    crawl_source_documents,
    enrich_missing_addresses,
)
from .enrichment.io import read_jsonl
from .enrichment.embeddings import CachedBatchEmbedder
from .gemini_client import GeminiClient
from .neo4j_store import Neo4jGraphStore
from .normalizer import normalize_dataset, read_processed, write_processed
from .rag import TravelGraphRAG
from .retrieval import SearchRequest, available_strategies, get_strategy
from .versions.v2.graph_store import V2GraphStore
from .versions.v2.retrieval import V2RetrievalService
from .versions.v3.graph_store import V3GraphStore
from .versions.v3.retrieval import V3RetrievalService
from .versions.v4.extraction import DescriptionExtractor
from .versions.v4.graph_store import V4GraphStore
from .versions.v4.retrieval import V4RetrievalService
from .versions.v5.graph_store import V5GraphStore
from .versions.v5.retrieval import V5RetrievalService
from .versions.v6.retrieval import V6RetrievalService
from .versions.v7.retrieval import V7RetrievalService
from .versions.v8.retrieval import V8RetrievalService
from .versions.v8.graph_store import V8GraphStore
from .versions.v8.canonical_importer import (
    CanonicalV8ReleaseManifestWriter,
    apply_canonical_v8_import,
    prepare_canonical_v8_import,
)
from .versions.v8.observation_publisher import (
    V8ObservationPublisher,
    build_v8_observation_plan,
    write_v8_observation_plan,
)
from .versions.v8.label_migration import (
    apply_generic_label_migration,
    preflight_generic_label_migration,
)


DEFAULT_V3_BENCHMARK = (
    Path(__file__).resolve().parents[1] / "docs" / "test_cases_benchmark_v3.md"
)


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def cmd_prepare(args: argparse.Namespace) -> None:
    bundle = normalize_dataset(args.data_dir)
    write_processed(bundle, args.out_dir)
    print(json.dumps(bundle["manifest"], ensure_ascii=False, indent=2))
    print(f"Processed data written to {Path(args.out_dir).resolve()}")


def cmd_schema(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = Neo4jGraphStore(settings)
    try:
        store.ensure_schema(settings.embedding_dim)
    finally:
        store.close()
    print("Neo4j constraints, fulltext index, and vector index are ready.")


def cmd_load(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    bundle = read_processed(args.processed_dir)
    embedder = GeminiClient(settings) if args.with_embeddings else None
    store = Neo4jGraphStore(settings)
    try:
        store.ensure_schema(settings.embedding_dim)
        store.load_cities(bundle["cities"])
        store.load_places(
            bundle["places"],
            embedder=embedder,
            batch_size=args.batch_size,
        )
    finally:
        store.close()
        if embedder is not None:
            embedder.close()

    embedding_note = "with Gemini embeddings" if args.with_embeddings else "without embeddings"
    print(
        f"Loaded {len(bundle['cities'])} cities and {len(bundle['places'])} places into Neo4j {embedding_note}."
    )


def cmd_v2_build(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    bundle = read_processed(args.processed_dir)
    embedder = None
    if args.with_embeddings:
        embedder = CachedBatchEmbedder(
            GeminiClient(settings),
            Path(args.embedding_cache),
            model=settings.embedding_model,
            dimensions=settings.embedding_dim,
            delay=args.embedding_delay,
            max_retries=args.embedding_retries,
        )
    store = V2GraphStore(settings.for_v2())
    try:
        store.ensure_v2_schema(settings.embedding_dim)
        statistics = store.replace_graph(
            bundle["cities"],
            bundle["places"],
            embedder=embedder,
            batch_size=args.batch_size,
        )
    finally:
        store.close()
        if embedder is not None:
            embedder.close()
    print(json.dumps({"kb_version": "v2", **statistics}, ensure_ascii=False, indent=2))


def cmd_v2_query(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V2GraphStore(settings.for_v2())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        response = V2RetrievalService(store, gemini).query(args.query, args.top_k)
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    print(response.model_dump_json(indent=2))


def cmd_v2_validate(args: argparse.Namespace) -> None:
    store = V2GraphStore(Settings.from_env().for_v2())
    try:
        report = store.validate_invariants(args.expected_places)
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "pass":
        raise SystemExit(1)


def cmd_v2_benchmark(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V2GraphStore(settings.for_v2())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        report = run_v2_benchmark(
            V2RetrievalService(store, gemini),
            args.dataset,
        )
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)


def cmd_v2_l1_audit(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V2GraphStore(settings.for_v2())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        report = run_l1_audit(
            V2RetrievalService(store, gemini),
            args.source,
            args.canonical,
        )
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))
    print(f"Detailed report written to {output_path.resolve()}")


def cmd_v3_build(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    bundle = read_processed(args.processed_dir)
    embedder = None
    if args.with_embeddings:
        embedder = CachedBatchEmbedder(
            GeminiClient(settings),
            Path(args.embedding_cache),
            model=settings.embedding_model,
            dimensions=settings.embedding_dim,
            delay=args.embedding_delay,
            max_retries=args.embedding_retries,
        )
    store = V3GraphStore(settings.for_v3())
    try:
        store.ensure_v3_schema(settings.embedding_dim)
        statistics = store.replace_graph(
            bundle["cities"],
            bundle["places"],
            embedder=embedder,
            batch_size=args.batch_size,
        )
    finally:
        store.close()
        if embedder is not None:
            embedder.close()
    print(json.dumps({"kb_version": "v3", **statistics}, ensure_ascii=False, indent=2))


def cmd_v3_query(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V3GraphStore(settings.for_v3())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        response = V3RetrievalService(store, gemini).query(args.query, args.top_k)
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    print(response.model_dump_json(indent=2))


def cmd_v3_validate(args: argparse.Namespace) -> None:
    store = V3GraphStore(Settings.from_env().for_v3())
    try:
        report = store.validate_invariants(args.expected_places)
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "pass":
        raise SystemExit(1)


def cmd_v3_l1_audit(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V3GraphStore(settings.for_v3())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        report = run_l1_audit(
            V3RetrievalService(store, gemini),
            args.source,
            args.canonical,
        )
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))


def cmd_v4_build(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    bundle = read_processed(args.processed_dir)
    embedding_client = GeminiClient(settings) if args.with_embeddings else None
    extraction_client = GeminiClient(settings) if args.with_description_extraction else None
    embedder = None
    if embedding_client is not None:
        embedder = CachedBatchEmbedder(
            embedding_client,
            Path(args.embedding_cache),
            model=settings.embedding_model,
            dimensions=settings.embedding_dim,
            delay=args.embedding_delay,
            max_retries=args.embedding_retries,
        )
    extractor = DescriptionExtractor(
        extraction_client,
        Path(args.description_cache),
    )
    store = V4GraphStore(settings.for_v4(), extractor)
    try:
        store.ensure_v4_schema(settings.embedding_dim)
        statistics = store.replace_graph(
            bundle["cities"],
            bundle["places"],
            embedder=embedder,
            batch_size=args.batch_size,
        )
    finally:
        store.close()
        if embedding_client is not None:
            embedding_client.close()
        if extraction_client is not None:
            extraction_client.close()
    print(json.dumps({"kb_version": "v4", **statistics}, ensure_ascii=False, indent=2))


def cmd_v4_query(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V4GraphStore(settings.for_v4())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        response = V4RetrievalService(store, gemini).query(args.query, args.top_k)
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    print(response.model_dump_json(indent=2))


def cmd_v4_validate(args: argparse.Namespace) -> None:
    store = V4GraphStore(Settings.from_env().for_v4())
    try:
        report = store.validate_invariants(args.expected_places)
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "pass":
        raise SystemExit(1)


def cmd_v4_l1_audit(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V4GraphStore(settings.for_v4())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        report = run_l1_audit(
            V4RetrievalService(store, gemini),
            args.source,
            args.canonical,
        )
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))


def cmd_v5_build(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    bundle = read_processed(args.processed_dir)
    embedding_client = GeminiClient(settings) if args.with_embeddings else None
    extraction_client = GeminiClient(settings) if args.with_description_extraction else None
    embedder = None
    if embedding_client is not None:
        embedder = CachedBatchEmbedder(
            embedding_client,
            Path(args.embedding_cache),
            model=settings.embedding_model,
            dimensions=settings.embedding_dim,
            delay=args.embedding_delay,
            max_retries=args.embedding_retries,
        )
    extractor = DescriptionExtractor(extraction_client, Path(args.description_cache))
    store = V5GraphStore(settings.for_v5(), extractor)
    try:
        store.ensure_v5_schema(settings.embedding_dim)
        statistics = store.replace_graph(
            bundle["cities"],
            bundle["places"],
            embedder=embedder,
            batch_size=args.batch_size,
        )
    finally:
        store.close()
        if embedding_client is not None:
            embedding_client.close()
        if extraction_client is not None:
            extraction_client.close()
    print(json.dumps({"kb_version": "v5", **statistics}, ensure_ascii=False, indent=2))


def cmd_v5_query(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V5GraphStore(settings.for_v5())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        response = V5RetrievalService(store, gemini).query(args.query, args.top_k)
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    print(response.model_dump_json(indent=2))


def cmd_v5_validate(args: argparse.Namespace) -> None:
    store = V5GraphStore(Settings.from_env().for_v5())
    try:
        report = store.validate_invariants(args.expected_places)
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_v5_benchmark(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V5GraphStore(settings.for_v5())
    gemini = GeminiClient(settings) if args.planner_mode == "configured" else OfflinePlanner()
    try:
        report = run_v3_benchmark(
            V5RetrievalService(store, gemini),
            args.source,
            output_path=args.output,
        )
    finally:
        store.close()
        close = getattr(gemini, "close", None)
        if callable(close):
            close()
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )
    if report["status"] != "pass":
        raise SystemExit(1)


def cmd_v6_query(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V5GraphStore(settings.for_v5())
    gemini = GeminiClient(settings) if args.with_gemini_planner else None
    try:
        response = V6RetrievalService(store, gemini).query(args.query, args.top_k)
    finally:
        store.close()
        if gemini is not None:
            gemini.close()
    print(response.model_dump_json(indent=2))


def cmd_v6_benchmark(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V5GraphStore(settings.for_v5())
    gemini = GeminiClient(settings) if args.planner_mode == "configured" else OfflinePlanner()
    service = V6RetrievalService(store, gemini)
    try:
        report = run_v3_benchmark(
            service,
            args.source,
            output_path=args.output,
            conversation_runner=service.query_conversation,
            benchmark_version="v6",
        )
    finally:
        store.close()
        close = getattr(gemini, "close", None)
        if callable(close):
            close()
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_v7_query(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V5GraphStore(settings.for_v5())
    gemini = GeminiClient(settings)
    try:
        response = V7RetrievalService(store, gemini).query(args.query, args.top_k)
    finally:
        store.close()
        gemini.close()
    print(response.model_dump_json(indent=2))


def cmd_v7_benchmark(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V5GraphStore(settings.for_v5())
    gemini = GeminiClient(settings) if args.planner_mode == "configured" else OfflinePlanner()
    try:
        report = run_v3_benchmark(
            V7RetrievalService(store, gemini),
            args.source,
            output_path=args.output,
            benchmark_version="v7",
        )
    finally:
        store.close()
        close = getattr(gemini, "close", None)
        if callable(close):
            close()
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_v8_query(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V8GraphStore(settings.for_v8())
    gemini = GeminiClient(settings)
    try:
        response = V8RetrievalService(store, gemini).query(args.query, args.top_k)
    finally:
        store.close()
        gemini.close()
    print(response.model_dump_json(indent=2))


def cmd_v8_benchmark(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = V8GraphStore(settings.for_v8())
    gemini = GeminiClient(settings) if args.planner_mode == "configured" else OfflinePlanner()
    service = V8RetrievalService(store, gemini)
    try:
        report = run_v3_benchmark(
            service,
            args.source,
            output_path=args.output,
            conversation_runner=service.query_conversation,
            benchmark_version="v8",
        )
    finally:
        store.close()
        close = getattr(gemini, "close", None)
        if callable(close):
            close()
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_v8_project(args: argparse.Namespace) -> None:
    if not args.allow_legacy_shared_database:
        raise SystemExit(
            "v8-project is a destructive legacy migration. Use "
            "v8-import-canonical for isolated V8, or pass "
            "--allow-legacy-shared-database only for a reviewed shared V5/V8 DB."
        )
    settings = Settings.from_env()
    store = V8GraphStore(settings.for_v8())
    try:
        statistics = store.project_from_v5(replace=args.replace)
    finally:
        store.close()
    print(json.dumps(statistics, ensure_ascii=False, indent=2))


def cmd_v8_import_canonical(args: argparse.Namespace) -> None:
    """Validate an immutable canonical release and optionally activate it."""

    plan = prepare_canonical_v8_import(
        args.canonical_dataset,
        args.readiness,
        args.completeness_audit,
    )
    release_path = CanonicalV8ReleaseManifestWriter(args.output_root).write(
        plan.release
    )
    if args.apply:
        store = Neo4jGraphStore(Settings.from_neo4j_env("v8"))
        try:
            result = apply_canonical_v8_import(
                store,
                plan,
                batch_size=args.batch_size,
            )
        finally:
            store.close()
    else:
        result = plan.dry_run_result()
    print(
        json.dumps(
            {
                "release_manifest": str(release_path),
                "result": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


class _DryRunObservationStore:
    def run(self, _query: str, **_params: object) -> list[dict[str, object]]:
        raise AssertionError("dry-run observation publishing touched Neo4j")


def cmd_v8_publish_observations(args: argparse.Namespace) -> None:
    """Plan verified dynamic observations and optionally append them to V8."""

    plan = build_v8_observation_plan(
        args.canonical_dataset,
        hotel_price_root=args.hotel_price_root,
        hotel_availability_root=args.hotel_availability_root,
        current_menu_root=args.menu_root,
        opening_approval_root=args.opening_approval_root,
    )
    output_root = Path(args.output_root)
    plan_path = write_v8_observation_plan(
        output_root / f"plan={plan.plan_id}" / "v8-observation-plan.json",
        plan,
    )
    run_path = (
        output_root
        / f"run={plan.plan_id}-{'apply' if args.apply else 'dry-run'}-{uuid4().hex}"
        / "v8-observation-publish-manifest.json"
    )
    store: Neo4jGraphStore | _DryRunObservationStore
    store = (
        Neo4jGraphStore(Settings.from_neo4j_env("v8"))
        if args.apply
        else _DryRunObservationStore()
    )
    try:
        manifest = V8ObservationPublisher(
            store,
            batch_size=args.batch_size,
        ).publish(
            plan,
            dry_run=not args.apply,
            manifest_path=run_path,
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    print(
        json.dumps(
            {
                "plan": str(plan_path),
                "publish_manifest": str(run_path),
                "result": manifest.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_v8_migrate_labels(args: argparse.Namespace) -> None:
    """Replace legacy V8-prefixed graph labels without changing graph data."""

    store = Neo4jGraphStore(Settings.from_neo4j_env("v8"))
    try:
        if args.apply:
            result = apply_generic_label_migration(
                store,
                batch_size=args.batch_size,
            )
        else:
            result = {
                "status": "planned",
                "batch_size": args.batch_size,
                "preflight": preflight_generic_label_migration(store),
            }
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_ask(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = Neo4jGraphStore(settings)
    gemini = GeminiClient(settings)
    rag = TravelGraphRAG(store, gemini)
    try:
        answer = rag.answer(
            question=args.question,
            city=args.city,
            entity_types=args.type or None,
            top_k=args.top_k or settings.top_k,
            strategy=args.strategy,
        )
    finally:
        store.close()
        gemini.close()
    print(answer)


def cmd_search(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    city_id = None
    if args.city:
        from .normalizer import CITY_DEFINITIONS, canonical_city

        city_id = CITY_DEFINITIONS[canonical_city(args.city)]["id"]
    store = Neo4jGraphStore(settings)
    gemini = GeminiClient(settings)
    try:
        response = get_strategy(args.strategy).search(
            SearchRequest(
                query=args.query,
                limit=args.top_k or settings.top_k,
                city_id=city_id,
                entity_types=args.type or None,
            ),
            store,
            gemini,
        )
    finally:
        store.close()
        gemini.close()
    payload = {
        "strategy": response.strategy,
        "results": [
            {
                "place_id": row["place"].get("id"),
                "name": row["place"].get("name"),
                "score": row.get("score"),
                "retrieval": row.get("retrieval", {}),
            }
            for row in response.results
        ],
        "trace": response.trace,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_benchmark(args: argparse.Namespace) -> None:
    report = run_benchmark(
        Settings.from_env(),
        strategy_name=args.strategy,
        dataset_path=args.dataset,
    )
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(f"Benchmark report written to {output_path.resolve()}")
    print(output)


def cmd_build_source_artifacts(args: argparse.Namespace) -> None:
    report = build_source_artifacts(args.data_dir, args.workspace)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_crawl_sources(args: argparse.Namespace) -> None:
    report = crawl_source_documents(
        args.data_dir,
        args.workspace,
        limit=args.limit,
        refresh=args.refresh,
        delay=args.delay,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_enrich_addresses(args: argparse.Namespace) -> None:
    report = enrich_missing_addresses(
        args.data_dir,
        args.workspace,
        limit=args.limit,
        refresh=args.refresh,
        delay=args.delay,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_build_article_text_units(args: argparse.Namespace) -> None:
    report = build_article_text_units(
        args.workspace,
        max_chars=args.max_chars,
        overlap_chars=args.overlap_chars,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_load_evidence(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    output_dir = Path(args.workspace) / "output"
    documents = read_jsonl(output_dir / "graph_documents.jsonl")
    text_units = read_jsonl(output_dir / "article_text_units.jsonl")
    embedder = None
    if args.with_embeddings:
        embedder = CachedBatchEmbedder(
            GeminiClient(settings),
            Path(args.workspace) / "cache" / "embeddings",
            model=settings.embedding_model,
            dimensions=settings.embedding_dim,
            delay=args.embedding_delay,
            max_retries=args.embedding_retries,
        )
    store = Neo4jGraphStore(settings)
    try:
        store.ensure_schema(settings.embedding_dim)
        store.load_evidence_graph(
            documents,
            text_units,
            embedder=embedder,
            batch_size=args.batch_size,
            replace=not args.keep_existing,
        )
    finally:
        store.close()
        if embedder is not None:
            embedder.close()
    mode = "with embeddings" if embedder else "without embeddings"
    print(f"Loaded {len(documents)} Documents and {len(text_units)} TextUnits {mode}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextrip-graphrag",
        description="Build and query a Neo4j GraphRAG system for NexTrip travel data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="Normalize an explicitly supplied historical travel dataset.",
    )
    prepare.add_argument(
        "--data-dir",
        required=True,
        help="External legacy import directory; never a V8/current data source.",
    )
    prepare.add_argument(
        "--out-dir",
        required=True,
        help="Explicit historical output directory; V8 never reads it.",
    )
    prepare.set_defaults(func=cmd_prepare)

    schema = subparsers.add_parser("schema", help="Create Neo4j constraints and indexes.")
    schema.set_defaults(func=cmd_schema)

    load = subparsers.add_parser("load", help="Load processed data into Neo4j.")
    load.add_argument("--processed-dir", default="processed_verified")
    load.add_argument("--with-embeddings", action="store_true", help="Generate Gemini embeddings while loading.")
    load.add_argument("--batch-size", type=int, default=16)
    load.set_defaults(func=cmd_load)

    v2_build = subparsers.add_parser(
        "v2-build",
        help="Build the isolated typed/provenance GraphRAG V2 graph.",
    )
    v2_build.add_argument("--processed-dir", default="processed_verified")
    v2_build.add_argument("--with-embeddings", action="store_true")
    v2_build.add_argument("--batch-size", type=int, default=16)
    v2_build.add_argument("--embedding-cache", default="tmp/v2_embedding_cache")
    v2_build.add_argument("--embedding-delay", type=float, default=0.5)
    v2_build.add_argument("--embedding-retries", type=int, default=5)
    v2_build.set_defaults(func=cmd_v2_build)

    v2_query = subparsers.add_parser(
        "v2-query",
        help="Plan and execute a typed GraphRAG V2 query.",
    )
    v2_query.add_argument("query")
    v2_query.add_argument("--top-k", type=int, default=DEFAULT_TYPED_QUERY_TOP_K)
    v2_query.add_argument("--with-gemini-planner", action="store_true")
    v2_query.set_defaults(func=cmd_v2_query)

    v2_validate = subparsers.add_parser(
        "v2-validate",
        help="Validate V2 graph counts, hierarchy and provenance invariants.",
    )
    v2_validate.add_argument("--expected-places", type=int, default=692)
    v2_validate.set_defaults(func=cmd_v2_validate)

    v2_benchmark = subparsers.add_parser(
        "v2-benchmark",
        help="Run the typed V2 benchmark and report deterministic pass/fail results.",
    )
    v2_benchmark.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "evaluation" / "datasets" / "l1_1_v2.json"),
    )
    v2_benchmark.add_argument("--output", default=None)
    v2_benchmark.add_argument("--with-gemini-planner", action="store_true")
    v2_benchmark.set_defaults(func=cmd_v2_benchmark)

    v2_l1_audit = subparsers.add_parser(
        "v2-l1-audit",
        help="Run all 100 Level 1 Markdown cases and separate strict accuracy from coverage.",
    )
    v2_l1_audit.add_argument("--source", default="docs/test_cases_benchmark.md")
    v2_l1_audit.add_argument(
        "--canonical",
        default=str(Path(__file__).parent / "evaluation" / "datasets" / "l1_1_v2.json"),
    )
    v2_l1_audit.add_argument(
        "--output",
        default=str(Path(__file__).parent / "evaluation" / "results" / "level_1_v2_audit.json"),
    )
    v2_l1_audit.add_argument("--with-gemini-planner", action="store_true")
    v2_l1_audit.set_defaults(func=cmd_v2_l1_audit)

    v3_build = subparsers.add_parser("v3-build", help="Build GraphRAG V3 in isolated Neo4j.")
    v3_build.add_argument("--processed-dir", default="processed_verified")
    v3_build.add_argument("--with-embeddings", action="store_true")
    v3_build.add_argument("--batch-size", type=int, default=16)
    v3_build.add_argument("--embedding-cache", default="tmp/v3_embedding_cache")
    v3_build.add_argument("--embedding-delay", type=float, default=0.5)
    v3_build.add_argument("--embedding-retries", type=int, default=5)
    v3_build.set_defaults(func=cmd_v3_build)

    v3_query = subparsers.add_parser("v3-query", help="Run a typed graph-first V3 query.")
    v3_query.add_argument("query")
    v3_query.add_argument("--top-k", type=int, default=DEFAULT_TYPED_QUERY_TOP_K)
    v3_query.add_argument("--with-gemini-planner", action="store_true")
    v3_query.set_defaults(func=cmd_v3_query)

    v3_validate = subparsers.add_parser("v3-validate", help="Validate V3 graph invariants.")
    v3_validate.add_argument("--expected-places", type=int, default=692)
    v3_validate.set_defaults(func=cmd_v3_validate)

    v3_l1_audit = subparsers.add_parser("v3-l1-audit", help="Audit all 100 L1 cases against V3.")
    v3_l1_audit.add_argument("--source", default="docs/test_cases_benchmark.md")
    v3_l1_audit.add_argument(
        "--canonical",
        default=str(Path(__file__).parent / "evaluation" / "datasets" / "l1_1_v2.json"),
    )
    v3_l1_audit.add_argument(
        "--output",
        default=str(Path(__file__).parent / "evaluation" / "results" / "level_1_v3_audit.json"),
    )
    v3_l1_audit.add_argument("--with-gemini-planner", action="store_true")
    v3_l1_audit.set_defaults(func=cmd_v3_l1_audit)

    v4_build = subparsers.add_parser("v4-build", help="Build ontology-guided GraphRAG V4.")
    v4_build.add_argument("--processed-dir", default="processed_verified")
    v4_build.add_argument("--with-embeddings", action="store_true")
    v4_build.add_argument("--with-description-extraction", action="store_true")
    v4_build.add_argument("--batch-size", type=int, default=16)
    v4_build.add_argument("--embedding-cache", default="tmp/v4_embedding_cache")
    v4_build.add_argument("--description-cache", default="tmp/v4_description_cache")
    v4_build.add_argument("--embedding-delay", type=float, default=0.5)
    v4_build.add_argument("--embedding-retries", type=int, default=5)
    v4_build.set_defaults(func=cmd_v4_build)

    v4_query = subparsers.add_parser("v4-query", help="Run ontology-guided adaptive V4 retrieval.")
    v4_query.add_argument("query")
    v4_query.add_argument("--top-k", type=int, default=DEFAULT_TYPED_QUERY_TOP_K)
    v4_query.add_argument("--with-gemini-planner", action="store_true")
    v4_query.set_defaults(func=cmd_v4_query)

    v4_validate = subparsers.add_parser("v4-validate", help="Validate V4 graph and claim invariants.")
    v4_validate.add_argument("--expected-places", type=int, default=None)
    v4_validate.set_defaults(func=cmd_v4_validate)

    v4_l1_audit = subparsers.add_parser("v4-l1-audit", help="Audit all L1 cases against V4.")
    v4_l1_audit.add_argument("--source", default="docs/test_cases_benchmark.md")
    v4_l1_audit.add_argument(
        "--canonical",
        default=str(Path(__file__).parent / "evaluation" / "datasets" / "l1_1_v2.json"),
    )
    v4_l1_audit.add_argument(
        "--output",
        default=str(Path(__file__).parent / "evaluation" / "results" / "level_1_v4_audit.json"),
    )
    v4_l1_audit.add_argument("--with-gemini-planner", action="store_true")
    v4_l1_audit.set_defaults(func=cmd_v4_l1_audit)

    v5_build = subparsers.add_parser("v5-build", help="Build typed-target GraphRAG V5.")
    v5_build.add_argument("--processed-dir", default="processed_verified")
    v5_build.add_argument("--with-embeddings", action="store_true")
    v5_build.add_argument("--with-description-extraction", action="store_true")
    v5_build.add_argument("--batch-size", type=int, default=16)
    v5_build.add_argument("--embedding-cache", default="tmp/v5_embedding_cache")
    v5_build.add_argument("--description-cache", default="tmp/v5_description_cache")
    v5_build.add_argument("--embedding-delay", type=float, default=0.5)
    v5_build.add_argument("--embedding-retries", type=int, default=5)
    v5_build.set_defaults(func=cmd_v5_build)

    v5_query = subparsers.add_parser("v5-query", help="Run typed-target V5 retrieval.")
    v5_query.add_argument("query")
    v5_query.add_argument("--top-k", type=int, default=DEFAULT_TYPED_QUERY_TOP_K)
    v5_query.add_argument("--with-gemini-planner", action="store_true")
    v5_query.set_defaults(func=cmd_v5_query)

    v5_validate = subparsers.add_parser("v5-validate", help="Validate V5 graph invariants.")
    v5_validate.add_argument("--expected-places", type=int, default=None)
    v5_validate.set_defaults(func=cmd_v5_validate)

    v5_benchmark = subparsers.add_parser(
        "v5-benchmark",
        help="Run all 500 GraphRAG benchmark v3 cases against V5.",
    )
    v5_benchmark.add_argument(
        "--source",
        default=str(DEFAULT_V3_BENCHMARK),
    )
    v5_benchmark.add_argument(
        "--output",
        default=str(
            Path(__file__).parent
            / "evaluation"
            / "results"
            / "benchmark_v3_v5.json"
        ),
    )
    v5_benchmark.add_argument(
        "--planner-mode",
        choices=("offline", "configured"),
        default="offline",
    )
    v5_benchmark.set_defaults(func=cmd_v5_benchmark)

    v6_query = subparsers.add_parser(
        "v6-query",
        help="Run stateful, itinerary-aware V6 retrieval.",
    )
    v6_query.add_argument("query")
    v6_query.add_argument("--top-k", type=int, default=DEFAULT_TYPED_QUERY_TOP_K)
    v6_query.add_argument("--with-gemini-planner", action="store_true")
    v6_query.set_defaults(func=cmd_v6_query)

    v6_benchmark = subparsers.add_parser(
        "v6-benchmark",
        help="Run all 500 GraphRAG benchmark v3 cases against V6.",
    )
    v6_benchmark.add_argument(
        "--source",
        default=str(DEFAULT_V3_BENCHMARK),
    )
    v6_benchmark.add_argument(
        "--output",
        default=str(
            Path(__file__).parent
            / "evaluation"
            / "results"
            / "benchmark_v3_v6.json"
        ),
    )
    v6_benchmark.add_argument(
        "--planner-mode",
        choices=("offline", "configured"),
        default="offline",
    )
    v6_benchmark.set_defaults(func=cmd_v6_benchmark)

    v7_query = subparsers.add_parser(
        "v7-query",
        help="Run LLM-native semantic planning with graph entity grounding.",
    )
    v7_query.add_argument("query")
    v7_query.add_argument("--top-k", type=int, default=DEFAULT_TYPED_QUERY_TOP_K)
    v7_query.set_defaults(func=cmd_v7_query)

    v7_benchmark = subparsers.add_parser(
        "v7-benchmark",
        help="Run the GraphRAG benchmark with the semantic-only V7 planner.",
    )
    v7_benchmark.add_argument(
        "--source",
        default=str(DEFAULT_V3_BENCHMARK),
    )
    v7_benchmark.add_argument(
        "--output",
        default=str(
            Path(__file__).parent
            / "evaluation"
            / "results"
            / "benchmark_v3_v7.json"
        ),
    )
    v7_benchmark.add_argument(
        "--planner-mode",
        choices=("offline", "configured"),
        default="configured",
    )
    v7_benchmark.set_defaults(func=cmd_v7_benchmark)

    v8_query = subparsers.add_parser(
        "v8-query",
        help="Run V8 tolerant semantic, stateful GraphRAG retrieval.",
    )
    v8_query.add_argument("query")
    v8_query.add_argument("--top-k", type=int, default=DEFAULT_TYPED_QUERY_TOP_K)
    v8_query.set_defaults(func=cmd_v8_query)

    v8_benchmark = subparsers.add_parser(
        "v8-benchmark",
        help="Run the GraphRAG benchmark against V8.",
    )
    v8_benchmark.add_argument(
        "--source",
        default=str(DEFAULT_V3_BENCHMARK),
    )
    v8_benchmark.add_argument(
        "--output",
        default=str(
            Path(__file__).parent
            / "evaluation"
            / "results"
            / "benchmark_v3_v8.json"
        ),
    )
    v8_benchmark.add_argument(
        "--planner-mode",
        choices=("offline", "configured"),
        default="configured",
    )
    v8_benchmark.set_defaults(func=cmd_v8_benchmark)

    v8_project = subparsers.add_parser(
        "v8-project",
        help=(
            "Legacy only: project V5 into V8 when both already share one "
            "database; new V8 databases use v8-import-canonical."
        ),
    )
    v8_project.add_argument(
        "--replace",
        action="store_true",
        help="Replace only the existing kb_version=v8 projection.",
    )
    v8_project.add_argument(
        "--allow-legacy-shared-database",
        action="store_true",
        help=(
            "Acknowledge the legacy shared V5/V8 migration; canonical-managed "
            "V8 databases are still rejected."
        ),
    )
    v8_project.set_defaults(func=cmd_v8_project)

    v8_import_canonical = subparsers.add_parser(
        "v8-import-canonical",
        help=(
            "Validate a gated canonical dataset and optionally activate it in "
            "the isolated Neo4j V8 database."
        ),
    )
    v8_import_canonical.add_argument("--canonical-dataset", required=True)
    v8_import_canonical.add_argument("--readiness", required=True)
    v8_import_canonical.add_argument("--completeness-audit", required=True)
    v8_import_canonical.add_argument(
        "--output-root",
        default="data/neo4j/v8/releases",
    )
    v8_import_canonical.add_argument("--batch-size", type=int, default=500)
    v8_import_canonical.add_argument(
        "--apply",
        action="store_true",
        help="Activate the validated release in NEO4J_V8_*; default is dry-run.",
    )
    v8_import_canonical.set_defaults(func=cmd_v8_import_canonical)

    v8_publish_observations = subparsers.add_parser(
        "v8-publish-observations",
        help=(
            "Validate canonical opening plus current price/availability/menu "
            "artifacts and optionally append them to the active V8 release."
        ),
    )
    v8_publish_observations.add_argument("--canonical-dataset", required=True)
    v8_publish_observations.add_argument(
        "--hotel-price-root",
        default="data/current/hotel_price",
    )
    v8_publish_observations.add_argument(
        "--hotel-availability-root",
        default="data/current/hotel_availability",
    )
    v8_publish_observations.add_argument(
        "--menu-root",
        default="data/current/menu",
    )
    v8_publish_observations.add_argument(
        "--opening-approval-root",
        default="data/approvals/opening_status",
        help=(
            "Immutable human approvals for canonical pending opening reviews."
        ),
    )
    v8_publish_observations.add_argument(
        "--output-root",
        default="data/neo4j/v8/observation_runs",
    )
    v8_publish_observations.add_argument("--batch-size", type=int, default=500)
    v8_publish_observations.add_argument(
        "--apply",
        action="store_true",
        help="Append the plan to NEO4J_V8_*; default is an offline dry-run.",
    )
    v8_publish_observations.set_defaults(func=cmd_v8_publish_observations)

    v8_migrate_labels = subparsers.add_parser(
        "v8-migrate-labels",
        help=(
            "Replace legacy V8-prefixed Neo4j labels with canonical business "
            "labels; default is a read-only plan."
        ),
    )
    v8_migrate_labels.add_argument("--batch-size", type=int, default=500)
    v8_migrate_labels.add_argument(
        "--apply",
        action="store_true",
        help="Apply the resumable label/schema migration to NEO4J_V8_*.",
    )
    v8_migrate_labels.set_defaults(func=cmd_v8_migrate_labels)

    ask = subparsers.add_parser("ask", help="Ask the GraphRAG chatbot.")
    ask.add_argument("question")
    ask.add_argument("--city", choices=["Quy Nhơn", "Đà Nẵng", "Quy Nhon", "Da Nang"], default=None)
    ask.add_argument(
        "--type",
        action="append",
        choices=["attraction", "cafe", "hotel", "nightlife", "restaurant"],
        help="Filter by place type. Can be passed multiple times.",
    )
    ask.add_argument("--top-k", type=int, default=None)
    ask.add_argument("--strategy", choices=available_strategies(), default="v1")
    ask.set_defaults(func=cmd_ask)

    search = subparsers.add_parser("search", help="Run a versioned retrieval strategy without answer generation.")
    search.add_argument("query")
    search.add_argument("--city", default=None)
    search.add_argument("--type", action="append", choices=["attraction", "cafe", "hotel", "nightlife", "restaurant"])
    search.add_argument("--top-k", type=int, default=None)
    search.add_argument("--strategy", choices=available_strategies(), default="v1")
    search.set_defaults(func=cmd_search)

    benchmark = subparsers.add_parser("benchmark", help="Compare a retrieval strategy against a benchmark dataset.")
    benchmark.add_argument("--strategy", choices=available_strategies(), required=True)
    benchmark.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "evaluation" / "datasets" / "smoke_v1.json"),
    )
    benchmark.add_argument("--output", default=None)
    benchmark.set_defaults(func=cmd_benchmark)

    artifacts = subparsers.add_parser(
        "build-source-artifacts",
        help="Build the staged source catalog and place-level text units.",
    )
    artifacts.add_argument(
        "--data-dir",
        required=True,
        help="External legacy import directory; never a V8/current data source.",
    )
    artifacts.add_argument("--workspace", required=True)
    artifacts.set_defaults(func=cmd_build_source_artifacts)

    crawl = subparsers.add_parser(
        "crawl-sources",
        help="Crawl source articles into a local, robots-aware staging workspace.",
    )
    crawl.add_argument(
        "--data-dir",
        required=True,
        help="External legacy import directory; never a V8/current data source.",
    )
    crawl.add_argument("--workspace", required=True)
    crawl.add_argument("--limit", type=int, default=None)
    crawl.add_argument("--delay", type=float, default=0.75)
    crawl.add_argument("--refresh", action="store_true")
    crawl.set_defaults(func=cmd_crawl_sources)

    addresses = subparsers.add_parser(
        "enrich-addresses",
        help="Collect review-only address candidates from OpenStreetMap Nominatim.",
    )
    addresses.add_argument(
        "--data-dir",
        required=True,
        help="External legacy import directory; never a V8/current data source.",
    )
    addresses.add_argument("--workspace", required=True)
    addresses.add_argument("--limit", type=int, default=None)
    addresses.add_argument("--delay", type=float, default=1.1)
    addresses.add_argument("--refresh", action="store_true")
    addresses.set_defaults(func=cmd_enrich_addresses)

    article_units = subparsers.add_parser(
        "build-article-text-units",
        help="Chunk usable source articles and extract exact Place mentions.",
    )
    article_units.add_argument("--workspace", default="enrichment_workspace")
    article_units.add_argument("--max-chars", type=int, default=1400)
    article_units.add_argument("--overlap-chars", type=int, default=180)
    article_units.set_defaults(func=cmd_build_article_text_units)

    evidence = subparsers.add_parser(
        "load-evidence",
        help="Load Document/TextUnit provenance graph into Neo4j.",
    )
    evidence.add_argument("--workspace", default="enrichment_workspace")
    evidence.add_argument("--with-embeddings", action="store_true")
    evidence.add_argument("--batch-size", type=int, default=16)
    evidence.add_argument("--embedding-delay", type=float, default=2.0)
    evidence.add_argument("--embedding-retries", type=int, default=5)
    evidence.add_argument("--keep-existing", action="store_true")
    evidence.set_defaults(func=cmd_load_evidence)

    return parser


def main() -> None:
    configure_console_encoding()
    load_dotenv_if_available()
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
