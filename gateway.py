#!/usr/bin/env python3
"""
Ogma gateway — a secure remote command runner over chat, with an optional
Claude Code assistant layer.

One always-on process. Receives messages through a pluggable transport
(transport_telegram.py or transport_matrix.py, selected via OGMA_TRANSPORT;
the surface is small enough that further backends are drop-ins) and, for each
message from an allow-listed sender, either
runs a whitelisted ogmactl command (deterministic, no LLM) or — when the LLM
layer is enabled (OGMA_LLM=claude, the default when the `claude` binary
exists) — hands free text to a resumable headless Claude Code session (see
llm_claude.py). Without the LLM layer this is a pure command runner: Python
stdlib only.

Hardening on the command path: per-command argument validation, /confirm for
destructive commands, an admin/guest role split, and an append-only audit log
(state/audit.log).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE, ignores blanks/#comments). No deps.

    Real environment variables win over the file. Duplicate keys in the file
    resolve last-wins, and one pair of surrounding quotes is stripped — the
    same two rules as bin/_env.sh, keep them in sync.
    """
    if not path.exists():
        return
    vals: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        vals[key.strip()] = val
    for key, val in vals.items():
        os.environ.setdefault(key, val)


load_env(BASE / ".env")


def cfg(name: str, default: str = "") -> str:
    """Read an OGMA_<name> setting from the environment."""
    return os.environ.get(f"OGMA_{name}", default).strip()


def _id_set(var: str) -> set[str]:
    return {x.strip() for x in os.environ.get(var, "").split(",") if x.strip()}


# Chat backend: telegram (default) or matrix. The matrix backend keeps its
# host-local credentials in config/matrix_bot.env.local (default-deny dir +
# .local naming — doubly gitignored), loaded here so .env stays uncluttered.
# Allow-lists are per-backend (<BACKEND>_ALLOWED_USERS): the ids live in
# different namespaces, and a stale list must not carry over on a switch.
TRANSPORT_KIND = (cfg("TRANSPORT", "telegram").lower() or "telegram")
if TRANSPORT_KIND == "matrix":
    load_env(BASE / "config" / "matrix_bot.env.local")
ID_VAR_PREFIX = "MATRIX" if TRANSPORT_KIND == "matrix" else "TELEGRAM"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = _id_set(f"{ID_VAR_PREFIX}_ALLOWED_USERS")   # admins: everything
GUESTS = _id_set(f"{ID_VAR_PREFIX}_GUEST_USERS") - ALLOWED  # read-only command subset
CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", str(Path.home() / ".local/bin/claude")
)

_inflight: set = set()                       # chat_ids with a message currently being handled
_inflight_lock = threading.Lock()
DENY_COOLDOWN = 600                          # seconds between replies to a non-allowed sender
_denied: dict[str, float] = {}               # sender -> when we last answered its denial

# How long a /confirm-protected command stays armed. Human-paced: the prompt has
# to reach a phone and the reply travel back — 60s proved too tight in practice.
CONFIRM_TTL = max(30, int(cfg("CONFIRM_TTL", "180") or "180"))
_pending_confirm: dict[str, tuple[float, str, list[str]]] = {}  # chat -> (expiry, cmd, argv)
_confirm_lock = threading.Lock()

DOC_THRESHOLD = 8000  # command output longer than this is sent as a file, not chunks


def log(*a: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------------------
# Audit log — append-only JSON lines in state/audit.log: who ran what, when,
# with what result. The gateway log is for operating the bot; this file is for
# answering "what did the bot actually execute" later. Best-effort by design:
# auditing must never take the gateway down.
# ---------------------------------------------------------------------------
AUDIT_FILE = BASE / "state" / "audit.log"
_audit_lock = threading.Lock()


def audit(event: str, actor: str, detail: str = "", **extra: object) -> None:
    rec: dict[str, object] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "actor": actor,
    }
    if detail:
        rec["detail"] = detail
    rec.update(extra)
    try:
        AUDIT_FILE.parent.mkdir(exist_ok=True)
        with _audit_lock, open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log("audit write failed:", e)


