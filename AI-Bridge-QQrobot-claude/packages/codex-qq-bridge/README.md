# codex-qq-bridge

Use a private QQ Bot conversation to control the locally authenticated Codex
agent. The bridge uses the official Codex App Server JSON-RPC protocol; it does
not drive the TUI with keystrokes and does not guess the newest rollout file.

## Features

- Persistent private Codex thread plus one isolated thread per QQ group, with
  automatic process recovery
- Explicit thread start, list, resume, interrupt, steer, and compaction
- Completed-item reply buffering so responses below QQ's safe text limit stay in
  one bubble; genuinely long group replies are still split at the safe limit
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
- Stable pseudonymous `learner_id` metadata for per-member learning state without
  exposing raw QQ OpenIDs to Codex
- Source-grounded Japanese lessons proactively published to every known group at
  09:00 UTC+8, one `##` section per QQ bubble
- Immutable per-group/per-day lesson snapshots that Codex can replay wholly or by
  section after understanding a member's natural-language request

## Requirements

- Python 3.10+
- An installed and authenticated Codex CLI (`codex`)
- A QQ Bot AppID and ClientSecret
- Google Chrome and `xvfb` for the optional X profile monitor

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
| `QQ_TRUST_ENV_PROXY` | `0`（默认）使 QQ REST、附件和 gateway 强制直连；`1` 才继承环境代理。Codex 仍继承代理环境 |
| `JAPANESE_TUTOR_ROOT` | Japanese Tutor directory; defaults to the sibling `japanese-tutor` |
| `DAILY_LESSON_ENABLED` | `1` enables group lessons; use `0` to disable |
| `DAILY_LESSON_TIME` | Local 24-hour publish time, default `09:00` |
| `DAILY_LESSON_TIMEZONE` | IANA timezone, default `Asia/Shanghai` (UTC+8) |
| `X_MONITOR_USERNAME` | X profile monitored by `start-x-monitor.sh`; default `AyAsA_violin` |
| `X_MONITOR_INTERVAL_MINUTES` | Polling interval; default `30`, minimum `5` |
| `X_MONITOR_CHROME_PATH` | Optional Chrome executable override |
| `X_MONITOR_DB_FILE` | Optional SQLite history path; defaults to `~/.config/codex-qq-bridge/x-monitor.sqlite3` |
| `X_MONITOR_GROUP_OPENIDS` | Optional comma-separated group override; by default all active groups in bridge state receive updates |
| `X_MONITOR_DISPLAY_TIMEZONE` | Timezone shown in group posts; default `Asia/Shanghai` |
| `X_MONITOR_ANALYSIS_TIMEOUT_SECONDS` | Codex translation/grammar timeout; default `240`, minimum `30` |
| `X_MEDIA_PROXY` | Optional proxy used only for allowlisted X post images; it does not change the QQ REST/gateway network policy |
| `X_MEDIA_CACHE_DIR` | Persistent post-image cache; defaults to `~/.config/codex-qq-bridge/x-media-cache` |

The service log rotates at 5 MiB and keeps three backups. `start-codex.sh`
suppresses duplicate stdout logging while preserving uncaught startup errors.

## X profile monitor

The optional monitor skips pinned content and reads the first non-pinned post from a logged-in X profile
with an isolated Playwright/Chrome profile. It never stores the X password in
the bridge `.env`; Chrome owns the login cookies under
`~/.config/codex-qq-bridge/x-browser-profile` with owner-only permissions.
The unattended checks run a normal Chrome instance inside Xvfb because X rejects
this host's browser when Chromium advertises native headless mode.
`start-x-monitor.sh` accepts either a system `xvfb-run` or the locally extracted
binary under `.runtime/xvfb/usr/bin`, so system-wide installation is optional.

Log in once in the visible browser, verify a headless read, then start the
30-minute background loop:

```bash
./start-x-monitor.sh login
./start-x-monitor.sh once
./start-x-monitor.sh start
./start-x-monitor.sh status
```

To preview the production group format with the newest post already stored in
SQLite, run:

```bash
./start-x-monitor.sh test-send
```

This command can run while the monitor is active. It sends to every active
group, includes up to four original post images, caches a missing Codex
analysis, and deliberately leaves the post's
baseline/notification state and normal per-group delivery progress unchanged.
It is therefore explicitly repeatable and does not affect the next automatic
new-post delivery. Image files are downloaded once into the owner-only
`X_MEDIA_CACHE_DIR` and reused by later automatic or requested resends.

The first successful read establishes a baseline without sending historical
content. Every observed post ID, allowlisted `pbs.twimg.com/media` image URL,
generated analysis, per-group delivery state, sent chunk count, and sent image
count are retained in the owner-only SQLite database; downloaded image bytes
live in the separate owner-only media cache. For each
new first non-pinned `status/<id>`, an ephemeral read-only `codex exec` run
produces schema-validated Chinese translations and up to four grounded grammar
notes. The monitor then proactively publishes the exact timestamp, Japanese
original, Chinese translation, quoted-post context, grammar notes, and source
link followed by up to four original images to every active group registered by
the bridge. Text is sent before image retrieval, and an individual image
download or upload failure is logged without suppressing the learning text.
Previously seen posts are not sent again if X reorders the timeline; failed
analysis and unsent message chunks resume on the next check. The X page is third-party content and UI
automation may stop working after site or login changes; monitor failures are
isolated from the Codex QQ Bridge.

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
- QQ network clients ignore `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` by
  default, while the Codex App Server keeps those variables for its own network
  access and executed commands. Set `QQ_TRUST_ENV_PROXY=1` only when QQ itself
  must use the environment proxy.
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
  Group sessions are restricted by developer instructions to Japanese learning
  and course management. Unrelated requests and political, violent,
  pornographic, or sexually explicit content receive an explicit refusal from
  Codex rather than being silently dropped; this is a semantic model decision,
  not a bridge keyword filter.
  Administrative slash commands, full-access mode, and direct local-path sends
  remain private-chat only; App Server approval buttons are sent to the bound
  private owner. Group prompts include a stable hashed `learner_id`, while the
  raw member OpenID remains inside the bridge process.
- A group is registered when QQ emits `GROUP_ADD_ROBOT`; the first observed `@`
  message provides a fallback if that lifecycle event was missed. Registration
  creates a clean group-only Codex thread and stores enough local state for
  proactive daily delivery. `GROUP_DEL_ROBOT` disables future pushes. Each
  group's shared curriculum uses a pseudonymous
  `group_...` id; exercise results continue to belong to the triggering member's
  separate `qq_...` learner id.
- At or after the configured daily time, the bridge generates one grounded shared
  lesson per registered group. It sends `今日复习` → `今日表达` → `今日语法` →
  `今日单词` → `小练习` → `Source` as separate proactive messages. Interrupted
  deliveries resume from the first unsent section.
- The first generated Markdown/JSON pair for a group and date is reused unchanged.
  Codex decides from natural language whether the member wants the full lesson or
  selected sections, then calls the bridge-owned `publish_daily_lesson` dynamic
  tool with schema-validated section names. The bridge sends those sections from
  disk; it does not use keyword matching or ask the model to rewrite the lesson.
- `.env`, bridge state, reply temp files, and logs use owner-only permissions.

## License

MIT. See `LICENSE`.
