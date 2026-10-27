"""
common.py: shared by every script. Loads your keys (repo .env or the environment),
loads and checks config.json, and reads meetings from Fathom.
"""
import os, sys, json
import requests
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")

# --- keys ---
def load_env():
    d = {}
    if os.path.exists(ENV):
        for line in open(ENV):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    return d
_E = load_env()

def key(name):
    """A key from the repo .env, else the environment (GitHub Actions secrets)."""
    return _E.get(name) or os.environ.get(name) or None

# --- config.json ---
def config_path():
    return os.environ.get("FATHOM_CLICKUP_CONFIG") or os.path.join(ROOT, "config.json")

def load_config():
    """config.json with defaults filled in. Exits with a readable list of problems
    instead of failing halfway through a run."""
    path = config_path()
    if not os.path.exists(path):
        sys.exit(f"No config at {path}. Copy config.example.json to config.json and fill it in "
                 "(README, step 3).")
    try: c = json.load(open(path))
    except json.JSONDecodeError as e: sys.exit(f"{path} is not valid JSON: {e}")
    if c.get("_example"):
        sys.exit(f"{path} still holds the example values. Put in your own, then delete the "
                 "\"_example\" line.")
    problems = []
    me = c.get("me") or {}
    if not me.get("fathom_email"):
        problems.append("me.fathom_email is required (the email Fathom assigns your action items to)")
    if not (c.get("clickup") or {}).get("unrouted_list_id"):
        problems.append("clickup.unrouted_list_id is required (where tasks with no client go)")
    names = set()
    for i, cl in enumerate(c.get("clients") or []):
        n = cl.get("name")
        if not n: problems.append(f"clients[{i}] has no name"); continue
        if n in names: problems.append(f"client {n!r} is listed twice")
        names.add(n)
        if not cl.get("list_id"): problems.append(f"client {n!r} has no list_id")
    if problems:
        sys.exit(f"{path} needs fixing:\n  - " + "\n  - ".join(problems))

    c.setdefault("task_prefix", "From Fathom:")
    c.setdefault("internal_meetings", [])
    c.setdefault("clients", [])
    me.setdefault("name", "the user")
    me.setdefault("role", "works on these client accounts")
    c["me"] = me
    m = c.setdefault("mentions", {}) or {}
    if m.get("speaker"): m.setdefault("label", "Per " + m["speaker"].split()[0])
    c["mentions"] = m
    c.setdefault("clickup", {})
    llm = c.setdefault("llm", {}) or {}
    llm.setdefault("model", "claude-opus-5-5")
    llm.setdefault("fallbacks", "default")     # set to null for a model without server-side fallback
    c["llm"] = llm
    return c

# --- Fathom ---
def fathom_meetings(fathom_key, days, include_transcript=False, include_action_items=True):
    """Meetings created in the last `days` days, newest first, as Fathom returns them."""
    out, cursor = [], None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for _ in range(20):
        url = ("https://api.fathom.ai/external/v1/meetings"
               f"?include_action_items={'true' if include_action_items else 'false'}"
               f"&include_transcript={'true' if include_transcript else 'false'}")
        if cursor: url += f"&cursor={cursor}"
        r = requests.get(url, headers={"X-Api-Key": fathom_key}, timeout=60)
        if r.status_code == 401: sys.exit("Fathom rejected FATHOM_API_KEY (401). Generate a new key.")
        r.raise_for_status(); d = r.json()
        items = d.get("items", [])
        out += items
        # stop once we're past the window
        oldest = items[-1].get("created_at") if items else None
        cursor = d.get("next_cursor")
        if not cursor: break
        if oldest and datetime.fromisoformat(oldest.replace("Z","+00:00")) < cutoff: break
    res = []
    for m in out:
        try:
            if datetime.fromisoformat((m.get("created_at") or "").replace("Z","+00:00")) < cutoff: continue
        except Exception: pass
        res.append(m)
    return res
