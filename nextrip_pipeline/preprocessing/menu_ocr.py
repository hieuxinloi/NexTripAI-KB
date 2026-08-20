from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    MenuItem,
    MenuObservation,
    MenuOcrLine,
    NormalizedMenu,
    NormalizedMenuItem,
    RecordSubjectType,
    SourceRecord,
    VerificationStatus,
)


class MenuOcrDependencyError(RuntimeError):
    """Raised when the optional local OCR runtime is not installed."""


class MenuOcrEngine(Protocol):
    name: str
    version: str

    def extract(self, image_path: Path) -> list[MenuOcrLine]: ...


class RapidOcrEngine:
    name = "rapidocr"

    def __init__(self) -> None:
        try:
            from rapidocr import RapidOCR
        except ImportError as error:
            raise MenuOcrDependencyError(
                "Local menu OCR is not installed. Run: "
                "python -m pip install -e .[menu-ocr]"
            ) from error
        self.version = importlib.metadata.version("rapidocr")
        self._engine = RapidOCR()

    def extract(self, image_path: Path) -> list[MenuOcrLine]:
        output = self._engine(str(image_path))
        if hasattr(output, "txts"):
            texts = list(output.txts) if output.txts is not None else []
            scores = list(output.scores) if output.scores is not None else []
            boxes = list(output.boxes) if output.boxes is not None else []
            return [
                MenuOcrLine(
                    text=str(text),
                    confidence=float(score),
                    bounding_box=self._box(box),
                )
                for text, score, box in zip(texts, scores, boxes)
                if str(text).strip()
            ]
        rows = output[0] if isinstance(output, tuple) else output
        if not rows:
            return []
        return [
            MenuOcrLine(
                text=str(row[1]),
                confidence=float(row[2]),
                bounding_box=self._box(row[0]),
            )
            for row in rows
            if len(row) >= 3 and str(row[1]).strip()
        ]

    @staticmethod
    def _box(value: object) -> list[list[float]]:
        if hasattr(value, "tolist"):
            value = value.tolist()
        if not isinstance(value, list):
            return []
        return [
            [float(point[0]), float(point[1])]
            for point in value
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]


class MenuOcrCache:
    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def load(
        self, content_hash: str, engine: MenuOcrEngine
    ) -> list[MenuOcrLine] | None:
        path = self._path(content_hash, engine)
        if not path.exists():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        return [MenuOcrLine.model_validate(item) for item in document["lines"]]

    def store(
        self,
        content_hash: str,
        engine: MenuOcrEngine,
        lines: list[MenuOcrLine],
    ) -> Path:
        destination = self._path(content_hash, engine)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "image_content_hash": content_hash,
            "ocr_engine": engine.name,
            "ocr_engine_version": engine.version,
            "lines": [line.model_dump(mode="json") for line in lines],
        }
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return destination
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        return destination

    def _path(self, content_hash: str, engine: MenuOcrEngine) -> Path:
        key = hashlib.sha256(
            f"{content_hash}:{engine.name}:{engine.version}".encode()
        ).hexdigest()
        return self.root_directory / f"ocr={engine.name}" / f"result={key}.json"


@dataclass(frozen=True, slots=True)
class _Candidate:
    name: str
    amount: Decimal
    confidence: float
    section: str | None


