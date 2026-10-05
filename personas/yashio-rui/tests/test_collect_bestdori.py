"""Offline checks for attribution and playback order; no copyrighted fixtures."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "collect_bestdori.py"
SPEC = importlib.util.spec_from_file_location("rui_collector", SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def talk(name="瑠唯", character_id=30, body="示例文本"):
    return {"windowDisplayName": name, "body": body,
            "talkCharacters": [{"characterId": character_id}], "voices": []}


class CollectorTests(unittest.TestCase):
    def test_unknown_display_name_with_explicit_id_is_target(self):
        self.assertEqual(collector.classify(talk("？？？")), ("solo", [30]))

    def test_voice_id_identifies_target_without_talk_character(self):
        line = talk(character_id=0)
        line["voices"] = [{"characterId": 30, "voiceId": "synthetic"}]
        self.assertEqual(collector.classify(line), ("solo", [30]))

    def test_other_unknown_speaker_is_not_target(self):
        self.assertEqual(collector.classify(talk("？？？", 26)), ("other", [26]))

    def test_name_without_id_and_unrecognized_alias_fail(self):
        for line in (talk(character_id=26), talk("陌生别名")):
            with self.subTest(line=line), self.assertRaises(ValueError):
                collector.classify(line)

    def test_group_is_not_solo_even_with_target_voice(self):
        line = talk("瑠唯・真白")
        line["voices"] = [{"characterId": 26, "voiceId": "synthetic"}]
        self.assertEqual(collector.classify(line), ("group", [26, 30]))

    def test_monologue_and_pauses_are_separate_from_speech(self):
        cases = {"（内心示例）": "inner_monologue", "(example)": "inner_monologue",
                 "…………！": "punctuation_only", "？": "punctuation_only",
                 "……可以吗？": "spoken_text"}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(collector.utterance_kind(text), expected)

    def test_playback_references_determine_order_and_exclude_unused_talk(self):
        asset = {"Base": {"talkData": [talk(body="甲"), talk(body="乙"), talk(body="未播放")],
                          "snippets": [{"actionType": 2, "referenceIndex": 999},
                                       {"actionType": 1, "referenceIndex": 1},
                                       {"actionType": 1, "referenceIndex": 0},
                                       {"actionType": 1, "referenceIndex": 1}]}}
        with patch.object(collector, "fetch_json", return_value=(asset, {})):
            rows, snapshot = collector.collect_scene((1, "synthetic", 303,
                                      {"scenarioId": "band6-001", "title": [None]*3+["synthetic"]}))
        self.assertEqual([r["text"] for r in rows], ["乙", "甲", "乙"])
        self.assertEqual([r["snippet_index"] for r in rows], [1, 2, 3])
        self.assertEqual(len({r["row_id"] for r in rows}), 3)
        self.assertEqual(snapshot["unreferenced_talk_count"], 1)
        self.assertEqual(snapshot["repeated_talk_reference_count"], 1)

    def test_invalid_talk_reference_fails_instead_of_using_wrong_context(self):
        asset = {"Base": {"talkData": [talk()],
                          "snippets": [{"actionType": 1, "referenceIndex": -1}]}}
        with patch.object(collector, "fetch_json", return_value=(asset, {})):
            with self.assertRaises(ValueError):
                collector.collect_scene((1, "synthetic", 303,
                                  {"scenarioId": "band6-001", "title": [None]*3+["synthetic"]}))

    def test_surface_counts_exclude_monologues_and_pauses(self):
        lines = ["（如果成功！）", "……！", "如果可以，先试一次。", "如果如果"]
        metrics = collector.surface_metrics([{"text": t, "utterance_kind": collector.utterance_kind(t)} for t in lines])
        self.assertEqual(metrics["spoken_text_events"], 2)
        self.assertEqual(metrics["events_with_exclamation"], 0)
        self.assertEqual(metrics["events_containing_literal"]["如果"], 2)


if __name__ == "__main__":
    unittest.main()
