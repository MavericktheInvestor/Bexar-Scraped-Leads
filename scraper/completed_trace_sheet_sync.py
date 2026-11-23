"""Preservation-first completed trace reconciliation. Never performs skip tracing."""
import copy
import hashlib
import re
from datetime import date

SHEET_ID = '1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk'
TARGET = 'Sheet1'
QUEUE = 'Skip Trace Queue'
OUTPUT_HEADERS = ['Phone 1', 'Phone 2', 'Skip Trace Status', 'Skip Trace Date']


class SyncError(ValueError):
    """Constant messages only: safe to expose without lead data."""


def normalized(value):
    return ' '.join(str(value).strip().casefold().split())


def phone(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'[+() 0-9.-]+', value):
        return None
    digits = re.sub(r'[^0-9]', '', value)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def identity_valid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{11}(?: \| [0-9]{11})*', value):
        return False
    parts = value.split(' | ')
    return len(parts) == len(set(parts))


def owner_property(row):
    return tuple(normalized(row.get(key, '')) for key in ('first', 'last', 'prop_address'))


def select_completed(snapshots, *, live_queue=False):
    # Snapshots must be newest-first. Absence never revokes a recovered result.
    # Only the first snapshot can be a rebuilt live queue; history blanks still block.
    from collections import Counter, defaultdict
    groups, duplicate = defaultdict(list), set()
    decisions = {}
    for snapshot_index, rows in enumerate(snapshots):
        counts = Counter(row.get('doc_num') for row in rows)
        duplicate.update(key for key, count in counts.items() if count > 1)
        for row in rows:
            groups[row.get('doc_num')].append(row)
            if not (live_queue and snapshot_index == 0
                    and not normalized(row.get('Skip Trace Status', ''))):
                decisions.setdefault(row.get('doc_num'), row)
    selected, held = {}, Counter()
    for identity, rows in groups.items():
        reason = None
        latest = decisions.get(identity, {})
        if not identity_valid(identity):
            reason = 'invalid_identity'
        elif identity in duplicate:
            reason = 'duplicate_source_identity'
        elif len({owner_property(row) for row in rows}) != 1:
            reason = 'source_owner_property_collision'
        elif normalized(latest.get('Skip Trace Status', '')) != 'done':
            reason = 'latest_not_completed'
        else:
            candidates = []
            for row in rows:
                if normalized(row.get('Skip Trace Status', '')) != 'done':
                    continue
                try:
                    trace_date = date.fromisoformat(row.get('Skip Trace Date', ''))
                    if trace_date.isoformat() != row.get('Skip Trace Date'):
                        raise ValueError
                except (ValueError, TypeError):
                    reason = 'invalid_trace_date'
                    break
                if not phone(row.get('Phone 1')) or (row.get('Phone 2') and not phone(row['Phone 2'])):
                    reason = 'invalid_source_phone'
                    break
                candidates.append((trace_date, row))
            if not reason and candidates:
                newest = max(day for day, row in candidates)
                supported = [row for day, row in candidates if day == newest]
                if len({(phone(row['Phone 1']), phone(row.get('Phone 2'))) for row in supported}) > 1:
                    reason = 'same_date_phone_conflict'
                else:
                    selected[identity] = copy.deepcopy(supported[0])
        if reason:
            held[reason] += 1
    return selected, dict(held)


ALIASES = {
    'doc_num': ('doc_num', 'document number'), 'first': ('first', 'first name'),
    'last': ('last', 'last name'), 'prop_address': ('prop_address', 'property address'),
    'Phone 1': ('phone 1', 'phone', 'primary phone', 'phone number'),
    'Phone 2': ('phone 2', 'alternate phone', 'alt phone'),
    'Skip Trace Status': ('skip trace status',), 'Skip Trace Date': ('skip trace date',),
}