class MenuOcrNormalizer:
    parser_version = "1.0.0"
    _price_pattern = re.compile(
        r"(?<!\w)(\d{1,3}(?:[.,]\d{3})+|\d{1,7})\s*(k|K|nghìn|đ|₫|vnd)?\s*$",
        flags=re.IGNORECASE,
    )
    _section_names = {
        "cafe",
        "ca phe",
        "tra",
        "tra sua",
        "sinh to",
        "da xay",
        "nuoc ep",
        "soda",
        "sua chua",
        "thuc uong khac",
        "mon an",
        "do an",
    }
    _metadata_terms = {"wifi", "wi-fi", "password", "hotline", "dia chi", "address"}

    def normalize(
        self,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
        lines: list[MenuOcrLine],
        *,
        engine_name: str,
        engine_version: str,
    ) -> MenuObservation:
        if record.subject_type is not RecordSubjectType.MENU:
            raise ValueError("source record is not a menu image")
        if record.subject_id != mapping.entity_id:
            raise ValueError("menu record and mapping entity differ")
        image_hash = record.raw_payload.get("image_content_hash")
        if not isinstance(image_hash, str):
            raise ValueError("menu source record has no image content hash")
        items = self._items(lines)
        return MenuObservation(
            observation_id=f"{record.source_record_id}:menu",
            run_id=record.run_id,
            place_id=mapping.entity_id,
            source_record_id=record.source_record_id,
            source_url=record.source_url,
            image_content_hash=image_hash,
            ocr_engine=engine_name,
            ocr_engine_version=engine_version,
            raw_ocr_text="\n".join(line.text for line in lines),
            ocr_lines=lines,
            valid_on=record.crawled_at.date(),
            items=items,
            observed_at=record.crawled_at,
            verification_status=VerificationStatus.PENDING_REVIEW,
        )

    def _items(self, lines: list[MenuOcrLine]) -> list[MenuItem]:
        candidates: list[_Candidate] = []
        pending_names: list[tuple[MenuOcrLine, str, str | None]] = []
        ordered = self._reading_order(lines)
        headings = [
            (line, re.sub(r"\s+", " ", line.text).strip(" -–—:|"))
            for line in ordered
            if self._ascii(line.text.strip(" -–—:|")) in self._section_names
        ]
        for line in ordered:
            text = re.sub(r"\s+", " ", line.text).strip(" -–—:|")
            if not text:
                continue
            normalized = self._ascii(text)
            if normalized in self._section_names:
                continue
            if any(term in normalized for term in self._metadata_terms):
                continue
            section = self._section_for(line, headings)
            match = self._price_pattern.search(text)
            if match:
                amount = self._amount(match.group(1), match.group(2))
                if amount is None:
                    continue
                name = text[: match.start()].strip(" .:-–—|")
                if not name:
                    paired = self._nearest_name(line, pending_names)
                    if paired is not None:
                        name, paired_section, name_confidence = paired
                        candidates.append(
                            _Candidate(
                                name,
                                amount,
                                min(line.confidence, name_confidence),
                                paired_section,
                            )
                        )
                    continue
                if len(name) >= 2 and any(char.isalpha() for char in name):
                    candidates.append(
                        _Candidate(name, amount, line.confidence, section)
                    )
            elif len(text) >= 2 and any(char.isalpha() for char in text):
                pending_names.append((line, text, section))
        unique: dict[tuple[str, Decimal], _Candidate] = {}
        for item in candidates:
            unique.setdefault((self._ascii(item.name), item.amount), item)
        return [
            MenuItem(
                item_id=f"menu-item-{index:03d}",
                name=item.name,
                section=item.section,
                amount=item.amount,
                ocr_confidence=item.confidence,
            )
            for index, item in enumerate(unique.values(), start=1)
        ]

    @classmethod
    def _amount(cls, raw: str, suffix: str | None) -> Decimal | None:
        digits = re.sub(r"[.,]", "", raw)
        value = Decimal(digits)
        if suffix and suffix.casefold() in {"k", "nghìn"}:
            return value * 1000
        if not suffix and value < 1000:
            return value * 1000
        if not suffix and value < 10000:
            return None
        return value

    @classmethod
    def _section_for(
        cls,
        item_line: MenuOcrLine,
        headings: list[tuple[MenuOcrLine, str]],
    ) -> str | None:
        item_center = cls._center(item_line)
        if item_center is None:
            return None
        item_x, item_y = item_center
        candidates = []
        for heading_line, heading in headings:
            center = cls._center(heading_line)
            if center is None or center[1] > item_y + 10:
                continue
            x_distance = abs(item_x - center[0])
            y_distance = item_y - center[1]
            candidates.append((y_distance + x_distance * 0.35, heading))
        return min(candidates)[1] if candidates else None

    @staticmethod
    def _center(line: MenuOcrLine) -> tuple[float, float] | None:
        if not line.bounding_box:
            return None
        return (
            sum(point[0] for point in line.bounding_box) / len(line.bounding_box),
            sum(point[1] for point in line.bounding_box) / len(line.bounding_box),
        )

    @staticmethod
    def _reading_order(lines: list[MenuOcrLine]) -> list[MenuOcrLine]:
        def key(line: MenuOcrLine) -> tuple[float, float]:
            if not line.bounding_box:
                return float("inf"), float("inf")
            return (
                min(point[1] for point in line.bounding_box),
                min(point[0] for point in line.bounding_box),
            )

        return sorted(lines, key=key)

    @classmethod
    def _nearest_name(
        cls,
        price_line: MenuOcrLine,
        pending: list[tuple[MenuOcrLine, str, str | None]],
    ) -> tuple[str, str | None, float] | None:
        if not price_line.bounding_box or not pending:
            return None
        price_y = sum(point[1] for point in price_line.bounding_box) / len(
            price_line.bounding_box
        )
        ranked = []
        for index, (line, text, section) in enumerate(pending):
            if not line.bounding_box:
                continue
            line_y = sum(point[1] for point in line.bounding_box) / len(
                line.bounding_box
            )
            ranked.append(
                (abs(price_y - line_y), index, text, section, line.confidence)
            )
        if not ranked:
            return None
        distance, index, text, section, confidence = min(ranked)
        if distance > 60:
            return None
        pending.pop(index)
        return text, section, confidence

    @staticmethod
    def _ascii(value: str) -> str:
        value = unicodedata.normalize("NFKD", value.casefold())
        return " ".join(
            "".join(char for char in value if not unicodedata.combining(char)).split()
        )


class NormalizedMenuWriter:
    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, observation: MenuObservation) -> Path:
        destination = (
            self.root_directory
            / "entity=menu"
            / f"date={observation.observed_at.date().isoformat()}"
            / f"run={quote(observation.run_id, safe='-_.')}"
            / f"observation={quote(observation.observation_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Menu observation already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            normalized = self.to_normalized_menu(observation)
            file.write(normalized.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination

    @staticmethod
    def to_normalized_menu(observation: MenuObservation) -> NormalizedMenu:
        items = []
        for item in observation.items:
            if item.amount is None or item.amount != item.amount.to_integral_value():
                raise ValueError("normalized VND menu items require an integer amount")
            items.append(
                NormalizedMenuItem(
                    name=item.name,
                    section=item.section,
                    currency=item.currency,
                    amount=int(item.amount),
                )
            )
        return NormalizedMenu(place_id=observation.place_id, items=items)
