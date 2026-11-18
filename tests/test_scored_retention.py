"""Synthetic retention tests; standard-library CI, no CRM connections."""
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scraper'))
import scored_retention as r

LOC = r.LOCATION
ROW = {'first': 'Example', 'last': 'Owner', 'prop_address': '10 TEST ST',
       'prop_city': 'SAN ANTONIO', 'prop_state': 'TX', 'prop_zip': '78201', 'score': '100'}


def contact(**kw):
    value = {'id': 'synthetic-contact', 'locationId': LOC, 'firstName': 'example', 'lastName': 'owner',
             'customFields': [{'id': r.SCORE_FIELD, 'value': 100},
                              {'id': r.PROPERTY_FIELD, 'value': '10 TEST ST, SAN ANTONIO, TX, 78201'}]}
    value.update(kw)
    return value


class ScoredRetentionTests(unittest.TestCase):
    def test_real_scored_property_prospect_retained(self):
        out = r.partition([contact()], r.source_claims([ROW]), LOC)
        self.assertEqual(len(out['protected_scored']), 1)
        self.assertFalse(out['delete_candidates'])

    def test_wrong_identity_or_score_is_held_not_falsely_verified(self):
        changes = [{'firstName': 'other'}, {'lastName': 'other'},
                   {'customFields': [{'id': r.SCORE_FIELD, 'value': 90},
                                     {'id': r.PROPERTY_FIELD, 'value': '10 TEST ST, SAN ANTONIO, TX, 78201'}]}]
        for change in changes:
            with self.subTest(change=change):
                out = r.partition([contact(**change)], r.source_claims([ROW]), LOC)
                self.assertFalse(out['protected_scored'])
                self.assertEqual(len(out['review_held']), 1)
                self.assertFalse(out['delete_candidates'])

    def test_unrelated_unscored_no_phone_keeps_original_cleanup_behavior(self):
        out = r.partition([contact(customFields=[])], r.source_claims([ROW]), LOC)
        self.assertEqual(len(out['delete_candidates']), 1)
        self.assertFalse(out['protected_scored'])

    def test_scored_history_disappearing_from_current_source_is_not_deleted(self):
        out = r.partition([contact()], r.source_claims([{**ROW, 'score': '90'}]), LOC)
        self.assertEqual(len(out['review_held']), 1)
        self.assertFalse(out['delete_candidates'])

    def test_invalid_collection_fails_before_any_delete(self):
        for change in ({'locationId': 'foreign'}, {'id': ''}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                r.partition([contact(**change)], r.source_claims([ROW]), LOC)
        with self.assertRaises(ValueError):
            r.partition([contact(), contact()], r.source_claims([ROW]), LOC)

    def test_phone_contacts_preserved(self):
        out = r.partition([contact(phone='+15555550123')], r.source_claims([ROW]), LOC)
        self.assertFalse(out['delete_candidates'])
        self.assertFalse(out['protected_scored'])

    def test_empty_claims_fail_closed(self):
        with self.assertRaises(ValueError):
            r.partition([contact()], [], LOC)

    def test_source_claim_preserves_full_literal_property(self):
        full = {**ROW, 'prop_address': '10 TEST ST, SAN ANTONIO, TX, 78201'}
        self.assertEqual(r.source_claims([full]), r.source_claims([ROW]))

    def test_title_case_aliases_and_zero_are_real_scores(self):
        row = {'First Name': 'Example', 'Last Name': 'Owner', 'Property Address': '10 TEST ST',
               'Property City': 'SAN ANTONIO', 'Property State': 'TX', 'Property Zip': '78201', 'Seller Score': '0'}
        self.assertEqual(r.source_claims([row])[0]['score'], 0)

    def test_unknown_score_cannot_become_source_claim(self):
        self.assertEqual(r.source_claims([{**ROW, 'score': ''}]), [])

    def test_malformed_source_cannot_hide_behind_other_valid_claims(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            root = Path(directory)
            for name in ('skip_traced.csv', 'skip_trace_queue.csv'):
                with (root / name).open('w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=list(ROW))
                    writer.writeheader()
                    writer.writerow(ROW)
            (root / 'dm_fresh_leads.csv').write_text('unexpected,column\nwrong,data\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'source'):
                r.load_claims(root)


if __name__ == '__main__':
    unittest.main()
