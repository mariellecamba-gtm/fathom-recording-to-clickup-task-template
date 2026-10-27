#!/usr/bin/env python3
"""
audit-tasks.py: read back every task fathom-to-clickup created and report what you
did with it. This is the feedback loop the skill in skill/ learns from.

For each task in state/created_tasks.jsonl it fetches the live ClickUp task and records:
  done / dismissed / open / deleted   did the task turn out to be real work?
                                      (dismissed = archived while still open)
  renamed                             the format or wording needed fixing
  moved list / Client changed         the router picked the wrong client
  suspected duplicate                 same meeting filed twice (Fathom double records)

Writes reports/audit-YYYY-MM-DD.md (readable) and reports/audit-latest.json (per task),
plus reports/client-options.json and reports/client-lists.json, the ids a new route needs.

Usage:
  python audit-tasks.py                   # every task in the ledger
  python audit-tasks.py --since 2026-10-01

Needs CLICKUP_API_TOKEN (repo .env, or the Actions secret).
"""
import os, sys, json, time, argparse
import requests
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from common import ROOT, key, load_config

LEDGER = os.path.join(ROOT, "state", "created_tasks.jsonl")
REPORTS = os.path.join(ROOT, "reports")
CFG = load_config()
CLIENT_FIELD_ID = CFG["clickup"].get("client_field_id")
UNROUTED_LIST = str(CFG["clickup"]["unrouted_list_id"])   # any client list carries the Client field
CLIENTS_SPACE = CFG["clickup"].get("clients_space_id")
MENTION_LABEL = CFG["mentions"].get("label") or "Mention"
STALE_DAYS = 14

CLICKUP_TOK = key("CLICKUP_API_TOKEN")