def header_mapping(header, required):
    if not header or any(not isinstance(label, str) for label in header):
        raise SyncError('Missing or malformed headers')
    names = [normalized(label) for label in header]
    nonblank = [label for label in names if label]
    if len(nonblank) != len(set(nonblank)):
        raise SyncError('Duplicate header labels')
    mapping = {}
    for key, aliases in ALIASES.items():
        matches = [i for i, label in enumerate(names) if label in aliases]
        if len(matches) > 1:
            raise SyncError('Ambiguous header aliases')
        if matches:
            mapping[key] = matches[0]
    if not set(required).issubset(mapping):
        raise SyncError('Required header mapping missing')
    return mapping


def parse_rows(rows):
    mapping = header_mapping(rows[0] if rows else [], ('doc_num', 'first', 'last', 'prop_address'))
    if any(key in mapping for key in OUTPUT_HEADERS) and not all(key in mapping for key in OUTPUT_HEADERS):
        raise SyncError('Incomplete trace-result schema')
    result = []
    for values in rows[1:]:
        if not any(value != '' for value in values):
            continue
        row = {key: values[column] if column < len(values) else '' for key, column in mapping.items()}
        # Sheets serializes numeric document cells; CSV strings remain literal.
        identity = row['doc_num']
        if type(identity) is int or (type(identity) is float and identity.is_integer()):
            row['doc_num'] = str(int(identity))
        result.append(row)
    return result


def parse_csv(text):
    import csv
    import io
    try:
        rows = [row for row in csv.reader(io.StringIO(text.lstrip('\ufeff')), strict=True) if row]
    except csv.Error:
        raise SyncError('Historical CSV parse failure') from None
    if not rows or any(len(row) != len(rows[0]) for row in rows[1:]):
        raise SyncError('Historical CSV row width mismatch')
    return parse_rows(rows)


def load_history(repo):
    import subprocess
    from pathlib import Path
    def git(*args):
        try:
            return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode('utf-8-sig')
        except (subprocess.CalledProcessError, UnicodeError):
            raise SyncError('Git history read failed') from None
    if git('rev-parse', '--is-shallow-repository').strip() != 'false':
        raise SyncError('Full nonshallow history is required')
    path = 'data/skip_traced.csv'
    revisions = git('log', '--all', '--date-order', '--format=%H', '--', path).splitlines()
    if not revisions:
        raise SyncError('No trace history found')
    snapshots = [parse_csv(git('show', revision + ':' + path)) for revision in revisions]
    current = parse_csv((Path(repo) / path).read_text(encoding='utf-8-sig'))
    snapshots.insert(0, current)
    return snapshots, dict(revisions_enumerated=len(revisions), revisions_parsed=len(revisions),
                           historical_rows=sum(len(rows) for rows in snapshots[1:]), current_rows=len(current))


def apply_matrix(before, changes):
    after = copy.deepcopy(before)
    for row, column, value in changes:
        while len(after) <= row:
            after.append([])
        after[row].extend([''] * max(0, column + 1 - len(after[row])))
        after[row][column] = value
    return after


