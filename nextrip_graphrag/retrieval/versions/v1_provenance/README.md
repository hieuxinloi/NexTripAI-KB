# V1 Provenance Retrieval

Retrieval experiment tren graph V1 co `Document` va `TextUnit`:

1. Embed query.
2. Vector va fulltext search tren `TextUnit`.
3. Di theo `(:TextUnit)-[:MENTIONS]->(:Place)`.
4. Gom ket qua theo Place bang RRF.
5. Tra Place, graph context va source-backed evidence chunks.

Strategy nay khong phai GraphRAG V2. No duoc benchmark song song voi `v1` va `v1_hybrid`.
