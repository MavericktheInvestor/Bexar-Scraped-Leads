"""Offline safety tests. All names/document numbers below are synthetic."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scraper import subtrst_sheet_sync as sync


def record(**changes):
    result = dict(doc_num="20260000001", doc_type="SUBTRST", owner="EXAMPLE LLC",
                  is_business=True, is_motivated=False, score=None, flags=[],
                  filed="2026-11-18", mail_address="", prop_address="")
    result.update(changes)
    return result


def snapshot(*records):
    return dict(scrape_complete=True, total=len(records),
                fetched_at="2026-11-18T23:20:54Z", records=list(records))


class SourceTests(unittest.TestCase):
    def test_selects_all_subtrst_without_contact_business_score_filters(self):
        selected = sync.select_source(snapshot(record(), record(doc_num="20260000002", score=0),
                                               record(doc_type="AFFH", doc_num="20260000003")))
        self.assertEqual([r['doc_num'] for r in selected], ["20260000001", "20260000002"])
        self.assertIsNone(selected[0]['score'])

    def test_source_numeric_identity_and_nonfinite_scores_are_not_normalized(self):
        with self.assertRaises(ValueError):
            sync.select_source(snapshot(record(doc_num=20260000001)))
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.plan_rows([record(score=value)], [HEADERS])

    def test_requires_complete_well_formed_source_without_repairing_identity(self):
        for payload in [[], {}, snapshot(), dict(snapshot(record()), scrape_complete=False),
                        dict(snapshot(record()), total=2), snapshot(record(doc_num='20260000001 ')),
                        snapshot(record(doc_num='1e10')), snapshot(record(owner='')),
                        snapshot(record(doc_types=['SUBTRST'], doc_type='AFFH')),
                        snapshot(record(), record(owner='DIFFERENT OWNER'))]:
            with self.subTest(case=str(payload)[:30]), self.assertRaises(ValueError):
                sync.select_source(payload)
        composite = '20260000001 | 20260000002'
        selected = sync.select_source(snapshot(record(doc_num=composite), record(doc_num=composite)))
        self.assertEqual([r['doc_num'] for r in selected], [composite])
        with self.assertRaises(ValueError):
            sync.select_source(snapshot(record(doc_num='20260000001 | 20260000001')))


HEADERS = ['Last Name ', 'First name', 'Mailing Address', 'Mailing City', 'Mailing State',
           'Mailing Zip', 'Property Address', 'Property City', 'Property State', 'Property Zip',
           'Lead Type', 'Document Type', 'Date Filed', 'Document Number', 'Amount/Debt Owed',
           'Seller Score', 'Motivated Seller Flags', 'Source', 'Public Records URL', 'Status',
           'Partner Notes', 'Date Contacted']


class PlanningTests(unittest.TestCase):
    def test_maps_real_headers_dedupes_exact_identity_preserves_unknown_columns_and_blanks(self):
        headers = HEADERS + ['Custom', '', '']
        old = [''] * 23
        old[11] = 'SUBTRST'
        old[13] = '20260000001'
        old[20] = '=HYPERLINK("example", "note")'
        before = [headers, old, [], ['Column6']]
        original = copy.deepcopy(before)
        composite = '20260000002 | 20260000003'
        plan = sync.plan_rows([record(), record(doc_num=composite, owner='EXAMPLE, JANE')], before)
        self.assertEqual(before, original)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0:2], ['EXAMPLE', 'JANE'])
        self.assertEqual(plan[0][13], composite)
        self.assertEqual(plan[0][15], '')
        self.assertEqual(plan[0][17], 'Bexar County Clerk')
        self.assertEqual(plan[0][19:], ['', '', '', '', '', ''])
        self.assertEqual(sync.plan_rows([record(doc_num=composite)], before + plan), [])
        # Source identifiers remain exact: a component is not a composite identity.
        self.assertEqual(len(sync.plan_rows([record(doc_num='20260000002')], before + plan)), 1)

    def test_audit_dedupes_numeric_sheet_cells_without_changing_source_strings(self):
        row = [''] * 22
        row[11] = 'SUBTRST'
        row[13] = 20260000001
        self.assertEqual(sync.plan_rows([record()], [HEADERS, row]), [])
        self.assertEqual(row[13], 20260000001)
        with self.assertRaises(ValueError):
            sync.document_identity(20260000001.0)

    def test_unrelated_bottom_tables_are_preserved_not_treated_as_malformed_leads(self):
        footer = [''] * 66
        footer[11] = 'Column 6'
        footer[13] = 'Column 8'
        shifted = [''] * 66
        shifted[13] = 'Unrelated text'
        shifted[16] = 'Substitute Trustee'
        shifted[17] = 'Substitute Trustee'
        shifted[23] = 'Bexar County Clerk'
        before = [HEADERS] + [[] for _ in range(100)] + [footer, shifted]
        service = MemorySheets(before)
        report = sync.synchronize(service, snapshot(record()), 'apply', lambda _: None)
        self.assertEqual(report['inserted'], 1)
        self.assertEqual(service.rows[2:], before[1:])
        # An unrelated table's numeric field cannot suppress a proven source lead.
        unrelated = [''] * 66
        unrelated[13] = '20260000001'
        self.assertEqual(len(sync.plan_rows([record()], [HEADERS, unrelated])), 1)

    def test_rejects_ambiguous_header_mapping_and_existing_document_identity(self):
        bad_headers = [[], HEADERS[:-3], HEADERS + ['document number'],
                       ['wrong'] + HEADERS[1:]]
        for headers in bad_headers:
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                sync.plan_rows([record()], [headers])
        row = [''] * 22
        row[11] = 'SUBTRST'
        row[13] = 'BAD'
        with self.assertRaises(ValueError):
            sync.plan_rows([record()], [HEADERS, row])
        row[13] = '20260000001'
        with self.assertRaises(ValueError):
            sync.plan_rows([record()], [HEADERS, row, row])


class Request:
    def __init__(self, action):
        self.action = action

    def execute(self, **kwargs):
        return self.action()


class MemorySheets:
    """Offline Google API contract double with real mutable cells/grid bounds."""
    def __init__(self, rows, grid_rows=None):
        self.rows = copy.deepcopy(rows)
        self.grid_rows = grid_rows or len(rows)
        self.calls = []
        self.read_count = 0
        self.on_read = None
        self.corrupt_write = False

    def get(self, **kwargs):
        self.calls.append(('metadata', kwargs))
        return Request(lambda: {'properties': {'title': 'Synthetic leads'}, 'sheets': [
            {'properties': {'title': 'Sheet1', 'sheetId': 7,
                            'gridProperties': {'rowCount': self.grid_rows, 'columnCount': 70}}}]})

    def values(self):
        owner = self
        class Values:
            def get(self, **kwargs):
                owner.calls.append(('read', kwargs))
                def read():
                    owner.read_count += 1
                    if owner.on_read:
                        owner.on_read(owner)
                    return {'values': copy.deepcopy(owner.rows)}
                return Request(read)

            def update(self, **kwargs):
                owner.calls.append(('write', kwargs))
                def write():
                    start = int(kwargs['range'].split('!A')[1]) - 1
                    data = copy.deepcopy(kwargs['body']['values'])
                    if start + len(data) > owner.grid_rows:
                        raise RuntimeError('write exceeds grid')
                    while len(owner.rows) < start:
                        owner.rows.append([])
                    owner.rows[start:start+len(data)] = data
                    if owner.corrupt_write:
                        owner.rows[1][20] = 'CORRUPTED'
                    return {'updatedRows': len(data)}
                return Request(write)
        return Values()

    def batchUpdate(self, **kwargs):
        self.calls.append(('batch', kwargs))
        def apply():
            for request in kwargs['body']['requests']:
                if 'insertDimension' in request:
                    spec = request['insertDimension']
                    span = spec['range']
                    count = span['endIndex'] - span['startIndex']
                    self.rows[span['startIndex']:span['startIndex']] = [[] for _ in range(count)]
                    self.grid_rows += count
                elif 'updateCells' in request:
                    spec = request['updateCells']
                    assert spec['fields'] == 'userEnteredValue'
                    for i, row in enumerate(spec['rows'], spec['start']['rowIndex']):
                        self.rows[i] = [next(iter(c['userEnteredValue'].values()))
                                        for c in row['values']]
                elif 'moveDimension' in request:
                    spec = request['moveDimension']
                    span = spec['source']
                    assert span['dimension'] == 'ROWS' and span['sheetId'] == 7
                    start, end = span['startIndex'], span['endIndex']
                    dest = spec['destinationIndex']
                    assert dest < start  # all production moves are upward
                    moved = self.rows[start:end]
                    del self.rows[start:end]
                    self.rows[dest:dest] = moved
                else:
                    raise AssertionError('Unsafe request: ' + str(request.keys()))
            return {}
        return Request(apply)


class NewestFirstTests(unittest.TestCase):
    def test_consolidates_scattered_leads_by_parsed_date_in_one_atomic_batch(self):
        leads = sync.plan_rows([record(filed='9/30/2026'),
                                record(doc_num='20260000002', filed='2026-10-01'),
                                record(doc_num='20260000003', filed=46300)], [HEADERS])
        leads[0] += [''] * 47 + ['far-right manual note']
        before = [HEADERS, leads[0], [], ['unrelated'], [], leads[1], leads[2]]
        service = MemorySheets(before)
        payload = snapshot(record(), record(doc_num='20260000002'))
        audit = sync.synchronize(service, payload, 'audit', lambda _: None)
        self.assertGreater(audit.get('planned_moves', 0), 0)
        self.assertFalse(any(name == 'batch' for name, _ in service.calls))
        sync.synchronize(service, payload, 'apply', lambda _: None)
        self.assertEqual(service.rows, [HEADERS, leads[2], leads[1], leads[0], [], ['unrelated'], []])
        self.assertEqual(sum(name == 'batch' for name, _ in service.calls), 1)
        requests = next(k['body']['requests'] for n, k in service.calls if n == 'batch')
        self.assertTrue(all('moveDimension' in r for r in requests))
        sync.synchronize(service, payload, 'apply', lambda _: None)
        self.assertEqual(sum(name == 'batch' for name, _ in service.calls), 1)


    def test_future_arrivals_are_sorted_by_filing_date_not_ingestion_and_raw_typed(self):
        original = sync.plan_rows([record(filed='2026-10-15')], [HEADERS])[0]
        service = MemorySheets([HEADERS, original, [], ['unrelated']])
        newer = record(doc_num='20260000002', filed='2026-11-01', score=0,
                       owner='=NOT_A_FORMULA', amount=True)
        older = record(doc_num='20260000003', filed='2026-09-01')
        report = sync.synchronize(service, snapshot(record(), newer, older), 'apply', lambda _: None)
        self.assertEqual(report['inserted'], 2)
        self.assertEqual([r[13] for r in service.rows[1:4]],
                         ['20260000002', '20260000001', '20260000003'])
        self.assertEqual(service.rows[2], original)
        self.assertEqual(service.rows[4:], [[], ['unrelated']])
        requests = next(k['body']['requests'] for n, k in service.calls if n == 'batch')
        self.assertEqual(requests[1]['updateCells']['rows'][0]['values'][0],
                         {'userEnteredValue': {'stringValue': '=NOT_A_FORMULA'}})
        self.assertEqual(requests[1]['updateCells']['rows'][0]['values'][14],
                         {'userEnteredValue': {'boolValue': True}})

    def test_formula_anywhere_in_target_blocks_relocation_before_write(self):
        for formula_row in (['=SUM(A2:A10)'], [''] * 69 + ['=Sheet2!A1']):
            with self.subTest(width=len(formula_row)):
                service = MemorySheets([HEADERS, [], formula_row])
                with self.assertRaisesRegex(ValueError, 'formula'):
                    sync.synchronize(service, snapshot(record()), 'apply', lambda _: None)
                self.assertFalse(any(n == 'batch' for n, _ in service.calls))

    def test_other_tab_formula_blocks_move_without_touching_either_tab(self):
        class OtherTabSheets(MemorySheets):
            def get(self, **kwargs):
                response = super().get(**kwargs).execute()
                response['sheets'].append({'properties': {'title': "Other's tab", 'sheetId': 8,
                                                          'gridProperties': {'rowCount': 10, 'columnCount': 2}}})
                return Request(lambda: response)

            def values(self):
                base = super().values()
                original = base.get
                def get(**kwargs):
                    if kwargs['range'] == "'Other''s tab'":
                        self.calls.append(('other_read', kwargs))
                        return Request(lambda: {'values': [["=Sheet1!A2"]]})
                    return original(**kwargs)
                base.get = get
                return base
        service = OtherTabSheets([HEADERS, [], ['unrelated']])
        with self.assertRaisesRegex(ValueError, 'formula'):
            sync.synchronize(service, snapshot(record()), 'apply', lambda _: None)
        self.assertFalse(any(n == 'batch' for n, _ in service.calls))

    def test_invalid_lead_dates_fail_closed_without_mutation(self):
        for value in ('', 'tomorrow', '2/30/2026', '2026-13-01', float('inf'), True):
            with self.subTest(value=value):
                row = sync.plan_rows([record()], [HEADERS])[0]
                row[12] = value
                service = MemorySheets([HEADERS, row, [], ['footer']])
                with self.assertRaisesRegex(ValueError, 'Date Filed'):
                    sync.synchronize(service, snapshot(record()), 'apply', lambda _: None)
                self.assertFalse(any(n == 'batch' for n, _ in service.calls))


class SyncTests(unittest.TestCase):
    def test_audit_is_read_only_and_apply_preserves_all_cells_inserts_grid_rows_and_is_idempotent(self):
        old = [''] * 70
        old[11] = 'SUBTRST'
        old[13] = '20260000001'
        old[12] = '11/17/2026'
        old[20] = 'manual note'
        old[69] = 'far right unrelated'
        before = [HEADERS, old] + [[] for _ in range(100)] + [['Column6']]
        service = MemorySheets(before)
        output = []
        payload = snapshot(record(), record(doc_num='20260000002', score=0))
        report = sync.synchronize(service, payload, 'audit', output.append)
        self.assertEqual(report['missing'], 1)
        self.assertEqual(report['existing_physical_rows'], 102)
        self.assertEqual(report['existing_nonempty_rows'], 2)
        self.assertEqual(service.rows, before)
        self.assertFalse(any(c[0] in ('batch', 'write') for c in service.calls))
        self.assertIn('Synthetic leads', '\n'.join(output))
        self.assertIn('Sheet1', '\n'.join(output))
        self.assertNotIn('EXAMPLE', '\n'.join(output))
        result = sync.synchronize(service, payload, 'apply', output.append)
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(service.rows[2:], before[1:])
        write = next(kwargs for name, kwargs in service.calls if name == 'batch')
        requests = write['body']['requests']
        self.assertEqual(requests[0]['insertDimension']['range']['startIndex'], 1)
        self.assertEqual(requests[1]['updateCells']['rows'][0]['values'][15],
                         {'userEnteredValue': {'numberValue': 0}})
        read = next(kwargs for name, kwargs in service.calls if name == 'read')
        self.assertEqual(read['range'], "'Sheet1'")
        self.assertEqual(read['valueRenderOption'], 'FORMULA')
        writes = sum(name == 'batch' for name, _ in service.calls)
        again = sync.synchronize(service, payload, 'apply', output.append)
        self.assertEqual(again['inserted'], 0)
        self.assertEqual(sum(name == 'batch' for name, _ in service.calls), writes)

    def test_aborts_before_writing_on_concurrent_change(self):
        service = MemorySheets([HEADERS, ['unrelated']])
        def mutate(s):
            if s.read_count == 2:
                s.rows[1][0] = 'human edit'
        service.on_read = mutate
        with self.assertRaisesRegex(ValueError, 'changed before write'):
            sync.synchronize(service, snapshot(record()), 'apply', lambda _: None)
        self.assertFalse(any(name in ('write', 'batch') for name, _ in service.calls))

    def test_readback_rejects_boolean_to_number_type_change(self):
        service = MemorySheets([HEADERS, ['footer']])
        def mutate(s):
            if any(n == 'batch' for n, _ in s.calls):
                s.rows[1][14] = 1
        service.on_read = mutate
        with self.assertRaisesRegex(ValueError, 'readback'):
            sync.synchronize(service, snapshot(record(amount=True)), 'apply', lambda _: None)

    def test_readback_rejects_changed_grid_even_when_blank_rows_are_omitted(self):
        service = MemorySheets([HEADERS, ['footer']], grid_rows=50)
        def mutate(s):
            if any(n == 'batch' for n, _ in s.calls):
                s.grid_rows -= 1
        service.on_read = mutate
        with self.assertRaisesRegex(ValueError, 'readback'):
            sync.synchronize(service, snapshot(record()), 'apply', lambda _: None)

    def test_readback_rejects_mutation_missing_rows_and_wrong_values(self):
        for corruption in ('existing', 'missing', 'new'):
            with self.subTest(corruption=corruption):
                service = MemorySheets([HEADERS, ['unrelated']], grid_rows=100)
                def mutate(s):
                    if any(name == 'batch' for name, _ in s.calls):
                        if corruption == 'existing':
                            s.rows[-1][0] = 'changed'
                        elif corruption == 'missing':
                            s.rows.pop(1)
                        else:
                            s.rows[1][13] = '20260000009'
                service.on_read = mutate
                with self.assertRaisesRegex(ValueError, 'readback'):
                    sync.synchronize(service, snapshot(record()), 'apply', lambda _: None)


class CLITests(unittest.TestCase):
    def test_cli_defaults_audit_accepts_validated_archive_and_redacts_access_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.json'
            source.write_text(json.dumps(snapshot(record())), encoding='utf-8')
            service = MemorySheets([HEADERS])
            seen = []
            def factory(info, mode):
                seen.append((info, mode))
                return service
            env = {'GOOGLE_CREDENTIALS': json.dumps({'client_email': 'robot@project.iam.gserviceaccount.com',
                                                     'private_key': 'PRIVATE DO NOT PRINT'})}
            output = []
            self.assertEqual(sync.main(['--source', str(source)], env, factory, output.append), 0)
            self.assertEqual(seen[0][1], 'audit')
            self.assertFalse(any(name == 'write' for name, _ in service.calls))
            def denied(info, mode):
                raise RuntimeError('PRIVATE DO NOT PRINT personal record body')
            self.assertEqual(sync.main(['--source', str(source)], env, denied, output.append), 1)
            text = '\n'.join(output)
            self.assertIn('robot@project.iam.gserviceaccount.com', text)
            self.assertNotIn('PRIVATE', text)
            self.assertNotIn('personal record', text)
            self.assertEqual(sync.main(['--source', str(source)], {}, factory, output.append), 1)
            source.write_text(json.dumps(dict(snapshot(record()), scrape_complete=False)), encoding='utf-8')
            self.assertEqual(sync.main(['--source', str(source)], env, factory, output.append), 1)
            self.assertIn('Source must be a nonempty complete snapshot/archive', '\n'.join(output))


class ArchiveTests(unittest.TestCase):
    def test_archive_accumulates_deep_copies_without_inventing_filing_metadata(self):
        original = [record(), record(doc_type='AFFH', doc_num='20260000002')]
        captured = sync.capture_subtrst(original)
        original[0]['flags'].append('later merge mutation')
        self.assertEqual(captured[0]['flags'], [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'subtrst.json'
            sync.persist_archive(path, captured, '2026-11-18T00:00:00Z')
            sync.persist_archive(path, [record(doc_num='20260000003')], '2026-11-19T00:00:00Z')
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(payload['scrape_complete'])
            self.assertEqual(payload['total'], 2)
            self.assertEqual([r['doc_num'] for r in sync.select_source(payload)],
                             ['20260000001', '20260000003'])
            old = path.read_bytes()
            with self.assertRaises(ValueError):
                sync.persist_archive(path, [record(owner='CONFLICTING OWNER')], '2026-11-20T00:00:00Z')
            self.assertEqual(path.read_bytes(), old)
            with self.assertRaises(ValueError):
                sync.persist_archive(path, [record(doc_num='malformed')], '2026-11-20T00:00:00Z')
            self.assertEqual(path.read_bytes(), old)
            path.write_text('{"scrape_complete": false}', encoding='utf-8')
            with self.assertRaises(ValueError):
                sync.persist_archive(path, [record()], '2026-11-20T00:00:00Z')


if __name__ == '__main__':
    unittest.main()
