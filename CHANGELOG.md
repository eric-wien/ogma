# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [1.2.0] — 2026-06-19

### Added
- **Persona overrides — name, conversation style, and default language.** Each Ogma instance can now
  be personalised without touching the tracked persona. Three optional fields live in `.env`
  (`OGMA_PERSONA_NAME` / `_STYLE` / `_LANG`) and are rendered into a managed block in the gitignored
  `workspace/CLAUDE.local.md` overlay: a **name** that replaces "Ogma" everywhere, a free-text
  **conversation style** (e.g. "terse and dry", "more formal"), and a **default language** that the
  assistant replies in even when written to in another (falling back to the user's language when
  unset). `bin/setup` now prompts for all three, and a new whitelisted `ogmactl set-persona
  <name|style|language|show|clear> [value]` lets the bot (or operator) change them live — handy for
  running Ogma on several machines, each with its own identity. Changes apply on the next
  `ogmactl restart`. A shared `bin/_persona.sh` renders the block for both setup and `ogmactl`, so
  they stay in lockstep, and the tracked `CLAUDE.md` now defers to the host notes for name/language.
  i18n is intentionally instruction-only (no per-locale string files or pre-translated context
  variants) — the model handles cross-language output from the one-line directive.
- **Backups of host-local files.** New `bin/backup` archives everything host-specific — i.e. the
  gitignored set (`.env`, `state/`, `tickets/`, `memory-backups/`, the presence DB, and any
  host-local `bin`/`config` extensions) — into a timestamped, `chmod 600` `.tar.gz`. The manifest is
  derived from `git ls-files --ignored`, so it can never drift from `.gitignore`. Archives land
  **outside** the repo (`~/ogma-backups/` by default; `--out`/`OGMA_BACKUP_DIR`) so they survive
  `git pull`, reinstalls, and uninstall, with retention to the newest 14 (`--keep`/`OGMA_BACKUP_KEEP`).
  Runnable on demand, via the bot (new whitelisted `ogmactl backup`), or nightly through the new
  `ogma-backup.timer` (installed by `bin/setup`, notifies on completion). `--list` shows existing
  archives. Archive filenames are now collision-guarded so two backups in the same second never
  overwrite each other.
- **Restore (`bin/restore`).** Applies a backup back into the checkout — newest archive by default,
  or a specific file/`--from DIR`; `--list` and `--dry-run` to inspect first. Snapshots the current
  host-local files before overwriting (so a restore is itself reversible; `--no-backup` to skip) and
  prompts unless `-y`. Intended for recovery and for standing a fresh clone up on a new box (restore,
  then `bin/setup` to re-render host overlays + reinstall units). CLI-only — not exposed to the bot,
  since it overwrites `.env`.
- **Selective `bin/setup` re-runs.** First run still does the full interview, but re-running on an
  existing install now lets you pick which sections to revisit — `env`, `persona`, `model`,
  `overlays`, `skills`, `systemd`, `auth` — via a menu, or non-interactively with
  `bin/setup --reconfigure systemd,skills` (`--all` keeps the classic full run). Sections read what
  they need from `.env`, so revisiting one (e.g. regenerating overlays) doesn't require re-entering
  others. Makes installing a newly-added systemd unit after a `git pull` a one-liner
  (`bin/setup --reconfigure systemd`); the executable-permissions step always runs so pulled-in
  scripts become runnable. (New/changed *bot commands* need no setup at all — the gateway
  re-registers its Telegram menu on every startup; just restart it.)
- **Uninstall path (`bin/uninstall`).** Backs up host-local files first (unless `--no-backup`), stops
  and removes the systemd `--user` units and the copied skills, then **deletes the Ogma directory
  itself** — the script `exec`s `rm`, so the repo can erase the very script that's running (no npx
  needed). System-level units (e.g. `ogma-pihole-watch`) are left to a printed `sudo` command rather
  than touched. Your Claude memory directory and the backup archives are never deleted. Guards refuse
  to run against `$HOME`, `/`, or anything that isn't an Ogma checkout. Flags: `-y/--yes`,
  `--no-backup`, `--backup-dir`, `--keep-skills`.

