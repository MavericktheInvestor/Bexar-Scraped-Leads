import copy
import csv
import io
import unittest
from pathlib import Path
from scraper import completed_trace_sheet_sync as sync


HEAD = ['Document Number', 'First Name', 'Last Name', 'Property Address', 'Date Filed', 'Partner Notes', 'Formula']
DOC = '20260123456'


def source(**updates):
    row = dict(doc_num=DOC, first='Jane', last='Doe', prop_address='123 Main St',
               **{'Phone 1': '(210) 555-0101', 'Phone 2': '', 'Skip Trace Status': 'Done', 'Skip Trace Date': '2026-11-01'})
    row.update(updates)
    return row


def master():
    return [HEAD[:], [DOC, 'Jane', 'Doe', '123 Main St', '2026-09-01', 'manual note', '=1+1']]


class PlanTests(unittest.TestCase):
    def test_property_mismatch_and_incomplete_identity_are_distinguished(self):
        cases = [
            ({}, ['Jane', 'Doe', '999 Other St'], 'property_mismatch', True, False, True, True),
            ({}, ['Doe', 'Jane', '999 Other St'], 'property_mismatch', False, True, True, True),
            ({'prop_address': ''}, ['Jane', 'Doe', ''], 'incomplete_identity', True, False, False, True),
            ({'first': '', 'last': ''}, ['', '', '123 Main St'], 'incomplete_identity', True, True, True, False),
            ({}, ['Jane', 'Other', '123 Main St'], 'name_mismatch', False, False, True, True),
        ]
        for updates, values, classification, direct, swapped, present, populated in cases:
            with self.subTest(classification=classification, values=values):
                rows = master(); rows[1][1:4] = values
                plan = sync.plan_patch({DOC: source(**updates)}, rows)
                diagnostic = plan['master_identity_diagnostics']
                candidate = diagnostic['candidates'][0]
                self.assertEqual({classification: 1}, diagnostic['classifications'])
                self.assertEqual(classification, candidate['classification'])
                self.assertEqual(direct, candidate['direct_name_match'])
                self.assertEqual(swapped, candidate['swapped_name_match'])
                self.assertEqual(present, candidate['source_property_present'])
                self.assertEqual(populated, candidate['firstlast_both_populated'])
                self.assertEqual([], plan['changes'])
                self.assertEqual({'master_owner_property_collision': 1}, plan['holds'])

    def test_exact_name_swap_allows_only_trace_additions_without_renaming(self):
        import hashlib
        chosen, _ = sync.select_completed([[source()]])
        rows = master()
        rows[0][1:3] = ['Last Name ', 'First name']
        before = copy.deepcopy(rows)
        plan = sync.plan_patch(chosen, rows)
        diagnostic = plan['master_identity_diagnostics']
        candidate = diagnostic['candidates'][0]
        self.assertEqual(1, diagnostic['counts']['swapped_name_match'])
        self.assertEqual({'name_swap_property_exact': 1}, diagnostic['classifications'])
        self.assertEqual(1, candidate['master_row_index'])
        self.assertEqual(hashlib.sha256(DOC.encode()).hexdigest(), candidate['identity_sha256'])
        self.assertFalse(candidate['direct_name_match'])
        for key in ['swapped_name_match', 'property_exact', 'source_property_present', 'firstlast_both_populated']:
            self.assertTrue(candidate[key])
        self.assertEqual({}, plan['holds'])
        self.assertEqual(1, plan['matched'])
        self.assertEqual(
            [(0, 7, 'Phone 1'), (0, 8, 'Phone 2'),
             (0, 9, 'Skip Trace Status'), (0, 10, 'Skip Trace Date'),
             (1, 7, '(210) 555-0101'), (1, 9, 'Done'), (1, 10, '2026-11-01')],
            plan['changes'])
        after = sync.apply_matrix(rows, plan['changes'])
        self.assertEqual(before, [row[:7] for row in after])
        self.assertEqual([], sync.plan_patch(chosen, after)['changes'])
        self.assertEqual(before, rows)

    def test_reversed_names_do_not_bypass_identity_or_manual_holds(self):
        cases = []
        def reversed_master():
            rows = master()
            rows[0][1:3] = ['Last Name', 'First Name']
            return rows
        for updates, values in [
                ({}, ['Jane', 'Other', '123 Main St']),
                ({}, ['Jane', 'Doe', '999 Other St']),
                ({'prop_address': ''}, ['Jane', 'Doe', '']),
                ({'first': ''}, ['', 'Doe', '123 Main St']),
                ({'last': ''}, ['Jane', '', '123 Main St']),
                ({}, ['', 'Doe', '123 Main St']),
                ({}, ['Jane', '', '123 Main St'])]:
            rows = reversed_master(); rows[1][1:4] = values
            cases.append(([source(**updates)], rows, 'master_owner_property_collision'))
        rows = reversed_master(); rows.append(rows[1][:])
        cases.append(([source()], rows, 'duplicate_master_identity'))
        for identity in [' ' + DOC, DOC + ' | 20260123457']:
            rows = reversed_master(); rows[1][0] = identity
            cases.append(([source()], rows, 'missing_master_row'))
        for status in ['Hold', 'Skipped', 'No Phone', 'Do Not Contact', 'DNC']:
            rows = reversed_master(); rows[0].append('Status'); rows[1].append(status)
            cases.append(([source()], rows, 'manual_lead_hold'))
        for label, value in [('Phone 1', '2105550199'), ('Phone 2', '=A2'),
                             ('Skip Trace Status', 'Hold'), ('Skip Trace Date', '2026-10-01')]:
            rows = reversed_master(); rows[0].append(label); rows[1].append(value)
            cases.append(([source()], rows, 'existing_contact_conflict'))
        for records, rows, reason in cases:
            with self.subTest(reason=reason, row=rows[1]):
                before = copy.deepcopy(rows)
                chosen, _ = sync.select_completed([records])
                plan = sync.plan_patch(chosen, rows)
                self.assertEqual([], plan['changes'])
                self.assertEqual({reason: 1}, plan['holds'])
                self.assertEqual(before, rows)
        # Direct matching keeps its pre-existing single populated name behavior.
        rows = master(); rows[1][1] = ''
        chosen, _ = sync.select_completed([[source(first='')]])
        self.assertEqual(1, sync.plan_patch(chosen, rows)['matched'])

    def test_reversed_names_require_literal_valid_selected_document_identity(self):
        rows = master(); rows[0][1:3] = ['Last Name', 'First Name']
        for identity, record in [(' ' + DOC, source(doc_num=' ' + DOC)),
                                 (DOC, source(doc_num=DOC + ' | 20260123457'))]:
            with self.subTest(identity=identity):
                rows[1][0] = identity
                plan = sync.plan_patch({identity: record}, rows)
                self.assertEqual([], plan['changes'])
                self.assertEqual({'master_owner_property_collision': 1}, plan['holds'])

    def test_pretrace_history_schema_is_parsed_but_never_qualifies(self):
        rows = sync.parse_csv('doc_num,first,last,prop_address\n20260123456,Jane,Doe,123 Main St\n')
        chosen, held = sync.select_completed([rows])
        self.assertEqual({}, chosen)
        self.assertEqual({'latest_not_completed': 1}, held)

    def test_history_parsing_uses_actual_headers_and_fails_closed(self):
        import tempfile
        import subprocess
        required = list(source())
        text = io.StringIO()
        writer = csv.DictWriter(text, fieldnames=required)
        writer.writeheader()
        writer.writerow(source())
        parsed = sync.parse_csv(text.getvalue())
        self.assertEqual([source()], parsed)
        sheet = [['Skip Trace Status', 'Property Address', 'Document Number', 'Last Name', 'First Name', 'Phone 1', 'Phone 2', 'Skip Trace Date', 'Date Filed'],
                 ['Done', '123 Main St', DOC, 'Doe', 'Jane', '2105550101', '', '2026-11-01', '2026-09-01']]
        self.assertEqual('Done', sync.parse_rows(sheet)[0]['Skip Trace Status'])
        for bad in ['', 'doc_num,doc_num\n1,2\n', text.getvalue() + 'too,few\n', text.getvalue() + '\"unclosed']:
            with self.subTest(bad=bad[:10]), self.assertRaises(sync.SyncError):
                sync.parse_csv(bad)
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], stderr=subprocess.DEVNULL)
            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Test')
            path = Path(directory) / 'data' / 'skip_traced.csv'
            path.parent.mkdir()
            for content in [text.getvalue(), text.getvalue().replace('Done', 'Skipped')]:
                path.write_text(content, encoding='utf-8')
                git('add', '.')
                git('commit', '-qm', 'fixture')
            snapshots, stats = sync.load_history(directory)
            self.assertEqual(2, stats['revisions_parsed'])
            self.assertEqual('Skipped', snapshots[0][0]['Skip Trace Status'])
            path.write_text('broken', encoding='utf-8')
            git('add', '.')
            git('commit', '-qm', 'broken')
            with self.assertRaises(sync.SyncError):
                sync.load_history(directory)

    def test_ambiguous_or_unsupported_source_identity_is_held(self):
        cases = [
            [[source(), source()]],
            [[source()], [source(last='Other')]],
            [[source(**{'Phone 1': 'invalid'})]],
            [[source(**{'Skip Trace Date': 'yesterday'})]],
            [[source(doc_num=' ' + DOC)]],
            [[source()], [source(**{'Phone 1': '2105550199'})]],
        ]
        for snapshots in cases:
            with self.subTest(case=cases.index(snapshots)):
                chosen, held = sync.select_completed(snapshots)
                self.assertEqual({}, chosen)
                self.assertGreater(sum(held.values()), 0)
        composite = DOC + ' | 20260123457'
        chosen, _ = sync.select_completed([[source(doc_num=composite)], [source(doc_num=DOC)]])
        self.assertEqual({composite, DOC}, set(chosen))
        newer = source(**{'Phone 1': '2105550199', 'Skip Trace Date': '2026-11-02'})
        chosen, _ = sync.select_completed([[newer], [source()]])
        self.assertEqual('2105550199', chosen[DOC]['Phone 1'])

    def test_newest_presence_controls_holds_without_revoking_absent_history(self):
        for status in ['Skipped', 'No Phone', 'Error', 'Hold', 'Pending']:
            with self.subTest(status=status):
                chosen, held = sync.select_completed([[source(**{'Skip Trace Status': status})], [source()]])
                self.assertEqual({}, chosen)
                self.assertEqual(1, sum(held.values()))
        chosen, held = sync.select_completed([[], [source()]])
        self.assertEqual([DOC], list(chosen))
        self.assertEqual({}, held)

    def test_unmapped_phone_schema_and_manual_lead_holds_fail_closed(self):
        chosen, _ = sync.select_completed([[source()]])
        rows = master(); rows[0].append('Phone Number'); rows[1].append('2105550199')
        self.assertEqual([], sync.plan_patch(chosen, rows)['changes'])
        rows = master(); rows[0].append('Additional Phone Details')
        with self.assertRaises(sync.SyncError):
            sync.plan_patch(chosen, rows)
        for status in ['Hold', 'Skipped', 'No Phone', 'Do Not Contact', 'DNC']:
            rows = master(); rows[0].append('Status'); rows[1].append(status)
            self.assertEqual([], sync.plan_patch(chosen, rows)['changes'])

    def test_master_collisions_and_manual_data_hold_entire_lead(self):
        chosen, _ = sync.select_completed([[source()]])
        cases = []
        duplicate = master(); duplicate.append(duplicate[1][:]); cases.append(duplicate)
        collision = master(); collision[1][2] = 'Other'; cases.append(collision)
        missing = [HEAD[:]]; cases.append(missing)
        for label, value in [('Phone 1', '2105550199'), ('Phone 2', '2105550199'),
                             ('Skip Trace Status', 'Hold'), ('Skip Trace Date', '2026-10-01')]:
            rows = master(); rows[0].append(label); rows[1].append(value); cases.append(rows)
        for rows in cases:
            with self.subTest(case=cases.index(rows)):
                plan = sync.plan_patch(chosen, rows)
                self.assertEqual([], plan['changes'])
                self.assertEqual(1, sum(plan['holds'].values()))
        rows = master(); rows[0].append('Phone 1'); rows[1].append('+1 210-555-0101')
        plan = sync.plan_patch(chosen, rows)
        self.assertFalse(any(r == 1 and c == 7 for r, c, value in plan['changes']))
        rows = master(); rows[0].append('Phone 1'); rows[1].append('=A2')
        self.assertEqual([], sync.plan_patch(chosen, rows)['changes'])

    def test_shifted_headers_map_status_and_date_without_touching_unrelated_cells(self):
        rows = master()
        rows[0] += ['Skip Trace Date', 'Phone 2', 'Skip Trace Status', 'Phone 1']
        rows[1] += ['', '', '', '']
        order = list(reversed(range(len(rows[0]))))
        shifted = [[row[i] for i in order] for row in rows]
        chosen, _ = sync.select_completed([[source()]])
        plan = sync.plan_patch(chosen, shifted)
        self.assertEqual([], plan['new_headers'])
        after = sync.apply_matrix(shifted, plan['changes'])
        self.assertEqual('Done', after[1][shifted[0].index('Skip Trace Status')])
        self.assertEqual('2026-11-01', after[1][shifted[0].index('Skip Trace Date')])
        self.assertEqual('2026-09-01', after[1][shifted[0].index('Date Filed')])
        rows = master(); rows[0] += ['Phone', 'Phone 1']
        with self.assertRaises(sync.SyncError):
            sync.plan_patch(chosen, rows)

    def test_existing_equivalent_phone_in_other_slot_preserves_both_source_numbers(self):
        record = source(**{'Phone 2': '2105550102'})
        chosen, _ = sync.select_completed([[record]])
        for existing_label, existing_phone in [('Phone 1', '2105550102'), ('Phone 2', '2105550101')]:
            rows = master(); rows[0].append(existing_label); rows[1].append(existing_phone)
            after = sync.apply_matrix(rows, sync.plan_patch(chosen, rows)['changes'])
            mapping = sync.header_mapping(after[0], ())
            numbers = {sync.phone(after[1][mapping[label]]) for label in sync.OUTPUT_HEADERS[:2]}
            self.assertEqual({'2105550101', '2105550102'}, numbers)
            self.assertEqual([], sync.plan_patch(chosen, after)['changes'])

    def test_phone_only_patch_preserves_notes_formula_and_filing_date(self):
        before = master()
        selected, held = sync.select_completed([[source()]])
        plan = sync.plan_patch(selected, before)
        self.assertEqual({}, held)
        self.assertEqual(['Phone 1', 'Phone 2', 'Skip Trace Status', 'Skip Trace Date'], plan['new_headers'])
        after = sync.apply_matrix(before, plan['changes'])
        self.assertEqual(before[1], after[1][:len(before[1])])
        self.assertEqual(['(210) 555-0101', '', 'Done', '2026-11-01'], after[1][7:])
        self.assertEqual([], sync.plan_patch(selected, after)['changes'])


