# NexTripAI-KB

Knowledge Base and GraphRAG repo for NexTripAI. This repo owns travel data collection, cleaning, entity/relationship modeling, vector indexing, graph construction, and retrieval evaluation.

## Main Responsibilities

- Collect travel data for target destinations.
- Normalize data for places, hotels, restaurants, activities, prices, ratings, opening hours, and locations.
- Extract entities and relationships for graph-based retrieval.
- Build embeddings and vector indexes.
- Build or export graph data for GraphRAG.
- Provide retrieval APIs or packaged indexes for the backend.
- Maintain evaluation sets for factual accuracy and retrieval quality.

## Suggested Tech Stack

- Python
- PostgreSQL for structured metadata
- FAISS or Qdrant for vector search
- Neo4j or PostgreSQL graph-style tables for graph relations
- multilingual-e5-large or another Vietnamese-friendly embedding model
- Docker

## Suggested Structure

```txt
NexTripAI-KB/
  data/
    raw/
    processed/
    exports/
  notebooks/
  src/
    ingestion/
    cleaning/
    extraction/
    indexing/
    retrieval/
    evaluation/
  tests/
  .env.example
  requirements.txt
  README.md
```

## Data Scope

Initial destinations should follow the project proposal and team decision. Keep the first version small and high quality before expanding.

Suggested minimum data types:

- Attractions and landmarks
- Hotels and homestays
- Restaurants and cafes
- Activities and tours
- Weather-sensitive tags, such as indoor, outdoor, beach, culture, family, budget, luxury
- Coordinates and area relationships

## GraphRAG Direction

The KB should support hybrid retrieval:

```txt
User query
-> intent/entities from backend
-> vector search
-> graph expansion
-> reranking
-> evidence package
-> backend agent response
```

Example relationships:

- place `LOCATED_IN` destination/area
- restaurant `NEAR` attraction
- activity `SUITABLE_FOR` interest/travel style
- place `HAS_CONSTRAINT` opening hours, price, weather condition
- hotel `NEAR` area/attraction

## Output Contract For Backend

The backend should receive grounded retrieval results, not only plain text.

Recommended fields:

- `id`
- `name`
- `type`
- `description`
- `location`
- `price_range`
- `rating`
- `tags`
- `relationships`
- `source_url`
- `evidence_text`
- `confidence_score`

## Development Notes

- Track data source and crawl/update time for every item.
- Prefer quality and verifiability over large noisy data.
- Keep a small golden test set for retrieval and factual accuracy.