def get_task(tid):
    """Live task JSON, or {"_gone": code} when ClickUp no longer has it. Retries 429s
    until the rate-limit window resets (100 req/min on most plans)."""
    for _ in range(6):
        r = requests.get(f"https://api.clickup.com/api/v2/task/{tid}",
                         headers={"Authorization": CLICKUP_TOK}, timeout=60)
        if r.status_code == 429:
            reset = float(r.headers.get("X-RateLimit-Reset") or 0)
            time.sleep(max(1.0, min(65.0, reset - time.time() + 1)) if reset else 30); continue
        if r.status_code in (401, 404):
            # Deleted tasks answer 404 (ITEM_013) or 401 (no access to a trashed item).
            try: ecode = r.json().get("ECODE")
            except Exception: ecode = None
            if ecode == "OAUTH_025": sys.exit("ClickUp token invalid (OAUTH_025). Rotate CLICKUP_API_TOKEN.")
            return {"_gone": f"{r.status_code} {ecode or ''}".strip()}
        if r.status_code >= 300: raise RuntimeError(f"ClickUp {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"ClickUp kept rate-limiting task {tid}")

def client_option(task):
    """(option_id, option_name) currently set in the Client dropdown, or (None, None)."""
    for f in task.get("custom_fields") or []:
        if not CLIENT_FIELD_ID or f.get("id") != CLIENT_FIELD_ID: continue
        val = f.get("value")
        for o in (f.get("type_config") or {}).get("options") or []:
            if val is not None and (o.get("orderindex") == val or o.get("id") == val):
                return o.get("id"), o.get("name")
    return None, None

def ms_to_iso(ms, with_time=False):
    fmt = "%Y-%m-%dT%H:%M:%SZ" if with_time else "%Y-%m-%d"
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime(fmt) if ms else None

def audit_row(row, task):
    out = {k: row.get(k) for k in ("task_id", "pass", "client", "meeting_date", "created_at",
                                   "points", "actions")}
    out["original_name"] = row.get("name") or ""
    if "_gone" in task:
        out.update(state="deleted", detail=task["_gone"]); return out
    status = task.get("status") or {}
    closed = status.get("type") in ("closed", "done")
    opt_id, opt_name = client_option(task)
    cur_name = task.get("name") or ""
    # Archiving a task you won't do is the usual way to clear it, so archived while
    # still open = dismissed. Closed tasks count as done, archived or not.
    out.update(
        state="done" if closed else ("dismissed" if task.get("archived") else "open"),
        status=status.get("status"),
        updated_at=ms_to_iso(task.get("date_updated"), with_time=True),
        current_name=cur_name,
        renamed=cur_name != row.get("name"),
        list_now=(task.get("list") or {}).get("name"),
        moved=(task.get("list") or {}).get("id") != row.get("list_id"),
        client_now=opt_name,
        client_changed=bool(CLIENT_FIELD_ID) and (opt_id or None) != (row.get("option_id") or None),
        closed_on=ms_to_iso(task.get("date_closed") or task.get("date_done")),
        url=task.get("url"),
    )
    return out

def find_duplicates(rows):
    """Groups of tasks that look like one piece of work filed more than once."""
    groups = defaultdict(list)
    for r in rows:
        if r["pass"] == "mentions": groups[("mentions", r["client"], r["meeting_date"])].append(r)
        elif r["pass"] == "watch": groups[("watch", r["original_name"].lower(), r["created_at"][:10])].append(r)
    dups = [g for g in groups.values() if len(g) > 1]
    items = defaultdict(list)
    for r in rows:
        if r["pass"] == "items": items[r["meeting_date"]].append(r)
    for day, rs in items.items():
        seen = set()
        for i, a in enumerate(rs):
            if a["task_id"] in seen: continue
            g = [a] + [b for b in rs[i+1:] if b["task_id"] not in seen and
                       SequenceMatcher(None, a["original_name"].lower(), b["original_name"].lower()).ratio() >= 0.75]
            if len(g) > 1: dups.append(g); seen.update(x["task_id"] for x in g)
    return dups

def render(rows, since):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    L = [f"# Fathom to ClickUp audit, {today}", "",
         f"{len(rows)} tasks read back" + (f" (created since {since})" if since else "") + ".", "",
         "Dismissed = archived while still open.", "",
         "| Pass | Created | Done | Dismissed | Open | Deleted | Done rate |", "|---|---|---|---|---|---|---|"]
    for p in ("items", "watch", "mentions"):
        c = Counter(r["state"] for r in rows if r["pass"] == p)
        n = sum(c.values())
        if not n: continue
        rate = f"{100 * c['done'] / n:.0f}%"
        L.append(f"| {p} | {n} | {c['done']} | {c['dismissed']} | {c['open']} | {c['deleted']} | {rate} |")
    unrouted = [r for r in rows if r["pass"] == "items" and not r["client"]]
    if unrouted:
        c = Counter(r["state"] for r in unrouted)
        L.append(f"\nUnrouted action items: {len(unrouted)} ({c['done']} done, {c['dismissed']} dismissed, "
                 f"{c['open']} open, {c['deleted']} deleted).")
    mentions = [r for r in rows if r["pass"] == "mentions"]
    if mentions:
        for label, rs in (("with an ACTION point", [r for r in mentions if r.get("actions")]),
                          ("FYI only", [r for r in mentions if r.get("actions") == 0])):
            c = Counter(r["state"] for r in rs)
            L.append(f"{MENTION_LABEL} {label}: {len(rs)} ({c['done']} done, {c['dismissed']} dismissed, {c['open']} open).")
    # Many tasks dismissed in the same minute = a bulk cleanup, not task-by-task triage.
    sweeps = Counter((r.get("updated_at") or "")[:16] for r in rows if r["state"] == "dismissed")
    bulk = [(t, n) for t, n in sweeps.most_common() if t and n >= 5]
    if bulk:
        L.append("Bulk dismissals (5+ in one minute): " + ", ".join(f"{t}Z ({n})" for t, n in bulk) + ".")

    def section(title, picked, fmt):
        L.extend(["", f"## {title} ({len(picked)})", ""])
        L.extend(fmt(r) for r in picked) if picked else L.append("None.")

    live = [r for r in rows if r["state"] != "deleted"]
    section("Routing corrections: moved list or Client changed",
            [r for r in live if r.get("moved") or r.get("client_changed")],
            lambda r: f"- `{r['task_id']}` {r['pass']}: filed as **{r['client'] or 'UNROUTED'}**, now list "
                      f"**{r['list_now']}**, Client **{r['client_now'] or '-'}**. {r['current_name']}")
    section("Renamed",
            [r for r in live if r.get("renamed")],
            lambda r: f"- `{r['task_id']}` {r['pass']}: {r['original_name']}  →  {r['current_name']}")
    section("Deleted",
            [r for r in rows if r["state"] == "deleted"],
            lambda r: f"- `{r['task_id']}` {r['pass']} / {r['client'] or 'UNROUTED'} ({r['meeting_date'] or r['created_at'][:10]}): {r['original_name']}")
    dups = find_duplicates(rows)
    L.extend(["", f"## Suspected duplicates ({len(dups)} groups)", ""])
    for g in dups:
        L.append("- " + "  |  ".join(f"`{r['task_id']}` {r['state']}: {r['original_name']}" for r in g))
    if not dups: L.append("None.")
    now = datetime.now(timezone.utc)
    stale = [r for r in live if r["state"] == "open" and
             (now - datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))).days > STALE_DAYS]
    section(f"Open longer than {STALE_DAYS} days",
            stale,
            lambda r: f"- `{r['task_id']}` {r['pass']} / {r['client'] or 'UNROUTED'} ({r['created_at'][:10]}): {r['current_name']}")
    return "\n".join(L) + "\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only tasks created on/after YYYY-MM-DD")
    a = ap.parse_args()
    if not CLICKUP_TOK: sys.exit("Missing CLICKUP_API_TOKEN (repo .env, or GitHub Actions secret)")
    ledger = [json.loads(l) for l in open(LEDGER) if l.strip()] if os.path.exists(LEDGER) else []
    if a.since: ledger = [r for r in ledger if r["created_at"][:10] >= a.since]
    rows = []
    for i, row in enumerate(ledger, 1):
        rows.append(audit_row(row, get_task(row["task_id"])))
        if i % 25 == 0: print(f"  read {i}/{len(ledger)}")
    os.makedirs(REPORTS, exist_ok=True)
    md = render(rows, a.since)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    open(os.path.join(REPORTS, f"audit-{day}.md"), "w").write(md)
    json.dump(rows, open(os.path.join(REPORTS, "audit-latest.json"), "w"), indent=1, ensure_ascii=False)
    # The Client dropdown's current options. A new client needs its option id here
    # (plus its list id) before it can be added to config.json.
    if CLIENT_FIELD_ID:
        r = requests.get(f"https://api.clickup.com/api/v2/list/{UNROUTED_LIST}/field",
                         headers={"Authorization": CLICKUP_TOK}, timeout=60)
        if r.status_code < 300:
            f = next((f for f in r.json().get("fields", []) if f.get("id") == CLIENT_FIELD_ID), {})
            opts = [{k: o.get(k) for k in ("id", "name", "orderindex")}
                    for o in (f.get("type_config") or {}).get("options") or []]
            json.dump(opts, open(os.path.join(REPORTS, "client-options.json"), "w"), indent=1, ensure_ascii=False)
            print(f"{len(opts)} Client options written to reports/client-options.json")
        else: print(f"Could not read Client options: ClickUp {r.status_code}")
    # Every folder's lists in the clients space, for finding a new client's list id.
    if CLIENTS_SPACE:
        r = requests.get(f"https://api.clickup.com/api/v2/space/{CLIENTS_SPACE}/folder?archived=false",
                         headers={"Authorization": CLICKUP_TOK}, timeout=60)
        if r.status_code < 300:
            folders = [{"folder": f.get("name"), "folder_id": f.get("id"),
                        "lists": [{"id": l.get("id"), "name": l.get("name")} for l in f.get("lists") or []]}
                       for f in r.json().get("folders", [])]
            json.dump(folders, open(os.path.join(REPORTS, "client-lists.json"), "w"), indent=1, ensure_ascii=False)
            print(f"{len(folders)} folders written to reports/client-lists.json")
        else: print(f"Could not read the clients space: ClickUp {r.status_code}")
    print(md)
    print(f"{len(ledger)} in / {len(rows)} out")

if __name__ == "__main__":
    main()