def plan_patch(selected, before, minimum_new_column=0):
    from collections import Counter, defaultdict
    mapping = header_mapping(before[0] if before else [], ('doc_num', 'first', 'last', 'prop_address'))
    names = [normalized(label) for label in before[0]]
    recognized_phone_columns = {mapping[key] for key in OUTPUT_HEADERS[:2] if key in mapping}
    if any('phone' in name and i not in recognized_phone_columns for i, name in enumerate(names)):
        raise SyncError('Unmapped phone-related master header; review schema before apply')
    manual_status = names.index('status') if 'status' in names else None
    width = max(minimum_new_column, max(map(len, before)))
    missing = [label for label in OUTPUT_HEADERS if label not in mapping]
    headers = []
    for label in missing:
        mapping[label] = width
        headers.append((0, width, label))
        width += 1
    def cell(row, key):
        column = mapping[key]
        return row[column] if column < len(row) else ''
    indexes = defaultdict(list)
    for index, row in enumerate(before[1:], 1):
        identity = cell(row, 'doc_num')
        if type(identity) is int or (type(identity) is float and identity.is_integer()):
            identity = str(int(identity))
        if isinstance(identity, str):
            indexes[identity].append(index)
    changes, holds, matched, unchanged = [], Counter(), 0, 0
    diagnostic_counts, classifications, candidates = Counter(), Counter(), []
    for identity, record in selected.items():
        positions = indexes[identity]
        if len(positions) != 1:
            holds['missing_master_row' if not positions else 'duplicate_master_identity'] += 1
            continue
        index = positions[0]
        row = before[index]
        existing_identity = tuple(normalized(cell(row, key)) for key in ('first', 'last', 'prop_address'))
        expected_identity = owner_property(record)
        flags = dict(
            direct_name_match=existing_identity[:2] == expected_identity[:2],
            swapped_name_match=existing_identity[:2] == expected_identity[:2][::-1],
            property_exact=existing_identity[2] == expected_identity[2],
            source_property_present=bool(expected_identity[2]),
            firstlast_both_populated=all(existing_identity[:2] + expected_identity[:2]))
        classification = ('incomplete_identity' if not all(existing_identity + expected_identity) else
                          'property_mismatch' if not flags['property_exact'] else
                          'exact_identity' if existing_identity == expected_identity else
                          'name_swap_property_exact' if flags['swapped_name_match'] and flags['property_exact']
                          else 'name_mismatch')
        # Matrix indexes are zero-based (header=0); these diagnostics never authorize writes.
        candidates.append(dict(
            master_row_index=index, identity_sha256=hashlib.sha256(identity.encode('utf-8')).hexdigest(),
            classification=classification, **flags))
        diagnostic_counts.update({key: int(value) for key, value in flags.items()})
        classifications[classification] += 1
        if (manual_status is not None and manual_status < len(row)
                and normalized(row[manual_status]) in {'hold', 'skipped', 'no phone', 'do not contact', 'dnc'}):
            holds['manual_lead_hold'] += 1
            continue
        if (existing_identity != expected_identity or not expected_identity[2]
                or not any(expected_identity[:2])):
            holds['master_owner_property_collision'] += 1
            continue
        wanted_phones = {phone(record.get(label)) for label in OUTPUT_HEADERS[:2]} - {None}
        conflict = False
        for label in OUTPUT_HEADERS:
            old, new = cell(row, label), record.get(label, '')
            if old == '':
                continue
            if label in OUTPUT_HEADERS[:2]:
                agrees = phone(old) is not None and phone(old) in wanted_phones
            else:
                agrees = old == new
            if not agrees:
                conflict = True
        if conflict:
            holds['existing_contact_conflict'] += 1
            continue
        additions = []
        existing_phones = {phone(cell(row, label)) for label in OUTPUT_HEADERS[:2]} - {None}
        remaining = []
        for label in OUTPUT_HEADERS[:2]:
            value = record.get(label, '')
            if value and phone(value) not in existing_phones:
                remaining.append(str(value))
                existing_phones.add(phone(value))
        slots = [mapping[label] for label in OUTPUT_HEADERS[:2] if cell(row, label) == '']
        if len(remaining) > len(slots):
            holds['insufficient_blank_phone_slots'] += 1
            continue
        additions.extend((index, column, value) for column, value in zip(slots, remaining))
        matched += 1
        for label in OUTPUT_HEADERS[2:]:
            value = record.get(label, '')
            if value and cell(row, label) == '':
                additions.append((index, mapping[label], value))
        if not additions:
            unchanged += 1
        changes.extend(additions)
    if changes:
        changes = headers + changes
    return dict(changes=changes, new_headers=missing if changes else [], holds=dict(holds),
                matched=matched, unchanged=unchanged,
                master_identity_diagnostics=dict(counts=dict(diagnostic_counts),
                                                 classifications=dict(classifications), candidates=candidates))


