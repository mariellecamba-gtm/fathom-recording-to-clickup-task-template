# Fathom recording to ClickUp task

**Turns your Fathom call recordings into ClickUp tasks, filed in the right client's list,
with a link to the exact minute of the call.** Runs on GitHub Actions twice a day. You
bring your own Fathom, ClickUp and Anthropic keys; nothing in this repo talks to anyone
else's account.

Every task starts with `From Fathom:`, so you can tell at a glance which tasks the
automation made. A day's output looks like this:

| Task | Filed in | Body |
|---|---|---|
| `From Fathom: Send Acme the revised Q3 list` | Acme | the meeting, the action item, `▶ Recording: <link>  (scrub to 00:14:32)` |
| `From Fathom: Watch call: Globex weekly sync` | Globex | `New Globex call recorded in Fathom (2026-03-04)` + recording link |
| `From Fathom: Per Jordan — Acme (03/04)` | Acme | `🔴 ACTION` / `🔵 FYI` points, each with Jordan's quote and a timestamp |

## What it does

It runs three passes over your recent Fathom meetings.

| Pass | `--only` | What it files |
|---|---|---|
| 1 | `items` | Action items Fathom assigned **to you** in your internal meetings, as `From Fathom: <item>` |
| 2 | `watch` | Any new call with an attendee from a client's email domain, as `From Fathom: Watch call: <title>` |
| 3 | `mentions` | A full-transcript scan for clients **one person** (your manager, say) mentions without assigning anything, as `From Fathom: Per <name> — <client> (MM/DD)` |

Pass 1 routes each item to a client by name first, then, if no name matches, asks Claude
to read the two minutes of transcript around the item. Fathom mishears client names, and
the client is often named in the surrounding talk rather than in the item. Anything it
can't route goes to an Unrouted list you choose.

Every task is assigned to you and filed in that client's list. If you use a Client
dropdown custom field, it gets set too. A dedup file means reruns never double-create.

## What you need

| | What for | Cost |
|---|---|---|
| **Fathom** with API access | reading your meetings | your existing plan |
| **ClickUp** personal API token | creating tasks | your existing plan |
| **Anthropic API key** (optional) | pass 3, and pass 1's fallback for items with no client name | pennies per run, see [Costs](#costs) |
| **GitHub account** | running it on a schedule | free; a private repo uses roughly 150 of the 2,000 free Actions minutes a month |

Without an Anthropic key, passes 1 and 2 still run. Pass 1 then routes by client name
only.

## Setup

### 1. Make your own copy

Click **Use this template**, then **Create a new repository**. **Make it private.** Your
`config.json` will list your clients' names and email domains. It holds no keys, but
your client list is still your business.

```bash
git clone https://github.com/<you>/<your-copy>.git
cd <your-copy>
python3 -m venv .venv && source .venv/bin/activate     # Python 3.10 or newer
pip install -r requirements.txt
cp .env.example .env
```

### 2. Get your keys

Put them in `.env` (for running locally). `.env` is in `.gitignore` and never gets
committed.

- **Fathom:** in Fathom, Settings, then API Access. The key sees the recordings your
  Fathom account can see.
- **ClickUp:** in ClickUp, Settings, then Apps, then API Token. It starts with `pk_`.
- **Anthropic** (optional): create a key in the Claude Console.

### 3. Fill in `config.json`

```bash
cp config.example.json config.json
python scripts/find-ids.py clickup             # your user id, and every space, folder and list id
python scripts/find-ids.py clickup --list ID   # a list's dropdown fields and option ids
python scripts/find-ids.py fathom --days 14    # your recent meeting titles and attendee domains
```

`find-ids.py` is read-only. Copy what it prints into `config.json`, then delete the
`"_example": true` line. The script refuses to run while that line is there.

| Key | What it is |
|---|---|
| `me.name` | Your first name. The LLM prompts use it. |
| `me.fathom_email` | The email Fathom assigns your action items to. Pass 1 files only items assigned to this address. |
| `me.clickup_user_id` | Every task is assigned to this user. `null` leaves tasks unassigned. |
| `me.role` | One line on what you do for these clients. It helps pass 3 tell ACTION from FYI. |
| `internal_meetings` | Exact Fathom titles of the meetings passes 1 and 3 read, and pass 2 skips. An empty list means every meeting. That makes pass 3 scan every transcript, which costs more. |
| `mentions.speaker` | Pass 3 watches what this person says, matched to their Fathom display name. Leave it out to turn pass 3 off. |
| `mentions.label` | The start of a pass 3 task name. Default `Per <first name>`. |
| `task_prefix` | Default `From Fathom:`. Dedup reads task names, so pick one and keep it. |
| `clickup.unrouted_list_id` | **Required.** Where tasks with no matching client go. |
| `clickup.client_field_id` | Optional. The id of a dropdown custom field that names the client. |
| `clickup.unrouted_option_id` | Optional. The dropdown option to set on Unrouted tasks. |
| `clickup.clients_space_id` | Optional. The audit lists this space's folders and lists, so adding a client is copy and paste. |
| `llm.model` | Default `claude-opus-5-5`. |
| `llm.fallbacks` | Default `"default"`: if the model declines a request, the API re-runs it on a fallback model. Set it to `null` if your model doesn't support server-side fallback. |
| `clients[].name` | The client's name, as it appears in task routing and the audit. |
| `clients[].list_id` | The ClickUp list this client's tasks go to. |
| `clients[].option_id` | The client's option in the Client dropdown, or `null`. |
| `clients[].aliases` | Other ways the name shows up, including Fathom's mishears (`"akme"`). Matching is case-insensitive and whole-word. The name itself always counts. |
| `clients[].domains` | Attendee email domains. A call with someone from one of them gets a pass 2 "Watch call" task. `[]` turns this off for the client. |
| `clients[].track_mentions` | `true` to include this client in the pass 3 scan. |

