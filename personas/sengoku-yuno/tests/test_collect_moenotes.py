"""Offline extraction checks with synthetic text, never downloaded scripts."""
import importlib.util
import json
import unittest
from html import escape
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "collect_moenotes.py"
SPEC = importlib.util.spec_from_file_location("yuno_collector", SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def line(name="由乃", speaker_id="adv_yuno", text="示例文本", kind="dialogue"):
    return {"speaker": name, "speakerId": speaker_id, "text": text, "kind": kind,
            "textId": "synthetic_1", "index": 1}


class CollectorTests(unittest.TestCase):
    def test_astro_decoder_nested_object_array(self):
        value = [0, {"rows": [1, [[0, {"id": [0, 15]}], [0, None]]]}]
        self.assertEqual(collector.unwrap(value), {"rows": [{"id": 15}, None]})
        with self.assertRaises(ValueError):
            collector.unwrap([2, "not a supported type"])

    def test_html_props_are_decoded_without_script_execution(self):
        props = escape(json.dumps({"locale": [0, "zh-CN"]}), quote=True)
        page = f'<astro-island component-url="/StoryDetail.synthetic.js" props="{props}"></astro-island>'.encode()
        self.assertEqual(collector.read_props(page, "StoryDetail."), {"locale": "zh-CN"})
        for bad_page in (b"<html>error</html>", page+page):
            with self.assertRaises(ValueError):
                collector.read_props(bad_page, "StoryDetail.")

    def test_solo_speaker_id_and_name_both_required(self):
        for speaker_id in ("yuno", "adv_yuno"):
            self.assertEqual(collector.classify(line(speaker_id=speaker_id)), "solo")
        for candidate in (line(speaker_id="adv_arale"), line(name="阿拉蕾")):
            with self.assertRaises(ValueError):
                collector.classify(candidate)
        self.assertEqual(collector.classify(line("阿拉蕾", "adv_arale")), "other")

    def test_group_is_separate_even_if_first_id_is_another_character(self):
        self.assertEqual(collector.classify(line("都子・由乃", "adv_miyako")), "group")
        self.assertEqual(collector.classify(line("由乃・都子")), "group")

    def test_timeline_orders_lines_and_does_not_duplicate_video_subtitles(self):
        script = {"lines": [{"text": "甲"}, {"text": "乙"}, {"text": "未播放"}],
                  "timeline": [{"kind": "video", "video": {}}, {"kind": "line", "line": 1},
                               {"kind": "line", "line": 0}]}
        self.assertEqual([r["text"] for r in collector.playback_lines(script)], ["乙", "甲"])

    def test_negative_timeline_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            collector.playback_lines({"lines": [line()], "timeline": [{"kind": "line", "line": -1}]})

    def test_rich_text_removes_formatting_and_preserves_literal_unknown_tags(self):
        self.assertEqual(collector.clean_text('<size=180%>甲<br><align="right">乙</align></size>'), "甲\n乙")
        self.assertEqual(collector.clean_text('甲<pos=3em><voffset=-1em>乙</voffset>'), "甲乙")
        self.assertEqual(collector.clean_text('<color="blue">space </color>'), "space ")
        self.assertEqual(collector.clean_text("<unknown>甲</unknown>"), "<unknown>甲</unknown>")

    def test_monologue_pause_and_chat_are_separate(self):
        self.assertEqual(collector.utterance_kind(line(text="（思考示例）")), "inner_monologue")
        self.assertEqual(collector.utterance_kind(line(text="……！？")), "punctuation_only")
        self.assertEqual(collector.utterance_kind(line(text="示例", kind="chat")), "chat_text")
        # Quotation marks also surround telephone replies, so no blanket exclusion.
        self.assertEqual(collector.utterance_kind(line(text="『示例回复』")), "spoken_text")

    def test_missing_chinese_is_rejected_instead_of_language_fallback(self):
        candidate = line(text="English fallback")
        props = {"locale": "zh-CN", "advId": 1,
                 "initialScript": {"locale": "zh-CN", "scriptName": "adv_script_synthetic",
                                   "lines": [candidate], "timeline": [{"kind": "line", "line": 0}]}}
        table = {"_allData": [{"_id": "synthetic_1", "_simplifiedChinese": "", "_english": "English fallback"}]}
        story = {"advId": 1, "assets": {"advEpisodeAsset": "adv_script_synthetic"}}
        with patch.object(collector, "read_props", return_value=props), \
             patch.object(collector, "fetch", side_effect=[(b"page", {}), (json.dumps(table).encode(), {})]):
            with self.assertRaisesRegex(ValueError, "mismatch or fallback"):
                collector.collect_story(story)


if __name__ == "__main__":
    unittest.main()