class MemoryTransport:
    def __init__(self):
        self.state = {'meta': {'properties': {'title': 'Master'}, 'sheets': [
            {'properties': {'sheetId': 0, 'title': sync.TARGET, 'gridProperties': {'rowCount': 100, 'columnCount': 12}}},
            {'properties': {'sheetId': 1, 'title': sync.QUEUE, 'gridProperties': {'rowCount': 100, 'columnCount': 12}}}]},
            'rows': {sync.TARGET: master(), sync.QUEUE: [list(source()), list(source().values())]}}
        self.notes = {(0, 1, 5): 'native cell note'}
        self.writes = 0
        self.reads = 0
        self.mutate_at = None
        self.corrupt = False

    def read(self):
        self.reads += 1
        if self.reads == self.mutate_at:
            self.state['rows'][sync.TARGET][1][5] = 'concurrent edit'
        return copy.deepcopy(self.state)

    def write(self, changes, columns):
        self.writes += 1
        props = self.state['meta']['sheets'][0]['properties']
        props['gridProperties']['columnCount'] = columns
        self.state['rows'][sync.TARGET] = sync.apply_matrix(self.state['rows'][sync.TARGET], changes)
        if self.corrupt:
            self.state['rows'][sync.TARGET][1][5] = 'lost note'


