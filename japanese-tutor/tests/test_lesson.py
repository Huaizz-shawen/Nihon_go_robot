import json
import re
from datetime import date
from pathlib import Path

from japanese_tutor import grammar, lesson, storage
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
    assert re.search(r"[一-龯]+（[ぁ-ゖー]+）", expressions)
    assert "例句：私（わたし）は学生（がくせい）です。" in vocabulary
    assert "## Source" in lesson_text
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    grammar_id = metadata["grammar"]["item_id"]
    assert len(metadata["vocabulary"]) == 2
    assert all(item["example_japanese"] for item in metadata["vocabulary"])
    assert all(item["example_japanese_annotated"] for item in metadata["vocabulary"])
    assert all(item["example_chinese"] for item in metadata["vocabulary"])
    assert "（" in metadata["examples"][0]["japanese_annotated"]
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
    assert "例句：私（わたし）は学生（がくせい）です。" in next_text


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


def test_version_one_snapshot_gets_reading_only_format_migration(
    tmp_path: Path, monkeypatch
) -> None:
    _sandbox(tmp_path, monkeypatch)
    day = date(2026, 8, 26)
    output = tmp_path / "lessons" / "owner"
    output.mkdir(parents=True)
    markdown_path = output / f"{day.isoformat()}.md"
    metadata_path = output / f"{day.isoformat()}.json"
    markdown_path.write_text(
        "# Lesson\n\n## 今日表达\n\n1. 私は学生です。\n"
        "   - 中文：我是学生。\n\n## 今日单词\n\n"
        "1. **学生（がくせい）**\n   - 例句：私は学生です。\n"
        "\n## 小练习\n",
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(
            {
                "version": 1,
                "vocabulary": [
                    {
                        "word": "私",
                        "reading": "わたし",
                        "example_japanese": "私は学生です。",
                    },
                    {"word": "学生", "reading": "がくせい"},
                ],
                "examples": [{"japanese": "私は学生です。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    migrated = generate_lesson("owner", lesson_date=day)

    text = migrated.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "1. 私（わたし）は学生（がくせい）です。" in text
    assert "例句：私（わたし）は学生（がくせい）です。" in text
    assert metadata["version"] == lesson.LESSON_FORMAT_VERSION
    assert metadata["examples"][0]["japanese_annotated"] == (
        "私（わたし）は学生（がくせい）です。"
    )


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


def test_expression_selection_checks_all_daily_vocabulary(
    tmp_path: Path, monkeypatch
) -> None:
    _sandbox(tmp_path, monkeypatch)
    point = grammar.load_curriculum("N5")[0]
    vocabulary = [
        grammar.VocabularyItem(str(index), str(index), str(index), "test.md")
        for index in range(1, 5)
    ]

    def search_fourth_word(word: str, limit: int = 4) -> list[dict[str, object]]:
        if word != "4":
            return []
        return [
            {
                "japanese": "四番目の単語です。",
                "chinese": "这是第四个单词。",
                "jp_id": 4,
                "zh_id": 44,
            }
        ]

    monkeypatch.setattr(lesson, "search_examples", search_fourth_word)

    selected = lesson._select_examples(point, vocabulary, 2)

    assert len(selected) == 2
    assert selected[0].source_name == "Tatoeba"


def test_vocabulary_example_falls_back_to_numbered_source_line(
    tmp_path: Path, monkeypatch
) -> None:
    _sandbox(tmp_path, monkeypatch)
    point = grammar.load_curriculum("N5")[0]
    point.examples = []
    point.reference_examples = []
    point.raw = """### 用法
1. A：彼は10年も日本に住んでいたんですよ。
   B：**道理で**日本語が上手なわけだ。（难怪日语那么好。）
"""
    item = grammar.VocabularyItem("道理で", "どうりで", "难怪", point.source_path)

    rows = lesson._vocabulary_rows([item], point, set())

    assert rows[0]["example_japanese"] == "道理で日本語が上手なわけだ。"
    assert rows[0]["example_chinese"] == "难怪日语那么好。"


def test_completed_curriculum_automatically_promotes_through_n3(
    tmp_path: Path, monkeypatch
) -> None:
    _sandbox(tmp_path, monkeypatch)
    grammar_root = tmp_path / "knowledge" / "grammar" / "japanese-grammar"
    for level, lesson_number in (("N4", 18), ("N3", 35)):
        lesson_path = (
            grammar_root / "grammar" / level / f"lesson{lesson_number}_test.md"
        )
        lesson_path.parent.mkdir(parents=True)
        lesson_path.write_text(
            LESSON.replace(
                "## 本课单词表", "## 本课单词表||Vocabulary"
            ).replace("\n---\n\n## 1.", "\n\n## 1."),
            encoding="utf-8",
        )

    progress_path = tmp_path / "learner" / "owner" / "progress.yaml"
    progress = storage.load_yaml(progress_path)
    n5_ids = [point.item_id for point in grammar.load_curriculum("N5")]
    progress["grammar"]["learned"] = n5_ids
    progress["grammar"]["review"][n5_ids[0]] = {
        "title": "N5 review",
        "due": "2099-01-01",
        "mastered": False,
    }
    storage.write_yaml(progress_path, progress)

    n4_path = generate_lesson("owner", lesson_date=date(2026, 8, 27))
    n4_metadata = json.loads(n4_path.with_suffix(".json").read_text(encoding="utf-8"))
    profile = storage.load_yaml(tmp_path / "learner" / "owner" / "profile.yaml")
    promoted_progress = storage.load_yaml(progress_path)
    assert profile["level"] == "N4"
    assert n4_metadata["grammar"]["item_id"].startswith("grammar:N4:")
    assert promoted_progress["grammar"]["review"][n5_ids[0]]["title"] == "N5 review"

    progress = storage.load_yaml(progress_path)
    progress["grammar"]["learned"].extend(
        point.item_id for point in grammar.load_curriculum("N4")
    )
    storage.write_yaml(progress_path, progress)

    n3_path = generate_lesson("owner", lesson_date=date(2026, 8, 28))
    n3_metadata = json.loads(n3_path.with_suffix(".json").read_text(encoding="utf-8"))
    profile = storage.load_yaml(tmp_path / "learner" / "owner" / "profile.yaml")
    assert profile["level"] == "N3"
    assert n3_metadata["grammar"]["item_id"].startswith("grammar:N3:")
    assert n3_metadata["level"] == "N3"
