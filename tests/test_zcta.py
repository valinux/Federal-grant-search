import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

import shapefile

from locations import location_resolver
from repair_zcta import (VERIFIED_NAME, apply, decide, display_address, postal_repair, scan,
                         us_city_index, zcta_boundaries)

# A 0.4° x 0.4° test polygon in solid New Jersey interior for ZIP 08608.
# Shapefile exterior rings must be clockwise.
POLYGON = [(-74.9, 39.9), (-74.9, 40.3), (-74.5, 40.3), (-74.5, 39.9), (-74.9, 39.9)]
INSIDE = (40.1, -74.7)
OUTSIDE = (40.35, -74.7)  # north of the polygon edge, still inside NJ
FAR = (43.9999, -98.2828)
REFERENCE = {("US", "08608"): [{"city": "Fairton", "state": "NJ", "lat": INSIDE[0], "lon": INSIDE[1]}]}


def write_zcta_zip(path, entries):
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary) / "cb_2020_us_zcta520_500k"
        writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
        writer.field("ZCTA5CE20", "C", size=5)
        for code, ring in entries:
            writer.record(code)
            writer.poly([ring])
        writer.close()
        with zipfile.ZipFile(path, "w") as archive:
            for extension in ("shp", "shx", "dbf"):
                archive.write(base.with_suffix(f".{extension}"), f"cb_2020_us_zcta520_500k.{extension}")


def census_row(filing_id="1", matched="10 MAIN ST, FAIRTON, NJ, 08608", coords="-74.7,40.1"):
    return [filing_id, "10 MAIN ST, Fairton, NJ 08608", "Match", "Exact", matched, coords]


def make_suspect(geo, address, current=None, filing_id="1", city_reference=None):
    if current is None:
        lat, lon = geo.split(",")
        current = {"lat": float(lat), "lon": float(lon), "precision": "original"}
    suspect = {"id": filing_id, "filer_ein": 123456789, "filer_name": "Example",
               "corp_address": address, "geo_loc": geo, "zip": "08608", "state": "NJ",
               "current": current, "issues": ["outside_zcta"]}
    if city_reference:
        suspect["city_reference"] = city_reference
    return suspect


class DisplayAddressTests(unittest.TestCase):
    def test_parses_correction_display_addresses(self):
        self.assertEqual(display_address("141 E COLLEGE AVE, Decatur, GA 30030"),
                         {"street": "", "city": "Decatur", "state": "GA", "zip": "30030"})
        self.assertEqual(display_address("Kingshill, VI 00851"),
                         {"street": "", "city": "Kingshill", "state": "VI", "zip": "00851"})
        for bad in [None, "", "Brazil", "No state here 12345"]:
            self.assertIsNone(display_address(bad))


class ZctaStructureTests(unittest.TestCase):
    def test_boundaries_load_from_shapefile_zip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cb_2020_us_zcta520_500k.zip"
            write_zcta_zip(path, [("08608", POLYGON)])
            boundaries = zcta_boundaries(temporary)
            self.assertEqual(set(boundaries), {"08608"})
            self.assertEqual(boundaries["08608"].bounds, (-74.9, 39.9, -74.5, 40.3))


class PostalRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        write_zcta_zip(Path(cls.temp.name) / "cb_2020_us_zcta520_500k.zip", [("08608", POLYGON)])
        cls.zctas = zcta_boundaries(cls.temp.name)
        cls.address = {"street": "PO BOX 1", "city": "Fairton", "state": "NJ", "zip": "08608"}

    def test_geonames_centroid_inside_polygon_is_kept(self):
        result = postal_repair(self.address, REFERENCE, self.zctas)
        self.assertEqual(result["source"], "GeoNames postal dataset (CC BY 4.0)")
        self.assertEqual((result["lat"], result["lon"]), INSIDE)

    def test_geonames_centroid_outside_polygon_snaps_to_zcta(self):
        reference = {("US", "08608"): [{"city": "Fairton", "state": "NJ", "lat": 40.0, "lon": -74.0}]}
        result = postal_repair(self.address, reference, self.zctas)
        self.assertIn("ZCTA", result["source"])
        point = self.zctas["08608"].distance(__import__("shapely").Point(result["lon"], result["lat"]))
        self.assertEqual(point, 0)

    def test_zcta_point_used_when_geonames_lacks_the_zip(self):
        result = postal_repair(self.address, {}, self.zctas)
        self.assertIn("ZCTA", result["source"])
        self.assertEqual(result["precision"], "postal")

    def test_no_reference_anywhere_returns_none(self):
        self.assertIsNone(postal_repair({**self.address, "zip": "99998"}, {}, self.zctas))

    def test_geonames_record_far_from_its_city_is_distrusted(self):
        # Regression: GeoNames carries "Barrington, IL 60011" in Lake Michigan.
        reference = {("US", "08608"): [{"city": "Fairton", "state": "NJ", "lat": 40.0, "lon": -74.0}]}
        cities = {"FAIRTON": {"NJ": (INSIDE[0], INSIDE[1], 5000)}}
        result = postal_repair(self.address, reference, self.zctas, cities)
        self.assertIn("ZCTA", result["source"])  # distrusted centroid falls back to the polygon

        cities_far = {"FAIRTON": {"NJ": (INSIDE[0], INSIDE[1], 5000)}}
        good = postal_repair(self.address, REFERENCE, self.zctas, cities_far)
        self.assertEqual(good["source"], "GeoNames postal dataset (CC BY 4.0)")


class DecideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        write_zcta_zip(Path(cls.temp.name) / "cb_2020_us_zcta520_500k.zip", [("08608", POLYGON)])
        cls.zctas = zcta_boundaries(cls.temp.name)

    def test_census_match_inside_polygon_repairs_with_street(self):
        result_action, result = decide(make_suspect(f"{FAR[0]},{FAR[1]}", "10 MAIN ST\nFairton, NJ 08608"),
                                       census_row(), REFERENCE, self.zctas)
        self.assertEqual(result_action, "repair")
        self.assertEqual(result["precision"], "street")

    def test_census_outside_polygon_agreeing_with_pin_means_polygon_mismatch(self):
        row = census_row(filing_id="3", matched="10 EDGE RD, FAIRTON, NJ, 08608", coords=f"{OUTSIDE[1]},{OUTSIDE[0]}")
        row[1] = "10 EDGE RD, Fairton, NJ 08608"
        action, result = decide(make_suspect(f"{OUTSIDE[0]},{OUTSIDE[1]}", "10 EDGE RD\nFairton, NJ 08608"),
                                row, REFERENCE, self.zctas)
        self.assertEqual((action, result), ("keep", None))

    def test_census_inside_polygon_replaces_nearby_imprecise_pin(self):
        geo = f"{INSIDE[0] + 0.03},{INSIDE[1]}"  # 3 km away, and the pin is outside the polygon
        suspect = make_suspect(geo, "10 MAIN ST\nFairton, NJ 08608")
        action, result = decide(suspect, census_row(coords=f"{INSIDE[1]},{INSIDE[0]}"), REFERENCE, self.zctas)
        self.assertEqual(action, "repair")
        self.assertEqual(result["precision"], "street")

    def test_census_agreeing_with_raw_coordinates_restores_original(self):
        current = {"lat": 40.0, "lon": -74.7, "precision": "postal",
                   "matched_address": "Fairton, NJ 08608"}
        suspect = make_suspect("40.15,-74.7", "10 MAIN ST\nFairton, NJ 08608", current=current)
        row = census_row(coords="-74.7,40.15")
        action, result = decide(suspect, row, REFERENCE, self.zctas)
        self.assertEqual((action, result), ("restore", None))

    def test_po_box_falls_back_to_postal_area(self):
        suspect = make_suspect(f"{FAR[0]},{FAR[1]}", "PO BOX 1\nFairton, NJ 08608")
        action, result = decide(suspect, None, REFERENCE, self.zctas)
        self.assertEqual(action, "repair")
        self.assertEqual(result["precision"], "postal")

    def test_correction_address_is_used_when_source_address_is_missing(self):
        current = {"lat": 18.09, "lon": -66.24, "precision": "postal",
                   "matched_address": "Fairton, NJ 08608"}
        suspect = make_suspect("", None, current=current)
        action, result = decide(suspect, None, REFERENCE, self.zctas)
        self.assertEqual(action, "repair")
        self.assertEqual(result["precision"], "postal")

    def test_city_reference_is_the_last_resort(self):
        suspect = make_suspect(f"{FAR[0]},{FAR[1]}", "PO BOX 9\nFairton, NJ 99998",
                               city_reference={"lat": 39.5, "lon": -75.1})
        suspect["zip"] = "99998"
        action, result = decide(suspect, None, {}, self.zctas)
        self.assertEqual(action, "repair")
        self.assertEqual(result["precision"], "city")


