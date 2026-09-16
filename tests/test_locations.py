import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from locations import correction_path, location_resolver, misplaced_in_brazil
from repair_locations import census_match, census_street, postal_match, split_address
from search import SearchStore


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "filings.db"
        self.address = "10 MAIN ST\nSampletown, NJ 08701"
        self.original_geo = "-8.2051072,-34.9615292"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("CREATE TABLE filings(filer_ein INTEGER, filer_name TEXT, corp_address TEXT, geo_loc TEXT)")
            conn.execute("INSERT INTO filings VALUES(123456789, 'Test Foundation', ?, ?)", [self.address, self.original_geo])
            conn.execute("INSERT INTO filings VALUES(123456790, 'Brazil Foundation', 'Sao Paulo, Brazil', '-23.55,-46.63')")
            conn.commit()
        self.store = SearchStore(self.database)
        self.correction = {"filer_ein": "123456789", "original_address": self.address,
                           "original_geo_loc": self.original_geo, "lat": 40.1, "lon": -74.2,
                           "precision": "street", "source": "Census test fixture", "matched_address": self.address}

    def save(self, correction=None):
        correction_path(self.database).write_text(json.dumps({"version": 1, "corrections": {"1": correction or self.correction}}))

    def test_known_wrong_country_is_not_mapped_without_a_correction(self):
        self.assertEqual(self.store.map_search('Test')["results"], [])
        self.assertEqual(self.store.detail(1)["location"]["precision"], "unresolved")
        self.assertIsNone(self.store.detail(1)["geo_loc"])

    def test_valid_foreign_address_is_preserved(self):
        result = self.store.map_search("Brazil")["results"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["lat"], -23.55)
        self.assertFalse(misplaced_in_brazil("-23.55,-46.63", "Sao Paulo, Brazil"))

    def test_map_bounds_and_details_use_corrected_coordinates(self):
        before = self.database.read_bytes()
        self.save()
        results = self.store.map_search("Test", bbox="-75,39,-73,41")["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["lat"], results[0]["lon"]), (40.1, -74.2))
        self.assertEqual(results[0]["location_precision"], "street")
        self.assertEqual(self.store.map_search("Test", bbox="-36,-10,-34,-7")["results"], [])
        detail = self.store.detail(1)
        self.assertEqual(detail["geo_loc"], "40.1,-74.2")
        self.assertEqual(detail["location"]["source"], "Census test fixture")
        self.assertEqual(self.database.read_bytes(), before)

    def test_stale_row_ein_address_and_coordinates_never_override_new_data(self):
        for field, value in [("filer_ein", "987654321"), ("original_address", "Different address"), ("original_geo_loc", "-20,-50")]:
            with self.subTest(field=field):
                self.save({**self.correction, field: value})
                self.assertEqual(self.store.map_search("Test")["results"], [])

    def test_correction_cache_reloads_and_postal_precision_is_exposed(self):
        self.save()
        self.assertEqual(self.store.map_search("Test")["results"][0]["lat"], 40.1)
        self.save({**self.correction, "lat": 40.22222, "precision": "postal"})
        row = self.store.map_search("Test")["results"][0]
        self.assertEqual(row["lat"], 40.22222)
        self.assertEqual(row["location_precision"], "postal")
        self.assertIn("not a building", self.store.detail(1)["location"]["label"])

    def test_corrupt_or_nonfinite_corrections_do_not_restore_bad_pins(self):
        for payload in ["broken", "[]", '{"version":1,"corrections":null}']:
            correction_path(self.database).write_text(payload)
            self.assertEqual(self.store.map_search("Test")["results"], [])
        self.save({**self.correction, "lat": "NaN"})
        self.assertEqual(self.store.map_search("Test")["results"], [])

    def test_address_preparation_skips_po_boxes_and_strips_care_of(self):
        self.assertIsNone(census_street("P.O. BOX 12"))
        self.assertIsNone(census_street("PO BOX 12"))
        self.assertEqual(census_street("C/O EXAMPLE CPA 10 MAIN ST SUITE 100"), "10 MAIN ST")
        self.assertEqual(split_address(self.address)["zip"], "08701")

    def test_census_matches_require_same_number_state_and_zip(self):
        address = split_address(self.address)
        row = {"corp_address": self.address}
        valid = ["1", "input", "Match", "Exact", "10 MAIN ST, SAMPLETOWN, NJ, 08701", "-74.2,40.1"]
        self.assertEqual(census_match(row, address, valid)["precision"], "street")
        for match in ["12 MAIN ST, SAMPLETOWN, NJ, 08701", "10 MAIN ST, SAMPLETOWN, NY, 08701", "10 MAIN ST, SAMPLETOWN, NJ, 08702"]:
            changed = valid.copy(); changed[4] = match
            self.assertIsNone(census_match(row, address, changed))
        changed = valid.copy(); changed[2] = "Tie"
        self.assertIsNone(census_match(row, address, changed))

    def test_postal_fallback_checks_state_and_labels_approximation(self):
        reference = {("US", "08701"): [{"city": "Sampletown", "state": "NJ", "lat": 40.1, "lon": -74.2}]}
        address = split_address(self.address)
        self.assertEqual(postal_match(address, reference)["precision"], "postal")
        self.assertIsNone(postal_match({**address, "state": "CA"}, reference))


if __name__ == "__main__":
    unittest.main()
