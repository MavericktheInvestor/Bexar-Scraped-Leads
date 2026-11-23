"""Newest-filed-first Substitute Trustee sheet sync; no CRM or tracing imports."""


import copy
import math
import re

IDENTITY = re.compile(r"[0-9]{11}(?: \| [0-9]{11})*")


class SyncError(ValueError):
    """Only constant, PII-free validation messages may be exposed to operators."""


def document_identity(value):
    # Numeric Sheet cells are lossless integers; never repair source text tokens.
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise SyncError('Malformed document identity; correct offline')
    parts = value.split(' | ')
    if len(set(parts)) != len(parts):
        raise SyncError('Ambiguous repeated component in document identity')
    return value


def select_source(payload):
    if (not isinstance(payload, dict) or payload.get('scrape_complete') is not True
            or not isinstance(payload.get('records'), list) or not payload['records']):
        raise SyncError('Source must be a nonempty complete snapshot/archive')
    if 'total' in payload and (type(payload['total']) is not int
                              or payload['total'] != len(payload['records'])):
        raise SyncError('Source total does not match records')
    selected = {}
    for record in payload['records']:
        if not isinstance(record, dict):
            raise SyncError('Malformed source record')
        types = record.get('doc_types', [])
        if not isinstance(types, list) or any(not isinstance(t, str) for t in types):
            raise SyncError('Malformed merged document types')
        if record.get('doc_type') != 'SUBTRST':
            if 'SUBTRST' in types:
                raise SyncError('Merged SUBTRST lacks a proven filing/type mapping; recover original source')
            continue
        if not isinstance(record.get('doc_num'), str):
            raise SyncError('Source document identities must be literal strings')
        identity = document_identity(record.get('doc_num'))
        if not isinstance(record.get('owner'), str) or not record['owner'].strip():
            raise SyncError('Source identity has no owner')
        if identity in selected and selected[identity] != record:
            raise SyncError('Conflicting source records share a document identity')
        selected[identity] = copy.deepcopy(record)
    if not selected:
        raise SyncError('No proven SUBTRST records in complete source')
    return list(selected.values())


FIELDS = {
    'mailing address': 'mail_address', 'mailing city': 'mail_city',
    'mailing state': 'mail_state', 'mailing zip': 'mail_zip',
    'property address': 'prop_address', 'property city': 'prop_city',
    'property state': 'prop_state', 'property zip': 'prop_zip',
    'document type': 'doc_type', 'date filed': 'filed', 'document number': 'doc_num',
    'amount/debt owed': 'amount', 'seller score': 'score', 'public records url': 'clerk_url',
}
REQUIRED = set(FIELDS) | {'last name', 'first name', 'lead type', 'motivated seller flags',
                          'source', 'status', 'partner notes', 'date contacted'}


def header_map(rows):
    if not rows or not rows[0] or any(not isinstance(h, str) for h in rows[0]):
        raise SyncError('Missing or malformed existing sheet header')
    names = [h.strip().lower() for h in rows[0]]
    nonblank = [h for h in names if h]
    if len(set(nonblank)) != len(nonblank) or not REQUIRED.issubset(names):
        raise SyncError('Missing or ambiguous existing sheet header mapping')
    return {h: i for i, h in enumerate(names) if h}


def lead_indexes(rows, mapping):
    indexes = []
    for index, row in enumerate(rows[1:], 1):
        # Only the main table's recognized filing/lead/source markers prove
        # that this column contains a document identity. Other regions may put
        # text, headers or even 11-digit phone/account numbers in this column.
        def cell(name):
            index = mapping[name]
            return str(row[index]).strip().lower() if len(row) > index else ''
        declared_lead = (cell('document type').upper() in
                         {'AFFH', 'PRO', 'SUBTRST', 'TAXDLQ', 'DIV', 'NOFC', 'TAXDEED'}
                         or cell('lead type') in {'substitute trustee', 'affidavit of heirship',
                                                 'probate', 'tax delinquency', 'divorce',
                                                 'notice of foreclosure', 'tax deed'}
                         or cell('source') == 'bexar county clerk')
        if declared_lead:
            indexes.append(index)
    return indexes


def existing_identities(rows, mapping):
    found = set()
    column = mapping['document number']
    for index in lead_indexes(rows, mapping):
        row = rows[index]
        if len(row) <= column or row[column] == '':
            raise SyncError('Recognized existing lead is missing its document identity')
        identity = document_identity(row[column])
        if identity in found:
            raise SyncError('Duplicate existing document identity; resolve offline')
        found.add(identity)
    return found


def plan_rows(records, before):
    mapping = header_map(before)
    found = existing_identities(before, mapping)
    planned = []
    for record in records:
        if document_identity(record['doc_num']) in found:
            continue
        row = [''] * len(before[0])
        for header, field in FIELDS.items():
            value = record.get(field)
            row[mapping[header]] = '' if value is None else value
        # Only comma-separated clerk names prove a first/last split. Otherwise
        # retain the entire owner (including businesses) without inventing names.
        names = record['owner'].split(',', 1)
        row[mapping['last name']] = names[0].strip()
        row[mapping['first name']] = names[1].strip() if len(names) == 2 else ''
        row[mapping['lead type']] = 'Substitute Trustee'
        row[mapping['source']] = 'Bexar County Clerk'
        row[mapping['motivated seller flags']] = ' | '.join(record.get('flags') or [])
        if any(type(value) not in (str, int, float, bool) for value in row):
            raise SyncError('Source contains an unsupported cell value')
        if any(type(value) is float and not math.isfinite(value) for value in row):
            raise SyncError('Source contains a nonfinite numeric cell')
        planned.append(row)
        found.add(record['doc_num'])
    return planned


