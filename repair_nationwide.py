"""Audit every filing's map coordinates against its mailing state and repair mismatches.

No network requests occur in this module. Boundary data comes from the local
Census cartographic extract (data/us_states_2025.json.gz, see geography.py).
GeoNames postal reference ZIPs and the Census batch response are placed in the
reference/repair directories beforehand, then applied here.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from geography import distance_km, state_check, state_distance
from locations import correction_path, location_resolver
from postal import mailing_state
from repair_locations import census_match, postal_match, postal_reference, split_address

# A coordinate this far from its mailing ZIP centroid needs independent support.
POSTAL_DISTANCE_LIMIT = 50
POSTAL_DISTANCE_LIMIT_AK = 150  # Alaskan ZIP areas can be enormous.
# Census agreeing with the original point confirms it; keep the more precise source.
CENSUS_AGREES_KM = 25
# Coastal postal centroids can sit just offshore of simplified land boundaries.
POSTAL_OFFSHORE_DEGREES = 0.1

COUNTRY_CODES = {"canada": "CA"}
FOREIGN_ADDRESS = re.compile(
    r"(?P<street>.*?)\n(?P<city>[^,\n]+),\s*(?P<province>[A-Z]{2})\s+"
    r"(?P<code>[A-Z]\d[A-Z]\s?\d[A-Z]\d)\s*,\s*(?P<country>[A-Za-z]+)\s*$", re.S)


def foreign_address(text):
    """Parse verified non-U.S. corrections such as Canadian civic addresses."""
    match = FOREIGN_ADDRESS.fullmatch(str(text or "").strip())
    if not match or match["country"].lower() not in COUNTRY_CODES:
        return None
    code = match["code"].upper().replace(" ", "")
    return {"street": " ".join(match["street"].split()), "city": match["city"].strip(),
            "state": match["province"].upper(), "zip": code[:3], "country": COUNTRY_CODES[match["country"].lower()]}


def scan(database, reference):
    """Flag filings whose usable coordinates conflict with their mailing state."""
    resolve = location_resolver(database)
    suspects = []
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT rowid AS id, filer_ein, filer_name, corp_address, geo_loc FROM filings")
        for row in rows:
            row = dict(row)
            state = mailing_state(row["corp_address"])
            if not state:
                continue  # Without a mailing state there is nothing to verify against.
            location = resolve(row["id"], row["geo_loc"], row["corp_address"], row["filer_ein"])
            lat, lon = location["lat"], location["lon"]
            status = state_check(lat, lon, state)
            address = split_address(row["corp_address"] or "")
            postal = postal_match(address, reference) if address else None
            distance = distance_km(lat, lon, postal["lat"], postal["lon"]) if postal and lat is not None else None
            limit = POSTAL_DISTANCE_LIMIT_AK if state == "AK" else POSTAL_DISTANCE_LIMIT
            issues = []
            if status in ("outside", "missing", "near_boundary"):
                issues.append(status)
            if distance is not None and distance > limit:
                issues.append("postal_distance")
            if issues:
                suspects.append({**row, "state": state, "current": location, "issues": issues})
    finally:
        conn.close()
    return suspects


def decide(suspect, census_row, exception, reference):
    """Choose a correction for one suspect, or None to keep the original coordinates."""
    issues = set(suspect["issues"])
    if issues == {"near_boundary"}:
        # Simplified cartographic coastlines and borders put some genuine points
        # just outside the polygon; with nothing else wrong, keep the original.
        return None
    state = suspect["state"]
    address_source_url = ""
    display_address = ""
    if exception:
        corrected = exception["corrected_address"]
        address_source_url = exception.get("address_source_url", "")
        display_address = ", ".join(part.strip() for part in corrected.splitlines())
        address = split_address(corrected) or foreign_address(corrected)
        row = {**suspect, "corp_address": corrected}
    else:
        address = split_address(suspect["corp_address"] or "")
        row = suspect
    if address:
        country = address.get("country", "US")
        result = census_match(row, address, census_row) if country == "US" else None
        if result and state_check(result["lat"], result["lon"], address["state"]) not in ("inside", "near_boundary"):
            result = None  # A geocode outside its own matched state is not a repair.
        if result:
            original = suspect["current"]
            if not exception and issues <= {"postal_distance", "near_boundary"} and original["lat"] is not None \
                    and distance_km(result["lat"], result["lon"], original["lat"], original["lon"]) <= CENSUS_AGREES_KM:
                return None  # Census confirms the original point; keep the more precise source.
            if address_source_url:
                result["address_source_url"] = address_source_url
                result["reason"] += " Mailing address checked against the organization's own website."
            return result
        result = postal_match(address, reference, address.get("country"))
        if result:
            distance = state_distance(result["lat"], result["lon"], address["state"])
            if country != "US" or distance is None or distance <= POSTAL_OFFSHORE_DEGREES:
                if display_address:
                    result["matched_address"] = display_address
                if address_source_url:
                    result["address_source_url"] = address_source_url
                    result["reason"] += " Mailing/physical address checked against the organization's own website."
                return result
    return {"lat": None, "lon": None, "precision": "unresolved", "source": "", "source_url": "",
            "matched_address": display_address, "address_source_url": address_source_url,
            "reason": "The original coordinates conflict with the filing's mailing state, "
                      "and no sufficiently reliable replacement was found."}


def load_census(directory):
    path = Path(directory) / "census-results.csv"
    if not path.exists():
        return {}
    with path.open(newline="") as source:
        return {row[0]: row for row in csv.reader(source) if row}


def apply(database, directory, reference_dir=None, exceptions=None):
    directory = Path(directory)
    reference = postal_reference(reference_dir or directory)
    census = load_census(directory)
    exceptions = json.loads(Path(exceptions).read_text()) if exceptions else {}
    suspects = scan(database, reference)
    target = correction_path(database)
    existing = json.loads(target.read_text()) if target.exists() else {"version": 1, "corrections": {}}
    audit, kept = [], 0
    for suspect in suspects:
        result = decide(suspect, census.get(str(suspect["id"])), exceptions.get(str(suspect["id"])), reference)
        if result is None:
            kept += 1
            continue
        correction = {"filer_ein": str(suspect["filer_ein"]), "filer_name": suspect["filer_name"],
                      "original_address": suspect["corp_address"], "original_geo_loc": suspect["geo_loc"], **result}
        existing["corrections"][str(suspect["id"])] = correction
        audit.append({"filing_id": suspect["id"], "issues": suspect["issues"], **correction})
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    run = dict(Counter(row["precision"] for row in audit))
    run["kept_original"] = kept
    existing["summary"] = {"total_corrections": len(existing["corrections"]), "last_nationwide_run": run}
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(existing, out, indent=2, ensure_ascii=False)
            out.write("\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    (directory / "correction-audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    print(f"Audited {len(suspects)} flagged filings; saved {len(audit)} corrections to {target.name}: {run}")
    return existing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "apply"])
    parser.add_argument("--db", type=Path,
                        default=Path(os.environ.get("GLOWSEARCH_DB", Path(__file__).resolve().parent / "output_two.db")))
    parser.add_argument("--directory", type=Path, default=Path(".location-repair/nationwide"))
    parser.add_argument("--reference", type=Path, default=Path(".location-repair"))
    parser.add_argument("--exceptions", type=Path)
    args = parser.parse_args()
    if args.command == "scan":
        suspects = scan(args.db, postal_reference(args.reference))
        args.directory.mkdir(parents=True, exist_ok=True)
        (args.directory / "suspects.json").write_text(json.dumps(suspects, indent=2, ensure_ascii=False) + "\n")
        summary = {"suspects": len(suspects),
                   "issues": dict(Counter(issue for row in suspects for issue in row["issues"]))}
        print(json.dumps(summary))
    else:
        apply(args.db, args.directory, args.reference, args.exceptions)


if __name__ == "__main__":
    main()
