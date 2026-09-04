#!/usr/bin/env bash
# Test suite for .claude/hooks/scripts/*.sh.
#
# Each test:
#   1. Pipes a synthetic JSON payload to a hook script
#   2. Asserts on exit code (0 = allow, 2 = block) and stderr content
#
# Run from repo root: ./.claude/hooks/tests/test_hooks.sh
#
# Exit code 0 = all green. Non-zero = at least one assertion failed.

set -u

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SCRIPTS="$ROOT/.claude/hooks/scripts"

PASS=0
FAIL=0
declare -a FAILURES

assert() {
    local name="$1" expected_code="$2" actual_code="$3" expected_pattern="$4" stderr_actual="$5"

    if [[ "$expected_code" != "$actual_code" ]]; then
        FAILURES+=("$name: expected exit $expected_code, got $actual_code")
        FAIL=$((FAIL + 1))
        echo "  ✗ $name (exit code: got $actual_code, expected $expected_code)"
        return
    fi

    if [[ -n "$expected_pattern" ]] && ! printf '%s' "$stderr_actual" | grep -q -E "$expected_pattern"; then
        FAILURES+=("$name: stderr did not match '$expected_pattern'")
        FAIL=$((FAIL + 1))
        echo "  ✗ $name (stderr did not match: $expected_pattern)"
        echo "    stderr: $stderr_actual"
        return
    fi

    PASS=$((PASS + 1))
    echo "  ✓ $name"
}

run_hook() {
    # Usage: run_hook <script> <payload> -> sets $RC, $STDERR, $STDOUT
    local script="$1" payload="$2"
    local stderr_file stdout_file
    stderr_file="$(mktemp)"
    stdout_file="$(mktemp)"
    set +e
    printf '%s' "$payload" | "$script" 2>"$stderr_file" >"$stdout_file"
    RC=$?
    set -e
    STDERR="$(cat "$stderr_file")"
    STDOUT="$(cat "$stdout_file")"
    rm -f "$stderr_file" "$stdout_file"
}

echo "=== detect-secrets.sh ==="

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"hello world"}}'
assert "allows benign Write" 0 "$RC" "" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"key = sk_live_abc123def456ghi789jkl012mno345"}}'
assert "blocks Stripe live key in Write content" 2 "$RC" "Stripe live secret key" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Edit","tool_input":{"file_path":"/x.py","new_string":"AKIAIOSFODNN7EXAMPLE"}}'
assert "blocks AWS access key id in Edit new_string" 2 "$RC" "AWS access key id" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Edit","tool_input":{"file_path":"/x.py","new_string":"sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"}}'
assert "blocks Anthropic API key" 2 "$RC" "Anthropic API key" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"MultiEdit","tool_input":{"file_path":"/x.py","edits":[{"new_string":"benign"},{"new_string":"-----BEGIN RSA PRIVATE KEY-----"}]}}'
assert "blocks RSA private key in MultiEdit edits[]" 2 "$RC" "RSA private key" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"# placeholder: sk_live_PLACEHOLDER"}}'
assert "allows placeholder shorter than threshold" 0 "$RC" "" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}'
assert "skips non-Edit tools" 0 "$RC" "" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"OPENAI_API_KEY=sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq"}}'
assert "blocks OpenAI sk-proj- key" 2 "$RC" "OpenAI project API key" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"STRIPE_WEBHOOK_SECRET=whsec_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"}}'
assert "blocks Stripe whsec_" 2 "$RC" "Stripe webhook signing secret" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"DATABASE_URL=postgresql://user:hunter2@db.example.com/postgres"}}'
assert "blocks Postgres URL with password" 2 "$RC" "Postgres URL with password" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"SLACK_BOT_TOKEN=xoxb-12345678901-12345678901-AbCdEfGhIjKlMnOpQrStUvWx"}}'
assert "blocks Slack bot token" 2 "$RC" "Slack bot token" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"PAT=github_pat_11ABCDEFG0CdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYzAb"}}'
assert "blocks GitHub fine-grained PAT" 2 "$RC" "GitHub fine-grained PAT" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}}'
assert "blocks uppercase AWS_SECRET_ACCESS_KEY assignment" 2 "$RC" "AWS secret access key" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"NS_CONSUMER_SECRET=1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"}}'
assert "blocks NetSuite TBA consumer secret" 2 "$RC" "NetSuite TBA consumer secret" "$STDERR"

run_hook "$SCRIPTS/detect-secrets.sh" '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"FERNET_KEY=YgQzcsK1ulOEXcd4yPgEr3ovBQOXyaJaO8aGc5T3-Q4="}}'
assert "blocks Fernet symmetric key assignment" 2 "$RC" "Fernet symmetric key" "$STDERR"

