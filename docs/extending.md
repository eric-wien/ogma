# Extending Ogma: your own commands, no fork required

Ogma's core is a chat-driven command runner restricted to commands **you**
predefine. This is the tutorial for adding one — end to end, without touching
any tracked file, so `git pull` never conflicts with your setup. No LLM
involved anywhere on this path.

## The three tiers (what you're extending)

1. **Core** — the gateway + command runner (`gateway.py`,
   `transport_telegram.py`, `bin/ogmactl`). Python stdlib only.
2. **LLM layer** (optional) — free-text chat via Claude Code or OpenAI Codex
   (`llm_claude.py` / `llm_codex.py`; `OGMA_LLM=claude|codex`).
3. **Assistant layer** (optional, needs the LLM layer) — briefing, dream/memory,
   persona, skills.

Your own commands extend tier 1 and work identically in all modes.

## How a command flows

```
Telegram /mycmd 30m
   └─ gateway: allow-list + role check → arg regexes → (optional /confirm)
        └─ bin/ogmactl mycmd 30m         (argv, never a shell string)
             └─ bin/ogmactl.local mycmd 30m   (your code, gitignored)
```

Two layers both have to say yes: the JSON file *declares* the command
(name, menu text, validation, protection), and `ogmactl`/`ogmactl.local`
*implements and gates* it. Declaring something in JSON can never run anything
your ogmactl.local would refuse.

Your local files can't leak into a fork/PR by accident: `.gitignore` ignores
anything with `.local` in its name wherever it lives, and `bin/`, `config/` and
`systemd/` are default-deny — everything in them is ignored except the
whitelisted public files. Anything you add there stays on your host without
any `.gitignore` edit.

## Step 1 — implement the subcommand (`bin/ogmactl.local`)

Create an executable `bin/ogmactl.local` (gitignored; `ogmactl` delegates any
subcommand it doesn't recognise to it):

```bash
#!/usr/bin/env bash
set -uo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "${1:-}" in
  disk-usage)
    df -h / /home 2>/dev/null
    ;;
  vpn-restart)
    systemctl --user restart my-vpn && echo "✅ VPN restarted"
    ;;
  help)
    echo "          local: disk-usage | vpn-restart"
    ;;
  *)
    echo "ogmactl.local: unknown command '${1:-}'" ; exit 2
    ;;
esac
```

```bash
chmod +x bin/ogmactl.local
bin/ogmactl disk-usage      # test from the shell first
```

Discipline that keeps this safe:

- **Whitelist, don't pass through.** The `*)` arm must refuse. Never
  `eval`/`sh -c` anything derived from arguments.
- **Treat arguments as data.** They arrive as separate argv words, never via a
  shell — keep it that way in your code (quote `"$2"`, use parameterized SQL,
  etc.).
- **Prefer read-only.** For write operations, keep them bounded and
  reversible, and protect them with `confirm` in step 2.
- **Exit non-zero on failure** — the exit code lands in the audit log.

## Step 2 — declare it (`config/commands.local.json`)

Copy `config/commands.local.example.json` to `config/commands.local.json`
(gitignored) if you haven't, and add:

```json
{"cmd": "disk",        "run": "disk-usage",  "desc": "Disk usage", "menu": true, "guest": true},
{"cmd": "vpn_restart", "run": "vpn-restart", "desc": "Restart the VPN", "menu": false,
 "confirm": true}
```

Field reference (see the example file for the full comments):

| Field | Meaning |
|---|---|
| `cmd` | Telegram command name, `[a-z0-9_]`, ≤32 chars |
| `run` | the ogmactl subcommand it maps to |
| `desc` | text in the / menu and /help |
| `menu` | show in Telegram's / menu (hidden commands still work when typed) |
| `args` | usage hint for /help, e.g. `"<dur>"` |
| `guest` | `TELEGRAM_GUEST_USERS` may run it — read-only commands only |
| `confirm` | arm instead of run; fires only on `/confirm` within the window (default 180s) |
| `validate` | list of regexes, one per positional arg, checked **before** execution; the list length is the max arg count; a broken regex disables the command |
| `min_args` | how many of those args are required |

Validated example — a bounded duration argument:

```json
{"cmd": "vpn_pause", "run": "vpn-pause", "desc": "Pause the VPN", "menu": false,
 "args": "<dur>", "validate": ["^[0-9]{1,4}[smh]?$"], "min_args": 1, "confirm": true}
```

## Step 3 — restart and test over chat

```bash
bin/ogmactl restart
```

The gateway re-registers the Telegram menu on every start, so `/disk` appears
immediately. Test the failure paths too: a bad argument (should be refused
with the usage hint), `/vpn_restart` (should demand `/confirm`), and — if you
use guests — that a guest chat can't see or run what it shouldn't.

Everything executed lands in `state/audit.log` (JSON lines: actor, argv, exit
code, duration) — `/logs audit` shows it over chat.

## Optional: document your commands for the bot

If you keep a reference for your host-local commands in
`docs/commands.local.md` (gitignored — usage, arguments, gotchas, one short
section per command), `bin/setup` will point the persona at it, and Ogma will
consult it when someone asks how a command works or what options exist.

## What NOT to do

- Don't edit `gateway.py`, `bin/ogmactl`, or other tracked files for a
  host-local command — you'd fork yourself away from updates. The two local
  files above are the whole extension surface.
- Don't add commands that print secrets (tokens, keys) — chat transcripts and
  the gateway log are not a secret store.
- Don't wrap arbitrary shell (`{"cmd": "sh", ...}`) — you'd be handing your
  chat app a root-adjacent terminal and defeating the entire design.

## Sharing a command set

`bin/ogmactl.local` + `config/commands.local.json` are self-contained: copy
them to another Ogma install (e.g. via `bin/backup` archives, which include
all host-local files) and they work as-is. If you build something generally
useful, PRs against the *core* `bin/ogmactl` are welcome — that's the tier
where shared commands live.
