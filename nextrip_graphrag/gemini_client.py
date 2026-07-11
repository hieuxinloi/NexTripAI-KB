from __future__ import annotations

from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel

from .config import Settings


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
        self.types = types
        self.client = self._create_client(genai)

    def close(self) -> None:
        self.client.close()

    def _create_client(self, genai):
        if self.settings.google_genai_use_vertexai:
            if not self.settings.google_cloud_project:
                raise RuntimeError(
                    "GOOGLE_CLOUD_PROJECT is required when GOOGLE_GENAI_USE_VERTEXAI=true."
                )
            if not self.settings.google_cloud_location:
                raise RuntimeError(
                    "GOOGLE_CLOUD_LOCATION is required when GOOGLE_GENAI_USE_VERTEXAI=true."
                )
            if self.settings.google_application_credentials:
                credentials_path = Path(self.settings.google_application_credentials)
                if not credentials_path.exists():
                    raise RuntimeError(
                        "GOOGLE_APPLICATION_CREDENTIALS points to a missing file: "
                        f"{credentials_path}"
                    )
            return genai.Client(
                vertexai=True,
                project=self.settings.google_cloud_project,
                location=self.settings.google_cloud_location,
            )

        if self.settings.google_api_key:
            return genai.Client(api_key=self.settings.google_api_key)

        raise RuntimeError(
            "Configure Gemini auth with either GOOGLE_API_KEY/GEMINI_API_KEY, or "
            "set GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT, "
            "GOOGLE_CLOUD_LOCATION, and local ADC such as GOOGLE_APPLICATION_CREDENTIALS."
        )

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        return self._embed(list(texts), task_type="RETRIEVAL_DOCUMENT")

    def embed_query(self, query: str) -> list[float]:
        return self._embed([query], task_type="RETRIEVAL_QUERY")[0]

    def _embed(self, contents: list[str], task_type: str) -> list[list[float]]:
        config = self.types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self.settings.embedding_dim,
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

    def generate_structured(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: type[StructuredModel],
    ) -> StructuredModel:
        config = self.types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0,
            response_mime_type="application/json",
            response_schema=response_schema,
        )
        response = self.client.models.generate_content(
            model=self.settings.gemini_model,
            contents=prompt,
            config=config,
        )
        if isinstance(response.parsed, response_schema):
            return response.parsed
        return response_schema.model_validate_json(response.text or "{}")
