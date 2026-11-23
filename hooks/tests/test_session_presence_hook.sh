#!/usr/bin/env bash
# Tests for hooks/cabal-session-presence.sh (cabal#142).
# The stub `cbl` ASSERTS ITS ARGV rather than ignoring it — a stub that accepts
# anything lets a mutant that calls the wrong subcommand pass (tenishai/agents-archive#91 r1).
set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/cabal-session-presence.sh"
pass=0; fail=0
check() { # check <label> <expected-substring> <actual>
  if [[ "$3" == *"$2"* ]]; then pass=$((pass+1));
  else fail=$((fail+1)); echo "FAIL: $1"; echo "  want substring: $2"; echo "  got: $3"; fi
}
check_not() {
  if [[ "$3" != *"$2"* ]]; then pass=$((pass+1));
  else fail=$((fail+1)); echo "FAIL: $1"; echo "  did NOT want: $2"; echo "  got: $3"; fi
}

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/cbl" <<'STUB'
#!/usr/bin/env bash
echo "$@" >> "$CBL_LOG"
[ "${CBL_FAIL:-0}" = "1" ] && exit 1
exit 0
STUB
chmod +x "$TMP/bin/cbl"
export CBL_LOG="$TMP/calls.log"

run() { # run <mode> [cwd]
  : > "$CBL_LOG"
  ( cd "${2:-$TMP}" && PATH="$TMP/bin:$PATH" CABAL_SESSION_NAME=agent-b \
      bash "$HOOK" "$1" </dev/null >/dev/null 2>&1 )
  echo "exit=$?"
  cat "$CBL_LOG" 2>/dev/null
}

# 1. start registers under the operator-supplied name
out=$(run start); check "start registers" "session register agent-b" "$out"

# 2. a non-repo directory still registers — the empty-args path is ordinary,
#    and on bash 3.2 the naive expansion would abort here
check "start exits 0 outside a repo" "exit=0" "$out"
check_not "no empty --repo flag" "--repo " "$out"

# 3. inside a git repo, repo and branch are reported
GITDIR="$TMP/proj"; mkdir -p "$GITDIR"
( cd "$GITDIR" && git init -q . \
    && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init \
    && git checkout -q -b feat/142 )
out=$(run beat "$GITDIR")
check "beat reports repo"   "--repo proj"        "$out"
check "beat reports branch" "--branch feat/142"  "$out"

# 3b. a DETACHED worktree (what every Prudence review runs in) reports the sha,
#     not the useless literal "HEAD"
( cd "$GITDIR" && git checkout -q --detach HEAD )
out=$(run beat "$GITDIR")
check "detached reports a sha" "--branch detached@" "$out"
check_not "detached is not literal HEAD" "--branch HEAD" "$out"
( cd "$GITDIR" && git checkout -q feat/142 )

# 4. a failing heartbeat falls back to register — covers a reaped row
out=$( : > "$CBL_LOG"; ( cd "$GITDIR" && PATH="$TMP/bin:$PATH" CBL_FAIL=1 \
        CABAL_SESSION_NAME=agent-b bash "$HOOK" beat </dev/null >/dev/null 2>&1 ); \
      echo "exit=$?"; cat "$CBL_LOG" )
check "heartbeat falls back to register" "session register agent-b" "$out"
check "fallback still exits 0" "exit=0" "$out"

# 5. the task line only appears when the operator sets one — never guessed
out=$( : > "$CBL_LOG"; ( cd "$GITDIR" && PATH="$TMP/bin:$PATH" \
        CABAL_SESSION_NAME=agent-b CABAL_SESSION_TASK="hitting #142" \
        bash "$HOOK" beat </dev/null >/dev/null 2>&1 ); cat "$CBL_LOG" )
check "task passed through" "--task hitting #142" "$out"

# 6. FAILS OPEN when cabal is absent — the whole safety argument
out=$( : > "$CBL_LOG"; ( cd "$GITDIR" && PATH="/usr/bin:/bin" \
        CABAL_SESSION_NAME=agent-b bash "$HOOK" start </dev/null >/dev/null 2>&1 ); \
      echo "exit=$?" )
check "no cbl on PATH exits 0" "exit=0" "$out"

# 7. FAILS OPEN when cbl itself errors
out=$( : > "$CBL_LOG"; ( cd "$GITDIR" && PATH="$TMP/bin:$PATH" CBL_FAIL=1 \
        CABAL_SESSION_NAME=agent-b bash "$HOOK" start </dev/null >/dev/null 2>&1 ); \
      echo "exit=$?" )
check "cbl failure exits 0" "exit=0" "$out"

# 8. with no name and no session_id on stdin, do nothing rather than register junk
out=$( : > "$CBL_LOG"; ( cd "$GITDIR" && PATH="$TMP/bin:$PATH" \
        bash "$HOOK" start </dev/null >/dev/null 2>&1 ); echo "exit=$?"; cat "$CBL_LOG" )
check "nameless exits 0" "exit=0" "$out"
check_not "nameless registers nothing" "session register" "$out"

# 9. the session_id from the hook payload names the row when no override is set
out=$( : > "$CBL_LOG"; ( cd "$GITDIR" && PATH="$TMP/bin:$PATH" \
        bash "$HOOK" start <<<'{"session_id":"abcd1234efgh"}' >/dev/null 2>&1 ); \
      cat "$CBL_LOG" )
check "falls back to session id" "session register abcd1234" "$out"

# 9b. a FAILING call leaves a footprint in the log, while still exiting 0.
#     This is the whole point of nit 1: fail-open must not mean fail-silent.
cat > "$TMP/bin/cbl" <<'NOISY'
#!/usr/bin/env bash
echo "$@" >> "$CBL_LOG"
echo "no token; run 'cbl login' first." >&2
exit 1
NOISY
chmod +x "$TMP/bin/cbl"
export TMPDIR="$TMP"
rm -f "$TMP/cabal-session-presence.log"
out=$( ( cd "$GITDIR" && PATH="$TMP/bin:$PATH" TMPDIR="$TMP" CABAL_SESSION_NAME=agent-b \
        bash "$HOOK" start </dev/null >/dev/null 2>&1 ); echo "exit=$?" )
check "a failing call still exits 0"       "exit=0"                    "$out"
check "stderr is captured, not discarded"  "no token"                  "$(cat "$TMP/cabal-session-presence.log" 2>/dev/null)"

# 9c. stdout is still discarded — the log must not fill with success chatter
cat > "$TMP/bin/cbl" <<'CHATTY'
#!/usr/bin/env bash
echo "$@" >> "$CBL_LOG"
echo "registered agent-b (kind=session)"
exit 0
CHATTY
chmod +x "$TMP/bin/cbl"
rm -f "$TMP/cabal-session-presence.log"
( cd "$GITDIR" && PATH="$TMP/bin:$PATH" TMPDIR="$TMP" CABAL_SESSION_NAME=agent-b \
    bash "$HOOK" start </dev/null >/dev/null 2>&1 )
check_not "success chatter stays out of the log" "registered agent-b" \
  "$(cat "$TMP/cabal-session-presence.log" 2>/dev/null)"

# restore the plain stub for anything after this point
cat > "$TMP/bin/cbl" <<'STUB2'
#!/usr/bin/env bash
echo "$@" >> "$CBL_LOG"
[ "${CBL_FAIL:-0}" = "1" ] && exit 1
exit 0
STUB2
chmod +x "$TMP/bin/cbl"

# 10. end de-registers
out=$(run end); check "end de-registers" "session end agent-b" "$out"

echo "---"; echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
