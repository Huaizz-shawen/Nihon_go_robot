"""Environment shared by Codex subprocesses launched from unattended services."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


def codex_subprocess_env(codex_bin: str) -> dict[str, str]:
    """Let an npm-installed CLI find the Node installed alongside its launcher."""
    env = os.environ.copy()
    executable = shutil.which(codex_bin, path=env.get("PATH", os.defpath))
    if executable:
        # Keep the launcher directory: resolving an npm symlink would move to
        # lib/node_modules, which does not contain the matching Node executable.
        directory = str(Path(executable).absolute().parent)
        env["PATH"] = directory + os.pathsep + env.get("PATH", os.defpath)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env
