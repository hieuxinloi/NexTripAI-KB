from threading import Lock
from types import SimpleNamespace

from pydantic import BaseModel
import pytest

from nextrip_graphrag.config import Settings
from nextrip_graphrag.gemini_client import GeminiClient


class FakeTypes:
    @staticmethod
    def HttpRetryOptions(**kwargs):
        return kwargs

    @staticmethod
    def HttpOptions(**kwargs):
        return kwargs

    @staticmethod
    def GenerateContentConfig(**kwargs):
        return kwargs

    @staticmethod
    def ThinkingConfig(**kwargs):
        return kwargs


class FakeGenai:
    captured = None

    @classmethod
    def Client(cls, **kwargs):
        cls.captured = kwargs
        return kwargs


class StructuredResult(BaseModel):
    value: str


def test_gemini_client_configures_sdk_timeout_and_retry_policy() -> None:
    client = GeminiClient.__new__(GeminiClient)
    client.settings = Settings(
        google_api_key="test-key",
        gemini_planner_model="test-planner-model",
        gemini_timeout_ms=12000,
        gemini_retry_attempts=4,
    )
    client.types = FakeTypes

    client._create_client(FakeGenai)

    assert FakeGenai.captured["api_key"] == "test-key"
    assert "vertexai" not in FakeGenai.captured
    options = FakeGenai.captured["http_options"]
    assert options["timeout"] == 12000
    assert options["retry_options"]["attempts"] == 4
    assert 429 in options["retry_options"]["http_status_codes"]


def test_gemini_client_requires_ai_studio_api_key() -> None:
    client = GeminiClient.__new__(GeminiClient)
    client.settings = Settings(google_api_key=None)
    client.types = FakeTypes

    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        client._create_client(FakeGenai)


def test_gemini_query_embedding_reuses_persistent_cache(tmp_path) -> None:
    client = GeminiClient.__new__(GeminiClient)
    client._query_embedding_lock = Lock()
    client._query_embedding_cache = tmp_path
    calls = []

    def fake_embed(contents, task_type):
        calls.append((contents, task_type))
        return [[0.1, 0.2]]

    client._embed = fake_embed

    first = client.embed_query("nghỉ ngơi")
    second = client.embed_query("nghỉ ngơi")

    assert first == second == [0.1, 0.2]
    assert calls == [(["nghỉ ngơi"], "RETRIEVAL_QUERY")]


def test_gemini_structured_generation_uses_sdk_parsed_model() -> None:
    expected = StructuredResult(value="parsed")
    captured = {}
    client = GeminiClient.__new__(GeminiClient)
    client.settings = Settings(
        gemini_planner_model="test-planner-model",
        gemini_thinking_level="minimal",
    )
    client.types = FakeTypes
    client.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: (
                captured.update(kwargs)
                or SimpleNamespace(
                    parsed=expected,
                    text='{"value":"text"}',
                )
            )
        )
    )

    result = client.generate_structured("system", "prompt", StructuredResult)

    assert result is expected
    assert captured["model"] == "test-planner-model"
    assert captured["config"]["thinking_config"] == {"thinking_level": "minimal"}


def test_gemini_structured_generation_parses_text_when_sdk_cannot_parse() -> None:
    client = GeminiClient.__new__(GeminiClient)
    client.settings = Settings(gemini_planner_model="test-planner-model")
    client.types = FakeTypes
    client.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(
                parsed=None,
                text='{"value":"fallback"}',
            )
        )
    )

    result = client.generate_structured("system", "prompt", StructuredResult)

    assert result == StructuredResult(value="fallback")


def test_gemini_uses_fail_fast_client_only_for_structured_generation() -> None:
    calls = []
    client = GeminiClient.__new__(GeminiClient)
    client.settings = Settings(gemini_planner_model="test-planner-model")
    client.types = FakeTypes
    client.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: (
                calls.append("general")
                or SimpleNamespace(text="OK", parsed=None)
            )
        )
    )
    client.structured_client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: (
                calls.append("structured")
                or SimpleNamespace(
                    text='{"value":"structured"}',
                    parsed=StructuredResult(value="structured"),
                )
            )
        )
    )

    assert client.generate("system", "prompt") == "OK"
    assert client.generate_structured(
        "system",
        "prompt",
        StructuredResult,
    ) == StructuredResult(value="structured")
    assert calls == ["general", "structured"]
