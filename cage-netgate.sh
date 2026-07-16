#!/bin/bash
# cage netgate — manage domain allow/deny lists for --net gate mode.
# Sourced from the cage script; expects SCRIPT_DIR to be set.

NETGATE_CONFIG_DIR="$HOME/.claude/netgate"
NETGATE_DEFAULTS="$SCRIPT_DIR/netgate/defaults.json"

_netgate_usage() {
    echo "Usage: cage netgate [list|allow|deny|remove|reset] [...]"
    echo ""
    echo "Commands:"
    echo "  cage netgate [list] [PATH]            Show rules in effect"
    echo "  cage netgate allow DOMAIN [--global]   Allow a domain"
    echo "  cage netgate deny DOMAIN [PATH]        Deny a domain (project-only)"
    echo "  cage netgate remove DOMAIN [--global]  Remove a decision (re-enables prompting)"
    echo "  cage netgate reset [PATH] [--global]   Delete all decisions"
    echo ""
    echo "PATH defaults to current directory for project-specific rules."
    echo "Files: ~/.claude/netgate/{global,project-HASH}.json"
}

# --- Helpers ---

_netgate_repo_hash() {
    local repo_path
    repo_path="$(cd "$1" 2>/dev/null && pwd -P)" || {
        echo "Directory not found: $1" >&2
        return 1
    }
    if command -v md5 &>/dev/null; then
        echo -n "$repo_path" | md5 -q | cut -c1-8
    else
        echo -n "$repo_path" | md5sum | cut -c1-8
    fi
}

_netgate_read_json() {
    local file="$1"
    [ -f "$file" ] || return 0
    python3 -I -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    for d in data.get('domains', []): print('domain:' + d)
    for d in data.get('denied', []): print('denied:' + d)
except (json.JSONDecodeError, OSError):
    pass
" "$file"
}

_netgate_add_entry() {
    local file="$1" key="$2" domain="$3"
    python3 -I -c "
import json, os, sys
path, key, domain = sys.argv[1], sys.argv[2], sys.argv[3]
data = {}
if os.path.isfile(path):
    try:
        with open(path) as f: data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
entries = data.setdefault(key, [])
if domain in entries:
    sys.exit(1)
entries.append(domain)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
" "$file" "$key" "$domain"
}

_netgate_remove_entry() {
    local file="$1" domain="$2"
    [ -f "$file" ] || return 1
    python3 -I -c "
import json, sys
path, domain = sys.argv[1], sys.argv[2]
try:
    with open(path) as f: data = json.load(f)
except (json.JSONDecodeError, OSError):
    sys.exit(1)
found = False
for key in ('domains', 'denied'):
    if domain in data.get(key, []):
        data[key].remove(domain)
        found = True
if not found:
    sys.exit(1)
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
" "$file" "$domain"
}

_netgate_remove_from_denied() {
    local file="$1" domain="$2"
    [ -f "$file" ] || return 0
    python3 -I -c "
import json, sys
path, domain = sys.argv[1], sys.argv[2]
try:
    with open(path) as f: data = json.load(f)
except (json.JSONDecodeError, OSError):
    sys.exit(0)
if domain in data.get('denied', []):
    data['denied'].remove(domain)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
" "$file" "$domain"
}

# --- Subcommands ---

_netgate_list() {
    local repo_path="${1:-}"

    # Defaults
    echo "Defaults (shipped):"
    if [ -f "$NETGATE_DEFAULTS" ]; then
        local has_defaults=0
        while IFS= read -r line; do
            case "$line" in
                domain:*) echo "  allow  ${line#domain:}"; has_defaults=1 ;;
            esac
        done < <(_netgate_read_json "$NETGATE_DEFAULTS")
        [ "$has_defaults" -eq 0 ] && echo "  (none)" || true
    else
        echo "  (file not found)"
    fi

    # Global
    local global_file="$NETGATE_CONFIG_DIR/global.json"
    echo ""
    echo "Global ($global_file):"
    if [ -f "$global_file" ]; then
        local has_global=0
        while IFS= read -r line; do
            case "$line" in
                domain:*) echo "  allow  ${line#domain:}"; has_global=1 ;;
                denied:*) echo "  deny   ${line#denied:}"; has_global=1 ;;
            esac
        done < <(_netgate_read_json "$global_file")
        [ "$has_global" -eq 0 ] && echo "  (empty)" || true
    else
        echo "  (no file)"
    fi

    # Project
    if [ -n "$repo_path" ]; then
        local hash
        hash="$(_netgate_repo_hash "$repo_path")" || return 1
        local project_file="$NETGATE_CONFIG_DIR/project-${hash}.json"
        echo ""
        echo "Project: $repo_path (project-${hash}.json):"
        if [ -f "$project_file" ]; then
            local has_project=0
            while IFS= read -r line; do
                case "$line" in
                    domain:*) echo "  allow  ${line#domain:}"; has_project=1 ;;
                    denied:*) echo "  deny   ${line#denied:}"; has_project=1 ;;
                esac
            done < <(_netgate_read_json "$project_file")
            [ "$has_project" -eq 0 ] && echo "  (empty)" || true
        else
            echo "  (no file)"
        fi
    fi
}