class CityIndexTests(unittest.TestCase):
    def test_state_filtered_city_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            def line(name, state, lat, lon, population):
                fields = ["1", name, name, "", str(lat), str(lon), "P", "PPL", "US",
                          "", state, "", "", "", str(population), "", "", "X", "2020-01-01"]
                return "\t".join(fields)
            with zipfile.ZipFile(Path(temporary) / "cities500.zip", "w") as archive:
                archive.writestr("cities500.txt", "\n".join([
                    line("Greenwood", "SC", 34.19, -82.16, 23000),
                    line("Greenwood", "IN", 39.61, -86.10, 63000),
                    line("Greenwood", "XX", 0.0, 0.0, 1),  # ignored state still recorded
                ]) + "\n")
            index = us_city_index(temporary)
            self.assertEqual(index["GREENWOOD"]["SC"][:2], (34.19, -82.16))
            self.assertEqual(index["GREENWOOD"]["IN"][:2], (39.61, -86.10))
            self.assertNotIn("NOPE", index)


class ApplyZctaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "filings.db"
        rows = [
            ("Street Fixed", 1, "10 MAIN ST\nFairton, NJ 08608", f"{FAR[0]},{FAR[1]}"),
            ("PO Box Fixed", 2, "PO BOX 1\nFairton, NJ 08608", f"{FAR[0]},{FAR[1]}"),
            ("Polygon Kept", 3, "10 EDGE RD\nFairton, NJ 08608", f"{OUTSIDE[0]},{OUTSIDE[1]}"),
            ("Clean", 4, "1 MAIN ST\nFairton, NJ 08608", f"{INSIDE[0]},{INSIDE[1]}"),
        ]
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("CREATE TABLE filings(filer_name, filer_ein, corp_address, geo_loc)")
            conn.executemany("INSERT INTO filings VALUES (?,?,?,?)", rows)
            conn.commit()
        self.repair_dir = root / "repair"
        self.repair_dir.mkdir()
        write_zcta_zip(self.repair_dir / "cb_2020_us_zcta520_500k.zip", [("08608", POLYGON)])
        with (self.repair_dir / "census-results-zcta.csv").open("w", newline="") as out:
            out.write('1,"10 MAIN ST, Fairton, NJ 08608",Match,Exact,"10 MAIN ST, FAIRTON, NJ, 08608","-74.7,40.1"\n')
            out.write(f'3,"10 EDGE RD, Fairton, NJ 08608",Match,Exact,"10 EDGE RD, FAIRTON, NJ, 08608","{OUTSIDE[1]},{OUTSIDE[0]}"\n')
        self.reference_dir = root / "reference"
        self.reference_dir.mkdir()
        with zipfile.ZipFile(self.reference_dir / "US.zip", "w") as archive:
            archive.writestr("US.txt", "US\t08608\tFairton\tNew Jersey\tNJ\t\t\t\t\t40.1\t-74.7\t4\n")

    def test_apply_repairs_keeps_and_verifies(self):
        before = self.database.read_bytes()
        result = apply(self.database, self.repair_dir, self.reference_dir)
        self.assertEqual(self.database.read_bytes(), before)
        corrections = result["corrections"]
        self.assertEqual(corrections["1"]["precision"], "street")
        self.assertEqual(corrections["2"]["precision"], "postal")
        self.assertEqual((corrections["2"]["lat"], corrections["2"]["lon"]), (40.1, -74.7))
        self.assertNotIn("3", corrections)  # kept: Census confirmed the pin outside the polygon
        self.assertNotIn("4", corrections)

        verified = json.loads((self.repair_dir / VERIFIED_NAME).read_text())
        self.assertEqual(verified["3"]["geo_loc"], f"{OUTSIDE[0]},{OUTSIDE[1]}")

        resolve = location_resolver(self.database)
        self.assertEqual(resolve(1, f"{FAR[0]},{FAR[1]}", "10 MAIN ST\nFairton, NJ 08608", 1)["precision"], "street")
        self.assertEqual(resolve(4, f"{INSIDE[0]},{INSIDE[1]}", "1 MAIN ST\nFairton, NJ 08608", 4)["precision"], "original")

        # A re-scan with the verification list is clean; a changed pin re-flags.
        suspects, _blind, stats = scan(self.database, self.repair_dir, {}, {}, verified)
        self.assertEqual(suspects, [])
        self.assertEqual(stats["verified"], 1)
        verified["3"]["geo_loc"] = "stale"
        suspects, _blind, _stats = scan(self.database, self.repair_dir, {}, {}, verified)
        self.assertEqual([s["id"] for s in suspects], [3])


if __name__ == "__main__":
    unittest.main()
