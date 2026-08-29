# Data Enrichment Run Report

Run date: 2026-07-11

## Scope

Pipeline chi doc `travel_data_verified/`. Khong file verified nao bi sua. Cache, article text va
address candidates nam trong `enrichment_workspace/` va khong duoc commit.

## Provenance artifacts

- 519 verified places.
- 54 unique source article URLs duoc tao thanh source catalog.
- 519/519 place co `TextUnit` gan voi source `Document`.
- Full crawl request: 54 URLs.
- 48 Documents dat nguong toi thieu 500 ky tu article text.
- 4 Documents co text nhung khong du chat luong de lam evidence.
- 2 Documents that bai: mot HTTP 403, mot trang khong trich duoc text.

Hai source that bai:

- `dulichviet.com.vn/...30-dia-diem-du-lich-binh-dinh...`: HTTP 403.
- `pasgo.vn/...quan-nhau-quy-nhon...`: HTTP 200 nhung article text rong.

Bon source `insufficient_article_text` can crawler adapter hoac source thay the:

- Bazan Travel bar Da Nang.
- Review Da Nang local food.
- Food Hunter cafe Quy Nhon.
- Quy Nhon Me hotel article.

Ket luan: URL ton tai khong dong nghia voi provenance co the dung ngay. Document can quality gate,
content hash va failure reason truoc khi load vao graph.

## Address candidates

Nominatim duoc chay tuan tu cho 175 places dang thieu address, voi cache va delay 1.1 giay:

- 21 `high_confidence_review`.
- 28 `manual_review`.
- 125 `reverse_context_review` sau khi name search khong co ket qua.
- 1 `no_match`: Soul Specialty Coffee Da Nang.
- 1 provider no-result payload, duoc xu ly thanh no-match thay vi lam dung batch.
- 0 automatic merges.

Nhom high-confidence gom cac entity de doi chieu nhu Eo Gio, Hon Kho, Thap Doi, Cau Rong, Cho Con,
Da Nang Downtown va Surf Bar. Tat ca van can review vi reverse/search geocoding co the tra ve OSM
object gan nhat thay vi dung dia diem mong muon.

## Findings for graph quality

- `Surf Bar` va `Surf bar Quy Nhon` cung map den mot OSM object. Day la duplicate candidate can
  entity resolution truoc khi build GraphRAG V1 hoan chinh.
- OSM hien co ten don vi hanh chinh moi trong mot so display address. Khong nen dung chuoi address
  de suy ra city node mot cach may moc; city verified va toa do van la constraint chinh.
- Chi 49/175 place co name-search candidate; 125 reverse results chi cung cap context quanh toa do.
  Nominatim khong du de lam nguon duy nhat cho cafe, hotel va restaurant. Buoc sau can source-specific
  extraction va manual verification queue, khong ha confidence threshold.
- Rating/review count khong duoc bo sung boi pipeline nay. Day la du lieu dong va rang buoc policy;
  neu can cho demo thi nen lay qua provider API hop le tai runtime.

## Decision

Address candidates chua duoc merge vao source of truth. Provenance artifacts da duoc load vao Neo4j:

- 54 Documents.
- 536 article TextUnits.
- 519 verified-record TextUnits.
- 1,198 MENTIONS edges va coverage 519/519 Places.
- 1,055 TextUnit embeddings.

Buoc tiep theo cua data enrichment la review address/duplicate/category queue; buoc tiep theo cua
GraphRAG research la mo rong benchmark L1-L5 va entity resolution.
