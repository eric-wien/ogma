#!/usr/bin/env bash
# Shared provider helpers for unattended assistant jobs. Source after _env.sh.
llm_mode() {
  local value="${OGMA_LLM:-$(env_get "$ENV" OGMA_LLM)}"
  printf '%s' "${value:-claude}"
}
llm_available() {
  local binary
  binary="$(llm_binary)" || return 1
  command -v "$binary" >/dev/null 2>&1
}
llm_binary() {
  local value
  case "$(llm_mode)" in
    claude) value="${CLAUDE_BIN:-$(env_get "$ENV" CLAUDE_BIN)}"; printf '%s' "${value:-claude}" ;;
    codex) value="${CODEX_BIN:-$(env_get "$ENV" CODEX_BIN)}"; printf '%s' "${value:-codex}" ;;
    *) return 1 ;;
  esac
}
llm_default_model() {
  case "$(llm_mode)" in
    claude) printf '%s' 'claude-sonnet-4-6' ;;
    codex) printf '%s' '' ;;
  esac
}
