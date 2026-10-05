"""Collect two CN Morfonica chapters and extract attributed dialogue locally."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw" / "bestdori" / "cn"
BASE_URL = "https://bestdori.com"
TARGET_ID = 30


def fetch_json(url: str, path: Path) -> tuple[dict, dict]:
    # Bestdori is accessible directly on this workstation, without its proxy.
    if path.is_file():
        data = path.read_bytes()
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": BASE_URL + "/"}
        )
        with opener.open(request, timeout=30) as response:
            data = response.read(5_000_001)
        if len(data) > 5_000_000:
            raise ValueError(f"Unexpectedly large JSON response: {url}")
        # Validate before caching; an HTML error page must never become an asset.
        json.loads(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError(f"Expected JSON object: {url}")
    return result, {
        "url": url,
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def classify(talk: dict) -> tuple[str, list[int]]:
    ids = {
        int(item["characterId"])
        for key in ("talkCharacters", "voices")
        for item in talk.get(key, [])
        if int(item.get("characterId", 0)) != 0
    }
    name = talk["windowDisplayName"]
    if TARGET_ID not in ids:
        if name == "瑠唯":
            raise ValueError("Rui name lacks target character/voice ID")
        return "other", sorted(ids)
    if ids != {TARGET_ID} or "・" in name:
        return "group", sorted(ids)
    if name not in {"瑠唯", "？？？"}:
        raise ValueError(f"Unrecognized target speaker alias: {name}")
    return "solo", sorted(ids)


def utterance_kind(text: str) -> str:
    """Use textual conventions, not a claim of audio verification."""
    text = text.strip()
    if text.startswith(("（", "(")):
        return "inner_monologue"
    if not any(char.isalnum() for char in text):
        return "punctuation_only"
    return "spoken_text"


def surface_metrics(rows: list[dict]) -> dict:
    texts = [r["text"] for r in rows if r["utterance_kind"] == "spoken_text"]
    lengths = [len("".join(text.split())) for text in texts]
    terms = ["仓田同学", "桐谷同学", "广町同学", "二叶同学", "如果", "不过", "只是", "应该"]
    return {
        "spoken_text_events": len(texts),
        "median_characters_per_event": median(lengths) if lengths else None,
        "events_containing_literal": {term: sum(term in t for t in texts) for term in terms},
        "events_with_ellipsis": sum("…" in t or "..." in t for t in texts),
        "events_with_exclamation": sum(any(c in t for c in "!！") for t in texts),
        "events_with_tilde": sum(any(c in t for c in "~～") for t in texts),
    }


def collect_scene(item: tuple) -> tuple[list[dict], dict]:
    chapter, chapter_title, story_id, story = item
    scenario = story["scenarioId"]
    if not re.fullmatch(r"band6-\d{3}", scenario):
        raise ValueError(f"Unexpected scenario ID: {scenario}")
    filename = f"Scenario{scenario}.asset"
    url = f"{BASE_URL}/assets/cn/scenario/band/021_rip/{filename}"
    asset, snapshot = fetch_json(url, RAW / filename)
    base = asset["Base"]
    talks = base["talkData"]
    refs = [(i, s["referenceIndex"]) for i, s in enumerate(base["snippets"]) if s["actionType"] == 1]
    rows = []
    for order, (snippet_index, talk_index) in enumerate(refs):
        if not isinstance(talk_index, int) or not 0 <= talk_index < len(talks):
            raise ValueError(f"Invalid talk reference: {scenario}:{talk_index}")
        talk = talks[talk_index]
        attribution, ids = classify(talk)
        rows.append({
            "row_id": f"cn:{story_id}:snippet:{snippet_index}:talk:{talk_index}",
            "server": "cn", "chapter": chapter, "chapter_title": chapter_title,
            "story_id": story_id, "scene_title": story["title"][3],
            "scenario_id": scenario, "order": order, "snippet_index": snippet_index,
            "talk_index": talk_index, "speaker": talk["windowDisplayName"],
            "character_ids": ids, "rui_attribution": attribution,
            "text": talk["body"],
            "utterance_kind": utterance_kind(talk["body"]),
            "voice_ids": [v["voiceId"] for v in talk.get("voices", [])],
            "source_url": url,
        })
    snapshot.update({
        "chapter": chapter, "story_id": story_id, "scenario_id": scenario,
        "scene_title": story["title"][3], "talk_data_count": len(talks),
        "played_talk_count": len(rows),
        "unreferenced_talk_count": len(talks) - len({ref for _, ref in refs}),
        "repeated_talk_reference_count": len(refs) - len({ref for _, ref in refs}),
    })
    return rows, snapshot


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    metadata, meta_snapshot = fetch_json(
        BASE_URL + "/api/misc/bandstories.5.json", RAW / "bandstories.json"
    )
    character, character_snapshot = fetch_json(
        BASE_URL + "/api/characters/30.json", RAW / "character30.json"
    )
    assert character["firstName"][3] == "瑠唯" and character["bandId"] == 21
    chapters = sorted(
        [c for c in metadata.values() if c["bandId"] == 21 and c["chapterNumber"] in (1, 2)],
        key=lambda c: c["chapterNumber"],
    )
    if [len(c["stories"]) for c in chapters] != [20, 15]:
        raise ValueError("Chapter coverage changed; inspect metadata before collection")
    work = [
        (c["chapterNumber"], c["subTitle"][3], int(story_id), story)
        for c in chapters for story_id, story in sorted(c["stories"].items(), key=lambda x: int(x[0]))
    ]
    rows, snapshots, target_rows = [], [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for scene_rows, snapshot in pool.map(collect_scene, work):
            snapshots.append(snapshot)
            rows.extend(scene_rows)
            for i, row in enumerate(scene_rows):
                if row["rui_attribution"] == "other":
                    continue
                target_rows.append({
                    **row,
                    "context_before": scene_rows[max(0, i - 2):i],
                    "context_after": scene_rows[i + 1:i + 3],
                })
            print(f"Collected story {snapshot['story_id']}: {len(scene_rows)} talk events", flush=True)
    for filename, content in (("dialogue.jsonl", rows), ("rui.jsonl", target_rows)):
        (RAW / filename).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in content), encoding="utf-8"
        )
    stats = {
        "server": "cn", "character_id": TARGET_ID,
        "attribution": "explicit_character_or_voice_id",
        "interpretation": "Labels identify speakers, not emotional intent or addressees.",
        "utterance_kind_method": "Parenthesis-prefixed text = inner_monologue; no alphanumeric characters = punctuation_only; remaining text = spoken_text. Textual heuristic, not audio verification.",
        "surface_metrics_method": "Rui solo spoken_text only. Count dialogue events, not sentences. Character length excludes whitespace, includes punctuation. Literal counts count events containing a string, not syntax or occurrence frequency.",
        "chapters": [],
    }
    for chapter in (1, 2):
        talks = [r for r in rows if r["chapter"] == chapter]
        solo = [r for r in talks if r["rui_attribution"] == "solo"]
        stats["chapters"].append({
            "chapter": chapter, "title": next(c["subTitle"][3] for c in chapters if c["chapterNumber"] == chapter),
            "scene_count": len({r["story_id"] for r in talks}),
            "all_talk_events": len(talks), "rui_solo_events": len(solo),
            "rui_group_events": sum(r["rui_attribution"] == "group" for r in talks),
            "rui_display_names": dict(Counter(r["speaker"] for r in solo)),
            "rui_unique_text_count": len({r["text"] for r in solo}),
            "rui_utterance_kinds": dict(Counter(r["utterance_kind"] for r in solo)),
            "surface_metrics": surface_metrics(solo),
        })
    write_json(ROOT / "corpus_stats.json", stats)
    manifest_path = ROOT / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_sources = [
        {"id": "bestdori_tutorial", "url": "https://www.bilibili.com/opus/1209357246072356870", "author": "涼风_青叶"},
        {"id": "bestdori_cn", "url": BASE_URL + "/api/misc/bandstories.5.json", "provenance": "第三方 Bestdori 托管的国服剧情资产，非社区创作或自行翻译。", "verified_against_game_recording": False},
    ]
    manifest["sources"] = [s for s in manifest["sources"] if s["id"] not in {x["id"] for x in new_sources}] + new_sources
    manifest["bestdori"] = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "server": "cn", "band_id": 21, "character_id": TARGET_ID,
        "metadata": [meta_snapshot, character_snapshot], "assets": snapshots,
        "derived_files": ["raw/bestdori/cn/dialogue.jsonl", "raw/bestdori/cn/rui.jsonl"],
        "dialogue_order": "snippets actionType=1 referenceIndex",
        "group_dialogue_excluded_from_solo_analysis": True,
        "speaker_labels_present": True,
        "utterance_kind_method": stats["utterance_kind_method"],
    }
    write_json(manifest_path, manifest)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
