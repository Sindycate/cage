"""Compatibility API for Cage central configuration.

Implementation lives in :mod:`cage_core.configuration`. These explicit
re-exports preserve existing callers while dependencies remain owned by their
individual modules. Tests should patch the module that performs the operation.
"""

import sys
from pathlib import Path

# Preserve direct isolated execution as well as package imports. Trust only the
# resolved installation root, never the working directory or PYTHONPATH.
_INSTALL_ROOT = Path(__file__).resolve().parents[1]
if str(_INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(_INSTALL_ROOT))

from cage_core.models import ContractError, ResolvedConfig, StoragePolicy
from cage_core.state import SyncError
from cage_core import bridge as bridge_policy, codex_policy, codex_runtime

from cage_core.configuration.schema import (
    AUTH_KEYS,
    CODEX_PROFILE_RE,
    ConfigError,
    DEFAULT_KEYS,
    EDITABLE_COLLECTIONS,
    ENV_RE,
    HEADER_ENV_REF_RE,
    HOST_COMMAND_KEYS,
    HTTP_HEADER_NAME_RE,
    IDENTITY_KEYS,
    MCP_PACK_KEYS,
    MCP_SERVER_KEYS,
    NAME_RE,
    PRESET_KEYS,
    SKILL_NAME_RE,
    SKILL_PACK_KEYS,
    STORAGE_KEYS,
    TOP_LEVEL_KEYS,
    TRANSPORT_NAME_RE,
    VALID_EXEC_TARGETS,
    VALID_TOOLS,
    as_list,
    as_str_list,
    as_table,
    bool_to_flag,
    collect_env,
    dedupe,
    is_sensitive_header_name,
    optional_str,
    parse_config_text,
    reject_unknown_keys,
    require_env_name,
    require_name,
    require_skill_name,
    require_transport_name,
    storage_policy_from_config,
    transport_key,
    validate_headers,
    validate_named_table,
    validate_schema,
)

from cage_core.configuration.selection import (
    default_net,
    default_session_sync,
    effective_exec_state,
    expand_path_string,
    matching_auth_names,
    normalize_project_path,
    preset_tool,
    select_project_preset,
    selected_seed_preset,
    str_list_or_empty,
    valid_named_tables,
)

from cage_core.configuration.storage import (
    atomic_write_text,
    config_destination,
    config_write_lock,
    create_config_backup,
    default_config_path,
    load_config,
    load_ui_request,
    sha256_text,
)

from cage_core.configuration.rendering import (
    PROJECTS_TABLE_RE,
    TABLE_RE,
    parse_header_path,
    render_config_changes,
    render_table,
    replace_projects_section,
    replace_table,
    replace_top_level_value,
    table_spans,
    toml_key,
    toml_quote,
    toml_value,
)

from cage_core.configuration.editing import (
    affected_preset_names,
    apply_ui_operations,
    hidden_project_preset_name,
    referenced_by,
    update_references,
    validate_affected_presets,
    validate_references,
)

from cage_core.configuration.interaction import (
    InteractiveSelections,
    TtyIO,
    build_interactive_preset,
    host_command_label,
    interactive_select,
    mcp_pack_label,
    open_tty,
    prompt_multi,
    prompt_single,
    prompt_yes_no,
    resolve_interactive_selection,
    skill_pack_label,
)

from cage_core.configuration.codex import (
    CODEX_INVENTORY_TIMEOUT,
    MAX_CODEX_CONFIG_BYTES,
    MAX_CODEX_INVENTORY_BYTES,
    SAFE_CODEX_PASSTHROUGH_CONFIG_ROOTS,
    codex_key_segment,
    codex_mcp_inventory_enabled,
    codex_mcp_overrides,
    config_override_root,
    host_codex_arg_lines,
    host_codex_payload_for,
    mcp_disable_override,
    mcp_disable_plan,
    mcp_server_transports_in_toml,
    merge_mcp_transport_maps,
    pin_host_executable,
    reject_unsafe_codex_passthrough_args,
    selected_mcp_names_in_file,
    toml_string,
    toml_string_array,
    validate_codex_layers,
)

from cage_core.configuration.resolution import (
    resolve_config,
)

from cage_core.configuration.diagnostics import (
    emit_resolved_json,
    explain,
    format_server,
    host_github_token_available,
)

from cage_core.configuration.oauth import (
    codex_auth_source_for_monitor,
    command_mcp_auth,
    command_mcp_login,
    command_mcp_logout,
    selected_codex_oauth_mcp_server_for_auth,
    selected_oauth_mcp_server,
)

from cage_core.configuration.ui import (
    command_ui_commit,
    command_ui_export,
    command_ui_preview,
    command_ui_resolve_json,
    resolve_ui_result,
    ui_summary,
)

from cage_core.configuration.cli import (
    SAMPLE_CONFIG,
    build_parser,
    command_default_tool,
    command_doctor,
    command_edit,
    command_explain,
    command_host_codex_args,
    command_init,
    command_list,
    command_resolve_json,
    command_set_project,
    command_show,
    command_validate_codex_argv,
    command_validate_codex_layers,
    main,
)


if __name__ == "__main__":
    raise SystemExit(main())