echo
echo "=== guard-destructive.sh ==="

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}'
assert "allows ls" 0 "$RC" "" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/x"}}'
assert "allows scoped rm -rf" 0 "$RC" "" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'
assert "blocks rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~"}}'
assert "blocks rm -rf ~" 2 "$RC" "rm -rf on home dir" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"git push --force origin main"}}'
assert "blocks force push" 2 "$RC" "force push" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"git push -f origin main"}}'
assert "blocks short -f push" 2 "$RC" "force push" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"ls && rm -rf /"}}'
assert "blocks rm -rf / chained after &&" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"FOO=1 BAR=2 rm -rf /"}}'
assert "strips env-var wrappers and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"sudo rm -rf /"}}'
assert "strips sudo wrapper and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"sudo -E rm -rf /"}}'
assert "strips sudo -E flag and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"sudo -u root rm -rf /"}}'
assert "strips sudo -u user and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"timeout 30s rm -rf /"}}'
assert "strips timeout wrapper and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"docker exec app rm -rf /"}}'
assert "strips docker exec wrapper and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"docker exec -it app rm -rf /"}}'
assert "strips docker exec -it wrapper and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"mise exec -- rm -rf /"}}'
assert "strips mise exec wrapper and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"npx rm -rf /"}}'
assert "strips npx wrapper and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf --no-preserve-root /"}}'
assert "blocks rm -rf --no-preserve-root /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~/*"}}'
assert "blocks rm -rf ~/* glob" 2 "$RC" "rm -rf on home dir" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"timeout --kill-after=5s 30s rm -rf /"}}'
assert "strips timeout --kill-after= flag and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"env -i rm -rf /"}}'
assert "strips env -i flag and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"nice -n 10 rm -rf /"}}'
assert "strips nice -n level wrapper and still blocks" 2 "$RC" "rm -rf on absolute root" "$STDERR"

