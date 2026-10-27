#!/usr/bin/env python3
"""
find-ids.py: print the ids and names config.json needs. Read-only, creates nothing.

  python scripts/find-ids.py clickup               # your user id, then every space, folder and list
  python scripts/find-ids.py clickup --list ID     # that list's dropdown fields and their option ids
  python scripts/find-ids.py fathom --days 14      # recent meeting titles + attendee domains

`clickup` needs CLICKUP_API_TOKEN, `fathom` needs FATHOM_API_KEY (repo .env or the
environment). Neither needs config.json.
"""
import sys, argparse
import requests
from collections import defaultdict
from common import key, fathom_meetings

def cu(path):
    r = requests.get(f"https://api.clickup.com/api/v2/{path}",
                     headers={"Authorization": key("CLICKUP_API_TOKEN")}, timeout=60)
    if r.status_code == 401: sys.exit("ClickUp rejected CLICKUP_API_TOKEN (401). Generate a new one.")
    r.raise_for_status()
    return r.json()

def clickup(list_id):
    if not key("CLICKUP_API_TOKEN"): sys.exit("No CLICKUP_API_TOKEN. Add it as a repository secret (README, step 2), or to .env for local runs.")
    if list_id:
        print(f"Custom fields on list {list_id} (Client dropdown -> clickup.client_field_id,")
        print("each option -> a client's option_id):\n")
        for f in cu(f"list/{list_id}/field").get("fields", []):
            print(f"  {f.get('name')}  [{f.get('type')}]  field id: {f.get('id')}")
            for o in (f.get("type_config") or {}).get("options") or []:
                print(f"      option {o.get('name')!r}: {o.get('id')}")
        return
    u = cu("user").get("user", {})
    print(f"You: {u.get('username')} <{u.get('email')}>  ->  me.clickup_user_id = {u.get('id')}\n")
    for team in cu("team").get("teams", []):
        print(f"Workspace {team.get('name')} ({team.get('id')})")
        for sp in cu(f"team/{team['id']}/space?archived=false").get("spaces", []):
            print(f"  Space {sp.get('name')}  ->  clickup.clients_space_id = {sp.get('id')}")
            for fo in cu(f"space/{sp['id']}/folder?archived=false").get("folders", []):
                print(f"    Folder {fo.get('name')}")
                for l in fo.get("lists") or []:
                    print(f"      List {l.get('name')}: {l.get('id')}")
            for l in cu(f"space/{sp['id']}/list?archived=false").get("lists", []):
                print(f"    List {l.get('name')} (no folder): {l.get('id')}")
    print("\nNext: python scripts/find-ids.py clickup --list <a client list id>  for the Client dropdown ids.")

def fathom(days):
    fk = key("FATHOM_API_KEY")
    if not fk: sys.exit("No FATHOM_API_KEY. Add it as a repository secret (README, step 2), or to .env for local runs.")
    meetings = fathom_meetings(fk, days, include_action_items=False)
    by_title = defaultdict(lambda: {"n": 0, "domains": set(), "last": ""})
    for m in meetings:
        t = by_title[(m.get("title") or "Untitled").strip()]
        t["n"] += 1
        t["last"] = max(t["last"], (m.get("created_at") or "")[:10])
        for inv in m.get("calendar_invitees") or []:
            email = (inv.get("email") or "") if isinstance(inv, dict) else ""
            if "@" in email: t["domains"].add(email.split("@")[1].lower())
    print(f"{len(meetings)} recording(s) in the last {days} days, {len(by_title)} distinct title(s).")
    print("Internal meetings go in internal_meetings (exact title); client domains go in each client's domains.\n")
    for title, t in sorted(by_title.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        print(f"  {t['n']:>3}x  last {t['last']}  {title}")
        if t["domains"]: print(f"         attendees from: {', '.join(sorted(t['domains']))}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["clickup", "fathom"])
    ap.add_argument("--list", help="ClickUp list id to show custom fields for")
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    clickup(a.list) if a.what == "clickup" else fathom(a.days)

if __name__ == "__main__":
    main()
