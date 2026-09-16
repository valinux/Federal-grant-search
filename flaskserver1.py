"""GlowSearch: a bounded, read-only browser for nonprofit filings."""

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, render_template, request

from search import DatabaseUnavailable, SearchError, SearchStore, parse_page, parse_search

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.config.update(DATABASE=os.environ.get("GLOWSEARCH_DB", str(ROOT / "output_two.db")),
                  QUERY_TIMEOUT=15.0, MAX_CONTENT_LENGTH=16_384)
SEARCH_FIELDS = {
    "all": "All fields", "corporation_name": "Organization name", "ein": "EIN",
    "officials": "Officer name", "founder": "Founder", "address": "Address",
    "zip_code": "ZIP code", "state": "State", "phone": "Phone number", "corp_description": "Description",
}


def store():
    return SearchStore(app.config["DATABASE"], app.config["QUERY_TIMEOUT"])


@app.route("/", methods=["GET", "POST"])
@app.route("/map", methods=["GET", "POST"])
def index():
    view = "map" if request.path == "/map" else "list"
    if request.method == "POST":
        return redirect(request.path + "?" + urlencode({"q": request.form.get("query", "").strip(),
                        "field": request.form.get("search_field", "all")}), code=303)
    return render_template("index.html", view=view, fields=SEARCH_FIELDS,
                           query=request.args.get("q", "")[:200], field=request.args.get("field", "all"))


@app.get("/api/status")
def status():
    try:
        with store().connect() as conn:
            columns = store().columns(conn)
        return jsonify(ready=True, map_available="geo_loc" in columns)
    except (SearchError, sqlite3.Error) as error:
        return jsonify(ready=False, map_available=False, message=str(error) if isinstance(error, SearchError)
                       else "The database could not be read. Check output_two.db and try again.")


@app.get("/api/search")
def search():
    query, field, after = parse_search(request.args, SEARCH_FIELDS)
    return jsonify(store().search(query, field, after, parse_page(request.args.get("page")), request.args.get("bbox")))


@app.get("/api/map")
def map_results():
    query, field, _ = parse_search(request.args, SEARCH_FIELDS)
    return jsonify(store().map_search(query, field, request.args.get("bbox")))


@app.get("/api/filings/<int:filing_id>")
def filing(filing_id):
    if not 0 < filing_id <= 9_223_372_036_854_775_807:
        return jsonify(error="This filing could not be found."), 404
    result = store().detail(filing_id)
    if result is None:
        return jsonify(error="This filing could not be found."), 404
    return jsonify(result)


@app.errorhandler(SearchError)
def search_error(error):
    return jsonify(error=str(error)), 503 if isinstance(error, DatabaseUnavailable) else 400


@app.errorhandler(sqlite3.Error)
def database_error(error):
    app.logger.warning("Database query failed: %s", error)
    if "interrupted" in str(error) or "locked" in str(error):
        return jsonify(error="This search took too long. Try a more specific name, EIN, or search field."), 503
    return jsonify(error="The database could not be read. Check that output_two.db contains a filings table."), 503


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=False)