def canonical(rows):
    rows = copy.deepcopy(rows)
    for row in rows:
        while row and row[-1] == '':
            row.pop()
    while rows and not rows[-1]:
        rows.pop()
    return [[('number' if type(value) in (int, float) else type(value).__name__, value)
             for value in row] for row in rows]


def snapshot_equal(left, right):
    return (left['meta'] == right['meta'] and left['rows'].keys() == right['rows'].keys()
            and all(canonical(rows) == canonical(right['rows'][title])
                    for title, rows in left['rows'].items()))


def profile_snapshot(snapshot):
    return dict(title=snapshot['meta']['properties']['title'], tabs=[
        dict(title=sheet['properties']['title'], grid=sheet['properties']['gridProperties'],
             headers=rows[0] if rows else [], physical_rows=max(0, len(rows) - 1),
             nonempty_rows=sum(any(value != '' for value in row) for row in rows[1:]))
        for sheet in snapshot['meta']['sheets']
        for rows in [snapshot['rows'][sheet['properties']['title']]]])


def synchronize(transport, history, mode='audit', before=None):
    if mode not in ('audit', 'apply'):
        raise SyncError('Mode must be audit or apply')
    before = transport.read() if before is None else before
    tabs = [sheet['properties'] for sheet in before['meta']['sheets']]
    target = [tab for tab in tabs if tab['title'] == TARGET]
    if len(target) != 1 or QUEUE not in before['rows']:
        raise SyncError('Existing master and source queue tabs are required')
    live = parse_rows(before['rows'][QUEUE])
    selected, source_holds = select_completed([live] + history, live_queue=True)
    old_columns = target[0]['gridProperties']['columnCount']
    plan = plan_patch(selected, before['rows'][TARGET], old_columns)
    changes = plan['changes']
    new_columns = max([old_columns] + [column + 1 for row, column, value in changes])
    report = dict(mode=mode, spreadsheet_id=SHEET_ID, title=before['meta']['properties']['title'],
                  tabs=[dict(title=tab['title'], grid=tab['gridProperties'],
                             headers=before['rows'][tab['title']][0] if before['rows'][tab['title']] else [],
                             physical_rows=max(0, len(before['rows'][tab['title']]) - 1),
                             nonempty_rows=sum(any(v != '' for v in row) for row in before['rows'][tab['title']][1:]))
                        for tab in tabs], live_source_rows=len(live), selected_completed=len(selected),
                  source_holds=source_holds, master_holds=plan['holds'],
                  master_identity_diagnostics=plan['master_identity_diagnostics'],
                  planned_headers=plan['new_headers'], planned_cells=len(changes),
                  planned_leads=len({row for row, column, value in changes if row > 0}),
                  already_synced=plan['unchanged'], written_cells=0, verified=False,
                  limitations=['Missing master leads are held, never reconstructed.',
                               'Exact composite identities remain intact.',
                               'Sheets has no compare-and-swap; avoid concurrent editors.',
                               'Only userEnteredValue fields are changed; native notes/formatting are not independently read back.'])
    if mode == 'audit':
        return report
    expected = copy.deepcopy(before)
    expected['rows'][TARGET] = apply_matrix(before['rows'][TARGET], changes)
    for sheet in expected['meta']['sheets']:
        if sheet['properties']['title'] == TARGET:
            sheet['properties']['gridProperties']['columnCount'] = new_columns
    if changes:
        if not snapshot_equal(before, transport.read()):
            raise SyncError('Snapshot changed before write; rerun audit')
        transport.write(changes, new_columns)
    after = transport.read()
    if not snapshot_equal(expected, after):
        raise SyncError('Full matrix or metadata readback failed; manual audit required')
    rerun_selected, _ = select_completed([parse_rows(after['rows'][QUEUE])] + history, live_queue=True)
    if plan_patch(rerun_selected, after['rows'][TARGET], new_columns)['changes']:
        raise SyncError('Immediate rerun is not idempotent; manual audit required')
    report.update(written_cells=len(changes), verified=True, immediate_rerun_cells=0)
    return report


