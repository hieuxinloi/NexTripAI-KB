# NexTrip GraphRAG

Hệ thống GraphRAG cho chatbot du lịch Quy Nhơn và Đà Nẵng, dùng Gemini để tạo embedding/trả lời và Neo4j để lưu knowledge graph.

## Dữ liệu

Thư mục `travel_data/` hiện có 524 địa điểm:

- `attraction`: 118
- `restaurant`: 188
- `hotel`: 73
- `cafe`: 106
- `nightlife`: 39

Hai node chính là `City`: `Quy Nhơn` và `Đà Nẵng`. Mỗi địa điểm được chuẩn hóa thành `Place` và gắn thêm label phụ như `Attraction`, `Restaurant`, `Hotel`, `Cafe`, `Nightlife`.

## Graph Schema

```text
(:City)-[:HAS_PLACE]->(:Place)
(:Place)-[:IN_CITY]->(:City)
(:City)-[:HAS_PLACE_TYPE]->(:PlaceType)-[:CONTAINS_PLACE]->(:Place)
(:Place)-[:HAS_TYPE]->(:PlaceType)
(:Place)-[:HAS_CATEGORY]->(:Category)
(:Place)-[:TAGGED_WITH|HAS_AMENITY|HAS_FEATURE|HAS_CUISINE|SERVES|SUITABLE_FOR]->(:Term)
(:Place)-[:FROM_SOURCE]->(:Source)
(:Place)-[:NEAR {distance_km}]->(:Place)
```

`Place` lưu các property đã flatten để Neo4j dùng được trực tiếp: `name`, `city`, `entity_type`, `category_name`, `description`, `address`, `lat`, `lng`, `opening_hours_open`, `opening_hours_close`, `rating`, `review_count`, `search_text`, `embedding`, ...

## Cài đặt

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Cập nhật `.env`:

```dotenv
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
GOOGLE_API_KEY=your-gemini-api-key
```

Chạy Neo4j local nếu cần:

```powershell
docker run --name nextrip-neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/your-password neo4j:5
```

## Chuẩn hóa dữ liệu

```powershell
python -m nextrip_graphrag prepare --data-dir travel_data --out-dir processed
```

Output:

- `processed/cities.json`
- `processed/places.jsonl`
- `processed/manifest.json`

## Nạp Neo4j

Tạo constraint/index:

```powershell
python -m nextrip_graphrag schema
```

Nạp graph kèm Gemini embeddings:

```powershell
python -m nextrip_graphrag load --processed-dir processed --with-embeddings
```

Nếu chỉ muốn kiểm tra graph không gọi Gemini:

```powershell
python -m nextrip_graphrag load --processed-dir processed
```

## Hỏi thử chatbot

```powershell
python -m nextrip_graphrag ask "Gợi ý 3 quán hải sản ở Đà Nẵng phù hợp đi gia đình" --city "Đà Nẵng" --type restaurant
python -m nextrip_graphrag ask "Ở Quy Nhơn nên đi biển nào buổi sáng?" --city "Quy Nhơn" --type attraction
python -m nextrip_graphrag ask "Tôi cần khách sạn có hồ bơi gần biển ở Quy Nhơn" --city "Quy Nhơn" --type hotel
```

## Ghi chú kỹ thuật

- Gemini SDK dùng package `google-genai`.
- Embedding mặc định: `gemini-embedding-001`, ép số chiều bằng `GEMINI_EMBEDDING_DIM=1536` để Neo4j vector index ổn định.
- Neo4j dùng `CREATE VECTOR INDEX` và `db.index.vector.queryNodes` cho semantic retrieval, sau đó mở rộng ngữ cảnh qua quan hệ graph.
