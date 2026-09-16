"""Recover locations for filings that have neither an address nor coordinates.

Their IRS mailing addresses are fetched from the ProPublica Nonprofit Explorer
by EIN, geocoded through the same validated Census/GeoNames pipeline as the
other repairs, and stored in the location-corrections overlay. Only the
`fetch` and `resolve` commands make network requests; `prepare` and `apply`
are local. `resolve` is a last-resort OpenStreetMap Nominatim lookup for the
few rows the local references cannot place.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import ssl
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from geography import state_check, state_distance
from locations import US_CODES, correction_path, parse_coordinates
from postal import mailing_state, mailing_zip
from repair_locations import CENSUS_URL, POSTAL_URL, census_match, census_street, postal_match, postal_reference

API_URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{}.json"
ORG_URL = "https://projects.propublica.org/nonprofits/organizations/{}"
CACHE_NAME = "propublica-addresses.json"
INPUT_NAME = "census-input-nocoords.csv"
RESULTS_NAME = "census-results-nocoords.csv"
# Coastal postal centroids can sit just offshore of simplified land boundaries.
POSTAL_OFFSHORE_DEGREES = 0.1


def addressless(database):
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT rowid AS id, filer_ein, filer_name FROM filings "
            "WHERE (corp_address IS NULL OR TRIM(corp_address) = '') "
            "AND (geo_loc IS NULL OR TRIM(geo_loc) = '')")]
    finally:
        conn.close()


def tls_context():
    """Framework Python builds on macOS ship without CA roots; use the system bundle."""
    context = ssl.create_default_context()
    if not context.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():
        context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return context


def fetch(database, directory):
    """Download ProPublica mailing addresses; resumable through its JSON cache."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cache_path = directory / CACHE_NAME
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    cache = {ein: entry for ein, entry in cache.items() if entry.get("status") != "error"}
    rows = [row for row in addressless(database) if str(row["filer_ein"]).replace("-", "").zfill(9) not in cache]
    context = tls_context()
    for index, row in enumerate(rows, 1):
        ein = str(row["filer_ein"]).replace("-", "").zfill(9)
        try:
            request = urllib.request.Request(API_URL.format(ein), headers={"User-Agent": "GlowSearch location repair"})
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                organization = json.load(response).get("organization") or {}
            entry = {"status": "ok", "name": organization.get("name"),
                     "address": organization.get("address"), "city": organization.get("city"),
                     "state": organization.get("state"), "zipcode": organization.get("zipcode")}
        except urllib.error.HTTPError as error:
            entry = {"status": "not_found" if error.code == 404 else "error", "http_status": error.code}
        except Exception as error:  # network failure: keep the row retryable
            entry = {"status": "error", "error": str(error)}
        cache[ein] = entry
        if index % 25 == 0 or index == len(rows):
            cache_path.write_text(json.dumps(cache, indent=1) + "\n")
            print(f"Fetched {index}/{len(rows)}", flush=True)
        time.sleep(0.12)
    print(f"Cached {len(cache)} ProPublica records at {cache_path}")


def usable_address(entry):
    if entry.get("status") != "ok":
        return None
    state = str(entry.get("state") or "").upper()
    zipcode = str(entry.get("zipcode") or "")[:5]
    if state not in US_CODES or len(zipcode) != 5 or not zipcode.isdigit() or zipcode == "00000":
        return None
    if not entry.get("address") or not entry.get("city"):
        return None
    return {"street": entry["address"].strip(), "city": entry["city"].strip(), "state": state, "zip": zipcode}


COUNTRY_CODES = {
    "antigua & barbuda": "AG", "australia": "AU", "austria": "AT", "bangladesh": "BD",
    "belgium": "BE", "belize": "BZ", "bermuda": "BM", "canada": "CA", "china": "CN",
    "costa rica": "CR", "cote d'ivoire": "CI", "czech republic": "CZ", "france": "FR",
    "germany": "DE", "greece": "GR", "guatemala": "GT", "haiti": "HT", "hong kong": "HK",
    "india": "IN", "indonesia": "ID", "ireland": "IE", "israel": "IL", "italy": "IT", "japan": "JP",
    "kenya": "KE", "lebanon": "LB", "madagascar": "MG", "mexico": "MX", "netherlands": "NL",
    "new zealand": "NZ", "nigeria": "NG", "philippines": "PH", "poland": "PL",
    "republic of korea": "KR", "south africa": "ZA", "sweden": "SE", "switzerland": "CH",
    "taiwan": "TW", "thailand": "TH", "uganda": "UG", "united arab emirates": "AE",
    "united kingdom": "GB",
}

