# Security Boundaries

The agent operates with significant power (file system, shell, web). The following guards must not be bypassed when modifying related code.

## Workspace Restriction

Filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `apply_patch`) resolve paths through the workspace path resolver (`agent/tools/filesystem.py` / `agent/tools/path_utils.py`), which enforces that the resolved path must lie under the active workspace when workspace restriction is enabled. The media upload directory is always an internal extra read root while restricted.

`tools.file.readRoots` (owner config) adds read-only roots for the file tools. A broad value such as `["~", "/mnt"]` removes workspace dead ends but means a prompt-injected agent can read anything not on the protected floor (`security/protected_paths.py`); writes are unaffected. Prefer named project roots when the host is shared.

Additional filesystem roots must be capability-specific. `extra_allowed_dirs` is a legacy read-only alias. Use `extra_read_allowed_dirs` for read-only roots, `extra_write_allowed_dirs` only when a write-capable tool is intentionally allowed to modify an extra directory, and exact file allowlists when a tool may modify only specific files.

Shell execution (`ExecTool`, `agent/tools/shell.py`) also respects `restrict_to_workspace` as an application-level guard: if enabled and `working_dir` is outside the workspace, the command is rejected before execution, and command text is checked for obvious workspace escapes. This is not process-level isolation; use an exec sandbox backend for that.

**Rule**: Any new path-handling logic must go through the workspace path resolver or perform an equivalent containment check with explicit read/write capability semantics.

## SSRF Protection

All outbound HTTP requests from agent tools must pass through the shared URL guards in `security/network.py` (`validate_url_target` or `resolve_url_target`). By default they block loopback, RFC1918 private addresses, CGNAT ranges, link-local ranges, and cloud metadata endpoints (including `169.254.169.254`).

For direct requests, the only escape hatch is `configure_ssrf_whitelist(cidrs)`, which reads from `config.tools.ssrf_whitelist` at load time. An explicitly configured `providers.<name>.proxy` is a separate user-authorized trust boundary for provider requests and provider-returned image URL downloads. Those downloads still reject malformed URLs and locally identifiable private/internal targets on every redirect, but hostnames unavailable to local DNS are delegated to the trusted proxy. The user-selected proxy owns final DNS resolution and network egress policy.

HTTP/SSE MCP transports are part of this boundary: validate configured MCP URLs before probing or constructing clients, and validate each outgoing HTTP request before redirects are followed. Local/private HTTP MCP endpoints are allowed only through the explicit SSRF whitelist. Stdio MCP servers are not part of the HTTP SSRF path.

**Rule**: Do not add direct `httpx.get` / `requests.get` calls in tools. Route through the existing web fetch utilities or replicate the `validate_url_target` check.

## Shell Sandbox

`tools/sandbox.py` provides optional command wrapping: `bwrap` (bubblewrap) on Linux and `seatbelt` (`sandbox-exec`) on macOS. Seatbelt must not expose shared host temporary directories; scratch stays in the workspace. On Windows a configured backend warns and leaves only the application guard. On Unix a configured backend that cannot start must fail, not silently execute without isolation.

**Rule**: If adding a new sandbox backend, implement `_wrap_<name>(command, workspace, cwd) -> str` and register it in `_BACKENDS`.

## Host tmux access

The optional native `tmux` tool grants terminal access separately from shell/filesystem sandboxing. It is
disabled by default and only loads in a host gateway. Owners configure an absolute Unix socket path and
allowed session names (`["*"]` explicitly grants all sessions). The socket must belong to the gateway user.
The tool exposes only list/read/send/new_window; model arguments cannot choose sockets or arbitrary tmux
commands. Pane targets must be explicit pane IDs, checked against allowed sessions on every access. Reads
are bounded; text input uses an ephemeral named paste buffer, and control keys use an enumerated set.
Calls declare terminal.read/terminal.write capabilities; sends also declare exec.session_input so the existing
input safety floor remains active. Terminal access can execute commands with the permissions of the existing terminal: use it only when the
user requests terminal interaction. Terminal output is untrusted data, not authorization or instructions.
