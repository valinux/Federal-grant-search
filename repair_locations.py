"""Prepare a Census batch and apply independently sourced location corrections.

No network requests occur in this script. Download postal references and submit
the prepared CSV to Census separately, then apply the returned results.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from locations import ADDRESS_END, correction_path, misplaced_in_brazil, parse_coordinates

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html"
POSTAL_URL = "https://download.geonames.org/export/zip/"


def split_address(address):
    match = ADDRESS_END.search(address)
    if not match:
        return None
    prefix = address[:match.start()].strip()
    if "\n" not in prefix:
        return None
    street, city = prefix.rsplit("\n", 1)
    return {"street": " ".join(street.split()), "city": city.strip(), "state": match[1].upper(), "zip": match[2]}


def census_street(street):
    if re.search(r"\bP\.?\s*O\.?\s*BOX\b", street, re.I):
        return None
    if street.upper().startswith("C/O "):
        found = re.search(r"\b\d+[\w-]*\s+\S", street)
        if found:
            street = street[found.start():]
    return re.sub(r"\s+(?:SUITE|STE|UNIT|APT|RM)(?:\b|/).*$", "", street, flags=re.I)


def candidates(database):
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT rowid AS id, filer_ein, filer_name, corp_address, geo_loc FROM filings")
                if misplaced_in_brazil(row["geo_loc"], row["corp_address"])]
    finally:
        conn.close()


def prepare(database, directory):
    rows = candidates(database)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "census-input.csv").open("w", newline="") as target:
        writer = csv.writer(target)
        count = 0
        for row in rows:
            address = split_address(row["corp_address"])
            street = census_street(address["street"]) if address else None
            if street:
                writer.writerow([row["id"], street, address["city"], address["state"], address["zip"]])
                count += 1
    print(f"Prepared {count} street addresses from {len(rows)} misplaced filings.")


def postal_reference(directory):
    reference = defaultdict(list)
    for path in directory.glob("*.zip"):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.lower() == "readme.txt" or not name.endswith(".txt"):
                    continue
                for line in archive.read(name).decode("utf-8").splitlines():
                    parts = line.split("\t")
                    if len(parts) < 11:
                        continue
                    lat, lon = parse_coordinates(f"{parts[9]},{parts[10]}")
                    if lat is not None:
                        reference[(parts[0], parts[1])].append({"city": parts[2], "state": parts[4], "lat": lat, "lon": lon})
    return reference


def postal_match(address, reference, country=None):
    country = country or (address["state"] if address["state"] in {"PR", "VI", "GU", "AS", "MP"} else "US")
    choices = reference.get((country, address["zip"]), [])
    if country == "US":
        choices = [choice for choice in choices if choice["state"] == address["state"]]
    if not choices:
        return None
    # Multiple locality names may share a postal code; prefer the input locality.
    normal = lambda text: re.sub(r"\W", "", text).casefold()
    exact = [choice for choice in choices if normal(choice["city"]) == normal(address["city"])]
    if exact:
        choices = exact
    # Do not choose arbitrarily if one postal code has geographically distant places.
    if max(choice["lat"] for choice in choices) - min(choice["lat"] for choice in choices) > 0.5 or max(choice["lon"] for choice in choices) - min(choice["lon"] for choice in choices) > 0.5:
        return None
    return {"lat": sum(choice["lat"] for choice in choices) / len(choices),
            "lon": sum(choice["lon"] for choice in choices) / len(choices),
            "precision": "postal", "source": "GeoNames postal dataset (CC BY 4.0)", "source_url": POSTAL_URL,
            "matched_address": f"{address['city']}, {address['state']} {address['zip']}",
            "reason": "Postal-area estimate; an exact building coordinate has not been established."}


def census_match(row, address, result):
    if not result or len(result) < 6 or result[2] != "Match":
        return None
    match = re.search(r",\s*([A-Z]{2}),\s*(\d{5})$", result[4])
    if not match or (match[1], match[2]) != (address["state"], address["zip"]):
        return None
    # Census also offers loose matches. Reject a change of building number,
    # state or ZIP; those require an independently verified address correction.
    source_number = re.match(r"[\w-]+", census_street(address["street"]) or "")
    match_number = re.match(r"[\w-]+", result[4])
    if not source_number or not match_number or source_number[0].replace("-", "") != match_number[0].replace("-", ""):
        return None
    try:
        lon, lat = result[5].split(",")
        lat, lon = parse_coordinates(f"{lat},{lon}")
    except ValueError:
        return None
    if lat is None or misplaced_in_brazil(f"{lat},{lon}", row["corp_address"]):
        return None
    return {"lat": lat, "lon": lon, "precision": "street", "source": "U.S. Census Geocoder · Public_AR_Current",
            "source_url": CENSUS_URL, "matched_address": result[4],
            "reason": f"Census {result[3]} match; building number, state and ZIP agree. Coordinates are interpolated along the street."}


def apply(database, directory, exceptions=None):
    rows = candidates(database)
    reference = postal_reference(directory)
    census = {}
    result_path = directory / "census-results.csv"
    if result_path.exists():
        with result_path.open(newline="") as source:
            census = {row[0]: row for row in csv.reader(source) if row}
    exceptions = json.loads(Path(exceptions).read_text()) if exceptions else {}
    target = correction_path(database)
    existing = json.loads(target.read_text()) if target.exists() else {"version": 1, "corrections": {}}
    audit = []
    for row in rows:
        address = split_address(row["corp_address"])
        result = census_match(row, address, census.get(str(row["id"]))) if address else None
        if not result and address:
            result = postal_match(address, reference)
        exception = exceptions.get(str(row["filer_ein"]))
        if exception and exception.get("original_address") == row["corp_address"]:
            corrected = exception["address"]
            manual_result = postal_match(corrected, reference, exception.get("country"))
            if manual_result:
                result = manual_result
                result["matched_address"] = exception["display_address"]
                result["address_source_url"] = exception["source_url"]
                result["reason"] += " Mailing/physical address checked against the organization's own website."
        if not result:
            result = {"lat": None, "lon": None, "precision": "unresolved", "source": "", "source_url": "",
                      "matched_address": "", "reason": "No sufficiently reliable address or postal match was found."}
        correction = {"filer_ein": str(row["filer_ein"]), "filer_name": row["filer_name"],
                      "original_address": row["corp_address"], "original_geo_loc": row["geo_loc"], **result}
        existing["corrections"][str(row["id"])] = correction
        audit.append({"filing_id": row["id"], **correction})
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing["summary"] = dict(Counter(row["precision"] for row in audit))
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as source:
            json.dump(existing, source, indent=2, ensure_ascii=False)
            source.write("\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    (directory / "correction-audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    print(f"Saved {len(audit)} corrections to {target.name}: {existing['summary']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "apply"])
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("GLOWSEARCH_DB", Path(__file__).resolve().parent / "output_two.db")))
    parser.add_argument("--directory", type=Path, default=Path(".location-repair"))
    parser.add_argument("--exceptions", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.db, args.directory)
    else:
        apply(args.db, args.directory, args.exceptions)
