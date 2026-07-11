# KB V1 Retrieval Experiment

Ngay chay: 2026-07-11

Day la retrieval ablation tren cung KB V1, khong phai so sanh official KB V1 va KB V2.

## Provenance graph runtime

- Dataset: `travel_data_verified`, 519 places.
- 54 `Document` nodes: 48 usable crawled articles, 6 unusable sources van giu metadata.
- 536 crawled article `TextUnit` nodes.
- 519 verified-record `TextUnit` nodes.
- Tong: 1,055 TextUnits, 1,198 `MENTIONS`, coverage 519/519 Places.
- 1,055/1,055 TextUnits co embedding 1,536 dimensions.
- `text_unit_embedding` va `text_unit_fulltext` indexes: `ONLINE`.

Hai evidence tiers:

| Tier | Origin | Confidence | Purpose |
| --- | --- | ---: | --- |
| Article | `crawled_article` | 1.0 exact-name mention | Source-backed evidence |
| Record | `verified_record` | 0.7 owner link | Full Place coverage va structured facts |

## Retrieval variants

- `v1`: Place vector-first, keyword fallback.
- `v1_hybrid`: Place retrieval + deterministic graph filters + weighted RRF.
- `v1_provenance`: TextUnit vector + TextUnit fulltext + graph filters + weighted RRF, sau do
  traversal `TextUnit -> Place` va tra evidence URL/chunk.

## Provenance ablations

| Metric | Article only | Two-tier | Two-tier + graph filters |
| --- | ---: | ---: | ---: |
| Hit@5 | 0.8333 | 0.8333 | 1.0000 |
| Precision@5 | 0.3000 | 0.5000 | 0.7333 |
| Relevance@5 | 0.5667 | 0.7667 | 1.0000 |
| Accepted recall@5 | 0.4013 | 0.4810 | 0.6437 |
| MRR | 0.5056 | 0.8333 | 1.0000 |
| Mean latency | 5622.27 ms cold | 122.56 ms warm | 166.42 ms warm |

Final smoke benchmark co 6 cases L1-L3. Moi case deu hit, va accepted result dung dau moi query.
Case indoor/rain tang tu 0 valid result len 5/5 valid results nho structured graph filter.

## Limits van con

- Sau cung chi la 6 smoke cases; chua du de ket luan he thong "uu viet".
- Exact mention extraction chua co aliases/entity resolution bang model.
- 6 source articles khong co usable text; verified record la evidence fallback, khong phai article quote.
- Benchmark chua do evidence faithfulness, answer groundedness, diversity, geo constraint va L4-L5.
- Query feature extraction dang deterministic va chi phu mot so intent: work, seafood, rooftop, rain/indoor.
- Dynamic rating/open-hours chua co runtime provider.
- Smoke answer cho thay mot so opening-hours co the la gia tri mac dinh; answer layer phai hien
  timestamp/confidence va khong trinh bay field chua verify nhu live fact.

Raw reports:

- `nextrip_graphrag/evaluation/results/v1_provenance.json`
- `nextrip_graphrag/evaluation/results/v1_provenance_two_tier.json`
- `nextrip_graphrag/evaluation/results/v1_provenance_final.json`
