# NexTrip browser crawl

The pipeline captures live hotel prices from the official Trivago remote MCP
and immutable HTML evidence from Google Maps. It does not require Agoda,
Booking.com, Trivago, or Google API keys.

The canonical Trivago website is `https://www.trivago.vn/`. Automated price
search uses the official endpoint `https://mcp.trivago.com/mcp` (documentation:
`https://mcp.trivago.com/docs`); `/vi/srl` is not treated as a public API
endpoint. The master coordinates select Trivago's radius-search tool, with text
search reserved for records that have no coordinates.

## Install runtimes

The hotel-price branch only needs the base project dependencies. Playwright is
needed only when Google Maps crawling is enabled later.

```powershell
python -m pip install -e ".[crawl]"
python -m playwright install chromium
```

## Capture a hotel price page

The operational mapping `config/trivago-mapping.json` currently connects
`hotel_qn_025` (Fleur De Lys Hotel Quy Nhon) to its Trivago accommodation.
The MCP call returns structured live prices without Playwright.

```powershell
python -m nextrip_pipeline.cli crawl-hotel-prices `
  --mapping config/trivago-mapping.json `
  --hotel-name "Khách sạn mẫu" `
  --destination "Quy Nhơn" `
  --check-in 2026-08-20 `
  --check-out 2026-08-21 `
  --adults 2 `
  --rooms 1
```

If omitted, occupancy defaults to `2` adults, `0` children, and `1` room. User
input overrides those values through `--adults`, `--rooms`, and `--children`.
For children, also pass every age, for example
`--children 2 --children-ages 6 10`.

Normalize the raw path printed by the crawl command:

```powershell
python -m nextrip_pipeline.cli normalize-hotel-price `
  --input "data/raw/entity=hotel_price/source=trivago-mcp/.../record=....json" `
  --mapping config/trivago-mapping.json
```

The normalized `HotelPriceObservation` is written below
`data/normalized/entity=hotel_price`. Normalization requires an exact Trivago
`accommodation_id` match and never interprets MCP `system_message` content.

Run the complete price refresh with one command:

```powershell
python -m nextrip_pipeline.cli refresh-hotel-price `
  --mapping config/trivago-mapping.json `
  --hotel-name "Fleur De Lys Hotel Quy Nhon" `
  --destination "Quy Nhơn" `
  --check-in 2026-08-20 `
  --check-out 2026-08-21
```

This writes raw, normalized, validation, and decision history, then atomically
updates the exact hotel-price context below:

```text
data/current/hotel_price/
  hotel=<hotel_id>/
  checkin=<date>/checkout=<date>/
  occupancy=<adults>a-<children>c-<rooms>r/
  children_ages=<sorted-ages-or-none>/
  offer=<seller-and-stable-hash>.json
```

Prices for different dates, occupancy, exact child ages, rooms, or sellers
coexist. Any valid price change for the same context passes immediately.
Invalid, stale, or unconfirmed data is quarantined and never removes the last
good current price.

For all verified master hotels, use the registry and batch commands documented
in `docs/airflow-hotel-prices.md`. The batch uses MCP discovery, not fabricated
Trivago IDs, and can be bounded with `--max-requests`.

## Seed verified master places before Google crawling

The five verified master files can become the initial current-place read model
without a Google mapping, crawl, LLM, or human-review step:

```powershell
python -m nextrip_pipeline.cli bootstrap-master-places
```

The command reads attraction, cafe, hotel, nightlife, and restaurant master
files. Known fields are copied with `verified-master-data` provenance; fields
not present in master remain `null`. General master opening hours are retained
separately and are not presented as a date-specific `open_now` observation.
This is a seed-only operation: an existing accepted Google snapshot is never
overwritten.

## Capture Google Maps place evidence

The operational mapping `config/google-maps-mapping.json` currently targets
`attr_qn_001` (Eo Gió). Add further places to the mapping registry before
enabling the complete daily batch.

```powershell
python -m nextrip_pipeline.cli crawl-google-maps `
  --mapping config/google-maps-mapping.json
