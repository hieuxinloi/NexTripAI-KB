from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from .io import cache_path, read_json, write_json


T = TypeVar("T")


class CachedBatchEmbedder:
    """Content-addressed embedding cache with conservative quota retries."""

    def __init__(
        self,
        embedder: Any,
        cache_dir: str | Path,
        *,
        model: str,
        dimensions: int,
        delay: float = 2.0,
        max_retries: int = 5,
    ) -> None:
        self.embedder = embedder
        self.cache_dir = Path(cache_dir) / f"{model.replace('/', '_')}-{dimensions}"
        self.delay = max(0.0, delay)
        self.max_retries = max(0, max_retries)
        self.last_request_at = 0.0

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        values = list(texts)
        vectors: list[list[float] | None] = [None] * len(values)
        missing_indexes: list[int] = []
        for index, value in enumerate(values):
            path = cache_path(self.cache_dir, value)
            if path.exists():
                vectors[index] = read_json(path)["embedding"]
            else:
                missing_indexes.append(index)

        if missing_indexes:
            missing_texts = [values[index] for index in missing_indexes]
            missing_vectors = self._request_with_retry(missing_texts)
            for index, vector in zip(missing_indexes, missing_vectors, strict=True):
                vectors[index] = vector
                write_json(cache_path(self.cache_dir, values[index]), {"embedding": vector})

        return [vector for vector in vectors if vector is not None]

    def embed_query(self, query: str) -> list[float]:
        key = f"query|{query}"
        path = cache_path(self.cache_dir, key)
        if path.exists():
            return read_json(path)["embedding"]
        vector = self._run_with_retry(lambda: self.embedder.embed_query(query))
        write_json(path, {"embedding": vector})
        return vector

    def _request_with_retry(self, texts: list[str]) -> list[list[float]]:
        return self._run_with_retry(lambda: self.embedder.embed_documents(texts))

    def _run_with_retry(self, operation: Callable[[], T]) -> T:
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self.last_request_at
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            try:
                result = operation()
                self.last_request_at = time.monotonic()
                return result
            except Exception as exc:
                self.last_request_at = time.monotonic()
                message = str(exc).lower()
                transient = any(
                    marker in message
                    for marker in ("429", "resource_exhausted", "503", "unavailable", "deadline")
                )
                if not transient or attempt >= self.max_retries:
                    raise
                time.sleep(min(15 * (2**attempt), 120))
        raise RuntimeError("Embedding retry loop exited unexpectedly")
