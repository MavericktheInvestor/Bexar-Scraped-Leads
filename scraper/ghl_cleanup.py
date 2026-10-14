"""
GHL Cleanup — delete contacts with no phone number.
Reads all contacts in the location, deletes any without a phone.
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error

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
    contacts = []
    page = 1
    after = None
    after_id = None
    while True:
        params = {"locationId": LOCATION_ID, "limit": 100}
        if after:
            params["startAfter"] = after
        if after_id:
            params["startAfterId"] = after_id

        data = ghl_get("/contacts/", params)

        # Debug: show top-level keys
        if page == 1:
            print(f"  Response keys: {list(data.keys())}", flush=True)
            meta = data.get("meta", {})
            print(f"  Meta: {meta}", flush=True)

        batch = data.get("contacts", [])
        if not batch:
            print("  No more contacts in batch, stopping.", flush=True)
            break
        contacts.extend(batch)

        meta = data.get("meta", {})
        after    = meta.get("startAfter")
        after_id = meta.get("startAfterId")
        total    = meta.get("total", "?")

        print(f"  Page {page}: fetched {len(batch)}, total so far: {len(contacts)} / {total}", flush=True)
        page += 1

        if not after and not after_id:
            break
        time.sleep(0.3)
    return contacts

def main():
    print(f"{'🔍 DRY RUN — no deletions' if DRY_RUN else '🗑  LIVE — will delete'}", flush=True)
    print(f"Location: {LOCATION_ID}", flush=True)
    print(f"API key prefix: {GHL_API_KEY[:10]}...", flush=True)
    print()

    print("Fetching all contacts…", flush=True)
    try:
        all_contacts = get_all_contacts()
    except Exception as e:
        print(f"\n❌ Failed to fetch contacts: {type(e).__name__}: {e}", flush=True)
        sys.exit(1)

    print(f"\nTotal contacts fetched: {len(all_contacts)}", flush=True)

    no_phone = [c for c in all_contacts if not (c.get("phone") or "").strip()]
    print(f"Contacts without phone: {len(no_phone)}", flush=True)
    print(f"Contacts with phone:    {len(all_contacts) - len(no_phone)}", flush=True)
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