SHEET_ID = '1njU3FG3_gtzdhJeitL2sE-bXKPMgR-kYgVDwqmCMbMc'
SHEET_NAME = 'Sheet1'
SHEET_RANGE = "'Sheet1'"


def read_rows(service, sheet_name=SHEET_NAME):
    quoted = "'" + sheet_name.replace("'", "''") + "'"
    return service.values().get(
        spreadsheetId=SHEET_ID, range=quoted, valueRenderOption='FORMULA',
        dateTimeRenderOption='SERIAL_NUMBER').execute().get('values', [])


def canonical(rows):
    result = copy.deepcopy(rows)
    for row in result:
        while row and row[-1] == '':
            row.pop()
    while result and not result[-1]:
        result.pop()
    # Sheets numbers are doubles (1 and 1.0 are equivalent), but booleans
    # must never compare equal to 0/1 as Python's normal list equality allows.
    return [[('number' if type(value) in (int, float) else type(value).__name__, value)
             for value in row] for row in result]


def filed_date(value):
    """Parse sort keys only: never normalize or replace source cells."""
    from datetime import date, timedelta
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return date(1899, 12, 30) + timedelta(days=math.floor(value))
        if isinstance(value, str):
            if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                return date.fromisoformat(value)
            if re.fullmatch(r'[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}', value):
                month, day, year = map(int, value.split('/'))
                return date(year, month, day)
    except (ValueError, OverflowError):
        pass
    raise SyncError('Missing or unsupported lead Date Filed; correct offline before moving rows')


def plan_placement(before, planned, sheet_id):
    """Sequential, upward whole-row moves preserve all other rows in order."""
    rows = copy.deepcopy(before)
    requests = []
    if planned:
        requests.extend([
            {'insertDimension': {'range': {'sheetId': sheet_id, 'dimension': 'ROWS',
                                          'startIndex': 1, 'endIndex': 1 + len(planned)},
                                 'inheritFromBefore': False}},
            {'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': 1, 'columnIndex': 0},
                             'rows': [{'values': [{'userEnteredValue': {
                                 ('boolValue' if type(v) is bool else
                                  'numberValue' if type(v) in (int, float) else 'stringValue'): v}}
                                                  for v in row]} for row in planned],
                             'fields': 'userEnteredValue'}}])
        rows[1:1] = copy.deepcopy(planned)
    mapping = header_map(rows)
    indexes = lead_indexes(rows, mapping)
    dates = {i: filed_date(rows[i][mapping['date filed']]
                          if len(rows[i]) > mapping['date filed'] else '') for i in indexes}
    ordered = sorted(indexes, key=lambda index: dates[index], reverse=True)  # stable equal-date order
    tokens = list(range(len(rows)))
    for destination, token in enumerate(ordered, 1):
        source = tokens.index(token)
        if source == destination:
            continue
        assert source > destination
        requests.append({'moveDimension': {
            'source': {'sheetId': sheet_id, 'dimension': 'ROWS',
                       'startIndex': source, 'endIndex': source + 1},
            'destinationIndex': destination}})
        tokens.insert(destination, tokens.pop(source))
        rows.insert(destination, rows.pop(source))
    return requests, rows


