# Ogma roadmap — from "Claude bridge" to "secure remote command runner"

*Drafted 2026-07-02. Direction: the core product is secure execution of a
predefined set of commands on a remote machine through a chat interface.
The Claude Code integration becomes an optional layer on top — a nice
conversation partner that can perform tasks and analyze, but not a
prerequisite. Anyone should be able to run Ogma commands-only and code all
functionality themselves.*

The architecture is already most of the way there: the
`gateway → whitelisted ogmactl subcommands → commands.local.json` chain **is**
the secure command runner — deterministic, shell-free, double-gated, and
LLM-free. What has to change is the framing: today the code treats Claude as
the foundation (`gateway.py` refuses to start without the `claude` binary, and
every unrecognized message falls through to `ask_claude()`). The phases below
invert that.

## Phase 1 — Invert the dependency (make Claude optional)

Make the gateway boot and run in **command-only mode** when Claude is absent
or disabled.

- `OGMA_LLM=claude|off` setting. Default: `claude` when the binary exists, so
  existing installs change nothing. A missing binary becomes a log line +
  command-only mode instead of `sys.exit`.
- Extract everything Claude-specific into one module (`llm_claude.py`):
  `ask_claude()`, sessions.json handling, `/new`, `/model`, `/effort`,
  `/fallback`, the `/search` prompt rewrite. The gateway only uses it when the
  mode is on. This seam is what makes "optional" real.
- Free-text fallback in command-only mode: reply with help + "this instance
  runs commands only". Menu and `/help` are built conditionally so a
  command-only bot doesn't advertise `/model`.
- `bin/setup` offers two paths: "with Claude Code" vs. "commands only".
- README repositioned around the command runner; Claude becomes the optional
  assistant layer.

## Phase 2 — Harden the command runner into the actual product

Features the LLM used to paper over, now first-class:

- **Confirmation for destructive commands** — `confirm: true` in
  commands.local.json requires a second step (reply/inline keyboard) before
  running. `/restart` or a future reboot command shouldn't fire on a typo.
- **Argument validation per command** — `args` is currently only a help hint;
  add optional per-arg regex/enum so validation happens in the gateway, not in
  every ogmactl.local case.
- **Audit log** — append-only `state/audit.log`: timestamp, chat_id, command,
  args, exit code. Cheap, and it's what lets us say "secure" with a straight
  face.
- **Roles** — admin vs. read-only chat IDs, per-command `role` field. Matters
  the moment a second person uses an instance.
- **Output handling** — send long output as a document instead of many
  4000-char chunks.
- Keep the two-layer design (JSON declares, ogmactl gates) — it is the
  security story. Document "write your own ogmactl.local subcommand" as *the*
  extension mechanism (tutorial in docs/): that's the answer for people who
  want to code everything themselves.

## Phase 3 — Transport abstraction (the Signal on-ramp)

Pull Telegram specifics (`tg()`, `send()`, typing, the `getUpdates` loop,
menu registration) behind a small transport interface: `poll() → messages`,
`send(chat, text)`, capability flags for typing/menus/files. Then a
`signal-cli` backend is a second implementation of a small interface instead
of a rewrite. Cut the seam first; build the Signal backend later.

## Phase 4 — Reclassify the assistant features

`briefing`, `dream`, `search`, persona, and the memory-persist hook belong to
the optional LLM layer. The twofold system and ticketing stay. End state,
three tiers:

1. **Core** — gateway + command runner. Python stdlib only, no other
   dependencies.
2. **LLM layer** — Claude Code, sessions, search.
3. **Assistant layer** — briefing, dream, memory, persona.

## Order rationale

Phase 1 before 2 because the module seam determines where Phase 2 features
live. Phase 2 before 3 because hardening delivers user value now; Signal is a
horizon goal. Phase 3 before writing the Signal backend so the migration is
an implementation, not a refactor. Nothing breaks running instances:
command-only mode is opt-in-by-absence.

## Security note (pre-existing)

The gateway authorizes on `chat.id`. Fine for private chats, but if a group
chat is ever allow-listed, every member of that group can run commands.
Check `from.id` as well before the "secure" claim goes into the README
(candidate for Phase 2).