class ProcessTests(unittest.TestCase):
    def test_seven_swapped_candidates_preserve_full_snapshot_and_are_idempotent(self):
        import json
        import hashlib
        records = [source(doc_num=str(20260123000 + i),
                          **{'Phone 2': '2105550102'}) for i in range(7)]
        for mode in ['audit', 'apply']:
            transport = MemoryTransport()
            header = HEAD[:]; header[1:3] = ['Last Name ', 'First name']
            rows = [header] + [[r['doc_num'], ' Jane ', ' DOE ', '123  MAIN st',
                                '2026-09-01', 'manual note', '=1+1'] for r in records]
            transport.state['rows'][sync.TARGET] = rows
            transport.state['rows'][sync.QUEUE] = [list(source())]
            before = copy.deepcopy(transport.state)
            notes = copy.deepcopy(transport.notes)
            report = sync.synchronize(transport, [records], mode=mode)
            diagnostics = report['master_identity_diagnostics']
            self.assertEqual(7, len(diagnostics['candidates']))
            self.assertEqual({'name_swap_property_exact': 7}, diagnostics['classifications'])
            self.assertEqual(7, diagnostics['counts']['swapped_name_match'])
            self.assertEqual(0, diagnostics['counts']['direct_name_match'])
            self.assertEqual({}, report['source_holds'])
            self.assertEqual({}, report['master_holds'])
            self.assertEqual(7, report['selected_completed'])
            self.assertEqual(7, report['planned_leads'])
            self.assertEqual(sync.OUTPUT_HEADERS, report['planned_headers'])
            self.assertEqual(32, report['planned_cells'])
            expected = copy.deepcopy(before)
            if mode == 'apply':
                expected['meta']['sheets'][0]['properties']['gridProperties']['columnCount'] = 16
                expected['rows'][sync.TARGET] = [header + [''] * 5 + sync.OUTPUT_HEADERS] + [
                    row + [''] * 5 + [record[key] for key in sync.OUTPUT_HEADERS]
                    for row, record in zip(rows[1:], records)]
                self.assertEqual(32, report['written_cells'])
                self.assertTrue(report['verified'])
                self.assertEqual(0, report['immediate_rerun_cells'])
                rerun = sync.synchronize(transport, [records], mode='apply')
                self.assertEqual(0, rerun['written_cells'])
                self.assertEqual(7, rerun['already_synced'])
                self.assertTrue(rerun['verified'])
            else:
                self.assertEqual(0, report['written_cells'])
                self.assertFalse(report['verified'])
            self.assertEqual(int(mode == 'apply'), transport.writes)
            self.assertEqual(expected, transport.state)
            self.assertEqual(notes, transport.notes)
            self.assertEqual(header, report['tabs'][0]['headers'])
            self.assertEqual(header, sync.profile_snapshot(before)['tabs'][0]['headers'])
            serialized = json.dumps(report)
            for record in records:
                for key in ['doc_num', 'first', 'last', 'prop_address', 'Phone 1', 'Phone 2']:
                    self.assertNotIn(record[key], serialized)
            for i, candidate in enumerate(diagnostics['candidates'], 1):
                self.assertEqual(i, candidate['master_row_index'])
                self.assertEqual(hashlib.sha256(records[i - 1]['doc_num'].encode()).hexdigest(), candidate['identity_sha256'])

    def test_reversed_names_do_not_bypass_source_decisions_or_duplicates(self):
        cases = [
            [[source(), source()]],
            [[source()], [source(first='Other')]],
            *[[[source(**{'Skip Trace Status': status})], [source()]]
              for status in ['', 'Hold', 'Skipped', 'No Phone', 'Pending', 'Error']],
        ]
        for history in cases:
            with self.subTest(history=history):
                transport = MemoryTransport()
                transport.state['rows'][sync.TARGET][0][1:3] = ['Last Name', 'First Name']
                transport.state['rows'][sync.QUEUE] = [list(source())]
                before = copy.deepcopy(transport.state)
                report = sync.synchronize(transport, history, mode='apply')
                self.assertEqual(0, report['planned_leads'])
                self.assertEqual(0, report['written_cells'])
                self.assertGreater(sum(report['source_holds'].values()), 0)
                self.assertEqual(0, transport.writes)
                self.assertEqual(before, transport.state)
                self.assertTrue(report['verified'])

    def test_live_blank_reset_recovers_done_without_bypassing_decisions_or_collisions(self):
        cases = [
            ('', [[source()]], 1),
            ('   ', [[source()]], 1),
            *[(status, [[source()]], 0) for status in ['Skipped', 'No Phone', 'Hold', 'Pending', 'Error']],
            *[('', [[source(**{'Skip Trace Status': status})], [source()]], 0)
              for status in ['', 'Skipped', 'No Phone', 'Hold', 'Pending']],
            ('', [[source(last='Other')]], 0),
        ]
        for status, history, expected in cases:
            with self.subTest(status=status, history=history):
                transport = MemoryTransport()
                reset = source(**{'Phone 1': '', 'Phone 2': '',
                                  'Skip Trace Status': status, 'Skip Trace Date': ''})
                transport.state['rows'][sync.QUEUE] = [list(reset), list(reset.values())]
                report = sync.synchronize(transport, history, mode='apply')
                self.assertEqual(expected, report['planned_leads'])
                self.assertEqual(expected, transport.writes)
                self.assertTrue(report['verified'])
                if expected:
                    self.assertGreater(report['written_cells'], 0)
                    self.assertEqual(0, report['immediate_rerun_cells'])
                else:
                    self.assertGreater(sum(report['source_holds'].values()), 0)

    def test_audit_apply_full_snapshot_readback_and_immediate_no_write_rerun(self):
        transport = MemoryTransport()
        before = copy.deepcopy(transport.state)
        report = sync.synchronize(transport, [[source()]], mode='audit')
        self.assertEqual(0, transport.writes)
        self.assertEqual(['Phone 1', 'Phone 2', 'Skip Trace Status', 'Skip Trace Date'], report['planned_headers'])
        self.assertEqual(2, len(report['tabs']))
        self.assertEqual(12, report['tabs'][0]['grid']['columnCount'])
        report = sync.synchronize(transport, [[source()]], mode='apply')
        self.assertEqual(1, transport.writes)
        self.assertEqual(7, report['written_cells'])
        self.assertEqual('native cell note', transport.notes[(0, 1, 5)])
        after = transport.state['rows'][sync.TARGET]
        self.assertEqual(before['rows'][sync.TARGET][1], after[1][:7])
        self.assertEqual('Phone 1', after[0][12])
        self.assertEqual(0, sync.synchronize(transport, [[source()]], mode='apply')['written_cells'])
        self.assertEqual(1, transport.writes)

    def test_changed_snapshot_aborts_and_bad_readback_is_not_success(self):
        transport = MemoryTransport(); transport.mutate_at = 2
        with self.assertRaises(sync.SyncError):
            sync.synchronize(transport, [[source()]], mode='apply')
        self.assertEqual(0, transport.writes)
        transport = MemoryTransport(); transport.corrupt = True
        with self.assertRaises(sync.SyncError):
            sync.synchronize(transport, [[source()]], mode='apply')
        self.assertEqual(1, transport.writes)


