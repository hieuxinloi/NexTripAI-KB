from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .evaluation import run_benchmark
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

    embedding_note = "with Gemini embeddings" if args.with_embeddings else "without embeddings"
    print(
        f"Loaded {len(bundle['cities'])} cities and {len(bundle['places'])} places into Neo4j {embedding_note}."
    )


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
    print(answer)


def cmd_search(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    city_id = None
    if args.city:
        from .normalizer import CITY_DEFINITIONS, canonical_city

        city_id = CITY_DEFINITIONS[canonical_city(args.city)]["id"]
    store = Neo4jGraphStore(settings)
    try:
        response = get_strategy(args.strategy).search(
            SearchRequest(
                query=args.query,
                limit=args.top_k or settings.top_k,
                city_id=city_id,
                entity_types=args.type or None,
            ),
            store,
            GeminiClient(settings),
        )
    finally:
        store.close()
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
    mode = "with embeddings" if embedder else "without embeddings"
    print(f"Loaded {len(documents)} Documents and {len(text_units)} TextUnits {mode}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextrip-graphrag",
        description="Build and query a Neo4j GraphRAG system for NexTrip travel data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Normalize verified travel data into processed files.")
    prepare.add_argument("--data-dir", default="travel_data_verified")
    prepare.add_argument("--out-dir", default="processed_verified")
    prepare.set_defaults(func=cmd_prepare)

    schema = subparsers.add_parser("schema", help="Create Neo4j constraints and indexes.")
    schema.set_defaults(func=cmd_schema)

    load = subparsers.add_parser("load", help="Load processed data into Neo4j.")
    load.add_argument("--processed-dir", default="processed_verified")
    load.add_argument("--with-embeddings", action="store_true", help="Generate Gemini embeddings while loading.")
    load.add_argument("--batch-size", type=int, default=16)
    load.set_defaults(func=cmd_load)

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
    artifacts.add_argument("--data-dir", default="travel_data_verified")
    artifacts.add_argument("--workspace", default="enrichment_workspace")
    artifacts.set_defaults(func=cmd_build_source_artifacts)

    crawl = subparsers.add_parser(
        "crawl-sources",
        help="Crawl source articles into a local, robots-aware staging workspace.",
    )
    crawl.add_argument("--data-dir", default="travel_data_verified")
    crawl.add_argument("--workspace", default="enrichment_workspace")
    crawl.add_argument("--limit", type=int, default=None)
    crawl.add_argument("--delay", type=float, default=0.75)
    crawl.add_argument("--refresh", action="store_true")
    crawl.set_defaults(func=cmd_crawl_sources)

    addresses = subparsers.add_parser(
        "enrich-addresses",
        help="Collect review-only address candidates from OpenStreetMap Nominatim.",
    )
    addresses.add_argument("--data-dir", default="travel_data_verified")
    addresses.add_argument("--workspace", default="enrichment_workspace")
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
