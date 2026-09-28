# AI-Bridge-QQrobot-claude

> Remote-control your Claude Code or Codex agent from QQ on your phone.

**AI-Bridge-QQrobot-claude** contains bridges between **Claude Code or Codex** and
**QQ**. A local agent stays available on your machine; you talk to it from QQ —
start long tasks, check on them, send files, and approve actions from your phone.

The goal: turn your machine into a personal AI workstation you can drive remotely —
one machine, operated from anywhere.

This project is re-developed based on the ideas and base code of
[zz327455573/agent-keep](https://github.com/zz327455573/agent-keep). Many thanks to
the original author for sharing their work.

## Features

- **Always-on agent** — Claude lives in a tmux session and keeps its context; if it
  exits or crashes, the bridge restarts it automatically and restores the session.
- **Approve from QQ** — permission prompts arrive as QQ buttons; tap `allow`,
  `allow always`, or `deny` to respond.
- **Full control from your phone** — change working directories and permission
  modes, send images and files, and manage sessions.

## QQ commands

| Command | Description |
|---|---|
| `/cd <path>` | Change Claude's working directory |
| `/resume` | List recent sessions; `/resume N` restores session N |
| `/pwd` | Show the current working directory |
| `/ls [path]` | List directory contents |
| `/context` | Show context usage |
| `/compact` | Compress the current conversation |
| `/clear` | Start a brand-new session |
| `/btw <question>` | Ask a side question without interrupting the main flow |
| `/mode` | Switch permission mode (Auto → Accept edits → Plan → Manual) |
| `/stop` | Interrupt the current task |
| `/sendimg <path>` | Send a local image from the bridge host to QQ |
| `/sendfile <path>` | Send a local file from the bridge host to QQ |

## Quick Start

```bash
git clone https://github.com/thelightonmyway/AI-Bridge-QQrobot-claude.git
cd AI-Bridge-QQrobot-claude
./setup.sh <APP_ID> <CLIENT_SECRET>
```

`setup.sh` checks your dependencies, installs the `claude-code-qq-bridge` package,
and writes your QQ bot `APP_ID` and `CLIENT_SECRET` into `~/AI-Bridge-QQrobot-claude/.env`.
Run it with no arguments to get a `.env` template you fill in by hand. Then start the bridge:

```bash
./start.sh start
./start.sh status    # → Bridge running: pid N
```

Send your bot a plain message from QQ — you will get a reply from Claude.

### Codex bridge

The Codex package uses the official local Codex App Server protocol and has its
own isolated installer and service launcher:

```bash
./setup-codex.sh
.venv-codex/bin/codex-qq-bridge --init
./start-codex.sh start
./start-codex.sh status
```

It supports persistent/resumable Codex threads, QQ approval buttons, workspace
switching, interruption, compaction, context usage, attachments, local file
delivery, long-reply files, group `@` replies, and automatic App Server recovery. See
[`packages/codex-qq-bridge/README.md`](packages/codex-qq-bridge/README.md).

An optional Playwright monitor can also watch the first non-pinned post on
`@AyAsA_violin` every 30 minutes. New posts are turned into a compact group
learning card containing the timestamp, Japanese original, Chinese translation,
quoted-post context, key grammar, source link, and up to four original post
images, then proactively sent to each active QQ group. It uses an isolated,
locally logged-in Chrome profile, keeps an
SQLite delivery history to suppress duplicates and resume partial sends, and
runs independently from the bridge. Original images are downloaded once into
an owner-only persistent cache and reused for later resends:

```bash
./start-x-monitor.sh login
./start-x-monitor.sh once
./start-x-monitor.sh start
```

Use `./start-x-monitor.sh test-send` to send the current stored profile post and
its images to all active groups in the exact production learning format without
changing normal notification state.

## Updating

`setup.sh` also installs a global command `ai-bridge-update` into `~/.local/bin`.
From **any directory**, update the project to the latest version of the `custom`
branch:

```bash
ai-bridge-update
```

Update and then safely restart the QQ bridge (single instance only):

```bash
ai-bridge-update --restart
```

How it works:

* pulls with `git fetch origin custom` + a **fast-forward only** merge — it never
  runs `git reset --hard`, so your own edits are never silently overwritten;
* if any *tracked* file has local modifications, the update stops and lists those
  files instead of overwriting them;
* your `.env` (credentials) is protected — it is checked into `.gitignore` and its
  content is verified unchanged before/after every update;
* the Python editable install of `claude-code-qq-bridge` is refreshed and verified
  (`import claude_code_qq_bridge` + the `claude-code-qq-bridge` CLI).

Alternative (from the project directory):

```bash
cd ~/AI-Bridge-QQrobot-claude
./update.sh            # update only
./update.sh --restart  # update + safe restart
```

## Documentation

Full documentation: <https://ai-bridge-qqrobot-claude.readthedocs.io/en/latest/>

## Version

The current release is recorded in [`VERSION`](VERSION).

## License

[MIT](LICENSE)
