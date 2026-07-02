#!/usr/bin/env bash
# _env.sh — shared .env reader for Ogma's shell tools. Sourced, never executed.
#
# One parser so a value behaves identically everywhere; gateway.py's load_env()
# follows the same two rules, keep them in sync:
#   - duplicate keys resolve LAST-wins (the dotenv convention)
#   - one pair of surrounding single or double quotes is stripped from the value

# env_get <file> <KEY> — print KEY's value (empty if unset). KEY is a literal
# name (OGMA_*, TELEGRAM_*), not a pattern.
env_get() {
  local val
  val="$(grep -E "^${2}=" "$1" 2>/dev/null | tail -n1 | cut -d= -f2-)"
  # trim surrounding whitespace
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  # strip one pair of matching surrounding quotes
  case "$val" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
  esac
  printf '%s' "$val"
}