_netgate_allow() {
    local domain="" global=0 repo_path=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --global) global=1; shift ;;
            -*) echo "Unknown option: $1" >&2; return 1 ;;
            *)
                if [ -z "$domain" ]; then
                    domain="$1"
                else
                    repo_path="$1"
                fi
                shift
                ;;
        esac
    done

    if [ -z "$domain" ]; then
        echo "Usage: cage netgate allow DOMAIN [PATH] [--global]" >&2
        return 1
    fi

    if [ "$global" -eq 1 ]; then
        local file="$NETGATE_CONFIG_DIR/global.json"
        if _netgate_add_entry "$file" "domains" "$domain"; then
            echo "Added '$domain' to global allowlist"
        else
            echo "'$domain' is already in global allowlist"
        fi
        _netgate_remove_from_denied "$file" "$domain"
    else
        repo_path="${repo_path:-$(pwd -P)}"
        local hash
        hash="$(_netgate_repo_hash "$repo_path")" || return 1
        local file="$NETGATE_CONFIG_DIR/project-${hash}.json"
        if _netgate_add_entry "$file" "domains" "$domain"; then
            echo "Added '$domain' to project allowlist ($repo_path)"
        else
            echo "'$domain' is already in project allowlist"
        fi
        _netgate_remove_from_denied "$file" "$domain"
    fi
}

_netgate_deny() {
    local domain="" repo_path=""

    while [ $# -gt 0 ]; do
        case "$1" in
            -*) echo "Unknown option: $1" >&2; return 1 ;;
            *)
                if [ -z "$domain" ]; then
                    domain="$1"
                else
                    repo_path="$1"
                fi
                shift
                ;;
        esac
    done

    if [ -z "$domain" ]; then
        echo "Usage: cage netgate deny DOMAIN [PATH]" >&2
        return 1
    fi

    repo_path="${repo_path:-$(pwd -P)}"
    local hash
    hash="$(_netgate_repo_hash "$repo_path")" || return 1
    local file="$NETGATE_CONFIG_DIR/project-${hash}.json"

    if _netgate_add_entry "$file" "denied" "$domain"; then
        echo "Added '$domain' to project denylist ($repo_path)"
    else
        echo "'$domain' is already in project denylist"
    fi
}

_netgate_remove() {
    local domain="" global=0 repo_path=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --global) global=1; shift ;;
            -*) echo "Unknown option: $1" >&2; return 1 ;;
            *)
                if [ -z "$domain" ]; then
                    domain="$1"
                else
                    repo_path="$1"
                fi
                shift
                ;;
        esac
    done

    if [ -z "$domain" ]; then
        echo "Usage: cage netgate remove DOMAIN [PATH] [--global]" >&2
        return 1
    fi

    if [ "$global" -eq 1 ]; then
        local file="$NETGATE_CONFIG_DIR/global.json"
        if _netgate_remove_entry "$file" "$domain"; then
            echo "Removed '$domain' from global lists"
        else
            echo "'$domain' not found in global lists" >&2
            return 1
        fi
    else
        repo_path="${repo_path:-$(pwd -P)}"
        local hash
        hash="$(_netgate_repo_hash "$repo_path")" || return 1
        local file="$NETGATE_CONFIG_DIR/project-${hash}.json"
        if _netgate_remove_entry "$file" "$domain"; then
            echo "Removed '$domain' from project lists ($repo_path)"
        else
            echo "'$domain' not found in project lists" >&2
            return 1
        fi
    fi
}

_netgate_reset() {
    local global=0 repo_path=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --global) global=1; shift ;;
            -*) echo "Unknown option: $1" >&2; return 1 ;;
            *) repo_path="$1"; shift ;;
        esac
    done

    if [ "$global" -eq 1 ]; then
        local file="$NETGATE_CONFIG_DIR/global.json"
        if [ ! -f "$file" ]; then
            echo "No global netgate file to reset."
            return 0
        fi
        if [ -t 0 ]; then
            read -rp "Delete $file? [y/N]: " confirm
            case "$confirm" in
                [Yy]*) ;;
                *) echo "Aborted."; return 0 ;;
            esac
        fi
        rm -f "$file"
        echo "Reset global netgate decisions."
    else
        repo_path="${repo_path:-$(pwd -P)}"
        local hash
        hash="$(_netgate_repo_hash "$repo_path")" || return 1
        local file="$NETGATE_CONFIG_DIR/project-${hash}.json"
        if [ ! -f "$file" ]; then
            echo "No project netgate file for $repo_path."
            return 0
        fi
        if [ -t 0 ]; then
            read -rp "Delete project decisions for $repo_path? [y/N]: " confirm
            case "$confirm" in
                [Yy]*) ;;
                *) echo "Aborted."; return 0 ;;
            esac
        fi
        rm -f "$file"
        echo "Reset project netgate decisions for $repo_path."
    fi
}

# --- Main dispatch ---

case "${1:-}" in
    list)
        shift
        _netgate_list "${1:-}"
        ;;
    allow)
        shift
        _netgate_allow "$@"
        ;;
    deny)
        shift
        _netgate_deny "$@"
        ;;
    remove)
        shift
        _netgate_remove "$@"
        ;;
    reset)
        shift
        _netgate_reset "$@"
        ;;
    --help|-h)
        _netgate_usage
        ;;
    "")
        _netgate_list ""
        ;;
    *)
        # If the arg is a directory, treat as: list PATH
        if [ -d "$1" ]; then
            _netgate_list "$1"
        else
            echo "Unknown netgate command: $1" >&2
            _netgate_usage >&2
            exit 1
        fi
        ;;
esac