class GoogleAdapterTests(unittest.TestCase):
    def test_real_api_adapter_only_updates_exact_cell_values_and_appends_columns(self):
        class Request:
            def __init__(self, data): self.data = data
            def execute(self): return copy.deepcopy(self.data)
        class API:
            def __init__(self): self.memory = MemoryTransport(); self.requests = []; self.options = []
            def values(self): return self
            def get(self, **kwargs):
                self.options.append(kwargs)
                if 'range' in kwargs:
                    title = kwargs['range'][1:-1].replace("''", "'")
                    return Request({'values': self.memory.state['rows'][title]})
                return Request(self.memory.state['meta'])
            def batchUpdate(self, **kwargs):
                self.requests.extend(kwargs['body']['requests'])
                for request in kwargs['body']['requests']:
                    if 'appendDimension' in request:
                        item = request['appendDimension']
                        self.assert_dimension(item)
                        self.memory.state['meta']['sheets'][0]['properties']['gridProperties']['columnCount'] += item['length']
                    else:
                        item = request['updateCells']
                        assert item['fields'] == 'userEnteredValue'
                        assert len(item['rows']) == len(item['rows'][0]['values']) == 1
                        start = item['start']; value = item['rows'][0]['values'][0]['userEnteredValue']['stringValue']
                        assert start['columnIndex'] < self.memory.state['meta']['sheets'][0]['properties']['gridProperties']['columnCount']
                        self.memory.state['rows'][sync.TARGET] = sync.apply_matrix(self.memory.state['rows'][sync.TARGET], [(start['rowIndex'], start['columnIndex'], value)])
                return Request({})
            def assert_dimension(self, item):
                assert item['sheetId'] == 0 and item['dimension'] == 'COLUMNS'
        api = API()
        report = sync.synchronize(sync.GoogleTransport(api), [[source()]], 'apply')
        self.assertTrue(report['verified'])
        self.assertEqual(8, len(api.requests))
        for options in api.options:
            self.assertEqual(sync.SHEET_ID, options['spreadsheetId'])
            if 'range' in options:
                self.assertEqual('FORMULA', options['valueRenderOption'])
        self.assertEqual('native cell note', api.memory.notes[(0, 1, 5)])
        # Live Sheets cells can be integers; exercise the real adapter, not just the planner.
        numeric = source(**{'Phone 1': 2105550101, 'Phone 2': 12105550102})
        api = API()
        api.memory.state['rows'][sync.QUEUE] = [list(numeric), list(numeric.values())]
        report = sync.synchronize(sync.GoogleTransport(api), [], 'apply')
        self.assertTrue(report['verified'])
        self.assertEqual(0, report['immediate_rerun_cells'])
        rows = api.memory.state['rows'][sync.TARGET]
        self.assertEqual('2105550101', rows[1][rows[0].index('Phone 1')])
        self.assertEqual('12105550102', rows[1][rows[0].index('Phone 2')])
        self.assertEqual(0, sync.synchronize(sync.GoogleTransport(api), [], 'apply')['written_cells'])


