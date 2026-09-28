from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .storage import PROJECT_ROOT


GRAMMAR_ROOT = PROJECT_ROOT / "knowledge" / "grammar" / "japanese-grammar"
HEADING_RE = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)


@dataclass
class VocabularyItem:
    word: str
    reading: str
    meaning: str
    source_path: str

    @property
    def item_id(self) -> str:
        return f"vocab:{self.word}:{self.reading}"


@dataclass
class Example:
    japanese: str
    chinese: str
    source_id: str
    source_name: str = "Japanese Grammar Notes"


@dataclass
class GrammarPoint:
    item_id: str
    level: str
    title: str
    structure: str
    meaning: str
    common_errors: str
    examples: list[Example]
    reference_examples: list[Example]
    vocabulary: list[VocabularyItem]
    source_path: str
    raw: str = field(repr=False)


def _plain(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = text.replace("`", "")
    return text.strip()


def _chinese_only(text: str) -> str:
    output: list[str] = []
    mode = "neutral"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == ":::zh":
            mode = "zh"
            continue
        if stripped == ":::en":
            mode = "en"
            continue
        if stripped == ":::" and mode in {"zh", "en"}:
            mode = "neutral"
            continue
        if mode != "en":
            output.append(line)
    return "\n".join(output).strip()


def _subsection(section: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(
            rf"^###\s+{re.escape(name)}(?:\|\|[^\n]*)?\s*$\n(.*?)(?=^###\s+|\Z)",
            section,
            flags=re.MULTILINE | re.DOTALL,
        )
        if match:
            return _chinese_only(match.group(1))
    return ""


def _compact_markdown(text: str, *, max_lines: int = 8) -> str:
    lines: list[str] = []
    for line in _chinese_only(text).splitlines():
        if line.lstrip().startswith("#"):
            continue
        stripped = _plain(line).strip()
        if not stripped or stripped == "---" or stripped.startswith("|"):
            continue
        lines.append(stripped)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def _parse_examples(section: str, source_path: str, item_id: str) -> list[Example]:
    body = _subsection(section, ("例句",))
    examples: list[Example] = []
    for line in body.splitlines():
        cleaned = _plain(line).strip()
        cleaned = re.sub(r"^(?:[-*]\s+|\d+[.)]\s*)", "", cleaned)
        if not cleaned or "❌" in cleaned:
            continue
        match = re.match(r"(.+?)[（(]([^（）()]*)[）)](?:\s*.*)?$", cleaned)
        if not match:
            continue
        japanese, chinese = match.group(1).strip(), match.group(2).strip()
        if not re.search(r"[ぁ-んァ-ヶ一-龯]", japanese):
            continue
        examples.append(
            Example(japanese, chinese, f"{source_path}#{item_id}:example-{len(examples) + 1}")
        )
    return examples


def _parse_vocabulary(text: str, source_path: str) -> list[VocabularyItem]:
    heading = re.search(
        r"^##\s+本课单词表(?:\|\|[^\n]*)?\s*$", text, flags=re.MULTILINE
    )
    if not heading:
        return []
    next_heading = re.search(r"^##\s+", text[heading.end() :], flags=re.MULTILINE)
    end = (
        heading.end() + next_heading.start()
        if next_heading is not None
        else len(text)
    )
    body = text[heading.end() : end]
    result: list[VocabularyItem] = []
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in {"单词", "---", "------"}:
            continue
        if set(cells[0]) <= {"-", ":"}:
            continue
        meaning = cells[2].split("//", 1)[0].strip()
        result.append(VocabularyItem(_plain(cells[0]), _plain(cells[1]), meaning, source_path))
    return result


def parse_lesson(path: Path, level: str) -> list[GrammarPoint]:
    text = path.read_text(encoding="utf-8")
    source_path = str(path.relative_to(GRAMMAR_ROOT))
    vocabulary = _parse_vocabulary(text, source_path)
    matches = list(HEADING_RE.finditer(text))
    points: list[GrammarPoint] = []
    for index, match in enumerate(matches):
        section = text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else None]
        number = match.group(1)
        title = _plain(match.group(2).split("||", 1)[0])
        item_id = f"grammar:{level}:{path.stem}:{number}"
        structure = _compact_markdown(_subsection(section, ("接続", "结构", "变形规则")), max_lines=6)
        meaning = _compact_markdown(
            _subsection(section, ("含义", "核心语感", "核心語感", "用法")), max_lines=8
        )
        if not meaning:
            meaning = _compact_markdown(section, max_lines=5)
        errors = _compact_markdown(_subsection(section, ("易错点", "注意", "常见错误")), max_lines=6)
        examples = _parse_examples(section, source_path, item_id)
        points.append(
            GrammarPoint(
                item_id=item_id,
                level=level,
                title=title,
                structure=structure,
                meaning=meaning,
                common_errors=errors,
                examples=examples,
                reference_examples=[],
                vocabulary=vocabulary,
                source_path=source_path,
                raw=section,
            )
        )
    lesson_examples = [example for point in points for example in point.examples]
    for point in points:
        point.reference_examples = lesson_examples
    return points


def load_curriculum(level: str) -> list[GrammarPoint]:
    level = level.upper()
    directory = GRAMMAR_ROOT / "grammar" / level
    if not directory.is_dir():
        raise FileNotFoundError(
            f"未找到 {directory}。先运行 python scripts/download_sources.py grammar"
        )
    points: list[GrammarPoint] = []
    for path in sorted(directory.glob("lesson*.md")):
        if path.name.startswith("lesson00") or "复习" in path.name:
            continue
        points.extend(parse_lesson(path, level))
    if not points:
        raise RuntimeError(f"{directory} 中没有解析到语法点")
    return points
