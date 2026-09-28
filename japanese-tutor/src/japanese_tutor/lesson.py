from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from .databases import lookup_vocabulary, search_examples
from .grammar import Example, GrammarPoint, VocabularyItem, load_curriculum
from .readings import annotate_kanji_readings
from .storage import (
    append_jsonl,
    learner_dir,
    lesson_dir,
    load_learner,
    next_review_date,
    save_progress,
    utc_now,
    write_json,
    write_yaml,
)


LEVEL_PROGRESSION = {"N5": "N4", "N4": "N3"}
LESSON_FORMAT_VERSION = 3
MISSING_EXAMPLE = "当前本地来源未检索到例句"


def _due_reviews(progress: dict[str, Any], today: date) -> list[dict[str, Any]]:
    due: list[dict[str, Any]] = []
    for kind in ("grammar", "vocabulary"):
        reviews = progress.get(kind, {}).get("review", {})
        for item_id, entry in reviews.items():
            if not isinstance(entry, dict) or entry.get("mastered"):
                continue
            if str(entry.get("due", "9999-12-31")) <= today.isoformat():
                due.append({"item_id": item_id, "kind": kind, **entry})
    return sorted(due, key=lambda item: (str(item.get("due", "")), str(item.get("title", ""))))


def _word_tokens(word: str) -> tuple[str, ...]:
    tokens = [word]
    if len(word) >= 2 and word[-1] in "うくぐすつぬぶむる":
        tokens.append(word[:-1])
    return tuple(token for token in tokens if token)


def _matching_example(item: VocabularyItem, examples: list[Example]) -> Example | None:
    tokens = _word_tokens(item.word)
    return next(
        (example for example in examples if any(token in example.japanese for token in tokens)),
        None,
    )


def _numbered_source_example(
    item: VocabularyItem, point: GrammarPoint
) -> Example | None:
    """Recover grounded examples from numbered source lines outside 例句 sections."""
    mode = "neutral"
    for line_number, raw_line in enumerate(point.raw.splitlines(), 1):
        stripped = raw_line.strip()
        if stripped == ":::zh":
            mode = "zh"
            continue
        if stripped == ":::en":
            mode = "en"
            continue
        if stripped == ":::" and mode in {"zh", "en"}:
            mode = "neutral"
            continue
        prefix = re.match(r"^(?:\d+[.)]|[ABＡＢ][：:])\s*", stripped)
        if mode == "en" or prefix is None:
            continue
        cleaned = stripped[prefix.end() :]
        cleaned = cleaned.replace("**", "").replace("`", "").strip()
        match = re.match(r"(.+?)[（(]([^（）()]*)[）)](?:\s*.*)?$", cleaned)
        if match:
            japanese, chinese = match.group(1).strip(), match.group(2).strip()
        else:
            japanese, chinese = cleaned, ""
        if not re.search(r"[ぁ-んァ-ヶ一-龯]", japanese):
            continue
        if not any(token in japanese for token in _word_tokens(item.word)):
            continue
        return Example(
            japanese=japanese,
            chinese=chinese,
            source_id=f"{point.source_path}#{point.item_id}:line-{line_number}",
        )
    return None


def _select_vocabulary(
    point: GrammarPoint, count: int, learned_items: set[str]
) -> list[VocabularyItem]:
    haystack = point.raw
    ordered = sorted(
        point.vocabulary,
        key=lambda item: (
            item.item_id in learned_items,
            _matching_example(item, point.examples) is None,
            _matching_example(item, point.reference_examples) is None,
            item.word not in haystack,
            len(item.word),
        ),
    )
    selected: list[VocabularyItem] = []
    seen: set[tuple[str, str]] = set()
    for item in ordered:
        key = (item.word, item.reading)
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= count:
            break
    return selected


