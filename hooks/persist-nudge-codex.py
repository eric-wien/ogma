#!/usr/bin/env python3
"""Best-effort, detached Codex memory pass after a successful gateway turn."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

THROTTLE_SECONDS = 1800
BASE = Path(__file__).resolve().parent.parent
STATE = BASE / "state" / "persist-nudge-codex.last"


def env_value(key: str) -> str:
    value = ""
    try:
        for raw in (BASE / ".env").read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, candidate = line.split("=", 1)
            if name.strip() == key:
                value = candidate.strip().strip("\"'")
    except OSError:
        pass
    return os.environ.get(key, value).strip()


def find_transcript(session_id: str) -> Path | None:
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    if not root.is_dir():
        return None
    for path in sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            if session_id in path.read_text(errors="replace")[:65536]:
                return path
        except OSError:
            continue
    return None


def conversation_text(path: Path) -> str:
    chunks: list[str] = []
    try:
        lines = path.read_text(errors="replace").splitlines()[-800:]
    except OSError:
        return ""
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload") or record
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role", "unknown")
            content = payload.get("content", [])
            text = " ".join(x.get("text", "") for x in content if isinstance(x, dict))
            if text:
                chunks.append(f"{role}: {text}")
        elif kind in ("user_message", "agent_message") and payload.get("message"):
            chunks.append(f"{kind}: {payload['message']}")
    return "\n\n".join(chunks)[-120000:]


def main() -> int:
    if len(sys.argv) != 2:
        return 0
    now = time.time()
    try:
        if now - float(STATE.read_text()) < THROTTLE_SECONDS:
            return 0
    except (OSError, ValueError):
        pass
    transcript = find_transcript(sys.argv[1])
    text = conversation_text(transcript) if transcript else ""
    if not text:
        return 0
    try:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(str(now))
    except OSError:
        pass

    home_project = str(Path.home()).replace("/", "-")
    default_memory = Path.home() / ".claude" / "projects" / home_project / "memory"
    memory = Path(env_value("OGMA_MEMORY_DIR") or default_memory).expanduser()
    memory.mkdir(parents=True, exist_ok=True)
    codex = env_value("CODEX_BIN") or str(Path.home() / ".local" / "bin" / "codex")
    notify = BASE / "bin" / "tg-send"
    prompt = (
        "Silent memory maintenance. The conversation is supplied on stdin. Save only new, "
        "durable, explicitly supported facts or preferences in this directory. Keep MEMORY.md "
        "as a lean index and use one focused Markdown file per memory. Correct stale memory when "
        "the conversation clearly contradicts it. Do not modify anything outside this directory. "
        f"If and only if memory changed, run {notify} once with a short '🧠 Noted: …' message. "
        "Return no conversational output."
    )
    cmd = [codex, "exec", "--sandbox", "workspace-write", "--skip-git-repo-check",
           "--cd", str(memory), prompt]
    try:
        subprocess.run(cmd, input=text, text=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
