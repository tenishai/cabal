#!/usr/bin/env bash
# cabal#142 — register this Claude Code session on cabal's bulletin board so
# `cbl status` can answer "what is actually running, and what is it doing".
#
# Presence only. The row is deliberately not dispatchable: cabal gives it a
# `session://` endpoint and an empty capability map, so nothing can route work
# here (see core/bulletin/store.py::register_session).
#
# Install into ~/.claude/settings.json (or a project .claude/settings.json):
#
#   "hooks": {
#     "SessionStart": [{"hooks": [{"type": "command",
#        "command": "/path/to/cabal/hooks/cabal-session-presence.sh start"}]}],
#     "Stop":         [{"hooks": [{"type": "command",
#        "command": "/path/to/cabal/hooks/cabal-session-presence.sh beat"}]}]
#   }
#
# FAILS OPEN, ALWAYS. Presence is an operator convenience; it must never break
# a turn. Every path below exits 0, including "cabal is not running".

set -uo pipefail

MODE="${1:-beat}"

# --- name this session ------------------------------------------------------
# CABAL_SESSION_NAME is how an operator gets a readable roster ("poe2") instead
# of a wall of uuids. Falling back to the session id keeps the row correct when
# nobody set one -- an ugly name is recoverable, a missing row is the bug.
input=$(cat 2>/dev/null || true)
sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)
NAME="${CABAL_SESSION_NAME:-${sid:0:8}}"
[ -n "$NAME" ] || exit 0

# --- gates, each fails open -------------------------------------------------
command -v cbl >/dev/null 2>&1 || exit 0

# --- what am I doing --------------------------------------------------------
# Repo and branch are cheap and factual. The task line is left to the operator
# via CABAL_SESSION_TASK rather than guessed from the transcript: a wrong
# one-liner in a roster is worse than a blank one, because it reads as known.
repo=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || true)
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
# A DETACHED HEAD reports the literal string "HEAD", which tells an operator
# nothing. This is not an edge case here: a Prudence review worktree runs
# `git checkout <sha>` on purpose, so the reviewer seats are detached by design
# and "HEAD" would be the branch shown for every review. Report the sha, which
# is the thing you actually want to know about a detached session.
if [ "$branch" = "HEAD" ]; then
  branch=$(git rev-parse --short HEAD 2>/dev/null || true)
  [ -n "$branch" ] && branch="detached@$branch"
fi

args=()
[ -n "$repo" ]   && args+=(--repo "$repo")
[ -n "$branch" ] && args+=(--branch "$branch")
[ -n "${CABAL_SESSION_TASK:-}" ] && args+=(--task "$CABAL_SESSION_TASK")

# `${args[@]+...}` NOT `"${args[@]}"`: under `set -u`, expanding an EMPTY array
# the plain way is an unbound-variable error on bash 3.2 -- which is what
# /bin/bash is on macOS. A session outside a git repo has no repo, no branch and
# no task, so the empty case is the ordinary one, not the edge.
case "$MODE" in
  start) cbl session register "$NAME" ${args[@]+"${args[@]}"} >/dev/null 2>&1 ;;
  beat)  cbl session heartbeat "$NAME" ${args[@]+"${args[@]}"} >/dev/null 2>&1 \
           || cbl session register "$NAME" ${args[@]+"${args[@]}"} >/dev/null 2>&1 ;;
  end)   cbl session end "$NAME" >/dev/null 2>&1 ;;
esac

# Unconditionally 0: see the fail-open note at the top.
exit 0
