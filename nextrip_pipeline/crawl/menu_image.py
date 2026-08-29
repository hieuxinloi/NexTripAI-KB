from __future__ import annotations

import hashlib
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from nextrip_pipeline.crawl.raw_writer import RawJsonWriter, compute_content_hash
from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    RecordSubjectType,
    SourceRecord,
)


class MenuImageDownloadError(RuntimeError):
    """Raised when a menu image is unavailable or is not a valid image."""


def google_image_high_resolution_url(url: str, size: int = 2000) -> str:
    """Replace a Google image resize suffix while retaining the image identity."""
    if not url.startswith("https://lh3.googleusercontent.com/"):
        return url
    return re.sub(r"=[^/?#]+$", f"=s{size}", url)


@dataclass(frozen=True, slots=True)
class MenuImageCapture:
    requested_url: str
    final_url: str
    content: bytes
    content_type: str
    content_hash: str
    width: int | None
    height: int | None
    crawled_at: datetime

    @property
    def extension(self) -> str:
        return {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(self.content_type.split(";", 1)[0].lower(), ".img")


class MenuImageDownloader:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: float = 30,
        max_bytes: int = 15 * 1024 * 1024,
    ) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def fetch(self, url: str) -> MenuImageCapture:
        requested_url = google_image_high_resolution_url(url)
        owns_client = self.client is None
        client = self.client or httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 NexTripAI-MenuCapture/1.0"},
        )
        try:
            response = client.get(requested_url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise MenuImageDownloadError(
                f"menu image download failed: {error}"
            ) from error
        finally:
            if owns_client:
                client.close()
        content = response.content
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise MenuImageDownloadError(
                f"menu URL returned {content_type or 'an unknown content type'}"
            )
        if not content or len(content) > self.max_bytes:
            raise MenuImageDownloadError(
                "menu image is empty or exceeds the size limit"
            )
        width, height = self._dimensions(content)
        return MenuImageCapture(
            requested_url=requested_url,
            final_url=str(response.url),
            content=content,
            content_type=content_type,
            content_hash=hashlib.sha256(content).hexdigest(),
            width=width,
            height=height,
            crawled_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _dimensions(content: bytes) -> tuple[int | None, int | None]:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(content)) as image:
                image.verify()
                return image.width, image.height
        except (ImportError, OSError):
            return None, None


def build_menu_source_record(
    capture: MenuImageCapture,
    mapping: ExternalEntityMapping,
    *,
    run_id: str,
) -> SourceRecord:
    record_id = f"menu-image-{uuid4().hex}"
    payload = {
        "requested_url": capture.requested_url,
        "final_url": capture.final_url,
        "image_content_hash": capture.content_hash,
        "content_type": capture.content_type,
        "byte_size": len(capture.content),
        "width": capture.width,
        "height": capture.height,
        "sidecar_filename": f"record={record_id}{capture.extension}",
    }
    return SourceRecord(
        source_record_id=record_id,
        run_id=run_id,
        source_id=mapping.source_id,
        entity_type=mapping.entity_type,
        subject_type=RecordSubjectType.MENU,
        subject_id=mapping.entity_id,
        crawled_at=capture.crawled_at,
        raw_payload=payload,
        content_hash=compute_content_hash(payload),
        parser_version="menu-image-capture/1.0.0",
        source_url=capture.final_url,
        http_status=200,
        content_type=capture.content_type,
    )


class RawMenuImageWriter:
    """Writes image bytes beside the immutable SourceRecord envelope."""

    def __init__(self, raw_writer: RawJsonWriter) -> None:
        self.raw_writer = raw_writer

    def write(self, record: SourceRecord, capture: MenuImageCapture) -> Path:
        destination = self.raw_writer.destination_for(record).with_suffix(
            capture.extension
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Raw menu image already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "wb") as file:
            file.write(capture.content)
            file.flush()
            os.fsync(file.fileno())
        return destination
