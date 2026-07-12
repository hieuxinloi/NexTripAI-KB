from threading import Lock
from types import SimpleNamespace

from pydantic import BaseModel

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
        gemini_timeout_ms=12000,
        gemini_retry_attempts=4,
    )
    client.types = FakeTypes

    client._create_client(FakeGenai)

    options = FakeGenai.captured["http_options"]
    assert options["timeout"] == 12000
    assert options["retry_options"]["attempts"] == 4
    assert 429 in options["retry_options"]["http_status_codes"]


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
    client = GeminiClient.__new__(GeminiClient)
    client.settings = Settings()
    client.types = FakeTypes
    client.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(
                parsed=expected,
                text='{"value":"text"}',
            )
        )
    )

    result = client.generate_structured("system", "prompt", StructuredResult)

    assert result is expected


def test_gemini_structured_generation_parses_text_when_sdk_cannot_parse() -> None:
    client = GeminiClient.__new__(GeminiClient)
    client.settings = Settings()
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
