"""Exercise the real CSV loaders without network or modifying checked-in data."""
import csv
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('queue_builder', ROOT / 'scraper/skip_trace_queue.py')
assert SPEC is not None and SPEC.loader is not None
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


class QueueScoreTests(unittest.TestCase):
    def test_both_loaders_reject_unknown_and_nonfinite_but_preserve_real_numbers(self):
        for filename, column, loader in [
            ('leads.csv', 'Seller Score', queue.load_county_leads),
            ('inmate_property_matches.csv', 'Inmate Score', queue.load_inmate_matches),
        ]:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                with (Path(directory) / filename).open('w', newline='', encoding='utf-8') as handle:
                    writer = csv.DictWriter(handle, fieldnames=['First Name', 'Last Name', column])
                    writer.writeheader()
                    for index, score in enumerate(['', 'unknown', 'NaN', 'inf', '-inf', '70.5', '0']):
                        writer.writerow({'First Name': f'Person{index}', 'Last Name': 'Example', column: score})
                with patch.object(queue, 'DATA_DIR', Path(directory)), patch.object(queue, 'SCORE_THRESHOLD', 0), redirect_stdout(io.StringIO()):
                    rows = loader()
                self.assertEqual([(r['first'], r['score']) for r in rows], [('Person5', 70), ('Person6', 0)])

    def test_unknown_county_scores_are_not_invented_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'leads.csv'
            with path.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=['First Name', 'Last Name', 'Seller Score'])
                writer.writeheader()
                for first, score in [('Missing', ''), ('Malformed', 'unknown'), ('Actual', '0')]:
                    writer.writerow({'First Name': first, 'Last Name': 'Person', 'Seller Score': score})
            with patch.object(queue, 'DATA_DIR', Path(directory)), patch.object(queue, 'SCORE_THRESHOLD', 0), redirect_stdout(io.StringIO()):
                rows = queue.load_county_leads()
            self.assertEqual([(r['first'], r['score']) for r in rows], [('Actual', 0)])


if __name__ == '__main__':
    unittest.main()
