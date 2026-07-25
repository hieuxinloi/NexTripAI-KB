# Data Enrichment Workflow

Tai lieu nay mo ta pipeline bo sung provenance va address cho `travel_data_verified`.
Pipeline chi tao candidate trong staging; khong lenh nao tu dong sua cac file verified.

## Nguyen tac

- `travel_data_verified/` la source of truth va chi duoc cap nhat sau khi review thu cong.
- `enrichment_workspace/` chua cache va output tam, duoc Git ignore.
- Moi bai viet nguon la mot `Document`; moi mo ta dia diem gan voi nguon la mot `TextUnit`.
- Crawler ton trong `robots.txt`, request tuan tu, co delay va cache de khong request lai.
- Nominatim chi tao address candidate. Toa do hien co duoc dung de tinh khoang cach va confidence.
- Khong crawl Google Maps rating/review de luu lau dai. Du lieu dong nay can API/provider hop le.

## 1. Tao source catalog va TextUnit

```powershell
python -m nextrip_graphrag build-source-artifacts
```

Output:

- `enrichment_workspace/output/source_catalog.json`
- `enrichment_workspace/output/text_units.jsonl`
- `enrichment_workspace/output/source_artifacts_report.json`

## 2. Crawl 54 source article

Smoke test truoc:

```powershell
python -m nextrip_graphrag crawl-sources --limit 3
```

Chay tat ca URL, cac URL da cache se khong bi request lai:

```powershell
python -m nextrip_graphrag crawl-sources
```

Output `source_documents.jsonl` chua title, cleaned text, content hash, URL va danh sach place
duoc bai viet ho tro. `crawl_report.json` liet ke URL thanh cong, loi HTTP va URL bi robots chan.

## 3. Tim address candidate

Smoke test:

```powershell
python -m nextrip_graphrag enrich-addresses --limit 5
```

Chay tat ca place dang thieu address:

```powershell
python -m nextrip_graphrag enrich-addresses
```

Nominatim public service yeu cau toi da mot request moi giay; CLI ep delay toi thieu 1 giay va
cache tung query. Output `address_candidates.jsonl` co ba trang thai:

- `high_confidence_review`: ten, city va khoang cach phu hop; van can nguoi review.
- `manual_review`: co candidate nhung evidence chua du manh.
- `reverse_context_review`: search theo ten khong co ket qua; address chi la OSM object gan toa do.
- `no_match`: khong tim thay candidate.

## 4. Review va merge

V1 nay chua co lenh auto-merge co chu dich. Reviewer can doi chieu URL/OSM candidate, dia chi,
toa do va city. Chi sau khi chap nhan moi cap nhat place trong `travel_data_verified` kem:

- `last_verified`
- `verification_status`
- `verified_sources`

Quy trinh nay giu ro provenance va ngan du lieu ngoai ghi de nham source of truth.

## 5. Build va load provenance graph

```powershell
python -m nextrip_graphrag build-article-text-units
python -m nextrip_graphrag load-evidence --with-embeddings
```

Graph co hai evidence tiers:

- `crawled_article`: article chunk co exact-name `MENTIONS`, confidence 1.0.
- `verified_record`: mot TextUnit cho moi Place, owner relationship confidence 0.7.

Embedding cache nam trong `enrichment_workspace/cache/embeddings`. Loader delay va retry khi Gemini API
AI tra quota error, nen co the resume ma khong embed lai content da thanh cong.
