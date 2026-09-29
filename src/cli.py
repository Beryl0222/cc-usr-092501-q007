"""命令入口。

兼容旧用法：

    python3 -m src.cli <事件文件>          # 校验单条事件信封

新增后端命令：

    python3 -m src.cli verify <日志>                  # 校验事件链完整性
    python3 -m src.cli consumer <日志> <消费者标识>   # 消费者 API 视图
    python3 -m src.cli todo <日志> <门店标识>         # 门店待办（只含本店）
    python3 -m src.cli reconcile <日志> <YYYY-MM>     # 月度核对
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .envelope import validate_event
from .journal import EventJournal, TamperError
from .readmodel import consumer_view, monthly_reconciliation, store_todo
from .services import Backend


def _load_backend(path: str) -> Backend:
    journal = EventJournal(path)
    journal.verify_chain()
    return Backend(journal)


def _print_json(payload: object) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_validate(path: str) -> int:
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"无法读取事件：{error}", file=sys.stderr)
        return 2
    errors = validate_event(record)
    if errors:
        print("；".join(errors), file=sys.stderr)
        return 1
    print(f"事件有效：{record['event_id']}")
    return 0


def cmd_verify(path: str) -> int:
    try:
        journal = EventJournal(path)
        journal.verify_chain()
    except TamperError as error:
        print(f"事件链校验失败：{error}", file=sys.stderr)
        return 1
    print(f"事件链完整：{len(journal.events())} 条记录")
    return 0


def cmd_consumer(path: str, consumer_id: str) -> int:
    return _print_json(consumer_view(_load_backend(path), consumer_id))


def cmd_todo(path: str, store_id: str) -> int:
    return _print_json(store_todo(_load_backend(path), store_id))


def cmd_reconcile(path: str, month: str) -> int:
    report = monthly_reconciliation(_load_backend(path), month)
    _print_json(report)
    return 0 if report["balanced"] else 3


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 1 and not argv[0].startswith("-") and argv[0] not in {
        "verify", "consumer", "todo", "reconcile", "validate"
    }:
        return cmd_validate(argv[0])

    parser = argparse.ArgumentParser(prog="src.cli", description="承诺与争议处置后端命令")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="校验单条事件信封 JSON")
    p.add_argument("file")

    p = sub.add_parser("verify", help="校验只追加事件日志的哈希链")
    p.add_argument("journal")

    p = sub.add_parser("consumer", help="消费者视图：采用的合同与每笔退款计算")
    p.add_argument("journal")
    p.add_argument("consumer_id")

    p = sub.add_parser("todo", help="门店待办（仅限本店涉案事项）")
    p.add_argument("journal")
    p.add_argument("store_id")

    p = sub.add_parser("reconcile", help="月度核对承诺兑现、退款与未结调查")
    p.add_argument("journal")
    p.add_argument("month")

    args = parser.parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args.file)
    if args.command == "verify":
        return cmd_verify(args.journal)
    if args.command == "consumer":
        return cmd_consumer(args.journal, args.consumer_id)
    if args.command == "todo":
        return cmd_todo(args.journal, args.store_id)
    if args.command == "reconcile":
        return cmd_reconcile(args.journal, args.month)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
