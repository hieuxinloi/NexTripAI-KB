# Retrieval Experiments On KB V1

Folder nay hien chi luu retrieval ablations tren cung graph schema V1.

| Strategy | Graph version | Mo ta |
| --- | --- | --- |
| `v1` | KB V1 | Vector-first, keyword fallback |
| `v1_hybrid` | KB V1 | Weighted RRF + deterministic graph filters |
| `v1_provenance` | KB V1 | TextUnit hybrid retrieval + graph filters + evidence |

Tat ca strategy tren la ablation cua KB V1; khong strategy nao la official GraphRAG V2.

Official KB versions V1-V5 phai version hoa ca ontology, graph builder, embedding va retrieval theo `docs/GRAPHRAG_VERSIONING.md`.
