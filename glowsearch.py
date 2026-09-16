"""Command-line access to the same bounded search used by the web app."""

import argparse
import os
from pathlib import Path

from search import SearchStore


def main():
    parser = argparse.ArgumentParser(description="Search local nonprofit filings.")
    parser.add_argument("query", nargs="?", help="Organization, officer, EIN, or location")
    parser.add_argument("--db", default=os.environ.get("GLOWSEARCH_DB", str(Path(__file__).resolve().parent / "output_two.db")))
    parser.add_argument("--field", default="all", choices=["all", "corporation_name", "ein", "officials", "founder", "address", "zip_code", "state", "phone", "corp_description"])
    parser.add_argument("--after", type=int, default=0, help="Continue from a previous page's cursor")
    args = parser.parse_args()
    query = (args.query if args.query is not None else input("Search: ")).strip()
    if not query:
        parser.error("Enter a search query.")
    result = SearchStore(args.db).search(query, args.field, args.after)
    for row in result["results"]:
        amount = f"${row['govt_amt']:,.0f}" if row["govt_amt"] is not None else "Not reported"
        print(f"{row['filer_name']} | EIN {row['filer_ein']} | {row['tax_year'] or 'Year not reported'}")
        print(f"  {row['corp_address'] or 'Address not reported'}")
        print(f"  Government funding: {amount}\n")
    print(f"{len(result['results'])} filings on this page.")
    if result["has_more"]:
        print(f"More results available. Repeat the search with --after {result['next_cursor']}.")


if __name__ == "__main__":
    main()