POSTAL_PATTERNS = [
    re.compile(r"[A-Z]\d[A-Z]\s?\d[A-Z]\d"),              # Canada
    re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"),  # United Kingdom
    re.compile(r"\b\d{3}-\d{4}\b"),                        # Japan
    re.compile(r"\b\d{2}-\d{3}\b"),                        # Poland
    re.compile(r"\b\d{4,7}\b"),                            # generic numeric postcodes
]


def normalize_key(text):
    decomposed = unicodedata.normalize("NFKD", str(text or "").upper())
    return re.sub(r"[^A-Z0-9]", "", decomposed)


def name_keys(tokens):
    """All keys for a token sequence, expanding common address abbreviations."""
    tokens = [normalize_key(token) for token in tokens if normalize_key(token)]
    variants = [tokens]
    for index, token in enumerate(tokens):
        for short, full in (("ST", "SAINT"), ("MT", "MOUNT"), ("FT", "FORT")):
            if token == short or token == full:
                variants.append(tokens[:index] + [full if token == short else short] + tokens[index + 1:])
    return {"".join(variant) for variant in variants}


def world_reference(reference, directory):
    """Normalized postal index plus a per-country city index (postal places + cities500)."""
    postal, cities = {}, {}
    for (country, code), places in reference.items():
        keys = {normalize_key(code)}
        # French business mail uses "74301 CEDEX" for the same commune as 74300.
        bare = normalize_key(re.sub(r"\s+CEDEX\b.*$", "", str(code), flags=re.I))
        if bare:
            keys.add(bare)
        for key in keys:
            postal.setdefault((country, key), []).extend(places)
        for place in places:
            for key in name_keys(re.findall(r"[A-Za-z]+", str(place["city"]).upper())):
                cities.setdefault(country, {}).setdefault(key, {"lat": place["lat"], "lon": place["lon"], "weight": 1})
    cities_zip = Path(directory) / "cities500.zip"
    if cities_zip.exists():
        with zipfile.ZipFile(cities_zip) as archive:
            for line in archive.read("cities500.txt").decode("utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) < 15:
                    continue
                lat, lon = parse_coordinates(f"{parts[4]},{parts[5]}")
                if lat is None:
                    continue
                try:
                    population = int(parts[14] or 0)
                except ValueError:
                    population = 0
                index = cities.setdefault(parts[8], {})
                for name in {parts[1], parts[2]}:  # name and ASCII name
                    for key in name_keys(re.findall(r"[A-Za-z]+", name.upper())):
                        if key and (key not in index or population > index[key]["weight"]):
                            index[key] = {"lat": lat, "lon": lon, "weight": population}
    return postal, cities


def foreign_locate(text, country_name, postal, cities):
    """Place a foreign address at its postal area, or failing that its city area."""
    code = COUNTRY_CODES.get(str(country_name or "").strip().casefold())
    text = str(text or "").strip()
    if not code or not text:
        return None
    upper = text.upper()
    for pattern in POSTAL_PATTERNS:
        for found in pattern.findall(upper):
            key = normalize_key(found)
            candidates = [key] + ([key[:3]] if code == "CA" and len(key) > 3 else [])
            for candidate in candidates:
                places = postal.get((code, candidate))
                if places:
                    return {"lat": sum(p["lat"] for p in places) / len(places),
                            "lon": sum(p["lon"] for p in places) / len(places),
                            "precision": "postal"}
    index = cities.get(code, {})
    tokens = re.findall(r"[A-Za-z]+", upper)
    # Longest match first; within a size, the earliest token wins. Addresses
    # name the most specific locality first ("SANDYFORD DUBLIN 17"), while a
    # later token is usually the parent city or region.
    for size in range(min(4, len(tokens)), 0, -1):
        for start in range(len(tokens) - size + 1):
            for key in name_keys(tokens[start:start + size]):
                place = index.get(key)
                if place:
                    return {"lat": place["lat"], "lon": place["lon"], "precision": "city"}
    # Spaceless names (LE GRAND-SACONNEX written GRANDSACONNEX) only match by
    # containment; require length so short names cannot cause false positives.
    best = None
    for token in tokens:
        key = normalize_key(token)
        if len(key) < 6:
            continue
        for name, place in index.items():
            if len(name) >= 6 and (key in name or name in key):
                if best is None or place["weight"] > best["weight"]:
                    best = place
    if best:
        return {"lat": best["lat"], "lon": best["lon"], "precision": "city"}
    return None


def prepare(database, directory):
    """Write a Census batch for recovered street addresses."""
    directory = Path(directory)
    cache = json.loads((directory / CACHE_NAME).read_text())
    written = 0
    with (directory / INPUT_NAME).open("w", newline="") as target:
        writer = csv.writer(target)
        for row in addressless(database):
            address = usable_address(cache.get(str(row["filer_ein"]).replace("-", "").zfill(9)) or {})
            street = census_street(address["street"]) if address else None
            if street:
                writer.writerow([row["id"], street, address["city"], address["state"], address["zip"]])
                written += 1
    print(f"Prepared {written} recovered street addresses for Census.")


def recovered_correction(row, address, result, reason):
    ein = str(row["filer_ein"]).replace("-", "").zfill(9)
    return {"filer_ein": str(row["filer_ein"]), "filer_name": row["filer_name"],
            "original_address": None, "original_geo_loc": None,
            "address_source_url": ORG_URL.format(ein), **result,
            "reason": "Address and coordinates were missing from this dataset; the IRS mailing address "
                      "was recovered from the organization's ProPublica Nonprofit Explorer record. " + reason}


def apply(database, directory, reference_dir=None):
    """Geocode recovered addresses and write them into the corrections overlay."""
    directory = Path(directory)
    reference = postal_reference(reference_dir or directory)
    cache = json.loads((directory / CACHE_NAME).read_text())
    census = {}
    results_path = directory / RESULTS_NAME
    if results_path.exists():
        with results_path.open(newline="") as source:
            census = {row[0]: row for row in csv.reader(source) if row}
    target = correction_path(database)
    overlay = json.loads(target.read_text()) if target.exists() else {"version": 1, "corrections": {}}
    postal_index, city_index = world_reference(reference, reference_dir or directory)
    counts = Counter()
    audit = []
    for row in addressless(database):
        entry = cache.get(str(row["filer_ein"]).replace("-", "").zfill(9)) or {}
        address = usable_address(entry)
        correction = None
        if address:
            display = f"{address['street']}, {address['city']}, {address['state']} {address['zip']}"
            result = census_match({"corp_address": display}, address, census.get(str(row["id"])))
            if result and state_check(result["lat"], result["lon"], address["state"]) not in ("inside", "near_boundary"):
                result = None  # A geocode outside its own matched state is not a repair.
            if result:
                correction = recovered_correction(row, address, result, result["reason"])
                correction["matched_address"] = display
            else:
                result = postal_match(address, reference)
                if result:
                    distance = state_distance(result["lat"], result["lon"], address["state"])
                    if distance is None or distance <= POSTAL_OFFSHORE_DEGREES:
                        correction = recovered_correction(row, address, result, result["reason"])
        else:
            found = foreign_locate(entry.get("address"), entry.get("city"), postal_index, city_index)
            if found:
                country = str(entry.get("city") or "").strip()
                display = ", ".join(part for part in [str(entry.get("address") or "").strip(), country] if part)
                area = "postal area" if found["precision"] == "postal" else "city area"
                result = {**found, "source": "GeoNames places/postal dataset (CC BY 4.0)", "source_url": POSTAL_URL,
                          "matched_address": display}
                correction = recovered_correction(
                    row, None, result,
                    f"Foreign mailing address recovered from the organization's ProPublica record; "
                    f"placed at the matching {area} from GeoNames, not a building location.")
            else:
                # A foreign-labeled record can still hold a U.S. mailing address
                # (e.g. "SEATTLE WA 98104-1610" filed under Canada). Only trust
                # it when the foreign country itself produced no location.
                us_state = mailing_state(entry.get("address"))
                us_zip = mailing_zip(entry.get("address")) if us_state else None
                if us_state and us_zip:
                    found_us = postal_match({"street": "", "city": "", "state": us_state, "zip": us_zip[:5]}, reference)
                    us_distance = found_us and state_distance(found_us["lat"], found_us["lon"], us_state)
                    if found_us and (us_distance is None or us_distance <= POSTAL_OFFSHORE_DEGREES):
                        correction = recovered_correction(
                            row, None, found_us,
                            "The ProPublica record is labeled with a foreign country but holds a U.S. mailing "
                            "address; placed at the matching U.S. postal area, not a building location.")
                        correction["matched_address"] = str(entry.get("address") or "").strip()
        if correction:
            overlay["corrections"][str(row["id"])] = correction
            audit.append({"filing_id": row["id"], **correction})
            counts[correction["precision"]] += 1
        else:
            counts["unresolved"] += 1
    overlay["updated_at"] = datetime.now(timezone.utc).isoformat()
    overlay["summary"] = {"total_corrections": len(overlay["corrections"]),
                          "last_address_recovery_run": dict(counts)}
    descriptor, temporary = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(overlay, out, indent=2, ensure_ascii=False)
            out.write("\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    supplement = directory / NOMINATIM_AUDIT
    if supplement.exists():
        audit.extend(json.loads(supplement.read_text()))
    (directory / "recovery-audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    print(f"Recovered {sum(counts[p] for p in ('street', 'postal', 'city'))} locations into {target.name}: {dict(counts)}")
    return overlay


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_AUDIT = "recovery-audit-nominatim.json"
# OpenStreetMap reports Hong Kong and Macao under China's country code.
ACCEPTED_COUNTRY_CODES = {"HK": {"HK", "CN"}, "MO": {"MO", "CN"}}
# IRS addresses with a verified misspelling: EIN -> (gazetteer name, country).
# 900474714 filed "KFAR KISHUR 25149"; the only such place in the Israeli
# gazetteer is Kishor (32.947, 35.249), inside the 2514x Karmiel/Misgav
# postal block that 25149 belongs to.
GAZETTEER_ALIASES = {"900474714": ("Kishor", "IL")}


def gazetteer_locate(query, country, directory):
    """Exact-name lookup in a downloaded GeoNames country gazetteer (dump-{CC}.zip)."""
    path = Path(directory).parent / f"dump-{country}.zip"
    if not path.exists():
        return None
    wanted = name_keys(re.findall(r"[A-Za-z]+", query.upper()))
    with zipfile.ZipFile(path) as archive:
        for line in archive.read(f"{country}.txt").decode("utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) < 15:
                continue
            for name in [parts[1], parts[2]] + parts[3].split(","):
                if name_keys(re.findall(r"[A-Za-z]+", name.upper())) & wanted:
                    lat, lon = parse_coordinates(f"{parts[4]},{parts[5]}")
                    if lat is not None:
                        return {"lat": lat, "lon": lon, "display": f"{parts[1]} (GeoNames {parts[0]})"}
    return None


def nominatim_locate(text, country_name, context):
    """One guarded Nominatim lookup; the result country must match the record."""
    query = re.sub(r"\b0{5}(?:-0{4})?\b", "", str(text or ""))
    # Bermuda-style "MA BX" sector codes confuse the search; drop them.
    query = re.sub(r"\b[A-Z]{2}\s+[A-Z0-9]{2}\b", "", query.upper())
    query = re.sub(r"\s+", " ", query).strip(" ,")
    expected = COUNTRY_CODES.get(str(country_name or "").strip().casefold())
    if not query or not expected:
        return None
    params = urllib.parse.urlencode({"format": "jsonv2", "limit": 1, "addressdetails": 1,
                                     "q": f"{query}, {country_name}"})
    request = urllib.request.Request(f"{NOMINATIM_URL}?{params}",
                                     headers={"User-Agent": "GlowSearch location repair"})
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        results = json.load(response)
    if not results:
        return None
    found = results[0]
    accepted = ACCEPTED_COUNTRY_CODES.get(expected, {expected})
    if str((found.get("address") or {}).get("country_code", "")).upper() not in accepted:
        return None
    lat, lon = parse_coordinates(f"{found.get('lat')},{found.get('lon')}")
    if lat is None:
        return None
    return {"lat": lat, "lon": lon, "display": found.get("display_name")}


def resolve(database, directory):
    """Nominatim fallback for address-less rows still unresolved after `apply`."""
    directory = Path(directory)
    cache = json.loads((directory / CACHE_NAME).read_text())
    target = correction_path(database)
    overlay = json.loads(target.read_text()) if target.exists() else {"version": 1, "corrections": {}}
    context = tls_context()
    counts = Counter()
    audit = []
    for row in addressless(database):
        if str(row["id"]) in overlay["corrections"]:
            continue
        entry = cache.get(str(row["filer_ein"]).replace("-", "").zfill(9)) or {}
        if entry.get("status") != "ok" or not str(entry.get("address") or "").strip():
            counts["no_address"] += 1
            continue
        if usable_address(entry) or str(entry.get("state") or "").upper() in US_CODES:
            # Military/PO routing addresses have no building for a geocoder to find.
            counts["skipped_us_routing"] += 1
            continue
        ein = str(row["filer_ein"]).replace("-", "").zfill(9)
        alias = GAZETTEER_ALIASES.get(ein)
        if alias:
            found_gazetteer = gazetteer_locate(alias[0], alias[1], directory)
            if found_gazetteer:
                country = str(entry.get("city") or "").strip()
                display = ", ".join(part for part in [str(entry.get("address") or "").strip(), country] if part)
                result = {"lat": found_gazetteer["lat"], "lon": found_gazetteer["lon"], "precision": "city",
                          "source": "GeoNames gazetteer (CC BY 4.0)", "source_url": POSTAL_URL,
                          "matched_address": display}
                correction = recovered_correction(
                    row, None, result,
                    "Foreign mailing address recovered from the organization's ProPublica record; the filed "
                    f"place name matches {found_gazetteer['display']} in the national gazetteer (the filed "
                    "spelling does not exist); placed at that locality, not a building location.")
                overlay["corrections"][str(row["id"])] = correction
                audit.append({"filing_id": row["id"], **correction})
                counts["city"] += 1
                print(f"Resolved filing {row['id']} via gazetteer: {display} -> {found_gazetteer['display']}", flush=True)
                continue
        found = nominatim_locate(entry["address"], entry.get("city"), context)
        time.sleep(1.1)  # Nominatim usage policy: at most one request per second
        if not found:
            counts["unresolved"] += 1
            continue
        country = str(entry.get("city") or "").strip()
        display = ", ".join(part for part in [str(entry.get("address") or "").strip(), country] if part)
        result = {"lat": found["lat"], "lon": found["lon"], "precision": "city",
                  "source": "OpenStreetMap contributors (ODbL) via Nominatim",
                  "source_url": "https://www.openstreetmap.org/copyright", "matched_address": display}
        correction = recovered_correction(
            row, None, result,
            "Foreign mailing address recovered from the organization's ProPublica record; placed at the "
            "matching area returned by OpenStreetMap Nominatim, not a building location.")
        correction["nominatim_display"] = found["display"]
        overlay["corrections"][str(row["id"])] = correction
        audit.append({"filing_id": row["id"], **correction})
        counts["city"] += 1
        print(f"Resolved filing {row['id']}: {display} -> {found['display']}", flush=True)
    overlay["updated_at"] = datetime.now(timezone.utc).isoformat()
    overlay.setdefault("summary", {})["total_corrections"] = len(overlay["corrections"])
    overlay["summary"]["last_nominatim_run"] = dict(counts)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(overlay, out, indent=2, ensure_ascii=False)
            out.write("\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    # Keep entries from previous resolve runs; this run only covers leftovers.
    supplement = directory / NOMINATIM_AUDIT
    if supplement.exists():
        fresh = {entry["filing_id"] for entry in audit}
        audit = [entry for entry in json.loads(supplement.read_text()) if entry["filing_id"] not in fresh] + audit
    supplement.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    print(f"Nominatim resolved {counts['city']} filings: {dict(counts)}")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "prepare", "apply", "resolve"])
    parser.add_argument("--db", type=Path,
                        default=Path(os.environ.get("GLOWSEARCH_DB", Path(__file__).resolve().parent / "output_two.db")))
    parser.add_argument("--directory", type=Path, default=Path(".location-repair/nationwide"))
    parser.add_argument("--reference", type=Path, default=Path(".location-repair"))
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.db, args.directory)
    elif args.command == "prepare":
        prepare(args.db, args.directory)
    elif args.command == "apply":
        apply(args.db, args.directory, args.reference)
    else:
        resolve(args.db, args.directory)


if __name__ == "__main__":
    main()
