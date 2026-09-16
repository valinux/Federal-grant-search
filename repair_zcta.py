"""Audit every filing's coordinates against its own ZIP area and repair mismatches.

The earlier audits verified the mailing *state* (repair_nationwide.py) and a
50 km radius around the postal centroid. This audit is finer: each filing's pin
must fall inside (or within ~1 km of) the Census ZCTA polygon for its own
mailing ZIP — PO boxes included. Filings whose ZIP has no ZCTA polygon
(PO-box-only and unique ZIPs) fall back to a 40 km postal-centroid check.

No network requests occur in this module. Reference data is placed beforehand:
the Census 2020 ZCTA 500k shapefile in the repair directory, GeoNames postal
references and the Census batch response as in the other repairs.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import shapefile
from shapely import Point, prepare as prepare_geometry
from shapely.geometry import shape as as_shape

from geography import distance_km, state_check
from locations import correction_path, location_resolver, parse_coordinates
from postal import mailing_state, mailing_zip
from repair_locations import census_match, census_street, postal_match, postal_reference, split_address

ZCTA_FILE = "cb_2020_us_zcta520_500k.zip"
ZCTA_URL = "https://www.census.gov/geographies/mapping-files/2020/geo/carto-boundary-file.html"
# Simplified cartographic edges: tolerate pins just outside their own polygon.
ZCTA_BUFFER_DEGREES = 0.01
# Fallback for ZIPs without a ZCTA polygon (mostly PO-box-only/unique ZIPs).
NO_ZCTA_KM = 40
NO_ZCTA_KM_AK = 150  # Alaskan ZIP areas can be enormous.
# A GeoNames postal entry this far from its own mailing city is a bad record.
POSTAL_CITY_KM = 25
# Census geocoding the same building to the same spot confirms a pin. Tight,
# because this audit's question is "the right ZIP area", not "the right state".
CENSUS_AGREES_ZIP_KM = 3
INPUT_NAME = "census-input-zcta.csv"
RESULTS_NAME = "census-results-zcta.csv"
SUSPECTS_NAME = "zcta-suspects.json"
AUDIT_NAME = "zcta-audit.json"
VERIFIED_NAME = "zcta-verified.json"


def zcta_boundaries(directory):
    """{ZIP5: shapely geometry} from the Census 2020 ZCTA 500k shapefile."""
    path = Path(directory) / ZCTA_FILE
    boundaries = {}
    with zipfile.ZipFile(path) as archive:
        with archive.open("cb_2020_us_zcta520_500k.shp") as shp, \
             archive.open("cb_2020_us_zcta520_500k.shx") as shx, \
             archive.open("cb_2020_us_zcta520_500k.dbf") as dbf:
            reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
            for record in reader.iterShapeRecords():
                boundaries[record.record.as_dict()["ZCTA5CE20"]] = as_shape(record.shape.__geo_interface__)
    for geometry in boundaries.values():
        prepare_geometry(geometry)
    return boundaries


def effective_address(location, corp_address):
    """The verified correction address wins over the source row's own text."""
    if location["precision"] in ("street", "postal", "city") and location.get("matched_address"):
        return location["matched_address"]
    return corp_address