# ---------------------------------------------------------------------------
# Optional LLM layer. OGMA_LLM=claude (default) enables it when the `claude`
# binary exists; OGMA_LLM=off runs a pure command runner. LLM stays None in
# command-only mode and every LLM feature checks it — llm_claude is only
# imported (and its config only read) when the layer is actually on.
# ---------------------------------------------------------------------------
LLM_MODE = (cfg("LLM", "claude").lower() or "claude")
LLM = None
if LLM_MODE in ("off", "none", "0", "false"):
    log("LLM layer disabled (OGMA_LLM=off) — command-only mode")
elif LLM_MODE == "claude":
    if Path(CLAUDE_BIN).exists():
        from llm_claude import ClaudeLLM
        LLM = ClaudeLLM(BASE)
    else:
        log(f"claude binary not found at {CLAUDE_BIN} — running in command-only "
            "mode (install Claude Code or set CLAUDE_BIN to enable the LLM layer)")
else:
    log(f"unknown OGMA_LLM={LLM_MODE!r} (use 'claude' or 'off') — command-only mode")


# ---------------------------------------------------------------------------
# Transport. All chat-protocol specifics live behind this object (see
# transport_telegram.py for the surface every backend implements). Imported
# lazily so only the selected backend's module is loaded.
# ---------------------------------------------------------------------------
if TRANSPORT_KIND == "matrix":
    from transport_matrix import MatrixTransport
    TRANSPORT = MatrixTransport(
        os.environ.get("MATRIX_HOMESERVER", "").strip(),
        os.environ.get("MATRIX_USER_ID", "").strip(),
        os.environ.get("MATRIX_ACCESS_TOKEN", "").strip(),
        state_dir=BASE / "state",
    )
elif TRANSPORT_KIND == "telegram":
    from transport_telegram import TelegramTransport
    TRANSPORT = TelegramTransport(TOKEN)
else:
    sys.exit(f"unknown OGMA_TRANSPORT={TRANSPORT_KIND!r} (use 'telegram' or 'matrix')")


def send(chat_id: str, text: str) -> None:
    TRANSPORT.send(chat_id, text)


def typing(chat_id: str) -> None:
    TRANSPORT.typing(chat_id)


def keep_typing(chat_id: str, stop: threading.Event) -> None:
    """Re-send the typing indicator every few seconds until told to stop.

    The indicator expires after a few seconds; a long Claude reply would otherwise
    look like the bot died. This keeps it visibly working for the whole turn.
    """
    while not stop.is_set():
        typing(chat_id)
        stop.wait(4)


# ---------------------------------------------------------------------------
# Slash commands — generic CORE (this file, public) merged at runtime with a
# host-LOCAL extension (config/commands.local.json, gitignored). Same core+local
# split as ogmactl/ogmactl.local and .env/.env.example: ship/update the source
# without touching a user's own commands, and keep host specifics out of git.
#
# A command spec:
#   argv     [subcommand]           what to pass to ogmactl (args appended)
#   guest    bool                   guests may run it (default False)
#   confirm  bool                   needs /confirm within 60s (default False)
#   validate [regex, ...] | None    per-positional-arg patterns; None = pass through
#   min_args int                    with validate: how many args are required
#   args     str                    usage hint shown in /help
# ---------------------------------------------------------------------------
OGMACTL = str(BASE / "bin" / "ogmactl")
LOCAL_COMMANDS_FILE = BASE / "config" / "commands.local.json"
_TG_CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")     # Telegram command-name rule
_SUBCMD_RE = re.compile(r"^[a-z0-9-]{1,40}$")     # an ogmactl subcommand token


def _spec(run: str, *, guest: bool = False, confirm: bool = False,
          validate: list[str] | None = None, min_args: int = 0,
          args: str = "") -> dict:
    compiled = None
    if validate is not None:
        compiled = [re.compile(p) for p in validate]
    return {"argv": [run], "guest": guest, "confirm": confirm,
            "validate": compiled, "min_args": min_args, "args": args}


# CORE: only commands that exist in the public ogmactl. Underscore names map to
# ogmactl's hyphenated subcommands. Args are passed positionally (never via a
# shell) and ogmactl refuses anything off its own whitelist — no widening.
# /status and /health are the read-only pair guests get; /restart is the one
# core command that can interrupt service, so it asks for /confirm.
CORE_OGMACTL_CMDS: dict[str, dict] = {
    "/status":   _spec("status", guest=True),
    "/health":   _spec("health", guest=True),
    "/logs":     _spec("logs"),
    "/restart":  _spec("restart", confirm=True),
    "/backup":   _spec("backup"),
    "/remember": _spec("remember"),
    "/ticket":   _spec("ticket"),
    "/tickets":  _spec("tickets"),
}
# Commands that only exist with the LLM layer: sessions, model tuning, and the
# scripts that are themselves claude invocations (briefing/dream/search).
LLM_ONLY_CMDS = ("/new", "/model", "/effort", "/fallback", "/briefing", "/dream", "/search")
NO_LLM_MSG = ("🔒 This Ogma runs in command-only mode — it executes its predefined "
              "commands, there is no AI assistant behind it. /help lists what's available.")
