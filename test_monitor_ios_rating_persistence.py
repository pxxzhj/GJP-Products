import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


class IOSRatingPersistenceTests(unittest.TestCase):
    def test_rating_only_update_regenerates_company_and_index(self):
        app = {'platform': 'iOS', 'pkg_or_id': '123', 'company_cn': 'Example',
               'name': 'Game', 'developer': 'Studio', 'rating_count': 10,
               'release_date': '2026/01/01', 'last_update': '2026/01/01'}
        lookup = {'results': [{'trackId': 123, 'userRatingCount': 12,
                              'currentVersionReleaseDate': '2026-01-01T00:00:00Z'}]}
        with patch.object(monitor, 'itunes_lookup', return_value=lookup) as fetch, \
                patch.object(monitor.time, 'sleep'):
            updates = monitor.check_ios_updates([app])
        self.assertIn('country=us', fetch.call_args.args[0])
        self.assertEqual(updates[0]['new_rating_count'], 12)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            (root / 'index.html').write_text('const companiesData = [];' )
            with patch.object(monitor, 'BASE_DIR', folder):
                monitor.regenerate_files([app], {u['company'] for u in updates})
            company = (root / 'data' / 'Example.js').read_text()
            index = (root / 'product_index.js').read_text()
            self.assertEqual(json.loads(company[company.index('['):-2])[0]['rating_count'], 12)
            self.assertEqual(json.loads(index[index.index('['):-2])[0]['rating_count'], 12)
