"""Build an optional, disposable substring index without changing source data.

Usage: python build_index.py [path/to/output_two.db]
The app detects the completed index automatically. Re-run after replacing data.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

INDEX_VERSION = 1


def fingerprint(path):
    stat = path.stat()
    return json.dumps([INDEX_VERSION, str(path.resolve()), stat.st_size, stat.st_mtime_ns])


def index_path(path):
    return path.with_name(path.stem + ".search.db")


def index_text(row):
    values = [str(row[key] or "") for key in row.keys() if key != "id"]
    values.append(str(row["filer_ein"] or "").replace("-", "").zfill(9))
    try:
        officials = json.loads(row["officials_json"] or "[]") if "officials_json" in row.keys() else []
        if isinstance(officials, list):
            values.extend(str(person.get("n") or "") for person in officials if isinstance(person, dict))
    except (ValueError, TypeError):
        pass
    for key in ("ceo_home1", "shell_game"):
        if key not in row.keys():
            continue
        raw = row[key]
        try:
            data = json.loads(raw or "null")
            raw = data.get("phone", "") if isinstance(data, dict) else ""
        except (ValueError, TypeError):
            pass
        values.append(re.sub(r"\D", "", str(raw or "")))
    # Nulls terminate FTS text. They cannot be queried through the API, but
    # replacing them here preserves searchable data later in the same record.
    return "\n".join(values).replace("\x00", " ")


def build(path, progress=print):
    path = Path(path).expanduser().resolve()
    original = fingerprint(path)
    started = time.monotonic()
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = index_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=target.stem + ".", suffix=".db", dir=target.parent)
    os.close(descriptor)
    index = sqlite3.connect(temporary)
    try:
        columns = {row["name"] for row in source.execute("PRAGMA table_info(filings)")}
        if not {"filer_ein", "filer_name"} <= columns:
            raise ValueError("The source needs a filings table with filer_ein and filer_name columns.")
        fields = [field for field in ("filer_ein", "filer_name", "corp_address", "corp_description", "ceo_home1", "shell_game", "officials_json") if field in columns]
        index.execute("CREATE VIRTUAL TABLE search_fts USING fts5(body, tokenize='trigram', content='')")
        index.execute("CREATE TABLE metadata (fingerprint TEXT NOT NULL)")
        cursor = source.execute("SELECT rowid AS id, " + ", ".join(fields) + " FROM filings ORDER BY rowid")
        count = 0
        while rows := cursor.fetchmany(500):
            index.executemany("INSERT INTO search_fts(rowid, body) VALUES (?, ?)",
                              ((row["id"], index_text(row)) for row in rows))
            count += len(rows)
            if count % 10_000 == 0:
                index.commit()
                progress(f"Indexed {count:,} filings…")
        progress("Optimizing index…")
        index.execute("INSERT INTO search_fts(search_fts) VALUES ('optimize')")
        if fingerprint(path) != original:
            raise RuntimeError("The database changed during indexing. Run the command again.")
        index.execute("INSERT INTO metadata VALUES (?)", [original])
        index.commit()
        index.close()
        os.replace(temporary, target)
        progress(f"Ready: {count:,} filings in {time.monotonic() - started:.1f}s. Index: {target.name}")
        return target
    finally:
        source.close()
        index.close()
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GLOWSEARCH_DB", Path(__file__).resolve().parent / "output_two.db"))
