# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/REPLACE_ME/ogma/releases/tag/v0.1.0
