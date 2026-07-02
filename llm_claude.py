#!/usr/bin/env python3
"""
Claude Code layer for the Ogma gateway — everything LLM-specific lives here.

The gateway imports this module only when the LLM mode is on (OGMA_LLM=claude,
the default when the `claude` binary exists). Ogma's core — the whitelisted
command runner — must keep working without this file's dependencies, so nothing
in gateway.py may require it: the import is guarded, and this module talks back
to the gateway only through returned reply strings, never by sending itself.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# What a model alias/id may look like (/model, /fallback). Anything outside this —
# especially whitespace/newlines — is refused before it reaches .env or the CLI.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def log(*a: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def default_claude_bin() -> str:
    return os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))


class ClaudeLLM:
    """One Claude Code backend: headless `claude -p` runs + per-chat sessions.

    Construct AFTER the gateway has loaded .env — all config is read from the
    environment here, not at import time.
    """

    def __init__(self, base: Path) -> None:
        def cfg(name: str, default: str = "") -> str:
            return os.environ.get(f"OGMA_{name}", default).strip()

        self.base = base
        self.claude_bin = default_claude_bin()
        self.workdir = cfg("WORKDIR", str(base / "workspace"))
        self.permission_mode = cfg("PERMISSION_MODE")   # e.g. acceptEdits
        self.allowed_tools = cfg("ALLOWED_TOOLS")       # e.g. "Read WebSearch"
        self.model = cfg("MODEL")
        self.fallback_model = cfg("FALLBACK_MODEL")  # auto-fallback when primary is unavailable
        self.effort = cfg("EFFORT").lower()          # low|medium|high|xhigh|max ('' = default)
        self.timeout = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
        # Max concurrent Claude runs. Default 1 — small boxes (e.g. a Pi) OOM
        # if several run at once.
        self.max_concurrent = max(1, int(cfg("MAX_CONCURRENT", "1") or "1"))
        self._run_sem = threading.Semaphore(self.max_concurrent)
        self.sessions_file = base / "sessions.json"
        self.env_file = base / ".env"
        self._sessions_lock = threading.Lock()  # guards the sessions dict + file
        self.sessions = self._load_sessions()
        # Don't let a bad hand-edited effort value fail every message — ignore it.
        if self.effort and self.effort not in EFFORT_LEVELS:
            log(f"ignoring invalid OGMA_EFFORT={self.effort!r} "
                f"(use one of {', '.join(EFFORT_LEVELS)})")
            self.effort = ""

    # -----------------------------------------------------------------------
    # Session persistence (chat_id -> claude session_id)
    # -----------------------------------------------------------------------
    def _load_sessions(self) -> dict[str, str]:
        if self.sessions_file.exists():
            try:
                return json.loads(self.sessions_file.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_sessions(self) -> None:
        # Atomic replace — a crash mid-write must not corrupt the file (a corrupt
        # sessions.json silently drops every chat's session on the next start).
        tmp = self.sessions_file.with_name(self.sessions_file.name + ".tmp")
        tmp.write_text(json.dumps(self.sessions, indent=2))
        tmp.replace(self.sessions_file)

    def _set_env_var(self, key: str, value: str) -> None:
        """Persist KEY=value into .env (updating an existing/commented line or appending).

        Lets runtime changes (e.g. /model, /effort) survive a restart. Best-effort.
        """
        # A line break in the value would inject arbitrary .env lines (e.g. CLAUDE_BIN=…),
        # so collapse CR/LF unconditionally — callers validate, this is the backstop.
        value = value.replace("\r", " ").replace("\n", " ").strip()
        try:
            lines = self.env_file.read_text().splitlines() if self.env_file.exists() else []
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
            self.env_file.write_text("\n".join(lines) + "\n")
        except OSError as e:  # noqa: BLE001
            log("set_env_var failed:", e)

    # -----------------------------------------------------------------------
    # Claude headless invocation
    # -----------------------------------------------------------------------
    def ask(self, prompt: str, session_id: str | None) -> tuple[str, str | None]:
        """Run `claude -p`. Returns (reply_text, new_session_id)."""
        cmd = [self.claude_bin, "-p", prompt, "--output-format", "json",
               "--add-dir", self.workdir]
        if session_id:
            cmd += ["--resume", session_id]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            cmd += ["--allowedTools", *self.allowed_tools.split()]
        if self.model:
            cmd += ["--model", self.model]
        if self.fallback_model:
            cmd += ["--fallback-model", self.fallback_model]
        if self.effort:
            cmd += ["--effort", self.effort]
        try:
            proc = subprocess.run(
                cmd, cwd=self.workdir, capture_output=True, text=True,
                timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            return ("⏱️ That took too long and timed out. Try a smaller ask?", session_id)
        if proc.returncode != 0:
            log("claude exited", proc.returncode, proc.stderr[:500])
            # Retry fresh ONLY when the resume itself failed (the CLI says "No conversation
            # found with session ID: …"). A blanket retry would silently drop the chat's
            # context whenever a transient error cleared on the second attempt.
            if session_id and "no conversation found" in (proc.stderr or "").lower():
                log("stale session id — retrying with a fresh session")
                return self.ask(prompt, None)
            # Surface the actual reason (e.g. unknown model / bad flag) instead of a bare code.
            hint = next((ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()), "")
            msg = f"⚠️ Claude error (exit {proc.returncode})."
            if hint:
                msg = f"{msg} {hint[:200]}".rstrip()
            if session_id:
                msg += " (Your session is kept — if this persists, /new starts fresh.)"
            return (msg, session_id)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return (proc.stdout.strip() or "⚠️ Empty response.", session_id)
        if out.get("is_error"):
            return (f"⚠️ {out.get('result', 'error')}", out.get("session_id", session_id))
        return (out.get("result", "").strip() or "(no reply)",
                out.get("session_id", session_id))

    def run_turn(self, chat_id: str, text: str) -> str:
        """One full free-text turn: resume the chat's session, ask, persist the id."""
        with self._sessions_lock:
            prior = self.sessions.get(chat_id)
        with self._run_sem:  # bound concurrent claude runs (RAM safety on small boxes)
            reply, sid = self.ask(text, prior)
        if sid:
            with self._sessions_lock:
                if sid != self.sessions.get(chat_id):
                    self.sessions[chat_id] = sid
                    self._save_sessions()
        return reply

    # -----------------------------------------------------------------------
    # LLM slash commands — each returns the reply text for the gateway to send
    # -----------------------------------------------------------------------
    def command(self, cmd: str, chat_id: str, arg: str) -> str | None:
        """Handle an LLM slash command. Returns the reply, or None if not ours."""
        if cmd == "/new":
            return self.reset(chat_id)
        if cmd == "/model":
            return self.handle_model(arg)
        if cmd == "/effort":
            return self.handle_effort(arg)
        if cmd == "/fallback":
            return self.handle_fallback(arg)
        return None

    def reset(self, chat_id: str) -> str:
        with self._sessions_lock:
            self.sessions.pop(chat_id, None)
            self._save_sessions()
        return "🧹 Fresh session."

    def handle_model(self, arg: str) -> str:
        if not arg:
            return (f"Current model: {self.model or '(Claude Code default)'}\n\n"
                    "Change with /model <name>:\n"
                    "• sonnet — fast, good default on a Pi (claude-sonnet-4-6)\n"
                    "• haiku — fastest / cheapest (claude-haiku-4-5)\n"
                    "• opus — most capable, slower (claude-opus-4-8)\n"
                    "• <full model id> — anything Claude Code accepts\n"
                    "• default — reset to the Claude Code default")
        if arg.lower() in ("default", "reset", "none"):
            self.model = ""
            self._set_env_var("OGMA_MODEL", "")
            return "✅ Model reset to the Claude Code default. Applies to your next message."
        if not MODEL_NAME_RE.match(arg):
            return ("⚠️ That doesn't look like a model name — use an alias or id "
                    "(letters, digits, . _ : - only, no spaces).")
        self.model = arg
        self._set_env_var("OGMA_MODEL", arg)
        return f"✅ Model set to {arg}. Applies to your next message."

    def handle_effort(self, arg: str) -> str:
        if not arg:
            return (f"Current effort: {self.effort or '(default)'}\n\n"
                    "Change with /effort <level>: low | medium | high | xhigh | max | default\n"
                    "Higher = more thorough but slower/pricier; lower = snappier.")
        val = arg.lower()
        if val in ("default", "reset", "none"):
            self.effort = ""
            self._set_env_var("OGMA_EFFORT", "")
            return "✅ Effort reset to default. Applies to your next message."
        if val not in EFFORT_LEVELS:
            return f"⚠️ Unknown effort '{arg}'. Use: {', '.join(EFFORT_LEVELS)} — or default."
        self.effort = val
        self._set_env_var("OGMA_EFFORT", val)
        return f"✅ Effort set to {val}. Applies to your next message."

    def handle_fallback(self, arg: str) -> str:
        if not arg:
            return (f"Current fallback model: {self.fallback_model or '(none)'}\n\n"
                    "Set with /fallback <name> — used automatically if the main model is "
                    "unavailable (rate limit/outage). Accepts an alias or full id; "
                    "/fallback none clears it.")
        if arg.lower() in ("none", "off", "clear", "default"):
            self.fallback_model = ""
            self._set_env_var("OGMA_FALLBACK_MODEL", "")
            return "✅ Fallback model cleared."
        if not MODEL_NAME_RE.match(arg):
            return ("⚠️ That doesn't look like a model name — use an alias or id "
                    "(letters, digits, . _ : - only, no spaces).")
        self.fallback_model = arg
        self._set_env_var("OGMA_FALLBACK_MODEL", arg)
        return f"✅ Fallback model set to {arg}."
