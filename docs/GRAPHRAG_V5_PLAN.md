# GraphRAG V5 Plan: Typed Targets, Geo Hierarchy, and Reliable Retrieval

## 1. Mục tiêu

V5 nâng V4 từ graph chủ yếu xoay quanh `Place` thành knowledge graph có thể
truy vấn đúng loại thực thể và đúng phạm vi địa lý. V5 vẫn dùng duy nhất
`travel_data_verified` làm nguồn địa điểm gốc, nhưng không được biến mọi cụm từ
trong testcase thành dữ liệu nếu chưa có evidence.

V5 tập trung vào:

- phân biệt City, GeoArea, Place, Dish, Activity, Amenity và Community;
- truy vấn địa bàn như Tuy Phước, Nhơn Lý mà không nhầm với City;
- trả đúng target type, ví dụ hỏi món ăn phải trả Dish thay vì Place ngẫu nhiên;
- hỗ trợ multi-hop có provenance;
- phân biệt câu không có dữ liệu, câu cần external tool và lỗi planner/runtime;
- cung cấp candidates/evidence tốt cho BE planning, không tự nhận làm toàn bộ L4/L5.

## 2. Baseline V4 đã đóng mốc

Commit baseline: `a67540a`.

V4 hiện có:

- 519 Place, 8.269 Fact, 2.871 TextUnit;
- 282 Concept, 2.420 Claim, 2.369 ontology edges;
- 401 Offering, 10 Community và 10 CommunityReport;
- 519 Place embeddings;
- graph invariants hiện tại pass.

Điểm mạnh:

- entity detail theo tên và typed fact;
- count/list theo hai City;
- recommendation Place theo concept, rating và popularity;
- provenance cho Fact/Claim;
- multi-entity lookup giữ đủ subject;
- hard constraint cơ bản và grounded answer context.

## 3. Findings từ data, graph và benchmark

### 3.1 Data có nhưng graph chưa biểu diễn đúng

- Tuy Phước xuất hiện trong 2 attraction thực sự nằm tại địa bàn và 1 restaurant
  chỉ nhắc đến nguồn hải sản Tuy Phước.
- Nhơn Lý xuất hiện trong tên, địa chỉ hoặc mô tả của nhiều attraction,
  restaurant và nightlife.
- Neo4j V4 chưa có label `GeoArea`, dù schema đã có `ConceptType.GEO_AREA`.
- Trường `district` chỉ có 2/519 bản ghi và một giá trị chưa sạch; không thể dùng
  trực tiếp làm source of truth cho toàn bộ geo hierarchy.

V5 phải phân biệt:

```text
(Place)-[:LOCATED_IN]->(GeoArea)
(Document|TextUnit)-[:MENTIONS_GEO_AREA]->(GeoArea)
(Place)-[:NEAR {distance_km}]->(Place)
(GeoArea)-[:PART_OF]->(GeoArea|City)
```

Mention trong description không được tự động nâng thành `LOCATED_IN`.

### 3.2 Target type còn quá hẹp

V4 chỉ trả `EntityResult` dạng Place cho hầu hết query. Benchmark còn hỏi trực
tiếp về:

- Dish/cuisine và đặc sản;
- Activity/Experience;
- City/GeoArea profile;
- Event/Festival và mùa du lịch;
- Tour;
- shopping/service POI;
- transport hub/route;
- climate/weather information.

Hiện graph có 195 Dish nhưng retrieval chưa trả Dish làm target. Activity chỉ có
4 node; Cuisine có 4 node; community summary hiện chỉ là câu chứa số lượng Place.
Không có GeoArea, Event, Tour, Service, TransportHub hoặc ClimateProfile hoàn chỉnh.

### 3.3 False positive nguy hiểm

Query so sánh Đà Nẵng và Quy Nhơn hiện anchor nhầm thành Place có tên gần giống:
`Đà Nẵng Corner` và `Seagull Quy Nhơn`. Có kết quả nhưng sai target type nguy hiểm
hơn trả `unsupported`.

Các failure khác đã quan sát:

- `Khách sạn gần bãi biển Mỹ Khê`: planner đặt `near_subject` sai vào predicate;
- `Tuy Phước/Nhơn Lý có gì đặc biệt`: planner hiểu community search nhưng schema
  reject vì chỉ có hai City;
- `Món đặc sản Đà Nẵng`: trả Place thay vì Dish;
- `Lịch sử thành phố`: community query thiếu entity type nên bị reject;
- `Khách sạn 4 sao, gần biển, dưới 2 triệu`: `biển` không phải named Place nên
  anchor thất bại;
- `trời mưa`: model có thể sinh weather value ngoài enum;
- itinerary: KB chưa trả planning candidate bundle có duration/geo grouping.
- `cho tôi biết thêm về <địa điểm>`: đây là entity profile lookup; predicates
  rỗng phải mang nghĩa lấy profile fact whitelist, không phải invalid plan.

