# Ogma

A minimal personal-assistant gateway: talk to **Claude Code** from **Telegram**, from anywhere.
Inspired by [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent), rebuilt
on Claude Code's native skills + memory + subagents. Named for Ogma, the Celtic god of eloquence
and the inventor of writing.

One always-on Python process (stdlib only) long-polls Telegram and bridges each chat to a
resumable headless `claude` session. No inbound ports, no pip installs, no API key plumbing — it
reuses your existing Claude Code auth on the box.

```
gateway.py               the bridge (Telegram long-poll <-> `claude -p`)
workspace/               Claude's working dir; workspace/CLAUDE.md is the assistant persona
workspace/.claude/       gateway-scoped settings (the memory-persist Stop hook)
hooks/persist-nudge.py   the out-of-band "remember durable facts" pass (Stop hook)
bin/                     setup (guided installer) + ogmactl (self-management) + routines (briefing/dream/health)
skills/                  reusable procedures (tickets, session-search, daily-briefing) → ~/.claude/skills/
tickets/                 the bot↔interactive-session bridge (see docs/workflow.md)
systemd/                 unit templates for always-on operation
docs/workflow.md         how the whole thing is designed — start here
.env.example             config template
```

## How it works (read this)
Ogma has **two surfaces, one brain, bridged by tickets**: an always-on but deliberately *restricted*
Telegram bot, and your *full-power* interactive Claude Code sessions. When the bot can't safely do
something (edit files, run code), it files a **ticket** instead of faking it; you clear tickets in a
full session with the `tickets` skill, and shared memory + skills carry the learning forward. The
full design — and why the bot is intentionally limited — is in **[docs/workflow.md](docs/workflow.md)**.

> **Self-host model.** Ogma is meant to be run by you, on your own always-on machine, talking to
> your own Telegram bot, using your own Claude Code auth. There is no hosted service and nothing
> phones home.

## ⚠️ Security — read this first

This bridges a chat app to an agent on a machine that may hold **SSH keys, wallets, credentials,
and sudo**. Treat it accordingly:

- **Keep `TELEGRAM_ALLOWED_USERS` tight** — only your own chat ID(s). Everyone else is denied, but
  the bot token itself is a secret: `chmod 600 .env`.
- The persona (`workspace/CLAUDE.md`) tells Claude to refuse touching secrets over chat, but that
  is **guidance, not a sandbox.**
- Tool permissions default to the **safe** posture: tools needing approval are skipped in headless
  mode. Opt into more (`OGMA_PERMISSION_MODE` / `OGMA_ALLOWED_TOOLS`) **deliberately.**
- Do **not** set `--dangerously-skip-permissions`.
- The allow-list is also your **cost control** — anyone you allow can spend your Claude usage.

Provided as-is, no warranty. You are responsible for what you let it do on your machine.

## Setup

> **You run your own bot.** Ogma is self-hosted: every user creates their **own** Telegram bot and
> uses their **own** Claude Code auth. There is no shared bot or service — the repo ships no token.

### Quick start (recommended)
```bash
cd ~/ogma
bin/setup        # interactive: token, persona, skills, systemd, chat-ID — all guided
```
The script walks you through everything below and never transmits anything off your machine. The
manual steps are documented here too, in case you prefer to do it by hand.

### Manual setup

