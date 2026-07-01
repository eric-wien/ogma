#!/usr/bin/env bash
# _persona.sh — shared helpers for Ogma persona overrides (name / style / language).
#
# Sourced by bin/setup and bin/ogmactl; not meant to be run directly. Persona values
# are stored in .env (OGMA_PERSONA_NAME / _STYLE / _LANG) as the source of truth, then
# rendered into a delimited block in workspace/CLAUDE.local.md — the gitignored overlay
# Claude Code imports via `@CLAUDE.local.md`. Storing in .env lets `bin/setup` re-runs
# preserve the values; rendering into CLAUDE.local.md is what actually reaches the model.
#
# The effect is purely instructional: the name replaces "Ogma", the style and language
# are appended as guidance. No translation step, no locale framework — the model handles
# cross-language output fine from a one-line instruction. Same core(tracked) + local
# (gitignored) split as ogmactl/ogmactl.local and .env/.env.example.

PERSONA_BEGIN='<!-- BEGIN persona-overrides (managed by bin/setup / ogmactl set-persona) -->'
PERSONA_END='<!-- END persona-overrides -->'

# persona_cfg <env_file> <suffix>  — echo OGMA_PERSONA_<suffix> from .env (empty if unset).
persona_cfg() {
  grep -E "^OGMA_PERSONA_${2}=" "$1" 2>/dev/null | head -n1 | cut -d= -f2-
}

# persona_set_env <env_file> <KEY> <value>  — update existing (even commented-out) line, or append.
# Pure bash/awk so bin/ogmactl keeps no new dependency. Value may contain spaces/slashes.
persona_set_env() {
  local env="$1" key="$2" val="$3" tmp
  # Persona values are single-line by design (persona_render emits one bullet each);
  # a line break here would inject arbitrary .env lines, so collapse CR/LF.
  val="${val//$'\r'/ }"; val="${val//$'\n'/ }"
  tmp="$(mktemp)" || return 1
  KEY="$key" VAL="$val" awk '
    BEGIN { key=ENVIRON["KEY"]; val=ENVIRON["VAL"]; done=0 }
    !done && $0 ~ "^#?[[:space:]]*" key "=" { print key "=" val; done=1; next }
    { print }
    END { if (!done) print key "=" val }
  ' "$env" > "$tmp" && mv "$tmp" "$env"
}

# persona_render <env_file> <claude_local_md>
# Rebuild the delimited persona-override block in CLAUDE.local.md from the .env values.
# Removes the block entirely when no override is set. Idempotent.
persona_render() {
  local env="$1" md="$2" name style lang block="" body
  name="$(persona_cfg "$env" NAME)"
  style="$(persona_cfg "$env" STYLE)"
  lang="$(persona_cfg "$env" LANG)"

  if [ -n "$name$style$lang" ]; then
    block="$PERSONA_BEGIN"$'\n'"## Persona overrides"$'\n'
    [ -n "$name" ]  && block+="- Your name is **$name** — use it in place of \"Ogma\" everywhere, including how you refer to yourself."$'\n'
    [ -n "$style" ] && block+="- Conversation style: $style"$'\n'
    [ -n "$lang" ]  && block+="- Default language: $lang — reply in $lang even when the user writes in another language, unless they ask you to switch."$'\n'
    block+="$PERSONA_END"
  fi

  # Strip any existing block (markers inclusive). Command substitution also trims trailing
  # newlines, so we re-add exactly one separating blank line before the fresh block.
  body="$(awk -v b="$PERSONA_BEGIN" -v e="$PERSONA_END" '
    index($0,b){skip=1; next}
    index($0,e){skip=0; next}
    !skip{print}
  ' "$md")"
  {
    printf '%s\n' "$body"
    [ -n "$block" ] && printf '\n%s\n' "$block"
  } > "$md"
}
