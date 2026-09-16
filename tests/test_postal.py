import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from build_index import build, index_path
from flaskserver1 import app
from postal import mailing_state, mailing_zip, normalize_zip
from search import SearchError, SearchStore


class PostalParsingTests(unittest.TestCase):
    def test_normalization_retains_leading_zeros_and_zip4(self):
        for raw, expected in [(' 08701 ', '08701'), ('08701-0012', '08701-0012'),
                              ('087010012', '08701-0012'), ('08701 0012', '08701-0012')]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_zip(raw), expected)

    def test_parser_requires_postal_position_after_a_state(self):
        for address in ['34947 Main St, Sample, FL 34950',
                        'PO Box 34947\nSample, SD 57025',
                        'Somewhere, XX 34947', 'Somewhere, FL 134947',
                        'Somewhere, FL 34947-25289', 'Somewhere, FL 34947 phone 123',
                        'Somewhere, FL 34947, Brazil', None, 34947]:
            with self.subTest(address=address):
                self.assertNotEqual((mailing_zip(address) or '')[:5], '34947')
        for address in ['401 Angle Rd\nFort Pierce, FL 34947-2528',
                        '401 Angle Rd, Fort Pierce FL 349472528',
                        '401 Angle Rd\r\nFort Pierce fl 34947 2528\r\nUSA',
                        '401 Angle Rd\nFort Pierce, FL 34947-2528, United States.']:
            with self.subTest(address=address):
                self.assertEqual(mailing_zip(address), '34947-2528')
        self.assertEqual(mailing_zip('PO Box 7, San Juan, PR 00901'), '00901')
        self.assertEqual(mailing_zip('Unit 7, APO AE 09012-0001'), '09012-0001')

    def test_mailing_state_requires_postal_position(self):
        self.assertEqual(mailing_state('8 Main St\nSample, FL 34947'), 'FL')
        self.assertEqual(mailing_state('123 MAIN ST, FLOOR 2\nNewark, NJ 07001'), 'NJ')
        # Census-geocoded addresses carry a comma between state and ZIP.
        self.assertEqual(mailing_state('10 MAIN ST, TRENTON, NJ, 08608'), 'NJ')
        self.assertEqual(mailing_zip('10 MAIN ST, TRENTON, NJ, 08608'), '08608')
        self.assertIsNone(mailing_state('8 Main St\nSample, FL 34947, Brazil'))
        self.assertIsNone(mailing_state('Somewhere, XX 34947'))
        self.assertIsNone(mailing_state(None))


class PostalSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / 'postal.db'
        addresses = [
            '401 Angle Rd\nFort Pierce, FL 34947',
            '401 Angle Rd\nFort Pierce, FL 34947-2528',
            '401 Angle Rd\nFort Pierce FL 349472528',
            '401 Angle Rd\nFort Pierce, FL 34947 2528',
            '401 Angle Rd\nFort Pierce, fl 34947\nUSA',
            'PO Box 1\nFort Pierce, FL 34947-4506',
            '34947 Main St\nSample, FL 34950',
            'PO Box 714\nElk Point, SD 57025-0714',
            '8 Main St\nSample, FL 34946',
            '8 Main St\nSample, FL 134947',
            '8 Main St\nSample, FL 34947-25289',
            '8 Main St\nSample, FL 34947, Brazil',
            None,
            '8 Main St\nLakewood, NJ 08701-0001',
            '123 MAIN ST, FLOOR 2\nNewark, NJ 07001',
        ]
        with closing(sqlite3.connect(cls.path)) as conn:
            conn.execute('CREATE TABLE filings(filer_name, filer_ein, corp_address, shell_game, ceo_home1, geo_loc)')
            for number, address in enumerate(addresses, start=1):
                # Every non-address field deliberately contains the query.
                # The EIN reproduces the original 34947 false positive.
                conn.execute('INSERT INTO filings VALUES (?,?,?,?,?,?)',
                             (f'Organization 34947 number {number}', 460434947, address,
                              json.dumps({'corp_ein': '46-0434947', 'address': '1 Main St, Fort Pierce, FL 34947'}),
                              json.dumps({'phone': '123-349-4700'}),
                              f'{27.44 + number / 10000},-80.36' if number <= 6 else '42.68,-96.68'))
            conn.commit()
        cls.store = SearchStore(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_zip_matches_only_the_primary_mailing_postal_code(self):
        data = self.store.search('34947', 'zip_code', page=1)
        self.assertEqual([row['id'] for row in data['results']], [1, 2, 3, 4, 5, 6])
        self.assertEqual(data['total'], 6)
        self.assertEqual(data['total_pages'], 1)
        self.assertEqual(self.store.search('08701', 'zip_code')['results'][0]['id'], 14)

    def test_zip4_matches_exact_extension_in_every_stored_format(self):
        for query in ['34947-2528', '349472528', '34947 2528']:
            with self.subTest(query=query):
                data = self.store.search(query, 'zip_code')
                self.assertEqual([row['id'] for row in data['results']], [2, 3, 4])
        self.assertEqual(self.store.search('34947-9999', 'zip_code')['total'], 0)

    def test_map_list_and_area_filters_share_the_exact_zip_predicate(self):
        data = self.store.map_search('34947', 'zip_code')
        self.assertEqual(data['total_matching'], 6)
        self.assertEqual(sum(row['count'] for row in data['results']), 6)
        self.assertTrue(all(27 < row['lat'] < 28 and -81 < row['lon'] < -80 for row in data['results']))
        elsewhere = '-100,40,-90,45'
        self.assertEqual(self.store.map_search('34947', 'zip_code', bbox=elsewhere)['visible_count'], 0)
        self.assertEqual(self.store.search('34947', 'zip_code', bbox=elsewhere)['total'], 0)

    def test_state_search_matches_mailing_state_exactly(self):
        data = self.store.search('FL', 'state')
        self.assertEqual([row['id'] for row in data['results']], [1, 2, 3, 4, 5, 6, 7, 9])
        self.assertEqual(data['total'], 8)
        nj = self.store.search('nj', 'state')
        self.assertEqual([row['id'] for row in nj['results']], [14, 15])
        self.assertEqual(self.store.search('SD', 'state')['total'], 1)
        # ", FL" appears mid-address (", FLOOR 2") in row 15 and before an
        # unparseable trailing country in row 12; neither is a Florida filing.
        self.assertNotIn(15, [row['id'] for row in data['results']])

    def test_state_search_rejects_non_state_codes(self):
        previous = app.config['DATABASE']
        app.config['DATABASE'] = str(self.path)
        try:
            client = app.test_client()
            for query in ['XX', 'California', 'F', 'FL2', '12']:
                with self.assertRaises(SearchError):
                    self.store.search(query, 'state')
                response = client.get('/api/search', query_string={'q': query, 'field': 'state'})
                self.assertEqual(response.status_code, 400)
            self.assertEqual(client.get('/api/search', query_string={'q': 'fl', 'field': 'state'}).status_code, 200)
        finally:
            app.config['DATABASE'] = previous

    def test_invalid_zips_report_useful_errors_in_both_apis(self):
        previous = app.config['DATABASE']
        app.config['DATABASE'] = str(self.path)
        try:
            client = app.test_client()
            for query in ['3494', '349470', '34947%', '34947 OR 08701', 'Florida',
                          '34947-123', '34947-12345', '3 4947', '３４９４７']:
                for endpoint in ['/api/search', '/api/map']:
                    with self.subTest(query=query, endpoint=endpoint):
                        response = client.get(endpoint, query_string={'q': query, 'field': 'zip_code'})
                        self.assertEqual(response.status_code, 400)
                        self.assertIn('five-digit ZIP', response.json['error'])
                with self.assertRaises(SearchError):
                    self.store.search(query, 'zip_code')
        finally:
            app.config['DATABASE'] = previous

    def test_index_candidates_preserve_exact_results_and_zip4_variants(self):
        queries = ['34947', '34947-2528', '349472528', '34947 2528', '08701', '08701-0001']
        expected = [self.store.search(query, 'zip_code') for query in queries]
        build(self.path, progress=lambda message: None)
        try:
            for query, original in zip(queries, expected):
                with self.subTest(query=query):
                    indexed = self.store.search(query, 'zip_code')
                    self.assertEqual(indexed['results'], original['results'])
                    self.assertEqual(indexed['total'], original['total'])
            with closing(sqlite3.connect(index_path(self.path))) as conn:
                conn.execute("UPDATE metadata SET fingerprint='stale'")
                conn.commit()
            self.assertEqual(self.store.search('349472528', 'zip_code')['total'], 3)
        finally:
            index_path(self.path).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
