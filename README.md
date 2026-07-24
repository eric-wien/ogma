# Ogma

[![CI](https://github.com/eric-wien/ogma/actions/workflows/ci.yml/badge.svg)](https://github.com/eric-wien/ogma/actions/workflows/ci.yml)

Ogma is a self-hosted remote command runner with an optional personal-assistant layer. It connects
your Linux machine to **Telegram or Matrix**, accepts commands only from an allow-list, and can send
free-text conversations to **Claude Code or OpenAI Codex**.

It uses one standard-library Python process, opens no inbound port, and reuses the selected
assistant CLI's existing login.

## Choose your setup

Transport and assistant harness are independent:

| Choice | Options | What it changes |
|---|---|---|
| Transport | `telegram`, `matrix` | Where messages are received and sent |
| Assistant | `claude`, `codex`, `off` | Who handles free text; commands work in every mode |

All six combinations are supported. With `OGMA_LLM=off`, Ogma remains a useful allow-listed command
runner and has no model-provider dependency.

## Quick start

```bash
cd ~/ogma
bin/setup
```

Setup creates or updates `.env`, configures the assistant, model, persona, skills, and systemd
units, and offers to restart a running gateway when necessary.

Useful reconfiguration commands:

```bash
bin/setup --check
bin/setup --reconfigure llm
bin/setup --reconfigure model,persona
bin/setup --reconfigure systemd,skills
bin/setup --all
```

Switching between Claude and Codex automatically opens model configuration and installs missing
Ogma skills for the selected harness. Existing customized skills and provider-specific chat
sessions are preserved.

### Telegram

Create a private bot with Telegram's `@BotFather`, then configure:

```dotenv
OGMA_TRANSPORT=telegram
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_ALLOWED_USERS=your-numeric-chat-id
```

If you do not know your chat ID, leave the allow-list empty, run `python3 gateway.py`, and message
the bot. The denial response includes the ID to authorize.

### Matrix

Use a bot account on a trusted homeserver and an **unencrypted room**; Ogma does not implement
Matrix E2EE. Select the transport in `.env`:

```dotenv
OGMA_TRANSPORT=matrix
```

Put the credentials in the gitignored `config/matrix_bot.env.local` (or directly in `.env`):

```dotenv
MATRIX_HOMESERVER=https://matrix.example.org
MATRIX_USER_ID=@ogma:matrix.example.org
MATRIX_ACCESS_TOKEN=your-access-token
MATRIX_ALLOWED_USERS=@you:matrix.example.org
MATRIX_NOTIFY_ROOM=!room-id:matrix.example.org
```

The local Matrix file takes precedence over `.env`. The bot auto-joins invitations only from
allowed or guest users.

### Assistant harness

```dotenv
# Claude Code
OGMA_LLM=claude

# OpenAI Codex
OGMA_LLM=codex

# Commands only
OGMA_LLM=off
```

Claude requires an authenticated `claude` CLI; Codex requires an authenticated `codex` CLI.
Leaving `OGMA_MODEL` blank follows the selected CLI's default model.

## Run Ogma

For a foreground test:

```bash
python3 gateway.py
```

For the installed user service:

```bash
systemctl --user enable --now ogma-gateway
systemctl --user status ogma-gateway
journalctl --user -u ogma-gateway -f
```

Use `loginctl enable-linger "$USER"` if the service should continue after logout.

## Commands

Always available:

- `/help`
- `/status`, `/health`, `/logs`
- `/backup`, `/remember`
- `/ticket`, `/tickets`
- `/restart`, protected by `/confirm` or `/cancel`
- host commands defined in `bin/ogmactl.local` and `config/commands.local.json`

With Claude or Codex enabled:

- `/new` starts a fresh provider-specific chat session.
- `/model`, `/effort`, and `/fallback` show or change runtime model settings.
- `/briefing`, `/dream`, and `/search` run assistant routines.
- Other text is sent to the selected assistant harness.

Every executed command is appended to `state/audit.log`.

## Security

Ogma bridges a chat account to a machine that may contain sensitive data. At minimum:

- Authorize only trusted IDs in `TELEGRAM_ALLOWED_USERS` or `MATRIX_ALLOWED_USERS`.
- Keep `.env` and Matrix credentials private (`chmod 600`).
- Use guest lists for limited access: `TELEGRAM_GUEST_USERS` or `MATRIX_GUEST_USERS`.
- Keep destructive commands behind confirmation and validate their arguments.
- Keep Claude permissions and the Codex sandbox restrictive. Persona instructions are not a
  security boundary.
- Never use Claude's `--dangerously-skip-permissions` or Codex
  `danger-full-access` for a messenger-facing assistant without understanding the risk.

The allow-list is also cost control: an authorized user can consume your model allowance.

## Main configuration

See [.env.example](.env.example) for the complete, commented reference.

| Variable | Purpose | Default |
|---|---|---|
| `OGMA_TRANSPORT` | `telegram` or `matrix` | `telegram` |
| `*_ALLOWED_USERS` | comma-separated admin IDs for the selected transport | none |
| `*_GUEST_USERS` | limited/read-only user IDs | none |
| `OGMA_LLM` | `claude`, `codex`, or `off` | `claude` |
| `CLAUDE_BIN`, `CODEX_BIN` | assistant CLI paths | CLI on `PATH` / user-local path |
| `OGMA_MODEL` | selected assistant model; blank follows CLI default | blank |
| `OGMA_EFFORT` | reasoning effort supported by the selected CLI | CLI default |
| `OGMA_FALLBACK_MODEL` | retry model for provider outages or rate limits | none |
| `OGMA_CODEX_SANDBOX` | `read-only`, `workspace-write`, or `danger-full-access` | `read-only` |
| `OGMA_MEMORY_DIR` | memory shared between harnesses | generated by setup |
| `OGMA_CONFIRM_TTL` | confirmation window in seconds | `180` |
| `OGMA_OWNER_NAME`, `OGMA_WEATHER_LOC`, `OGMA_RSS_FEEDS` | briefing preferences | see example |

Model aliases and supported effort levels differ between Claude and Codex. Use
`bin/setup --reconfigure model` after changing harnesses; selecting the `llm` section now does this
automatically.

## Extend Ogma

`bin/ogmactl` is the only executable the gateway invokes. Add host-specific commands without
editing the gateway:

1. Implement a strict subcommand in the gitignored `bin/ogmactl.local`.
2. Describe its arguments, validation, guest access, and confirmation requirement in
   `config/commands.local.json`.
3. Restart the gateway to refresh the command menu.

See [docs/extending.md](docs/extending.md) for the tutorial.

## Skills and memory

The bundled skills are:

- `tickets` — resolve work deferred by the restricted messenger assistant.
- `session-search` — search earlier Claude and Codex conversations.
- `daily-briefing` — build a weather and RSS morning briefing.

Setup installs them under `~/.claude/skills/` or `~/.codex/skills/` as appropriate. Memory and the
persona are shared across harnesses, while Claude and Codex session IDs remain separate.

See [docs/workflow.md](docs/workflow.md) for the restricted-bot/full-session workflow and
[skills/README.md](skills/README.md) for skill details.

## Scheduled jobs

Setup can install these systemd user timers:

| Job | Purpose |
|---|---|
| `ogma-briefing.timer` | RSS and weather briefing summarized by the selected assistant |
| `ogma-dream.timer` | nightly memory consolidation with pre-run snapshots |
| `ogma-health.timer` | machine health alerts |
| `ogma-backup.timer` | archive host-local configuration and state |

Enable any installed timer with:

```bash
systemctl --user enable --now ogma-briefing.timer
```

## Backup and restore

```bash
bin/backup
bin/backup --list
bin/restore --list
bin/restore path/to/archive.tar.gz
bin/restore --dry-run
```

Backups default to `~/ogma-backups/`, outside the checkout. They include private host-local files,
so archives are created with mode `600`. Restore snapshots the current state before overwriting it.

## Update and uninstall

After `git pull`, restart the gateway. Re-run `bin/setup --reconfigure systemd` only when new unit
files were added.

```bash
bin/uninstall
```

Uninstall backs up local data, removes its user units and managed files, and deletes the checkout.
Backup archives and the external memory directory are preserved. Use `--help` to see uninstall
options.

## Limitations

- All authorized users share one persona, workspace, and memory.
- Matrix rooms must be unencrypted.
- Health checks target Linux and skip unavailable metrics.

## License

[MIT](LICENSE) © 2026 @eric.wien. Inspired by
[Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent).
