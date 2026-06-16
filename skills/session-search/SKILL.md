---
name: session-search
description: Search across past conversations/transcripts to recall what was said or decided earlier. Use when the user refers to something from before ("what did we decide about X", "last time", "you mentioned", "remind me what I said about…") and it isn't already in memory.
---

# Session Search

Recall information from earlier conversations that isn't in persistent memory.

## Where to look
- **First check memory** — Claude Code's per-project memory lives under
  `~/.claude/projects/<project>/memory/` (the `<project>` segment is the working directory with
  slashes replaced by dashes, e.g. `-home-youruser`). It's faster and curated; only fall through
  to transcripts if memory doesn't cover it.
- Transcripts are JSONL, one file per session, under `~/.claude/projects/*/` (every project dir,
  including the Ogma workspace's own project dir).

## How
1. Use Grep over `~/.claude/projects/` for the key terms (case-insensitive, `glob: *.jsonl`).
   Start broad, then narrow.
2. For promising hits, Read the surrounding lines of that `.jsonl`. Each line is a JSON record;
   the human/assistant text lives in the `content`/`message` fields and `timestamp` gives the date.
3. Synthesize a short answer with *when* it was discussed. Quote sparingly.
4. If you recover something durable and reusable, persist it to memory so next time is instant.

## Notes
- Don't dump raw transcript JSON at the user — summarize.
- If nothing is found, say so plainly rather than guessing.
