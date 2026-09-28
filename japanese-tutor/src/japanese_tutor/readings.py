from __future__ import annotations

from functools import lru_cache
import re
from typing import Mapping

from fugashi import Tagger

from .databases import lookup_vocabulary


KANJI_RE = re.compile(r"[一-龯々〆ヵヶ]")
KANJI_ONLY_RE = re.compile(r"^[一-龯々〆ヵヶ]+$")
READING_ANNOTATION_RE = re.compile(r"(?<=[一-龯々〆ヵヶ])（[ぁ-ゖー]+）")
DEFAULT_READINGS = {"私": "わたし"}
_TAGGER: Tagger | None = None


def katakana_to_hiragana(value: str) -> str:
    """Convert standard full-width katakana readings to hiragana."""
    return "".join(
        chr(ord(character) - 0x60)
        if "ァ" <= character <= "ヶ"
        else character
        for character in value
    )


def _tagger() -> Tagger:
    global _TAGGER
    if _TAGGER is None:
        _TAGGER = Tagger()
    return _TAGGER


def _is_hiragana(character: str) -> bool:
    return "ぁ" <= character <= "ゖ" or character == "ー"


def _annotated_surface(surface: str, reading: str) -> str:
    """Keep visible okurigana outside the reading parentheses when possible."""
    prefix_length = 0
    while (
        prefix_length < len(surface)
        and prefix_length < len(reading)
        and _is_hiragana(surface[prefix_length])
        and surface[prefix_length] == reading[prefix_length]
    ):
        prefix_length += 1

    suffix_length = 0
    while (
        suffix_length < len(surface) - prefix_length
        and suffix_length < len(reading) - prefix_length
        and _is_hiragana(surface[-suffix_length - 1])
        and surface[-suffix_length - 1] == reading[-suffix_length - 1]
    ):
        suffix_length += 1

    surface_end = len(surface) - suffix_length if suffix_length else len(surface)
    reading_end = len(reading) - suffix_length if suffix_length else len(reading)
    written = surface[prefix_length:surface_end]
    kana = reading[prefix_length:reading_end]
    if not written or not kana or not KANJI_RE.search(written):
        return f"{surface}（{reading}）"
    return (
        surface[:prefix_length]
        + f"{written}（{kana}）"
        + (surface[surface_end:] if suffix_length else "")
    )


def _priority_rank(values: object) -> tuple[int, int]:
    priorities = [str(value) for value in values] if isinstance(values, list) else []
    if "ichi1" in priorities:
        category = 0
    elif "news1" in priorities:
        category = 1
    elif any(value.startswith("nf") for value in priorities):
        category = 2
    elif priorities:
        category = 3
    else:
        category = 4
    frequency = min(
        (
            int(value[2:])
            for value in priorities
            if value.startswith("nf") and value[2:].isdigit()
        ),
        default=99,
    )
    return category, frequency


@lru_cache(maxsize=4096)
def _dictionary_reading(surface: str) -> str:
    rows = lookup_vocabulary(surface)
    if not rows:
        return ""
    preferred = min(
        rows,
        key=lambda row: (
            _priority_rank(row.get("priority")),
            bool(re.search(r"[ァ-ヶ]", str(row.get("reading") or ""))),
        ),
    )
    return katakana_to_hiragana(str(preferred.get("reading") or ""))


def annotate_kanji_readings(
    sentence: str,
    overrides: Mapping[str, str] | None = None,
) -> str:
    """Add QQ-friendly hiragana readings after words containing kanji."""
    if not sentence or not KANJI_RE.search(sentence):
        return sentence
    sentence = READING_ANNOTATION_RE.sub("", sentence)
    known = dict(DEFAULT_READINGS)
    known.update(
        {str(word): str(reading) for word, reading in (overrides or {}).items()}
    )
    tokens = list(_tagger()(sentence))
    output: list[str] = []
    index = 0
    while index < len(tokens):
        surface = str(tokens[index].surface)
        if not KANJI_RE.search(surface):
            output.append(surface)
            index += 1
            continue

        chosen_surface = surface
        chosen_reading = known.get(surface, "")
        consumed = 1

        # UniDic-lite sometimes splits common all-kanji compounds such as 日本語.
        # Prefer the longest exact daily-vocabulary/JMdict compound before using
        # the token's contextual UniDic reading.
        upper = min(len(tokens), index + 5)
        for end in range(upper, index + 1, -1):
            candidate = "".join(str(token.surface) for token in tokens[index:end])
            if not KANJI_ONLY_RE.fullmatch(candidate):
                continue
            reading = known.get(candidate) or _dictionary_reading(candidate)
            if reading:
                chosen_surface = candidate
                chosen_reading = reading
                consumed = end - index
                break

        if not chosen_reading:
            chosen_reading = katakana_to_hiragana(
                str(getattr(tokens[index].feature, "kana", "") or "")
            )
        if chosen_reading:
            output.append(_annotated_surface(chosen_surface, chosen_reading))
        else:
            output.append(chosen_surface)
        index += consumed
    return "".join(output)
