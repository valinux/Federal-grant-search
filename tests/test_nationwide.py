import io
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
import zipfile
from pathlib import Path

from locations import location_resolver
from repair_nationwide import apply, decide, foreign_address

TRENTON = ("40.2206", "-74.7597")
REFERENCE = {
    ("US", "08608"): [{"city": "Trenton", "state": "NJ", "lat": 40.21, "lon": -74.75}],
    ("CA", "V2Y"): [{"city": "Langley Township Northwest", "state": "BC", "lat": 49.1285, "lon": -122.6236}],
}


def make_suspect(issues, geo="43.9999,-98.2828", address="10 MAIN ST\nTrenton, NJ 08608", current=None, filing_id=1):
    if current is None:
        lat, lon = (float(part) for part in geo.split(","))
        current = {"lat": lat, "lon": lon, "precision": "original"}
    return {"id": filing_id, "filer_ein": 123456789, "filer_name": "Example", "corp_address": address,
            "geo_loc": geo, "state": "NJ", "current": current, "issues": issues}


def census_row(filing_id="1", matched="10 MAIN ST, TRENTON, NJ, 08608", coords="-74.7597,40.2206", quality="Exact"):
    return [filing_id, "10 MAIN ST, Trenton, NJ, 08608", "Match", quality, matched, coords]


