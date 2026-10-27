#!/usr/bin/env python3
"""
fathom-to-clickup.py: file your Fathom action items into ClickUp.

PASS 1 (items)    For each internal meeting (config: internal_meetings), take the action
                  items Fathom assigned to you, detect the client (alias match first, then
                  an LLM read of the transcript around the item, for Fathom mis-hears like
                  "Akme" and items whose client is only named in the surrounding talk), and
                  create "From Fathom: <item>" in that client's list.
PASS 2 (watch)    Any new call with an attendee from a client's domain -> a
                  "Watch call: <title>" task.
PASS 3 (mentions) Read the FULL transcript of the internal meetings and catch what PASS 1
                  misses: where one person (config: mentions.speaker, e.g. your manager)
                  *mentions* a client without it becoming a formal action item. One
                  "<label> — <client> (MM/DD)" task per client per meeting; body lists every
                  mention, tagged ACTION/FYI + timestamp.
All tasks: assigned to you, routed to the client's list, Client dropdown set if configured.
Dedup via state file so reruns never double-create.

Usage:
  python fathom-to-clickup.py                  # DRY RUN, PASS 1+2 (default)
  python fathom-to-clickup.py --only mentions  # DRY RUN, just PASS 3 (transcript scan)
  python fathom-to-clickup.py --only all       # DRY RUN, all three passes
  python fathom-to-clickup.py --only all --commit   # actually create the tasks
  python fathom-to-clickup.py --days 10        # look back N days (default 2)

Keys from the repo .env or the environment: FATHOM_API_KEY, CLICKUP_API_TOKEN.
ANTHROPIC_API_KEY is needed for PASS 3 and turns on PASS 1's LLM client fallback.
Everything else comes from config.json (see config.example.json).
"""
import os, sys, json, re, hashlib, argparse
import requests
import anthropic
from datetime import datetime, timezone
from common import ROOT, key, load_config, fathom_meetings

STATE = os.path.join(ROOT, "state", "fathom_clickup_done.json")
# One JSON line per task created. audit-tasks.py reads these back from ClickUp.
LEDGER = os.path.join(ROOT, "state", "created_tasks.jsonl")

FATHOM_KEY = key("FATHOM_API_KEY")
CLICKUP_TOK = key("CLICKUP_API_TOKEN")
ANTHROPIC_KEY = key("ANTHROPIC_API_KEY")

CFG = load_config()
PREFIX = CFG["task_prefix"]
ME = CFG["me"]
ME_EMAIL = ME["fathom_email"].lower()
ME_CU_ID = ME.get("clickup_user_id")
MEETINGS = set(CFG["internal_meetings"])     # empty = every meeting counts as internal
SPEAKER = CFG["mentions"].get("speaker")
MENTION_LABEL = CFG["mentions"].get("label")
LLM = CFG["llm"]
# Classifying one action item is easy; finding every mention in a full transcript
# is the step where more thinking pays off.
ROUTE_EFFORT, MENTION_EFFORT = "low", "medium"
CLIENT_FIELD_ID = CFG["clickup"].get("client_field_id")
UNROUTED_LIST = str(CFG["clickup"]["unrouted_list_id"])
UNROUTED_OPTION = CFG["clickup"].get("unrouted_option_id")

CLIENTS = CFG["clients"]
# canonical client -> (list_id, client_option_id)
ROUTES = {c["name"]: (str(c["list_id"]), c.get("option_id")) for c in CLIENTS}
# alias pattern (lowercase, whole word) -> canonical. The name itself always counts.
ALIASES = {"|".join(re.escape(a.lower()) for a in [c["name"], *(c.get("aliases") or [])]): c["name"]
           for c in CLIENTS}
# PASS 3 only files mention-tasks for these.
SUPPORTED_CLIENTS = [c["name"] for c in CLIENTS if c.get("track_mentions")]