def _select_examples(point: GrammarPoint, vocabulary: list[VocabularyItem], count: int) -> list[Example]:
    selected: list[Example] = []
    seen: set[str] = set()
    for item in vocabulary:
        for row in search_examples(item.word, limit=4):
            japanese = str(row["japanese"])
            if japanese in seen:
                continue
            selected.append(
                Example(
                    japanese=japanese,
                    chinese=str(row["chinese"]),
                    source_id=f"Tatoeba jpn:{row['jp_id']} cmn:{row['zh_id']}",
                    source_name="Tatoeba",
                )
            )
            seen.add(japanese)
            if len(selected) >= count:
                return selected
    for example in point.examples:
        if example.japanese in seen:
            continue
        selected.append(example)
        seen.add(example.japanese)
        if len(selected) >= count:
            break
    return selected


POS_LABELS = (
    ("pronoun", "代词"),
    ("adjectival nouns", "な形容词"),
    ("Ichidan verb", "一段动词"),
    ("Godan verb", "五段动词"),
    ("Suru verb", "サ变动词"),
    ("Kuru verb", "カ变动词"),
    ("intransitive verb", "自动词"),
    ("transitive verb", "他动词"),
    ("adverb", "副词"),
    ("i-adjective", "い形容词"),
    ("adjective (keiyoushi)", "い形容词"),
    ("interjection", "感叹词"),
    ("conjunction", "接续词"),
    ("particle", "助词"),
    ("counter", "量词"),
    ("noun", "名词"),
)


def _display_pos(values: list[object]) -> list[str]:
    labels: list[str] = []
    for value in values:
        raw = str(value)
        label = next((translated for marker, translated in POS_LABELS if marker in raw), raw)
        if label not in labels:
            labels.append(label)
    return labels


def _vocabulary_example(item: VocabularyItem, point: GrammarPoint) -> Example | None:
    grounded = _matching_example(item, point.examples)
    if grounded:
        return grounded
    grounded = _matching_example(item, point.reference_examples)
    if grounded:
        return grounded
    grounded = _numbered_source_example(item, point)
    if grounded:
        return grounded
    for token in _word_tokens(item.word):
        rows = search_examples(token, limit=5)
        if rows:
            row = rows[0]
            return Example(
                japanese=str(row["japanese"]),
                chinese=str(row["chinese"]),
                source_id=f"Tatoeba jpn:{row['jp_id']} cmn:{row['zh_id']}",
                source_name="Tatoeba",
            )
    return None


