from __future__ import annotations

from typing import Any

from .gemini_client import GeminiClient
from .neo4j_store import Neo4jGraphStore
from .normalizer import canonical_city


SYSTEM_INSTRUCTION = """
Bạn là trợ lý du lịch cho chatbot NexTrip, chuyên tư vấn Quy Nhơn và Đà Nẵng.
Chỉ dùng thông tin trong ngữ cảnh GraphRAG được cung cấp. Nếu dữ liệu không đủ,
nói rõ phần nào chưa có dữ liệu. Trả lời bằng tiếng Việt, ưu tiên gợi ý thực tế:
địa điểm, lý do phù hợp, khoảng giá/giờ mở cửa nếu có, và nguồn dữ liệu khi hữu ích.
""".strip()


class TravelGraphRAG:
    def __init__(self, store: Neo4jGraphStore, gemini: GeminiClient):
        self.store = store
        self.gemini = gemini

    def answer(
        self,
        question: str,
        city: str | None = None,
        entity_types: list[str] | None = None,
        top_k: int = 8,
    ) -> str:
        city_id = None
        if city:
            city_name = canonical_city(city)
            city_id = "city_quy_nhon" if city_name == "Quy Nhơn" else "city_da_nang"

        try:
            query_embedding = self.gemini.embed_query(question)
            results = self.store.vector_search(
                embedding=query_embedding,
                limit=top_k,
                city_id=city_id,
                entity_types=entity_types,
            )
        except Exception:
            results = []

        if not results:
            fallback_results = self.store.keyword_search(
                query_text=question,
                limit=top_k,
                city_id=city_id,
                entity_types=entity_types,
            )
            results = fallback_results

        context = self.build_context(results)
        prompt = f"""
Câu hỏi của người dùng:
{question}

Ngữ cảnh lấy từ Neo4j GraphRAG:
{context}

Hãy trả lời như một tư vấn viên du lịch. Nếu có nhiều lựa chọn, nhóm theo nhu cầu
và ưu tiên các địa điểm khớp nhất với câu hỏi.
""".strip()
        return self.gemini.generate(SYSTEM_INSTRUCTION, prompt)

    @staticmethod
    def build_context(results: list[dict[str, Any]]) -> str:
        if not results:
            return "Không tìm thấy địa điểm phù hợp trong graph."

        blocks: list[str] = []
        for index, row in enumerate(results, start=1):
            place = row["place"]
            nearby_names = [
                item.get("name") for item in row.get("nearby", []) if item.get("name")
            ]
            lines = [
                f"[{index}] {place.get('name')} ({place.get('entity_label')}, {place.get('category_name')})",
                f"Thành phố: {place.get('city')}",
                f"Điểm liên quan: {row.get('score'):.4f}" if row.get("score") is not None else None,
                f"Địa chỉ: {place.get('address')}" if place.get("address") else None,
                f"Tọa độ: {place.get('lat')}, {place.get('lng')}",
                f"Rating: {place.get('rating')} ({place.get('review_count')} đánh giá)"
                if place.get("rating")
                else None,
                f"Giờ mở cửa: {place.get('opening_hours_open')} - {place.get('opening_hours_close')}"
                if place.get("opening_hours_open") or place.get("opening_hours_close")
                else None,
                f"Khoảng giá: {place.get('price_range')}" if place.get("price_range") else None,
                f"Mô tả: {place.get('description')}",
                f"Facet: {', '.join(row.get('facets') or [])}" if row.get("facets") else None,
                f"Gần: {', '.join(nearby_names)}" if nearby_names else None,
                f"Nguồn: {place.get('source_name')} - {place.get('source_url')}"
                if place.get("source_url")
                else None,
            ]
            blocks.append("\n".join(line for line in lines if line))
        return "\n\n".join(blocks)
