from types import SimpleNamespace

from nextrip_graphrag.config import Settings
from nextrip_graphrag.gemini_client import GeminiClient


class FakeTypes:
    @staticmethod
    def HttpRetryOptions(**kwargs):
        return kwargs

    @staticmethod
    def HttpOptions(**kwargs):
        return kwargs


class FakeGenai:
    captured = None

    @classmethod
    def Client(cls, **kwargs):
        cls.captured = kwargs
        return kwargs


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
