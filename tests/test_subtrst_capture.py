"""Execute the real scraper main with offline portal fixtures."""
import json
from pathlib import Path
import tempfile
import unittest

from test_scraper_pagination import MainHarness
from test_subtrst_sheet_sync import record


class CaptureBoundaryTests(unittest.TestCase):
    def test_premerge_capture_keeps_individual_bank_and_unmotivated_subtrst(self):
        records = [record(doc_num='20260000001', owner='EXAMPLE OWNER', is_motivated=True),
                   record(doc_num='20260000002', owner='EXAMPLE OWNER', is_motivated=True),
                   record(doc_num='20260000003', owner='EXAMPLE BANK', is_motivated=True),
                   record(doc_num='20260000004', owner='EXAMPLE OWNER', is_motivated=False)]
        harness = MainHarness(result=records)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            harness.namespace['DATA_DIR'] = path
            harness.run()
            archive = json.loads((path/'subtrst_records.json').read_text(encoding='utf-8'))
            self.assertEqual([r['doc_num'] for r in archive['records']],
                             ['20260000001', '20260000002', '20260000003', '20260000004'])
            self.assertEqual(archive['total'], 4)
            self.assertTrue(all('Business / LLC owner' not in r['flags'] for r in archive['records']))
            # The original merge, institution exclusion and export are untouched.
            original = json.loads((path/'records.json').read_text(encoding='utf-8'))
            self.assertEqual(original['total'], 1)
            self.assertEqual(original['records'][0]['doc_num'], '20260000001 | 20260000002')
            self.assertEqual(harness.exports, ['csv', 'sheets'])

    def test_incomplete_later_window_cannot_touch_archive(self):
        def results(call):
            if call > 1:
                raise RuntimeError('incomplete later window')
            return [record()]
        harness = MainHarness(result=results)
        harness.namespace['date_range_iso'] = lambda: ('2026-01-01', '2026-01-16')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            archive = path/'subtrst_records.json'
            archive.write_bytes(b'untouched sentinel')
            harness.namespace['DATA_DIR'] = path
            with self.assertRaisesRegex(RuntimeError, 'incomplete later window'):
                harness.run()
            self.assertEqual(archive.read_bytes(), b'untouched sentinel')


if __name__ == '__main__':
    unittest.main()
