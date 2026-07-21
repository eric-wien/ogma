#!/usr/bin/env python3
"""OpenAI Codex CLI layer for Ogma.

This mirrors :mod:`llm_claude` without changing it.  Each chat maps to a saved
Codex thread, and turns use the supported non-interactive ``codex exec`` /
``codex exec resume`` interface.  The gateway command runner remains unaware
of provider details.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


def log(*args: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


class CodexLLM:
    """Resumable, headless Codex backend with one thread per chat."""

    def __init__(self, base: Path) -> None:
        def cfg(name: str, default: str = "") -> str:
            return os.environ.get(f"OGMA_{name}", default).strip()

        self.base = base
        self.codex_bin = os.environ.get("CODEX_BIN", str(Path.home() / ".local/bin/codex"))
        self.workdir = cfg("WORKDIR", str(base / "workspace"))
        self.model = cfg("MODEL")
        self.fallback_model = cfg("FALLBACK_MODEL")
        self.effort = cfg("EFFORT").lower()
        self.sandbox = cfg("CODEX_SANDBOX", "read-only").lower()
        self.timeout = int(os.environ.get("CODEX_TIMEOUT", os.environ.get("CLAUDE_TIMEOUT", "300")))
        self.max_concurrent = max(1, int(cfg("MAX_CONCURRENT", "1") or "1"))
        home_project = str(Path.home()).replace("/", "-")
        legacy_shared = Path.home() / ".claude" / "projects" / home_project / "memory"
        self.memory_dir = Path(cfg("MEMORY_DIR", str(legacy_shared))).expanduser()
        self.sessions_file = base / "sessions.codex.json"
        self.env_file = base / ".env"
        self._sessions_lock = threading.Lock()
        self._run_sem = threading.Semaphore(self.max_concurrent)
        self.sessions = self._load_sessions()
        if self.effort and self.effort not in EFFORT_LEVELS:
            log(f"ignoring invalid OGMA_EFFORT={self.effort!r}")
            self.effort = ""
        if self.sandbox not in SANDBOX_MODES:
            log(f"ignoring invalid OGMA_CODEX_SANDBOX={self.sandbox!r}; using read-only")
            self.sandbox = "read-only"

    def _load_sessions(self) -> dict[str, str]:
        try:
            doc = json.loads(self.sessions_file.read_text())
            return doc if isinstance(doc, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_sessions(self) -> None:
        tmp = self.sessions_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.sessions, indent=2) + "\n")
        tmp.replace(self.sessions_file)

    def _set_env_var(self, key: str, value: str) -> None:
        value = value.replace("\r", " ").replace("\n", " ").strip()
        try:
            lines = self.env_file.read_text().splitlines() if self.env_file.exists() else []
        except OSError:
            lines = []
        pat = re.compile(rf"^#?\s*{re.escape(key)}=")
        for index, line in enumerate(lines):
            if pat.match(line):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        try:
            self.env_file.write_text("\n".join(lines) + "\n")
        except OSError as exc:
            log("set_env_var failed:", exc)

    def _common_args(self, model: str | None = None) -> list[str]:
        args = ["--json"]
        selected_model = self.model if model is None else model
        if selected_model:
            args += ["--model", selected_model]
        if self.effort:
            args += ["--config", f'model_reasoning_effort="{self.effort}"']
        # exec resume does not expose --sandbox, but accepts config overrides.
        args += ["--config", f'sandbox_mode="{self.sandbox}"']
        return args

    @staticmethod
    def _parse_events(stdout: str, prior: str | None) -> tuple[str, str | None]:
        thread_id, reply = prior, ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id") or thread_id
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                reply = item.get("text", "").strip() or reply
        return reply or "(no reply)", thread_id

    def ask(self, prompt: str, session_id: str | None,
            model: str | None = None) -> tuple[str, str | None]:
        common = self._common_args(model)
        prefix = [self.codex_bin]
        if self.memory_dir.exists():
            prefix += ["--add-dir", str(self.memory_dir)]
        if session_id:
            cmd = [*prefix, "exec", "resume", *common, session_id, prompt]
        else:
            cmd = [*prefix, "exec", *common, "--cd", self.workdir, prompt]
        try:
            proc = subprocess.run(cmd, cwd=self.workdir, capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return "⏱️ That took too long and timed out. Try a smaller ask?", session_id
        if proc.returncode:
            detail = next((line.strip() for line in proc.stderr.splitlines() if line.strip()), "")
            log("codex exited", proc.returncode, proc.stderr[:500])
            stale = session_id and any(s in proc.stderr.lower() for s in
                                       ("session not found", "no rollout found", "unknown session"))
            if stale:
                log("stale Codex thread id — retrying with a fresh session")
                return self.ask(prompt, None)
            unavailable = any(word in (proc.stderr or "").lower() for word in
                              ("rate limit", "rate_limit", "overloaded", "unavailable", "quota"))
            if unavailable and self.fallback_model and model is None:
                log(f"primary Codex model unavailable — retrying with {self.fallback_model}")
                return self.ask(prompt, session_id, self.fallback_model)
            msg = f"⚠️ Codex error (exit {proc.returncode})."
            if detail:
                msg += f" {detail[:200]}"
            if session_id:
                msg += " (Your session is kept — if this persists, /new starts fresh.)"
            return msg, session_id
        return self._parse_events(proc.stdout, session_id)

    def run_turn(self, chat_id: str, text: str) -> str:
        with self._sessions_lock:
            prior = self.sessions.get(chat_id)
        with self._run_sem:
            reply, session_id = self.ask(text, prior)
        if session_id:
            with self._sessions_lock:
                if session_id != self.sessions.get(chat_id):
                    self.sessions[chat_id] = session_id
                    self._save_sessions()
            self._launch_persist(session_id)
        return reply

    def _launch_persist(self, session_id: str) -> None:
        hook = self.base / "hooks" / "persist-nudge-codex.py"
        if not hook.exists():
            return
        try:
            subprocess.Popen([str(hook), session_id], cwd=self.workdir,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    def command(self, cmd: str, chat_id: str, arg: str) -> str | None:
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
            return (f"Current model: {self.model or '(Codex default)'}\n\n"
                    "Change with /model <model-id>, or /model default to reset.")
        if arg.lower() in ("default", "reset", "none"):
            self.model = ""
            self._set_env_var("OGMA_MODEL", "")
            return "✅ Model reset to the Codex default. Applies to your next message."
        if not MODEL_NAME_RE.match(arg):
            return "⚠️ Invalid model name (letters, digits, . _ : - only, no spaces)."
        self.model = arg
        self._set_env_var("OGMA_MODEL", arg)
        return f"✅ Model set to {arg}. Applies to your next message."

    def handle_effort(self, arg: str) -> str:
        if not arg:
            return (f"Current effort: {self.effort or '(default)'}\n\n"
                    f"Change with /effort <level>: {' | '.join(EFFORT_LEVELS)} | default")
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
                    "Used when Codex reports a rate limit or model outage; /fallback none clears it.")
        if arg.lower() in ("none", "off", "clear", "default"):
            self.fallback_model = ""
            self._set_env_var("OGMA_FALLBACK_MODEL", "")
            return "✅ Fallback model cleared."
        if not MODEL_NAME_RE.match(arg):
            return "⚠️ Invalid model name (letters, digits, . _ : - only, no spaces)."
        self.fallback_model = arg
        self._set_env_var("OGMA_FALLBACK_MODEL", arg)
        return f"✅ Fallback model set to {arg}."
