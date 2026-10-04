"""Country choices shared by the editor, CSV validation and API payloads."""

import json
from pathlib import Path
import re
import unicodedata


COUNTRIES = json.loads((Path(__file__).resolve().parent / "data" / "countries.json").read_text(encoding="utf-8"))


def _key(value):
    text = unicodedata.normalize("NFKD", value.strip()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


_LOOKUP = {}
for _country in COUNTRIES:
    for _alias in [_country["name"], _country["value"], _country["code"], *_country["aliases"]]:
        _normalized = _key(_alias)
        if _normalized in _LOOKUP and _LOOKUP[_normalized] != _country:
            raise ValueError(f"Ambiguous country alias: {_alias}")
        _LOOKUP[_normalized] = _country


def resolve_country(value):
    """Resolve a display name, vendor enum, common alias or two-letter code."""
    country = _LOOKUP.get(_key(value))
    if country is None:
        raise ValueError("choose a supported country from the Country list (CSV also accepts two-letter country codes)")
    return country
