"""命令入口。

用法：
    python3 -m src.cli validate   <事件文件>          校验单条领域事件信封
    python3 -m src.cli reconcile  <日志文件> <YYYY-MM> 输出月度核对结果
    python3 -m src.cli consumer   <日志文件> <消费者ID> 输出合同采用版本与每笔退款计算
    python3 -m src.cli store-todo <日志文件> <员工ID>  输出本店待办
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .domain import CampDomain
from .envelope import validate_event
from .store import DomainError, EventStore


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) == 2 and argv[0] == "validate":
        try:
            record = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"无法读取事件：{error}", file=sys.stderr)
            return 2
        errors = validate_event(record)
        if errors:
            print("；".join(errors), file=sys.stderr)
            return 1
        print(f"事件有效：{record['event_id']}")
        return 0

    if len(argv) == 3 and argv[0] in {"reconcile", "consumer", "store-todo"}:
        command, log_path, arg = argv
        try:
            domain = CampDomain(EventStore(log_path))
            if command == "reconcile":
                _print(domain.reconciliation_findings(arg))
            elif command == "consumer":
                _print(domain.consumer_view(arg))
            else:
                staff = domain.state.staff.get(arg)
                if staff is None:
                    print(f"员工未登记：{arg}", file=sys.stderr)
                    return 1
                _print(domain.store_todo({"id": arg, "role": staff["role"]}))
        except DomainError as error:
            print(f"领域错误：{error}", file=sys.stderr)
            return 1
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
