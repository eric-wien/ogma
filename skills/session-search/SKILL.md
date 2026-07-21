---
name: session-search
description: Search across past conversations/transcripts to recall what was said or decided earlier. Use when the user refers to something from before ("what did we decide about X", "last time", "you mentioned", "remind me what I said about…") and it isn't already in memory.
---

# Session Search

Recall information from earlier conversations that isn't in persistent memory.

## Where to look
- **First check memory** — use the shared directory from the workspace host notes (normally the
  Claude home-project memory). It's faster and curated; only fall through to transcripts if needed.
- Claude transcripts are JSONL under `~/.claude/projects/*/`. Codex transcripts are JSONL under
  `~/.codex/sessions/` (or `$CODEX_HOME/sessions/`). Search both so harness switches are seamless.

## How
1. Use Grep over both transcript roots for the key terms (case-insensitive, `glob: *.jsonl`).
   Start broad, then narrow.
2. For promising hits, Read the surrounding lines of that `.jsonl`. Each line is a JSON record;
   the human/assistant text lives in the `content`/`message` fields and `timestamp` gives the date.
3. Synthesize a short answer with *when* it was discussed. Quote sparingly.
4. If you recover something durable and reusable, persist it to memory so next time is instant.

## Notes
- Don't dump raw transcript JSON at the user — summarize.
- If nothing is found, say so plainly rather than guessing.
