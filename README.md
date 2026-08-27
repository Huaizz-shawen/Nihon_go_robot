# Nihon_go_robot

Private workspace for local robot and agent integrations.

## Projects

- [`AI-Bridge-QQrobot-claude`](AI-Bridge-QQrobot-claude/) — QQ bridge for
  Claude Code and Codex; `main` contains the current deployable version.
- [`japanese-tutor`](japanese-tutor/) — source-grounded daily Japanese lessons,
  learner state, review scheduling, and QQ group curriculum support.

Local credentials, virtual environments, runtime logs, caches, and the backup
of the former nested Git metadata are intentionally excluded from this
repository.

## Deploying to another workstation

Clone the private repository, authenticate Codex on the workstation, install the
bridge, and rebuild the reproducible knowledge sources:

```bash
gh repo clone Huaizz-shawen/Nihon_go_robot
cd Nihon_go_robot/AI-Bridge-QQrobot-claude
./setup-codex.sh
codex login

cd ../japanese-tutor
../AI-Bridge-QQrobot-claude/.venv-codex/bin/python scripts/download_sources.py grammar
```

Git intentionally excludes secrets and private runtime state. Transfer these
directly between the two machines over a trusted channel rather than through
GitHub when continuity is required:

- `AI-Bridge-QQrobot-claude/.env`
- `~/.config/codex-qq-bridge/state.json`
- `japanese-tutor/learner/data/`
- `japanese-tutor/lessons/`

JMdict/Tatoeba SQLite databases can either be copied directly or rebuilt with
the import commands in [`japanese-tutor/README.md`](japanese-tutor/README.md).
Codex thread history is stored separately under the local Codex home; without
migrating it, the bridge safely creates fresh private/group Codex sessions while
retaining copied learner progress and lesson snapshots.

Stop the laptop bridge before starting the workstation bridge. Running two
instances against the same QQ Bot can cause competing gateway sessions and
duplicate scheduled lessons.
