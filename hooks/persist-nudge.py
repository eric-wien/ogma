#!/usr/bin/env python3
"""
Stop hook: the "nudge to persist knowledge" loop (Ogma-style self-improvement).

Design note: this hook does NOT block the stop. In headless (`claude -p`) mode the final
result is the last turn's text, so a blocking nudge would overwrite the user's actual answer.
Instead, when a nudge is due, we spawn a *detached background* `claude` pass that reviews the
just-finished conversation (same session) and persists durable memory — then we exit 0 so the
real answer is returned to the user untouched. Memory consolidation happens out-of-band.

Loop-safe and throttled:
  - the background pass runs with OGMA_PERSIST_PASS=1; the hook no-ops when it sees that,
    so the background pass's own Stop hook can't recurse
  - global throttle: at most one nudge per THROTTLE_SECONDS of activity
Reads the hook payload (JSON) on stdin; never blocks; failures are swallowed.

Install: reference this file's absolute path from the gateway's
workspace/.claude/settings.json Stop hook (see the README).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

THROTTLE_SECONDS = 1800  # at most one memory pass per 30 min of active use
STATE = Path.home() / ".claude/hooks/.persist-nudge.last"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
# Default working dir: this repo's workspace/ (hooks/ sits next to it under the repo root).
DEFAULT_CWD = str(Path(__file__).resolve().parent.parent / "workspace")
# Claude Code's per-project memory: ~/.claude/projects/<home-with-slashes-as-dashes>/memory/
_PROJ = str(Path.home()).replace("/", "-")
MEMORY_DIR = str(Path.home() / ".claude/projects" / _PROJ / "memory") + "/"

NUDGE = (
    "Silent maintenance pass (the user is not waiting on this). Review the conversation that "
    "just happened for anything durable worth remembering about the user — a stable preference, a "
    "fact about them, project state, or feedback on how to work. If there is something new and "
    f"lasting (not already in memory, not one-off chatter), save it to {MEMORY_DIR} per the "
    "memory guidelines (write the file with frontmatter, then add a one-line pointer to "
    "MEMORY.md). Fix or delete any memory that is now wrong. If nothing is worth keeping, do "
    "nothing. Do not produce a user-facing reply."
)


def main() -> None:
    # If THIS is the background maintenance pass, do nothing (prevents recursion).
    if os.environ.get("OGMA_PERSIST_PASS") == "1":
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    session_id = payload.get("session_id")
    if not session_id:
        sys.exit(0)  # nothing to resume

    now = time.time()
    try:
        last = float(STATE.read_text().strip())
    except (FileNotFoundError, ValueError):
        last = 0.0
    if now - last < THROTTLE_SECONDS:
        sys.exit(0)

    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(str(now))
    except OSError:
        pass

    cwd = payload.get("cwd") or DEFAULT_CWD
    cmd = [
        CLAUDE_BIN, "-p", NUDGE, "--resume", session_id,
        "--output-format", "json", "--add-dir", cwd,
    ]
    env = {**os.environ, "OGMA_PERSIST_PASS": "1"}
    try:
        subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # fully detached; hook returns immediately
        )
    except Exception:  # noqa: BLE001
        pass  # best effort; never disrupt the session

    sys.exit(0)  # never block — the user's answer is returned untouched


if __name__ == "__main__":
    main()
