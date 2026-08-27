import json
from datetime import date
from pathlib import Path

from japanese_tutor import grammar, storage
from japanese_tutor.lesson import (
    generate_lesson,
    record_lesson_published,
    record_lesson_result,
)

from test_grammar import LESSON


def _sandbox(tmp_path: Path, monkeypatch) -> None:
    grammar_root = tmp_path / "knowledge" / "grammar" / "japanese-grammar"
    lesson_path = grammar_root / "grammar" / "N5" / "lesson01_test.md"
    lesson_path.parent.mkdir(parents=True)
    lesson_path.write_text(LESSON, encoding="utf-8")
    monkeypatch.setattr(grammar, "GRAMMAR_ROOT", grammar_root)
    monkeypatch.setattr(storage, "LEARNER_ROOT", tmp_path / "learner")
    monkeypatch.setattr(storage, "LESSON_ROOT", tmp_path / "lessons")
    template = tmp_path / "profile.yaml"
    template.write_text(
        "version: 1\nlearner_id: owner\ndisplay_name: Test\nlevel: N5\n"
        "daily:\n  vocabulary: 2\n  expressions: 1\n  exercises: 3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "PROJECT_ROOT", tmp_path)
    target = tmp_path / "learner" / "owner"
    target.mkdir(parents=True)
    (target / "profile.yaml").write_text(template.read_text(), encoding="utf-8")
    (target / "progress.yaml").write_text(
        "version: 1\ngrammar: {learned: [], review: {}}\n"
        "vocabulary: {learned: [], review: {}}\nweak_points: []\n"
        "recent_lessons: []\nlast_study_at: null\n",
        encoding="utf-8",
    )


def test_daily_lesson_then_result_updates_state(tmp_path: Path, monkeypatch) -> None:
    _sandbox(tmp_path, monkeypatch)
    day = date(2026, 8, 26)

    path = generate_lesson("owner", lesson_date=day)

    assert path.exists()
    lesson_text = path.read_text(encoding="utf-8")
    assert "### 〜です" in lesson_text
    expressions = lesson_text.split("## 今日表达", 1)[1].split("## 今日语法", 1)[0]
    vocabulary = lesson_text.split("## 今日单词", 1)[1].split("## 小练习", 1)[0]
    assert "来源：" not in expressions
    assert "例句来源：" not in vocabulary
    assert "词典核验：" not in vocabulary
    assert "## Source" in lesson_text
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    grammar_id = metadata["grammar"]["item_id"]
    assert len(metadata["vocabulary"]) == 2
    assert all(item["example_japanese"] for item in metadata["vocabulary"])
    assert all(item["example_chinese"] for item in metadata["vocabulary"])
    assert any(item["type"] == "vocabulary_meaning" for item in metadata["exercises"])
    result = record_lesson_result(
        "owner", day, score=2, total=3, wrong_items={grammar_id}
    )
    assert result["mastered"] is False
    progress = storage.load_yaml(tmp_path / "learner" / "owner" / "progress.yaml")
    assert grammar_id in progress["grammar"]["learned"]
    assert progress["grammar"]["review"][grammar_id]["last_result"] == "wrong"
    assert grammar_id in progress["weak_points"]

    next_lesson = generate_lesson("owner", lesson_date=date(2026, 8, 27))
    next_text = next_lesson.read_text(encoding="utf-8")
    assert "单词：" in next_text
    assert "例句：私は学生です。" in next_text


def test_published_group_lesson_advances_without_claiming_mastery(
    tmp_path: Path, monkeypatch
) -> None:
    _sandbox(tmp_path, monkeypatch)
    day = date(2026, 8, 26)
    path = generate_lesson("owner", lesson_date=day)
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))

    first = record_lesson_published("owner", day)
    second = record_lesson_published("owner", day)

    assert first["changed"] is True
    assert second["changed"] is False
    progress = storage.load_yaml(tmp_path / "learner" / "owner" / "progress.yaml")
    grammar_id = metadata["grammar"]["item_id"]
    grammar_review = progress["grammar"]["review"][grammar_id]
    assert grammar_id in progress["grammar"]["learned"]
    assert grammar_review["mastered"] is False
    assert grammar_review["last_result"] == "introduced"
    assert grammar_review["due"] == "2026-08-27"
    assert len(progress["published_lessons"]) == 1


def test_same_day_lesson_is_an_immutable_snapshot(tmp_path: Path, monkeypatch) -> None:
    _sandbox(tmp_path, monkeypatch)
    day = date(2026, 8, 26)
    path = generate_lesson("owner", lesson_date=day)
    original_markdown = path.read_bytes()
    original_metadata = path.with_suffix(".json").read_bytes()

    record_lesson_published("owner", day)
    reused = generate_lesson("owner", lesson_date=day)

    assert reused == path
    assert reused.read_bytes() == original_markdown
    assert reused.with_suffix(".json").read_bytes() == original_metadata


def test_group_lesson_result_updates_member_state(tmp_path: Path, monkeypatch) -> None:
    _sandbox(tmp_path, monkeypatch)
    day = date(2026, 8, 26)
    path = generate_lesson("owner", lesson_date=day)
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    member = tmp_path / "learner" / "qq_member"
    member.mkdir(parents=True)
    (member / "profile.yaml").write_text(
        "version: 1\nlearner_id: qq_member\ndisplay_name: Member\nlevel: N5\n"
        "daily:\n  vocabulary: 2\n  expressions: 1\n  exercises: 3\n",
        encoding="utf-8",
    )
    (member / "progress.yaml").write_text(
        "version: 1\ngrammar: {learned: [], review: {}}\n"
        "vocabulary: {learned: [], review: {}}\nweak_points: []\n"
        "recent_lessons: []\nlast_study_at: null\n",
        encoding="utf-8",
    )

    record_lesson_result(
        "qq_member",
        day,
        score=3,
        total=3,
        source_learner_id="owner",
    )

    progress = storage.load_yaml(member / "progress.yaml")
    assert metadata["grammar"]["item_id"] in progress["grammar"]["learned"]