class DecideTests(unittest.TestCase):
    def test_near_boundary_alone_keeps_original(self):
        self.assertIsNone(decide(make_suspect(["near_boundary"]), census_row(), None, REFERENCE))

    def test_outside_state_uses_validated_census_street(self):
        result = decide(make_suspect(["outside", "postal_distance"]), census_row(), None, REFERENCE)
        self.assertEqual(result["precision"], "street")
        self.assertAlmostEqual(result["lat"], 40.2206)

    def test_census_point_outside_its_own_matched_state_is_rejected(self):
        # Census returns lon/lat in column 5; a NY coordinate for an NJ address
        # must not become the repair.
        row = census_row(coords="-73.99,40.71")
        result = decide(make_suspect(["outside"]), row, None, REFERENCE)
        self.assertEqual(result["precision"], "postal")

    def test_census_agreement_keeps_more_precise_original(self):
        suspect = make_suspect(["postal_distance"], current={"lat": 40.20, "lon": -74.70, "precision": "original"})
        self.assertIsNone(decide(suspect, census_row(coords="-74.71,40.21"), None, REFERENCE))

    def test_census_disagreement_replaces_wrong_original(self):
        suspect = make_suspect(["postal_distance"], current={"lat": 39.5, "lon": -111.5, "precision": "original"})
        result = decide(suspect, census_row(coords="-74.71,40.21"), None, REFERENCE)
        self.assertEqual(result["precision"], "street")

    def test_postal_fallback_without_census(self):
        result = decide(make_suspect(["postal_distance"]), None, None, REFERENCE)
        self.assertEqual(result["precision"], "postal")
        self.assertAlmostEqual(result["lat"], 40.21)

    def test_postal_centroid_far_offshore_is_not_accepted(self):
        reference = {("US", "08608"): [{"city": "Trenton", "state": "NJ", "lat": 30.0, "lon": -60.0}]}
        result = decide(make_suspect(["postal_distance"]), None, None, reference)
        self.assertEqual(result["precision"], "unresolved")
        self.assertIsNone(result["lat"])

    def test_verified_canadian_address_uses_foreign_postal_reference(self):
        exception = {"corrected_address": "22500 UNIVERSITY DRIVE\nLangley, BC V2Y 1Y1, Canada",
                     "address_source_url": "https://example.com/contact"}
        result = decide(make_suspect(["outside", "postal_distance"]), None, exception, REFERENCE)
        self.assertEqual(result["precision"], "postal")
        self.assertAlmostEqual(result["lat"], 49.1285)
        self.assertEqual(result["matched_address"], "22500 UNIVERSITY DRIVE, Langley, BC V2Y 1Y1, Canada")
        self.assertEqual(result["address_source_url"], "https://example.com/contact")

    def test_verified_us_address_validates_census_against_correction(self):
        exception = {"corrected_address": "5843 VAN SIMMONS RD\nWauchula, FL 33873",
                     "address_source_url": "https://example.com/wishlist"}
        row = ["1", "5843 VAN SIMMONS RD, Wauchula, FL, 33873", "Match", "Exact",
               "5843 VAN SIMMONS RD, WAUCHULA, FL, 33873", "-81.677,27.581"]
        result = decide(make_suspect(["outside"]), row, exception, REFERENCE)
        self.assertEqual(result["precision"], "street")
        self.assertEqual(result["address_source_url"], "https://example.com/wishlist")

    def test_unresolved_when_nothing_is_reliable(self):
        result = decide(make_suspect(["outside"]), None, None, {})
        self.assertEqual(result["precision"], "unresolved")
        self.assertIsNone(result["lat"])

    def test_foreign_address_parser(self):
        parsed = foreign_address("22500 UNIVERSITY DRIVE\nLangley, BC V2Y 1Y1, Canada")
        self.assertEqual((parsed["city"], parsed["state"], parsed["zip"], parsed["country"]),
                         ("Langley", "BC", "V2Y", "CA"))
        self.assertIsNone(foreign_address("10 MAIN ST\nTrenton, NJ 08608"))
        self.assertIsNone(foreign_address("Rua 1\nSao Paulo, SP 01310, Brazil"))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "filings.db"
        rows = [
            ("Census Fixed", 1, "10 MAIN ST\nTrenton, NJ 08608", "43.9999,-98.2828"),
            ("Postal Fixed", 2, "20 STATE ST\nTrenton, NJ 08608", "39.6,-74.9"),
            ("Unresolvable", 3, "5 MAIN ST\nNowhere, NJ 99999", "0,0"),
            ("Clean", 4, "1 MAIN ST\nTrenton, NJ 08608", "40.21,-74.75"),
        ]
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("CREATE TABLE filings(filer_name, filer_ein, corp_address, geo_loc)")
            conn.executemany("INSERT INTO filings VALUES (?,?,?,?)", rows)
            conn.commit()
        self.repair_dir = root / "repair"
        self.repair_dir.mkdir()
        with (self.repair_dir / "census-results.csv").open("w", newline="") as out:
            out.write('1,"10 MAIN ST, Trenton, NJ, 08608",Match,Exact,"10 MAIN ST, TRENTON, NJ, 08608","-74.7597,40.2206"\n')
        self.reference_dir = root / "reference"
        self.reference_dir.mkdir()
        with zipfile.ZipFile(self.reference_dir / "US.zip", "w") as archive:
            archive.writestr("US.txt", "US\t08608\tTrenton\tNew Jersey\tNJ\tMercer\t\t\t\t40.21\t-74.75\t4\n")

    def test_apply_writes_guarded_corrections_and_keeps_source_untouched(self):
        before = self.database.read_bytes()
        result = apply(self.database, self.repair_dir, self.reference_dir)
        self.assertEqual(self.database.read_bytes(), before)
        corrections = result["corrections"]
        self.assertEqual(set(corrections), {"1", "2", "3"})
        self.assertEqual(corrections["1"]["precision"], "street")
        self.assertEqual(corrections["1"]["original_geo_loc"], "43.9999,-98.2828")
        self.assertEqual(corrections["1"]["original_address"], "10 MAIN ST\nTrenton, NJ 08608")
        self.assertEqual(corrections["2"]["precision"], "postal")
        self.assertEqual(corrections["3"]["precision"], "unresolved")
        self.assertTrue((self.repair_dir / "correction-audit.json").exists())

        resolve = location_resolver(self.database)
        self.assertAlmostEqual(resolve(1, "43.9999,-98.2828", "10 MAIN ST\nTrenton, NJ 08608", 1)["lat"], 40.2206)
        self.assertIsNone(resolve(3, "0,0", "5 MAIN ST\nNowhere, NJ 99999", 3)["lat"])
        clean = resolve(4, "40.21,-74.75", "1 MAIN ST\nTrenton, NJ 08608", 4)
        self.assertEqual((clean["lat"], clean["lon"], clean["precision"]), (40.21, -74.75, "original"))


if __name__ == "__main__":
    unittest.main()
