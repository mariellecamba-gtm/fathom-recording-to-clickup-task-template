---
name: fathom-to-clickup
description: "Run, debug and improve the Fathom to ClickUp automation in this repo (From Fathom action items, Watch call tasks, mention tasks). Use when: Fathom tasks look wrong, missing, duplicated or unrouted; adding a client; someone asks about the task format; or it's time to review what the automation created and improve it."
---

# Fathom to ClickUp

The automation in this repo turns Fathom calls into ClickUp tasks. This skill is how you
operate it and how you improve it from evidence: every task it creates is logged, the
audit reads them back from ClickUp, and changes come from what the owner actually did
with them.

| Thing | Where |
|---|---|
| Filing script | `scripts/fathom-to-clickup.py` |
| Audit script | `scripts/audit-tasks.py` |
| Id finder | `scripts/find-ids.py` (read-only: ClickUp user/list/field ids, Fathom titles and domains) |
| Settings | `config.json` (who you are, internal meetings, clients, ClickUp ids, model) |
| Workflows | `Fathom to ClickUp` (morning + evening slots), `Audit created tasks` and `Find IDs` (on demand) |
| Dedup state | `state/fathom_clickup_done.json` (hashes, committed after every run) |
| Task ledger | `state/created_tasks.jsonl` (one line per created task) |
| Audit output | `reports/audit-YYYY-MM-DD.md`, `reports/audit-latest.json`, `reports/client-options.json`, `reports/client-lists.json` |
| Learnings | `skill/LEARNINGS.md`, one dated line each |

Keys (`FATHOM_API_KEY`, `CLICKUP_API_TOKEN`, `ANTHROPIC_API_KEY`) live in the GitHub repo
secrets, or in a local `.env` that is never committed. Never print a key, paste one into
a file other than `.env`, or commit `.env`.

## The task format

Don't change it without evidence from the audit's Renamed section. Every name starts
with `task_prefix` (default `From Fathom:`), because that's how the automation's tasks
are spotted and the dedup reads the name.

| Pass | Name | Body |
|---|---|---|
| 1 `items` | `From Fathom: <action item text>` | Meeting title + date, the action item, `▶ Recording: <share url>  (scrub to HH:MM:SS)` |
| 2 `watch` | `From Fathom: Watch call: <call title>` | `New <Client> call recorded in Fathom (date)` + recording link |
| 3 `mentions` | `From Fathom: <mentions.label> — <Client> (MM/DD)` | One block per point: `🔴 ACTION` or `🔵 FYI`, a one-line instruction, the speaker's quote, scrub-to timestamp |

Every task is assigned to `me.clickup_user_id`, filed in the client's list, and has the
Client dropdown set when `clickup.client_field_id` is configured. Anything that can't be
routed goes to `clickup.unrouted_list_id`.

## Operating it

```bash
R=$(gh repo view --json nameWithOwner -q .nameWithOwner)

# What did recent runs create?
gh run list -R $R -L 10
gh run view <id> -R $R --log | grep -E "created →|ERROR|skip dup"

# Dry run over the last few days (creates nothing)
gh workflow run fathom-to-clickup.yml -R $R -f only=all -f days=3 -f dry_run=true

# Real run, one pass
gh workflow run fathom-to-clickup.yml -R $R -f only=items -f days=2
```

- **Add a client.** Add an entry to `clients` in `config.json`: `name`, `list_id`,
  `option_id`, `aliases` (keywords, including the ways Fathom mishears the name),
  `domains` (attendee email domains, for the watch pass) and `track_mentions` (for the
  mention pass). Get the ids from `gh workflow run find-ids.yml -R $R -f what=clickup`
  (or `python scripts/find-ids.py clickup` with a local `.env`), or from
  `reports/client-lists.json` and `reports/client-options.json` after an audit.
- **Fix a misroute.** The ClickUp API can't move tasks between lists. Move it in the UI,
  or recreate it in the right list and delete the original. Ask before deleting.
- **Force a re-create.** Remove its hash from `state/fathom_clickup_done.json`.
- **A red run.** The script exits non-zero when any ClickUp create or LLM call failed,
  even though everything else filed. Read the log's `ERROR` lines. An LLM error usually
  means the Anthropic key is out of credit or the configured model doesn't accept a
  setting (set `llm.fallbacks` to `null` for models without server-side fallback).
- **Runs are late.** GitHub delivers scheduled runs hours late sometimes. The guard keys
  on which cron fired, so a late run still files. Check githubstatus.com before suspecting
  billing.

## The improvement loop

Run it when the owner asks to improve or review the Fathom tasks, and about weekly
otherwise.

1. **Read back.** `gh workflow run audit.yml -R $R`, wait for it to finish
   (`gh run watch`), then `git pull` and read the newest `reports/audit-*.md`. Use
   `reports/audit-latest.json` for per-task detail.
2. **Turn signals into changes.** Each signal maps to a specific fix:

   | Signal | What it means | Change |
   |---|---|---|
   | Routing correction (moved list or Client changed) | Router picked the wrong client | Add or fix the client's `aliases`, or tighten the `classify_client_llm` prompt |
   | Unrouted item that was routed by hand | Client has no entry, or a mishear isn't aliased | New `clients` entry, or a new alias |
   | Renamed | Wording or format didn't work | Adjust the name or body builder only if several renames point the same way |
   | Dismissed (archived while open) or deleted, clustered by pass, client or kind | That category may be noise | Check the report's "Bulk dismissals" line first: 5+ in one minute is a backlog clear, not a verdict. Ask before dropping a whole category |
   | Suspected duplicate | Same work filed twice | Usually Fathom returning one meeting as two recordings; see Gotchas in the README |
   | Low done rate on a pass | The pass may not earn its keep | Report it with the numbers. Don't turn a pass off on your own |
   | Open longer than 14 days | Stale or not real work | Look for a pattern before acting |

3. **Change one thing per signal**, then dry-run it through the workflow
   (`-f dry_run=true -f days=7`) and compare the output with what was filed before.
4. **Log it.** Append one dated line to `skill/LEARNINGS.md` with the evidence (counts
   from the audit) and what changed.
5. **Ship it.** Commit and push, then report `N in / M out` for the audit plus the change
   made.

Never delete or edit ClickUp tasks as part of the loop without asking. The audit is
read-only.
