# Security Policy

Ogma bridges a chat app to an autonomous agent running on a machine that may hold **SSH keys,
wallets, credentials, and sudo**. Treat your deployment accordingly — most of the risk lives in how
*you* run it, so please read this and the "Security" section of the README before exposing it.

## Operator responsibilities
- **Keep `TELEGRAM_ALLOWED_USERS` tight** — only your own chat ID(s). The allow-list is the trust
  boundary *and* your cost control (anyone you allow can spend your Claude usage).
- **Protect the bot token** — it's a secret. `chmod 600 .env`; never commit it (`.env` is gitignored).
- **Run your own bot.** Each user creates their own via @BotFather; never share a token.
- **Default to the safe tool posture.** Tools that need approval are skipped in headless mode.
  Opting into `OGMA_PERMISSION_MODE` / `OGMA_ALLOWED_TOOLS` (e.g. edits, shell) is a deliberate
  risk you take on. Never use `--dangerously-skip-permissions`.
- The persona's "don't touch secrets" instruction is **guidance to the model, not a sandbox.**

## What ships in this repo
No credentials. No tokens, chat IDs, session data, logs, or memory are included; `.gitignore`
excludes them all. If you ever find a secret committed here, please report it (below).

## Reporting a vulnerability
Please report security issues **privately**, not in public issues:
- Use GitHub's **private vulnerability reporting** (the repo's **Security** tab → *Report a
  vulnerability*).

Include what you found, how to reproduce it, and the impact. You'll get an acknowledgement and,
where applicable, a fix and a credit (if you want one). As a small self-hosted project there's no
formal SLA, but reports are taken seriously and triaged as fast as is practical.

## Supported versions
The latest `0.x` release is the supported line; fixes land there.
