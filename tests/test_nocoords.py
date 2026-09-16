import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from locations import location_resolver
from repair_nocoords import (CACHE_NAME, RESULTS_NAME, apply, foreign_locate, gazetteer_locate,
                             name_keys, usable_address, world_reference)
from search import SearchStore


def geonames_line(code, name, country, lat, lon, population=0, alternates=""):
    fields = ["1", name, name, alternates, str(lat), str(lon), "P", "PPL", country,
              "", "", "", "", "", str(population), "", "", "Europe/Dublin", "2020-01-01"]
    assert len(fields) == 19
    return "\t".join(fields)


def postal_line(country, code, city, state, lat, lon):
    return f"{country}\t{code}\t{city}\tRegion\t{state}\t\t\t\t\t{lat}\t{lon}\t4"


class HelperTests(unittest.TestCase):
    def test_usable_address_requires_us_state_zip_street_and_city(self):
        entry = {"status": "ok", "address": "141 E COLLEGE AVE", "city": "Decatur",
                 "state": "ga", "zipcode": "30030-3770"}
        self.assertEqual(usable_address(entry),
                         {"street": "141 E COLLEGE AVE", "city": "Decatur", "state": "GA", "zip": "30030"})
        for broken in [{"status": "error"}, {**entry, "state": None}, {**entry, "zipcode": "00000-0000"},
                       {**entry, "address": ""}, {**entry, "city": None}, {}]:
            with self.subTest(broken=broken):
                self.assertIsNone(usable_address(broken))

    def test_name_keys_expand_abbreviations_on_spaceless_strings(self):
        self.assertIn("STCATHARINES", name_keys(["ST", "CATHARINES"]))
        self.assertIn("SAINTCATHARINES", name_keys(["ST", "CATHARINES"]))
        self.assertIn("MOUNTCHARLESTON", name_keys(["MT", "CHARLESTON"]))


class ForeignLocateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        reference = {
            ("CA", "M5G 2C4"): [{"city": "Toronto", "state": "ON", "lat": 43.65, "lon": -79.38}],
            ("CA", "V0R"): [{"city": "Shawnigan Lake", "state": "BC", "lat": 48.65, "lon": -123.63}],
            ("GB", "SN2 2NA"): [{"city": "Swindon", "state": "", "lat": 51.56, "lon": -1.78}],
            ("FR", "74301 CEDEX"): [{"city": "Cluses", "state": "", "lat": 46.06, "lon": 6.57}],
        }
        with zipfile.ZipFile(root / "cities500.zip", "w") as archive:
            archive.writestr("cities500.txt", "\n".join([
                geonames_line("", "Sandyford", "IE", 53.2747, -6.2253, population=5200),
                geonames_line("", "Dublin", "IE", 53.3331, -6.2489, population=1024027),
                geonames_line("", "Le Grand-Saconnex", "CH", 46.2319, 6.1209, population=12000),
            ]) + "\n")
        cls.postal, cls.cities = world_reference(reference, root)

    def test_canadian_full_code_and_fsa_fallback(self):
        found = foreign_locate("TORONTO ONTARIO M5G 2C4", "Canada", self.postal, self.cities)
        self.assertEqual((found["precision"], found["lat"]), ("postal", 43.65))
        found = foreign_locate("SHAWNIGAN LAKE BC V0R 2W1", "Canada", self.postal, self.cities)
        self.assertEqual((found["precision"], found["lat"]), ("postal", 48.65))

    def test_uk_spaceless_postcode(self):
        found = foreign_locate("UNITED KINGDOM WILTSHIRE SN22NA", "United Kingdom", self.postal, self.cities)
        self.assertEqual((found["precision"], found["lat"]), ("postal", 51.56))

    def test_french_cedex_matches_bare_commune_code(self):
        found = foreign_locate("CALUSES CEDEX 74301", "France", self.postal, self.cities)
        self.assertEqual((found["precision"], found["lat"]), ("postal", 46.06))

    def test_earliest_city_token_beats_bigger_parent_city(self):
        found = foreign_locate("SANDYFORD DUBLIN 17", "Ireland", self.postal, self.cities)
        self.assertEqual((found["precision"], found["lat"]), ("city", 53.2747))

    def test_spaceless_city_name_matches_by_containment(self):
        found = foreign_locate("GRANDSACONNEX", "Switzerland", self.postal, self.cities)
        self.assertEqual((found["precision"], found["lat"]), ("city", 46.2319))

    def test_unknown_country_and_empty_text_are_unresolved(self):
        self.assertIsNone(foreign_locate("SOME PLACE 1", "Atlantis", self.postal, self.cities))
        self.assertIsNone(foreign_locate(None, "Canada", self.postal, self.cities))
        self.assertIsNone(foreign_locate("NOWHERESVILLE QT", "Canada", self.postal, self.cities))


