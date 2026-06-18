from __future__ import annotations

from typing import Iterable

from .config import Settings


class GeminiClient:
    def __init__(self, settings: Settings):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "Missing google-genai. Install dependencies with: pip install -r requirements.txt"
            ) from exc

        if not settings.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY or GEMINI_API_KEY is required for Gemini calls.")

        self.settings = settings
        self.types = types
        self.client = genai.Client(api_key=settings.google_api_key)

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        contents = [
            "Document for Vietnamese travel recommendation retrieval:\n" + text
            for text in texts
        ]
        return self._embed(contents)

    def embed_query(self, query: str) -> list[float]:
        contents = ["Query for Vietnamese travel recommendation retrieval:\n" + query]
        return self._embed(contents)[0]

    def _embed(self, contents: list[str]) -> list[list[float]]:
        config = self.types.EmbedContentConfig(
            output_dimensionality=self.settings.embedding_dim
        )
        response = self.client.models.embed_content(
            model=self.settings.embedding_model,
            contents=contents,
            config=config,
        )
        return [list(embedding.values) for embedding in response.embeddings]

    def generate(self, system_instruction: str, prompt: str) -> str:
        config = self.types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.settings.temperature,
        )
        response = self.client.models.generate_content(
            model=self.settings.gemini_model,
            contents=prompt,
            config=config,
        )
        return (response.text or "").strip()
