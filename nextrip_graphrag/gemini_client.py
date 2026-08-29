from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Iterable, TypeVar

from loguru import logger
from pydantic import BaseModel

from .config import Settings
from .enrichment.io import cache_path, read_json, write_json


StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class GeminiClient:
    def __init__(self, settings: Settings):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "Missing google-genai. Install dependencies with: pip install -r requirements.txt"
            ) from exc

        self.settings = settings
        if not settings.gemini_planner_model:
            raise RuntimeError("GEMINI_PLANNER_MODEL is required.")
        self.types = types
        self._query_embedding_lock = Lock()
        self._query_embedding_cache = (
            Path(settings.query_embedding_cache)
            / f"{settings.embedding_model.replace('/', '_')}-{settings.embedding_dim}"
        )
        self.client = self._create_client(genai)
        self.structured_client = self._create_client(
            genai,
            timeout_ms=settings.structured_gemini_timeout_ms,
            retry_attempts=settings.structured_gemini_retry_attempts,
        )

    def close(self) -> None:
        self.client.close()
        self.structured_client.close()

    def _create_client(
        self,
        genai,
        *,
        timeout_ms: int | None = None,
        retry_attempts: int | None = None,
    ):
        http_options = self.types.HttpOptions(
            timeout=timeout_ms or self.settings.gemini_timeout_ms,
            retry_options=self.types.HttpRetryOptions(
                attempts=retry_attempts or self.settings.gemini_retry_attempts,
                initial_delay=1,
                max_delay=8,
                exp_base=2,
                jitter=1,
                http_status_codes=[429, 500, 502, 503, 504],
            ),
        )
        if not self.settings.google_api_key:
            raise RuntimeError(
                "Configure Gemini Developer API authentication with "
                "GOOGLE_API_KEY or GEMINI_API_KEY."
            )
        return genai.Client(
            api_key=self.settings.google_api_key,
            http_options=http_options,
        )

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        return self._embed(list(texts), task_type="RETRIEVAL_DOCUMENT")

    def embed_query(self, query: str) -> list[float]:
        path = cache_path(self._query_embedding_cache, f"query|{query}")
        with self._query_embedding_lock:
            if path.exists():
                return list(read_json(path)["embedding"])
            embedding = self._embed(
                [query],
                task_type="RETRIEVAL_QUERY",
                client=self.structured_client,
            )[0]
            write_json(path, {"embedding": embedding})
            return embedding

    def _embed(
        self,
        contents: list[str],
        task_type: str,
        *,
        client=None,
    ) -> list[list[float]]:
        config = self.types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self.settings.embedding_dim,
        )
        active_client = self.client if client is None else client
        response = active_client.models.embed_content(
            model=self.settings.embedding_model,
            contents=contents,
            config=config,
        )
        return [list(embedding.values) for embedding in response.embeddings]

    def generate(self, system_instruction: str, prompt: str) -> str:
        config = self.types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.settings.temperature,
            thinking_config=self.types.ThinkingConfig(
                thinking_level=self.settings.gemini_thinking_level,
            ),
        )
        response = self.client.models.generate_content(
            model=self.settings.gemini_planner_model,
            contents=prompt,
            config=config,
        )
        self._log_generation_usage(response, "generate")
        return (response.text or "").strip()

    def generate_structured(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: type[StructuredModel],
    ) -> StructuredModel:
        config = self.types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.settings.structured_temperature,
            response_mime_type="application/json",
            response_schema=response_schema,
            thinking_config=self.types.ThinkingConfig(
                thinking_level=self.settings.gemini_thinking_level,
            ),
        )
        response = self.structured_client.models.generate_content(
            model=self.settings.gemini_planner_model,
            contents=prompt,
            config=config,
        )
        self._log_generation_usage(response, "generate_structured")
        if response.parsed is None:
            return response_schema.model_validate_json(response.text or "{}")
        return response_schema.model_validate(response.parsed)

    def _log_generation_usage(self, response, operation: str) -> None:
        usage = response.usage_metadata
        input_tokens = int(usage.prompt_token_count or 0) if usage else 0
        output_tokens = int(usage.candidates_token_count or 0) if usage else 0
        thinking_tokens = int(usage.thoughts_token_count or 0) if usage else 0
        logger.info(
            "Gemini usage operation={} model={} input_tokens={} output_tokens={} "
            "thinking_tokens={}",
            operation,
            self.settings.gemini_planner_model,
            input_tokens,
            output_tokens,
            thinking_tokens,
        )


class GeminiEmbeddingClient(GeminiClient):
    """Embedding-only Gemini client that does not require a planner model."""

    def __init__(self, settings: Settings):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "Missing google-genai. Install dependencies with: "
                "pip install -r requirements.txt"
            ) from exc

        self.settings = settings
        self.types = types
        self._query_embedding_lock = Lock()
        self._query_embedding_cache = (
            Path(settings.query_embedding_cache)
            / f"{settings.embedding_model.replace('/', '_')}-{settings.embedding_dim}"
        )
        self.client = self._create_client(genai)
        self.structured_client = self.client

    def close(self) -> None:
        self.client.close()