## [1.1.2] — 2026-06-18

### Fixed
- **Slash-command menu now appears in private chats.** The gateway registered its `/` commands only at
  Telegram's `default` command scope, but a chat resolves its menu most-specific-scope-first — so an
  older set of commands left at the `all_private_chats` scope shadowed the full list in DMs, and the
  in-app menu showed a stale two-command set. `register_menu()` now registers at both the `default`
  and `all_private_chats` scopes, so the complete menu shows up in direct chats. (The commands always
  worked when typed; only the menu was affected.)

## [1.1.1] — 2026-06-18

### Changed
- **Install no longer dirties tracked files — host specifics moved to gitignored overlays.** `bin/setup`
  previously substituted `{{OWNER_NAME}}`/`{{OGMA_DIR}}`/`{{MEMORY_DIR}}` directly into the tracked
  `workspace/CLAUDE.md` and `workspace/.claude/settings.json`, so every configured instance showed
  those files as permanently modified and could not cleanly `git pull`. Setup now **generates two
  gitignored overlays** instead: `workspace/CLAUDE.local.md` (host notes — operator name, absolute
  paths — imported by the persona via `@CLAUDE.local.md`) and `workspace/.claude/settings.local.json`
  (the resolved `ogmactl` permission rule, memory `additionalDirectories`, and the persist-nudge Stop
  hook), both merged by Claude Code at runtime. The tracked `CLAUDE.md` is now generic/placeholder-free
  and `settings.json` is a minimal skeleton, so a configured instance has a clean `git status`. Same
  core(tracked) + local(gitignored) pattern as `ogmactl`/`ogmactl.local` and `.env`/`.env.example`.

## [1.1.0] — 2026-06-18

### Added
- **Native Telegram slash commands, with a core + host-local split.** The gateway now answers a set
  of `/` commands directly — `/status`, `/health`, `/logs`, `/restart`, `/remember`, `/ticket`,
  `/tickets`, plus `/briefing`, `/search`, and the existing session/model controls — by shelling
  straight to `ogmactl` (or launching the briefing/search flows). These run with **no `claude -p`
  invocation**, so they are instant and incur no model cost (previously even "status" spun up a full
  LLM turn). The set is registered with Telegram via `setMyCommands` at startup, so the commands show
  up in the in-app "/" menu. Operators add their OWN host commands in a gitignored
  `config/commands.local.json` (template: `config/commands.local.example.json`) — a declarative
  `{cmd, run, desc, menu, args}` map merged with the built-in commands at runtime, with `/help`
  generated to include them. Same core(tracked)+local(gitignored) pattern as `ogmactl`/`ogmactl.local`,
  so the source stays updatable without touching your own commands. A malformed local file is logged
  and ignored (never crashes the gateway), and `ogmactl`'s own whitelist still gates every command —
  so this adds no new reach for the bot.

## [1.0.3] — 2026-06-17

### Fixed
- **Telegram bot no longer hits permission walls on `ogmactl` and memory reads.** The gateway runs
  `claude -p` in default permission mode, where headless tool calls can't be approved interactively —
  so `ogmactl` (a Bash call) and reads of the memory directory (outside the workspace) were silently
  denied, with no UI to approve them. `workspace/.claude/settings.json` now ships a `permissions`
  block that pre-approves `Bash({{OGMA_DIR}}/bin/ogmactl:*)` and adds the canonical memory directory
  to `additionalDirectories`; `bin/setup` fills the new `{{MEMORY_DIR}}` placeholder (same `$HOME`
  derivation the dream/briefing use). Read-only by design — no `Write`/`Edit`/arbitrary-`Bash` is
  granted, so the bot's whitelist boundary is unchanged.

## [1.0.2] — 2026-06-17

### Added
- **Host-local `ogmactl` extensions.** `ogmactl` now delegates any subcommand it doesn't recognise
  to an optional, executable `bin/ogmactl.local` (gitignored), letting operators add host-specific
  commands without forking the host-agnostic tool. On a stock install there's no such file and
  unknown commands are refused exactly as before, so the bot's whitelist boundary is unchanged.
  Documented in the README; `bin/ogmactl.local` is gitignored.