### 4. Try it locally

A dry run is the default. It reads Fathom and prints what it would file, and creates
nothing.

```bash
python scripts/fathom-to-clickup.py --only all --days 7            # dry run
python scripts/fathom-to-clickup.py --only all --days 7 --commit   # create the tasks
```

### 5. Turn on the schedule

1. Commit `config.json` (in your private copy) and push.
2. Add the three keys as repository secrets under Settings, then Secrets and variables,
   then Actions: `FATHOM_API_KEY`, `CLICKUP_API_TOKEN` and, optionally,
   `ANTHROPIC_API_KEY`.
3. Under Actions, enable workflows if GitHub asks, then open **Fathom to ClickUp**, click
   **Run workflow** and tick `dry_run` once to check the output.

After every run, the workflow commits `state/` back to the repo. That's the dedup
memory, because runners start empty. It also counts as repo activity, and GitHub turns
off scheduled workflows in a repo that has had none for 60 days.

## Schedule

Passes 1 and 3 run at **10:45** and pass 2 at **18:00**, New York time, by default.

GitHub's cron runs in UTC and ignores daylight saving, so each slot is registered twice,
once for summer time and once for winter. A guard step works out which local time the
cron that fired stands for today, and skips the one that's wrong. The skipped run takes
a few seconds.

To use your own timezone and times:

1. Set repository **variables** (not secrets): `TIMEZONE` (for example
   `Europe/London`), `MORNING` (for example `09:00`) and `EVENING` (for example `17:30`).
2. In `.github/workflows/fathom-to-clickup.yml`, replace the four `cron:` lines with
   your two times in UTC, once for summer time and once for winter. London at 09:00
   is `0 8 * * *` in summer and `0 9 * * *` in winter. A timezone without daylight
   saving needs only one line per slot.

You can also run any pass by hand from the Actions tab with **Run workflow**. It takes
`only`, `days` and a `dry_run` toggle.

## Audit, and the Claude skill

Every task the script creates is appended to `state/created_tasks.jsonl`. Running
**Actions, then Audit created tasks** reads each one back from ClickUp and commits
`reports/audit-YYYY-MM-DD.md`. The report covers:

- done, dismissed (archived while open), open and deleted counts, per pass
- tasks moved to another list or given a different Client by hand, which means the router was wrong
- tasks you renamed, which means the format didn't work for you
- suspected duplicates and tasks open for more than 14 days

`skill/` is a Claude Code skill that runs this as a loop. It reads the audit, turns each
signal into one small change (a new alias, a new client, a tighter prompt), dry-runs the
change, and logs what it learned in `skill/LEARNINGS.md`. To install it:

```bash
ln -s "$PWD/skill" ~/.claude/skills/fathom-to-clickup
```

Then ask Claude Code to "review the Fathom tasks".

## Costs

These are rough figures on the default model; check the current Anthropic pricing.

- **Pass 1 fallback:** one small call per action item that names no client. About a cent
  or less each.
- **Pass 3:** one call per internal meeting the speaker talks in, over the full
  transcript. About 10 to 20 cents for an hour-long meeting.
- **Pass 2:** no LLM.
- **GitHub Actions:** four short runs a day.

## Gotchas

- Fathom has **no working timestamp deep-link**. Task bodies give the share URL plus an
  `HH:MM:SS` to scrub to.
- Fathom sometimes returns one meeting as **two recordings** about 30 seconds apart.
  Pass 2 collapses these (it dedups on client, title and date). Passes 1 and 3 dedup per
  recording, so an item can occasionally be filed twice. The audit lists these under
  Suspected duplicates.
- ClickUp's API **can't move a task between lists**. To fix a misroute, move it in the
  ClickUp UI.
- To force a re-create, remove the task's hash from `state/fathom_clickup_done.json`.
- A run goes **red** when any ClickUp create or LLM call fails, even though everything
  else filed. Items whose LLM lookup failed are filed Unrouted.

## Security

- Keys live only in your repo's secrets or your local `.env`. Neither is ever committed,
  and the scripts never print them.
- `config.json` holds ids, emails and client names, not keys. Keep your copy private.
- The scripts call only `api.fathom.ai`, `api.clickup.com` and `api.anthropic.com`.

## License

MIT
