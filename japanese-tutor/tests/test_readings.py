from japanese_tutor.readings import annotate_kanji_readings, katakana_to_hiragana


def test_katakana_reading_is_converted_to_hiragana():
    assert katakana_to_hiragana("ニホンゴ") == "にほんご"


def test_annotation_uses_overrides_and_contextual_unidic_readings():
    assert annotate_kanji_readings(
        "私は学生です。", {"私": "わたし", "学生": "がくせい"}
    ) == "私（わたし）は学生（がくせい）です。"
    assert (
        annotate_kanji_readings("今日は日本語を勉強します。")
        == "今日（きょう）は日本語（にほんご）を勉強（べんきょう）します。"
    )
    assert annotate_kanji_readings("眠らなければなりません。") == (
        "眠（ねむ）らなければなりません。"
    )


def test_annotation_uses_common_pronoun_reading_and_is_idempotent():
    annotated = annotate_kanji_readings("私は中国人です。")

    assert annotated == "私（わたし）は中国人（ちゅうごくじん）です。"
    assert annotate_kanji_readings(annotated) == annotated


def test_annotation_keeps_kana_only_sentence_unchanged():
    assert annotate_kanji_readings("おはようございます。") == "おはようございます。"
