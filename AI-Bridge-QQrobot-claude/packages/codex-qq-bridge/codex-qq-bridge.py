#!/usr/bin/env python3
"""Compatibility launcher; prefer the installed ``codex-qq-bridge`` command."""

from codex_qq_bridge.bridge import cli


if __name__ == "__main__":
    raise SystemExit(cli())
