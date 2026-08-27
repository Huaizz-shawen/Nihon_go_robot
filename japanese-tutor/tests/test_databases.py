from pathlib import Path

from japanese_tutor.databases import import_jmdict, import_tatoeba, lookup_vocabulary, search_examples


def test_import_jmdict_and_lookup(tmp_path: Path) -> None:
    source = tmp_path / "jmdict.xml"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<JMdict><entry><ent_seq>1358280</ent_seq><k_ele><keb>食べる</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>たべる</reb></r_ele><sense><pos>Ichidan verb</pos><gloss>to eat</gloss></sense>
</entry></JMdict>""",
        encoding="utf-8",
    )
    database = tmp_path / "jmdict.sqlite"

    assert import_jmdict(source, database) == 1
    rows = lookup_vocabulary("食べる", database)
    assert rows[0]["reading"] == "たべる"
    assert rows[0]["meanings"] == ["to eat"]


def test_import_tatoeba_preserves_ids_and_owners(tmp_path: Path) -> None:
    japanese = tmp_path / "jpn.tsv"
    chinese = tmp_path / "cmn.tsv"
    links = tmp_path / "links.tsv"
    japanese.write_text("1\tjpn\t私はパンを食べます。\tyuki\t2020\t2021\n", encoding="utf-8")
    chinese.write_text("2\tcmn\t我吃面包。\tming\t2020\t2021\n", encoding="utf-8")
    links.write_text("1\t2\n2\t1\n", encoding="utf-8")
    database = tmp_path / "tatoeba.sqlite"

    assert import_tatoeba(japanese, chinese, links, database) == 1
    rows = search_examples("食べ", db_path=database)
    assert rows[0]["jp_id"] == 1
    assert rows[0]["zh_id"] == 2
    assert rows[0]["jp_owner"] == "yuki"
    assert rows[0]["license"] == "CC-BY-2.0-FR"
