from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .databases import import_jmdict, import_tatoeba, lookup_vocabulary, search_examples
from .download import download_sources
from .lesson import generate_lesson, record_lesson_published, record_lesson_result
from .storage import init_learner


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nihongo-tutor")
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="下载已登记的数据源")
    download.add_argument("sources", nargs="*", help="grammar、jmdict、tatoeba")
    download.add_argument("--refresh", action="store_true")

    init = sub.add_parser("init", help="初始化学习者")
    init.add_argument("learner_id")
    init.add_argument("--name", default="")
    init.add_argument("--level", default="N5")

    generate = sub.add_parser("lesson", help="生成每日课程")
    generate.add_argument("learner_id")
    generate.add_argument("--date", type=_date, default=date.today())
    generate.add_argument("--force", action="store_true")

    record = sub.add_parser("record", help="记录课程答题结果")
    record.add_argument("learner_id")
    record.add_argument("--lesson", type=_date, required=True)
    record.add_argument("--score", type=int, required=True)
    record.add_argument("--total", type=int, required=True)
    record.add_argument("--wrong", action="append", default=[])
    record.add_argument(
        "--source-learner-id",
        help="从另一学习轨迹读取课程元数据（用于群共享课程）",
    )

    published = sub.add_parser("published", help="记录共享课程已经发布")
    published.add_argument("learner_id")
    published.add_argument("--lesson", type=_date, required=True)

    jmdict = sub.add_parser("import-jmdict", help="导入 JMdict XML/GZ")
    jmdict.add_argument("path", type=Path)

    tatoeba = sub.add_parser("import-tatoeba", help="导入日中 Tatoeba 句对")
    tatoeba.add_argument("--japanese", type=Path, required=True)
    tatoeba.add_argument("--chinese", type=Path, required=True)
    tatoeba.add_argument("--links", type=Path, required=True)

    vocab = sub.add_parser("search-vocabulary")
    vocab.add_argument("word")
    examples = sub.add_parser("search-examples")
    examples.add_argument("query")
    examples.add_argument("--limit", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        print(json.dumps(download_sources(args.sources, refresh=args.refresh), ensure_ascii=False, indent=2))
    elif args.command == "init":
        print(init_learner(args.learner_id, display_name=args.name, level=args.level))
    elif args.command == "lesson":
        print(generate_lesson(args.learner_id, lesson_date=args.date, force=args.force))
    elif args.command == "record":
        if args.score < 0 or args.total < 1 or args.score > args.total:
            raise SystemExit("score 必须在 0..total 之间，且 total >= 1")
        result = record_lesson_result(
            args.learner_id,
            args.lesson,
            score=args.score,
            total=args.total,
            wrong_items=set(args.wrong),
            source_learner_id=args.source_learner_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "published":
        result = record_lesson_published(args.learner_id, args.lesson)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "import-jmdict":
        print(f"imported={import_jmdict(args.path)}")
    elif args.command == "import-tatoeba":
        print(f"imported={import_tatoeba(args.japanese, args.chinese, args.links)}")
    elif args.command == "search-vocabulary":
        print(json.dumps(lookup_vocabulary(args.word), ensure_ascii=False, indent=2))
    elif args.command == "search-examples":
        print(json.dumps(search_examples(args.query, args.limit), ensure_ascii=False, indent=2))
    return 0
