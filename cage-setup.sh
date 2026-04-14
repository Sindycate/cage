#!/bin/bash
# cage setup wizard — interactive configuration generator
# Sourced from the cage script; expects CAGE_CONFIG_DIR to be set.

# ---------------------------------------------------------------------------
# Colors & output helpers
# ---------------------------------------------------------------------------

_setup_colors() {
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        BOLD=$'\033[1m'  DIM=$'\033[2m'  RESET=$'\033[0m'
        GREEN=$'\033[32m'  YELLOW=$'\033[33m'  RED=$'\033[31m'  CYAN=$'\033[36m'
    else
        BOLD="" DIM="" RESET="" GREEN="" YELLOW="" RED="" CYAN=""
    fi
}

_ok()     { echo "  ${GREEN}[ok]${RESET} $*"; }
_warn()   { echo "  ${YELLOW}[!!]${RESET} $*"; }
_err()    { echo "  ${RED}[error]${RESET} $*" >&2; }
_header() { echo ""; echo "${BOLD}=== $* ===${RESET}"; }
_dim()    { echo "  ${DIM}$*${RESET}"; }

# Read a value with a default shown in brackets.
# Usage: _prompt_value "Label" "default" result_var
_prompt_value() {
    local label="$1" default="$2" varname="$3" input
    if [ -n "$default" ]; then
        read -rp "  ${label} [${default}]: " input
    else
        read -rp "  ${label}: " input
    fi
    printf -v "$varname" '%s' "${input:-$default}"
}

# Yes/no prompt. Returns 0 for yes, 1 for no.
# Usage: _prompt_yn "Question?" [Y|N]
_prompt_yn() {
    local label="$1" default="${2:-Y}" input hint
    if [ "$default" = "Y" ]; then hint="Y/n"; else hint="y/N"; fi
    read -rp "  ${label} [${hint}]: " input
    input="${input:-$default}"
    case "$input" in
        [Yy]*) return 0 ;;
        *)     return 1 ;;
    esac
}

# Numbered-choice prompt. Returns the number (1-based) in the given variable.
# Usage: _prompt_choice result_var default "Option 1" "Option 2" ...
_prompt_choice() {
    local varname="$1" default="$2"; shift 2
    local i=1
    for opt in "$@"; do
        echo "  ${i}) ${opt}"
        i=$((i + 1))
    done
    local input
    read -rp "  Choice [${default}]: " input
    printf -v "$varname" '%s' "${input:-$default}"
}

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

_detect_ssh_keys() {
    # Populates _SSH_KEYS (paths).
    # Scans all files in ~/.ssh/ and identifies private keys by content header.
    _SSH_KEYS=()
    [ -d "$HOME/.ssh" ] || return 0
    local key header
    for key in "$HOME"/.ssh/*; do
        [ -f "$key" ] || continue
        # Skip .pub, known_hosts, config, authorized_keys, *.sock, etc.
        case "$key" in
            *.pub|*/known_hosts*|*/config|*/authorized_keys*|*.sock|*.old|*.bak) continue ;;
        esac
        # Check if it's actually a private key by reading the first line
        header="$(head -1 "$key" 2>/dev/null)" || continue
        case "$header" in
            "-----BEGIN OPENSSH PRIVATE KEY-----") ;;
            "-----BEGIN RSA PRIVATE KEY-----") ;;
            "-----BEGIN DSA PRIVATE KEY-----") ;;
            "-----BEGIN EC PRIVATE KEY-----") ;;
            "-----BEGIN PRIVATE KEY-----") ;;
            *) continue ;;
        esac
        _SSH_KEYS+=("$key")
    done
}

_detect_gh_accounts() {
    _GH_ACCOUNTS=()
    command -v gh &>/dev/null || return 0
    local line
    while IFS= read -r line; do
        # gh auth status lines like: "✓ Logged in to github.com account USERNAME (keyring)"
        if [[ "$line" =~ account[[:space:]]+([^[:space:]]+) ]]; then
            _GH_ACCOUNTS+=("${BASH_REMATCH[1]}")
        fi
    done < <(gh auth status 2>&1)
}

# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

