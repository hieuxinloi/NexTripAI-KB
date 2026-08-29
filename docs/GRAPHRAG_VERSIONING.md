# GraphRAG Versioning

Mot GraphRAG version la snapshot cua toan bo pipeline, khong chi retriever.

## Version Contract

Moi version phai dong bang:

- `dataset_manifest`: input files, count va content hash.
- `ontology`: node types, edge types, patterns va required properties.
- `graph_builder`: cach tao node/edge va entity resolution.
- `embedding`: retrieval units, model, dimension va content template.
- `indexes`: vector/fulltext/range indexes.
- `retrieval`: anchor search, traversal, filtering va reranking.
- `evidence`: output contract va provenance.
- `evaluation`: benchmark version, metrics va raw results.

## Target Structure

```txt
nextrip_graphrag/
  core/                       # contracts dung chung, khong chua logic rieng cua version
  versions/
    v1/
      manifest.py
      ontology.py
      graph_builder.py
      embedding.py
      retrieval.py
      README.md
    v2/
      manifest.py
      ontology.py
      graph_builder.py
      embedding.py
      retrieval.py
      README.md
    v3/
    v4/
    v5/
  evaluation/
    datasets/
    results/v1/
    results/v2/
```

Shared implementation duoc phep dat trong `core`, nhung version manifest phai reference ro component va config dang dung. Khong copy `travel_data_verified` cho tung version.

## Current Classification

- Official KB V1: current Place/Term graph + current embedding template + vector-first/keyword fallback.
- `v1_hybrid` experiment: weighted RRF va mot so deterministic graph filters vua implement; day la retrieval ablation tren graph V1, khong phai official KB V2.
- Official KB V2: chua implement. V2 bat dau khi co typed ontology, provenance chunks, embedding cache/resume va graph-traversal retrieval.

## Demo Rule

V1 va V2 phai co the rebuild va query doc lap. Moi response/report phai ghi:

- `kb_version`
- `dataset_version`
- `ontology_version`
- `embedding_version`
- `retrieval_version`

Neu chi doi retriever tren cung graph, report phai ghi `retrieval_experiment`, khong tang `kb_version`.