GUEST_MSG = ("🔒 This chat has guest (read-only) access — that isn't available here. "
             "/help shows what you can use.")


def _core_menu() -> list[tuple[str, str]]:
    """The built-in / menu; LLM entries only when the layer is on."""
    llm = LLM is not None
    items: list[tuple[str, str]] = []
    if llm:
        items.append(("new", "Start a fresh session"))
    items.append(("help", "Show commands"))
    if llm:
        items += [("model", "Show or set the model"),
                  ("effort", "Show or set reasoning effort")]
    items += [("status", "Ogma service status"),
              ("health", "Host health snapshot"),
              ("logs", "Recent gateway logs")]
    if llm:
        items += [("briefing", "Generate my briefing now"),
                  ("search", "Search past conversations")]
    items += [("tickets", "List open tickets"),
              ("remember", "Save a memory"),
              ("backup", "Back up host-local files")]
    return items


CORE_MENU_COMMANDS: list[tuple[str, str]] = _core_menu()
_HELP_LLM = (
    "Ogma here — just talk to me, or use a command:\n"
    "\n"
    "Session:\n"
    "/new — fresh session\n"
    "/model [name]   /effort [level]   /fallback [name]\n"
    "\n"
    "Ogma & host:\n"
    "/status   /health   /logs [src] [N]   /restart   /backup\n"
    "/remember <text>   /ticket <text>   /tickets\n"
    "\n"
    "Assistant:\n"
    "/briefing — make my briefing now\n"
    "/search <query> — search past chats\n"
    "\n"
    "Protected commands (e.g. /restart) ask you to /confirm first."
)
_HELP_CMD_ONLY = (
    "Ogma here — command-only mode (no AI layer). Available commands:\n"
    "\n"
    "Ogma & host:\n"
    "/status   /health   /logs [src] [N]   /restart   /backup\n"
    "/remember <text>   /ticket <text>   /tickets\n"
    "\n"
    "Protected commands (e.g. /restart) ask you to /confirm first."
)
CORE_HELP = _HELP_LLM if LLM else _HELP_CMD_ONLY


