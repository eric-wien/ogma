# How Ogma works — the design

Ogma is built around one core idea: **two surfaces, one brain, bridged by tickets.**

## The two surfaces

**1. The bot (Telegram) — always on, deliberately restricted.**
Your assistant lives on an always-on machine and you reach it from your phone. Because that machine
may hold SSH keys, wallets, and credentials — and because a chat surface is comparatively exposed —
the bot runs with a *tight* toolset: read-only + web, plus exactly one whitelisted shell helper
(`ogmactl`). It can answer, research, recall, and manage itself, but it **cannot** edit files, write
code, or run arbitrary commands. That restriction is the safety model, not a limitation to "fix".

**2. The interactive session — full power, on demand.**
When you sit down at the machine (or SSH in) and run Claude Code directly, you get the full toolset:
edits, code, shell, the works. This is where real changes happen.

```
   ┌─────────────────────────┐         ┌──────────────────────────────┐
   │  Telegram bot (Ogma)     │         │  Interactive Claude Code      │
   │  always on, restricted   │         │  full tools, on demand        │
   │  read-only + web + ogmactl│        │  edit / code / shell          │
   └───────────┬─────────────┘         └───────────────┬──────────────┘
               │ can't do it under restrictions          │ picks it up
               │  → ogmactl ticket "..."                 │  → `tickets` skill
               ▼                                         ▼
        ┌──────────────────────────  tickets/  ──────────────────────────┐
        │  *.md (open)   →   work it   →   ## Resolution + status: done    │
        │                                  →   tickets/done/              │
        └─────────────────────────────────────────────────────────────────┘
                          shared: memory · skills · workspace persona
```

## The bridge: tickets

This is the key move. When the bot is asked for something it can't safely do, it doesn't fake a
result and it doesn't get unsafe new powers — it **files a ticket**:

```
ogmactl ticket "Add a /weather command that ..."
```

That writes a markdown file into `tickets/`. Later, in a full interactive session, you say
"tickets" and the **`tickets` skill** lists the queue, you (with Claude) work the item with full
tools, append a `## Resolution`, mark it `done`, and move it to `tickets/done/`. If the fix taught
the system something — a new `ogmactl` subcommand, a relaxed permission — that knowledge stays.

Net effect: the bot stays safe and honest ("I can't do that here, but I filed a ticket"), and
nothing falls through the cracks.

## The shared brain

Both surfaces share the same context, so the bot and your interactive sessions feel like one
assistant:

- **Persona** — `workspace/CLAUDE.md` defines who Ogma is and how it behaves.
- **Memory** — Claude Code's per-project memory (`~/.claude/projects/<project>/memory/`) holds
  durable facts about you and your projects. The bot reads and writes it; so do you.
- **Skills** — reusable procedures in `~/.claude/skills/` (`tickets`, `session-search`,
  `daily-briefing`, and your own). Both surfaces load them on demand. See `../skills/`.

## Keeping itself sharp

- **Dream (nightly)** — `bin/dream` runs unattended: it distils the last ~24h into a rolling
  `yesterday.md` and tidies long-term memory (merge dupes, drop stale facts), snapshotting first.
- **Persist nudge (per turn)** — a Stop hook (`hooks/persist-nudge.py`) occasionally spawns a
  detached pass that saves anything durable from the conversation — without delaying your reply.
- **Self-management** — `ogmactl` lets the bot check its own status/logs, restart itself, and
  report host health, all without arbitrary shell.
- **Scheduled routines** — `briefing` / `health-check` deliver to Telegram via `tg-send`; add your
  own the same way.

## The loop, in one line
Talk to the bot → it does what it safely can, files tickets for the rest → you clear tickets in a
full session → memory + skills carry the learning forward → the bot is a little more capable
tomorrow.
