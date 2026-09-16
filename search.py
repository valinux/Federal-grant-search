"""Queries with fixed response sizes, rowid pagination, and execution deadlines."""

import json
import math
import re
import sqlite3
import time
from array import array
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from build_index import fingerprint, index_path
from locations import correction_path, corrected_entries, location_resolver, parse_coordinates
from postal import US_CODES, mailing_state, mailing_zip, normalize_zip

PAGE_SIZE = 24
MAP_COLUMNS = 32
MAP_ROWS = 16
MAP_NODE_LIMIT = MAP_COLUMNS * MAP_ROWS
SUMMARY_FIELDS = ("filer_ein", "filer_name", "corp_address", "tax_year", "govt_amt", "receipt_amt", "contrib_amt", "corp_description")
DETAIL_FIELDS = SUMMARY_FIELDS + ("officials_json", "ceo_home1", "shell_game", "serialized_graph", "xml_name", "geo_loc")


class SearchError(ValueError):
    pass


class DatabaseUnavailable(SearchError):
    pass


def parse_search(args, fields):
    query = args.get("q", "").strip()
    field = args.get("field", "all")
    if len(query) > 200:
        raise SearchError("Keep your search under 200 characters.")
    if any(ord(character) < 32 for character in query):
        raise SearchError("Use a search without control characters.")
    if field not in fields:
        raise SearchError("Choose a valid search field.")
    try:
        after = int(args.get("after", "0"))
        if not 0 <= after <= 9_223_372_036_854_775_807:
            raise ValueError
    except (ValueError, TypeError):
        raise SearchError("Invalid page cursor.") from None
    return query, field, after


def parse_page(value):
    if value is None:
        return None
    try:
        page = int(value)
        if not 1 <= page <= 1_000_000:
            raise ValueError
        return page
    except (TypeError, ValueError):
        raise SearchError("Choose a valid page number.") from None


def parse_bounds(bbox):
    if not bbox:
        return None
    try:
        west, south, east, north = [float(value) for value in bbox.split(",")]
        if not all(math.isfinite(value) for value in (west, south, east, north)) or not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError
        return west, south, east, north
    except (ValueError, TypeError, AttributeError):
        raise SearchError("Invalid map area. Reset the map and try again.") from None


