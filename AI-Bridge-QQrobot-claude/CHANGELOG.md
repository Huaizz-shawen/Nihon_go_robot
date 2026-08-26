# Changelog

This file is maintained by `scripts/release.py`: on every release, the entries
under `[Unreleased]` are moved to a versioned section and a new version is
created.

Format conventions:

- One `## [x.y.z] - YYYY-MM-DD` section per version;
- Each section uses `### Added / Changed / Fixed / Removed` categories;
- `## [Unreleased]` always sits at the top and records unpublished changes.

## [Unreleased]

## [0.2.0] - 2026-08-26

### Added

- Added a complete Codex QQ Bridge backed by the official Codex App Server
  JSON-RPC protocol, with persistent threads, streaming replies, session
  listing/resume, interruption, steering, compaction, token usage, and
  automatic App Server recovery.
- Added QQ approval buttons for Codex command execution, file changes, and
  permission requests, with timeout-to-deny behavior and current QQ interaction
  payload support.
- Added safe QQ image/audio input handling, local image/file return, long-reply
  TXT fallback, and workspace-scoped model media markers.
- Added QQ group `@` handling: every mentioned group member can trigger Codex,
  and replies return to the original group prefixed with the triggering member.
- Added isolated Codex installation and service scripts (`setup-codex.sh` and
  `start-codex.sh`), package documentation, and regression coverage.
- Long replies are no longer truncated: when a reply is too long for a normal
  QQ message, the complete response is sent as a TXT file.

### Changed

- The project documentation now covers both Claude Code and Codex bridge
  workflows.
- `/btw` answers now return the full text (long answers arrive as a TXT file)
  and no longer interrupt the current task while the question is answered.

### Fixed

- Prevented `MASTER_OPENID` takeover and environment-file injection while
  preserving first-user binding for private chat.
- Scoped App Server events and model-generated file sends to the active thread
  and workspace.
- Fixed QQ CDN image handling behind proxy Fake-IP DNS for both IPv4 and IPv6,
  while continuing to reject ordinary local and private-network targets.
- Fixed approval interaction parsing for current nested `resolved.button_data`
  payloads and made service shutdown clean up the App Server child process.
## [0.1.0] - 2026-08-14

First stable local release. This release turns the QQ Bridge from "runnable"
into "stable and recoverable":

### Fixed

- Fixed `/resume` leaving `PID=None`, which prevented the session from being bound
- Fixed incorrect PID / session binding
- Fixed normal messages and `/btw` panel content being sent to the Bash process by mistake
- Fixed the abnormal Claude state after `/stop`

### Added

- Automatic `--resume` recovery after Claude exits unexpectedly
- The bridge binds only the Claude inside its own tmux pane (pane-scoped PID detection)
- Re-binding of the new PID after automatic recovery
- Unified logging to `~/agent-keep/logs/bridge.log`
- New `~/agent-keep/start.sh` (start / stop / restart / status)

### Changed

- Logs are now written to `logs/bridge.log` instead of being mixed into `nohup.out`
- Session recovery now detects its own tmux pane to avoid binding another Claude
