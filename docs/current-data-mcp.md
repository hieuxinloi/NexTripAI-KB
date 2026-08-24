# Current Data HTTP and MCP facade

`nextrip_current` is the runtime boundary for data that should not be read
directly from Neo4j: canonical place projections, contextual hotel offers, and
short-lived traffic results. The HTTP API and MCP tools share one
`CurrentDataService`, so they cannot apply different freshness or identity
rules.

## HTTP API

Configure the internal credentials and traffic service, then start the API on
a port separate from the traffic API:

```powershell
$env:NEXTRIP_KB_ROOT = (Get-Location).Path
$env:NEXTRIP_CANONICAL_DATASET = "data/canonical/datasets/dataset=<dataset-id>/canonical-active-dataset.json"
$env:CURRENT_DATA_API_KEY = "replace-with-a-long-random-secret"
$env:CURRENT_DATA_TRAFFIC_API_URL = "http://127.0.0.1:8010"
$env:TRAFFIC_API_KEY = "the-key-used-by-the-traffic-service"
$env:CURRENT_DATA_TRIVAGO_REFRESH_ENABLED = "true"
python -m uvicorn nextrip_current.api:app --host 127.0.0.1 --port 8020
```

`NEXTRIP_CANONICAL_DATASET` is mandatory and is the sole authority for place
identity and static facts. The HTTP/MCP service does not accept a legacy place
root and fails closed when canonical loading fails. Hotel price and
availability directories remain append-only operational observation stores
keyed to canonical `place_id` values.

Public data endpoints require `X-NexTrip-Current-Key`; `/health` and `/ready`
remain unauthenticated for local health checks. The available operations are:

- `GET /api/current/places/{place_id}`
- `POST /api/current/places/batch`
- `POST /api/current/hotel-offers/search`
- `POST /api/current/hotel-availability/search`
- `POST /api/current/traffic/routes`
- `POST /api/current/traffic/recommendations`

Hotel offers are matched by exact check-in, check-out, adults, children,
rooms, child ages, currency, and optional seller. Stale offers are hidden by
default. To fill a missing or stale context, set `refresh_if_missing=true` and
send exactly one `hotel_id`; the service then executes one bounded Trivago run
through raw storage, mapping, normalization, validation, decision, and current
publication before reading the result again. For duration and fallback logic,
use `hotel-availability/search`; `hotel-offers/search` remains an exact-context
price lookup for backward compatibility.

```powershell
$headers = @{ "X-NexTrip-Current-Key" = $env:CURRENT_DATA_API_KEY }
$body = @{
  hotel_ids = @("hotel_qn_025")
  check_in = "2026-08-21"
  stay_nights = 2
  lookahead_days = 1
  occupancy = @{ adults = 2; children = 0; rooms = 1 }
  children_ages = @()
  currency = "VND"
  include_stale = $false
  refresh_if_missing = $true
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8020/api/current/hotel-availability/search" `
  -Method Post `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

The duration can be supplied in any one of these equivalent forms:

- `check_out`: exclusive checkout date;
- `stay_nights`: billable hotel nights;
- `stay_days`: inclusive calendar days (`3 days = 2 nights`).

When all are omitted, the request is one night. If several are supplied they
must agree. `lookahead_days` defaults to `1` and is bounded to `0..14`. Each
fallback shifts the complete stay interval; a two-night `24–26 Aug` request
becomes `25–27 Aug`, never two unrelated one-night prices.

The crawler advances to a later check-in only after fresh, confirmed
`unavailable` evidence. An unresolved mapping, missing price field, provider
omission, parser error, or network error is `unknown` and stops fallback. The
response separates:

- `lookup_status`: whether the current projection is fresh, stale, or missing;
- `availability`: whether room inventory is available, unavailable, or unknown.

If the requested date is unavailable and the next date has a valid price, both
windows remain in the response and `selected_window_index` points to the later
available window. Current availability and price are stored by exact stay,
occupancy, child ages, currency, and source with a five-hour TTL.

The availability HTTP/MCP contract defaults `refresh_if_missing=true`; set it
to `false` for a cache-only read. Runtime crawling remains fail-closed until
`CURRENT_DATA_TRIVAGO_REFRESH_ENABLED=true` is configured. The older exact
offer lookup keeps its cache-only default for backward compatibility.

The stable canonical `hotel_id` never changes. A confirmed Trivago mapping can
change only the exposed `display_name`; the response keeps `master_name`,
aliases, mapping ID, Trivago external ID/URL, and provenance. A REVIEW or
REJECTED candidate cannot rename a hotel.

`nextrip_current.mcp` exposes the same current-data service used by the HTTP
API. It is intentionally a thin protocol adapter: registry lookup, hotel-price
context, routing, transport ranking, validation, and error semantics remain in
`CurrentDataService`.

## Install and run

The SDK is optional so importing `nextrip_current` and serving its HTTP API do
not require MCP:

```console
pip install -e ".[mcp]"
nextrip-current-mcp
```

The default transport is `stdio`. Streamable HTTP is also selectable:

```console
nextrip-current-mcp --transport streamable-http
```

The CLI lazily calls
`nextrip_current.runtime.build_current_data_service()`, runs one MCP server,
and closes the service when the server stops. Do not write application logs to
stdout when using `stdio`, because stdout carries protocol messages.

## Tool contract

| MCP tool | Input | Service delegation |
| --- | --- | --- |
| `get_current_place` | `place_id: string` | `get_place(place_id)` |
| `get_current_places` | `place_ids: string[]` | `get_places(PlaceBatchRequest(...))` |
| `search_current_hotel_offers` | `request: object` | `search_hotel_offers(HotelOfferSearchRequest.model_validate(request))` |
| `search_hotel_availability` | `request: object` | `search_hotel_availability(HotelOfferSearchRequest.model_validate(request))` |
| `calculate_route` | `payload: object` | `route(TrafficRouteRequest.model_validate(payload))` |
| `recommend_transport` | `payload: object` | `recommend_transport(TransportRecommendationRequest.model_validate(payload))` |

Request dictionaries are validated with the same strict Pydantic contracts as
the HTTP boundary before delegation. This includes hotel
`refresh_if_missing`, exact occupancy/child ages, routing endpoints, and
transport constraints. Mapping responses are returned unchanged, Pydantic
responses use `model_dump(mode="json")`, and a bare service sequence is
represented as `{ "result": [...] }` so MCP structured content remains a JSON
object. Service exceptions are not translated by the facade; the MCP SDK
reports them as tool-call errors with the same application-level meaning.

For embedding or tests, construct the server with an already configured
service:

```python
from nextrip_current.mcp import create_mcp_server

server = create_mcp_server(current_data_service)
```

## SDK and deployment notes

This facade targets the stable v2 API documented in the official
[MCP Python SDK v2 notes](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md):
`from mcp.server import MCPServer`, `@server.tool()`, and `server.run(...)`.
The SDK's [client documentation](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/index.md)
also supports passing an in-memory `MCPServer` to a client for integration
tests. Streamable HTTP is preferred over legacy SSE according to the official
[running guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/index.md).

Selecting Streamable HTTP only starts the SDK transport. Authentication,
authorization, TLS, rate limits, and public ingress must be supplied by the
deployment layer before exposing it outside a trusted network; see the SDK's
[deployment guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/deploy.md).
