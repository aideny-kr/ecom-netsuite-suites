#!/usr/bin/env bash
# PreToolUse hook for Bash.
#
# Permissions globs in .claude/settings.json catch the obvious shapes
# (`rm -rf *`, `git push --force *`). This hook is the parser-aware fallback:
# it splits compound commands on `&&`, `||`, `;`, `|` and inspects each
# subcommand, then matches against a small allowlist of patterns Anthropic
# docs admit are bypassable at the glob layer (env-var prefixes, `time`,
# subshells, `xargs`, etc.).
#
# Anthropic hook contract: exit 2 + stderr = block.
#
# Conservative on purpose: when in doubt, allow. The permissions layer in
# .claude/settings.json already denies the broad shapes; this hook only
# catches the parser bypasses.

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

payload="$(cat)"
tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // ""')"
[[ "$tool_name" == "Bash" ]] || exit 0

cmd="$(printf '%s' "$payload" | jq -r '.tool_input.command // ""')"
[[ -z "$cmd" ]] && exit 0

# Split on shell separators so each subcommand is checked independently.
# A simple sed pass turns &&, ||, ;, | into newlines; arguments with quoted
# pipes survive because the tool_input.command is already shell-escaped at
# call sites we care about.
subs="$(printf '%s\n' "$cmd" | sed -E 's/(\&\&|\|\||;|\|)/\n/g')"

# Process-wrapper strip: drop leading wrappers like `env FOO=1`, `time`,
# `nice`, `nohup`, `stdbuf`, `xargs`, `sudo`, `timeout 30s`, `npx`,
# `docker exec <name>`, `mise exec --`, `devbox run --`. The goal is to
# reduce e.g. `sudo -E env A=1 timeout 30 docker exec app rm -rf /` down to
# `rm -rf /` for matching.
#
# Each iteration peels one wrapper. Wrappers that take ARGUMENTS before the
# actual subcommand (timeout, docker exec, mise exec, sudo -E -u user) need
# special handling so we don't peel a real argument.
strip_wrappers() {
    local s="$1"
    local guard
    # Cap the number of peeling iterations so a malformed input cannot loop.
    for ((guard=0; guard<32; guard++)); do
        case "$s" in
            # Plain prefix wrappers — drop the first word.
            "time "*|"nohup "*|"stdbuf "*|"xargs "*|"npx "*)
                s="${s#* }"
                ;;
            # env can take flags (`-i`, `-u VAR`) and any number of KEY=value
            # pairs before the actual command. NOTE: `-S "..."` and
            # `--split-string "..."` are intentionally NOT peeled here —
            # they wrap a quoted multi-token command that unwrap_nested
            # handles at the outer loop level. Peeling -S here would mangle
            # the inner string by splitting on the first space.
            "env "*)
                # Bail if env is being used to nest a command — let
                # unwrap_nested handle it on the next outer iteration.
                case "$s" in
                    "env -S "*|"env --split-string "*)
                        printf '%s' "$s"; return ;;
                esac
                s="${s#env }"
                while [[ "$s" == -* || "$s" == *=*" "* ]]; do
                    case "$s" in
                        "-i"*|"-i ")  s="${s#-i}"; s="${s# }" ;;
                        "-u "*|"--unset "*) s="${s#* }"; s="${s#* }" ;;
                        "-- "*) s="${s#-- }"; break ;;
                        "-"*) s="${s#* }" ;;
                        *=*" "*)
                            first="${s%% *}"
                            if [[ "$first" == *=* && "$first" != *" "* ]]; then
                                s="${s#* }"
                            else
                                break
                            fi
                            ;;
                        *) break ;;
                    esac
                done
                ;;
            # nice takes -n <level> or --adjustment=<level>.
            "nice "*)
                s="${s#nice }"
                case "$s" in
                    "-n "*) s="${s#-n }"; s="${s#* }" ;;
                    "--adjustment "*) s="${s#--adjustment }"; s="${s#* }" ;;
                    "--adjustment="*) s="${s#* }" ;;
                esac
                ;;
            # sudo can take many flags before the command (-E, -u user, -i, --).
            "sudo "*)
                s="${s#sudo }"
                while [[ "$s" == -* ]]; do
                    if [[ "$s" == "-- "* ]]; then s="${s#-- }"; break; fi
                    case "$s" in
                        "-u "*|"--user "*|"-g "*|"--group "*) s="${s#* }"; s="${s#* }" ;;
                        *) s="${s#* }" ;;
                    esac
                done
                ;;
            # timeout takes optional flags then a duration then the command.
            # GNU form: `timeout [OPTIONS] DURATION COMMAND ...`
            # Possible flag shapes: `--kill-after=5s`, `--kill-after 5s`, `-k 5s`,
            # `--preserve-status`, `--foreground`, `-s SIG`, `--signal=SIG`.
            "timeout "*)
                s="${s#timeout }"
                while [[ "$s" == -* ]]; do
                    case "$s" in
                        "-k "*|"--kill-after "*|"--signal "*|"-s "*) s="${s#* }"; s="${s#* }" ;;
                        "--kill-after="*|"--signal="*) s="${s#* }" ;;
                        *) s="${s#* }" ;;
                    esac
                done
                # Now the first token is the duration; drop it.
                s="${s#* }"
                ;;
            # `docker exec <container>` then optional flags then the command.
            "docker exec "*)
                s="${s#docker exec }"
                while [[ "$s" == -* ]]; do
                    case "$s" in
                        "-u "*|"--user "*|"-w "*|"--workdir "*|"-e "*|"--env "*)
                            s="${s#* }"; s="${s#* }" ;;
                        *) s="${s#* }" ;;
                    esac
                done
                # Now first token is the container name; drop it.
                s="${s#* }"
                ;;
            # `mise exec --` then the command.
            "mise exec "*)
                s="${s#mise exec }"
                if [[ "$s" == "-- "* ]]; then s="${s#-- }"; fi
                ;;
            # `devbox run --` then the command.
            "devbox run "*)
                s="${s#devbox run }"
                if [[ "$s" == "-- "* ]]; then s="${s#-- }"; fi
                ;;
            # Bare KEY=value <cmd> chains — drop one KEY=value at a time.
            *=*" "*)
                first="${s%% *}"
                if [[ "$first" == *=* && "$first" != *" "* ]]; then
                    s="${s#* }"
                else
                    printf '%s' "$s"; return
                fi
                ;;
            *)
                printf '%s' "$s"; return
                ;;
        esac
    done
    printf '%s' "$s"
}

