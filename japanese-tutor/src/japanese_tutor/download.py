from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .storage import PROJECT_ROOT, write_json


SOURCE_FILE = PROJECT_ROOT / "sources" / "sources.yaml"
RAW_ROOT = PROJECT_ROOT / "knowledge" / "raw"
GRAMMAR_TARGET = PROJECT_ROOT / "knowledge" / "grammar" / "japanese-grammar"
SOURCE_ALIASES = {
    "grammar": ["japanese_grammar_notes"],
    "jmdict": ["jmdict"],
    "tatoeba": ["tatoeba_japanese", "tatoeba_mandarin", "tatoeba_links"],
}


def load_sources() -> dict[str, dict[str, Any]]:
    value = yaml.safe_load(SOURCE_FILE.read_text(encoding="utf-8"))
    return dict(value["sources"])


def _download(url: str, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "nihongo-tutor/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, tempfile.NamedTemporaryFile(
        "wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
        temporary = Path(output.name)
        last_modified = response.headers.get("Last-Modified")
    temporary.replace(destination)
    return {
        "url": url,
        "path": str(destination.relative_to(PROJECT_ROOT)),
        "bytes": destination.stat().st_size,
        "sha256": digest.hexdigest(),
        "last_modified": last_modified,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _tree_digest(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                digest.update(chunk)
    return total, digest.hexdigest()


def _download_grammar_git(source: dict[str, Any]) -> dict[str, Any]:
    """Sparse-clone only curriculum files instead of the repository's large audio tree."""
    git_url = str(source.get("git_url") or source["homepage"])
    with tempfile.TemporaryDirectory(dir=GRAMMAR_TARGET.parent) as temp_name:
        checkout = Path(temp_name) / "checkout"
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", git_url, str(checkout)],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "sparse-checkout",
                "set",
                "--no-cone",
                "/grammar/",
                "/schedule.md",
                "/README.md",
                "/LICENSE",
            ],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        staging = Path(temp_name) / "japanese-grammar"
        staging.mkdir()
        for name in ("grammar", "schedule.md", "README.md", "LICENSE"):
            source_path = checkout / name
            if source_path.is_dir():
                shutil.copytree(source_path, staging / name)
            elif source_path.exists():
                shutil.copy2(source_path, staging / name)
        if not (staging / "grammar" / "N5").is_dir():
            raise ValueError("Sparse checkout did not contain grammar/N5")
        if GRAMMAR_TARGET.exists():
            shutil.rmtree(GRAMMAR_TARGET)
        shutil.move(str(staging), GRAMMAR_TARGET)
    total, digest = _tree_digest(GRAMMAR_TARGET)
    return {
        "url": git_url,
        "path": str(GRAMMAR_TARGET.relative_to(PROJECT_ROOT)),
        "bytes": total,
        "sha256": digest,
        "upstream_commit": commit,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def download_sources(names: list[str], *, refresh: bool = False) -> dict[str, dict[str, Any]]:
    sources = load_sources()
    expanded: list[str] = []
    for name in names or ["grammar"]:
        expanded.extend(SOURCE_ALIASES.get(name, [name]))
    snapshots_path = PROJECT_ROOT / "knowledge" / "source_snapshots.json"
    snapshots: dict[str, dict[str, Any]] = {}
    if snapshots_path.exists():
        snapshots = json.loads(snapshots_path.read_text(encoding="utf-8"))
    for source_id in dict.fromkeys(expanded):
        if source_id not in sources:
            raise KeyError(f"未知数据源：{source_id}")
        source = sources[source_id]
        if source_id == "japanese_grammar_notes":
            if GRAMMAR_TARGET.exists() and not refresh:
                total, digest = _tree_digest(GRAMMAR_TARGET)
                result = {
                    "url": source.get("git_url") or source["homepage"],
                    "path": str(GRAMMAR_TARGET.relative_to(PROJECT_ROOT)),
                    "bytes": total,
                    "sha256": digest,
                    "cached": True,
                }
            else:
                result = _download_grammar_git(source)
            result.update(
                {
                    "name": source["name"],
                    "license": source["license"],
                    "attribution": source["attribution"],
                }
            )
            snapshots[source_id] = result
            continue
        url = source.get("download_url")
        if not url:
            raise ValueError(f"数据源 {source_id} 没有自动下载地址")
        filename = Path(str(url)).name
        destination = RAW_ROOT / filename
        if destination.exists() and not refresh:
            result = snapshots.get(source_id, {})
            result.update({"path": str(destination.relative_to(PROJECT_ROOT)), "cached": True})
        else:
            result = _download(str(url), destination)
        result.update(
            {
                "name": source["name"],
                "license": source["license"],
                "attribution": source["attribution"],
            }
        )
        snapshots[source_id] = result
    write_json(snapshots_path, snapshots)
    return {source_id: snapshots[source_id] for source_id in dict.fromkeys(expanded)}