def alias_hint(names):
    """'Acme (also heard as 'akme'), Globex.' Helps the model map Fathom mis-hears."""
    parts = []
    for c in CLIENTS:
        if c["name"] not in names: continue
        al = [a for a in (c.get("aliases") or []) if a.lower() != c["name"].lower()]
        parts.append(c["name"] + (" (also heard as " + ", ".join(f"'{a}'" for a in al) + ")" if al else ""))
    return ", ".join(parts) + "." if parts else ""

# --- "Watch the call" pass: any new call for a client with domains in config ---
# Detected by external attendee email domain (reliable; titles/crm_matches are not).
# domain -> (client label, list_id, Client option_id)  [reuses ROUTES ids]
CLIENT_CALL_DOMAINS = {d.lower().lstrip("@"): (c["name"], *ROUTES[c["name"]])
                       for c in CLIENTS for d in (c.get("domains") or [])}

def call_client(meeting):
    """Return (label, list_id, option_id) if this call has an attendee from a
    client's domain, else None."""
    for inv in (meeting.get("calendar_invitees") or []):
        email = (inv.get("email") or "") if isinstance(inv, dict) else ""
        if "@" not in email: continue
        dom = email.split("@")[1].lower()
        if dom in CLIENT_CALL_DOMAINS:
            return CLIENT_CALL_DOMAINS[dom]
    return None

def detect_client(text):
    t = text.lower()
    best = None  # (pos, canonical)
    for pat, canon in ALIASES.items():
        m = re.search(r"(?<!\w)(" + pat + r")(?!\w)", t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), canon)
    return best[1] if best else None

# Canonical clients the LLM fallback may route to (everything with a route).
CLASSIFY_CLIENTS = list(ROUTES.keys())

# Every LLM failure lands here. Items still file (Unrouted), but the run exits
# non-zero at the end, so a dead key or an empty credit balance shows up as a
# red run instead of a week of Unrouted tasks.
FAILURES = []

