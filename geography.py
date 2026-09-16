"""Local Census boundary checks; coordinates are always longitude/latitude."""

import gzip
import json
import math
from functools import lru_cache
from pathlib import Path

from shapely import Point, prepare
from shapely.geometry import shape

BOUNDARIES = Path(__file__).resolve().parent / "data" / "us_states_2025.json.gz"
BOUNDARY_URL = "https://www.census.gov/geographies/mapping-files/2025/geo/carto-boundary-file.html"
# Cartographic boundaries simplify coastlines. Close points are separately
# labeled for review, rather than treating a small cartographic offset as proof.
BORDER_DEGREES = 0.01


@lru_cache(maxsize=1)
def state_boundaries():
    with gzip.open(BOUNDARIES, "rt") as source:
        data = json.load(source)
    result = {code: shape(item["geometry"]) for code, item in data["states"].items()}
    for geometry in result.values():
        prepare(geometry)
    return result


@lru_cache(maxsize=16384)
def state_check(lat, lon, state):
    geometry = state_boundaries().get(state)
    if geometry is None:
        return "not_covered"
    if lat is None or lon is None or not all(math.isfinite(v) for v in (lat, lon)):
        return "missing"
    point = Point(lon, lat)
    if geometry.covers(point):
        return "inside"
    if geometry.distance(point) <= BORDER_DEGREES:
        return "near_boundary"
    return "outside"


@lru_cache(maxsize=16384)
def state_distance(lat, lon, state):
    """Degrees from the state's boundary: 0.0 inside, None when not covered."""
    geometry = state_boundaries().get(state)
    if geometry is None or lat is None or lon is None or not all(math.isfinite(v) for v in (lat, lon)):
        return None
    point = Point(lon, lat)
    return 0.0 if geometry.covers(point) else geometry.distance(point)


def distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    value = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(max(0, value))))