**1. Create your own Telegram bot**
- In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy **your** token.
  (This is your private bot — don't share its token; each user makes their own.)

**2. Configure**
```bash
cd ~/ogma
cp .env.example .env
chmod 600 .env
# edit .env: paste TELEGRAM_BOT_TOKEN, leave TELEGRAM_ALLOWED_USERS empty for now
```
Then fill the persona placeholders in `workspace/CLAUDE.md` (`{{OWNER_NAME}}`, `{{OGMA_DIR}}`) and
the hook path in `workspace/.claude/settings.json` (`{{OGMA_DIR}}` → your absolute install dir,
e.g. `/home/youruser/ogma`).

**3. First run (discover your chat ID)**
```bash
python3 gateway.py
```
Message your bot anything. It replies "Not authorized. Your chat ID is `NNNN`". Stop the process
(Ctrl-C), put that number in `TELEGRAM_ALLOWED_USERS=` in `.env`, and start it again. Now it talks
to you.

**4. Run it always-on (systemd user service)**
The units in `systemd/` are templates — replace `{{OGMA_DIR}}` and `{{HOME}}` with your real paths
(`sed -i "s|{{OGMA_DIR}}|$HOME/ogma|g; s|{{HOME}}|$HOME|g" systemd/*`), then:
```bash
mkdir -p ~/.config/systemd/user
cp systemd/ogma-*.service systemd/ogma-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ogma-gateway
loginctl enable-linger "$USER"            # keep it running when you're not logged in
journalctl --user -u ogma-gateway -f      # watch logs
```
Optional routines: `systemctl --user enable --now ogma-briefing.timer ogma-dream.timer ogma-health.timer`.

**5. Install the skills**
The `tickets` skill is what makes the workflow work — install it (and the others) into Claude Code:
```bash
mkdir -p ~/.claude/skills
cp -r ~/ogma/skills/tickets ~/ogma/skills/session-search ~/ogma/skills/daily-briefing ~/.claude/skills/
```
See [`skills/README.md`](skills/README.md) for details and how to write your own.

> Note: these are `--user` services — the unit files must **not** set `User=` (that fails with
> 216/GROUP for a user service).

## Commands
- `/new` — start a fresh Claude session for this chat
- `/help` — usage
- anything else — sent to Claude

## Configuration reference (`.env`)
| Variable | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token (**required**) | — |
| `TELEGRAM_ALLOWED_USERS` | comma-separated allowed chat IDs (**required**) | — |
| `CLAUDE_BIN` | path to the `claude` CLI | `~/.local/bin/claude` |
| `CLAUDE_TIMEOUT` | per-message timeout (seconds) | `300` |
| `OGMA_WORKDIR` | Claude's working dir | `./workspace` |
| `OGMA_PERMISSION_MODE` | e.g. `acceptEdits`; empty = safest | empty |
| `OGMA_ALLOWED_TOOLS` | curated tool allow-list | empty |
| `OGMA_MODEL` | gateway model | Claude Code default |
| `OGMA_DREAM_MODEL` | nightly memory job model | `sonnet` |
| `OGMA_OWNER_NAME` | who the briefing addresses | `you` |
| `OGMA_WEATHER_LOC` | wttr.in location for the briefing | geolocate by IP |
| `OGMA_RSS_FEEDS` | `Label\|url` pairs for the briefing | generic world-news set |

## Self-management (`ogmactl`)
`bin/ogmactl` is the **only** shell command the bot is permitted to run (a fixed whitelist of
subcommands — granting it does *not* grant arbitrary shell): `status`, `logs [N]`, `restart`,
`pihole`, `health`, `ticket <text>`, `tickets`. To let the bot use it, add the scoped rule to
`OGMA_ALLOWED_TOOLS` (see `.env.example`).

## Scheduled routines (optional)
- **`bin/briefing`** — deterministic news (RSS via `bin/news-fetch`) + weather, summarised by
  Claude, delivered via `bin/tg-send`. Configure feeds/location/owner in `.env`. Dry-run:
  `BRIEFING_DRYRUN=1 bin/briefing`.
- **`bin/dream`** — nightly, silent memory consolidation (rolling `yesterday.md` + long-term
  memory tidy). Snapshots memory to `memory-backups/` first.
- **`bin/health-check`** — every ~5 min, alerts to Telegram if CPU temp / load / disk / free RAM
  cross thresholds (all `HEALTH_*`-overridable). Pure shell; degrades cleanly off-Pi.

## What's not included / known limitations
- **Single brain.** The persona, workspace, and memory are shared — adding several chat IDs to the
  allow-list gives them a *shared* assistant and memory, not isolated per-user accounts. True
  multi-user isolation is a future feature.
- Tuned for a small always-on Linux box (developed on a Raspberry Pi); some health/`pihole` bits
  are Pi-flavoured but skip cleanly elsewhere.

## License
[MIT](LICENSE) © 2026 @eric.wien. Provided as-is, without warranty. Inspired by Nous Research's
Hermes Agent (attribution retained above).