_claude = None
def ask_claude(system, user, schema, effort):
    """One structured-output call; returns the parsed JSON object. Raises on API
    errors, refusals and truncated replies."""
    global _claude
    if _claude is None: _claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    # fallbacks="default" re-runs a request the model declines on a fallback model,
    # server-side. Set llm.fallbacks to null for a model without that feature.
    extra = ({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": LLM["fallbacks"]}
             if LLM.get("fallbacks") else {})
    r = _claude.beta.messages.create(
        model=LLM["model"], max_tokens=16000, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        **extra)
    if r.stop_reason == "refusal":
        raise RuntimeError(f"model declined ({getattr(r.stop_details, 'category', None)})")
    if r.stop_reason == "max_tokens":
        raise RuntimeError("reply cut off at max_tokens")
    text = next((b.text for b in r.content if b.type == "text"), None)
    if not text: raise RuntimeError("reply had no text block")
    return json.loads(text)

def _to_sec(ts):
    p = (ts or "").split(":")
    return int(p[0])*3600 + int(p[1])*60 + int(p[2]) if len(p) == 3 and all(x.isdigit() for x in p) else None

def transcript_window(meeting, hhmmss, window_sec=120):
    """±window_sec of transcript around an HH:MM:SS mark, as labelled lines. This is
    the context the action item came from: the client is usually named here even
    when the item text doesn't say it (or Fathom mis-heard the name)."""
    segs = meeting.get("transcript")
    center = _to_sec(hhmmss)
    if not isinstance(segs, list) or not segs or center is None:
        return ""
    lines = []
    for s in segs:
        sec = _to_sec(s.get("timestamp"))
        if sec is not None and abs(sec - center) <= window_sec:
            who = ((s.get("speaker") or {}).get("display_name")) or "Unknown"
            lines.append(f"[{s.get('timestamp')}] {who}: {(s.get('text') or '').strip()}")
    return "\n".join(lines)

def classify_client_llm(desc, context):
    """LLM client attribution, used ONLY when the alias match misses. Fathom
    mis-hears names and the client is often named a few lines away, not in the item
    text. Returns None (-> Unrouted) with no key/context, for internal/cross-client
    items, and on errors (which are also recorded in FAILURES)."""
    if not ANTHROPIC_KEY or not context or not CLASSIFY_CLIENTS:
        return None
    schema = {"type": "object", "additionalProperties": False, "required": ["client"],
              "properties": {"client": {"type": "string", "enum": CLASSIFY_CLIENTS + ["NONE"],
                  "description": "Canonical client this item is for, or NONE if internal/scheduling/cross-client with no single owner."}}}
    system = (f"You route {ME['name']}'s meeting action items to the right client. Fathom often mis-hears client "
              f"names; aliases/mis-hears: {alias_hint(CLASSIFY_CLIENTS)} Use the surrounding transcript to decide: the "
              "client is often named a few lines before/after the item, not in the item text itself. "
              "CRITICAL: only pick a client from the list if the transcript is actually about THAT client. If the "
              "item is about a client that is NOT in the list (there are clients beyond this list), return "
              "NONE. NEVER substitute a different, similar-sounding, or merely related client. Also return NONE "
              "for internal ops, scheduling, or cross-client tasks with no single owner.")
    user = f"ACTION ITEM: {desc}\n\n--- SURROUNDING TRANSCRIPT ---\n{context}"
    try:
        c = ask_claude(system, user, schema, ROUTE_EFFORT).get("client")
    except Exception as e:
        FAILURES.append(f"client lookup: {e}")
        print(f"  [LLM ERROR, filing Unrouted] {e}")
        return None
    return c if (c and c != "NONE" and c in ROUTES) else None

def detect_action_item_client(meeting, item, desc):
    """Alias match first (fast/free), then transcript-context LLM for mis-hears /
    name-not-in-text cases."""
    return detect_client(desc) or classify_client_llm(desc, transcript_window(meeting, item.get("recording_timestamp") or ""))

def is_internal(meeting):
    return not MEETINGS or (meeting.get("title") or "") in MEETINGS

def is_mine(item):
    return ((item.get("assignee") or {}).get("email") or "").lower() == ME_EMAIL

def load_state():
    if os.path.exists(STATE):
        try: return set(json.load(open(STATE)))
        except Exception: return set()
    return set()
def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(sorted(s), open(STATE, "w"), indent=0)

def log_created(tid, pass_, name, client, list_id, option_id, meeting, **extra):
    """Append the new task to the ledger right away, so a crash mid-run still
    leaves a record of what exists in ClickUp."""
    row = {"task_id": tid, "pass": pass_, "client": client, "list_id": list_id,
           "option_id": option_id, "meeting_title": meeting.get("title"),
           "meeting_date": (meeting.get("created_at") or "")[:10] or None,
           "recording_id": meeting.get("recording_id"),
           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source": "run", "name": name, **extra}
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as fh: fh.write(json.dumps(row, ensure_ascii=False) + "\n")

def build_body(meeting, item, desc):
    # Fathom has no working timestamp deep-link (even its own API link won't seek),
    # so we give the recording link + the exact time to scrub to.
    share = meeting.get("share_url") or meeting.get("url", "")
    hhmmss = item.get("recording_timestamp") or ""
    lines = [f"Auto-created from Fathom **{meeting.get('title')}** "
             f"({(meeting.get('created_at') or '')[:10]}).",
             "", f"Action item: {desc}", ""]
    if share:
        at = f"  (scrub to **{hhmmss}**)" if hhmmss else ""
        lines.append(f"▶ Recording: {share}{at}")
    return "\n".join(lines)

def item_key(meeting, item):
    raw = str(meeting.get("recording_id") or meeting.get("url") or "") + "|" + (item.get("description") or "")
    return hashlib.sha1(raw.encode()).hexdigest()[:16]

# ---- PASS 3: transcript mention scan helpers ----
def transcript_script(meeting):
    """Flatten the Fathom transcript to a labelled script. Returns '' if the speaker
    never talks (no point calling the LLM)."""
    segs = meeting.get("transcript")
    if not isinstance(segs, list) or not segs: return ""
    saw, lines = False, []
    for s in segs:
        who = ((s.get("speaker") or {}).get("display_name")) or "Unknown"
        if who.strip().lower() == SPEAKER.strip().lower(): saw = True
        lines.append(f"[{s.get('timestamp') or '00:00:00'}] {who}: {(s.get('text') or '').strip()}")
    return "\n".join(lines) if saw else ""

def extract_mentions(meeting, already):
    """Ask Claude for every supported-client mention the speaker made. `already` =
    the action items PASS 1 already captured, so PASS 3 doesn't repeat them."""
    script = transcript_script(meeting)
    if not script: return []
    schema = {"type": "object", "additionalProperties": False, "required": ["mentions"],
        "properties": {"mentions": {"type": "array", "items": {"type": "object",
            "additionalProperties": False, "required": ["client", "points"],
            "properties": {"client": {"type": "string", "enum": SUPPORTED_CLIENTS},
                "points": {"type": "array", "items": {"type": "object",
                    "additionalProperties": False, "required": ["kind", "text", "quote", "timestamp"],
                    "properties": {
                    "kind": {"type": "string", "enum": ["ACTION", "FYI"], "description": f"ACTION if the mention implies work {ME['name']} could do; FYI if just status/context."},
                    "text": {"type": "string", "description": f"What {ME['name']} should do (ACTION) or know (FYI). Imperative, concrete, one sentence."},
                    "quote": {"type": "string", "description": f"A short direct quote from {SPEAKER} grounding this point."},
                    "timestamp": {"type": "string", "description": f"HH:MM:SS of {SPEAKER}'s line, copied from the transcript."}}}}}}}}}
    system = (f"You are triaging an internal meeting transcript for {ME['name']}, who {ME['role']}. "
        f"Find every place {SPEAKER.upper()} (only {SPEAKER}, ignore what others say) mentions one of "
        f"{ME['name']}'s SUPPORTED clients, and turn each into a point for them. "
        "Capture EVERY mention, even passing/status ones: tag it ACTION when the words imply something "
        f"{ME['name']} could do, otherwise FYI. Only use the canonical client names provided; map shorthand and "
        f"Fathom mis-hears to them: {alias_hint(SUPPORTED_CLIENTS)} Do NOT invent clients not in the list, and do "
        f"NOT repeat anything already in the 'already captured' list. If {SPEAKER} mentions no supported client, "
        "return an empty mentions array.")
    user = (f"Meeting: {meeting.get('title')} ({(meeting.get('created_at') or '')[:10]})\n"
        f"Supported clients (canonical names): {', '.join(SUPPORTED_CLIENTS)}\n"
        "Already captured as tasks (do not repeat):\n"
        + ("\n".join('- ' + a for a in already) if already else "- (none)")
        + f"\n\n--- TRANSCRIPT ---\n{script}")
    mentions = ask_claude(system, user, schema, MENTION_EFFORT).get("mentions") or []
    return [m for m in mentions if m.get("client") in SUPPORTED_CLIENTS and m.get("points")]

def build_mention_body(m, client, points):
    share = m.get("share_url") or m.get("url", "")
    head = (f"From Fathom **{m.get('title')}** ({(m.get('created_at') or '')[:10]}). "
            f"{SPEAKER} mentioned **{client}**, caught from the transcript (not a formally-assigned action item).\n")
    blocks = []
    for p in points:
        tag = "🔴 **ACTION**" if p.get("kind") == "ACTION" else "🔵 FYI"
        at = f"  _(scrub to {p.get('timestamp')})_" if p.get("timestamp") else ""
        blocks.append(f"{tag} — {p.get('text')}\n> “{p.get('quote')}”{at}")
    tail = [f"\n▶ Recording: {share}"] if share else []
    return "\n".join([head, *blocks, *tail])

def mention_key(meeting, client):
    raw = str(meeting.get("recording_id") or meeting.get("url") or "") + "|" + client + "|mention"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]

def create_task(list_id, name, desc, option_id):
    body = {
        "name": name,
        "description": desc,
        "assignees": [ME_CU_ID] if ME_CU_ID else [],
        "custom_fields": [{"id": CLIENT_FIELD_ID, "value": option_id}] if (CLIENT_FIELD_ID and option_id) else [],
    }
    r = requests.post(f"https://api.clickup.com/api/v2/list/{list_id}/task",
                      headers={"Authorization": CLICKUP_TOK, "Content-Type": "application/json"},
                      json=body, timeout=60)
    if r.status_code >= 300: raise RuntimeError(f"ClickUp {r.status_code}: {r.text[:200]}")
    return r.json().get("id")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--only", choices=["both", "items", "watch", "mentions", "all"], default="both")
    a = ap.parse_args()
    if not FATHOM_KEY or (a.commit and not CLICKUP_TOK):
        sys.exit("Missing FATHOM_API_KEY or CLICKUP_API_TOKEN (repo .env, or GitHub Actions secrets)")
    want_items    = a.only in ("both", "items", "all")
    want_watch    = a.only in ("both", "watch", "all")
    want_mentions = a.only in ("mentions", "all")
    if want_mentions and not (SPEAKER and SUPPORTED_CLIENTS):
        print("PASS 3 skipped: set mentions.speaker and track_mentions on at least one client to use it.\n")
        want_mentions = False
    if want_mentions and not ANTHROPIC_KEY:
        sys.exit("PASS 3 needs ANTHROPIC_API_KEY (repo .env, or the ANTHROPIC_API_KEY Actions secret).")

    done = load_state()
    # PASS 3 always needs the transcript; PASS 1 needs it too when the LLM client
    # fallback is available (to read the context around each action item).
    need_transcript = want_mentions or (want_items and bool(ANTHROPIC_KEY))
    meetings = fathom_meetings(FATHOM_KEY, a.days, include_transcript=need_transcript)
    print(f"{'COMMIT' if a.commit else 'DRY RUN'} | window {a.days}d | {len(meetings)} meeting(s) in range\n")
    planned = 0

    # ---- PASS 1: action items assigned to you in internal meetings ----
    pass1 = sorted(meetings, key=lambda x: x.get("created_at") or "") if want_items else []
    if want_items: print("--- PASS 1: your action items ---")
    for m in pass1:
        if not is_internal(m): continue
        mine = [ai for ai in (m.get("action_items") or []) if is_mine(ai)]
        if not mine: continue
        print(f"=== {m.get('created_at')} | {m.get('title')} | {len(mine)} item(s) ===")
        for ai in mine:
            desc = (ai.get("description") or "").strip()
            key_ = item_key(m, ai)
            if key_ in done: print(f"  [skip dup] {desc[:66]}"); continue
            client = detect_action_item_client(m, ai, desc)
            list_id, option_id = ROUTES.get(client, (UNROUTED_LIST, UNROUTED_OPTION))
            label = client or "UNROUTED"
            name, body = f"{PREFIX} {desc}", build_body(m, ai, desc)
            planned += 1
            if a.commit:
                try:
                    tid = create_task(list_id, name, body, option_id); done.add(key_)
                    log_created(tid, "items", name, client, list_id, option_id, m)
                    print(f"  [created → {label}] {desc[:60]} (task {tid})")
                except Exception as e:
                    FAILURES.append(f"create: {e}"); print(f"  [ERROR → {label}] {desc[:46]} :: {e}")
            else: print(f"  [→ {label:12}] {name[:78]}")

    # ---- PASS 2: "watch the call" for any new call with a client attendee ----
    # Fathom returns the same call as multiple records (diff recording_id/share_url),
    # so dedup by client+title+date: collapses those, but a weekly recurring call
    # (same title, different day) still gets its own task.
    if want_watch:
        watched = sorted({v[0] for v in CLIENT_CALL_DOMAINS.values()})
        print(f"\n--- PASS 2: watch client calls ({', '.join(watched) or 'no client has domains set'}) ---")
    uniq = {}
    for m in (meetings if want_watch else []):
        if MEETINGS and (m.get("title") or "") in MEETINGS: continue   # skip internal meetings
        hit = call_client(m)
        if not hit: continue
        label = hit[0]
        title = (m.get("title") or "Untitled call").strip()
        day = (m.get("created_at") or "")[:10]
        key_ = f"watch:{label}:{day}:{title.lower()}"
        uniq.setdefault(key_, (m, hit))                            # first record of the call wins
    for key_, (m, hit) in sorted(uniq.items(), key=lambda kv: kv[1][0].get("created_at") or ""):
        label, list_id, option_id = hit
        title = (m.get("title") or "Untitled call").strip()
        if key_ in done: print(f"  [skip dup → {label}] {title[:60]}"); continue
        share = m.get("share_url") or m.get("url", "")
        name = f"{PREFIX} Watch call: {title}"
        body = (f"New **{label}** call recorded in Fathom ({(m.get('created_at') or '')[:10]}).\n\n"
                f"▶ Recording: {share}")
        planned += 1
        if a.commit:
            try:
                tid = create_task(list_id, name, body, option_id); done.add(key_)
                log_created(tid, "watch", name, label, list_id, option_id, m)
                print(f"  [created → {label}] {title[:56]} (task {tid})")
            except Exception as e:
                FAILURES.append(f"create: {e}"); print(f"  [ERROR → {label}] {title[:46]} :: {e}")
        else: print(f"  [→ {label:12}] {name[:78]}")

    # ---- PASS 3: transcript mention scan, the speaker mentions a client ----
    # One task per client per internal meeting; body enumerates every mention the
    # speaker made about that client (each tagged ACTION/FYI, timestamped + quoted).
    pass3 = sorted([m for m in meetings if is_internal(m)],
                   key=lambda x: x.get("created_at") or "") if want_mentions else []
    if want_mentions: print(f"\n--- PASS 3: {SPEAKER}'s client mentions (transcript scan) ---")
    for m in pass3:
        already = [(ai.get("description") or "").strip()
                   for ai in (m.get("action_items") or []) if is_mine(ai)]
        already = [x for x in already if x]
        try:
            mentions = extract_mentions(m, already)
        except Exception as e:
            FAILURES.append(f"mention scan: {e}")
            print(f"  [ERROR extracting {m.get('title')} {(m.get('created_at') or '')[:10]}] :: {e}"); continue
        if not mentions: continue
        day = (m.get("created_at") or "")[5:10].replace("-", "/")   # MM/DD
        print(f"=== {m.get('created_at')} | {m.get('title')} | {len(mentions)} client(s) mentioned ===")
        for mn in mentions:
            client, points = mn["client"], mn.get("points", [])
            key_ = mention_key(m, client)
            if key_ in done: print(f"  [skip dup → {client}] {len(points)} point(s)"); continue
            list_id, option_id = ROUTES.get(client, (UNROUTED_LIST, UNROUTED_OPTION))
            name, body = f"{PREFIX} {MENTION_LABEL} — {client} ({day})", build_mention_body(m, client, points)
            n_act = sum(1 for p in points if p.get("kind") == "ACTION")
            planned += 1
            if a.commit:
                try:
                    tid = create_task(list_id, name, body, option_id); done.add(key_)
                    log_created(tid, "mentions", name, client, list_id, option_id, m,
                                points=len(points), actions=n_act)
                    print(f"  [created → {client}] {len(points)} pts ({n_act} ACTION) (task {tid})")
                except Exception as e:
                    FAILURES.append(f"create: {e}"); print(f"  [ERROR → {client}] :: {e}")
            else: print(f"  [→ {client:12}] {name[:50]}  ({len(points)} pts, {n_act} ACTION)")

    if a.commit: save_state(done)
    print(f"\n{'Created' if a.commit else 'Would create'} {planned} task(s).")
    if FAILURES:
        print(f"\n{len(FAILURES)} error(s) this run, first: {FAILURES[0]}")
        sys.exit(1)

if __name__ == "__main__":
    main()