```

Successful records are written below `data/raw`. CAPTCHA and browser errors
are not bypassed; diagnostic HTML and screenshots are written below
`data/crawl_artifacts`.

Run the complete Google Maps branch with:

```powershell
python -m nextrip_pipeline.cli refresh-google-place `
  --mapping config/google-maps-mapping.json
```

The Playwright adapter opens the selected place detail page and captures the
canonical URL, name, category, address, phone, website, coordinates, current
business status, weekly hours, Google price level, menu URL, and up to 20 image
references. Raw HTML and structured DOM evidence are retained together.

Google price level is only a spending indicator, not an exact menu price. For
restaurants, cafes, and nightlife venues, `MenuSourceObservation` passes the
Google menu URL to the separate official-menu pipeline. Exact item prices must
be parsed from that linked HTML/PDF/image and stored as `MenuObservation` and
`MenuItem`; they must never be inferred from the Google price level.

Confirmed mappings with valid identity, coordinates, provenance, and freshness
produce a `PASS` decision. Auto-matched mappings stop at `REVIEW`. A `PASS`
observation can replace the corresponding verified-master baseline in
`data/current/place`; missing crawl fields retain explicit master fallbacks.
The configured cadences are daily status, weekly details, menu every 14 days,
and media every 30 days. Google crawling remains disabled while the verified
master baseline is used directly.

Before enabling scheduled production crawls, review each site's current terms,
robots policy, and the acceptable request rate.
## Menu image OCR (Google Maps)

The menu refresh is independent from the daily opening-status crawl. It uses the
confirmed `attributes.verified_menu_image_url` in the Google Maps mapping, requests
the original image at a higher resolution, stores the image and its immutable raw
envelope, then runs free local RapidOCR/ONNX inference. OCR output is cached by the
image SHA-256 and OCR engine version.

Install the optional local runtime once:

```powershell
python -m pip install -e ".[menu-ocr]"
```

Run one place:

```powershell
python -m nextrip_pipeline.cli refresh-place-menu `
  --mapping config/google-maps-mapping-cafe-dn-062.json
```

Outputs are partitioned under `data/raw/entity=menu`,
`data/normalized/entity=menu`, `data/validation/entity=menu`, and
`data/decisions/entity=menu`. OCR menus intentionally receive a `review` decision
until their names and prices have been semantically or human verified; they must not
be auto-published to Neo4j from this stage.

The normalized KB contract deliberately contains no OCR coordinates or crawl
metadata:

```json
{
  "place_id": "cafe_dn_062",
  "items": [
    {
      "name": "Cafe sữa Sài Gòn",
      "section": "Cafe",
      "currency": "VND",
      "amount": 22000
    }
  ]
}
```

### Human menu review without a chatbot UI

Every `review` decision automatically creates one immutable task under
`data/review/menu/pending`. The queue deduplicates tasks by `place_id` and menu image
hash, so an unchanged image is not reviewed twice.

List pending tasks:

```powershell
python -m nextrip_pipeline.cli list-menu-reviews
```

Export one candidate to an editable JSON file:

```powershell
python -m nextrip_pipeline.cli export-menu-review `
  --review-id menu-cafe_dn_062-IMAGE_HASH_PREFIX `
  --output reviewed-menu.json
```

After correcting names, sections, and prices, approve and publish the current menu:

```powershell
python -m nextrip_pipeline.cli approve-menu `
  --review-id menu-cafe_dn_062-IMAGE_HASH_PREFIX `
  --input reviewed-menu.json `
  --reviewer oanh
```

Reject unusable evidence:

```powershell
python -m nextrip_pipeline.cli reject-menu `
  --review-id menu-cafe_dn_062-IMAGE_HASH_PREFIX `
  --reviewer oanh `
  --reason "Ảnh menu quá mờ"
```

Pending tasks remain immutable for audit. Approvals and rejections are written as
separate resolution files. Only an approved menu can update
`data/current/menu/{place_id}.json`; this current projection is the input for the
future Neo4j publisher.