def us_city_index(directory):
    """US city lookup from cities500: normalized name -> {state: (lat, lon)}."""
    path = Path(directory) / "cities500.zip"
    index = {}
    if not path.exists():
        return index
    with zipfile.ZipFile(path) as archive:
        for line in archive.read("cities500.txt").decode("utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) < 15 or parts[8] != "US":
                continue
            lat, lon = parse_coordinates(f"{parts[4]},{parts[5]}")
            if lat is None:
                continue
            try:
                population = int(parts[14] or 0)
            except ValueError:
                population = 0
            for name in {parts[1], parts[2]}:
                key = re.sub(r"[^A-Z0-9]", "", name.upper())
                if not key:
                    continue
                slot = index.setdefault(key, {})
                if parts[10] not in slot or population > slot[parts[10]][2]:
                    slot[parts[10]] = (lat, lon, population)
    return index


def city_reference_for(address, cities):
    if not address or not cities:
        return None
    key = re.sub(r"[^A-Z0-9]", "", address["city"].upper())
    return (cities.get(key) or {}).get(address["state"])


def trusted_postal(address, reference, cities):
    """GeoNames postal placement, rejected when it disagrees with the mailing city.

    GeoNames occasionally carries a bad record for a ZIP (one PO-box ZIP in
    Barrington, IL points into Lake Michigan); the cities500 gazetteer is the
    independent cross-check.
    """
    result = postal_match(address, reference)
    if result:
        city = city_reference_for(address, cities)
        if city and distance_km(result["lat"], result["lon"], city[0], city[1]) > POSTAL_CITY_KM:
            return None
    return result


def scan(database, directory, reference, cities=None, verified=None):
    """Flag filings whose usable coordinates fall outside their mailing ZIP area."""
    zctas = zcta_boundaries(directory)
    resolve = location_resolver(database)
    suspects, blind = [], []
    stats = Counter()
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT rowid AS id, filer_ein, filer_name, corp_address, geo_loc FROM filings")
        for row in rows:
            row = dict(row)
            location = resolve(row["id"], row["geo_loc"], row["corp_address"], row["filer_ein"])
            address_text = effective_address(location, row["corp_address"])
            zipcode = (mailing_zip(address_text) or "")[:5]
            state = mailing_state(address_text)
            if not zipcode:
                stats["no_mailing_zip"] += 1  # foreign corrections and unparsable rows
                continue
            lat, lon = location["lat"], location["lon"]
            if lat is None:
                stats["unlocated"] += 1  # already excluded from maps
                continue
            geometry = zctas.get(zipcode)
            if geometry is not None:
                distance = geometry.distance(Point(lon, lat))
                if distance <= ZCTA_BUFFER_DEGREES:
                    stats["inside"] += 1
                else:
                    suspects.append({**row, "zip": zipcode, "state": state, "current": location,
                                     "issues": ["outside_zcta"], "zcta_distance_deg": round(distance, 5)})
                    stats["outside_zcta"] += 1
                continue
            stats["no_zcta"] += 1
            address = split_address(address_text or "") or display_address(address_text)
            postal = trusted_postal(address, reference, cities) if address else None
            if postal is not None:
                distance = distance_km(lat, lon, postal["lat"], postal["lon"])
                limit = NO_ZCTA_KM_AK if state == "AK" else NO_ZCTA_KM
                if distance > limit:
                    suspects.append({**row, "zip": zipcode, "state": state, "current": location,
                                     "issues": ["postal_distance"], "postal_distance_km": round(distance, 1)})
                    stats["postal_distance"] += 1
                else:
                    stats["no_zcta_ok"] += 1
                continue
            # No postal reference (or a distrusted one): fall back to the
            # mailing city, state-filtered, from the cities500 gazetteer.
            city_ref = city_reference_for(address, cities)
            if city_ref is None:
                stats["no_reference"] += 1  # no local evidence to verify against
                blind.append({**row, "zip": zipcode, "state": state, "current": location})
                continue
            distance = distance_km(lat, lon, city_ref[0], city_ref[1])
            limit = NO_ZCTA_KM_AK if state == "AK" else NO_ZCTA_KM
            if distance > limit:
                suspects.append({**row, "zip": zipcode, "state": state, "current": location,
                                 "issues": ["city_distance"], "city_distance_km": round(distance, 1),
                                 "city_reference": {"lat": city_ref[0], "lon": city_ref[1]}})
                stats["city_distance"] += 1
            else:
                stats["blind_ok"] += 1
    finally:
        conn.close()
    if verified:
        def unverified(suspect):
            entry = verified.get(str(suspect["id"]))
            if not entry or entry.get("geo_loc") != suspect["geo_loc"]:
                return True
            stats["verified"] += 1  # Census already confirmed this exact pin
            return False
        suspects = [suspect for suspect in suspects if unverified(suspect)]
    return suspects, blind, stats


def postal_repair(address, reference, zctas, cities=None):
    """Postal-area placement: trusted GeoNames centroid, snapped to the ZCTA when needed."""
    result = trusted_postal(address, reference, cities)
    geometry = zctas.get(address["zip"])
    if result:
        if geometry is None or geometry.distance(Point(result["lon"], result["lat"])) <= ZCTA_BUFFER_DEGREES:
            return result
    if geometry is None:
        return result  # GeoNames-only placement; nothing better exists locally
    point = geometry.representative_point()
    base = {"lat": point.y, "lon": point.x, "precision": "postal",
            "source": "U.S. Census 2020 ZCTA boundary (CC0/public domain)", "source_url": ZCTA_URL,
            "matched_address": (result or {}).get("matched_address")
                       or f"{address['city']}, {address['state']} {address['zip']}",
            "reason": "Postal-area estimate from the Census ZIP Code Tabulation Area; "
                      "an exact building coordinate has not been established."}
    return base


def display_address(text):
    """Parse the comma-joined "STREET, CITY, ST ZIP" form used in corrections."""
    match = re.search(r",\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$", str(text or ""))
    if not match:
        return None
    parts = [part.strip() for part in str(text)[:match.start()].split(",") if part.strip()]
    if not parts:
        return None
    return {"street": "", "city": parts[-1], "state": match[1], "zip": match[2]}


def decide(suspect, census_row, reference, zctas, cities=None):
    """Choose a correction for one suspect.

    Returns (action, correction): action is "repair", "keep", or "restore"
    (an earlier correction was wrong and the original coordinates check out).
    A pin is kept when the Census geocoder places the same building at the same
    spot yet also outside the ZCTA — proof the simplified polygon mismaps the
    actual USPS ZIP delivery area rather than the pin being wrong.
    """
    address = split_address(suspect["corp_address"] or "") or display_address(suspect["current"].get("matched_address"))
    result = None
    if address:
        result = census_match(suspect, address, census_row)
        if result and state_check(result["lat"], result["lon"], address["state"]) not in ("inside", "near_boundary"):
            result = None  # A geocode outside its own matched state is not a repair.
    current = suspect["current"]
    if result:
        geometry = zctas.get(address["zip"])
        census_outside = geometry is not None and \
            geometry.distance(Point(result["lon"], result["lat"])) > ZCTA_BUFFER_DEGREES
        if current["lat"] is not None and \
                distance_km(result["lat"], result["lon"], current["lat"], current["lon"]) <= CENSUS_AGREES_ZIP_KM:
            if census_outside or geometry is None:
                return "keep", None  # The pin sits at the Census-verified address.
            # The pin agrees with the Census building point, so it belongs inside
            # the ZIP area too; use the more precise Census coordinates.
        raw_lat, raw_lon = parse_coordinates(suspect["geo_loc"])
        if raw_lat is not None and current["precision"] != "original" and \
                distance_km(result["lat"], result["lon"], raw_lat, raw_lon) <= CENSUS_AGREES_ZIP_KM:
            return "restore", None  # The source coordinates were right; an earlier repair was not.
        return "repair", result
    if address:
        result = postal_repair(address, reference, zctas, cities)
        if result:
            return "repair", result
    city_ref = suspect.get("city_reference")
    if city_ref:
        return "repair", {"lat": city_ref["lat"], "lon": city_ref["lon"], "precision": "city",
                          "source": "GeoNames cities500 dataset (CC BY 4.0)",
                          "source_url": "https://download.geonames.org/export/dump/",
                          "matched_address": f"{address['city']}, {address['state']} {address['zip']}" if address else "",
                          "reason": "City-area estimate from the GeoNames gazetteer for a ZIP with no "
                                    "postal-area reference; an exact building coordinate has not been established."}
    return "repair", {"lat": None, "lon": None, "precision": "unresolved", "source": "", "source_url": "",
                      "matched_address": "",
                      "reason": "The coordinates fall outside the filing's own ZIP area, "
                                "and no sufficiently reliable replacement was found."}


def prepare(database, directory):
    """Write a Census batch for flagged rows that have a street address."""
    directory = Path(directory)
    suspects = json.loads((directory / SUSPECTS_NAME).read_text())
    written = 0
    with (directory / INPUT_NAME).open("w", newline="") as target:
        writer = csv.writer(target)
        for suspect in suspects:
            address = split_address(suspect["corp_address"] or "")
            street = census_street(address["street"]) if address else None
            if street:
                writer.writerow([suspect["id"], street, address["city"], address["state"], address["zip"]])
                written += 1
    print(f"Prepared {written} flagged street addresses for Census.")


def apply(database, directory, reference_dir=None):
    directory = Path(directory)
    reference = postal_reference(reference_dir or directory)
    zctas = zcta_boundaries(directory)
    results_path = directory / RESULTS_NAME
    census = {}
    if results_path.exists():
        with results_path.open(newline="") as source:
            census = {row[0]: row for row in csv.reader(source) if row}
    verified_path = directory / VERIFIED_NAME
    verified = json.loads(verified_path.read_text()) if verified_path.exists() else {}
    cities = us_city_index(reference_dir or directory)
    suspects, blind, stats = scan(database, directory, reference, cities, verified)
    target = correction_path(database)
    existing = json.loads(target.read_text()) if target.exists() else {"version": 1, "corrections": {}}
    audit, counts = [], Counter()
    for suspect in suspects:
        action, result = decide(suspect, census.get(str(suspect["id"])), reference, zctas, cities)
        key = str(suspect["id"])
        if action == "keep":
            verified[key] = {"geo_loc": suspect["geo_loc"],
                             "matched_address": suspect["current"].get("matched_address", "")}
            counts["kept"] += 1
            continue
        verified.pop(key, None)  # a new repair supersedes the old verification
        if action == "restore":
            if key in existing["corrections"]:
                del existing["corrections"][key]
                audit.append({"filing_id": suspect["id"], "action": "restore", "issues": suspect["issues"]})
            counts["restored"] += 1
            continue
        correction = {"filer_ein": str(suspect["filer_ein"]), "filer_name": suspect["filer_name"],
                      "original_address": suspect["corp_address"], "original_geo_loc": suspect["geo_loc"], **result}
        existing["corrections"][key] = correction
        audit.append({"filing_id": suspect["id"], "action": "repair", "issues": suspect["issues"], **correction})
        counts[result["precision"]] += 1
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    summary = existing.setdefault("summary", {})
    summary["total_corrections"] = len(existing["corrections"])
    summary["last_zcta_run"] = dict(counts)
    summary["zcta_scan"] = dict(stats)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(existing, out, indent=2, ensure_ascii=False)
            out.write("\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    (directory / AUDIT_NAME).write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    verified_path.write_text(json.dumps(verified, indent=1) + "\n")
    print(f"ZCTA audit: {len(suspects)} flagged, {len(audit)} corrections changed in {target.name}: {dict(counts)}")
    return existing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "prepare", "apply"])
    parser.add_argument("--db", type=Path,
                        default=Path(os.environ.get("GLOWSEARCH_DB", Path(__file__).resolve().parent / "output_two.db")))
    parser.add_argument("--directory", type=Path, default=Path(".location-repair/nationwide"))
    parser.add_argument("--reference", type=Path, default=Path(".location-repair"))
    args = parser.parse_args()
    if args.command == "scan":
        verified_path = args.directory / VERIFIED_NAME
        verified = json.loads(verified_path.read_text()) if verified_path.exists() else {}
        suspects, blind, stats = scan(args.db, args.directory, postal_reference(args.reference),
                                      us_city_index(args.reference), verified)
        args.directory.mkdir(parents=True, exist_ok=True)
        (args.directory / SUSPECTS_NAME).write_text(json.dumps(suspects, indent=2, ensure_ascii=False) + "\n")
        (args.directory / "zcta-blind.json").write_text(json.dumps(blind, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"stats": dict(stats), "suspects": len(suspects), "blind": len(blind)}))
    elif args.command == "prepare":
        prepare(args.db, args.directory)
    else:
        apply(args.db, args.directory, args.reference)


if __name__ == "__main__":
    main()
