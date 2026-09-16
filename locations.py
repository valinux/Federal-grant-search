"""Auditable map corrections layered over the original filing database."""

import json
import math
import re
from functools import lru_cache
from pathlib import Path

from postal import US_CODES

ADDRESS_END = re.compile(r",\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$", re.I)
LABELS = {
    "street": "Street address · Census estimate",
    "postal": "Approximate postal area · not a building location",
    "city": "Approximate city area · not a building location",
    "original": "Original dataset coordinates · unverified",
    "unresolved": "Location needs verification · not shown on the map",
}


def parse_coordinates(value):
    try:
        lat, lon = (float(part.strip()) for part in value.split(","))
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except (ValueError, AttributeError, TypeError):
        pass
    return None, None


def misplaced_in_brazil(value, address):
    lat, lon = parse_coordinates(value)
    if lat is None or not (-34 <= lat <= 6 and -74 <= lon <= -34):
        return False
    match = ADDRESS_END.search(str(address or ""))
    return bool(match and match[1].upper() in US_CODES)


def correction_path(database):
    path = Path(database)
    return path.with_name(path.stem + ".locations.json")


@lru_cache(maxsize=4)
def read_corrections(path, modified_ns, size):
    # Cache invalidates when an atomic repair writes a new file.
    with open(path) as source:
        data = json.load(source)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("corrections"), dict):
        raise ValueError("Unsupported location correction format")
    return data["corrections"]


def corrected_entries(database):
    path = correction_path(database)
    try:
        stat = path.stat()
        return read_corrections(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError):
        return {}


def location_resolver(database):
    corrections = corrected_entries(database)

    @lru_cache(maxsize=2048)
    def resolve(filing_id, original_geo, address, ein):
        correction = corrections.get(str(filing_id))
        # Protect against applying an old rowid to a replaced/edited dataset.
        if isinstance(correction, dict) and (
            correction.get("original_geo_loc") == original_geo
            and correction.get("original_address") == address
            and str(correction.get("filer_ein")) == str(ein)
        ):
            lat, lon = parse_coordinates(f"{correction.get('lat')},{correction.get('lon')}")
            precision = correction.get("precision", "unresolved")
            if precision not in ("street", "postal", "city") or lat is None:
                precision, lat, lon = "unresolved", None, None
            return {"lat": lat, "lon": lon, "precision": precision, "label": LABELS[precision],
                    "source": correction.get("source", ""), "source_url": correction.get("source_url", ""),
                    "address_source_url": correction.get("address_source_url", ""),
                    "matched_address": correction.get("matched_address", ""), "reason": correction.get("reason", "")}
        if misplaced_in_brazil(original_geo, address):
            return {"lat": None, "lon": None, "precision": "unresolved", "label": LABELS["unresolved"],
                    "reason": "The source coordinates conflict with the filing's mailing country."}
        lat, lon = parse_coordinates(original_geo)
        precision = "original" if lat is not None else "unresolved"
        return {"lat": lat, "lon": lon, "precision": precision, "label": LABELS[precision]}

    return resolve