## [1.0.1] — 2026-06-17

### Fixed
- **Nightly dream / briefing no longer trip the 1M-context credit gate.** The headless jobs
  (`bin/dream`, `bin/briefing`) defaulted to the bare `sonnet` alias, which in headless runs can
  resolve to the credit-gated 1M-context model variant and fail with *"Usage credits required for
  1M context"* — silently skipping a nightly memory consolidation. They now default to the explicit
  standard-context id `claude-sonnet-4-6` (matching what `bin/setup` already writes for the gateway),
  which forces the standard 200k window. Override via `OGMA_MODEL` / `OGMA_DREAM_MODEL` as before.

## [1.0.0] — 2026-06-16

First stable release. The public surface — the `OGMA_*` settings, the `/new` · `/model` · `/effort` ·
`/fallback` commands, the skills + ticket workflow, and the systemd layout — is considered stable
going forward; breaking changes will bump the major version.

### Added
- **Continuous integration** — a GitHub Actions workflow runs `py_compile` (Python) and `bash -n`
  (shell) on every push to `main` and every pull request, with a build badge in the README. No
  secrets; never touches a deployment.

Consolidates the whole 0.x line: the long-poll gateway, guided installer (`bin/setup`, plus
`--check`), self-management (`ogmactl`), scheduled routines (briefing/dream/health), skill templates,
the ticket workflow, model/effort/fallback controls, the per-chat concurrency guard, and startup
token validation.

## [0.5.0] — 2026-06-16

### Added
- **Concurrency guard.** Messages are now handled in per-chat worker threads with **one in-flight
  message per chat** (a second is dropped with a notice rather than overlapping), plus a global
  semaphore that caps concurrent `claude` runs via **`OGMA_MAX_CONCURRENT`** (default 1 — protects
  RAM on small boxes like a Pi). A long run in one chat no longer blocks polling or other chats.
- **`bin/setup --check`** — a non-interactive doctor that validates the token (via getMe), allow-list,
  model/effort/fallback, the `claude` CLI, the systemd service, and installed skills. Changes nothing.

### Changed
- The gateway validates the bot token at startup — one clear `token OK` / `TOKEN CHECK FAILED` log
  line — instead of emitting a stream of 401s from getUpdates when the token is wrong.

## [0.4.0] — 2026-06-16

### Added
- **Fallback model.** `OGMA_FALLBACK_MODEL` + the `/fallback [name]` command: a model used
  automatically when the primary is unavailable (rate limit/outage). Prompted in `bin/setup`; the
  gateway passes `--fallback-model` to the `claude` CLI.

### Changed
- **Hardening & clearer errors.**
  - On a `claude` CLI failure the bot surfaces the actual stderr reason (e.g. unknown model / bad
    flag) instead of a bare exit code.
  - An invalid hand-edited `OGMA_EFFORT` is ignored at startup (and logged) rather than failing every
    message.
  - `bin/setup` prints a config summary at the end and warns if the token, allowed chats, or the
    `claude` CLI are missing.

## [0.3.0] — 2026-06-16

