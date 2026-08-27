from __future__ import annotations

import bz2
import gzip
import io
import json
import sqlite3
import tarfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from .storage import PROJECT_ROOT


VOCAB_DB = PROJECT_ROOT / "knowledge" / "vocabulary" / "jmdict.sqlite"
EXAMPLE_DB = PROJECT_ROOT / "knowledge" / "examples" / "tatoeba.sqlite"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_vocabulary_db(path: Path = VOCAB_DB) -> sqlite3.Connection:
    connection = _connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS vocabulary (
            id INTEGER PRIMARY KEY,
            ent_seq TEXT NOT NULL,
            word TEXT NOT NULL,
            reading TEXT NOT NULL,
            pos_json TEXT NOT NULL,
            meanings_json TEXT NOT NULL,
            priority_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'JMdict'
        );
        CREATE INDEX IF NOT EXISTS vocabulary_word_idx ON vocabulary(word);
        CREATE INDEX IF NOT EXISTS vocabulary_reading_idx ON vocabulary(reading);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    return connection


def _open_binary(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    return path.open("rb")


def import_jmdict(xml_path: Path, db_path: Path = VOCAB_DB) -> int:
    connection = init_vocabulary_db(db_path)
    connection.execute("DELETE FROM vocabulary")
    inserted = 0
    with _open_binary(xml_path) as stream:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag.rsplit("}", 1)[-1] != "entry":
                continue
            ent_seq = elem.findtext("ent_seq") or ""
            words = [node.findtext("keb") or "" for node in elem.findall("k_ele")]
            readings = [node.findtext("reb") or "" for node in elem.findall("r_ele")]
            words = [word for word in words if word] or readings[:1]
            senses = elem.findall("sense")
            pos = sorted({node.text or "" for sense in senses for node in sense.findall("pos") if node.text})
            meanings: list[str] = []
            for sense in senses:
                for gloss in sense.findall("gloss"):
                    language = gloss.attrib.get(XML_LANG) or gloss.attrib.get("lang") or "eng"
                    if language == "eng" and gloss.text:
                        meanings.append(gloss.text)
            priorities = sorted(
                {
                    node.text or ""
                    for container in list(elem.findall("k_ele")) + list(elem.findall("r_ele"))
                    for node in list(container.findall("ke_pri")) + list(container.findall("re_pri"))
                    if node.text
                }
            )
            for word in words:
                for reading in readings or [word]:
                    connection.execute(
                        "INSERT INTO vocabulary(ent_seq, word, reading, pos_json, meanings_json, priority_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            ent_seq,
                            word,
                            reading,
                            json.dumps(pos, ensure_ascii=False),
                            json.dumps(meanings, ensure_ascii=False),
                            json.dumps(priorities, ensure_ascii=False),
                        ),
                    )
                    inserted += 1
            elem.clear()
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('license', 'CC-BY-SA-4.0')"
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('attribution', "
        "'Electronic Dictionary Research and Development Group (EDRDG)')"
    )
    connection.commit()
    connection.close()
    return inserted


def lookup_vocabulary(word: str, db_path: Path = VOCAB_DB) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT ent_seq, word, reading, pos_json, meanings_json, priority_json "
            "FROM vocabulary WHERE word = ? OR reading = ? LIMIT 20",
            (word, word),
        ).fetchall()
    return [
        {
            "ent_seq": row["ent_seq"],
            "word": row["word"],
            "reading": row["reading"],
            "pos": json.loads(row["pos_json"]),
            "meanings": json.loads(row["meanings_json"]),
            "priority": json.loads(row["priority_json"]),
        }
        for row in rows
    ]


@contextmanager
def _open_tsv(path: Path) -> Iterator[TextIO]:
    if path.name.endswith(".tar.bz2"):
        with tarfile.open(path, "r:bz2") as archive:
            member = next(item for item in archive.getmembers() if item.isfile())
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"Archive has no readable TSV: {path}")
            with io.TextIOWrapper(extracted, encoding="utf-8", newline="") as text:
                yield text
        return
    if path.suffix == ".bz2":
        with bz2.open(path, "rt", encoding="utf-8", newline="") as text:
            yield text
        return
    with path.open("r", encoding="utf-8", newline="") as text:
        yield text


def _read_sentences(path: Path, expected_lang: str) -> dict[int, tuple[str, str]]:
    sentences: dict[int, tuple[str, str]] = {}
    with _open_tsv(path) as handle:
        for line in handle:
            cells = line.rstrip("\n").split("\t")
            if len(cells) < 3 or cells[1] != expected_lang:
                continue
            try:
                sentence_id = int(cells[0])
            except ValueError:
                continue
            owner = cells[3] if len(cells) > 3 else ""
            sentences[sentence_id] = (cells[2], owner)
    return sentences


def import_tatoeba(
    japanese_path: Path,
    chinese_path: Path,
    links_path: Path,
    db_path: Path = EXAMPLE_DB,
) -> int:
    japanese = _read_sentences(japanese_path, "jpn")
    chinese = _read_sentences(chinese_path, "cmn")
    connection = _connect(db_path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sentence_pairs (
            jp_id INTEGER NOT NULL,
            zh_id INTEGER NOT NULL,
            japanese TEXT NOT NULL,
            chinese TEXT NOT NULL,
            jp_owner TEXT NOT NULL,
            zh_owner TEXT NOT NULL,
            license TEXT NOT NULL,
            PRIMARY KEY (jp_id, zh_id)
        );
        CREATE INDEX IF NOT EXISTS sentence_pairs_japanese_idx ON sentence_pairs(japanese);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        DELETE FROM sentence_pairs;
        """
    )
    inserted = 0
    batch: list[tuple[object, ...]] = []
    with _open_tsv(links_path) as links:
        for line in links:
            cells = line.rstrip("\n").split("\t")
            if len(cells) < 2:
                continue
            try:
                left, right = int(cells[0]), int(cells[1])
            except ValueError:
                continue
            if left not in japanese or right not in chinese:
                continue
            jp_text, jp_owner = japanese[left]
            zh_text, zh_owner = chinese[right]
            batch.append((left, right, jp_text, zh_text, jp_owner, zh_owner, "CC-BY-2.0-FR"))
            if len(batch) >= 1000:
                connection.executemany(
                    "INSERT OR IGNORE INTO sentence_pairs VALUES (?, ?, ?, ?, ?, ?, ?)", batch
                )
                inserted += len(batch)
                batch.clear()
    if batch:
        connection.executemany(
            "INSERT OR IGNORE INTO sentence_pairs VALUES (?, ?, ?, ?, ?, ?, ?)", batch
        )
        inserted += len(batch)
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('attribution', 'Tatoeba contributors')"
    )
    connection.commit()
    actual = connection.execute("SELECT count(*) FROM sentence_pairs").fetchone()[0]
    connection.close()
    return int(actual)


def search_examples(query: str, limit: int = 10, db_path: Path = EXAMPLE_DB) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    escaped = query.replace("%", "\\%").replace("_", "\\_")
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM sentence_pairs WHERE japanese LIKE ? ESCAPE '\\' LIMIT ?",
            (f"%{escaped}%", limit),
        ).fetchall()
    return [dict(row) for row in rows]
