#!/usr/bin/env python3
import _bootstrap  # noqa: F401

from japanese_tutor.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["search-vocabulary", *__import__("sys").argv[1:]]))
