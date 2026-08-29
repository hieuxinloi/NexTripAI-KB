from __future__ import annotations

import math
import re
import unicodedata


# Open Location Code is an open specification.  Google Maps exposes the code
# for the selected listing in the place-information panel.  A ten-digit code
# identifies an area of roughly 14 m x 14 m; an optional eleventh digit narrows
# that area further.  This module implements only the small decode/recovery
# subset needed for provider-rendered Maps Plus Codes.
_ALPHABET = "23456789CFGHJMPQRVWX"
_BASE = len(_ALPHABET)
_SEPARATOR_POSITION = 8
_PAIR_CODE_LENGTH = 10
_PAIR_RESOLUTION = 0.000125
_GRID_ROWS = 5
_GRID_COLUMNS = 4
_CODE_PATTERN = re.compile(
    rf"(?<![{_ALPHABET}])([{_ALPHABET}]{{2,8}}\+[{_ALPHABET}]{{2,3}})"
    rf"(?![{_ALPHABET}])",
    flags=re.IGNORECASE,
)

# Short Plus Codes require a nearby reference.  These fixed service-area
# references are deliberately independent of a place's canonical coordinates
# and of the @lat,lng viewport used to submit the Maps search.
_CITY_REFERENCES = {
    "city_da_nang": (16.0544, 108.2022),
    "city_quy_nhon": (13.7820, 109.2190),
    "da nang": (16.0544, 108.2022),
    "quy nhon": (13.7820, 109.2190),
}


def google_maps_plus_code_from_text(value: object) -> str | None:
    """Extract one syntactically valid full or short Plus Code from text."""

    if not isinstance(value, str):
        return None
    for match in _CODE_PATTERN.finditer(value.upper()):
        code = match.group(1)
        if _is_valid(code):
            return code
    return None


def google_maps_plus_code_from_scoped_labels(values: object) -> str | None:
    """Extract explicit Plus Code evidence from selected-place aria labels.

    Historical captures retain every aria label but do not retain the
    ``data-item-id='oloc'`` DOM binding.  Requiring the provider's literal
    ``Plus code`` label prevents a code mentioned in a review or another
    unrelated page section from becoming coordinate evidence.
    """

    if not isinstance(values, (list, tuple)):
        return None
    for value in values:
        if not isinstance(value, str) or "plus code" not in value.casefold():
            continue
        if code := google_maps_plus_code_from_text(value):
            return code
    return None


def google_maps_plus_code_center(
    code: object,
    *,
    city_id: object = None,
    city_name: object = None,
) -> tuple[float, float] | None:
    """Return the center of a Maps Plus Code area without an external API.

    Short codes are recovered only for NexTrip's configured service areas.
    Unknown cities and malformed codes fail closed.
    """

    extracted = google_maps_plus_code_from_text(code)
    if extracted is None:
        return None
    if extracted.index("+") < _SEPARATOR_POSITION:
        reference = _city_reference(city_id, city_name)
        if reference is None:
            return None
        return _recover_short_code_center(extracted, *reference)
    return _decode_full_code_center(extracted)


def _city_reference(city_id: object, city_name: object) -> tuple[float, float] | None:
    if isinstance(city_id, str):
        value = _CITY_REFERENCES.get(city_id.strip().casefold())
        if value is not None:
            return value
    if isinstance(city_name, str):
        normalized = "".join(
            character
            for character in unicodedata.normalize("NFKD", city_name.casefold())
            if not unicodedata.combining(character)
        ).replace("đ", "d")
        normalized = " ".join(normalized.split())
        return _CITY_REFERENCES.get(normalized)
    return None


def _is_valid(code: str) -> bool:
    if code.count("+") != 1:
        return False
    separator = code.index("+")
    if separator < 2 or separator > _SEPARATOR_POSITION or separator % 2:
        return False
    suffix_length = len(code) - separator - 1
    if suffix_length not in {2, 3}:
        return False
    if any(character not in _ALPHABET for character in code.replace("+", "")):
        return False
    if separator == _SEPARATOR_POSITION:
        if _ALPHABET.index(code[0]) * _BASE >= 180:
            return False
        if _ALPHABET.index(code[1]) * _BASE >= 360:
            return False
    return True


def _recover_short_code_center(
    code: str,
    reference_latitude: float,
    reference_longitude: float,
) -> tuple[float, float] | None:
    missing_prefix_length = _SEPARATOR_POSITION - code.index("+")
    reference_code = _encode(reference_latitude, reference_longitude)
    recovered = f"{reference_code[:missing_prefix_length]}{code}"
    center = _decode_full_code_center(recovered)
    if center is None:
        return None

    latitude, longitude = center
    resolution = float(_BASE ** (2 - (missing_prefix_length / 2)))
    half_resolution = resolution / 2.0
    if reference_latitude + half_resolution < latitude and latitude - resolution >= -90:
        latitude -= resolution
    elif reference_latitude - half_resolution > latitude and latitude + resolution <= 90:
        latitude += resolution
    if reference_longitude + half_resolution < longitude:
        longitude -= resolution
    elif reference_longitude - half_resolution > longitude:
        longitude += resolution
    return round(latitude, 14), round(longitude, 14)


def _encode(latitude: float, longitude: float) -> str:
    latitude = min(90.0, max(-90.0, latitude))
    longitude = ((longitude + 180.0) % 360.0) - 180.0
    if latitude == 90.0:
        latitude -= _PAIR_RESOLUTION
    latitude += 90.0
    longitude += 180.0

    characters: list[str] = []
    resolution = 20.0
    for _ in range(_PAIR_CODE_LENGTH // 2):
        lat_digit = min(_BASE - 1, int(math.floor(latitude / resolution)))
        lng_digit = min(_BASE - 1, int(math.floor(longitude / resolution)))
        characters.extend((_ALPHABET[lat_digit], _ALPHABET[lng_digit]))
        latitude -= lat_digit * resolution
        longitude -= lng_digit * resolution
        resolution /= _BASE
    value = "".join(characters)
    return f"{value[:_SEPARATOR_POSITION]}+{value[_SEPARATOR_POSITION:]}"


def _decode_full_code_center(code: str) -> tuple[float, float] | None:
    if not _is_valid(code) or code.index("+") != _SEPARATOR_POSITION:
        return None
    digits = code.replace("+", "")
    latitude = -90.0
    longitude = -180.0
    resolution = 20.0
    pair_length = min(len(digits), _PAIR_CODE_LENGTH)
    for index in range(0, pair_length, 2):
        latitude += _ALPHABET.index(digits[index]) * resolution
        longitude += _ALPHABET.index(digits[index + 1]) * resolution
        if index < pair_length - 2:
            resolution /= _BASE
    latitude_precision = resolution
    longitude_precision = resolution

    for character in digits[_PAIR_CODE_LENGTH:]:
        value = _ALPHABET.index(character)
        latitude_precision /= _GRID_ROWS
        longitude_precision /= _GRID_COLUMNS
        latitude += (value // _GRID_COLUMNS) * latitude_precision
        longitude += (value % _GRID_COLUMNS) * longitude_precision
    return (
        round(min(latitude + latitude_precision / 2.0, 90.0), 14),
        round(longitude + longitude_precision / 2.0, 14),
    )