matches=()
add_match() {
    matches+=("$1")
}

# Nested-form detection — `bash -c "..."`, `sh -c "..."`, `env -S "..."`
# evade wrapper-strip + canonicalisation because the inner string is one
# quoted token. Pull the inner string out FIRST so the rest of the matcher
# evaluates it directly.
unwrap_nested() {
    local s="$1"
    case "$s" in
        "bash -c "*|"sh -c "*|"zsh -c "*)
            s="${s#* -c }" ;;
        "env -S "*|"env --split-string "*)
            s="${s#env }"; s="${s#-S }"; s="${s#--split-string }" ;;
        *) printf '%s' "$1"; return ;;
    esac
    s="${s#\"}"; s="${s%\"}"
    s="${s#\'}"; s="${s%\'}"
    printf '%s' "$s"
}

while IFS= read -r raw; do
    sub="$(printf '%s' "$raw" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    [[ -z "$sub" ]] && continue
    # Loop unwrap + wrapper-strip until the string stops shrinking so
    # compositions like `sudo bash -c "rm -rf /"` get fully peeled.
    canonical="$sub"
    prev=""
    guard_loops=0
    while [[ "$canonical" != "$prev" && $guard_loops -lt 16 ]]; do
        prev="$canonical"
        canonical="$(unwrap_nested "$canonical")"
        canonical="$(strip_wrappers "$canonical")"
        guard_loops=$((guard_loops + 1))
    done

    # Canonicalise rm invocations so the variants codex enumerated all
    # collapse to "rm -rf <target>" for matching:
    #   rm -r -f / | rm --recursive --force / | rm -R -f / | rm -rf -- /
    #   /bin/rm -rf / | command rm -rf / | rm -rf --one-file-system /
    #   rm -rf $HOME | rm -rf ${HOME} | rm -rf "$HOME"
    # Anything that isn't an rm invocation passes through untouched.
    canonical_rm() {
        local c="$1"
        # Strip explicit invocation prefixes.
        c="${c#command }"
        c="${c#/bin/}"; c="${c#/usr/bin/}"; c="${c#/usr/local/bin/}"
        [[ "$c" != "rm "* && "$c" != "rm" ]] && { printf '%s' "$canonical"; return; }

        # Tokenize on whitespace and rebuild canonical "rm -rf <target>".
        local has_r=0 has_f=0
        local -a targets=()
        local skip_separator=0
        # shellcheck disable=SC2206
        local -a toks=($c)
        local first=1
        for t in "${toks[@]}"; do
            if (( first )); then first=0; continue; fi  # drop the rm
            if (( skip_separator )); then targets+=("$t"); continue; fi
            case "$t" in
                --) skip_separator=1 ;;
                --recursive|--RECURSIVE) has_r=1 ;;
                --force) has_f=1 ;;
                --no-preserve-root|--one-file-system|--verbose|--interactive|--preserve-root|-v|-i|-I) ;;
                -[a-zA-Z]*)
                    # Combined flags like -rf, -fr, -RFv, -r, -f, -R
                    local f="${t#-}"
                    local i ch
                    for ((i=0; i<${#f}; i++)); do
                        ch="${f:$i:1}"
                        case "$ch" in
                            r|R) has_r=1 ;;
                            f) has_f=1 ;;
                        esac
                    done
                    ;;
                *) targets+=("$t") ;;
            esac
        done
        if (( has_r == 0 || has_f == 0 )); then
            printf '%s' "$canonical"  # not a force-recursive rm; uninteresting
            return
        fi
        # Expand env-var home and tilde forms into a single canonical token.
        # Strip surrounding quotes first, then map $HOME / ${HOME} to ~ so the
        # final matcher only needs to look at tilde shapes.
        local -a expanded=()
        local tgt unquoted
        for tgt in "${targets[@]}"; do
            unquoted="${tgt#\"}"; unquoted="${unquoted%\"}"
            unquoted="${unquoted#\'}"; unquoted="${unquoted%\'}"
            # Normalize $HOME and ${HOME} prefixes to ~ in one pass via sed.
            unquoted="$(printf '%s' "$unquoted" | sed -E 's#\$\{?HOME\}?#~#g')"
            expanded+=("$unquoted")
        done
        printf 'rm -rf %s' "${expanded[*]}"
    }

    canonical_norm="$(canonical_rm "$canonical")"

    # If this is a force-recursive rm, inspect each target individually so
    # multi-target deletes like ``rm -rf / /tmp/x`` can't smuggle a banned
    # target alongside an innocent one.
    if [[ "$canonical_norm" == "rm -rf "* ]]; then
        targets_str="${canonical_norm#rm -rf }"
        # shellcheck disable=SC2206
        target_toks=($targets_str)
        for tgt in "${target_toks[@]}"; do
            case "$tgt" in
                "/"|"/*")
                    add_match "rm -rf on absolute root: '$sub' (target '$tgt')" ;;
                "~"|"~/"|"~/*"|"~/.*")
                    add_match "rm -rf on home dir: '$sub' (target '$tgt')" ;;
            esac
        done
    fi

    case "$canonical_norm" in
        # Git history rewrites + force pushes.
        *"git push"*"--force"*|*"git push"*" -f "*|*"git push "*"-f"|*"git push +"*)
            add_match "git force push: '$sub'" ;;
        *"git push --force-with-lease"*)
            add_match "git force-with-lease push: '$sub' — confirm intent" ;;
        # git reset --hard and git clean -fdx are handled by
        # permissions.ask in .claude/settings.json so the user is prompted.
        # We do NOT hard-block them here — would defeat the ask policy.
        # Hosted DB destructive ops.
        *"supabase db reset"*|*"supabase db push --linked"*)
            add_match "supabase destructive op against linked project: '$sub'" ;;
        *"supabase"*"--linked"*)
            add_match "supabase against linked (remote) project: '$sub'" ;;
        # Production cloud deploys.
        *"vercel --prod"*|*"vercel deploy --prod"*)
            add_match "vercel production deploy: '$sub'" ;;
        *"terraform apply"*|*"terraform destroy"*)
            add_match "terraform mutation: '$sub'" ;;
        # Kubernetes destructive.
        *"kubectl delete"*|*"kubectl drain"*)
            add_match "kubectl destructive op: '$sub'" ;;
        # GHCR / docker prune.
        *"docker system prune"*|*"docker volume prune"*)
            add_match "docker prune: '$sub'" ;;
    esac
done <<<"$subs"

if (( ${#matches[@]} == 0 )); then
    exit 0
fi

{
    echo "BLOCKED by guard-destructive hook."
    echo "Subcommands matched destructive patterns the parser-aware guard refuses by default:"
    for m in "${matches[@]}"; do
        echo "  - $m"
    done
    echo
    echo "Either narrow the command (e.g. rm -rf ./scoped/dir), or invoke it directly"
    echo "outside the agent. If the action is intentional and you've thought about blast"
    echo "radius, run it from your own shell rather than through Claude."
} >&2
exit 2
