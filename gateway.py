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
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
SESSIONS_FILE = BASE / "sessions.json"

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
        return (f"⚠️ Claude error (exit {proc.returncode}).", session_id)
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
def handle(chat_id: str, text: str, sessions: dict[str, str]) -> None:
    cmd = text.strip().lower()
    if cmd in ("/start", "/help"):
        send(chat_id, "Ogma here. Just talk to me. /new starts a fresh session.")
        return
    if cmd == "/new":
        sessions.pop(chat_id, None)
        save_sessions(sessions)
        send(chat_id, "🧹 Fresh session.")
        return

    # Show "typing…" immediately on receipt, synchronously, so it is guaranteed
    # to reach Telegram before the (blocking) Claude call starts — then the
    # keepalive thread refreshes it (~every 4s) for the rest of the turn.
    typing(chat_id)
    stop = threading.Event()
    typer = threading.Thread(target=keep_typing, args=(chat_id, stop), daemon=True)
    typer.start()
    try:
        reply, sid = ask_claude(text, sessions.get(chat_id))
    finally:
        stop.set()
    if sid and sid != sessions.get(chat_id):
        sessions[chat_id] = sid
        save_sessions(sessions)
    send(chat_id, reply)


def main() -> None:
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
    if not Path(CLAUDE_BIN).exists():
        sys.exit(f"claude binary not found at {CLAUDE_BIN}")
    sessions = load_sessions()
    log(f"Ogma up. workdir={WORKDIR} allowed={ALLOWED or '(none — locked down)'}")
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
            try:
                handle(chat_id, text, sessions)
            except Exception as e:  # noqa: BLE001
                log("handler error:", e)
                send(chat_id, "⚠️ Something broke handling that. Logged it.")


if __name__ == "__main__":
    main()
