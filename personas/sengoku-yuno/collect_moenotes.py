"""Collect a bounded Yuno corpus from Moenotes; verify Chinese asset fields."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw" / "moenotes"
SITE = "https://bdon.moe"
ASSETS = "https://assets.bdon.moe"


def fetch(url: str, path: Path) -> tuple[bytes, dict]:
    if path.exists():
        data = path.read_bytes()
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(request, timeout=30) as response:
            data = response.read(5_000_001)
        if len(data) > 5_000_000:
            raise ValueError(f"Oversized response: {url}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data, {"url": url, "path": str(path.relative_to(ROOT)),
                  "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def unwrap(value):
    """Decode only Astro JSON object/array/scalar tags, without executing JS."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Invalid Astro value")
    tag, payload = value
    if tag == 0:
        return {k: unwrap(v) for k, v in payload.items()} if isinstance(payload, dict) else payload
    if tag == 1 and isinstance(payload, list):
        return [unwrap(v) for v in payload]
    raise ValueError(f"Unsupported Astro tag: {tag}")


class IslandParser(HTMLParser):
    def __init__(self, component: str):
        super().__init__()
        self.component = component
        self.matches = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "astro-island" and self.component in attributes.get("component-url", ""):
            props = json.loads(attributes["props"])
            self.matches.append({key: unwrap(value) for key, value in props.items()})


def read_props(data: bytes, component: str) -> dict:
    parser = IslandParser(component)
    parser.feed(data.decode("utf-8"))
    if len(parser.matches) != 1:
        raise ValueError(f"Expected one {component} island; got {len(parser.matches)}")
    return parser.matches[0]


def playback_lines(script: dict) -> list[dict]:
    lines = script["lines"]
    output = []
    for item in script["timeline"]:
        if item["kind"] != "line":
            continue
        index = item["line"]
        if type(index) is not int or not 0 <= index < len(lines):
            raise ValueError("Invalid timeline line reference")
        output.append(lines[index])
    return output


def classify(line: dict) -> str:
    names = [name.strip() for name in line["speaker"].split("・")]
    target_named = "由乃" in names
    target_id = line["speakerId"] in {"yuno", "adv_yuno"}
    if target_named and len(names) > 1:
        return "group"
    if target_named != target_id:
        raise ValueError(f"Unrecognized or inconsistent target label: {line['speakerId']!r}, {line['speaker']!r}")
    return "solo" if target_named else "other"


def utterance_kind(line: dict) -> str:
    text = line["text"].strip()
    if line["kind"] == "chat":
        return "chat_text"
    if text.startswith(("（", "(")):
        return "inner_monologue"
    if not any(c.isalnum() for c in text):
        return "punctuation_only"
    return "spoken_text"


def clean_text(text: str) -> str:
    # Match the reader's rich-text allowlist; unknown tags remain literal text.
    allowed = set("align alpha b br color cspace font font-weight gradient i indent line-height line-indent link lowercase margin mark mspace nobr noparse page pos r rotate ruby s size smallcaps space sprite strikethrough style sub sup u uppercase voffset width".split())
    def replace(match):
        name = match[2].lower()
        if name not in allowed:
            return match[0]
        return "\n" if name == "br" else ""
    # The reader trims the original field before removing formatting tags.
    return re.sub(r'<(/?)([a-z][a-z-]*)(?:\s*=\s*"?([^">]*)"?)?\s*>', replace, text.strip(), flags=re.I)


