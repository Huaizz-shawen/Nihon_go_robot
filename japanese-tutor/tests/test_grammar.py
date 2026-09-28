from pathlib import Path

from japanese_tutor import grammar


LESSON = """# Lesson 01

## 本课单词表

| 单词 | 读音 | 含义 |
|---|---|---|
| 私 | わたし | 我//I |
| 学生 | がくせい | 学生//student |

---

## 1. 〜です||Copula

### 接続
- 名词 + です

### 含义
:::zh
表示“是……”。
:::
:::en
Means is.
:::

### 例句
:::zh
1. 私は学生です。（我是学生。）
:::
:::en
1. I am a student.
:::

### 易错点
:::zh
- 不能在い形容词后加だ。
:::

## 2. は||Topic Marker

### 核心语感||Core Feel
:::zh
表示“说到……的话”，用于提示话题。
:::
:::en
Marks the topic.
:::

### 用法详解||Usage Details

#### 用法①：提示话题||Usage 1: Set the topic

:::zh
把某个事物设为话题，再说明关于它的信息。
:::
:::en
Set something as the topic.
:::

### 例句
:::zh
1. 私は学生です。（我是学生。）
:::
"""


def test_parse_lesson_extracts_grounded_content(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "source"
    path = root / "grammar" / "N5" / "lesson01_test.md"
    path.parent.mkdir(parents=True)
    path.write_text(LESSON, encoding="utf-8")
    monkeypatch.setattr(grammar, "GRAMMAR_ROOT", root)

    points = grammar.parse_lesson(path, "N5")

    assert len(points) == 2
    assert points[0].title == "〜です"
    assert points[0].structure == "- 名词 + です"
    assert points[0].examples[0].japanese == "私は学生です。"
    assert points[0].examples[0].chinese == "我是学生。"
    assert points[0].vocabulary[0].reading == "わたし"
    assert "Means is" not in points[0].meaning
    assert points[1].meaning == "表示“说到……的话”，用于提示话题。"
    assert "||" not in points[1].meaning
    assert "Usage Details" not in points[1].meaning
    assert "Usage 1" not in points[1].meaning


def test_parse_lesson_accepts_bilingual_vocabulary_heading_without_divider(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "source"
    path = root / "grammar" / "N3" / "lesson35_test.md"
    path.parent.mkdir(parents=True)
    source = LESSON.replace(
        "## 本课单词表", "## 本课单词表||Vocabulary"
    ).replace("\n---\n\n## 1.", "\n\n## 1.")
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(grammar, "GRAMMAR_ROOT", root)

    points = grammar.parse_lesson(path, "N3")

    assert [(item.word, item.reading) for item in points[0].vocabulary] == [
        ("私", "わたし"),
        ("学生", "がくせい"),
    ]
