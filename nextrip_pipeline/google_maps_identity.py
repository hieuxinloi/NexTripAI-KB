from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit


_GOOGLE_HOSTS = {"google.com", "maps.google.com", "www.google.com"}
_GOOGLE_SHARE_HOSTS = {*_GOOGLE_HOSTS, "maps.app.goo.gl", "goo.gl"}
_GOOGLE_DATA_ID_PATTERN = re.compile(
    r"!1s(?P<token>"
    r"0x[0-9a-f]+:0x[0-9a-f]+"
    r"|ChI[A-Za-z0-9_-]+"
    r"|/g/[A-Za-z0-9_-]+"
    r")(?=!|[?&#]|$)",
    re.IGNORECASE,
)
_GOOGLE_G_PATH_PATTERN = re.compile(
    r"(?:^|/)g/(?P<token>[A-Za-z0-9_-]+)(?:/|$)",
    re.IGNORECASE,
)
_DIRECT_STABLE_ID_PATTERN = re.compile(
    r"(?:"
    r"0x[0-9a-f]+:0x[0-9a-f]+"
    r"|cid:[0-9]+"
    r"|ChI[A-Za-z0-9_-]+"
    r"|/g/[A-Za-z0-9_-]+"
    r")",
    re.IGNORECASE,
)


def google_maps_search_placeholder(entity_id: str) -> str:
    """Return a collision-free non-provider identity used only for searching."""

    value = entity_id.strip()
    if not value:
        raise ValueError("Google Maps search placeholder requires an entity ID")
    return f"search:{value}"


def google_maps_official_share_url(value: object) -> str | None:
    """Return a public Google-owned Maps/share URL, never an arbitrary link."""

    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    parsed = urlsplit(text)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if host not in _GOOGLE_SHARE_HOSTS:
        return None
    path = unquote(parsed.path)
    if host == "goo.gl" and not path.casefold().startswith("/maps/"):
        return None
    if host in _GOOGLE_HOSTS and "/maps/" not in path.casefold():
        return None
    return text


def google_maps_stable_external_id(value: object) -> str | None:
    """Extract a stable Google place token from an ID or official Maps URL."""

    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if _DIRECT_STABLE_ID_PATTERN.fullmatch(text):
        return text

    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if (parsed.hostname or "").casefold() not in _GOOGLE_HOSTS:
        return None

    decoded = unquote(text)
    if match := _GOOGLE_DATA_ID_PATTERN.search(decoded):
        return match.group("token")
    if match := _GOOGLE_G_PATH_PATTERN.search(unquote(parsed.path)):
        return f"/g/{match.group('token')}"

    query_values = dict(parse_qsl(parsed.query, keep_blank_values=False))
    for key in ("query_place_id", "place_id"):
        if place_id := query_values.get(key):
            candidate = place_id.removeprefix("place_id:").strip()
            if _DIRECT_STABLE_ID_PATTERN.fullmatch(candidate):
                return candidate
    if (cid := query_values.get("cid")) and cid.isdecimal():
        return f"cid:{cid}"
    return None


def google_maps_stable_place_url(value: object) -> str | None:
    """Accept only an official ``/maps/place/`` URL carrying a stable place ID."""

    official_url = google_maps_official_share_url(value)
    if official_url is None:
        return None
    parsed = urlsplit(official_url)
    if (parsed.hostname or "").casefold() not in _GOOGLE_HOSTS:
        return None
    if "/maps/place/" not in unquote(parsed.path).casefold():
        return None
    if google_maps_stable_external_id(official_url) is None:
        return None
    return official_url


def google_maps_stable_external_ids(*values: object) -> tuple[str, ...]:
    """Collect unique stable IDs while detecting conflicting evidence."""

    by_key: dict[str, str] = {}
    for value in values:
        if external_id := google_maps_stable_external_id(value):
            # Google place identifiers are opaque provider values. Their
            # prefixes may be detected case-insensitively, but the identifier
            # itself must never be normalized for equality or uniqueness.
            by_key.setdefault(external_id, external_id)
    return tuple(by_key[key] for key in sorted(by_key))