### 3.4 Reliability và system boundary

Probe đồng thời 5 planner call đã gặp Vertex AI `429 RESOURCE_EXHAUSTED`. V4 đang
biến lỗi này thành plan `unsupported`, làm sai nghĩa lỗi.

Bộ 500 testcase là benchmark toàn hệ thống:

| Level | Owner chính | Vai trò KB V5 |
|---|---|---|
| L1 | KB + external tools | Fact/entity retrieval; weather/route động chuyển tool |
| L2 | KB | Typed recommendation, comparison, summary |
| L3 | KB + BE | KB lọc candidates; BE hợp nhất user constraints |
| L4 | BE Planning Agent | KB trả candidates, facts, duration và geo context |
| L5 | BE LangGraph memory | KB chỉ phục vụ retrieval từng turn |

Không dùng pass rate 500 câu để đánh giá riêng KB.

## 4. Ontology V5

### 4.1 Geographic subgraph

```text
(TravelCatalog)-[:HAS_CITY]->(City)
(City)-[:HAS_AREA]->(GeoArea)
(GeoArea)-[:PART_OF]->(City|GeoArea)
(Place)-[:LOCATED_IN {confidence, evidence_id}]->(City|GeoArea)
(TextUnit)-[:MENTIONS_GEO_AREA]->(GeoArea)
(Place)-[:NEAR {distance_km, method}]->(Place)
```

`GeoArea.area_type` dùng enum như district, ward, commune, locality, beach_area.
Mỗi node có aliases, normalized name, confidence và provenance. Raw address vẫn
được giữ nguyên trên Place/Fact.

Geo extraction dùng ba tầng:

1. structured field đã verified;
2. address component extraction có rule và confidence;
3. LLM extraction từ description chỉ tạo mention trước, không tự tạo location.

### 4.2 Retrievable target subgraphs

Các node đã có nhưng phải trở thành first-class retrieval target:

```text
Place, City, GeoArea, Dish, Cuisine, Activity, Experience, Amenity,
Audience, WeatherCondition, TimeWindow, Community, CommunityReport
```

Các node chỉ được thêm khi có nguồn dữ liệu:

```text
Tour, Event, Festival, ServicePOI, Shop, TransportHub, ClimateProfile
```

Không sinh các node này chỉ vì testcase có nhắc đến.

Quan hệ ưu tiên:

```text
(Place|Offering)-[:SERVES_DISH]->(Dish)
(Place)-[:PROVIDES_ACTIVITY]->(Activity)
(Place)-[:HAS_AMENITY]->(Amenity)
(Place)-[:SUITABLE_FOR]->(Audience)
(Place)-[:ACCESSIBLE_FOR]->(AccessibilityFeature)
(Place)-[:BEST_DURING]->(TimeWindow|Season)
(Event)-[:HELD_AT]->(Place|GeoArea|City)
(Tour)-[:VISITS]->(Place|GeoArea)
```

Mọi promoted relationship phải trỏ về Claim/TextUnit evidence.

### 4.3 Community and summary

Thay taxonomy community `city × entity_type` đơn giản bằng community nhiều cấp:

- geo community theo City/GeoArea;
- thematic community theo concept/path;
- report có summary, key entities, key relationships, evidence IDs và level;
- không tạo summary kiểu `contains N places` làm answer context chính.

V5 thử ba query mode để so sánh:

- Local Search cho entity/fact cụ thể;
- Global Search cho city/area/theme summary;
- DRIFT-style search cho câu rộng rồi mở rộng sang local entities.

## 5. Query Plan V5

Gemini chỉ tạo typed plan, không tạo raw Cypher.

```json
{
  "intent": "lookup | profile | list | recommend | compare | summarize | aggregate | plan_candidates | tool_required",
  "targets": [
    {"kind": "place | city | geo_area | dish | activity | concept", "value": null}
  ],
  "geo_scope": {
    "cities": [],
    "areas": [],
    "near_entities": []
  },
  "requested_fields": [],
  "required_concepts": [],
  "preferred_concepts": [],
  "ranking_criteria": [],
  "constraints": [],
  "duration_days": null,
  "limit": 5,
  "required_tools": [],
  "clarification_needed": false
}
```

Compiler thực hiện semantic validation theo graph vocabulary và target label.
Entity resolver phải resolve City thành City, GeoArea thành GeoArea và Place thành
Place; không fallback chéo label nếu chưa được plan cho phép.

`profile` dùng projection whitelist theo target type. Ví dụ restaurant ưu tiên
description, address, cuisine, signature dishes, opening hours, rating, price,
ambience, serves và suitability; không trả toàn bộ fact kỹ thuật.

## 6. Retrieval pipeline V5