def file_version(path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def data_version(path):
    return (file_version(path), file_version(Path(str(path) + "-wal")),
            file_version(index_path(path)), file_version(correction_path(path)))


def project(lat, lon):
    latitude = math.radians(max(-85.05112878, min(85.05112878, lat)))
    return (lon + 180) / 360, (1 - math.asinh(math.tan(latitude)) / math.pi) / 2


@dataclass
class MapPoints:
    ids: array
    lat: array
    lon: array
    x: array
    y: array
    precision: bytearray
    total_matching: int

    def visible(self, bounds):
        if bounds is None:
            return range(len(self.ids))
        west, south, east, north = bounds
        return [i for i in range(len(self.ids)) if west <= self.lon[i] <= east and south <= self.lat[i] <= north]


def json_value(value, expected, default):
    try:
        parsed = json.loads(value or "null")
        return parsed if isinstance(parsed, expected) else default
    except (ValueError, TypeError):
        return default


def coordinates(value):
    return parse_coordinates(value)


def number(value):
    try:
        result = float(str(value).replace(",", "").replace("$", ""))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SearchStore:
    def __init__(self, path, timeout=15.0):
        self.path = Path(path).expanduser().resolve()
        self.timeout = timeout

    @contextmanager
    def connect(self):
        if not self.path.is_file():
            raise DatabaseUnavailable("Your database is not connected yet. Place output_two.db in the GlowSearch folder, then try again.")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + self.timeout
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.create_function("phone_digits", 1, lambda value: re.sub(r"\D", "", str(value or "")), deterministic=True)
        conn.create_function("mailing_zip", 1, mailing_zip, deterministic=True)
        conn.create_function("mailing_state", 1, mailing_state, deterministic=True)
        resolve = location_resolver(self.path)

        def corrected_mailing(rid, geo, address, ein, extractor):
            # A validated correction carries the verified address; the source
            # row's own address text may be missing or even wrong.
            location = resolve(rid, geo, address, ein)
            if location["precision"] in ("street", "postal", "city"):
                return extractor(location["matched_address"])
            return extractor(address)

        conn.create_function("filing_state", 4, lambda rid, geo, address, ein: corrected_mailing(rid, geo, address, ein, mailing_state), deterministic=True)
        conn.create_function("filing_zip", 4, lambda rid, geo, address, ein: corrected_mailing(rid, geo, address, ein, mailing_zip), deterministic=True)
        conn.create_function("latitude", 1, lambda value: coordinates(value)[0], deterministic=True)
        conn.create_function("longitude", 1, lambda value: coordinates(value)[1], deterministic=True)
        conn.create_function("map_latitude", 4, lambda *values: resolve(*values)["lat"], deterministic=True)
        conn.create_function("map_longitude", 4, lambda *values: resolve(*values)["lon"], deterministic=True)
        conn.create_function("map_precision", 4, lambda *values: resolve(*values)["precision"], deterministic=True)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def columns(conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(filings)")}
        if not {"filer_name", "filer_ein"} <= columns:
            raise DatabaseUnavailable("This database needs a filings table with filer_name and filer_ein columns.")
        return columns

    def indexed_where(self, conn, query, field, columns):
        where, params = self.where(query, field, columns)
        target = index_path(self.path)
        if not target.is_file():
            return where, params
        # FTS supplies candidates only. The original predicate remains the
        # authority, so literal wildcards, fields, and JSON matching stay intact.
        if field == "state":
            # Two-letter codes are too short for the trigram index; the exact
            # mailing-state predicate scans the address column directly.
            terms = []
        elif field == "zip_code":
            # Use the base ZIP to find candidates regardless of the source's
            # ZIP+4 separator. The parsed postal predicate enforces exactness.
            zipcode = normalize_zip(query)
            # Rows whose addresses were recovered into the correction overlay
            # are not in the FTS index; scan directly when one could match.
            recovered = any(
                correction.get("original_address") is None
                and (mailing_zip(correction.get("matched_address") or "") or "")[:5] == zipcode[:5]
                for correction in corrected_entries(self.path).values())
            terms = [] if recovered else [zipcode[:5]]
        elif field == "address":
            terms = [word for word in query.split() if len(word) >= 3]
        elif field in ("phone", "ein"):
            normalized = re.sub(r"\D", "", query) if field == "phone" else query.replace("-", "")
            terms = [normalized] if len(normalized) >= 3 else []
        else:
            terms = [query] if len(query) >= 3 else []
        if not terms:
            return where, params
        expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
        if field == "all" and len(re.sub(r"\D", "", query)) >= 7:
            expression = "(" + expression + ' OR "' + re.sub(r"\D", "", query) + '")'
        try:
            conn.execute("ATTACH DATABASE ? AS search_cache", [target.as_uri() + "?mode=ro"])
            metadata = conn.execute("SELECT fingerprint FROM search_cache.metadata").fetchone()
            if not metadata or metadata[0] != fingerprint(self.path):
                return where, params
            # Guard against incomplete/corrupt caches; the source is sufficient.
            conn.execute("SELECT rowid FROM search_cache.search_fts WHERE search_fts MATCH ? LIMIT 0", [expression])
        except sqlite3.Error:
            return where, params
        return f"rowid IN (SELECT rowid FROM search_cache.search_fts WHERE search_fts MATCH ?) AND ({where})", [expression, *params]

    @staticmethod
    def selection(fields, columns, detail=False):
        # Identifiers come only from fixed field lists, never request input.
        return ", ".join(f'substr("{field}", 1, {262144 if detail else 800}) AS "{field}"'
                         if field in columns else f'NULL AS "{field}"' for field in fields)

    @staticmethod
    def where(query, field, columns):
        def contains(names, text):
            names = [name for name in names if name in columns]
            return ("(" + " OR ".join(f'"{name}" LIKE ? ESCAPE \'\\\'' for name in names) + ")", [like(text)] * len(names)) if names else ("0", [])

        def officials():
            if "officials_json" not in columns:
                return "0", []
            return ("EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(officials_json) THEN officials_json ELSE '[]' END) AS officer "
                    "WHERE CASE WHEN officer.type = 'object' THEN json_extract(officer.value, '$.n') END LIKE ? ESCAPE '\\')", [like(query)])

        def phone():
            digits = re.sub(r"\D", "", query)
            if len(digits) < 3:
                return "0", []
            parts, params = [], []
            for name in ("ceo_home1", "shell_game"):
                if name in columns:
                    parts.append(f"phone_digits(CASE WHEN json_valid({name}) THEN json_extract({name}, '$.phone') ELSE {name} END) LIKE ?")
                    params.append(f"%{digits}%")
            return ("(" + " OR ".join(parts) + ")", params) if parts else ("0", [])

        if field == "ein":
            # Original database stores EINs as integers, losing leading zeros.
            return "printf('%09d', replace(filer_ein, '-', '')) LIKE ? ESCAPE '\\'", [like(query.replace("-", ""))]
        if field == "officials":
            return officials()
        if field == "phone":
            return phone()
        if field == "state":
            code = query.upper()
            if code not in US_CODES:
                raise SearchError("Use a two-letter U.S. state or territory code, such as NJ or CA.")
            if "corp_address" not in columns:
                return "0", []
            # Only the postal position counts: street text like ", FLOOR 2" or
            # an unparseable trailing country must not create a state match.
            # Corrections carry verified addresses for rows whose source
            # address is missing or wrong.
            geo = "geo_loc" if "geo_loc" in columns else "NULL"
            return f"filing_state(rowid, {geo}, corp_address, filer_ein) = ?", [code]
        if field == "zip_code":
            try:
                zipcode = normalize_zip(query)
            except ValueError as error:
                raise SearchError(str(error)) from None
            if "corp_address" not in columns:
                return "0", []
            geo = "geo_loc" if "geo_loc" in columns else "NULL"
            postal = f"filing_zip(rowid, {geo}, corp_address, filer_ein)"
            if len(zipcode) == 5:
                postal = f"substr({postal}, 1, 5)"
            return f"{postal} = ?", [zipcode]
        if field == "address":
            parts, params = [], []
            for word in query.split():
                sql, values = contains(("corp_address", "shell_game"), word)
                parts.append(sql)
                params.extend(values)
            return " AND ".join(parts) or "0", params
        if field != "all":
            names = {"corporation_name": "filer_name", "founder": "ceo_home1", "corp_description": "corp_description"}
            return contains((names[field],), query)

        sql, params = contains(("filer_name", "filer_ein", "corp_address", "corp_description", "ceo_home1", "shell_game"), query)
        officer_sql, officer_params = officials()
        clauses = [sql, officer_sql]
        params.extend(officer_params)
        if re.fullmatch(r"\d{9}|\d{2}-\d{7}", query):
            clauses.append("printf('%09d', replace(filer_ein, '-', '')) = ?")
            params.append(query.replace("-", ""))
        if len(re.sub(r"\D", "", query)) >= 7:
            phone_sql, phone_params = phone()
            clauses.append(phone_sql)
            params.extend(phone_params)
        return "(" + " OR ".join(clauses) + ")", params

    @staticmethod
    def summary(row):
        result = dict(row)
        if result.get("filer_ein"):
            result["filer_ein"] = str(result["filer_ein"]).replace("-", "").zfill(9)
        for field in ("govt_amt", "receipt_amt", "contrib_amt"):
            if field in result:
                result[field] = number(result[field])
        return result

    def matching_ids(self, query, field):
        return cached_ids(str(self.path), data_version(self.path), self.timeout, query, field)

    def map_points(self, query, field):
        return cached_points(str(self.path), data_version(self.path), self.timeout, query, field)

    def rows_for_ids(self, ids, fields=SUMMARY_FIELDS):
        if not ids:
            return []
        with self.connect() as conn:
            columns = self.columns(conn)
            rows = conn.execute(f"SELECT rowid AS id, {self.selection(fields, columns)} FROM filings "
                                f"WHERE rowid IN ({','.join('?' for _ in ids)}) ORDER BY rowid", list(ids)).fetchall()
        return [self.summary(row) for row in rows]

    def search(self, query, field="all", after=0, page=None, bbox=None):
        bounds = parse_bounds(bbox)
        if not query:
            return {"results": [], "next_cursor": None, "has_more": False, "limit": PAGE_SIZE,
                    "total": 0, "total_pages": 0, "page": 1, "start": 0, "end": 0}
        started = time.monotonic()
        if bounds is None:
            ids = self.matching_ids(query, field)
        else:
            points = self.map_points(query, field)
            ids = array('q', (points.ids[i] for i in points.visible(bounds)))
        total = len(ids)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if page is None:
            start = bisect_right(ids, after)
            current_page = start // PAGE_SIZE + 1
        else:
            current_page = min(max(1, page), max(1, total_pages))
            start = (current_page - 1) * PAGE_SIZE
        results = self.rows_for_ids(ids[start:start + PAGE_SIZE])
        more = start + len(results) < total
        return {"results": results, "has_more": more, "next_cursor": results[-1]["id"] if more else None,
                "total": total, "total_pages": total_pages, "page": current_page,
                "start": start + 1 if results else 0, "end": start + len(results),
                "limit": PAGE_SIZE, "elapsed_ms": round((time.monotonic() - started) * 1000)}

    def map_search(self, query, field="all", bbox=None):
        bounds = parse_bounds(bbox)
        if not query:
            return {"results": [], "total_matching": 0, "total_mapped": 0, "visible_count": 0,
                    "missing_coordinates": 0, "approximate_count": 0, "bounds": None, "has_more": False}
        points = self.map_points(query, field)
        visible = points.visible(bounds)
        response = {"total_matching": points.total_matching, "total_mapped": len(points.ids),
                    "missing_coordinates": points.total_matching - len(points.ids), "visible_count": len(visible),
                    "approximate_count": sum(points.precision[i] >= 2 for i in visible), "has_more": False,
                    "results": [], "bounds": None}
        if not visible:
            return response
        west = min(points.lon[i] for i in visible)
        east = max(points.lon[i] for i in visible)
        south = min(points.lat[i] for i in visible)
        north = max(points.lat[i] for i in visible)
        response["bounds"] = [west, south, east, north]
        left, top = project(north, west)
        right, bottom = project(south, east)
        width, height = max(right - left, 1e-12), max(bottom - top, 1e-12)
        groups = {}
        for i in visible:
            x = max(0, min(MAP_COLUMNS - 1, int((points.x[i] - left) / width * MAP_COLUMNS)))
            y = max(0, min(MAP_ROWS - 1, int((points.y[i] - top) / height * MAP_ROWS)))
            key = x, y
            lat, lon = points.lat[i], points.lon[i]
            if key not in groups:
                groups[key] = {"count": 0, "lat": 0, "lon": 0, "bounds": [lon, lat, lon, lat], "id": points.ids[i]}
            group = groups[key]
            group["count"] += 1
            group["lat"] += lat
            group["lon"] += lon
            group["bounds"] = [min(group["bounds"][0], lon), min(group["bounds"][1], lat),
                               max(group["bounds"][2], lon), max(group["bounds"][3], lat)]
        singles = {group["id"] for group in groups.values() if group["count"] == 1}
        fields = ("filer_ein", "filer_name", "corp_address", "tax_year", "govt_amt")
        rows = {row["id"]: row for row in self.rows_for_ids(singles, fields)}
        for group in groups.values():
            group["lat"] /= group["count"]
            group["lon"] /= group["count"]
            if group["count"] == 1:
                row = rows[group["id"]]
                # Summary formatting pads EINs; look up precision from the cached
                # corrected point instead of attempting to rematch a source EIN.
                source_index = bisect_right(points.ids, group["id"]) - 1
                precision = ("original", "street", "postal", "city")[points.precision[source_index]]
                group.update(row, kind="filing", location_precision=precision)
            else:
                group["kind"] = "cluster"
                group.pop("id")
            response["results"].append(group)
        return response

    def detail(self, filing_id):
        with self.connect() as conn:
            columns = self.columns(conn)
            row = conn.execute(f"SELECT rowid AS id, {self.selection(DETAIL_FIELDS, columns, detail=True)} FROM filings WHERE rowid = ?", [filing_id]).fetchone()
        if row is None:
            return None
        location = location_resolver(self.path)(row["id"], row["geo_loc"], row["corp_address"], row["filer_ein"])
        result = self.summary(row)
        result["location"] = location
        result["geo_loc"] = f"{location['lat']},{location['lon']}" if location["lat"] is not None else None
        officers = json_value(result.pop("officials_json"), list, [])
        result["officers"] = [{"name": str(person.get("n") or "")[:200], "title": str(person.get("t") or "")[:200]}
                              for person in officers if isinstance(person, dict)][:100]
        result["officers_truncated"] = len(officers) > 100
        for field in ("ceo_home1", "shell_game"):
            result[field] = json_value(result[field], dict, {})
        ein = re.sub(r"\D", "", str(result["filer_ein"] or ""))
        result["source_url"] = f"https://projects.propublica.org/nonprofits/organizations/{ein}" if len(ein) == 9 else None
        xml = str(result.pop("xml_name") or "")
        result["filing_url"] = f"{result['source_url']}/{xml}/full" if result["source_url"] and re.fullmatch(r"[\w-]+", xml) else None
        graph = result.pop("serialized_graph") or ""
        result["graph_url"] = "https://datarepublican.com/expose/?" + urlencode({"custom_graph": graph, "title": f"Charity graph: {result['filer_name']}"}) if graph and len(graph) < 6000 else None
        return result


@lru_cache(maxsize=8)
def cached_ids(path, version, timeout, query, field):
    """Cache compact IDs, never complete filing payloads or database connections."""
    store = SearchStore(path, timeout)
    with store.connect() as conn:
        columns = store.columns(conn)
        where, params = store.indexed_where(conn, query, field, columns)
        return array('q', (row[0] for row in conn.execute(f"SELECT rowid FROM filings WHERE ({where}) ORDER BY rowid", params)))


@lru_cache(maxsize=4)
def cached_points(path, version, timeout, query, field):
    store = SearchStore(path, timeout)
    ids = cached_ids(path, version, timeout, query, field)
    points = MapPoints(*(array('q' if i == 0 else 'd') for i in range(5)), bytearray(), len(ids))
    resolve = location_resolver(path)
    with store.connect() as conn:
        columns = store.columns(conn)
        if "geo_loc" not in columns:
            raise SearchError("This database has no map coordinates. You can still explore its filings in List view.")
        address = "corp_address" if "corp_address" in columns else "NULL AS corp_address"
        deadline = time.monotonic() + timeout
        for offset in range(0, len(ids), 900):
            batch = ids[offset:offset + 900]
            cursor = conn.execute(f"SELECT rowid AS id, geo_loc, {address}, filer_ein FROM filings "
                                  f"WHERE rowid IN ({','.join('?' for _ in batch)}) ORDER BY rowid", list(batch))
            for row in cursor:
                location = resolve(row["id"], row["geo_loc"], row["corp_address"], row["filer_ein"])
                lat, lon = location["lat"], location["lon"]
                if lat is None:
                    continue
                x, y = project(lat, lon)
                points.ids.append(row["id"])
                points.lat.append(lat)
                points.lon.append(lon)
                points.x.append(x)
                points.y.append(y)
                points.precision.append({"original": 0, "street": 1, "postal": 2, "city": 3}[location["precision"]])
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError("interrupted")
    return points
