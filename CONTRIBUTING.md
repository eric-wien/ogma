# Contributing to Ogma

Thanks for your interest. Ogma is a small, deliberately minimal project — contributions that keep it
simple, dependency-free, and safe are very welcome.

## Before you start
- Read [`docs/workflow.md`](docs/workflow.md) — it explains the design (two surfaces, one brain,
  bridged by tickets). Most "can we add X?" questions are answered by that model.
- Get it running locally with `bin/setup` (you'll need your own Telegram bot and Claude Code auth).

## Ground rules
- **Stdlib only for the gateway.** `gateway.py` and the Python helpers use the standard library plus
  the `claude` CLI — no third-party packages. Keep it that way; it's a feature.
- **Match the surrounding style.** Small, readable, well-commented where intent isn't obvious.
- **Security first.** This software runs an agent on a box that may hold secrets. Don't widen the
  default tool posture, don't add network listeners, don't introduce anything that could leak `.env`
  or memory. Call out security-relevant changes explicitly in your PR.
- **No secrets in commits.** `.env`, tokens, chat IDs, logs, and memory are gitignored — keep it so.

## Proposing changes
1. Open an issue describing the problem/idea first for anything non-trivial, so we can agree on
   direction before you build.
2. Keep PRs focused and small. Update `README.md` / `docs/` and `CHANGELOG.md` when behaviour or
   config changes.
3. Test what you touch: `python3 -m py_compile` and `bash -n` at minimum; for the briefing,
   `BRIEFING_DRYRUN=1 bin/briefing`.

## Skills & routines
New reusable procedures belong in `skills/` as `SKILL.md` templates (see `skills/README.md`). New
scheduled jobs follow the existing `bin/` + `systemd/` pattern and deliver via `bin/tg-send`.

## Reporting security issues
Please report vulnerabilities privately — see [`SECURITY.md`](SECURITY.md), not public issues.
