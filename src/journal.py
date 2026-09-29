"""只追加事件日志：冻结领域事实的存储原语。

每条记录落盘时被包装成一个带内容指纹与链式哈希的信封：

- ``event_id`` 全局唯一：同一事件标识重复提交时返回已有信封，绝不产生第二行
  （同一支付或证据回执完整重传保持一次）。
- 信封内容经规范化 JSON 计算 SHA-256；每行还保存前一行的哈希，任何删除、
  插入或事后改写都会在重放时被发现。
- 本模块只保证“追加”和“可验证”，业务上的不可变（原始测量、安全事件不得改写）
  由领域服务在追加前拦截。
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GENESIS = "0" * 64


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(payload: Any) -> str:
    """对任意 JSON 可序列化内容计算稳定指纹（含键排序，无多余空白）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def file_fingerprint(path: str | Path) -> str:
    """计算上传证据文件的 SHA-256 指纹。"""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Envelope(dict):
    """一条已冻结的信封记录（dict 形态，可直接 JSON 序列化）。"""

    @property
    def seq(self) -> int:
        return self["seq"]

    @property
    def event_id(self) -> str:
        return self["event_id"]

    @property
    def body(self) -> dict[str, Any]:
        return copy.deepcopy(self["body"])

    @property
    def digest(self) -> str:
        return self["digest"]

    @property
    def prev_digest(self) -> str:
        return self["prev_digest"]


class TamperError(RuntimeError):
    """重放时发现日志被删除、插入或原地修改。"""


class DuplicateContentError(ValueError):
    """同一 event_id 被用于提交与首次不同的内容。

    事件标识一旦接收不得原地复用为另一份内容（领域约定）。
    """


class EventJournal:
    """线程安全的只追加 JSONL 日志，崩溃恢复后重放即可还原全部事实。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._by_id: dict[str, Envelope] = {}
        self._order: list[Envelope] = []
        self._tail = GENESIS
        self._replay()

    # ---------- 重放 ----------

    def _replay(self) -> None:
        if not self.path.exists():
            return
        prev = GENESIS
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                env = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TamperError(f"第 {lineno} 行不是合法 JSON：{exc}") from exc
            self._verify_envelope(env, lineno)
            if env.get("seq") != lineno:
                raise TamperError(f"第 {lineno} 行序号断裂：{env.get('seq')}（存在删除或插入）")
            if env.get("prev_digest") != prev:
                raise TamperError(f"第 {lineno} 行链指针断裂（存在删除或插入）")
            wrapped = Envelope(env)
            self._order.append(wrapped)
            self._by_id[wrapped.event_id] = wrapped
            self._tail = wrapped.digest
            prev = wrapped.digest

    @staticmethod
    def _verify_envelope(env: dict[str, Any], lineno: int) -> None:
        for key in ("seq", "event_id", "digest", "prev_digest", "logged_at", "body"):
            if key not in env:
                raise TamperError(f"第 {lineno} 行缺少信封字段：{key}")
        expected_digest = canonical_digest(
            {
                "seq": env["seq"],
                "event_id": env["event_id"],
                "prev_digest": env["prev_digest"],
                "logged_at": env["logged_at"],
                "body": env["body"],
            }
        )
        if not hmac.compare_digest(str(env["digest"]), str(expected_digest)):
            raise TamperError(f"第 {lineno} 行内容与指纹不一致（记录被改写）")

    def verify_chain(self) -> None:
        """显式完整性校验：链序、行号、event_id 唯一性与链式哈希。"""
        with self._lock:
            prev = GENESIS
            seen: set[str] = set()
            for idx, env in enumerate(self._order, 1):
                if env.seq != idx:
                    raise TamperError(f"第 {idx} 行序号断裂：{env.seq}")
                if env.prev_digest != prev:
                    raise TamperError(f"第 {idx} 行链指针断裂（存在删除或插入）")
                if env.event_id in seen:
                    raise TamperError(f"event_id 在日志中重复出现：{env.event_id}")
                seen.add(env.event_id)
                prev = env.digest

    # ---------- 追加 ----------

    def append(self, body: dict[str, Any], event_id: str | None = None) -> Envelope:
        """追加一条事件。

        未提供 ``event_id`` 时由 ``body['event_id']`` 决定；该 id 已存在时：
        内容完全一致 → 返回原信封（幂等重传）；内容不同 → 抛
        :class:`DuplicateContentError`。
        """
        eid = event_id or body.get("event_id")
        if not eid:
            raise ValueError("事件必须携带 event_id")
        with self._lock:
            existing = self._by_id.get(eid)
            if existing is not None:
                if canonical_digest(body) == canonical_digest(existing.body):
                    return existing
                raise DuplicateContentError(
                    f"event_id {eid} 已冻结另一份内容，不得复用标识改写事实"
                )
            env: dict[str, Any] = {
                "seq": len(self._order) + 1,
                "event_id": eid,
                "prev_digest": self._tail,
                "logged_at": utc_now_iso(),
                "body": copy.deepcopy(body),
            }
            env["digest"] = canonical_digest(env)
            line = json.dumps(env, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            wrapped = Envelope(env)
            self._order.append(wrapped)
            self._by_id[eid] = wrapped
            self._tail = wrapped.digest
            return wrapped

    # ---------- 读取 ----------

    def events(self) -> list[Envelope]:
        with self._lock:
            return list(self._order)

    def stream(self, event_type: str | None = None) -> Iterable[Envelope]:
        with self._lock:
            for env in self._order:
                if event_type is None or env.body.get("event_type") == event_type:
                    yield env

    def get(self, event_id: str) -> Envelope | None:
        with self._lock:
            return self._by_id.get(event_id)