def _vocabulary_rows(
    items: list[VocabularyItem], point: GrammarPoint, learned_items: set[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    reading_overrides = {entry.word: entry.reading for entry in point.vocabulary}
    for item in items:
        matches = lookup_vocabulary(item.word)
        exact = next((row for row in matches if row["reading"] == item.reading), None)
        example = _vocabulary_example(item, point)
        result.append(
            {
                **asdict(item),
                "item_id": item.item_id,
                "pos": exact["pos"] if exact else [],
                "pos_zh": _display_pos(exact["pos"] if exact else []),
                "jmdict_ent_seq": exact["ent_seq"] if exact else None,
                "jmdict_meanings_en": exact["meanings"][:3] if exact else [],
                "is_new": item.item_id not in learned_items,
                "example_japanese": example.japanese if example else "",
                "example_japanese_annotated": (
                    annotate_kanji_readings(example.japanese, reading_overrides)
                    if example
                    else ""
                ),
                "example_chinese": example.chinese if example else "",
                "example_source": example.source_id if example else "",
            }
        )
    return result


def _metadata_reading_overrides(metadata: dict[str, Any]) -> dict[str, str]:
    vocabulary = metadata.get("vocabulary")
    if not isinstance(vocabulary, list):
        return {}
    return {
        str(item.get("word")): str(item.get("reading"))
        for item in vocabulary
        if isinstance(item, dict) and item.get("word") and item.get("reading")
    }


def _annotate_snapshot_markdown(
    markdown: str, reading_overrides: dict[str, str]
) -> str:
    section = ""
    rendered: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        if section == "今日表达":
            match = re.match(r"^(\d+\.\s+)(.+)$", line)
            if match:
                line = match.group(1) + annotate_kanji_readings(
                    match.group(2), reading_overrides
                )
        elif section == "今日单词" and line.startswith("   - 例句："):
            prefix, sentence = line.split("：", 1)
            if sentence and sentence != MISSING_EXAMPLE:
                line = prefix + "：" + annotate_kanji_readings(
                    sentence, reading_overrides
                )
        rendered.append(line)
    return "\n".join(rendered) + ("\n" if markdown.endswith("\n") else "")


def _upgrade_lesson_readings(markdown_path: Path, metadata_path: Path) -> None:
    """Apply a deterministic reading-only format migration to old snapshots."""
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return
    if (
        not isinstance(metadata, dict)
        or int(metadata.get("version", 1)) >= LESSON_FORMAT_VERSION
    ):
        return
    overrides = _metadata_reading_overrides(metadata)
    markdown = markdown_path.read_text(encoding="utf-8")
    markdown_path.write_text(
        _annotate_snapshot_markdown(markdown, overrides), encoding="utf-8"
    )
    vocabulary = metadata.get("vocabulary")
    if isinstance(vocabulary, list):
        for item in vocabulary:
            if not isinstance(item, dict):
                continue
            sentence = str(item.get("example_japanese") or "")
            item["example_japanese_annotated"] = (
                annotate_kanji_readings(sentence, overrides) if sentence else ""
            )
    examples = metadata.get("examples")
    if isinstance(examples, list):
        for example in examples:
            if not isinstance(example, dict):
                continue
            sentence = str(example.get("japanese") or "")
            example["japanese_annotated"] = (
                annotate_kanji_readings(sentence, overrides) if sentence else ""
            )
    metadata["version"] = LESSON_FORMAT_VERSION
    write_json(metadata_path, metadata)


def _build_exercises(
    point: GrammarPoint,
    vocabulary: list[dict[str, Any]],
    examples: list[Example],
    count: int,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if examples:
        candidates.append(
            {
                "type": "jp_to_zh",
                "prompt": f"翻译成中文：{examples[0].japanese}",
                "answer": examples[0].chinese,
                "item_id": point.item_id,
            }
        )
    if vocabulary:
        word = vocabulary[0]
        candidates.append(
            {
                "type": "reading",
                "prompt": f"写出「{word['word']}」的假名读音。",
                "answer": str(word["reading"]),
                "item_id": str(word["item_id"]),
            }
        )
    if len(vocabulary) > 1:
        word = vocabulary[1]
        candidates.append(
            {
                "type": "vocabulary_meaning",
                "prompt": f"「{word['word']}（{word['reading']}）」是什么意思？",
                "answer": str(word["meaning"]),
                "item_id": str(word["item_id"]),
            }
        )
    if len(examples) > 1:
        candidates.append(
            {
                "type": "zh_to_jp",
                "prompt": f"参考今日句型翻译成日语：{examples[1].chinese}",
                "answer": examples[1].japanese,
                "item_id": point.item_id,
            }
        )
    candidates.append(
        {
            "type": "production",
            "prompt": f"使用「{point.title}」写一个与你今天有关的句子。",
            "answer": "开放题：句型、接续和语义自然即可。",
            "item_id": point.item_id,
        }
    )
    return candidates[:count]


def _select_curriculum_point(
    profile: dict[str, Any], progress: dict[str, Any]
) -> tuple[str, GrammarPoint]:
    """Select the next point, promoting completed N5/N4 curricula automatically."""
    level = str(profile.get("level", "N5")).upper()
    learned = set(progress.get("grammar", {}).get("learned", []))
    while True:
        curriculum = load_curriculum(level)
        point = next((item for item in curriculum if item.item_id not in learned), None)
        if point is not None:
            return level, point
        next_level = LEVEL_PROGRESSION.get(level)
        if next_level is None:
            return level, curriculum[0]
        level = next_level


def generate_lesson(
    learner_id: str,
    *,
    lesson_date: date | None = None,
    force: bool = False,
) -> Path:
    today = lesson_date or date.today()
    output_dir = lesson_dir(learner_id)
    markdown_path = output_dir / f"{today.isoformat()}.md"
    metadata_path = output_dir / f"{today.isoformat()}.json"
    if markdown_path.exists() and metadata_path.exists() and not force:
        _upgrade_lesson_readings(markdown_path, metadata_path)
        return markdown_path

    profile, progress = load_learner(learner_id)
    level, point = _select_curriculum_point(profile, progress)
    learned = set(progress.get("grammar", {}).get("learned", []))
    learned_vocabulary = set(progress.get("vocabulary", {}).get("learned", []))
    profile_level = str(profile.get("level", "N5"))
    if profile_level != level:
        profile = dict(profile)
        profile["level"] = level
        write_yaml(learner_dir(learner_id) / "profile.yaml", profile)
    daily = profile.get("daily", {})
    vocab_items = _select_vocabulary(
        point, int(daily.get("vocabulary", 6)), learned_vocabulary
    )
    vocabulary = _vocabulary_rows(vocab_items, point, learned_vocabulary)
    examples = _select_examples(point, vocab_items, int(daily.get("expressions", 3)))
    reading_overrides = {item.word: item.reading for item in point.vocabulary}
    exercises = _build_exercises(point, vocabulary, examples, int(daily.get("exercises", 4)))
    reviews = _due_reviews(progress, today)

    lines = [f"# 每日日本語 · {today.isoformat()}", ""]
    lines.extend([f"学习者：{profile.get('display_name', learner_id)}（{profile.get('level', 'N5')}）", ""])
    lines.extend(["## 今日复习", ""])
    if reviews:
        lines.extend(
            [
                f"- {'单词' if item.get('kind') == 'vocabulary' else '语法'}："
                f"{item.get('title', item['item_id'])}"
                f"（已复习 {item.get('review_count', 0)} 次）"
                for item in reviews[:3]
            ]
        )
    else:
        lines.append("- 暂无到期复习；今天建立第一批记忆。")
    lines.extend(["", "## 今日表达", ""])
    for index, example in enumerate(examples, 1):
        lines.extend(
            [
                f"{index}. {annotate_kanji_readings(example.japanese, reading_overrides)}",
                f"   - 中文：{example.chinese}",
            ]
        )
    lines.extend(["", "## 今日语法", "", f"### {point.title}", ""])
    if point.structure:
        lines.extend(["接续/结构：", "", point.structure, ""])
    lines.extend(["含义与用法：", "", point.meaning or "请依据来源段落讲解。", ""])
    if point.common_errors:
        lines.extend(["常见错误：", "", point.common_errors, ""])
    lines.extend(["## 今日单词", ""])
    for index, item in enumerate(vocabulary, 1):
        pos = "、".join(str(value) for value in item["pos_zh"][:2]) or "—"
        status = "新词" if item["is_new"] else "复现词"
        lines.extend(
            [
                f"{index}. **{item['word']}（{item['reading']}）** · {status}",
                f"   - 词性：{pos}",
                f"   - 中文：{item['meaning']}",
                f"   - 例句：{item['example_japanese_annotated'] or MISSING_EXAMPLE}",
            ]
        )
        if item["example_chinese"]:
            lines.append(f"   - 译文：{item['example_chinese']}")
    lines.extend(["", "## 小练习", ""])
    for index, exercise in enumerate(exercises, 1):
        lines.append(f"{index}. {exercise['prompt']}")
    lines.extend(
        [
            "",
            "## Source",
            "",
            f"- Japanese Grammar Notes（CC BY 4.0）：`{point.source_path}`，语法 id `{point.item_id}`",
            "- UniDic-lite 2.1.2（BSD）：本地分词与汉字读音标注；当天词表读音优先",
        ]
    )
    if any(item["jmdict_ent_seq"] for item in vocabulary):
        entry_ids = "、".join(
            str(item["jmdict_ent_seq"])
            for item in vocabulary
            if item["jmdict_ent_seq"]
        )
        lines.append(f"- JMdict / EDRDG（CC BY-SA 4.0）：词条 {entry_ids}")
    if any(example.source_name == "Tatoeba" for example in examples) or any(
        str(item.get("example_source", "")).startswith("Tatoeba") for item in vocabulary
    ):
        tatoeba_ids = sorted(
            {
                source_id
                for source_id in [
                    *(example.source_id for example in examples),
                    *(str(item.get("example_source", "")) for item in vocabulary),
                ]
                if source_id.startswith("Tatoeba")
            }
        )
        suffix = f"：{'、'.join(tatoeba_ids)}" if tatoeba_ids else ""
        lines.append(f"- Tatoeba contributors（CC BY 2.0 FR）{suffix}")
    lines.extend(["", "> Tutor 对来源内容的重组、练习题与补充说明属于 Agent 生成内容。", ""])

    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    metadata = {
        "version": LESSON_FORMAT_VERSION,
        "date": today.isoformat(),
        "learner_id": learner_id,
        "level": level,
        "grammar": {
            "item_id": point.item_id,
            "title": point.title,
            "source_path": point.source_path,
        },
        "vocabulary": vocabulary,
        "examples": [
            {
                **asdict(example),
                "japanese_annotated": annotate_kanji_readings(
                    example.japanese, reading_overrides
                ),
            }
            for example in examples
        ],
        "exercises": exercises,
        "review_items": [item["item_id"] for item in reviews[:3]],
    }
    write_json(metadata_path, metadata)
    return markdown_path


def record_lesson_published(learner_id: str, lesson_date: date) -> dict[str, Any]:
    """Advance a shared curriculum after its lesson has been delivered.

    Publishing means the material was introduced, not mastered. New grammar and
    vocabulary enter the review queue with a next-day due date; review items
    included in the lesson advance one spacing interval. The operation is
    idempotent so a bridge retry cannot advance the curriculum twice.
    """
    metadata_path = lesson_dir(learner_id) / f"{lesson_date.isoformat()}.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"没有找到课程元数据：{metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    _profile, progress = load_learner(learner_id)
    published = progress.setdefault("published_lessons", [])
    if lesson_date.isoformat() in published:
        return {"learner_id": learner_id, "lesson": lesson_date.isoformat(), "changed": False}

    now = utc_now()
    grammar = metadata["grammar"]
    grammar_state = progress.setdefault("grammar", {"learned": [], "review": {}})
    grammar_id = grammar["item_id"]
    if grammar_id not in grammar_state["learned"]:
        grammar_state["learned"].append(grammar_id)
    grammar_state["review"].setdefault(
        grammar_id,
        {
            "title": grammar["title"],
            "source_path": grammar["source_path"],
            "learned_at": now,
            "review_count": 0,
            "last_result": "introduced",
            "mastered": False,
            "due": next_review_date(0, from_date=lesson_date),
        },
    )

    vocabulary_state = progress.setdefault("vocabulary", {"learned": [], "review": {}})
    for item in metadata.get("vocabulary", []):
        item_id = item["item_id"]
        if item_id not in vocabulary_state["learned"]:
            vocabulary_state["learned"].append(item_id)
        vocabulary_state["review"].setdefault(
            item_id,
            {
                "title": f"{item['word']}（{item['reading']}）",
                "learned_at": now,
                "review_count": 0,
                "last_result": "introduced",
                "mastered": False,
                "due": next_review_date(0, from_date=lesson_date),
            },
        )

    for item_id in metadata.get("review_items", []):
        for kind in ("grammar", "vocabulary"):
            review = progress.get(kind, {}).get("review", {}).get(item_id)
            if not isinstance(review, dict):
                continue
            review["review_count"] = int(review.get("review_count", 0)) + 1
            review["last_result"] = "reviewed_in_lesson"
            review["last_reviewed_at"] = now
            review["mastered"] = int(review["review_count"]) >= 5
            review["due"] = next_review_date(
                int(review["review_count"]), from_date=lesson_date
            )
            break

    published.append(lesson_date.isoformat())
    progress["published_lessons"] = published[-90:]
    save_progress(learner_id, progress)
    return {"learner_id": learner_id, "lesson": lesson_date.isoformat(), "changed": True}


def record_lesson_result(
    learner_id: str,
    lesson_date: date,
    *,
    score: int,
    total: int,
    wrong_items: set[str] | None = None,
    source_learner_id: str | None = None,
) -> dict[str, Any]:
    wrong_items = wrong_items or set()
    metadata_owner = source_learner_id or learner_id
    metadata_path = lesson_dir(metadata_owner) / f"{lesson_date.isoformat()}.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"没有找到课程元数据：{metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    _profile, progress = load_learner(learner_id)
    now = utc_now()

    grammar = metadata["grammar"]
    grammar_id = grammar["item_id"]
    grammar_state = progress.setdefault("grammar", {"learned": [], "review": {}})
    if grammar_id not in grammar_state["learned"]:
        grammar_state["learned"].append(grammar_id)
    review = grammar_state["review"].setdefault(
        grammar_id,
        {
            "title": grammar["title"],
            "source_path": grammar["source_path"],
            "learned_at": now,
            "review_count": 0,
            "mastered": False,
        },
    )
    if grammar_id in wrong_items:
        review["review_count"] = max(0, int(review.get("review_count", 0)) - 1)
        review["last_result"] = "wrong"
    else:
        review["review_count"] = int(review.get("review_count", 0)) + 1
        review["last_result"] = "correct"
    review["last_reviewed_at"] = now
    review["mastered"] = int(review["review_count"]) >= 5
    review["due"] = next_review_date(int(review["review_count"]), from_date=lesson_date)

    vocab_state = progress.setdefault("vocabulary", {"learned": [], "review": {}})
    for item in metadata.get("vocabulary", []):
        item_id = item["item_id"]
        if item_id not in vocab_state["learned"]:
            vocab_state["learned"].append(item_id)
        state = vocab_state["review"].setdefault(
            item_id,
            {
                "title": f"{item['word']}（{item['reading']}）",
                "learned_at": now,
                "review_count": 0,
                "mastered": False,
            },
        )
        correct = item_id not in wrong_items
        state["review_count"] = max(0, int(state.get("review_count", 0)) + (1 if correct else -1))
        state["last_result"] = "correct" if correct else "wrong"
        state["last_reviewed_at"] = now
        state["mastered"] = int(state["review_count"]) >= 5
        state["due"] = next_review_date(int(state["review_count"]), from_date=lesson_date)

    for item_id in sorted(wrong_items):
        if item_id not in progress.setdefault("weak_points", []):
            progress["weak_points"].append(item_id)
        append_jsonl(
            learner_dir(learner_id) / "mistakes.jsonl",
            {
                "timestamp": now,
                "lesson": lesson_date.isoformat(),
                "item_id": item_id,
                "score": score,
                "total": total,
            },
        )
    progress.setdefault("recent_lessons", []).append(
        {"date": lesson_date.isoformat(), "score": score, "total": total}
    )
    progress["recent_lessons"] = progress["recent_lessons"][-30:]
    progress["last_study_at"] = now
    save_progress(learner_id, progress)
    return {
        "learner_id": learner_id,
        "lesson": lesson_date.isoformat(),
        "score": score,
        "total": total,
        "next_grammar_review": review["due"],
        "mastered": review["mastered"],
    }
