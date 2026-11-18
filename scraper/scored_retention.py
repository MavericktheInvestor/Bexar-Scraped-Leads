"""Fail-closed retention for genuine scored property prospects.
No network, credentials or contact mutations. Source rows come from checked-out
pinned repository CSVs. Retention is not authorization to import or contact.
"""
import csv
import math
from pathlib import Path

LOCATION='NW8GqAR007aPiHZtIsaD'
SCORE_FIELD='S7pxvQ1nLH3g9R6lE1vF'
PROPERTY_FIELD='XWXSP8tFTJZBJOOyo0B1'
ALIASES={'first':('First Name',),'last':('Last Name',),'prop_address':('Property Address',),
 'prop_city':('Property City',),'prop_state':('Property State',),'prop_zip':('Property Zip',),
 'score':('Seller Score','Inmate Score')}

def text(value):return ' '.join(str(value or '').casefold().split())

def numeric(value):
    if isinstance(value,bool):return None
    try:n=float(value)
    except (TypeError,ValueError):return None
    return n if math.isfinite(n) and 0<=n<=100 else None

def source_claims(rows):
    claims=[]
    for raw in rows:
        row={key:next((str(raw[k]).strip() for k in (key,*aliases) if raw.get(k) is not None and str(raw[k]).strip()),'') for key,aliases in ALIASES.items()}
        score=numeric(row['score'])
        if score is None or not row['first'] or not row['last'] or not row['prop_address']:continue
        prop=row['prop_address']
        if prop.count(',')<2:
            if not row['prop_city'] or not row['prop_state']:continue
            prop=', '.join(row[k] for k in ('prop_address','prop_city','prop_state','prop_zip') if row[k])
        claim={'owner':[text(row['first']),text(row['last'])],'property':text(prop),'score':score}
        if claim not in claims:claims.append(claim)
    return claims

def load_claims(directory):
    rows=[]
    for name in ('skip_traced.csv','skip_trace_queue.csv','dm_fresh_leads.csv'):
        path=Path(directory)/name
        if path.is_symlink() or not path.is_file():raise ValueError('Complete pinned source CSV set required for cleanup retention')
        with path.open(newline='',encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, strict=True)
            headers = reader.fieldnames or []
            required = ('first', 'last', 'prop_address') if name == 'dm_fresh_leads.csv' else ('first', 'last', 'prop_address', 'score')
            if len(set(headers)) != len(headers) or any(not any(k in headers for k in (key, *ALIASES[key])) for key in required):
                raise ValueError('Malformed source CSV headers; cleanup refuses deletion')
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('Malformed source CSV row; cleanup refuses deletion')
                rows.append(row)
    claims=source_claims(rows)
    if not claims:raise ValueError('No verified source claims; cleanup refuses deletion')
    return claims

def fields(contact):
    rows=contact.get('customFields',[])
    if not isinstance(rows,list):raise ValueError('Malformed custom fields; cleanup refuses deletion')
    result={}
    for item in rows:
        if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] in result:raise ValueError('Malformed/duplicate custom fields')
        result[item['id']]=item.get('value',item.get('fieldValue',item.get('field_value')))
    return result

def partition(contacts,claims,location):
    if location!=LOCATION or not isinstance(claims,list) or not claims:raise ValueError('Workspace/source retention evidence required')
    if any(set(c)!={'owner','property','score'} or not isinstance(c['owner'],list) or len(c['owner'])!=2 or not all(c['owner']) or not c['property'] or numeric(c['score']) is None for c in claims):raise ValueError('Malformed retention claims')
    protected,held,delete=[],[],[];seen=set()
    for c in contacts:
        if not c.get('id') or c['id'] in seen or c.get('locationId')!=location:raise ValueError('Invalid/duplicate/foreign contact')
        seen.add(c['id'])
        if str(c.get('phone') or '').strip():continue
        values=fields(c);score=numeric(values.get(SCORE_FIELD));prop=text(values.get(PROPERTY_FIELD))
        expected={'owner':[text(c.get('firstName')),text(c.get('lastName'))],'property':prop,'score':score}
        if score is not None and prop and expected in claims:
            protected.append(c)
        elif SCORE_FIELD in values or PROPERTY_FIELD in values:
            # Previously scored/source-drifted/ambiguous prospects need review,
            # not destructive cleanup. They are NOT verified source matches.
            held.append(c)
        else:delete.append(c)
    return {'protected_scored':protected,'review_held':held,'delete_candidates':delete}
