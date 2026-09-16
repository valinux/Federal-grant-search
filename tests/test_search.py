import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from build_index import build, index_path
from flaskserver1 import app
from search import MAP_NODE_LIMIT, PAGE_SIZE, SearchStore

FIXTURE_FILINGS = 550


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "filings.db"
        conn = sqlite3.connect(cls.path)
        conn.execute("""CREATE TABLE filings (
            filer_ein INTEGER, filer_name TEXT, receipt_amt, govt_amt, contrib_amt,
            tax_year INTEGER, xml_name TEXT, officials_json TEXT, serialized_graph TEXT,
            corp_address TEXT, ceo_home1 TEXT, shell_game TEXT, corp_description TEXT, geo_loc TEXT
        )""")
        conn.executemany("INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (12345678 + i, f"Foundation {i}", 10000, 2000, None, 2023, "12345", json.dumps([
                {"n": "Jane Example", "t": "Director"}, None, "unexpected"]), "",
             "10 Main St, Sampletown, NJ 08701", json.dumps({"name": "Jane Example", "phone": "(202) 555-0100"}),
             None, "Education and public service", f"{40 + i / 10000}, -74")
            for i in range(FIXTURE_FILINGS)
        ])
        conn.executemany("INSERT INTO filings (filer_ein, filer_name, officials_json, geo_loc, govt_amt) VALUES (?,?,?,?,?)", [
            (23456789, "100%_Community", "invalid JSON", "NaN, -74", None),
            (23456790, "<img src=x onerror=alert(1)>", "null", "999, 123", "NaN"),
            (23456791, "Malformed officials", '{"n":"Unexpected shape"}', "40,-75", "N/A"),
            (23456792, "Empty coordinates", "[]", "", "1,234"),
        ])
        conn.commit(); conn.close()
        cls.store = SearchStore(cls.path)
        app.config.update(TESTING=True, DATABASE=str(cls.path))
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_list_is_bounded_and_cursor_has_no_overlap(self):
        first = self.store.search("Foundation")
        second = self.store.search("Foundation", after=first["next_cursor"])
        self.assertEqual(len(first["results"]), PAGE_SIZE)
        self.assertTrue(first["has_more"])
        self.assertFalse({r["id"] for r in first["results"]} & {r["id"] for r in second["results"]})
        self.assertNotIn("officials_json", first["results"][0])
        self.assertNotIn("serialized_graph", first["results"][0])

    def test_map_represents_every_match_with_bounded_clusters(self):
        result = self.store.map_search("Foundation")
        self.assertEqual(result["total_mapped"], FIXTURE_FILINGS)
        self.assertEqual(result["visible_count"], FIXTURE_FILINGS)
        self.assertEqual(sum(row["count"] for row in result["results"]), FIXTURE_FILINGS)
        self.assertLessEqual(len(result["results"]), MAP_NODE_LIMIT)
        self.assertFalse(result["has_more"])
        self.assertFalse(self.store.map_search("onerror")["results"])
        self.assertFalse(self.store.map_search("100%")["results"])

    def test_map_area_can_reach_records_beyond_first_500(self):
        result = self.store.map_search("Foundation", bbox="-75,40.052,-73,41")
        self.assertTrue(result["results"])
        self.assertEqual(result["visible_count"], 30)
        self.assertEqual(sum(row["count"] for row in result["results"]), 30)
        self.assertTrue(all(row["lat"] >= 40.052 for row in result["results"]))
        self.assertFalse(result["has_more"])

    def test_empty_search_never_reads_missing_database(self):
        self.assertEqual(SearchStore('/nonexistent.db').search('')["results"], [])
        self.assertEqual(SearchStore('/nonexistent.db').map_search('')["results"], [])
        self.assertEqual(self.client.get('/api/search?q=%20%20').json['results'], [])

    def test_literal_wildcards(self):
        rows = self.store.search("100%_")["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["filer_name"], "100%_Community")

    def test_normalized_phone_and_ein(self):
        self.assertTrue(self.store.search("2025550100", "phone")["results"])
        self.assertTrue(self.store.search("202.555.0100", "all")["results"])
        rows = self.store.search("01-2345678", "ein")["results"]
        self.assertEqual(rows[0]["filer_ein"], "012345678")
        self.assertTrue(self.store.search("012345678", "all")["results"])

    def test_null_and_malformed_json_details_do_not_crash(self):
        result = self.store.detail(1)
        self.assertEqual(result['officers'], [{"name": "Jane Example", "title": "Director"}])
        self.assertIsNone(result["contrib_amt"])
        self.assertIn("/012345678", result["source_url"])
        for filing_id in range(FIXTURE_FILINGS + 1, FIXTURE_FILINGS + 5):
            self.assertEqual(self.client.get(f'/api/filings/{filing_id}').status_code, 200)
        self.assertTrue(self.store.search("Jane", "officials")["results"])

    def test_validation_and_missing_details(self):
        for query in ['field=bad&q=a', 'after=-1&q=a', 'after=xyz&q=a', 'after=99999999999999999999&q=a',
                      'q=' + 'a' * 201, 'q=%00', 'q=California&field=state', 'q=x&page=0', 'q=x&page=-1',
                      'q=x&page=abc', 'q=x&page=1000001']:
            self.assertEqual(self.client.get('/api/search?' + query).status_code, 400, query)
        for bounds in ['a,b,c,d', '1,2,3', '1,2,3,nan', '180,0,-180,20']:
            self.assertEqual(self.client.get('/api/map', query_string={'q': 'x', 'bbox': bounds}).status_code, 400)
        self.assertEqual(self.client.get('/api/filings/999999').status_code, 404)
        self.assertEqual(self.client.get('/api/filings/999999999999999999999999').status_code, 404)

    def test_pages_and_legacy_form_redirect(self):
        for path in ['/', '/map']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Follow the funding', response.data)
            response = self.client.post(path, data={'query': ' foundation ', 'search_field': 'corporation_name'})
            self.assertEqual(response.status_code, 303)
            self.assertIn('q=foundation', response.location)

    def test_missing_database_does_not_create_file(self):
        missing = Path(self.temp.name) / 'missing.db'
        app.config['DATABASE'] = str(missing)
        try:
            self.assertFalse(self.client.get('/api/status').json['ready'])
            self.assertEqual(self.client.get('/api/search?q=x').status_code, 503)
            self.assertFalse(missing.exists())
        finally:
            app.config['DATABASE'] = str(self.path)

    def test_query_deadline(self):
        with self.assertRaises(sqlite3.OperationalError):
            SearchStore(self.path, timeout=0).search('no-matches-at-all')

    def test_index_matches_unindexed_results_and_stale_index_is_ignored(self):
        cases = [('Foundation', 'all'), ('100%_', 'all'), ('Jane', 'officials'), ('012345678', 'all'),
                 ('01-2345678', 'ein'), ('202.555.0100', 'all'), ('2025550100', 'phone'),
                 ('Main NJ', 'address'), ('NJ', 'state'), ('no-matches', 'all'), ('Fo', 'all')]
        expected = [self.store.search(q, f)['results'] for q, f in cases]
        build(self.path, progress=lambda value: None)
        try:
            for (q, field), rows in zip(cases, expected):
                self.assertEqual(self.store.search(q, field)['results'], rows, (q, field))
            self.assertEqual(self.store.map_search('Foundation')['total_mapped'], FIXTURE_FILINGS)
            conn = sqlite3.connect(index_path(self.path))
            conn.execute("UPDATE metadata SET fingerprint='outdated'")
            conn.commit(); conn.close()
            self.assertTrue(self.store.search('Foundation')['results'])
        finally:
            index_path(self.path).unlink()

    def test_numbered_pages_include_full_total_and_direct_last_page(self):
        first = self.store.search("Foundation", page=1)
        self.assertEqual(first["total"], FIXTURE_FILINGS)
        self.assertEqual(first["total_pages"], 23)
        self.assertEqual((first["start"], first["end"]), (1, 24))
        last = self.store.search("Foundation", page=23)
        self.assertEqual(last["page"], 23)
        self.assertEqual((last["start"], last["end"]), (529, FIXTURE_FILINGS))
        self.assertFalse(last["has_more"])
        beyond = self.store.search("Foundation", page=1000)
        self.assertEqual(beyond["results"], last["results"])
        all_ids = []
        for page in range(1, first["total_pages"] + 1):
            all_ids.extend(row["id"] for row in self.store.search("Foundation", page=page)["results"])
        self.assertEqual(all_ids, list(range(1, FIXTURE_FILINGS + 1)))

    def test_map_cluster_can_open_all_its_filings_as_numbered_pages(self):
        response = self.store.map_search("Foundation")
        cluster = next(row for row in response["results"] if row["kind"] == "cluster")
        bbox = ",".join(map(str, cluster["bounds"]))
        first = self.store.search("Foundation", page=1, bbox=bbox)
        self.assertEqual(first["total"], cluster["count"])
        represented = sum(len(self.store.search("Foundation", page=page, bbox=bbox)["results"])
                          for page in range(1, first["total_pages"] + 1))
        self.assertEqual(represented, cluster["count"])

    def test_coincident_points_remain_accessible_at_exact_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'same-place.db'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE filings(filer_name, filer_ein, geo_loc)')
                conn.executemany('INSERT INTO filings VALUES (?, ?, ?)',
                                 [('Same location', 123456789 + i, '40,-74') for i in range(1000)])
                conn.commit()
            store = SearchStore(path)
            response = store.map_search('Same')
            self.assertEqual(len(response['results']), 1)
            self.assertEqual(response['results'][0]['count'], 1000)
            final = store.search('Same', page=42, bbox='-74,40,-74,40')
            self.assertEqual(final['total'], 1000)
            self.assertEqual(final['end'], 1000)

    def test_map_counts_distinguish_matches_without_coordinates(self):
        response = self.store.map_search('100%_')
        self.assertEqual(response['total_matching'], 1)
        self.assertEqual(response['total_mapped'], 0)
        self.assertEqual(response['missing_coordinates'], 1)

    def test_cached_totals_refresh_when_the_database_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changing.db'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE filings(filer_name, filer_ein, geo_loc)')
                conn.execute("INSERT INTO filings VALUES ('Changing', 1, '40,-74')")
                conn.commit()
            store = SearchStore(path)
            self.assertEqual(store.search('Changing')['total'], 1)
            self.assertEqual(store.map_search('Changing')['total_mapped'], 1)
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("INSERT INTO filings VALUES ('Changing too', 2, '41,-75')")
                conn.commit()
            self.assertEqual(store.search('Changing')['total'], 2)
            self.assertEqual(store.map_search('Changing')['total_mapped'], 2)


if __name__ == '__main__':
    unittest.main()
