"""
GHL Cleanup — delete contacts with no phone number.
Reads all contacts in the location, deletes any without a phone.
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error
from pathlib import Path
from scored_retention import load_claims, partition

def load_retention_claims():
    return load_claims(Path(__file__).resolve().parents[1] / 'data')

GHL_API_KEY  = os.environ["GHL_API_KEY"]
LOCATION_ID  = os.environ["GHL_LOCATION_ID"]
DRY_RUN      = os.environ.get("DRY_RUN", "true").lower() == "true"

BASE = "https://services.leadconnectorhq.com"

def headers():
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; GHL-Integration/1.0)",
    }

def ghl_get(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    print(f"  GET {url}", flush=True)
    req = urllib.request.Request(url, headers=headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            print(f"  → HTTP {r.status}", flush=True)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  → HTTP ERROR {e.code}: {body[:500]}", flush=True)
        raise
    except urllib.error.URLError as e:
        print(f"  → URL ERROR: {e.reason}", flush=True)
        raise

def ghl_delete(path):
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers=headers(), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  → HTTP ERROR {e.code} on DELETE {path}: {body[:200]}", flush=True)
        raise

def get_all_contacts():
    """Validate the entire paginated snapshot before any deletion is considered."""
    contacts, seen_ids, seen_cursors = [], set(), set()
    expected_total = None
    cursor = None
    while True:
        params = {"locationId": LOCATION_ID, "limit": 100}
        if cursor is not None:
            params.update(startAfter=cursor[0], startAfterId=cursor[1])
        data = ghl_get("/contacts/", params)
        if not isinstance(data, dict) or not isinstance(data.get('contacts'), list) or not isinstance(data.get('meta'), dict):
            raise ValueError('Malformed contact pagination response')
        batch, meta = data['contacts'], data['meta']
        total = meta.get('total')
        if type(total) is not int or total < 0:
            raise ValueError('Pagination total missing or invalid')
        if expected_total is None:
            expected_total = total
        if total != expected_total:
            raise ValueError('Contact total changed during pagination; retry a stable snapshot')
        for contact in batch:
            cid = contact.get('id') if isinstance(contact, dict) else None
            if not isinstance(cid, str) or not cid or cid in seen_ids:
                raise ValueError('Missing/duplicate contact ID during pagination')
            seen_ids.add(cid)
        contacts.extend(batch)
        if len(contacts) > expected_total:
            raise ValueError('Contact count exceeds advertised total')
        print(f"  Fetched {len(contacts)} / {expected_total}", flush=True)
        if len(contacts) == expected_total:
            return contacts
        if not batch:
            raise ValueError('Empty page before advertised contact total')
        cursor = (meta.get('startAfter'), meta.get('startAfterId'))
        if not isinstance(cursor[0], (str, int)) or isinstance(cursor[0], bool) or not isinstance(cursor[1], str) or not cursor[1] or cursor in seen_cursors:
            raise ValueError('Missing/invalid/repeated pagination cursor before total')
        seen_cursors.add(cursor)
        time.sleep(0.3)

def main():
    print(f"{'🔍 DRY RUN — no deletions' if DRY_RUN else '🗑  LIVE — will delete'}", flush=True)
    print(f"Location: {LOCATION_ID}", flush=True)
    # Never print credentials or partial credentials.
    print()

    print("Fetching all contacts…", flush=True)
    try:
        all_contacts = get_all_contacts()
    except Exception as e:
        print(f"\n❌ Failed to fetch contacts: {type(e).__name__}: {e}", flush=True)
        sys.exit(1)

    print(f"\nTotal contacts fetched: {len(all_contacts)}", flush=True)

    # Preserve source-verified scored prospects; hold ambiguous prior metadata.
    # Missing/malformed evidence raises before the first DELETE, never falls back.
    claims = load_retention_claims()
    decision = partition(all_contacts, claims, LOCATION_ID)
    no_phone = decision['delete_candidates']
    print(f"Protected scored prospects: {len(decision['protected_scored'])}", flush=True)
    print(f"Scored/property prospects held for review: {len(decision['review_held'])}", flush=True)
    print(f"Contacts without phone eligible for cleanup: {len(no_phone)}", flush=True)
    print(f"Contacts with phone:    {sum(bool((c.get('phone') or '').strip()) for c in all_contacts)}", flush=True)
    print()

    if not no_phone:
        print("✅ Nothing to delete.", flush=True)
        return

    deleted = errors = 0
    for i, c in enumerate(no_phone, 1):
        cid  = c["id"]
        name = f"{c.get('firstName','')} {c.get('lastName','')}".strip() or "(no name)"
        if DRY_RUN:
            print(f"  [{i}/{len(no_phone)}] Would delete: {name} ({cid})")
            continue
        try:
            ghl_delete(f"/contacts/{cid}")
            deleted += 1
            if i <= 10 or i % 50 == 0:
                print(f"  [{i}/{len(no_phone)}] ✅ Deleted: {name}", flush=True)
        except Exception as e:
            print(f"  [{i}/{len(no_phone)}] ✗ {name}: {e}", flush=True)
            errors += 1
        time.sleep(0.25)

    print(f"\n{'DRY RUN complete' if DRY_RUN else 'Done'}:", flush=True)
    if not DRY_RUN:
        print(f"  Deleted: {deleted}", flush=True)
        print(f"  Errors:  {errors}", flush=True)
    else:
        print(f"  Would delete: {len(no_phone)}", flush=True)

if __name__ == "__main__":
    main()
