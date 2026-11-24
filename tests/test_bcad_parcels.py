"""Exercise the real ParcelLookup class without importing fetch.py side effects."""
import ast
from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scraper' / 'fetch.py'


def parcel_class():
    node = next(n for n in ast.parse(SOURCE.read_text(encoding='utf-8')).body
                if isinstance(n, ast.ClassDef) and n.name == 'ParcelLookup')
    namespace = {'re': re, 'safe': lambda v: str(v).strip() if v is not None else '',
                 'time': types.SimpleNamespace(sleep=lambda _: None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace['ParcelLookup']


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = payload.get('_status', 200)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP request failed; sensitive URL must not be logged')

    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(next(self.payloads))


class ParcelMappingTests(unittest.TestCase):
    def test_only_requests_target_owners_from_official_ordered_api(self):
        import inspect
        cls = parcel_class()
        self.assertIn('owners', inspect.signature(cls).parameters,
                      'ParcelLookup must accept targeted owner names, not download the county')
        attrs = {'pacs_prop_id': 100, 'prop_val_yr': 2027,
                 'owner_name': "O'NEIL, JANE", 'situs': '123 SAMPLE ST'}
        session = Session([{'count': 1}, {'features': [{'attributes': attrs}]}, {'count': 1}])
        with redirect_stdout(io.StringIO()):
            lookup = cls(owners=["O'NEIL, JANE"], session=session)
        self.assertEqual(lookup.lookup("O'NEIL, JANE")['prop_address'], '123 SAMPLE ST')
        self.assertTrue(session.calls)
        for url, kwargs in session.calls:
            self.assertEqual(url, 'https://maps.bcad.org/arcgis/rest/services/PAMapSearch/MapServer/9/query')
            self.assertIn("O''NEIL", kwargs['params']['where'])
            self.assertNotEqual(kwargs.get('verify'), False)
        pages = [kwargs['params'] for _, kwargs in session.calls if 'outFields' in kwargs['params']]
        self.assertEqual(pages[0]['orderByFields'], 'pacs_prop_id,prop_val_yr,owner_name')
        self.assertEqual(pages[0]['resultOffset'], 0)


    def test_transient_server_failure_is_retried_but_denial_is_not(self):
        cls = parcel_class()
        session = Session([{'_status': 503}, {'count': 0}, {'count': 0}])
        with redirect_stdout(io.StringIO()):
            lookup = cls(owners=['EXAMPLE LLC'], session=session)
        self.assertEqual(lookup.status, 'complete')
        self.assertEqual(len(session.calls), 3)
        denied = Session([{'_status': 403}])
        with redirect_stdout(io.StringIO()):
            lookup = cls(owners=['EXAMPLE LLC'], session=denied)
        self.assertEqual(lookup.status, 'unavailable')
        self.assertEqual(len(denied.calls), 1)
        self.assertEqual(lookup.idx, {})

    def test_partial_names_and_multiple_properties_are_not_matches(self):
        cls = parcel_class()
        lookup = cls.__new__(cls)
        lookup.idx = {}
        row = {'pacs_prop_id': 100, 'prop_val_yr': 2027,
               'owner_name': 'DOE, JANE', 'situs': '123 SAMPLE ST'}
        self.assertEqual(cls._v('DOE'), [])
        self.assertEqual(cls._v('DOE,'), [])
        lookup._add(row)
        self.assertEqual(lookup.lookup('DOE'), {})
        self.assertEqual(lookup.lookup('JANE'), {})
        self.assertTrue(lookup.lookup('JANE DOE'))
        lookup._add({**row, 'pacs_prop_id': 200, 'situs': '456 OTHER ST'})
        self.assertEqual(lookup.lookup('DOE, JANE'), {})
        self.assertEqual(lookup.lookup('JANE DOE'), {})

    def test_ordered_pagination_is_exhausted_before_matches_are_used(self):
        cls = parcel_class()
        cls.PAGE_SIZE = 1
        row = {'pacs_prop_id': 100, 'prop_val_yr': 2026, 'owner_name': 'DOE, JANE', 'situs': 'OLD ADDRESS'}
        new = {**row, 'prop_val_yr': 2027, 'situs': 'CURRENT ADDRESS'}
        session = Session([{'count': 2}, {'features': [{'attributes': row}]},
                           {'features': [{'attributes': new}]}, {'count': 2}])
        with redirect_stdout(io.StringIO()):
            lookup = cls(owners=['DOE, JANE'], session=session)
        self.assertEqual(lookup.status, 'complete')
        self.assertEqual(lookup.lookup('DOE, JANE')['prop_address'], 'CURRENT ADDRESS')
        self.assertEqual(lookup.lookup('DOE, JANE')['bcad_data_year'], 2027)
        self.assertEqual([kw['params']['resultOffset'] for _, kw in session.calls
                          if 'outFields' in kw['params']], [0, 1])

    def test_incomplete_malformed_repeated_or_changing_results_discard_all_matches(self):
        row = {'pacs_prop_id': 100, 'prop_val_yr': 2027, 'owner_name': 'DOE, JANE', 'situs': '123 SAMPLE ST'}
        feature = {'features': [{'attributes': row}]}
        cases = [
            [{'error': {'code': 400}}],
            [{'count': '1'}],
            [{'count': 2}, feature, {'features': []}],
            [{'count': 2}, feature, feature],
            [{'count': 1}, {'features': [{'attributes': {'owner_name': 'DOE, JANE'}}]}],
            [{'count': 1}, feature, {'count': 2}],
        ]
        for payloads in cases:
            with self.subTest(payloads=payloads):
                cls = parcel_class()
                session = Session(payloads)
                log = io.StringIO()
                with redirect_stdout(log):
                    lookup = cls(owners=['DOE, JANE'], session=session)
                self.assertEqual(lookup.status, 'unavailable')
                self.assertEqual(lookup.idx, {})
                self.assertEqual(lookup.lookup('DOE, JANE'), {})
                self.assertNotIn('DOE', log.getvalue())

    def test_empty_owner_list_does_not_make_network_requests(self):
        cls = parcel_class()
        session = Session([])
        lookup = cls(owners=[], session=session)
        self.assertEqual(session.calls, [])
        self.assertEqual(lookup.status, 'not_requested')

    def test_failed_later_batch_discards_earlier_batch(self):
        cls = parcel_class()
        owners = [f'EXAMPLE LLC {i:02d}' for i in range(11)]
        row = {'pacs_prop_id': 100, 'prop_val_yr': 2027, 'owner_name': owners[0], 'situs': '123 SAMPLE ST'}
        session = Session([{'count': 1}, {'features': [{'attributes': row}]},
                           {'count': 1}, {'error': {'code': 500}}])
        with redirect_stdout(io.StringIO()):
            lookup = cls(owners=owners, session=session)
        self.assertEqual(lookup.status, 'unavailable')
        self.assertEqual(lookup.idx, {})

    def test_maps_official_arcgis_owner_and_address_fields(self):
        cls = parcel_class()
        lookup = cls.__new__(cls)
        lookup.idx = {}
        lookup._add({
            'pacs_prop_id': 100, 'prop_val_yr': 2027, 'owner_name': 'DOE, JANE',
            'situs': '123 SAMPLE ST, SAN ANTONIO, TX 78201',
            'addr_line1': 'PO BOX 7', 'addr_line2': '', 'addr_line3': '',
            'addr_city': 'SAN ANTONIO', 'addr_state': 'TX', 'addr_zip': '78202',
        })
        hit = lookup.lookup('DOE, JANE')
        self.assertEqual(hit['prop_address'], '123 SAMPLE ST, SAN ANTONIO, TX 78201')
        self.assertEqual(hit['mail_address'], 'PO BOX 7')
        self.assertEqual(hit['mail_zip'], '78202')


if __name__ == '__main__':
    unittest.main()