def synchronize(service, payload, mode='audit', log=print):
    if mode not in ('audit', 'apply'):
        raise SyncError('Mode must be audit or apply')
    records = select_source(payload)
    meta = service.get(spreadsheetId=SHEET_ID,
                       fields='properties(title),sheets(properties(sheetId,title,gridProperties))').execute()
    tabs = [sheet['properties'] for sheet in meta['sheets']]
    log('Spreadsheet title: ' + meta['properties']['title'])
    log('Tabs: ' + ', '.join(tab['title'] for tab in tabs))
    target = [tab for tab in tabs if tab['title'] == SHEET_NAME]
    if len(target) != 1:
        raise SyncError('Existing Sheet1 tab is required; never create or rename tabs')
    before = read_rows(service)
    planned = plan_rows(records, before)
    requests, expected = plan_placement(before, planned, target[0]['sheetId'])
    snapshots = {SHEET_NAME: before}
    if requests:
        # A move can rewrite references in other tabs too. Refuse formulas rather
        # than claiming unchanged text proves reference semantics were preserved.
        snapshots.update({tab['title']: read_rows(service, tab['title'])
                          for tab in tabs if tab['title'] != SHEET_NAME})
        if any(isinstance(value, str) and value.startswith('=')
               for rows in snapshots.values() for row in rows for value in row):
            raise SyncError('Existing formula cells prevent safe row-reference verification; resolve offline')
    report = dict(source_identities=len(records), existing_physical_rows=len(before)-1,
                  existing_nonempty_rows=sum(any(value != '' for value in row) for row in before[1:]),
                  existing_identities=len(existing_identities(before, header_map(before))),
                  composite_source_identities=sum(' | ' in r['doc_num'] for r in records),
                  missing=len(planned), inserted=0,
                  planned_moves=sum('moveDimension' in r for r in requests), moved=0)
    log('Counts: ' + str(report))
    log('Plan: insert missing leads below header, then whole-row moves to contiguous '
        'newest-Date-Filed-first order; all nonlead rows retain their relative order.')
    log('Source limitation: exact source identities, not independently verified distinct filings; '
        'historical owner-merged composites remain intact and cannot recover lost filings/types.')
    if mode == 'audit':
        return report
    if requests:
        for title, rows in snapshots.items():
            if canonical(read_rows(service, title)) != canonical(rows):
                raise SyncError('Sheet changed before write; rerun audit without concurrent editors')
        service.batchUpdate(spreadsheetId=SHEET_ID, body={'requests': requests}).execute()
    after = read_rows(service)
    if canonical(after) != canonical(expected):
        raise SyncError('Exact readback failed; lead payload/order or nonlead sequence changed; manual audit required')
    expected_meta = copy.deepcopy(meta)
    for sheet in expected_meta['sheets']:
        if sheet['properties']['sheetId'] == target[0]['sheetId']:
            sheet['properties']['gridProperties']['rowCount'] += len(planned)
    after_meta = service.get(
        spreadsheetId=SHEET_ID,
        fields='properties(title),sheets(properties(sheetId,title,gridProperties))').execute()
    if after_meta != expected_meta:
        raise SyncError('Grid/tab readback failed; manual audit required')
    for title, rows in snapshots.items():
        if title != SHEET_NAME and canonical(read_rows(service, title)) != canonical(rows):
            raise SyncError('Other-tab readback failed; manual audit required')
    if plan_rows(records, after):
        raise SyncError('Readback is missing source identities')
    report['inserted'] = len(planned)
    report['moved'] = report['planned_moves']
    log('Verified: exact full-row payloads and newest-first order; nonlead sequence preserved; '
        + 'inserted=' + str(len(planned)) + '; moved=' + str(report['moved']))
    return report


def capture_subtrst(records):
    """Freeze enriched individual filings before any owner merge or exclusions."""
    return copy.deepcopy([record for record in records if record.get('doc_type') == 'SUBTRST'])


def persist_archive(path, captured, fetched_at):
    """Atomically accumulate proven source identities after complete collection only."""
    import json
    import os
    from pathlib import Path
    import tempfile
    path = Path(path)
    current = select_source({'scrape_complete': True, 'records': captured})
    previous = select_source(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else []
    by_identity = {record['doc_num']: record for record in previous}
    for record in current:
        old = by_identity.get(record['doc_num'])
        if old and old['owner'] != record['owner']:
            raise SyncError('Archive identity has conflicting owners; recover original source')
        by_identity.setdefault(record['doc_num'], record)
    payload = dict(scrape_complete=True, fetched_at=fetched_at,
                   source='Cumulative SUBTRST archive; new captures are pre-owner-merge filings; '
                          'historical seeds may retain owner-merged composite identities',
                   total=len(by_identity), records=list(by_identity.values()))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def build_service(info, mode):
    from google.oauth2 import service_account
    from googleapiclient import discovery
    scope = 'https://www.googleapis.com/auth/spreadsheets'
    if mode == 'audit':
        scope += '.readonly'
    credentials = service_account.Credentials.from_service_account_info(info, scopes=[scope])
    return discovery.build('sheets', 'v4', credentials=credentials,
                           cache_discovery=False).spreadsheets()


def main(argv=None, environ=None, service_factory=build_service, log=print):
    import argparse
    import json
    import os
    from pathlib import Path
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('audit', 'apply'),
                        default=env.get('SUBTRST_SYNC_MODE', 'audit'))
    parser.add_argument('--source', default=env.get('SUBTRST_SOURCE', 'data/subtrst_records.json'))
    args = parser.parse_args(argv)
    client_email = None
    try:
        payload = json.loads(Path(args.source).read_text(encoding='utf-8'))
        select_source(payload)  # Validate before authenticating or reaching any network.
        info = json.loads(env.get('GOOGLE_CREDENTIALS', ''))
        email = info.get('client_email', '')
        if re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9-]+\.iam\.gserviceaccount\.com', email):
            client_email = email
        service = service_factory(info, args.mode)
        synchronize(service, payload, args.mode, log)
        return 0
    except Exception as exc:
        # API errors may contain entire request/response rows or secret material.
        # Never print exception messages, tracebacks, credentials, or source rows.
        log('SUBTRST sync FAILED (' + type(exc).__name__ + '); no success claimed. '
            'Check complete source/schema, sheet permissions and exact readback; rerun audit.')
        if isinstance(exc, SyncError):
            log(str(exc))
        if client_email:
            log('Service-account client_email: ' + client_email + '; share the existing sheet with this account.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
