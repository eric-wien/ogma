# Ogma skills

These are **reusable procedures** Claude Code loads on demand — the shared playbooks that both
surfaces of Ogma rely on (the restricted Telegram bot *and* your full interactive sessions). They
pair with the ticket system to make the whole workflow hang together; see `../docs/workflow.md`.

| Skill | What it's for |
|---|---|
| `tickets` | Pick up and resolve tickets the bot filed for things it couldn't do. **The backbone of the workflow.** |
| `session-search` | Recall what was said/decided in earlier conversations when it isn't in memory. |
| `daily-briefing` | Write the morning briefing (template — tune topics/language to taste). |

## Installing
Claude Code discovers skills under `~/.claude/skills/`. Copy (or symlink) the ones you want:

```bash
mkdir -p ~/.claude/skills
cp -r ~/ogma/skills/tickets ~/ogma/skills/session-search ~/ogma/skills/daily-briefing ~/.claude/skills/
# or symlink so repo updates flow through:
#   ln -s ~/ogma/skills/tickets ~/.claude/skills/tickets
```

Each skill is a directory with a `SKILL.md` (YAML frontmatter `name` + `description`, then the
instructions). These are **templates** — edit paths if you didn't install Ogma at `~/ogma`, and
adjust the briefing's topics/language to your own preferences.

## Writing your own
Drop a new `~/.claude/skills/<name>/SKILL.md` with a clear `description` (that's what Claude matches
on to decide when to use it). If a routine is something the *bot* should be able to trigger but
can't (needs file edits, code, shell), have the bot file a ticket instead — that's the workflow.
