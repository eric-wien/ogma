#!/usr/bin/env python3
"""
Ogma gateway — a minimal Telegram <-> Claude Code bridge.

One always-on process. Long-polls Telegram (no inbound ports needed, works behind
NAT/Tailscale), and for each message from an allow-listed chat it invokes the local
`claude` CLI in headless mode, keeping one resumable Claude session per chat.

Zero third-party dependencies: Python standard library + the `claude` binary only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE, ignores blanks/#comments). No deps."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env(BASE / ".env")


def cfg(name: str, default: str = "") -> str:
    """Read an OGMA_<name> setting from the environment."""
    return os.environ.get(f"OGMA_{name}", default).strip()


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = {
    x.strip()
    for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip()
}
CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", str(Path.home() / ".local/bin/claude")
)
WORKDIR = cfg("WORKDIR", str(BASE / "workspace"))
PERMISSION_MODE = cfg("PERMISSION_MODE")   # e.g. acceptEdits
ALLOWED_TOOLS = cfg("ALLOWED_TOOLS")       # e.g. "Read WebSearch"
MODEL = cfg("MODEL")
FALLBACK_MODEL = cfg("FALLBACK_MODEL")   # auto-fallback when the primary is unavailable
EFFORT = cfg("EFFORT").lower()           # low|medium|high|xhigh|max (empty = CLI default)
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
SESSIONS_FILE = BASE / "sessions.json"
ENV_FILE = BASE / ".env"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# What a model alias/id may look like (/model, /fallback). Anything outside this —
# especially whitespace/newlines — is refused before it reaches .env or the CLI.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
# Max concurrent Claude runs. Default 1 — small boxes (e.g. a Pi) OOM if several run at once.
MAX_CONCURRENT = max(1, int(cfg("MAX_CONCURRENT", "1") or "1"))

_inflight: set = set()                       # chat_ids with a message currently being handled
_inflight_lock = threading.Lock()
_sessions_lock = threading.Lock()            # guards the shared sessions dict + file
_run_sem = threading.Semaphore(MAX_CONCURRENT)  # bounds concurrent `claude` invocations

API = f"https://api.telegram.org/bot{TOKEN}"
TG_MAX = 4000  # Telegram hard limit is 4096; leave headroom


def log(*a: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------------------
# Session persistence (chat_id -> claude session_id)
# ---------------------------------------------------------------------------
def load_sessions() -> dict[str, str]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_sessions(s: dict[str, str]) -> None:
    SESSIONS_FILE.write_text(json.dumps(s, indent=2))


def set_env_var(key: str, value: str) -> None:
    """Persist KEY=value into .env (updating an existing/commented line or appending).

    Lets runtime changes (e.g. /model, /effort) survive a restart. Best-effort.
    """
    # A line break in the value would inject arbitrary .env lines (e.g. CLAUDE_BIN=…),
    # so collapse CR/LF unconditionally — callers validate, this is the backstop.
    value = value.replace("\r", " ").replace("\n", " ").strip()
    try:
        lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    except OSError:
        lines = []
    pat = re.compile(rf"^#?\s*{re.escape(key)}=")
    repl, found = f"{key}={value}", False
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i], found = repl, True
            break
    if not found:
        lines.append(repl)
    try:
        ENV_FILE.write_text("\n".join(lines) + "\n")
    except OSError as e:  # noqa: BLE001
        log("set_env_var failed:", e)


# ---------------------------------------------------------------------------
# Telegram API (urllib only)
# ---------------------------------------------------------------------------
def tg(method: str, params: dict, timeout: int = 60) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def send(chat_id: str, text: str) -> None:
    # Split long replies on line/space boundaries.
    while text:
        chunk, text = text[:TG_MAX], text[TG_MAX:]
        if text:
            cut = max(chunk.rfind("\n"), chunk.rfind(" "))
            if cut > TG_MAX // 2:
                text, chunk = chunk[cut:] + text, chunk[:cut]
        try:
            tg("sendMessage", {"chat_id": chat_id, "text": chunk})
        except Exception as e:  # noqa: BLE001
            log("sendMessage failed:", e)
            return


def typing(chat_id: str) -> None:
    try:
        tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:  # noqa: BLE001
        pass


def keep_typing(chat_id: str, stop: threading.Event) -> None:
    """Re-send the typing indicator every few seconds until told to stop.

    Telegram's typing action expires after ~5s; a long Claude reply would otherwise
    look like the bot died. This keeps it visibly working for the whole turn.
    """
    while not stop.is_set():
        typing(chat_id)
        stop.wait(4)


# ---------------------------------------------------------------------------
# Claude headless invocation
# ---------------------------------------------------------------------------
def ask_claude(prompt: str, session_id: str | None) -> tuple[str, str | None]:
    """Run `claude -p`. Returns (reply_text, new_session_id)."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--add-dir", WORKDIR]
    if session_id:
        cmd += ["--resume", session_id]
    if PERMISSION_MODE:
        cmd += ["--permission-mode", PERMISSION_MODE]
    if ALLOWED_TOOLS:
        cmd += ["--allowedTools", *ALLOWED_TOOLS.split()]
    if MODEL:
        cmd += ["--model", MODEL]
    if FALLBACK_MODEL:
        cmd += ["--fallback-model", FALLBACK_MODEL]
    if EFFORT:
        cmd += ["--effort", EFFORT]
    try:
        proc = subprocess.run(
            cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return ("⏱️ That took too long and timed out. Try a smaller ask?", session_id)
    if proc.returncode != 0:
        log("claude exited", proc.returncode, proc.stderr[:500])
        # A bad/expired session id is the common cause — retry once fresh.
        if session_id:
            return ask_claude(prompt, None)
        # Surface the actual reason (e.g. unknown model / bad flag) instead of a bare code.
        hint = next((ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()), "")
        msg = f"⚠️ Claude error (exit {proc.returncode})."
        return (f"{msg} {hint[:200]}".rstrip() if hint else msg, session_id)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return (proc.stdout.strip() or "⚠️ Empty response.", session_id)
    if out.get("is_error"):
        return (f"⚠️ {out.get('result', 'error')}", out.get("session_id", session_id))
    return (out.get("result", "").strip() or "(no reply)", out.get("session_id", session_id))


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Slash commands — generic CORE (this file, public) merged at runtime with a
# host-LOCAL extension (config/commands.local.json, gitignored). Same core+local
# split as ogmactl/ogmactl.local and .env/.env.example: ship/update the source
# without touching a user's own commands, and keep host specifics out of git.
# ---------------------------------------------------------------------------
OGMACTL = str(BASE / "bin" / "ogmactl")
LOCAL_COMMANDS_FILE = BASE / "config" / "commands.local.json"
_TG_CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")     # Telegram command-name rule
_SUBCMD_RE = re.compile(r"^[a-z0-9-]{1,40}$")     # an ogmactl subcommand token

# CORE: only commands that exist in the public ogmactl. Underscore names map to
# ogmactl's hyphenated subcommands. Args are passed positionally (never via a
# shell) and ogmactl refuses anything off its own whitelist — no widening.
CORE_OGMACTL_CMDS: dict[str, list[str]] = {
    "/status": ["status"], "/health": ["health"], "/logs": ["logs"],
    "/restart": ["restart"], "/backup": ["backup"], "/remember": ["remember"],
    "/ticket": ["ticket"], "/tickets": ["tickets"],
}
CORE_MENU_COMMANDS: list[tuple[str, str]] = [
    ("new", "Start a fresh session"),
    ("help", "Show commands"),
    ("model", "Show or set the model"),
    ("effort", "Show or set reasoning effort"),
    ("status", "Ogma service status"),
    ("health", "Host health snapshot"),
    ("logs", "Recent gateway logs"),
    ("briefing", "Generate my briefing now"),
    ("search", "Search past conversations"),
    ("tickets", "List open tickets"),
    ("remember", "Save a memory"),
    ("backup", "Back up host-local files"),
]
CORE_HELP = (
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
    "/search <query> — search past chats"
)


def load_local_commands() -> list[dict]:
    """Load + validate the host-local slash commands (config/commands.local.json).

    Schema: {"commands": [{"cmd","run","desc","menu"?,"args"?}, ...]}. A malformed
    file never crashes the gateway — it's logged and the core commands still work.
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
        out.append({"cmd": cmd, "run": run,
                    "desc": str(c.get("desc", run))[:256],
                    "menu": bool(c.get("menu", False)),
                    "args": str(c.get("args", ""))})
    return out


# Merge core + local at import. handle()/register_menu() use the merged globals.
LOCAL_COMMANDS = load_local_commands()
OGMACTL_CMDS: dict[str, list[str]] = dict(CORE_OGMACTL_CMDS)
OGMACTL_CMDS.update({f"/{c['cmd']}": [c["run"]] for c in LOCAL_COMMANDS})
MENU_COMMANDS: list[tuple[str, str]] = list(CORE_MENU_COMMANDS)
MENU_COMMANDS += [(c["cmd"], c["desc"]) for c in LOCAL_COMMANDS if c["menu"]]


def build_help() -> str:
    """Core help, plus a 'This host:' section generated from the local commands."""
    if not LOCAL_COMMANDS:
        return CORE_HELP
    lines = "\n".join(
        f"/{c['cmd']}{(' ' + c['args']) if c['args'] else ''} — {c['desc']}"
        for c in LOCAL_COMMANDS
    )
    return f"{CORE_HELP}\n\nThis host:\n{lines}"


def run_ogmactl(chat_id: str, argv: list[str]) -> None:
    """Invoke ogmactl with a whitelisted subcommand + positional args; relay output."""
    typing(chat_id)
    try:
        proc = subprocess.run([OGMACTL, *argv], cwd=str(BASE),
                              capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        send(chat_id, "⏱️ Command timed out.")
        return
    send(chat_id, (proc.stdout or proc.stderr or "").strip() or "(no output)")


def launch_detached(script: str) -> bool:
    """Fire-and-forget a bin/ script that delivers its own output (briefing/dream)."""
    path = BASE / "bin" / script
    if not path.exists():
        return False
    subprocess.Popen([str(path)], cwd=str(BASE),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def register_menu() -> None:
    """Register the slash-command menu with Telegram (server-side; idempotent).

    A chat resolves its command list most-specific-scope-first, so we register at
    BOTH `default` (groups / fallback) AND `all_private_chats`. The latter is
    required: any commands previously set at the private-chats scope would otherwise
    shadow the default-scope list and the menu would show a stale set in DMs.
    """
    payload = json.dumps([{"command": c, "description": d} for c, d in MENU_COMMANDS])
    for scope in (None, {"type": "all_private_chats"}):
        params = {"commands": payload}
        if scope:
            params["scope"] = json.dumps(scope)
        label = scope["type"] if scope else "default"
        try:
            r = tg("setMyCommands", params, timeout=15)
            log(f"menu registered ({label})" if r.get("ok")
                else f"menu register failed ({label}): {r.get('description')}")
        except Exception as e:  # noqa: BLE001
            log(f"setMyCommands error ({label}):", e)


def handle_model(chat_id: str, arg: str) -> None:
    global MODEL
    if not arg:
        send(chat_id,
             f"Current model: {MODEL or '(Claude Code default)'}\n\n"
             "Change with /model <name>:\n"
             "• sonnet — fast, good default on a Pi (claude-sonnet-4-6)\n"
             "• haiku — fastest / cheapest (claude-haiku-4-5)\n"
             "• opus — most capable, slower (claude-opus-4-8)\n"
             "• <full model id> — anything Claude Code accepts\n"
             "• default — reset to the Claude Code default")
        return
    if arg.lower() in ("default", "reset", "none"):
        MODEL = ""
        set_env_var("OGMA_MODEL", "")
        send(chat_id, "✅ Model reset to the Claude Code default. Applies to your next message.")
        return
    if not MODEL_NAME_RE.match(arg):
        send(chat_id, "⚠️ That doesn't look like a model name — use an alias or id "
                      "(letters, digits, . _ : - only, no spaces).")
        return
    MODEL = arg
    set_env_var("OGMA_MODEL", arg)
    send(chat_id, f"✅ Model set to {arg}. Applies to your next message.")


def handle_effort(chat_id: str, arg: str) -> None:
    global EFFORT
    if not arg:
        send(chat_id,
             f"Current effort: {EFFORT or '(default)'}\n\n"
             "Change with /effort <level>: low | medium | high | xhigh | max | default\n"
             "Higher = more thorough but slower/pricier; lower = snappier.")
        return
    val = arg.lower()
    if val in ("default", "reset", "none"):
        EFFORT = ""
        set_env_var("OGMA_EFFORT", "")
        send(chat_id, "✅ Effort reset to default. Applies to your next message.")
        return
    if val not in EFFORT_LEVELS:
        send(chat_id, f"⚠️ Unknown effort '{arg}'. Use: {', '.join(EFFORT_LEVELS)} — or default.")
        return
    EFFORT = val
    set_env_var("OGMA_EFFORT", val)
    send(chat_id, f"✅ Effort set to {val}. Applies to your next message.")


def handle_fallback(chat_id: str, arg: str) -> None:
    global FALLBACK_MODEL
    if not arg:
        send(chat_id,
             f"Current fallback model: {FALLBACK_MODEL or '(none)'}\n\n"
             "Set with /fallback <name> — used automatically if the main model is unavailable "
             "(rate limit/outage). Accepts an alias or full id; /fallback none clears it.")
        return
    if arg.lower() in ("none", "off", "clear", "default"):
        FALLBACK_MODEL = ""
        set_env_var("OGMA_FALLBACK_MODEL", "")
        send(chat_id, "✅ Fallback model cleared.")
        return
    if not MODEL_NAME_RE.match(arg):
        send(chat_id, "⚠️ That doesn't look like a model name — use an alias or id "
                      "(letters, digits, . _ : - only, no spaces).")
        return
    FALLBACK_MODEL = arg
    set_env_var("OGMA_FALLBACK_MODEL", arg)
    send(chat_id, f"✅ Fallback model set to {arg}.")


def handle(chat_id: str, text: str, sessions: dict[str, str]) -> None:
    stripped = text.strip()
    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    if "@" in cmd:                      # strip /command@botname (groups)
        cmd = cmd.split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        send(chat_id, build_help())
        return
    if cmd == "/new":
        with _sessions_lock:
            sessions.pop(chat_id, None)
            save_sessions(sessions)
        send(chat_id, "🧹 Fresh session.")
        return
    if cmd == "/model":
        handle_model(chat_id, arg)
        return
    if cmd == "/effort":
        handle_effort(chat_id, arg)
        return
    if cmd == "/fallback":
        handle_fallback(chat_id, arg)
        return

    # Deterministic commands -> ogmactl (no LLM call; instant and free).
    if cmd in OGMACTL_CMDS:
        if cmd == "/restart":
            send(chat_id, "♻️ Restarting the gateway…")
        run_ogmactl(chat_id, OGMACTL_CMDS[cmd] + arg.split())
        return

    # Script-backed commands that deliver their own output, run detached.
    if cmd == "/briefing":
        ok = launch_detached("briefing")
        send(chat_id, "🗞️ Putting your briefing together — it'll arrive shortly."
                      if ok else "⚠️ briefing script not found.")
        return
    if cmd == "/dream":
        ok = launch_detached("dream")
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

    # Show "typing…" immediately on receipt, synchronously, so it is guaranteed
    # to reach Telegram before the (blocking) Claude call starts — then the
    # keepalive thread refreshes it (~every 4s) for the rest of the turn.
    typing(chat_id)
    stop = threading.Event()
    typer = threading.Thread(target=keep_typing, args=(chat_id, stop), daemon=True)
    typer.start()
    try:
        with _sessions_lock:
            prior = sessions.get(chat_id)
        with _run_sem:  # bound concurrent claude runs (RAM safety on small boxes)
            reply, sid = ask_claude(text, prior)
    finally:
        stop.set()
    if sid:
        with _sessions_lock:
            if sid != sessions.get(chat_id):
                sessions[chat_id] = sid
                save_sessions(sessions)
    send(chat_id, reply)


def validate_token() -> tuple[bool, str]:
    """Check the bot token via getMe so a bad token is an obvious one-line log,
    not an endless stream of 401s from getUpdates."""
    try:
        r = tg("getMe", {}, timeout=15)
    except Exception as e:  # noqa: BLE001
        return (False, str(e))
    if r.get("ok"):
        return (True, (r.get("result") or {}).get("username", "?"))
    return (False, r.get("description", "not ok"))


def _worker(chat_id: str, text: str, sessions: dict[str, str]) -> None:
    """Handle one message in its own thread, then release the per-chat slot."""
    try:
        handle(chat_id, text, sessions)
    except Exception as e:  # noqa: BLE001
        log("handler error:", e)
        send(chat_id, "⚠️ Something broke handling that. Logged it.")
    finally:
        with _inflight_lock:
            _inflight.discard(chat_id)


def main() -> None:
    global EFFORT
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
    if not Path(CLAUDE_BIN).exists():
        sys.exit(f"claude binary not found at {CLAUDE_BIN}")
    # Don't let a bad hand-edited effort value fail every message — ignore it.
    if EFFORT and EFFORT not in EFFORT_LEVELS:
        log(f"ignoring invalid OGMA_EFFORT={EFFORT!r} (use one of {', '.join(EFFORT_LEVELS)})")
        EFFORT = ""
    ok_token, info = validate_token()
    if ok_token:
        log(f"token OK — bot @{info}")
        register_menu()
    else:
        log(f"⚠️ TOKEN CHECK FAILED ({info}). Fix TELEGRAM_BOT_TOKEN in .env and restart.")
    sessions = load_sessions()
    log(f"Ogma up. workdir={WORKDIR} allowed={ALLOWED or '(none — locked down)'} "
        f"max_concurrent={MAX_CONCURRENT}")
    offset = 0
    while True:
        try:
            resp = tg("getUpdates", {"offset": offset, "timeout": 50}, timeout=70)
        except Exception as e:  # noqa: BLE001
            log("getUpdates error:", e)
            time.sleep(5)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg or "text" not in msg:
                continue
            chat_id = str(msg["chat"]["id"])
            text = msg["text"]
            if chat_id not in ALLOWED:
                log("denied chat", chat_id)
                send(chat_id, f"⛔ Not authorized. Your chat ID is `{chat_id}` — "
                              "add it to TELEGRAM_ALLOWED_USERS to enable access.")
                continue
            log(f"[{chat_id}] {text[:80]}")
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
            threading.Thread(target=_worker, args=(chat_id, text, sessions), daemon=True).start()


if __name__ == "__main__":
    main()
