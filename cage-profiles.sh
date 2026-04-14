#!/bin/bash
# cage profiles — list, show, set, and reset profile/folder mappings.
# Sourced from the cage script; expects CAGE_CONFIG_DIR to be set.

PROFILES_DIR="$CAGE_CONFIG_DIR/profiles"
FOLDER_PROFILES_FILE="$CAGE_CONFIG_DIR/folder-profiles"

_profiles_usage() {
    echo "Usage: cage profiles [show|set|reset] [...]"
    echo ""
    echo "Commands:"
    echo "  cage profiles                  List profiles and folder mappings"
    echo "  cage profiles show NAME        Show a profile's configuration"
    echo "  cage profiles set PATH NAME    Assign a profile to a folder"
    echo "  cage profiles reset PATH       Remove a folder's profile mapping"
}

_profiles_list() {
    if [ ! -d "$PROFILES_DIR" ] || ! ls "$PROFILES_DIR"/*.conf &>/dev/null; then
        echo "No profiles found. Create one with: cage setup --profile <name>"
        return 0
    fi

    echo "Profiles:"
    for f in "$PROFILES_DIR"/*.conf; do
        [ -f "$f" ] || continue
        echo "  $(basename "${f%.conf}")"
    done

    if [ -f "$FOLDER_PROFILES_FILE" ] && [ -s "$FOLDER_PROFILES_FILE" ]; then
        echo ""
        echo "Folder mappings:"
        while IFS='=' read -r path profile; do
            [ -z "$path" ] && continue
            if [ "$profile" = "_none_" ]; then
                echo "  $path -> (none)"
            else
                echo "  $path -> $profile"
            fi
        done < "$FOLDER_PROFILES_FILE"
    fi
}

_profiles_show() {
    local name="$1"
    local file="$PROFILES_DIR/${name}.conf"
    if [ ! -f "$file" ]; then
        echo "Profile '$name' not found." >&2
        echo "Available profiles:" >&2
        _profiles_list_names >&2
        return 1
    fi
    cat "$file"
}

_profiles_list_names() {
    if [ -d "$PROFILES_DIR" ]; then
        for f in "$PROFILES_DIR"/*.conf; do
            [ -f "$f" ] || continue
            echo "  $(basename "${f%.conf}")"
        done
    fi
}

_profiles_set() {
    local target_path="$1" profile_name="$2"

    # Validate profile name
    if ! [[ "$profile_name" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        echo "Invalid profile name: '$profile_name' (use only letters, digits, hyphens, underscores)" >&2
        return 1
    fi

    # Verify profile exists
    if [ ! -f "$PROFILES_DIR/${profile_name}.conf" ]; then
        echo "Profile '$profile_name' not found." >&2
        _profiles_list_names >&2
        return 1
    fi

    # Resolve to absolute path
    target_path="$(cd "$target_path" 2>/dev/null && pwd -P)" || {
        echo "Directory not found: $target_path" >&2
        return 1
    }

    # Remove existing mapping for this path, then append new one
    _remove_mapping "$target_path"
    mkdir -p "$CAGE_CONFIG_DIR"
    echo "${target_path}=${profile_name}" >> "$FOLDER_PROFILES_FILE"
    echo "Set profile '$profile_name' for $target_path"
}

_profiles_reset() {
    local target_path="$1"

    # Resolve to absolute path
    target_path="$(cd "$target_path" 2>/dev/null && pwd -P)" || {
        echo "Directory not found: $target_path" >&2
        return 1
    }

    if [ ! -f "$FOLDER_PROFILES_FILE" ]; then
        echo "No folder mappings found."
        return 0
    fi

    _remove_mapping "$target_path"
    echo "Reset profile mapping for $target_path"
}

_remove_mapping() {
    local target_path="$1"
    [ -f "$FOLDER_PROFILES_FILE" ] || return 0
    local tmp
    tmp="$(mktemp)"
    grep -v "^${target_path}=" "$FOLDER_PROFILES_FILE" > "$tmp" 2>/dev/null || true
    mv "$tmp" "$FOLDER_PROFILES_FILE"
}

# --- Main dispatch ---

case "${1:-}" in
    show)
        if [ -z "${2:-}" ]; then
            echo "Usage: cage profiles show NAME" >&2
            exit 1
        fi
        _profiles_show "$2"
        ;;
    set)
        if [ -z "${2:-}" ] || [ -z "${3:-}" ]; then
            echo "Usage: cage profiles set PATH NAME" >&2
            exit 1
        fi
        _profiles_set "$2" "$3"
        ;;
    reset)
        if [ -z "${2:-}" ]; then
            echo "Usage: cage profiles reset PATH" >&2
            exit 1
        fi
        _profiles_reset "$2"
        ;;
    --help|-h)
        _profiles_usage
        ;;
    "")
        _profiles_list
        ;;
    *)
        echo "Unknown profiles command: $1" >&2
        _profiles_usage >&2
        exit 1
        ;;
esac
