#!/usr/bin/env python3
"""
Stop hook: the "nudge to persist knowledge" loop (Ogma-style self-improvement).

Design note: this hook does NOT block the stop. In headless (`claude -p`) mode the final
result is the last turn's text, so a blocking nudge would overwrite the user's actual answer.
Instead, when a nudge is due, we spawn a *detached background* `claude` pass that reviews the
just-finished conversation and persists durable memory — then we exit 0 so the real answer is
returned to the user untouched. Memory consolidation happens out-of-band.

Why it runs from $HOME (not the gateway workspace): the shared memory lives under the HOME
project (~/.claude/projects/<home>/memory). From the workspace cwd that dir belongs to a
*different* project, so Claude Code flags it "sensitive" and blocks the write even with
acceptEdits. Running as $HOME — exactly what bin/dream does — makes it this project's own
auto-memory, so writes go through. Because we can't --resume the workspace session from here,
we hand the pass the conversation's transcript file (from the hook payload) to review instead.

Announcing: the user asked to be told when a memory is actually written. The pass is otherwise
silent (detached, stdout discarded), so the ONLY channel back is bin/tg-send — the prompt has it
send a one-line Telegram note IF AND ONLY IF it saved/changed a memory.

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
REPO_ROOT = Path(__file__).resolve().parent.parent  # hooks/ sits under the repo root
TG_SEND = str(REPO_ROOT / "bin" / "tg-send")
# Claude Code's per-project memory: ~/.claude/projects/<home-with-slashes-as-dashes>/memory/
_PROJ = str(Path.home()).replace("/", "-")
PROJECTS_DIR = str(Path.home() / ".claude/projects")
MEMORY_DIR = str(Path.home() / ".claude/projects" / _PROJ / "memory") + "/"


def build_nudge(transcript: str) -> str:
    return (
        "Silent maintenance pass (the user is not waiting on this). Review the conversation "
        f"transcript at {transcript} — that JSONL holds the turns that just happened (human/"
        "assistant text in each record's message/content fields) — for anything durable worth "
        "remembering about the user: a stable preference, a fact about them, project state, or "
        "feedback on how to work. If there is something new and lasting (not already in memory, "
        f"not one-off chatter), save it to {MEMORY_DIR} per the memory guidelines (write the file "
        "with frontmatter, then add a one-line pointer to MEMORY.md). Fix or delete any memory "
        "that is now wrong. "
        f"IF AND ONLY IF you actually saved or changed a memory, run `{TG_SEND} \"<note>\"` exactly "
        f"once to tell the user — one short line naming what you remembered, e.g. "
        f"`{TG_SEND} \"\U0001f9e0 Noted: you prefer X\"`. If nothing is worth keeping, do nothing "
        "and send nothing. Produce no other output."
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
        sys.exit(0)  # nothing to review

    # Locate the just-finished conversation transcript. Prefer the path the hook hands us;
    # fall back to deriving it from the gateway's cwd + session id.
    transcript = payload.get("transcript_path")
    if not transcript:
        cwd = payload.get("cwd") or str(REPO_ROOT / "workspace")
        proj = cwd.replace("/", "-")
        transcript = f"{PROJECTS_DIR}/{proj}/{session_id}.jsonl"
    if not Path(transcript).is_file():
        sys.exit(0)  # no transcript to learn from

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

    # Run AS $HOME so the shared home-project memory is THIS project's own auto-memory (writing
    # into another project's .claude data is blocked as "sensitive"). acceptEdits + an explicit
    # tool whitelist (incl. just bin/tg-send for the announce) keep the headless write unblocked.
    home = str(Path.home())
    cmd = [
        CLAUDE_BIN, "-p", build_nudge(transcript),
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read", "Glob", "Grep", "Edit", "Write", f"Bash({TG_SEND}:*)",
        "--add-dir", PROJECTS_DIR,
    ]
    env = {**os.environ, "OGMA_PERSIST_PASS": "1"}
    try:
        subprocess.Popen(
            cmd, cwd=home, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # fully detached; hook returns immediately
        )
    except Exception:  # noqa: BLE001
        pass  # best effort; never disrupt the session

    sys.exit(0)  # never block — the user's answer is returned untouched


if __name__ == "__main__":
    main()
