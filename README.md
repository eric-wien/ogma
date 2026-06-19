# Ogma

[![CI](https://github.com/eric-wien/ogma/actions/workflows/ci.yml/badge.svg)](https://github.com/eric-wien/ogma/actions/workflows/ci.yml)

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
manual steps are documented here too, in case you prefer to do it by hand. To re-check an existing
install without changing anything, run `bin/setup --check` — it validates your token, allow-list,
model/effort/fallback, the `claude` CLI, the service, and installed skills.

**Re-running on an existing install.** First run does the full interview. When `.env` already exists,
`bin/setup` instead lets you pick **which sections to revisit** — `env`, `persona`, `model`,
`overlays`, `skills`, `systemd`, `auth` — so a small tweak doesn't walk the whole flow. Pick from
the menu, or go non-interactive: `bin/setup --reconfigure systemd,skills` (or `--all` for the classic
full run). This is the easiest way to **install a newly-added systemd unit after a `git pull`**:
`bin/setup --reconfigure systemd`. A re-run leaves a running gateway untouched, except that if you
changed something it reads at startup (`.env`/persona/model/overlays) it offers to **restart the
gateway so the change takes effect** (decline to apply it later with `ogmactl restart`).

**Updating after a `git pull`.** New/changed *bot commands* need no setup — the gateway re-registers
its slash-command menu with Telegram on every startup, so `ogmactl restart` (or
`systemctl --user restart ogma-gateway`) is enough. Re-run `bin/setup --reconfigure systemd` only when
a pull adds a new systemd **unit** (units are copied into `~/.config/systemd/user` at setup time).

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
`bin/setup` (above) generates the host-local overlays for you — `workspace/CLAUDE.local.md` (the
operator's name, absolute paths, and any persona overrides) and `workspace/.claude/settings.local.json`
(the resolved hook path and permissions), both gitignored and merged at runtime, so the tracked
`workspace/CLAUDE.md` stays generic. If you configure by hand instead, create those two `*.local.*`
files yourself rather than editing the tracked ones.

**Personalising the assistant (optional).** `bin/setup` asks for an assistant **name** (replaces
"Ogma"), a **conversation style** (free text, e.g. "terse and dry"), and a **default language** (e.g.
"German" — reply in it even when written to in English). All optional; blank keeps the defaults. Change
any of them later — or live, over Telegram — with `bin/ogmactl set-persona <name|style|language> <value>`
(then `ogmactl restart`). `set-persona show` prints the current values; `set-persona clear` resets them.

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
- `/model [name]` — show or change the model live (`sonnet`, `haiku`, `opus`, a full id, or `default`); persists to `.env`
- `/effort [level]` — show or change reasoning effort live (`low`/`medium`/`high`/`xhigh`/`max`/`default`); persists to `.env`
- `/fallback [name]` — model used automatically if the main one is unavailable (`none` to clear); persists to `.env`
- `/help` — usage
- anything else — sent to Claude

## Configuration reference (`.env`)
| Variable | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token (**required**) | — |
| `TELEGRAM_ALLOWED_USERS` | comma-separated allowed chat IDs (**required**) | — |
| `CLAUDE_BIN` | path to the `claude` CLI | `~/.local/bin/claude` |
| `CLAUDE_TIMEOUT` | per-message timeout (seconds) | `300` |
| `OGMA_MAX_CONCURRENT` | max concurrent Claude runs across chats (raise only on a roomy host) | `1` |
| `OGMA_WORKDIR` | Claude's working dir | `./workspace` |
| `OGMA_PERMISSION_MODE` | e.g. `acceptEdits`; empty = safest | empty |
| `OGMA_ALLOWED_TOOLS` | curated tool allow-list | empty |
| `OGMA_MODEL` | gateway model (live-changeable via `/model`) | Claude Code default |
| `OGMA_FALLBACK_MODEL` | model used if the primary is unavailable (live via `/fallback`) | none |
| `OGMA_EFFORT` | reasoning effort `low`..`max` (live-changeable via `/effort`) | CLI default |
| `OGMA_DREAM_MODEL` | nightly memory job model (standard-context id avoids the 1M credit gate) | `claude-sonnet-4-6` |
| `OGMA_OWNER_NAME` | who the briefing addresses | `you` |
| `OGMA_WEATHER_LOC` | wttr.in location for the briefing | geolocate by IP |
| `OGMA_RSS_FEEDS` | `Label\|url` pairs for the briefing | generic world-news set |

## Self-management (`ogmactl`)
`bin/ogmactl` is the **only** shell command the bot is permitted to run (a fixed whitelist of
subcommands — granting it does *not* grant arbitrary shell): `status`, `logs [N]`, `restart`,
`health`, `backup`, `ticket <text>`, `tickets`. `bin/setup` pre-approves it (and read access to your memory
directory) in `workspace/.claude/settings.json`, so the bot can self-manage and recall memory over
Telegram without hitting permission prompts — the headless gateway can't show an approval UI. This
grant is read-only by design: no `Write`/`Edit`/arbitrary-`Bash`. (`OGMA_ALLOWED_TOOLS` in `.env`
remains available if you want to widen or narrow the tool set further.)

**Host-specific commands.** To add commands for your own box without forking the tool, drop an
executable `bin/ogmactl.local` (gitignored) — `ogmactl` delegates any subcommand it doesn't
recognise to it. Keep the same discipline as `ogmactl`: whitelist your commands and refuse the
rest (the bot can reach them through `ogmactl`, so keep them read-only and safe). On a stock
install there's no such file and unknown commands are refused as before.

## Scheduled routines (optional)
- **`bin/briefing`** — deterministic news (RSS via `bin/news-fetch`) + weather, summarised by
  Claude, delivered via `bin/tg-send`. Configure feeds/location/owner in `.env`. Dry-run:
  `BRIEFING_DRYRUN=1 bin/briefing`.
- **`bin/dream`** — nightly, silent memory consolidation (rolling `yesterday.md` + long-term
  memory tidy). Snapshots memory to `memory-backups/` first.
- **`bin/health-check`** — every ~5 min, alerts to Telegram if CPU temp / load / disk / free RAM
  cross thresholds (all `HEALTH_*`-overridable). Pure shell; the temp check skips cleanly on hosts
  that don't expose it.
- **`bin/backup`** — nightly archive of your host-local files (see **Backups** below).

## Backups
`bin/backup` archives **all your host-local files** — everything that's gitignored: `.env`
(your token + persona), `state/`, `tickets/`, `memory-backups/`, the presence DB, and any
host-local `bin`/`config` extensions. The manifest *is* the gitignored set (`git ls-files
--ignored`), so it can never drift from `.gitignore`.

- **On demand:** `bin/backup` (or `bin/ogmactl backup`, so the bot can trigger one over Telegram).
- **Scheduled:** `systemctl --user enable --now ogma-backup.timer` (nightly ~03:30, notifies on
  completion). `bin/setup` installs the unit automatically.
- **Where:** archives land **outside** the repo — `~/ogma-backups/` by default (override with
  `--out DIR` or `OGMA_BACKUP_DIR`) — so they survive `git pull`, reinstalls, and uninstall.
  Each is a `chmod 600` `.tar.gz` (it contains `.env`). List them with `bin/backup --list`.
- **Retention:** keeps the newest 14 (`--keep N` or `OGMA_BACKUP_KEEP`; `0` = keep all).

### Restore
`bin/restore` applies a backup back into the checkout:
```bash
bin/restore                 # restore the newest archive in ~/ogma-backups
bin/restore path/to.tar.gz  # restore a specific archive
bin/restore --list          # see what's available
bin/restore --dry-run       # show what would be extracted, change nothing
```
It snapshots the current host-local files first (so a restore is itself reversible;
`--no-backup` to skip) and prompts before overwriting (`-y` to skip). On a **fresh box**:
clone the repo, `bin/restore /path/to/your-archive.tar.gz`, then run `bin/setup` — that
re-renders the host overlays (paths/persona) and reinstalls the systemd units for the new
host. Restore is CLI-only (it overwrites `.env`), so it is *not* exposed to the bot.

## Uninstall
```bash
bin/uninstall
```
It backs up your host-local files first (unless `--no-backup`), stops and removes the
systemd `--user` units and the copied skills, and then **deletes the Ogma directory itself**
(the script `exec`s `rm` so the repo can erase the very script that's running — no npx needed).
System-level units (e.g. `ogma-pihole-watch`) need root, so it prints the `sudo` commands for
you to run rather than touching them. **Never deleted:** your Claude memory directory and the
backup archives. Flags: `-y/--yes`, `--no-backup`, `--backup-dir DIR`, `--keep-skills`.

## What's not included / known limitations
- **Single brain.** The persona, workspace, and memory are shared — adding several chat IDs to the
  allow-list gives them a *shared* assistant and memory, not isolated per-user accounts. True
  multi-user isolation is a future feature.
- Tuned for a small always-on Linux box; the optional health checks read standard Linux metrics and
  skip anything a given host doesn't expose.

## License
[MIT](LICENSE) © 2026 @eric.wien. Provided as-is, without warranty. Inspired by Nous Research's
Hermes Agent (attribution retained above).