# Codex P1 round 3 — rm-variant canonicalisation tests.
run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -r -f /"}}'
assert "canonicalises split flags rm -r -f /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -R -f /"}}'
assert "canonicalises -R uppercase to recursive" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm --recursive --force /"}}'
assert "canonicalises long-form --recursive --force" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf -- /"}}'
assert "handles -- separator before target" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"/bin/rm -rf /"}}'
assert "strips /bin/ absolute-path prefix" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"command rm -rf /"}}'
assert "strips command-builtin prefix" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf --one-file-system /"}}'
assert "ignores extra --one-file-system flag" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf $HOME"}}'
assert "expands $HOME to ~" 2 "$RC" "rm -rf on home dir" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf ${HOME}"}}'
assert "expands ${HOME} to ~" 2 "$RC" "rm -rf on home dir" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf \"$HOME\""}}'
assert "expands quoted \"$HOME\" to ~" 2 "$RC" "rm -rf on home dir" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf $HOME/*"}}'
assert "expands $HOME/* to ~/*" 2 "$RC" "rm -rf on home dir" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~/.*"}}'
assert "blocks rm -rf ~/.* hidden glob" 2 "$RC" "rm -rf on home dir" "$STDERR"

# Nested forms.
run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"bash -c \"rm -rf /\""}}'
assert "blocks bash -c nested rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"sh -c \"rm -rf /\""}}'
assert "blocks sh -c nested rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"env -S \"rm -rf /\""}}'
assert "blocks env -S nested rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

# Codex P1 round 4 — multi-target + composed wrapper+nested.
run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf / /tmp/x"}}'
assert "inspects each target — blocks rm -rf / /tmp/x" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~ foo"}}'
assert "inspects each target — blocks rm -rf ~ foo" 2 "$RC" "rm -rf on home dir" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"sudo bash -c \"rm -rf /\""}}'
assert "iterates unwrap+strip — blocks sudo bash -c rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"sudo env -S \"rm -rf /\""}}'
assert "iterates unwrap+strip — blocks sudo env -S rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"env FOO=1 bash -c \"rm -rf /\""}}'
assert "iterates unwrap+strip — blocks env FOO=1 bash -c rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"timeout 1s bash -c \"rm -rf /\""}}'
assert "iterates unwrap+strip — blocks timeout 1s bash -c rm -rf /" 2 "$RC" "rm -rf on absolute root" "$STDERR"

# Codex P1 — hook must NOT hard-block what settings.ask handles.
run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"git reset --hard HEAD~1"}}'
assert "does not hard-block git reset --hard (settings.ask handles it)" 0 "$RC" "" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"git clean -fdx"}}'
assert "does not hard-block git clean -fdx (settings.ask handles it)" 0 "$RC" "" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"supabase db push --linked"}}'
assert "blocks supabase --linked op" 2 "$RC" "supabase" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Bash","tool_input":{"command":"terraform apply"}}'
assert "blocks terraform apply" 2 "$RC" "terraform" "$STDERR"

run_hook "$SCRIPTS/guard-destructive.sh" '{"tool_name":"Edit","tool_input":{"file_path":"/x.py","new_string":"ok"}}'
assert "skips non-Bash tools" 0 "$RC" "" "$STDERR"

echo
echo "=== format-on-write.sh ==="

# Synthetic file in /tmp so we don't write into a real project.
TMP_PY="$(mktemp -t fmt-test.XXXX.py)"
printf 'x=1\n' > "$TMP_PY"
run_hook "$SCRIPTS/format-on-write.sh" "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$TMP_PY\"}}"
assert "format hook always exits 0" 0 "$RC" "" "$STDERR"
rm -f "$TMP_PY"

run_hook "$SCRIPTS/format-on-write.sh" '{"tool_name":"Bash","tool_input":{"command":"ls"}}'
assert "skips non-Edit tools" 0 "$RC" "" "$STDERR"

echo
echo "=== worktree-banner.sh ==="

run_hook "$SCRIPTS/worktree-banner.sh" "{\"cwd\":\"$ROOT\"}"
# Banner must go to stdout (per Anthropic hook docs — stdout is fed back to
# Claude as additional context on SessionStart / CwdChanged events).
assert "emits banner on stdout for valid git checkout" 0 "$RC" "worktree" "$STDOUT"

run_hook "$SCRIPTS/worktree-banner.sh" '{"cwd":"/tmp"}'
assert "exits 0 cleanly outside a git checkout" 0 "$RC" "" "$STDERR"

echo
echo "=== bash-secret-scan.sh ==="

run_hook "$SCRIPTS/bash-secret-scan.sh" '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}'
assert "skips read-only commands" 0 "$RC" "" "$STDERR"

# Synthesize a tempfile and run the scanner against it. Each test creates a
# file with a fake secret and then references it from a bash command.
TMP_SECRET="$(mktemp -t bash-scan.XXXX)"
printf 'sk_live_abc123def456ghi789jkl012mno345\n' > "$TMP_SECRET"
run_hook "$SCRIPTS/bash-secret-scan.sh" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > $TMP_SECRET\"}}"
assert "flags Stripe live key in file referenced by command" 2 "$RC" "Stripe live secret key" "$STDERR"
rm -f "$TMP_SECRET"

TMP_SECRET="$(mktemp -t bash-scan.XXXX)"
printf 'postgresql://app:hunter2@db.example.com/postgres\n' > "$TMP_SECRET"
run_hook "$SCRIPTS/bash-secret-scan.sh" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cat $TMP_SECRET > /dev/null\"}}"
assert "flags Postgres URL in cat'd file (with redirect, not skipped)" 2 "$RC" "Postgres URL with password" "$STDERR"
rm -f "$TMP_SECRET"

TMP_SECRET="$(mktemp -t bash-scan.XXXX)"
printf 'rk_live_abcdefghij1234567890abcdefghij1234567890\n' > "$TMP_SECRET"
run_hook "$SCRIPTS/bash-secret-scan.sh" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > $TMP_SECRET\"}}"
assert "flags Stripe rk_live restricted key" 2 "$RC" "Stripe restricted key" "$STDERR"
rm -f "$TMP_SECRET"

TMP_SECRET="$(mktemp -t bash-scan.XXXX)"
printf -- '-----BEGIN PRIVATE KEY-----\nQUJD\n-----END PRIVATE KEY-----\n' > "$TMP_SECRET"
run_hook "$SCRIPTS/bash-secret-scan.sh" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > $TMP_SECRET\"}}"
assert "flags generic PRIVATE KEY block" 2 "$RC" "Google service account private key" "$STDERR"
rm -f "$TMP_SECRET"

TMP_SECRET="$(mktemp -t bash-scan.XXXX)"
printf 'xoxb-12345678901-12345678901-AbCdEfGhIjKlMnOpQrStUvWx\n' > "$TMP_SECRET"
run_hook "$SCRIPTS/bash-secret-scan.sh" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > $TMP_SECRET\"}}"
assert "flags Slack bot token" 2 "$RC" "Slack bot token" "$STDERR"
rm -f "$TMP_SECRET"

echo
echo "=== audit-config-change.sh ==="

# Set CLAUDE_PROJECT_DIR to a temp dir so the test doesn't pollute the worktree.
TMP_AUDIT_ROOT="$(mktemp -d -t audit-test.XXXX)"
(
    cd "$TMP_AUDIT_ROOT"
    git init -q .
    git commit -q --allow-empty -m init
)
run_hook "$SCRIPTS/audit-config-change.sh" '{"event_type":"ConfigChange","changed_paths":[".claude/settings.json"]}'
# The hook resolves toplevel via git from CWD, so we need to cd. Re-run:
RC=0
pushd "$TMP_AUDIT_ROOT" >/dev/null
printf '%s' '{"event_type":"ConfigChange","changed_paths":[".claude/settings.json"]}' | "$SCRIPTS/audit-config-change.sh" 2>/dev/null
RC=$?
popd >/dev/null
assert "audit hook exits 0 (never blocks)" 0 "$RC" "" ""
if [[ -f "$TMP_AUDIT_ROOT/.claude/audit/config-changes.log" ]]; then
    PASS=$((PASS + 1))
    echo "  ✓ audit log file created"
else
    FAIL=$((FAIL + 1))
    FAILURES+=("audit log was not created at $TMP_AUDIT_ROOT/.claude/audit/config-changes.log")
    echo "  ✗ audit log file not created"
fi
rm -rf "$TMP_AUDIT_ROOT"

echo
echo "──"
echo "passed: $PASS"
echo "failed: $FAIL"
if (( FAIL > 0 )); then
    echo
    for f in "${FAILURES[@]}"; do
        echo "  - $f"
    done
    exit 1
fi
exit 0
