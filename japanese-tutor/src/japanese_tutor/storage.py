from __future__ import annotations

import json
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEARNER_ROOT = PROJECT_ROOT / "learner" / "data"
LESSON_ROOT = PROJECT_ROOT / "lessons"
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REVIEW_INTERVALS = (1, 4, 7, 14, 30)


def validate_learner_id(learner_id: str) -> str:
    if not SAFE_ID.fullmatch(learner_id):
        raise ValueError("learner_id 只能包含 1-64 个字母、数字、下划线或连字符")
    return learner_id


def learner_dir(learner_id: str) -> Path:
    return LEARNER_ROOT / validate_learner_id(learner_id)


def lesson_dir(learner_id: str) -> Path:
    return LESSON_ROOT / validate_learner_id(learner_id)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected YAML mapping: {path}")
    return value


def write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    _atomic_write(path, rendered)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _atomic_write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def init_learner(
    learner_id: str,
    *,
    display_name: str = "",
    level: str = "N5",
    overwrite: bool = False,
) -> Path:
    target = learner_dir(learner_id)
    profile_path = target / "profile.yaml"
    if profile_path.exists() and not overwrite:
        return target
    template = load_yaml(PROJECT_ROOT / "learner" / "templates" / "profile.yaml")
    template["learner_id"] = learner_id
    template["display_name"] = display_name or learner_id
    template["level"] = level.upper()
    progress = {
        "version": 1,
        "grammar": {"learned": [], "review": {}},
        "vocabulary": {"learned": [], "review": {}},
        "weak_points": [],
        "recent_lessons": [],
        "last_study_at": None,
    }
    write_yaml(profile_path, template)
    write_yaml(target / "progress.yaml", progress)
    return target


def load_learner(learner_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    target = learner_dir(learner_id)
    if not (target / "profile.yaml").exists():
        init_learner(learner_id)
    return load_yaml(target / "profile.yaml"), load_yaml(target / "progress.yaml")


def save_progress(learner_id: str, progress: dict[str, Any]) -> None:
    write_yaml(learner_dir(learner_id) / "progress.yaml", progress)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_review_date(review_count: int, *, from_date: date | None = None) -> str:
    base = from_date or date.today()
    index = min(max(review_count - 1, 0), len(REVIEW_INTERVALS) - 1)
    return (base + timedelta(days=REVIEW_INTERVALS[index])).isoformat()
