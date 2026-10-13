"""
GHL Cleanup — delete contacts with no phone number.
Reads all contacts in the location, deletes any without a phone.
"""
import json, os, sys, time, urllib.request, urllib.parse

GHL_API_KEY  = os.environ["GHL_API_KEY"]
LOCATION_ID  = os.environ["GHL_LOCATION_ID"]
DRY_RUN      = os.environ.get("DRY_RUN", "false").lower() == "true"

BASE = "https://services.leadconnectorhq.com"

def headers():
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def ghl_get(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def ghl_delete(path):
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers=headers(), method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status

def get_all_contacts():
    contacts = []
    page = 1
    after = None
    while True:
        params = {"locationId": LOCATION_ID, "limit": 100}
        if after:
            params["startAfter"] = after
            params["startAfterId"] = after_id
        data = ghl_get("/contacts/", params)
        batch = data.get("contacts", [])
        if not batch:
            break
        contacts.extend(batch)
        meta = data.get("meta", {})
        after = meta.get("startAfter")
        after_id = meta.get("startAfterId")
        print(f"  Fetched {len(contacts)} contacts so far…", flush=True)
        if not meta.get("nextPageUrl") and not after:
            break
        time.sleep(0.3)
    return contacts

def main():
    print(f"{'🔍 DRY RUN — no deletions' if DRY_RUN else '🗑  LIVE — will delete'}")
    print(f"Location: {LOCATION_ID}\n")

    print("Fetching all contacts…")
    all_contacts = get_all_contacts()
    print(f"\nTotal contacts: {len(all_contacts)}")

    no_phone = [c for c in all_contacts if not c.get("phone", "").strip()]
    print(f"Contacts without phone: {len(no_phone)}")
    print(f"Contacts with phone:    {len(all_contacts) - len(no_phone)}\n")

    if not no_phone:
        print("✅ Nothing to delete.")
        return

    deleted = errors = 0
    for i, c in enumerate(no_phone, 1):
        cid  = c["id"]
        name = f"{c.get('firstName','')} {c.get('lastName','')}".strip() or "(no name)"
        if DRY_RUN:
            print(f"  [{i}/{len(no_phone)}] Would delete: {name} ({cid})")
            continue
        try:
            status = ghl_delete(f"/contacts/{cid}")
            deleted += 1
            if i <= 20 or i % 50 == 0:
                print(f"  [{i}/{len(no_phone)}] ✅ Deleted: {name}")
        except Exception as e:
            print(f"  [{i}/{len(no_phone)}] ✗ {name}: {e}")
            errors += 1
        time.sleep(0.25)

    print(f"\n{'DRY RUN complete' if DRY_RUN else 'Done'}:")
    if not DRY_RUN:
        print(f"  Deleted: {deleted}")
        print(f"  Errors:  {errors}")
    else:
        print(f"  Would delete: {len(no_phone)}")

if __name__ == "__main__":
    main()
