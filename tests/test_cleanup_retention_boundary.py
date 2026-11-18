"""Caller regression tests with synthetic credentials and blocked network."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scraper'))
import scored_retention as r

CLAIMS = [{'owner': ['example', 'owner'], 'property': '10 test st, san antonio, tx, 78201', 'score': 100.0}]


class CleanupBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'GHL_API_KEY': 'synthetic-test-not-a-real-key',
                                                'GHL_LOCATION_ID': r.LOCATION, 'DRY_RUN': 'true'}))
        spec = importlib.util.spec_from_file_location('cleanup_boundary', Path(__file__).resolve().parents[1] / 'scraper/ghl_cleanup.py')
        assert spec is not None and spec.loader is not None
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.enterContext(patch.object(self.mod.time, 'sleep'))
        self.transport = self.enterContext(patch.object(self.mod.urllib.request, 'urlopen', side_effect=AssertionError('Network forbidden in tests')))
        self.delete = self.enterContext(patch.object(self.mod, 'ghl_delete'))
        self.enterContext(patch.object(self.mod, 'load_retention_claims', return_value=CLAIMS))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_incomplete_or_drifting_pagination_aborts_before_delete(self):
        cases = [
            [{'contacts': [{'id': 'a'}], 'meta': {'total': 2}}],
            [{'contacts': [{'id': 'a'}], 'meta': {'total': 2, 'startAfter': 1, 'startAfterId': 'a'}}, {'contacts': [], 'meta': {'total': 2}}],
            [{'contacts': [{'id': 'a'}], 'meta': {'total': 2, 'startAfter': 1, 'startAfterId': 'a'}}, {'contacts': [{'id': 'a'}], 'meta': {'total': 2}}],
            [{'contacts': [{'id': 'a'}], 'meta': {'total': 2, 'startAfter': 1, 'startAfterId': 'a'}}, {'contacts': [{'id': 'b'}], 'meta': {'total': 3, 'startAfter': 2, 'startAfterId': 'b'}}],
            [{'contacts': []}],
            [{'contacts': [{'id': 'a'}], 'meta': {'total': 0}}],
        ]
        self.mod.DRY_RUN = False
        for pages in cases:
            for page in pages:
                for contact in page['contacts']:
                    contact.update(locationId=r.LOCATION, customFields=[])
            with self.subTest(pages=pages), patch.object(self.mod, 'ghl_get', side_effect=pages) as get:
                with self.assertRaises(SystemExit) as exc:
                    self.mod.main()
                self.assertEqual(exc.exception.code, 1)
                self.assertLessEqual(get.call_count, len(pages))
                self.delete.assert_not_called()

    def test_complete_pagination_follows_both_cursors(self):
        pages = [{'contacts': [{'id': 'a'}], 'meta': {'total': 2, 'startAfter': 123, 'startAfterId': 'a'}},
                 {'contacts': [{'id': 'b'}], 'meta': {'total': 2}}]
        with patch.object(self.mod, 'ghl_get', side_effect=pages) as get:
            self.assertEqual([c['id'] for c in self.mod.get_all_contacts()], ['a', 'b'])
            self.assertEqual(get.call_args_list[1].args[1], {'locationId': r.LOCATION, 'limit': 100, 'startAfter': 123, 'startAfterId': 'a'})

    def test_pagination_exhausts_six_pages(self):
        pages = [{'contacts': [{'id': str(i)}], 'meta': {'total': 6, 'startAfter': i, 'startAfterId': str(i)}} for i in range(6)]
        with patch.object(self.mod, 'ghl_get', side_effect=pages) as get:
            self.assertEqual(len(self.mod.get_all_contacts()), 6)
            self.assertEqual(get.call_count, 6)

    def test_repeated_cursor_is_rejected_before_total(self):
        pages = [{'contacts': [{'id': str(i)}], 'meta': {'total': 3, 'startAfter': 1, 'startAfterId': 'a'}} for i in range(2)]
        with patch.object(self.mod, 'ghl_get', side_effect=pages):
            with self.assertRaisesRegex(ValueError, 'repeated pagination cursor'):
                self.mod.get_all_contacts()

    def test_live_caller_does_not_delete_scored_no_phone_prospect(self):
        self.mod.DRY_RUN = False
        contact = {'id': 'synthetic', 'locationId': r.LOCATION, 'firstName': 'Example', 'lastName': 'Owner',
                   'customFields': [{'id': r.SCORE_FIELD, 'value': 100}, {'id': r.PROPERTY_FIELD, 'value': '10 TEST ST, SAN ANTONIO, TX, 78201'}]}
        with patch.object(self.mod, 'get_all_contacts', return_value=[contact]):
            self.mod.main()
        self.delete.assert_not_called()
        self.assertIn('Protected scored prospects: 1', self.output.getvalue())
        self.assertNotIn('Would delete:', self.output.getvalue())
        self.assertNotIn('synthetic-test', self.output.getvalue())

    def test_late_malformed_contact_aborts_before_first_delete(self):
        self.mod.DRY_RUN = False
        contacts = [{'id': 'eligible', 'locationId': r.LOCATION, 'customFields': []},
                    {'id': 'malformed', 'locationId': r.LOCATION, 'customFields': {}}]
        with patch.object(self.mod, 'get_all_contacts', return_value=contacts):
            with self.assertRaisesRegex(ValueError, 'Malformed custom'):
                self.mod.main()
        self.delete.assert_not_called()

    def test_dry_run_transport_is_get_only_with_delete_candidate(self):
        methods = []
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({'contacts': [{'id': 'synthetic', 'locationId': r.LOCATION, 'customFields': []}], 'meta': {'total': 1}}).encode()
        def urlopen(req, **kwargs):
            methods.append(req.get_method())
            return Response()
        self.transport.side_effect = urlopen
        self.mod.main()
        self.assertEqual(methods, ['GET'])
        self.delete.assert_not_called()
        self.assertIn('Would delete: 1', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
