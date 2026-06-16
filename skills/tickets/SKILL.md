---
name: tickets
description: Pick up and work on tickets that Ogma (the Telegram bot) filed for things it couldn't do under its restrictions. Use when the user says "tickets", "what did the bot leave me", "any open tickets", "work on the ticket(s)", or "what's in the queue".
---

# Tickets

Ogma runs on Telegram with a deliberately limited toolset (read-only + web + the `ogmactl`
whitelist). When it hits something it can't do — editing files, writing code, running arbitrary
commands — it files a **ticket** via `ogmactl ticket "..."`. This skill is how a full interactive
session (where you have all tools) picks those up. This is the core of Ogma's two-surface workflow
— see `docs/workflow.md`.

## Where
- Open tickets: `~/ogma/tickets/*.md`
- Resolved tickets: `~/ogma/tickets/done/`
- Each ticket is markdown with frontmatter (`id`, `created`, `status`, `source`) then a body.

(Adjust the path if you installed Ogma somewhere other than `~/ogma`.)

## Listing
Read `~/ogma/tickets/` (the top level only; ignore `done/`). For each open ticket show its `id`,
age, and a one-line summary. If there are none, say so.

## Working a ticket
1. Read the full ticket. Confirm with the user which one to tackle if there are several.
2. Do the work in this session (you have full tools here).
3. When finished, resolve it:
   - Append a `## Resolution` section to the ticket: what you did, the date, and any follow-up.
   - Set `status: done` in the frontmatter.
   - Move the file to `~/ogma/tickets/done/`.
4. Briefly tell the user what you did. If the fix changed how Ogma works (e.g. a new `ogmactl`
   subcommand or a relaxed permission), note that so the bot benefits next time.

## Notes
- Don't auto-resolve a ticket you only partially addressed — leave it open and note progress.
- If a ticket is unclear, leave it open and ask the user (or message back via `~/ogma/bin/tg-send`)
  for detail.
