#!/usr/bin/env python3
"""
Ogma gateway — a secure remote command runner over Telegram, with an optional
Claude Code assistant layer.

One always-on process. Long-polls Telegram (no inbound ports needed, works behind
NAT/Tailscale) and, for each message from an allow-listed chat, either runs a
whitelisted ogmactl command (deterministic, no LLM) or — when the LLM layer is
enabled (OGMA_LLM=claude, the default when the `claude` binary exists) — hands
free text to a resumable headless Claude Code session (see llm_claude.py).
Without the LLM layer this is a pure command runner: Python stdlib only.
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


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = {
    x.strip()
    for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip()
}
CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", str(Path.home() / ".local/bin/claude")
)

_inflight: set = set()                       # chat_ids with a message currently being handled
_inflight_lock = threading.Lock()
DENY_COOLDOWN = 600                          # seconds between replies to a non-allowed chat
_denied: dict[str, float] = {}               # chat_id -> when we last answered its denial

API = f"https://api.telegram.org/bot{TOKEN}"
TG_MAX = 4000  # Telegram hard limit is 4096; leave headroom


def log(*a: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


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
# Telegram API (urllib only)
# ---------------------------------------------------------------------------
def tg(method: str, params: dict, timeout: int = 60) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _tg_len(s: str) -> int:
    """Telegram's 4096 limit counts UTF-16 code units — emoji count double."""
    return len(s.encode("utf-16-le")) // 2


def send(chat_id: str, text: str) -> None:
    # Split long replies on line/space boundaries.
    while text:
        chunk, text = text[:TG_MAX], text[TG_MAX:]
        if text:
            cut = max(chunk.rfind("\n"), chunk.rfind(" "))
            if cut > TG_MAX // 2:
                text, chunk = chunk[cut:] + text, chunk[:cut]
        while _tg_len(chunk) > TG_MAX:
            text, chunk = chunk[-200:] + text, chunk[:-200]
        for attempt in (1, 2):  # one retry — a silently dropped reply looks like a dead bot
            try:
                tg("sendMessage", {"chat_id": chat_id, "text": chunk})
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    log("sendMessage failed (giving up):", e)
                    return
                log("sendMessage failed (retrying):", e)
                time.sleep(2)


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
# Commands that only exist with the LLM layer: sessions, model tuning, and the
# scripts that are themselves claude invocations (briefing/dream/search).
LLM_ONLY_CMDS = ("/new", "/model", "/effort", "/fallback", "/briefing", "/dream", "/search")
NO_LLM_MSG = ("🔒 This Ogma runs in command-only mode — it executes its predefined "
              "commands, there is no AI assistant behind it. /help lists what's available.")


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
    "/search <query> — search past chats"
)
_HELP_CMD_ONLY = (
    "Ogma here — command-only mode (no AI layer). Available commands:\n"
    "\n"
    "Ogma & host:\n"
    "/status   /health   /logs [src] [N]   /restart   /backup\n"
    "/remember <text>   /ticket <text>   /tickets"
)
CORE_HELP = _HELP_LLM if LLM else _HELP_CMD_ONLY


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


# Commands that legitimately run long get their own limit; everything else 60s.
# On expiry subprocess.run KILLS the child, so the message must say so.
CMD_TIMEOUTS: dict[str, int] = {"/backup": 300}


def run_ogmactl(chat_id: str, argv: list[str], timeout: int = 60) -> None:
    """Invoke ogmactl with a whitelisted subcommand + positional args; relay output."""
    typing(chat_id)
    try:
        proc = subprocess.run([OGMACTL, *argv], cwd=str(BASE),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        send(chat_id, f"⏱️ Command killed after {timeout}s — it may have stopped mid-run.")
        return
    send(chat_id, (proc.stdout or proc.stderr or "").strip() or "(no output)")


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


def handle(chat_id: str, text: str) -> None:
    stripped = text.strip()
    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    if "@" in cmd:                      # strip /command@botname (groups)
        cmd = cmd.split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        send(chat_id, build_help())
        return

    # LLM-layer commands: answered by the layer when it's on, refused otherwise.
    if cmd in LLM_ONLY_CMDS and LLM is None:
        send(chat_id, NO_LLM_MSG)
        return
    if LLM is not None:
        reply = LLM.command(cmd, chat_id, arg)   # /new /model /effort /fallback
        if reply is not None:
            send(chat_id, reply)
            return

    # Deterministic commands -> ogmactl (no LLM call; instant and free).
    if cmd in OGMACTL_CMDS:
        if cmd == "/restart":
            send(chat_id, "♻️ Restarting the gateway…")
        run_ogmactl(chat_id, OGMACTL_CMDS[cmd] + arg.split(), CMD_TIMEOUTS.get(cmd, 60))
        return

    # Script-backed commands that deliver their own output, run detached.
    if cmd == "/briefing":
        if script_busy("briefing"):
            send(chat_id, "🗞️ A briefing is already being generated — hang tight.")
            return
        ok = launch_detached("briefing")
        send(chat_id, "🗞️ Putting your briefing together — it'll arrive shortly."
                      if ok else "⚠️ briefing script not found.")
        return
    if cmd == "/dream":
        if script_busy("dream"):
            send(chat_id, "🌙 A consolidation pass is already running.")
            return
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

    # Free text. Command-only mode has nothing to hand it to — say so instead
    # of silently ignoring the message.
    if LLM is None:
        send(chat_id, NO_LLM_MSG)
        return

    # Show "typing…" immediately on receipt, synchronously, so it is guaranteed
    # to reach Telegram before the (blocking) Claude call starts — then the
    # keepalive thread refreshes it (~every 4s) for the rest of the turn.
    typing(chat_id)
    stop = threading.Event()
    typer = threading.Thread(target=keep_typing, args=(chat_id, stop), daemon=True)
    typer.start()
    try:
        reply = LLM.run_turn(chat_id, text)
    finally:
        stop.set()
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


def _worker(chat_id: str, text: str) -> None:
    """Handle one message in its own thread, then release the per-chat slot."""
    try:
        handle(chat_id, text)
    except Exception as e:  # noqa: BLE001
        log("handler error:", e)
        send(chat_id, "⚠️ Something broke handling that. Logged it.")
    finally:
        with _inflight_lock:
            _inflight.discard(chat_id)


def main() -> None:
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
    ok_token, info = validate_token()
    if ok_token:
        log(f"token OK — bot @{info}")
        register_menu()
    else:
        log(f"⚠️ TOKEN CHECK FAILED ({info}). Fix TELEGRAM_BOT_TOKEN in .env and restart.")
    mode = f"llm={LLM_MODE} workdir={LLM.workdir}" if LLM else "command-only"
    log(f"Ogma up. {mode} allowed={ALLOWED or '(none — locked down)'}")
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
                # Reply (and log) at most once per cooldown per chat — anyone who finds
                # the bot can message it, and answering every attempt is free spam
                # amplification plus a log flood. First contact still gets the chat-ID
                # hint, which bin/setup's authorization flow relies on.
                now = time.time()
                if now - _denied.get(chat_id, 0.0) >= DENY_COOLDOWN:
                    if len(_denied) > 1000:  # strangers must not grow this unbounded
                        _denied.clear()
                    _denied[chat_id] = now
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
            threading.Thread(target=_worker, args=(chat_id, text), daemon=True).start()


if __name__ == "__main__":
    main()