```text
Query
  -> Gemini typed planner
  -> semantic validator / repair
  -> typed entity resolver
  -> retriever router
     -> exact fact / aggregate
     -> geo subgraph
     -> concept target
     -> hybrid Place retrieval
     -> community/global retrieval
     -> tool-required response
  -> graph traversal and constraint verification
  -> evidence assembly
  -> typed KB response
```

Retriever implementations cần tách riêng:

- `EntityResolver`: exact alias, fulltext, vector, label guard;
- `FactRetriever`: structured predicate projection;
- `GeoRetriever`: `LOCATED_IN/PART_OF` traversal;
- `ConceptRetriever`: Dish/Activity/Amenity làm target;
- `HybridPlaceRetriever`: vector + fulltext + graph filters;
- `CommunityRetriever`: area/theme reports;
- `ComparisonRetriever`: cùng target type và comparable facts;
- `PlanningCandidateRetriever`: diverse candidates kèm duration/geo/cost evidence.

## 7. Planner/runtime reliability

- giới hạn concurrency riêng cho Gemini planner;
- truncated exponential backoff cho 429/5xx;
- cache query plan theo normalized query + graph manifest;
- trả lỗi `planner_unavailable` có retryable flag, không đổi thành `unsupported`;
- trace riêng planner latency, retry count, resolver, traversal và rerank;
- unknown field được report rõ; không broad-search đoán mò.

## 8. Kết nối BE/FE

BE bắt đầu cần LangGraph khi triển khai L4/L5:

```text
intent
  -> KB retrieval
  -> weather/route tools khi required
  -> planning
  -> grounded answer
```

- L4: BE tạo timeline; KB chỉ trả planning candidates.
- L5: dùng `session_id` làm thread ID và local SQLite checkpointer trước.
- Queue hiện tại tiếp tục bảo vệ concurrency của request.
- FE hiển thị `unsupported`, `missing_data`, `tool_required` và
  `service_temporarily_unavailable` thành các state khác nhau.

## 9. Benchmark V5

### 9.1 Chuẩn hóa benchmark trước khi tính pass rate

Mỗi testcase cần thêm:

- owner: KB, BE, FE hoặc external tool;
- required capability;
- expected target type;
- canonical entity IDs/facts hoặc acceptance rule;
- data availability: present, partial, absent, dynamic;
- expected behavior khi dữ liệu thiếu.

### 9.2 Acceptance gates

Graph:

- 519 Place không bị mất và V4 rebuild độc lập;
- mọi Place thuộc một City và ít nhất một geographic scope;
- location edge khác mention edge;
- mọi promoted edge có evidence/confidence;
- City/GeoArea/Dish queries resolve đúng label.

Retrieval:

- target-type accuracy >= 0.95;
- named entity Hit@1 >= 0.95;
- hard-constraint violation = 0;
- area-location precision >= 0.95;
- returned fact/evidence coverage = 1.0;
- city comparison không trả Place anchor giả.
- profile query trả đúng entity và các fact hữu ích có evidence;

Reliability:

- planner valid-plan rate >= 0.98 trên KB-owned benchmark;
- 429 được retry/backoff và không hiển thị là unsupported;
- trace phân biệt no-data, unsupported, tool-required và service error.

## 10. Thứ tự triển khai

1. Tag ownership và data availability cho 500 testcase.
2. Tạo `docker-compose.v5.yml`: Neo4j V5 tại HTTP 7478, Bolt 7691, volume riêng.
3. Tạo V5 manifest, schemas và graph invariants; không sửa graph V4.
4. Xây geo extraction audit, review output trước khi load.
5. Build GeoArea hierarchy và location/mention provenance.
6. Nâng Dish/Activity/Amenity thành retrievable targets.
7. Build grounded multi-level communities/reports.
8. Implement V5 planner schema, semantic repair và typed entity resolver.
9. Implement retriever router và whitelisted Cypher templates.
10. Thêm planner limiter, retry/backoff, cache và error taxonomy.
11. Chạy V4/V5 cùng benchmark KB-owned và viết failure report.
12. Wire BE bằng `kb_version=v5`; sau đó mới triển khai LangGraph planning/memory.

V5 chỉ được đổi từ `experimental` sang `baseline` khi đạt graph invariants,
target-type accuracy và reliability gates ở trên.

## 11. Nguồn kỹ thuật tham khảo

- Microsoft GraphRAG indexing: entity, relationship, claim extraction và
  multi-level community reports.
- Microsoft GraphRAG query engine: Local, Global và DRIFT Search.
- Neo4j GraphRAG retrievers: vector, hybrid, VectorCypher và retriever routing.
- Vertex AI 429 guidance: traffic smoothing và truncated exponential backoff.
- LangGraph persistence: checkpointer cho thread-scoped state, store cho
  long-term memory.
