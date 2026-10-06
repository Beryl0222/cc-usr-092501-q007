"""只追加事件存储与并发/幂等控制。

存储介质为 JSONL：每行一个信封事件。进程崩溃后重新加载日志即可恢复全部状态，
不会产生"半笔付款"（整行写入或不写入）。同一进程内的并发命令以文件锁 +
每聚合版本号做乐观并发控制：两个会话基于同一版本做决定时，只有一个能提交，
另一个收到 :class:`ConflictError` 后必须重放再决定（用于并发和解场景）。
"""

from __future__ import annotations

import fcntl
import json
import threading
from collections import defaultdict
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from .envelope import validate_event


class DomainError(Exception):
    """领域规则被违反的基类。"""


class PermissionDenied(DomainError):
    """角色无权执行该命令。"""


class ConflictError(DomainError):
    """聚合版本已变化，调用方需重放后重试。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Exclusive(AbstractContextManager):
    """命令级临界区：进程内 RLock + 跨进程 flock，同进程内可重入。

    保证"检查状态 → 追加事件"在所有进程间原子，且持锁期间可安全重放日志，
    使并发和解只有一方提交、故障恢复后不会重复付款。
    """

    def __init__(self, store: "EventStore"):
        self.store = store

    def __enter__(self) -> "EventStore":
        store = self.store
        store._lock.acquire()
        if store._cmd_depth == 0:
            fcntl.flock(store._lock_fh.fileno(), fcntl.LOCK_EX)
        store._cmd_depth += 1
        return store

    def __exit__(self, *exc) -> None:
        store = self.store
        store._cmd_depth -= 1
        if store._cmd_depth == 0:
            fcntl.flock(store._lock_fh.fileno(), fcntl.LOCK_UN)
        store._lock.release()


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cmd_depth = 0
        self._lock_fh = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+")
        self._events: list[dict] = []
        self._event_ids: set[str] = set()
        self._versions: dict[str, int] = defaultdict(int)
        self._reload()

    def exclusive(self) -> _Exclusive:
        return _Exclusive(self)

    def close(self) -> None:
        with self._lock:
            if not self._lock_fh.closed:
                self._lock_fh.close()

    # ---- 恢复 ----------------------------------------------------------

    def _reload(self) -> None:
        """从日志重放（启动恢复或测试中外部修改后调用）。"""
        self._events.clear()
        self._event_ids.clear()
        self._versions = defaultdict(int)
        if not self.path.exists():
            return
        for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            errors = validate_event(event)
            if errors:
                raise DomainError(f"日志第 {line_no} 行事件损坏：{'；'.join(errors)}")
            if event["event_id"] in self._event_ids:
                raise DomainError(f"日志第 {line_no} 行事件标识重复：{event['event_id']}")
            expected = self._versions[event["aggregate_id"]] + 1
            if event["version"] != expected:
                raise DomainError(
                    f"日志第 {line_no} 行聚合 {event['aggregate_id']} 版本断档："
                    f"期望 {expected}，实际 {event['version']}"
                )
            self._event_ids.add(event["event_id"])
            self._versions[event["aggregate_id"]] = expected
            self._events.append(event)

    def reload(self) -> None:
        with self._lock:
            self._reload()

    def resync_if_changed(self) -> None:
        """命令临界区内发现外部进程已追加日志时的重放（并发和解用）。"""
        self._reload()

    # ---- 读取 ----------------------------------------------------------

    def events(self, aggregate_id: str | None = None) -> list[dict]:
        with self._lock:
            if aggregate_id is None:
                return [dict(e) for e in self._events]
            return [dict(e) for e in self._events if e["aggregate_id"] == aggregate_id]

    def version(self, aggregate_id: str) -> int:
        with self._lock:
            return self._versions[aggregate_id]

    def has_event_id(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._event_ids

    # ---- 写入 ----------------------------------------------------------

    def append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        data: dict,
        *,
        actor: dict,
        summary: str,
        event_id: str,
        occurred_at: str | None = None,
        expected_version: int | None = None,
    ) -> dict:
        """原子追加一个事件。须在 exclusive() 临界区内调用。

        expected_version 用于乐观并发：跨进程命令由临界区串行化后，仍校验
        调用方进入命令时记录的版本，过期快照（如并发和解）会得到 ConflictError。
        """
        with self._lock:
            if event_id in self._event_ids:
                raise DomainError(f"事件标识已存在：{event_id}")
            current = self._versions[aggregate_id]
            if expected_version is not None and expected_version != current:
                raise ConflictError(
                    f"聚合 {aggregate_id} 版本冲突：基于 {expected_version}，当前 {current}"
                )
            event = {
                "event_id": event_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": occurred_at or now_iso(),
                "version": current + 1,
                "summary": summary,
                "actor": {"id": actor["id"], "role": actor["role"]},
                "data": data,
            }
            errors = validate_event(event)
            if errors:
                raise DomainError("；".join(errors))
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            # 单行写入：行缓冲追加，崩溃不会出现半行或重复付款；行级锁由
            # exclusive() 临界区持有。
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
            self._events.append(event)
            self._event_ids.add(event_id)
            self._versions[aggregate_id] += 1
            return dict(event)
