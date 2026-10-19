import json
import unittest
from unittest.mock import patch

from audit_domestic_reconciliation import verify_ios_lookup


class IOSCandidateVerificationTests(unittest.TestCase):
    def check_response(self, response):
        with patch('audit_domestic_reconciliation.fetch_url', return_value=response):
            return verify_ios_lookup(['123'])['123']

    def test_successful_empty_response_is_regional_absence(self):
        result = self.check_response({'status': 200, 'text': '{"resultCount":0,"results":[]}'})
        self.assertEqual(result['store_status'], 'not_found_us')
        self.assertEqual(result['store_error'], '')

    def test_rate_limit_is_error(self):
        self.assertEqual(self.check_response({'status': 429})['store_status'], 'error')

    def test_invalid_or_incomplete_payload_is_error(self):
        for payload in ('invalid', '{}', '{"resultCount":1,"results":[]}'):
            with self.subTest(payload=payload):
                self.assertEqual(self.check_response({'status': 200, 'text': payload})['store_status'], 'error')

    def test_incomplete_metadata_is_error(self):
        payload = {'resultCount': 1, 'results': [{'wrapperType': 'software', 'trackId': 123}]}
        self.assertEqual(self.check_response({'status': 200, 'text': json.dumps(payload)})['store_status'], 'error')

    def test_live_metadata_is_retained(self):
        item = {'wrapperType': 'software', 'trackId': 123, 'artistName': 'Studio',
                'artistViewUrl': 'https://apps.apple.com/us/developer/id456'}
        result = self.check_response({'status': 200, 'text': json.dumps({'resultCount': 1, 'results': [item]})})
        self.assertEqual(result['store_status'], '200')
        self.assertEqual(result['item'], item)
