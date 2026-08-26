# codex-qq-bridge

Use a private QQ Bot conversation to control the locally authenticated Codex
agent. The bridge uses the official Codex App Server JSON-RPC protocol; it does
not drive the TUI with keystrokes and does not guess the newest rollout file.

## Features

- Persistent Codex threads with automatic process recovery
- Explicit thread start, list, resume, interrupt, steer, and compaction
- Throttled incremental Codex replies with final-message deduplication
- Token usage delivered to QQ
- QQ approval buttons for commands, file changes, and additional permissions
- Safe default: `workspace-write` plus `on-request` approvals
- One-time `MASTER_OPENID` binding; another sender cannot replace the owner
- `/cd`, `/pwd`, and `/ls` workspace management
- Safely downloaded inline image/audio inputs and file-link fallback
- Local image/file upload to QQ and long-reply TXT fallback
- Independent ephemeral `/btw` side threads
- QQ WebSocket heartbeat, reconnect backoff, proxy detection, and deduplication
- Group `@` messages from any member, with replies quoted back to the same group
  and prefixed with the triggering member's display name

## Requirements

- Python 3.10+
- An installed and authenticated Codex CLI (`codex`)
- A QQ Bot AppID and ClientSecret

Install from the repository:

```bash
./setup-codex.sh
```

Copy `.env.example` to the repository root as `.env`, or run:

```bash
.venv-codex/bin/codex-qq-bridge --init
```

Do not commit `.env`. If `MASTER_OPENID` is empty, the first private QQ sender
is permanently bound as owner until the operator edits `.env` locally.

## Configuration

| Variable | Description |
|---|---|
| `APP_ID` | QQ Bot AppID |
| `CLIENT_SECRET` | QQ Bot client secret |
| `MASTER_OPENID` | Allowed QQ user; empty enables first-user binding |
| `CODEX_CWD` | Initial absolute Codex workspace path |
| `CODEX_BIN` | Codex executable, default `codex` from `PATH` |
| `CODEX_SANDBOX` | `read-only`, `workspace-write`, or `danger-full-access` |
| `CODEX_APPROVAL_POLICY` | `untrusted`, `on-request`, or `never` |
| `BRIDGE_LOG_DIR` | Optional log directory |
| `CODEX_QQ_STATE_FILE` | Optional persistent bridge-state file |

The service log rotates at 5 MiB and keeps three backups. `start-codex.sh`
suppresses duplicate stdout logging while preserving uncaught startup errors.

## QQ commands

| Command | Effect |
|---|---|
| text | Start a turn; while Codex is working, steer the active turn |
| `/new` | Start a new thread in the current directory |
| `/resume` | List recent threads for the current directory |
| `/resume N` | Resume a listed thread |
| `/stop` | Interrupt the active turn |
| `/cd <path>` | Change directory and start a new thread |
| `/pwd` | Show the working directory |
| `/ls [path]` | List up to 50 non-hidden entries |
| `/context` | Show the latest token usage event |
| `/compact` | Compact the current thread |
| `/btw <question>` | Ask on an ephemeral side thread |
| `/mode status` | Show sandbox and approval policy |
| `/mode safe` | Use workspace-write plus approvals |
| `/mode readonly` | Use a read-only sandbox |
| `/mode full confirm` | Explicitly opt into full filesystem access |
| `/sendimg <path>` | Upload a local image to QQ |
| `/sendfile <path>` | Upload a local file to QQ |

## Security notes

- Keep `CODEX_SANDBOX=workspace-write` unless full access is genuinely needed.
- Full access requires the explicit QQ command `/mode full confirm`.
- Approval requests expire after ten minutes and default to denial.
- The bridge ignores all non-owner messages and interactions after binding.
- App Server events are scoped to the recorded thread ID, preventing replies
  from unrelated local Codex sessions from being sent to QQ.
- Model-generated file markers are restricted to the active workspace. Sending
  a file outside it requires the owner's explicit `/sendfile` command.
- Remote media attachments are restricted to public HTTPS targets, bounded by
  size and MIME type, and converted to App Server-compatible inline data URLs.
  Known QQ CDN hosts may use the proxy-only `198.18.0.0/15` and
  `fdfe:dcba:9876::/48` Fake-IP ranges; ordinary local and private-network
  targets remain blocked.
- Any group member may trigger a normal Codex question after mentioning the bot.
  Administrative slash commands, full-access mode, and direct local-path sends
  remain private-chat only; App Server approval buttons are sent to the bound
  private owner.
- `.env`, bridge state, reply temp files, and logs use owner-only permissions.

## License

MIT. See `LICENSE`.