def load_local_commands() -> list[dict]:
    """Load + validate the host-local slash commands (config/commands.local.json).

    Schema: {"commands": [{"cmd","run","desc","menu"?,"args"?,"guest"?,"confirm"?,
    "validate"?,"min_args"?}, ...]}. A malformed file never crashes the gateway —
    it's logged and the core commands still work.
    """
    if not LOCAL_COMMANDS_FILE.exists():
        return []
    try:
        doc = json.loads(LOCAL_COMMANDS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log("commands.local.json ignored (parse error):", e)
        return []
    out = []
    for c in (doc.get("commands") or []):
        cmd, run = str(c.get("cmd", "")), str(c.get("run", ""))
        if not (_TG_CMD_RE.match(cmd) and _SUBCMD_RE.match(run)):
            log(f"commands.local.json: skipping invalid entry {c!r}")
            continue
        validate = c.get("validate")
        if validate is not None:
            try:
                validate = [str(p) for p in validate]
                [re.compile(p) for p in validate]
            except (TypeError, re.error) as e:
                # A broken pattern must fail closed for the command, not open.
                log(f"commands.local.json: skipping /{cmd} (bad validate): {e}")
                continue
        out.append({"cmd": cmd, "run": run,
                    "desc": str(c.get("desc", run))[:256],
                    "menu": bool(c.get("menu", False)),
                    "args": str(c.get("args", "")),
                    "guest": bool(c.get("guest", False)),
                    "confirm": bool(c.get("confirm", False)),
                    "validate": validate,
                    "min_args": max(0, int(c.get("min_args", 0) or 0))})
    return out


# Merge core + local at import. handle()/menu registration use the merged globals.
LOCAL_COMMANDS = load_local_commands()
OGMACTL_CMDS: dict[str, dict] = dict(CORE_OGMACTL_CMDS)
OGMACTL_CMDS.update({
    f"/{c['cmd']}": _spec(c["run"], guest=c["guest"], confirm=c["confirm"],
                          validate=c["validate"], min_args=c["min_args"],
                          args=c["args"])
    for c in LOCAL_COMMANDS
})
MENU_COMMANDS: list[tuple[str, str]] = list(CORE_MENU_COMMANDS)
MENU_COMMANDS += [(c["cmd"], c["desc"]) for c in LOCAL_COMMANDS if c["menu"]]


def build_help(guest: bool = False) -> str:
    """Core help, plus a 'This host:' section generated from the local commands.

    Guests get only the commands they can actually run.
    """
    if guest:
        core = "You have guest (read-only) access. Commands:\n\n/status   /health"
        locals_ = [c for c in LOCAL_COMMANDS if c["guest"]]
    else:
        core = CORE_HELP
        locals_ = LOCAL_COMMANDS
    if not locals_:
        return core
    lines = "\n".join(
        f"/{c['cmd']}{(' ' + c['args']) if c['args'] else ''} — {c['desc']}"
        for c in locals_
    )
    return f"{core}\n\nThis host:\n{lines}"


def validate_args(cmd: str, spec: dict, args: list[str]) -> str | None:
    """Check args against the spec's per-position patterns. None = OK."""
    pats = spec["validate"]
    if pats is None:
        return None
    usage = f"Usage: {cmd} {spec['args']}".strip()
    if len(args) < spec["min_args"] or len(args) > len(pats):
        return usage
    for a, p in zip(args, pats):
        if not p.fullmatch(a):
            return f"⚠️ '{a}' doesn't look right. {usage}"
    return None


# Commands that legitimately run long get their own limit; everything else 60s.
# On expiry subprocess.run KILLS the child, so the message must say so.
CMD_TIMEOUTS: dict[str, int] = {"/backup": 300}


def run_ogmactl(chat_id: str, actor: str, argv: list[str], timeout: int = 60) -> None:
    """Invoke ogmactl with a whitelisted subcommand + positional args; relay output."""
    typing(chat_id)
    t0 = time.monotonic()
    try:
        proc = subprocess.run([OGMACTL, *argv], cwd=str(BASE),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"[{chat_id}] ogmactl {' '.join(argv)} killed after {timeout}s")
        audit("cmd", actor, " ".join(argv), exit="timeout", secs=timeout)
        send(chat_id, f"⏱️ Command killed after {timeout}s — it may have stopped mid-run.")
        return
    secs = round(time.monotonic() - t0, 1)
    audit("cmd", actor, " ".join(argv), exit=proc.returncode, secs=secs)
    if proc.returncode != 0:
        log(f"[{chat_id}] ogmactl {' '.join(argv)} exited {proc.returncode}")
    out = (proc.stdout or proc.stderr or "").strip() or "(no output)"
    if len(out) > DOC_THRESHOLD:
        # A wall of 4000-char chunks is unreadable and floods the chat; a file
        # scrolls, searches, and forwards properly.
        TRANSPORT.send_document(chat_id, f"{argv[0]}.txt", out,
                                caption=f"{argv[0]} output ({len(out)} chars)")
        return
    send(chat_id, out)


def execute_command(chat_id: str, actor: str, cmd: str, argv: list[str]) -> None:
    """Run a whitelisted command (directly or after /confirm)."""
    if cmd == "/restart":
        send(chat_id, "♻️ Restarting the gateway…")
    run_ogmactl(chat_id, actor, argv, CMD_TIMEOUTS.get(cmd, 60))


def script_busy(script: str) -> bool:
    """True if bin/<script> currently holds its state/<script>.lock flock.

    briefing/dream run detached and each is a full claude invocation — without this
    check a repeated /briefing would stack concurrent runs outside the LLM layer's
    concurrency limit.
    """
    lock = BASE / "state" / f"{script}.lock"
    if not lock.exists():
        return False
    try:
        probe = subprocess.run(["flock", "-n", str(lock), "true"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5)
        return probe.returncode != 0
    except Exception:  # noqa: BLE001
        return False


def launch_detached(script: str) -> bool:
    """Fire-and-forget a bin/ script that delivers its own output (briefing/dream)."""
    path = BASE / "bin" / script
    if not path.exists():
        return False
    subprocess.Popen([str(path)], cwd=str(BASE),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def handle(chat_id: str, actor: str, text: str, guest: bool,
           sent_ts: float = 0.0) -> None:
    stripped = text.strip()
    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    if "@" in cmd:                      # strip /command@botname (groups)
        cmd = cmd.split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        send(chat_id, build_help(guest))
        return

    # Two-step confirmation for protected commands.
    if cmd == "/confirm":
        with _confirm_lock:
            pending = _pending_confirm.pop(chat_id, None)
        if not pending:
            send(chat_id, "Nothing awaiting confirmation.")
            return
        expiry, pcmd, pargv = pending
        # Judge by when the user SENT /confirm (transport's server timestamp),
        # not when it finally reached us — delivery lag on a flaky uplink must
        # not burn the confirmation window.
        if (sent_ts or time.time()) > expiry:
            audit("confirm-expired", actor, pcmd)
            send(chat_id, f"⌛ {pcmd} expired unconfirmed — send it again if you still want it.")
            return
        execute_command(chat_id, actor, pcmd, pargv)
        return
    if cmd == "/cancel":
        with _confirm_lock:
            pending = _pending_confirm.pop(chat_id, None)
        send(chat_id, f"🚫 {pending[1]} cancelled." if pending
             else "Nothing awaiting confirmation.")
        return

    # Guests get the read-only command subset and nothing else.
    if guest:
        spec = OGMACTL_CMDS.get(cmd)
        if spec is None or not spec["guest"]:
            audit("guest-refused", actor, cmd or text[:40])
            send(chat_id, GUEST_MSG)
            return

    # LLM-layer commands: answered by the layer when it's on, refused otherwise.
    if cmd in LLM_ONLY_CMDS and LLM is None:
        send(chat_id, NO_LLM_MSG)
        return
    if LLM is not None and not guest:
        reply = LLM.command(cmd, chat_id, arg)   # /new /model /effort /fallback
        if reply is not None:
            send(chat_id, reply)
            return

    # Deterministic commands -> ogmactl (no LLM call; instant and free).
    if cmd in OGMACTL_CMDS:
        spec = OGMACTL_CMDS[cmd]
        args = arg.split()
        problem = validate_args(cmd, spec, args)
        if problem:
            send(chat_id, problem)
            return
        argv = spec["argv"] + args
        if spec["confirm"]:
            with _confirm_lock:
                _pending_confirm[chat_id] = (time.time() + CONFIRM_TTL, cmd, argv)
            audit("confirm-wait", actor, cmd)
            send(chat_id, f"⚠️ {cmd} is protected — send /confirm within "
                          f"{CONFIRM_TTL}s to run it (or /cancel).")
            return
        execute_command(chat_id, actor, cmd, argv)
        return

    # Script-backed commands that deliver their own output, run detached.
    if cmd == "/briefing":
        if script_busy("briefing"):
            send(chat_id, "🗞️ A briefing is already being generated — hang tight.")
            return
        ok = launch_detached("briefing")
        audit("script", actor, "briefing", started=ok)
        send(chat_id, "🗞️ Putting your briefing together — it'll arrive shortly."
                      if ok else "⚠️ briefing script not found.")
        return
    if cmd == "/dream":
        if script_busy("dream"):
            send(chat_id, "🌙 A consolidation pass is already running.")
            return
        ok = launch_detached("dream")
        audit("script", actor, "dream", started=ok)
        send(chat_id, "🌙 Consolidating memory in the background (no output expected)."
                      if ok else "⚠️ dream script not found.")
        return

    # /search needs the LLM (session-search skill): rewrite the prompt, fall through.
    if cmd == "/search":
        if not arg:
            send(chat_id, "Usage: /search <what to look for>")
            return
        text = ("Use the session-search skill to search our past conversations, "
                f"then tell me what you find about: {arg}")

    # Free text. Command-only mode has nothing to hand it to — say so instead
    # of silently ignoring the message.
    if LLM is None:
        send(chat_id, NO_LLM_MSG)
        return

    # Show "typing…" immediately on receipt, synchronously, so it is guaranteed
    # to reach the chat before the (blocking) Claude call starts — then the
    # keepalive thread refreshes it (~every 4s) for the rest of the turn.
    typing(chat_id)
    stop = threading.Event()
    typer = threading.Thread(target=keep_typing, args=(chat_id, stop), daemon=True)
    typer.start()
    t0 = time.monotonic()
    try:
        reply = LLM.run_turn(chat_id, text)
    finally:
        stop.set()
    audit("llm", actor, text[:80], secs=round(time.monotonic() - t0, 1))
    send(chat_id, reply)


def _worker(chat_id: str, actor: str, text: str, guest: bool, sent_ts: float) -> None:
    """Handle one message in its own thread, then release the per-chat slot."""
    # Log completion + duration: the receipt line alone can't distinguish "still
    # running", "killed mid-flight", and "reply delayed by a Telegram stall".
    t0 = time.monotonic()
    try:
        handle(chat_id, actor, text, guest, sent_ts)
    except Exception as e:  # noqa: BLE001
        log("handler error:", e)
        send(chat_id, "⚠️ Something broke handling that. Logged it.")
    finally:
        log(f"[{chat_id}] done in {time.monotonic() - t0:.1f}s")
        with _inflight_lock:
            _inflight.discard(chat_id)


def main() -> None:
    if TRANSPORT_KIND == "matrix":
        if not os.environ.get("MATRIX_ACCESS_TOKEN", "").strip():
            sys.exit("MATRIX_ACCESS_TOKEN is not set — put the bot credentials in "
                     "config/matrix_bot.env.local or .env (see .env.example).")
    elif not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
    ok_token, info = TRANSPORT.validate()
    if ok_token:
        log(f"token OK — bot @{info}")
        TRANSPORT.register_menu(MENU_COMMANDS)
    else:
        log(f"⚠️ CREDENTIAL CHECK FAILED ({info}). Fix the {TRANSPORT.name} "
            "credentials in .env and restart.")
    mode = f"llm={LLM_MODE} workdir={LLM.workdir}" if LLM else "command-only"
    log(f"Ogma up. transport={TRANSPORT.name} {mode} "
        f"allowed={ALLOWED or '(none — locked down)'}"
        + (f" guests={GUESTS}" if GUESTS else ""))
    for upd in TRANSPORT.updates():
        chat_id, from_id, text = upd["chat_id"], upd["from_id"], upd["text"]
        # Authorize the ACTOR the transport designates — the sender in anything
        # group-shaped, the chat itself in Telegram DMs (see each transport's
        # updates()); otherwise allow-listing a group would hand the command
        # runner to every member.
        actor = upd.get("actor") or chat_id
        if actor in ALLOWED:
            guest = False
        elif actor in GUESTS:
            guest = True
        else:
            # Reply (and log) at most once per cooldown per sender — anyone who
            # finds the bot can message it, and answering every attempt is free
            # spam amplification plus a log flood. First contact still gets the
            # ID hint, which bin/setup's authorization flow relies on.
            now = time.time()
            if now - _denied.get(actor, 0.0) >= DENY_COOLDOWN:
                if len(_denied) > 1000:  # strangers must not grow this unbounded
                    _denied.clear()
                _denied[actor] = now
                log("denied", actor, f"(chat {chat_id})")
                audit("denied", actor, chat=chat_id)
                send(chat_id, f"⛔ Not authorized. Your ID is `{actor}` — add it "
                              f"to {ID_VAR_PREFIX}_ALLOWED_USERS to enable access.")
            continue
        # Surface inbound delivery lag (send time is Telegram's server clock):
        # a message that sat queued behind a dead long-poll looks like a slow
        # bot otherwise. Small offsets are clock noise, only log real lag.
        sent_ts = float(upd.get("date") or 0)
        lag = (time.time() - sent_ts) if sent_ts else 0.0
        log(f"[{chat_id}] {text[:80]}"
            + (f" (sent {lag:.0f}s ago)" if lag > 5 else ""))
        # Concurrency guard: one in-flight message per chat. Drop a second one
        # (with a notice) rather than overlapping runs. Handle in a thread so a
        # long run in one chat doesn't block polling or other chats.
        with _inflight_lock:
            busy = chat_id in _inflight
            if not busy:
                _inflight.add(chat_id)
        if busy:
            send(chat_id, "⏳ Still working on your previous message — give me a moment, "
                          "then resend if needed.")
            continue
        threading.Thread(target=_worker, args=(chat_id, actor, text, guest, sent_ts),
                         daemon=True).start()


if __name__ == "__main__":
    main()