def collect_story(story: dict) -> tuple[list[dict], list[dict]]:
    adv_id = story["advId"]
    url = f"{SITE}/story/{adv_id}"
    data, page_snapshot = fetch(url, RAW / f"story-{adv_id}.html")
    props = read_props(data, "StoryDetail.")
    if props["locale"] != "zh-CN" or props["advId"] != adv_id:
        raise ValueError("Wrong story ID or page locale")
    script = props["initialScript"]
    if not script or script["locale"] != "zh-CN":
        raise ValueError("Chinese script unavailable")
    script_name = script["scriptName"]
    if not re.fullmatch(r"adv_script_[A-Za-z0-9_]+", script_name) or script_name != story["assets"]["advEpisodeAsset"]:
        raise ValueError(f"Unexpected script name: {script_name}")
    table = script_name + "-Text"
    asset_url = f"{ASSETS}/zh-Hans/Adv/Episode/{script_name}/{table}/{table}.json"
    asset_data, asset_snapshot = fetch(asset_url, RAW / f"{table}.json")
    text_table = json.loads(asset_data)["_allData"]
    text_lookup = {text["_id"]: text for text in text_table}
    annotations = json.loads((ROOT / "annotations.json").read_text())["annotations"]
    overrides = {a["text_id"]: a for a in annotations if a["adv_id"] == adv_id}
    rows = []
    for order, line in enumerate(playback_lines(script)):
        source = text_lookup[line["textId"]]
        chinese = source.get("_simplifiedChinese", "")
        if not chinese.strip() or clean_text(chinese) != line["text"]:
            raise ValueError(f"Chinese source mismatch or fallback: {adv_id}:{line['textId']}")
        attribution = classify(line)
        rows.append({
            "row_id": f"zh-CN:{adv_id}:order:{order}:index:{line['index']}",
            "locale": "zh-CN", "adv_id": adv_id, "script_name": script_name,
            "episode_kind": story["episodeKind"], "episode_number": story["episodeNumber"],
            "title": story["title"], "chapter": story["chapterName"],
            "order": order, "index": line["index"], "line_kind": line["kind"],
            "speaker_id": line["speakerId"], "speaker": line["speaker"],
            "yuno_attribution": attribution, "text_id": line["textId"], "text": line["text"],
            "utterance_kind": overrides.get(line["textId"], {}).get("utterance_kind", utterance_kind(line)),
            "annotation_reason": overrides.get(line["textId"], {}).get("reason"),
            "channel": "video_subtitle" if "videoId" in line else "game_text",
            "video_id": line.get("videoId"), "cue": line.get("cue"),
            "source_url": url, "text_asset_url": asset_url,
            "verified_text_field": "_simplifiedChinese",
        })
    for snapshot in (page_snapshot, asset_snapshot):
        snapshot.update(adv_id=adv_id, episode_kind=story["episodeKind"], title=story["title"])
    return rows, [page_snapshot, asset_snapshot]


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    data, index_snapshot = fetch(SITE + "/story/main", RAW / "main.html")
    props = read_props(data, "StoryExplorer.")
    stories = [s for s in props["initialStories"] if s["bandId"] == 3 and
               (s["episodeKind"] in {"main", "extra"} or (s["episodeKind"] == "another" and s["episodeNumber"] == 5))]
    if Counter(s["episodeKind"] for s in stories) != {"main": 20, "extra": 3, "another": 1}:
        raise ValueError("Story coverage changed; inspect metadata")
    data, character_snapshot = fetch(SITE + "/characters/15", RAW / "character15.html")
    character = read_props(data, "CharacterDetail.")["initialData"]["value"]["character"]
    if (character["id"], character["bandId"], character["shortName"]) != (15, 3, "由乃"):
        raise ValueError("Wrong character profile")
    write_json(RAW / "profile.json", character)
    write_json(RAW / "stories.json", stories)
    rows, targets, snapshots = [], [], [index_snapshot, character_snapshot]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for scene_rows, scene_snapshots in pool.map(collect_story, stories):
            snapshots.extend(scene_snapshots)
            rows.extend(scene_rows)
            for index, row in enumerate(scene_rows):
                if row["yuno_attribution"] != "other":
                    targets.append({**row, "context_before": scene_rows[max(0, index-2):index],
                                    "context_after": scene_rows[index+1:index+3]})
            print(f"Collected ADV {scene_snapshots[0]['adv_id']}: {len(scene_rows)} events", flush=True)
    for filename, content in (("dialogue.jsonl", rows), ("yuno.jsonl", targets)):
        (RAW / filename).write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in content), encoding="utf-8")
    solo = [r for r in targets if r["yuno_attribution"] == "solo"]
    spoken = [r for r in solo if r["utterance_kind"] == "spoken_text"]
    stats = {
        "locale": "zh-CN", "character_id": 15, "speaker_ids": dict(Counter(r["speaker_id"] for r in solo)),
        "story_count": len(stories), "all_text_events": len(rows),
        "yuno_solo_events": len(solo), "yuno_group_events": len(targets)-len(solo),
        "yuno_utterance_kinds": dict(Counter(r["utterance_kind"] for r in solo)),
        "yuno_channels": dict(Counter(r["channel"] for r in solo)),
        "by_episode_kind": [{"kind": kind, "story_count": sum(s["episodeKind"] == kind for s in stories),
                             "yuno_solo_events": sum(r["episode_kind"] == kind for r in solo)}
                            for kind in ("main", "another", "extra")],
        "surface_metrics": {"scope": "solo spoken_text, playback events not sentences",
            "median_characters_excluding_whitespace": median(len("".join(r["text"].split())) for r in spoken),
            "events_containing_literal": {term: sum(term in r["text"] for r in spoken)
                for term in ("我说", "算了", "麻烦", "吧", "呢", "宫永同学", "藤同学", "仲町同学", "峰月同学")},
            "events_with_ellipsis": sum("…" in r["text"] or "..." in r["text"] for r in spoken),
            "events_with_exclamation": sum(any(c in r["text"] for c in "!！") for r in spoken)},
        "utterance_kind_method": "chat kind first; parentheses prefix = inner_monologue; no alphanumeric = punctuation_only; remaining = spoken_text. Text convention, not audio verification.",
    }
    write_json(ROOT / "corpus_stats.json", stats)
    path = ROOT / "source_manifest.json"
    manifest = json.loads(path.read_text()) if path.exists() else {"version": 1, "sources": []}
    manifest["collection"] = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": SITE, "band_id": 3, "character_id": 15, "requested_locale": "zh-CN",
        "server_selection": "site maps Chinese locale to tw (international export), distinct from Garupa CN",
        "text_verification": "Every normalized text matches the original exported _simplifiedChinese field; empty/fallback/mismatched text aborts collection.",
        "ordering": "initialScript.timeline kind=line references into initialScript.lines; video subtitles retained once",
        "speaker_attribution": "normalized speakerId yuno/adv_yuno cross-checked with speaker name; group lines excluded from solo",
        "provenance": "Third-party-hosted multilingual game text export; not independently verified against official client/recording.",
        "snapshots": snapshots,
        "derived_files": ["raw/moenotes/dialogue.jsonl", "raw/moenotes/yuno.jsonl", "raw/moenotes/profile.json", "raw/moenotes/stories.json"],
        "raw_git_ignored": True,
        "manual_annotations": "annotations.json: known readings of another character's posts excluded from personal speech metrics; telephone replies in quotation marks remain spoken_text.",
    }
    write_json(path, manifest)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