### Added
- **Model & reasoning-effort selection.**
  - `bin/setup` now prompts for a model (curated aliases `sonnet`/`haiku`/`opus`, `default`, or any
    full model id) and a reasoning-effort level — with a best-effort live model list when
    `ANTHROPIC_API_KEY` is set (Claude Code subscription auth can't list models, so it falls back to
    the curated choices).
  - New chat commands **`/model [name]`** and **`/effort [level]`** change them live and persist to
    `.env` (mirrors Claude Code's built-in `/model`).
  - New **`OGMA_EFFORT`** setting; the gateway passes `--effort` to the `claude` CLI alongside
    `--model`.

## [0.2.1] — 2026-06-16

### Fixed
- `news-fetch` now reads `OGMA_RSS_FEEDS` from `.env` as a fallback when it isn't in the environment,
  so configured feeds take effect whether it's run by `bin/briefing`, by systemd, or directly.
  Previously the `.env` setting silently had no effect unless the variable was exported by hand.

## [0.2.0] — 2026-06-16

### Changed
- Hardened `bin/setup`'s chat-ID authorization from best-effort to reliable:
  - reads the bot token from `.env` (fixes re-runs that kept an existing token and silently skipped).
  - validates the token up front, with a clear message on `401 Unauthorized`.
  - pauses a running `ogma-gateway` during detection so it can't swallow your message.
  - polls `getUpdates` for ~45s (non-destructive) instead of a single immediate check.
  - handles `409 Conflict` (another poller) and offers retry / manual entry / skip instead of
    failing quietly.

## [0.1.1] — 2026-06-16

### Changed
- Removed Raspberry-Pi-specific functionality for a cleaner, host-agnostic tool:
  - Dropped the `pihole` subcommand from `ogmactl`.
  - `ogmactl health` no longer shells out to `vcgencmd` (Pi firmware tool) — CPU temp now reads the
    standard Linux thermal sysfs, and the Pi-only "throttle" line is gone.
  - De-Pi'd wording across docs/comments and switched `news-fetch` to a neutral User-Agent.
- `bin/health-check` is unchanged in behaviour: its temp check reads the standard Linux thermal
  sysfs and still skips cleanly on hosts that don't expose it.

## [0.1.0] — 2026-06-16

First public release. A minimal, self-hosted bridge from Telegram to Claude Code.

### Added
- **Gateway** (`gateway.py`) — zero-dependency Python (stdlib + the `claude` CLI). Long-polls
  Telegram (no inbound ports), one resumable Claude session per chat, allow-list auth, persistent
  "typing" indicator, `/new` and `/help` commands.
- **Persona** (`workspace/CLAUDE.md`) — the assistant's character, with `{{OWNER_NAME}}` /
  `{{OGMA_DIR}}` placeholders filled by setup.
- **Guided installer** (`bin/setup`) — interactive: prerequisites, `.env`, your own bot token,
  persona/hook placeholders, skill install, systemd units, and automatic chat-ID discovery.
- **Self-management** (`bin/ogmactl`) — the single whitelisted helper the bot may run: `status`,
  `logs`, `restart`, `pihole`, `health`, `ticket`, `tickets`.
- **Scheduled routines** — `bin/briefing` (deterministic RSS + weather, summarised by Claude),
  `bin/dream` (nightly memory consolidation), `bin/health-check` (host alerts to Telegram),
  `bin/tg-send` (delivery), `bin/news-fetch` (configurable RSS digest).
- **Memory-persist hook** (`hooks/persist-nudge.py`) — out-of-band Stop hook that saves durable
  facts without delaying replies.
- **Skill templates** (`skills/`) — `tickets`, `session-search`, `daily-briefing`.
- **systemd templates** (`systemd/`) — user services/timers for always-on operation.
- **Docs** — `README.md`, `docs/workflow.md` (the two-surface + ticket design), `SECURITY.md`,
  `CONTRIBUTING.md`.

### Notes
- Self-hosted only: each user runs their own Telegram bot and their own Claude Code auth. No hosted
  service; no credentials ship with the repo.
- Single shared brain — multiple allow-listed chats share one persona/workspace/memory. Per-user
  isolation is planned (see issues).

[1.0.3]: https://github.com/eric-wien/ogma/releases/tag/v1.0.3
[1.0.2]: https://github.com/eric-wien/ogma/releases/tag/v1.0.2
[1.0.1]: https://github.com/eric-wien/ogma/releases/tag/v1.0.1
[1.0.0]: https://github.com/eric-wien/ogma/releases/tag/v1.0.0
[0.5.0]: https://github.com/eric-wien/ogma/releases/tag/v0.5.0
[0.4.0]: https://github.com/eric-wien/ogma/releases/tag/v0.4.0
[0.3.0]: https://github.com/eric-wien/ogma/releases/tag/v0.3.0
[0.2.1]: https://github.com/eric-wien/ogma/releases/tag/v0.2.1
[0.2.0]: https://github.com/eric-wien/ogma/releases/tag/v0.2.0
[0.1.1]: https://github.com/eric-wien/ogma/releases/tag/v0.1.1
[0.1.0]: https://github.com/eric-wien/ogma/releases/tag/v0.1.0