class GazetteerTests(unittest.TestCase):
    def test_exact_name_lookup_in_country_dump(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "nationwide").mkdir()
            with zipfile.ZipFile(root / "dump-IL.zip", "w") as archive:
                archive.writestr("IL.txt", "\n".join([
                    geonames_line("", "Kishor", "IL", 32.94699, 35.24921, alternates="Kishor,Kisor,כישור"),
                    geonames_line("", "Tel Aviv", "IL", 32.08, 34.78),
                ]) + "\n")
            found = gazetteer_locate("Kishor", "IL", root / "nationwide")
            self.assertEqual((found["lat"], found["lon"]), (32.94699, 35.24921))
            self.assertIsNone(gazetteer_locate("Nowhere", "IL", root / "nationwide"))
            self.assertIsNone(gazetteer_locate("Kishor", "JP", root / "nationwide"))


class ApplyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "filings.db"
        rows = [
            ("US Org", 111111111, None, None),
            ("Foreign Org", 222222222, None, None),
            ("Misrouted Org", 333333333, None, None),
            ("No Address Org", 444444444, None, None),
        ]
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("CREATE TABLE filings(filer_name, filer_ein, corp_address, shell_game, ceo_home1, geo_loc)")
            conn.executemany("INSERT INTO filings VALUES (?,?,NULL,NULL,NULL,NULL)",
                             [(name, ein) for name, ein, _addr, _geo in rows])
            conn.commit()
        self.repair_dir = root / "repair"
        self.repair_dir.mkdir()
        cache = {
            "111111111": {"status": "ok", "address": "141 E COLLEGE AVE", "city": "Decatur",
                          "state": "GA", "zipcode": "30030-3770"},
            "222222222": {"status": "ok", "address": "TORONTO ONTARIO M5G 2C4", "city": "Canada",
                          "state": None, "zipcode": "00000-0000"},
            "333333333": {"status": "ok", "address": "SEATTLE WA 98104-1610", "city": "Canada",
                          "state": None, "zipcode": "00000-0000"},
            "444444444": {"status": "ok", "address": None, "city": None, "state": None, "zipcode": None},
        }
        (self.repair_dir / CACHE_NAME).write_text(json.dumps(cache))
        (self.repair_dir / RESULTS_NAME).write_text(
            '1,"141 E COLLEGE AVE, Decatur, GA, 30030",Match,Exact,'
            '"141 E COLLEGE AVE, DECATUR, GA, 30030","-84.2938,33.7706"\n')
        self.reference_dir = root / "reference"
        self.reference_dir.mkdir()
        with zipfile.ZipFile(self.reference_dir / "US.zip", "w") as archive:
            archive.writestr("US.txt", "\n".join([
                postal_line("US", "30030", "Decatur", "GA", 33.77, -84.29),
                postal_line("US", "98104", "Seattle", "WA", 47.6036, -122.3256),
            ]) + "\n")
        with zipfile.ZipFile(self.reference_dir / "CA.zip", "w") as archive:
            archive.writestr("CA.txt", postal_line("CA", "M5G 2C4", "Toronto", "ON", 43.65, -79.38) + "\n")

    def test_apply_recovers_us_foreign_and_misrouted_rows(self):
        before = self.database.read_bytes()
        result = apply(self.database, self.repair_dir, self.reference_dir)
        self.assertEqual(self.database.read_bytes(), before)
        corrections = result["corrections"]
        self.assertNotIn("4", corrections)

        self.assertEqual(corrections["1"]["precision"], "street")
        self.assertEqual(corrections["1"]["matched_address"], "141 E COLLEGE AVE, Decatur, GA 30030")
        self.assertEqual(corrections["2"]["precision"], "postal")
        self.assertAlmostEqual(corrections["2"]["lat"], 43.65)
        self.assertEqual(corrections["3"]["precision"], "postal")
        self.assertAlmostEqual(corrections["3"]["lat"], 47.6036)
        self.assertEqual(corrections["3"]["matched_address"], "SEATTLE WA 98104-1610")
        for entry in corrections.values():
            self.assertIsNone(entry["original_address"])
            self.assertIsNone(entry["original_geo_loc"])
            self.assertIn("ProPublica", entry["reason"])

        resolve = location_resolver(self.database)
        self.assertEqual(resolve(1, None, None, 111111111)["precision"], "street")
        # The guard still rejects rows whose source values no longer match.
        self.assertEqual(resolve(1, "1,2", None, 111111111)["precision"], "original")
        self.assertIsNone(resolve(4, None, None, 444444444)["lat"])

        store = SearchStore(self.database)
        self.assertEqual([row["id"] for row in store.search("GA", "state")["results"]], [1])
        self.assertEqual([row["id"] for row in store.search("30030", "zip_code")["results"]], [1])
        self.assertEqual([row["id"] for row in store.search("WA", "state")["results"]], [3])
        self.assertEqual([row["id"] for row in store.search("98104", "zip_code")["results"]], [3])
        # A foreign correction belongs to no U.S. state search.
        for state in ("GA", "WA", "NJ", "NY", "CA"):
            self.assertNotIn(2, [row["id"] for row in store.search(state, "state")["results"]])
        toronto = store.map_search("Foreign Org", "corporation_name")
        self.assertEqual(toronto["results"][0]["lat"], 43.65)


if __name__ == "__main__":
    unittest.main()