_run_setup() {
    _setup_colors

    # Determine target: global cage.conf or a named profile
    local PROFILE_NAME="${CAGE_SETUP_PROFILE:-}"
    local CONF_FILE
    if [ -n "$PROFILE_NAME" ]; then
        # Validate profile name
        if ! [[ "$PROFILE_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
            _err "Invalid profile name: '$PROFILE_NAME' (use only letters, digits, hyphens, underscores)"
            return 1
        fi
        CONF_FILE="$CAGE_CONFIG_DIR/profiles/${PROFILE_NAME}.conf"
        echo "${BOLD}cage setup${RESET} — configuring profile: ${CYAN}${PROFILE_NAME}${RESET}"
    else
        CONF_FILE="$CAGE_CONFIG_DIR/cage.conf"
        echo "${BOLD}cage setup${RESET} — interactive configuration wizard"
    fi

    # --- Phase 0: existing config -------------------------------------------

    local EDIT_MODE=0

    if [ -f "$CONF_FILE" ]; then
        echo ""
        echo "  Existing config found at ${CYAN}${CONF_FILE}${RESET}:"
        echo ""
        # Show current values (strip blank lines, indent)
        while IFS= read -r line; do
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [ -z "$line" ] && continue
            echo "    $line"
        done < "$CONF_FILE"
        echo ""

        # Source existing config so current values become defaults
        source "$CONF_FILE"

        local mode
        _prompt_choice mode 2 \
            "Start fresh (current config will be backed up)" \
            "Edit existing (step through each setting)" \
            "Cancel"

        case "$mode" in
            1)
                local backup="${CONF_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
                cp "$CONF_FILE" "$backup"
                _ok "Backed up to ${backup}"
                # Clear all values so we start from scratch (env detection only)
                GIT_USER_NAME="" GIT_USER_EMAIL="" SSH_KEY="" SSH_HOST=""
                CAGE_DEFAULT="" CLAUDE_AUTH="" AWS_PROFILE="" AWS_REGION=""
                EXTRA_ENV="" CODEX_COPY_AUTH="" GH_AUTH="" GH_ACCOUNT=""
                ;;
            2)
                EDIT_MODE=1
                ;;
            *)
                echo "  Cancelled."
                return 0
                ;;
        esac
    fi

    # --- Phase 1: prerequisites ---------------------------------------------

    _header "Prerequisites"

    if command -v docker &>/dev/null; then
        _ok "Docker is installed"
        if docker info &>/dev/null 2>&1; then
            _ok "Docker daemon is running"
        else
            _warn "Docker daemon is not running"
            _dim "Start it before running cage (e.g., colima start or sudo systemctl start docker)"
        fi
    else
        _warn "Docker is not installed — required to run cage"
    fi

    if command -v python3 &>/dev/null; then
        _ok "python3 is available (needed for --net gate)"
    else
        _warn "python3 not found (needed only for --net gate)"
    fi

    # --- Phase 2: tool selection --------------------------------------------

    _header "Tool Selection"

    _dim "Which AI tool(s) do you want to configure?"
    _dim "Pick 'Both' to set up auth for Claude Code and Codex CLI."
    echo ""
    local tool_choice
    local default_tool_choice=1
    if [ "$EDIT_MODE" -eq 1 ]; then
        case "${CAGE_DEFAULT:-claude}" in
            codex) default_tool_choice=2 ;;
        esac
    fi
    _prompt_choice tool_choice "$default_tool_choice" \
        "Claude Code only" \
        "Codex CLI only" \
        "Both (configure auth for each)"

    local USE_CLAUDE=0 USE_CODEX=0
    local cfg_CAGE_DEFAULT=""
    case "$tool_choice" in
        1) USE_CLAUDE=1; cfg_CAGE_DEFAULT="claude" ;;
        2) USE_CODEX=1;  cfg_CAGE_DEFAULT="codex" ;;
        3) USE_CLAUDE=1; USE_CODEX=1; cfg_CAGE_DEFAULT="claude" ;;
    esac

    # --- Phase 3: Claude auth -----------------------------------------------

    local cfg_CLAUDE_AUTH="" cfg_AWS_PROFILE="" cfg_AWS_REGION=""

    if [ "$USE_CLAUDE" -eq 1 ]; then
        _header "Claude Code Authentication"

        local auth_choice
        local default_auth=1
        if [ "$EDIT_MODE" -eq 1 ] && [ "${CLAUDE_AUTH:-}" = "api-key" ]; then
            default_auth=2
        fi
        _prompt_choice auth_choice "$default_auth" \
            "AWS Bedrock (uses ~/.aws/credentials)" \
            "API key (ANTHROPIC_API_KEY environment variable)"

        case "$auth_choice" in
            1)
                cfg_CLAUDE_AUTH="bedrock"
                echo ""
                _prompt_value "AWS profile name" "${AWS_PROFILE:-default}" cfg_AWS_PROFILE
                _prompt_value "AWS region" "${AWS_REGION:-us-east-1}" cfg_AWS_REGION

                if [ -f "$HOME/.aws/credentials" ]; then
                    _ok "~/.aws/credentials found"
                    if grep -q "\\[${cfg_AWS_PROFILE}\\]" "$HOME/.aws/credentials" 2>/dev/null; then
                        _ok "Profile '${cfg_AWS_PROFILE}' exists in credentials file"
                    else
                        _warn "Profile '${cfg_AWS_PROFILE}' not found in ~/.aws/credentials"
                    fi
                else
                    _warn "~/.aws/credentials not found — configure AWS credentials before running cage"
                fi
                ;;
            2)
                cfg_CLAUDE_AUTH="api-key"
                echo ""
                if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
                    _ok "ANTHROPIC_API_KEY is set in your environment"
                else
                    _warn "ANTHROPIC_API_KEY is not set — set it in your shell profile before running cage"
                fi
                ;;
        esac
    fi

    # --- Phase 4: Codex auth ------------------------------------------------

    local cfg_CODEX_COPY_AUTH=""

    if [ "$USE_CODEX" -eq 1 ]; then
        _header "Codex CLI Authentication"

        _dim "Codex needs auth from ~/.codex/ (sign in on host first) or OPENAI_API_KEY."
        echo ""

        if [ -d "$HOME/.codex" ]; then
            _ok "Found ~/.codex/"
            echo ""
            _dim "By default, cage copies auth.json from ~/.codex/ into the container."
            _dim "If you use a non-OpenAI provider (e.g., Azure OpenAI, custom endpoint), disable this."
            if _prompt_yn "Copy auth.json into container?" "Y"; then
                cfg_CODEX_COPY_AUTH="1"
            else
                cfg_CODEX_COPY_AUTH="0"
            fi
        else
            _warn "~/.codex/ not found"
            _dim "Run 'codex' on your host first to sign in, or set OPENAI_API_KEY."
            cfg_CODEX_COPY_AUTH="1"
        fi

        echo ""
        if [ -n "${OPENAI_API_KEY:-}" ]; then
            _ok "OPENAI_API_KEY is set in your environment (will be passed to container)"
        else
            _dim "OPENAI_API_KEY is not set — that's fine if you're using ~/.codex/ auth."
        fi
    fi

    # --- Phase 5: GitHub CLI -------------------------------------------------

    _header "GitHub CLI"

    local cfg_GH_AUTH="" cfg_GH_ACCOUNT=""

    _detect_gh_accounts

    if [ ${#_GH_ACCOUNTS[@]} -eq 0 ]; then
        if command -v gh &>/dev/null; then
            _dim "gh is installed but no accounts found. Run 'gh auth login' first."
        else
            _dim "gh CLI not found on host. Install it to enable GitHub integration."
        fi
        _dim "Skipping GitHub CLI setup."
    else
        _dim "GitHub CLI auth gives the container access to create PRs, manage issues, etc."
        _dim "This is opt-in — the container has no GitHub access unless you enable it here."
        echo ""

        local gh_default_yn="N"
        [ "$EDIT_MODE" -eq 1 ] && [ "${GH_AUTH:-}" = "1" ] && gh_default_yn="Y"

        if _prompt_yn "Enable GitHub CLI auth in containers?" "$gh_default_yn"; then
            cfg_GH_AUTH="1"

            if [ ${#_GH_ACCOUNTS[@]} -eq 1 ]; then
                _ok "Account: ${_GH_ACCOUNTS[0]}"
                cfg_GH_ACCOUNT="${_GH_ACCOUNTS[0]}"
            else
                echo ""
                _dim "Multiple accounts found. Pick the default (override per-project in .cage.conf):"
                local acct_choice
                local default_acct=1
                if [ "$EDIT_MODE" -eq 1 ] && [ -n "${GH_ACCOUNT:-}" ]; then
                    local i
                    for i in "${!_GH_ACCOUNTS[@]}"; do
                        if [ "${_GH_ACCOUNTS[$i]}" = "$GH_ACCOUNT" ]; then
                            default_acct=$((i+1))
                            break
                        fi
                    done
                fi
                _prompt_choice acct_choice "$default_acct" "${_GH_ACCOUNTS[@]}"
                cfg_GH_ACCOUNT="${_GH_ACCOUNTS[$((acct_choice-1))]}"
                _ok "Selected: ${cfg_GH_ACCOUNT}"
            fi
        else
            _dim "Skipping GitHub CLI setup."
        fi
    fi

    # --- Phase 6: Git identity ----------------------------------------------

    _header "Git Identity"

    local cfg_GIT_USER_NAME="" cfg_GIT_USER_EMAIL=""

    # Detect from git config if not editing existing values
    local detected_name="" detected_email=""
    detected_name="$(git config --global user.name 2>/dev/null || true)"
    detected_email="$(git config --global user.email 2>/dev/null || true)"

    local default_name="${GIT_USER_NAME:-$detected_name}"
    local default_email="${GIT_USER_EMAIL:-$detected_email}"

    if [ -n "$default_name" ] || [ -n "$default_email" ]; then
        _dim "Detected: ${default_name} <${default_email}>"
    fi

    _prompt_value "Git user name" "$default_name" cfg_GIT_USER_NAME
    _prompt_value "Git user email" "$default_email" cfg_GIT_USER_EMAIL

    # --- Phase 7: SSH key ---------------------------------------------------

    _header "SSH Key (for git push)"

    local cfg_SSH_KEY="" cfg_SSH_HOST=""

    _detect_ssh_keys

    if [ ${#_SSH_KEYS[@]} -gt 0 ]; then
        echo "  Found SSH keys:"
        local i
        for i in "${!_SSH_KEYS[@]}"; do
            local display="${_SSH_KEYS[$i]/#$HOME/~}"
            echo "    $((i+1))) ${display}"
        done
        echo "    $((${#_SSH_KEYS[@]}+1))) None (skip SSH setup)"
        _dim "If your key has a passphrase, you'll be prompted each time (no ssh-agent in container)."

        local default_ssh_choice=""
        # Default to existing SSH_KEY if editing
        if [ "$EDIT_MODE" -eq 1 ] && [ -n "${SSH_KEY:-}" ]; then
            local expanded_existing="${SSH_KEY/#\~/$HOME}"
            for i in "${!_SSH_KEYS[@]}"; do
                if [ "${_SSH_KEYS[$i]}" = "$expanded_existing" ]; then
                    default_ssh_choice=$((i+1))
                    break
                fi
            done
        fi
        default_ssh_choice="${default_ssh_choice:-$((${#_SSH_KEYS[@]}+1))}"

        local ssh_choice
        read -rp "  Choice [${default_ssh_choice}]: " ssh_choice
        ssh_choice="${ssh_choice:-$default_ssh_choice}"

        if [ "$ssh_choice" -le "${#_SSH_KEYS[@]}" ] 2>/dev/null; then
            local idx=$((ssh_choice - 1))
            cfg_SSH_KEY="${_SSH_KEYS[$idx]/#$HOME/~}"
            _ok "Selected: ${cfg_SSH_KEY}"
        else
            _dim "Skipping SSH setup."
        fi
    else
        _dim "No SSH keys found in ~/.ssh/"
        _dim "Git push over SSH won't be available."
    fi

    # SSH host alias (only if a key was selected)
    if [ -n "$cfg_SSH_KEY" ]; then
        echo ""
        _dim "SSH host alias (optional, e.g., github-work=github.com)"
        _prompt_value "SSH_HOST" "${SSH_HOST:-}" cfg_SSH_HOST
    fi

    # --- Phase 8: Extra env vars --------------------------------------------

    _header "Extra Environment Variables"

    local cfg_EXTRA_ENV=""
    _dim "Space-separated variable names to pass into the container."
    _dim "Example: MY_TOKEN CUSTOM_VAR"
    _prompt_value "EXTRA_ENV" "${EXTRA_ENV:-}" cfg_EXTRA_ENV

    # --- Phase 9: summary & confirm -----------------------------------------

    _header "Configuration Summary"

    echo ""
    local summary=""

    _add_line() { summary+="$1"$'\n'; }

    _add_line "# cage configuration — generated by 'cage setup'"
    _add_line "# Re-run 'cage setup' to modify."
    _add_line ""

    _add_line "# Default tool: claude or codex"
    if [ "$cfg_CAGE_DEFAULT" = "claude" ]; then
        _add_line "# CAGE_DEFAULT=claude"
    else
        _add_line "CAGE_DEFAULT=$cfg_CAGE_DEFAULT"
    fi
    _add_line ""

    if [ "$USE_CLAUDE" -eq 1 ]; then
        _add_line "# Claude Code auth: bedrock or api-key"
        if [ "$cfg_CLAUDE_AUTH" = "bedrock" ]; then
            _add_line "CLAUDE_AUTH=bedrock"
            _add_line "AWS_PROFILE=$cfg_AWS_PROFILE"
            _add_line "AWS_REGION=$cfg_AWS_REGION"
        else
            _add_line "CLAUDE_AUTH=api-key"
            _add_line "# AWS_PROFILE="
            _add_line "# AWS_REGION="
        fi
        _add_line ""
    fi

    if [ "$USE_CODEX" -eq 1 ]; then
        _add_line "# Codex CLI auth"
        if [ "$cfg_CODEX_COPY_AUTH" = "0" ]; then
            _add_line "CODEX_COPY_AUTH=0"
        else
            _add_line "# CODEX_COPY_AUTH=1"
        fi
        _add_line ""
    fi

    _add_line "# Git identity"
    [ -n "$cfg_GIT_USER_NAME" ]  && _add_line "GIT_USER_NAME=\"$cfg_GIT_USER_NAME\""   || _add_line "# GIT_USER_NAME="
    [ -n "$cfg_GIT_USER_EMAIL" ] && _add_line "GIT_USER_EMAIL=\"$cfg_GIT_USER_EMAIL\"" || _add_line "# GIT_USER_EMAIL="
    _add_line ""

    _add_line "# SSH key for git push (must be unencrypted)"
    [ -n "$cfg_SSH_KEY" ]  && _add_line "SSH_KEY=\"$cfg_SSH_KEY\""   || _add_line "# SSH_KEY="
    [ -n "$cfg_SSH_HOST" ] && _add_line "SSH_HOST=\"$cfg_SSH_HOST\"" || _add_line "# SSH_HOST="
    _add_line ""

    _add_line "# Extra env vars to pass through (space-separated names)"
    [ -n "$cfg_EXTRA_ENV" ] && _add_line "EXTRA_ENV=\"$cfg_EXTRA_ENV\"" || _add_line "# EXTRA_ENV="
    _add_line ""

    _add_line "# GitHub CLI (opt-in, gives container access to PRs/issues)"
    if [ "$cfg_GH_AUTH" = "1" ]; then
        _add_line "GH_AUTH=1"
        [ -n "$cfg_GH_ACCOUNT" ] && _add_line "GH_ACCOUNT=$cfg_GH_ACCOUNT" || _add_line "# GH_ACCOUNT="
    else
        _add_line "# GH_AUTH=1"
        _add_line "# GH_ACCOUNT="
    fi

    # Display summary
    echo "$summary" | while IFS= read -r line; do
        echo "    $line"
    done
    echo ""
    echo "  Config file: ${CYAN}${CONF_FILE}${RESET}"
    echo ""

    if ! _prompt_yn "Write this configuration?" "Y"; then
        echo "  Cancelled — no changes written."
        return 0
    fi

    # --- Phase 10: write config ----------------------------------------------

    mkdir -p "$(dirname "$CONF_FILE")"
    printf '%s' "$summary" > "$CONF_FILE"

    echo ""
    _ok "Config written to ${CONF_FILE}"
    echo ""
    echo "  Next steps:"
    echo "    cage ~/path/to/repo           # run ${cfg_CAGE_DEFAULT:-claude}"
    if [ "$USE_CLAUDE" -eq 1 ] && [ "$USE_CODEX" -eq 1 ]; then
        echo "    cage codex ~/path/to/repo     # run Codex CLI"
    fi
    echo "    cage -y ~/path/to/repo        # yolo mode (skip prompts)"
    echo ""
}

# Run the wizard
_run_setup