class CLITests(unittest.TestCase):
    def test_actual_repository_offline_audit_and_apply_artifact_never_log_lead_data(self):
        import json
        import tempfile
        repo = Path(__file__).resolve().parents[1]
        history, stats = sync.load_history(repo)
        chosen, _ = sync.select_completed(history)
        self.assertEqual(stats['revisions_enumerated'], stats['revisions_parsed'])
        self.assertGreater(stats['revisions_parsed'], 0)
        # Exercise actual accepted history against a private offline matrix.
        rows = [HEAD[:]] + [[doc, r['first'], r['last'], r['prop_address'], '2026-01-01', 'keep', '=1+1'] for doc, r in chosen.items()]
        transport = MemoryTransport()
        transport.state['rows'][sync.TARGET] = rows
        transport.state['rows'][sync.QUEUE] = [list(source())]
        log = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            result = sync.main(['--repo', str(repo), '--summary', str(output)],
                environ={'GOOGLE_CREDENTIALS': '{"test":true}'},
                transport_factory=lambda info, mode: transport, log=log.append)
            self.assertEqual(0, result)
            report = json.loads(output.read_text())
            self.assertEqual('audit', report['mode'])
            self.assertEqual(len(chosen), report['history_completed_identities'])
            self.assertEqual(0, transport.writes)
            report = sync.synchronize(transport, history, 'apply')
            self.assertTrue(report['verified'])
            self.assertEqual(0, report['immediate_rerun_cells'])
            serialized = json.dumps(log) + output.read_text()
            for record in chosen.values():
                self.assertNotIn(record['Phone 1'], serialized)
                self.assertNotIn(record['doc_num'], serialized)

    def test_audit_schema_failure_still_saves_all_tab_metadata(self):
        import tempfile
        import json
        transport = MemoryTransport()
        transport.state['rows'][sync.TARGET][0].append('Unrecognized Phone Data')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'audit.json'
            code = sync.main(['--summary', str(output)], environ={'GOOGLE_CREDENTIALS': '{}'},
                             transport_factory=lambda info, mode: transport, log=lambda message: None)
            report = json.loads(output.read_text())
            self.assertEqual(1, code)
            self.assertEqual(2, len(report['tabs']))
            self.assertEqual('Master', report['title'])
            self.assertEqual(0, transport.writes)

    def test_failures_exit_nonzero_with_sanitized_artifact(self):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            log = []
            def fail(info, mode):
                raise RuntimeError('SECRET_PRIVATE_LEAD')
            code = sync.main(['--repo', str(Path(__file__).resolve().parents[1]), '--summary', str(output)],
                environ={'GOOGLE_CREDENTIALS': '{}'}, transport_factory=fail, log=log.append)
            self.assertEqual(1, code)
            self.assertEqual('failed', json.loads(output.read_text())['status'])
            self.assertNotIn('SECRET_PRIVATE_LEAD', ''.join(log) + output.read_text())


if __name__ == '__main__':
    unittest.main()