class GoogleTransport:
    """Sheets-only adapter: atomic typed-cell writes, no clear/append-table/reorder."""
    def __init__(self, service):
        self.service = service
        self.last_meta = None

    def read(self):
        meta = self.service.get(spreadsheetId=SHEET_ID,
            fields='properties(title),sheets(properties(sheetId,title,gridProperties))').execute()
        rows = {}
        for sheet in meta['sheets']:
            title = sheet['properties']['title']
            quoted = "'" + title.replace("'", "''") + "'"
            rows[title] = self.service.values().get(spreadsheetId=SHEET_ID,
                range=quoted, valueRenderOption='FORMULA', dateTimeRenderOption='SERIAL_NUMBER').execute().get('values', [])
        self.last_meta = meta
        return dict(meta=meta, rows=rows)

    def write(self, changes, columns):
        if self.last_meta is None:
            raise SyncError('Read snapshot required before writing')
        target = next(sheet['properties'] for sheet in self.last_meta['sheets']
                      if sheet['properties']['title'] == TARGET)
        old_columns = target['gridProperties']['columnCount']
        requests = []
        if columns > old_columns:
            requests.append({'appendDimension': {'sheetId': target['sheetId'],
                             'dimension': 'COLUMNS', 'length': columns - old_columns}})
        for row, column, value in changes:
            if not isinstance(value, str) or row >= target['gridProperties']['rowCount']:
                raise SyncError('Unsupported cell value or row outside existing grid')
            requests.append({'updateCells': {
                'start': {'sheetId': target['sheetId'], 'rowIndex': row, 'columnIndex': column},
                'rows': [{'values': [{'userEnteredValue': {'stringValue': value}}]}],
                'fields': 'userEnteredValue'}})
        if requests:
            self.service.batchUpdate(spreadsheetId=SHEET_ID, body={'requests': requests}).execute()


def build_transport(info, mode):
    from google.oauth2 import service_account
    from googleapiclient import discovery
    scope = 'https://www.googleapis.com/auth/spreadsheets' + ('.readonly' if mode == 'audit' else '')
    credentials = service_account.Credentials.from_service_account_info(info, scopes=[scope])
    service = discovery.build('sheets', 'v4', credentials=credentials, cache_discovery=False).spreadsheets()
    return GoogleTransport(service)


def main(argv=None, environ=None, transport_factory=build_transport, log=print):
    import argparse
    import json
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('audit', 'apply'), default='audit')
    parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--summary', default='completed-trace-audit.json')
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    report = {'status': 'failed', 'mode': args.mode, 'spreadsheet_id': SHEET_ID}
    code = 1
    try:
        history, stats = load_history(args.repo)
        completed, held = select_completed(history)
        report.update(history=stats, history_completed_identities=len(completed), history_holds=held)
        info = json.loads(env.get('GOOGLE_CREDENTIALS', ''))
        if not isinstance(info, dict):
            raise SyncError('Credentials must be a service-account object')
        transport = transport_factory(info, args.mode)
        snapshot = transport.read()
        report.update(profile_snapshot(snapshot))
        result = synchronize(transport, history, args.mode, before=snapshot)
        report.update(result, status='audited' if args.mode == 'audit' else 'verified')
        code = 0
    except Exception as exc:
        # Third-party exception text can contain rows, HTTP bodies or credentials.
        report.update(status='failed', error_type=type(exc).__name__)
        if isinstance(exc, SyncError):
            report['error'] = str(exc)
        log('Completed trace sync FAILED; inspect the sanitized audit artifact. No success claimed.')
    try:
        path = Path(args.summary)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    except OSError:
        log('Audit artifact write FAILED; no success claimed.')
        return 1
    log(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

