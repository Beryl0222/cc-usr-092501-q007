"""承诺与争议处置后端：应用服务 + 事件溯源状态机。

所有事实只追加进 :class:`~src.journal.EventJournal`，状态在启动与故障恢复时通过
重放还原。关键约束在此落实：

- 广告素材/话术/合同/付款计划/测量/请假退营/证据/退款协议全部带来源、指纹与有效期；
  合同签署时快照当时生效的承诺，承诺之后改版不影响已签合同。
- 业绩人员只能追加补充说明，原始测量与安全事件不可改写、不可删除。
- 争议专员只调取双方可见的材料；调解、退款批准、监管答复分属三个角色。
- 退款候选由签约时承诺按规则计算（见 :mod:`src.domain`）；已付款和解只能冲正/补付。
- 监管调查未结不得结案；同一支付/证据回执完整重传只记一次，标识相同而金额、文件
  指纹或提供方变化立即锁定案件。
- 跨门店续营继承未结争议；案件命令带期望版本号，并发和解只有一个能提交。
"""

from __future__ import annotations

import functools
import threading
from dataclasses import dataclass
from typing import Any

from . import domain
from .freeze import PERFORMANCE_ROLES, RoleError, require_role
from .journal import EventJournal, utc_now_iso

# ---- 事件类型 -------------------------------------------------------------

EV_PROMISE_PUBLISHED = "PROMISE_PUBLISHED"
EV_CONTRACT_SIGNED = "CONTRACT_SIGNED"
EV_PAYMENT_PLAN_FROZEN = "PAYMENT_PLAN_FROZEN"
EV_SESSION_DELIVERED = "SESSION_DELIVERED"
EV_PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
EV_MEASUREMENT_RECORDED = "MEASUREMENT_RECORDED"
EV_SAFETY_INCIDENT_RECORDED = "SAFETY_INCIDENT_RECORDED"
EV_STAFF_NOTE_ADDED = "STAFF_NOTE_ADDED"
EV_STAFF_REGISTERED = "STAFF_REGISTERED"
EV_STAFF_DEPARTED = "STAFF_DEPARTED"
EV_WITHDRAWAL_RECORDED = "WITHDRAWAL_RECORDED"
EV_EVIDENCE_HELD = "EVIDENCE_HELD"
EV_DISPUTE_OPENED = "DISPUTE_OPENED"
EV_DISPUTE_TRANSFERRED = "DISPUTE_TRANSFERRED"
EV_CASE_LOCKED = "CASE_LOCKED"
EV_MEDIATION_NOTE_ADDED = "MEDIATION_NOTE_ADDED"
EV_REFUND_CANDIDATE_CALCULATED = "REFUND_CANDIDATE_CALCULATED"
EV_REFUND_APPROVED = "REFUND_APPROVED"
EV_REFUND_SETTLED = "REFUND_SETTLED"
EV_SETTLEMENT_REVERSAL = "SETTLEMENT_REVERSAL"
EV_SETTLEMENT_TOPUP = "SETTLEMENT_TOPUP"
EV_INVESTIGATION_OPENED = "REGULATORY_INVESTIGATION_OPENED"
EV_INVESTIGATION_CLOSED = "REGULATORY_INVESTIGATION_CLOSED"
EV_REGULATORY_RESPONSE_FILED = "REGULATORY_RESPONSE_FILED"
EV_CASE_CLOSED = "CASE_CLOSED"

CASE_OPEN = "open"
CASE_CLOSED_STATUS = "closed"
INV_OPEN = "open"
INV_CLOSED = "closed"

BOTH_PARTIES = frozenset({"consumer", "store"})

# 本服务内的角色越权（离职、非本人角色、不相容职务同人）统一用 freeze.RoleError
RoleErrorLocal = RoleError


# ---- 错误 -----------------------------------------------------------------


class BackendError(RuntimeError):
    """后端业务规则冲突的基类。"""


class NotFoundError(BackendError):
    pass


class ImmutableRecordError(BackendError):
    """试图改写已冻结的原始测量或安全事件。"""


class CaseLockedError(BackendError):
    """案件因标识冲突被锁定，处置命令一律拒绝（证据保全除外）。"""


class CaseStateError(BackendError):
    """案件/退款单状态不允许该动作（含重复批准、重复付款、调查未结）。"""


class ConcurrentModificationError(BackendError):
    """命令携带的案件版本已过期（并发和解冲突）。"""


class SettlementConditionError(BackendError):
    """和解以删除不良记录为条件——明确违法，拒绝落账。"""


def _command(fn):
    """把整个“校验—追加”命令串行化，使期望版本号检查对并发工作线程有效。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._cmd:
            return fn(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class Actor:
    person_id: str
    role: str
    store_id: str | None = None


class Backend:
    def __init__(self, journal: EventJournal):
        self.journal = journal
        self._reset()
        for env in journal.events():
            self._index_envelope(env.body)
            self._apply(env.body, replay=True)

    # 各命令族在重放时用于重建幂等索引
    _DEDUPE_FIELDS = {
        EV_SESSION_DELIVERED: ("session", None),
        EV_PAYMENT_RECEIVED: ("payment", "payment_id"),
        EV_MEASUREMENT_RECORDED: ("measurement", "measurement_id"),
        EV_EVIDENCE_HELD: ("evidence", "evidence_id"),
        EV_REFUND_SETTLED: ("refund_pay", None),
        EV_SETTLEMENT_REVERSAL: ("reversal", "reversal_id"),
        EV_SETTLEMENT_TOPUP: ("topup", None),
        EV_STAFF_REGISTERED: ("staff", "person_id"),
    }

    def _index_envelope(self, e: dict[str, Any]) -> None:
        key = f"{e['aggregate_type']}:{e['aggregate_id']}"
        self.agg_versions[key] = self.agg_versions.get(key, 0) + 1
        spec = self._DEDUPE_FIELDS.get(e["event_type"])
        if spec is None:
            return
        family, field = spec
        p = e.get("payload", {})
        if field is None:
            if e["event_type"] == EV_SETTLEMENT_TOPUP:
                biz = f"{p['refund_id']}:{p['installment_id']}"
            elif e["event_type"] == EV_SESSION_DELIVERED:
                biz = f"{p['contract_id']}:{p['session_id']}"
            elif e["event_type"] == EV_REFUND_SETTLED:
                biz = f"{p['refund_id']}:{p['installment_id']}"
            else:
                biz = p["session_id"]
        else:
            biz = p[field]
        self.dedupe[(family, biz)] = e["event_id"]

    def _reset(self) -> None:
        self.promises: dict[str, dict[str, Any]] = {}
        self.contracts: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, list[dict[str, Any]]] = {}
        self.payment_index: dict[str, dict[str, Any]] = {}
        self.measurements: dict[str, list[dict[str, Any]]] = {}
        self.incidents: dict[str, list[dict[str, Any]]] = {}
        self.notes: dict[str, list[dict[str, Any]]] = {}
        self.cases: dict[str, dict[str, Any]] = {}
        self.case_events: dict[str, list[dict[str, Any]]] = {}
        self.evidence: dict[str, list[dict[str, Any]]] = {}
        self.evidence_index: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.staff: dict[str, dict[str, Any]] = {}
        self.anomalies: dict[str, list[dict[str, Any]]] = {}
        self.pending_lock: set[str] = set()
        self.dedupe: dict[tuple[str, str], str] = {}  # (命令族, 业务标识) -> event_id
        self.agg_versions: dict[str, int] = {}  # "聚合类型:标识" -> 已冻结版本数
        self._cmd = threading.RLock()

    # ---- 事件落盘与重放 ---------------------------------------------------

    def _next_version(self, agg_type: str, agg_id: str) -> int:
        return self.agg_versions.get(f"{agg_type}:{agg_id}", 0) + 1

    def _record(self, body: dict[str, Any], dedupe_key: tuple[str, str] | None = None) -> dict[str, Any]:
        if dedupe_key is not None:
            existing = self.dedupe.get(dedupe_key)
            if existing is not None:
                env = self.journal.get(existing)
                return env.body if env else body
        env = self.journal.append(body, event_id=body["event_id"])
        applied = env.body
        self._index_envelope(applied)
        self._apply(applied, replay=False)
        if dedupe_key is not None:
            self.dedupe[dedupe_key] = env.event_id
        return applied

    @staticmethod
    def _event(
        etype: str,
        agg_type: str,
        agg_id: str,
        version: int,
        summary: str,
        payload: dict[str, Any],
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event_id": f"{etype}:{agg_id}:v{version}",
            "event_type": etype,
            "aggregate_type": agg_type,
            "aggregate_id": agg_id,
            "occurred_at": occurred_at or utc_now_iso(),
            "version": version,
            "summary": summary,
            "payload": payload,
        }

    def _apply(self, e: dict[str, Any], replay: bool) -> None:
        p = e.get("payload", {})
        t = e["event_type"]
        if t == EV_PROMISE_PUBLISHED:
            self.promises[p["promise_id"]] = p
        elif t == EV_CONTRACT_SIGNED:
            self.contracts[p["contract_id"]] = p
        elif t == EV_PAYMENT_PLAN_FROZEN:
            self.contracts[p["contract_id"]]["payment_plan"] = p["plan"]
        elif t == EV_SESSION_DELIVERED:
            contract = self.contracts[p["contract_id"]]
            for s in contract["terms"].get("sessions", []):
                if s["session_id"] == p["session_id"]:
                    s["delivered"] = True
                    s["delivered_at"] = p["delivered_at"]
                    break
        elif t == EV_PAYMENT_RECEIVED:
            self.payments.setdefault(p["contract_id"], []).append(p)
            self.payment_index[p["payment_id"]] = p
        elif t == EV_MEASUREMENT_RECORDED:
            self.measurements.setdefault(p["contract_id"], []).append(p)
        elif t == EV_SAFETY_INCIDENT_RECORDED:
            self.incidents.setdefault(p["contract_id"], []).append(p)
        elif t == EV_STAFF_NOTE_ADDED:
            self.notes.setdefault(p["subject_type"] + ":" + p["subject_id"], []).append(p)
        elif t == EV_STAFF_REGISTERED:
            self.staff[p["person_id"]] = {"role": p["role"], "active": True, "store_id": p.get("store_id")}
        elif t == EV_STAFF_DEPARTED:
            if p["person_id"] in self.staff:
                self.staff[p["person_id"]]["active"] = False
        elif t == EV_WITHDRAWAL_RECORDED:
            self.notes.setdefault("contract:" + p["contract_id"], []).append(p)
        elif t == EV_EVIDENCE_HELD:
            self.evidence.setdefault(p["case_id"], []).append(p)
            self.evidence_index[p["evidence_id"]] = p
        elif t == EV_DISPUTE_OPENED:
            self.cases[p["case_id"]] = {
                "case_id": p["case_id"],
                "consumer_id": p["consumer_id"],
                "home_store_id": p["store_id"],
                "involved_stores": set(p["involved_stores"]),
                "contract_ids": list(p["contract_ids"]),
                # 主合同即争议指向的合同；跨店继承并入的新合同只影响处理范围与待办，
                # 不自动并入退款金额
                "primary_contract_id": p["contract_ids"][0],
                "reason": p["reason"],
                "claimed_at": p["claimed_at"],
                "status": CASE_OPEN,
                "locked": False,
                "lock_reasons": [],
                "version": 1,
                "refund_ids": [],
                "investigation": None,
                "opened_at": p["claimed_at"],
            }
            self.case_events.setdefault(p["case_id"], []).append(e)
        elif t == EV_DISPUTE_TRANSFERRED:
            case = self.cases[p["case_id"]]
            case["involved_stores"].add(p["new_store_id"])
            if p.get("new_contract_id") and p["new_contract_id"] not in case["contract_ids"]:
                case["contract_ids"].append(p["new_contract_id"])
            case["version"] += 1
            self.case_events.setdefault(p["case_id"], []).append(e)
        elif t == EV_CASE_LOCKED:
            case = self.cases[p["case_id"]]
            case["locked"] = True
            case["lock_reasons"].append(p["reason"])
            case["version"] += 1
            self.case_events.setdefault(p["case_id"], []).append(e)
            detail = p.get("detail")
            if replay and detail and detail.get("kind") in ("payment", "evidence"):
                # 重放时恢复标识冲突台账（同一异常可能随多案继承出现，按标识去重）
                ledger = self.anomalies.setdefault(case["consumer_id"], [])
                if not any(a["kind"] == detail["kind"] and a.get("identifier") == detail["identifier"]
                           for a in ledger):
                    ledger.append(
                        {"kind": detail["kind"], "identifier": detail["identifier"],
                         "reason": p["reason"], "at": p["locked_at"]}
                    )
                self.pending_lock.add(case["consumer_id"])
        elif t in (EV_MEDIATION_NOTE_ADDED, EV_REGULATORY_RESPONSE_FILED):
            case = self.cases[p["case_id"]]
            case["version"] += 1
            self.case_events.setdefault(p["case_id"], []).append(e)
        elif t == EV_REFUND_CANDIDATE_CALCULATED:
            case = self.cases[p["case_id"]]
            case["version"] += 1
            case["last_candidate_id"] = p["candidate_id"]
            self.case_events.setdefault(p["case_id"], []).append(e)
        elif t == EV_REFUND_APPROVED:
            case = self.cases[p["case_id"]]
            case["version"] += 1
            case["refund_ids"].append(p["refund_id"])
            self.refunds[p["refund_id"]] = {
                "refund_id": p["refund_id"],
                "case_id": p["case_id"],
                "candidate_id": p["candidate_id"],
                "approved_cents": p["amount_cents"],
                "approver_id": p["approver_id"],
                "specialist_id": p["specialist_id"],
                "installments": {},
                "settled_cents": 0,
                "reversed_cents": 0,
                "status": "approved",
                "agreement": p.get("agreement"),
            }
            self.case_events.setdefault(p["case_id"], []).append(e)
        elif t == EV_REFUND_SETTLED:
            refund = self.refunds[p["refund_id"]]
            refund["installments"][p["installment_id"]] = {
                "amount_cents": p["amount_cents"],
                "paid_at": p["paid_at"],
                "receipt_fingerprint": p["receipt_fingerprint"],
                "provider": p["provider"],
            }
            refund["settled_cents"] += p["amount_cents"]
            if refund["settled_cents"] >= refund["approved_cents"]:
                refund["status"] = "settled"
        elif t == EV_SETTLEMENT_REVERSAL:
            refund = self.refunds[p["refund_id"]]
            inst = refund["installments"][p["installment_id"]]
            inst["reversed"] = int(inst.get("reversed", 0)) + p["amount_cents"]
            refund["reversed_cents"] += p["amount_cents"]
            refund["settled_cents"] -= p["amount_cents"]
            refund["status"] = "settled" if refund["settled_cents"] == refund["approved_cents"] else "adjusting"
        elif t == EV_SETTLEMENT_TOPUP:
            refund = self.refunds[p["refund_id"]]
            refund["installments"][p["installment_id"]] = {
                "amount_cents": p["amount_cents"],
                "paid_at": p["paid_at"],
                "receipt_fingerprint": p["receipt_fingerprint"],
                "provider": p["provider"],
                "topup_for": p.get("reversal_id"),
            }
            refund["settled_cents"] += p["amount_cents"]
            if refund["settled_cents"] >= refund["approved_cents"]:
                refund["status"] = "settled"
        elif t == EV_INVESTIGATION_OPENED:
            case = self.cases[p["case_id"]]
            case["investigation"] = {
                "status": INV_OPEN,
                "authority": p["authority"],
                "reference": p["reference"],
                "opened_at": p["opened_at"],
            }
            case["version"] += 1
            self.case_events.setdefault(p["case_id"], []).append(e)
        elif t == EV_INVESTIGATION_CLOSED:
            case = self.cases[p["case_id"]]
            if case["investigation"]:
                case["investigation"]["status"] = INV_CLOSED
            case["version"] += 1
            self.case_events.setdefault(p["case_id"], []).append(e)
        elif t == EV_CASE_CLOSED:
            case = self.cases[p["case_id"]]
            case["status"] = CASE_CLOSED_STATUS
            case["closed_at"] = p["closed_at"]
            case["version"] += 1
            self.case_events.setdefault(p["case_id"], []).append(e)

    # ---- 内部校验 ---------------------------------------------------------

    def _require_active_performer(self, actor: Actor) -> None:
        staff = self.staff.get(actor.person_id)
        if staff is not None and not staff["active"]:
            raise RoleErrorLocal(f"{actor.person_id} 已离职，不能再执行业绩操作（其历史记录仍有效）")

    def _get_contract(self, contract_id: str) -> dict[str, Any]:
        try:
            return self.contracts[contract_id]
        except KeyError:
            raise NotFoundError(f"合同不存在：{contract_id}") from None

    def _get_case(self, case_id: str, *, writable: bool = True) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        if writable:
            if case["status"] == CASE_CLOSED_STATUS:
                raise CaseStateError(f"案件 {case_id} 已关闭")
            if case["locked"]:
                raise CaseLockedError(f"案件 {case_id} 已锁定：{case['lock_reasons']}")
        return case

    def _check_version(self, case: dict[str, Any], expected_version: int | None) -> None:
        if expected_version is not None and case["version"] != expected_version:
            raise ConcurrentModificationError(
                f"案件版本已变化：期望 {expected_version}，当前 {case['version']}"
            )

    def _lock_case(self, case_id: str, reason: str, occurred_at: str) -> None:
        case = self.cases[case_id]
        if case["locked"] and reason in case["lock_reasons"]:
            return
        body = self._event(
            EV_CASE_LOCKED,
            "dispute_case",
            case_id,
            self._next_version("dispute_case", case_id),
            f"案件锁定：{reason}",
            {
                "case_id": case_id,
                "reason": reason,
                "locked_at": occurred_at,
                "detail": self._last_anomaly(case_id, reason),
            },
            occurred_at=occurred_at,
        )
        self._record(body)

    def _last_anomaly(self, case_id: str, reason: str) -> dict[str, Any] | None:
        case = self.cases[case_id]
        for anomaly in reversed(self.anomalies.get(case["consumer_id"], [])):
            if anomaly["reason"] == reason:
                return {k: v for k, v in anomaly.items() if k != "reason"}
        return None

    def _flag_anomaly(self, consumer_id: str, anomaly: dict[str, Any]) -> None:
        ledger = self.anomalies.setdefault(consumer_id, [])
        if not any(a["kind"] == anomaly["kind"] and a.get("identifier") == anomaly["identifier"]
                   for a in ledger):
            ledger.append(anomaly)
        self.pending_lock.add(consumer_id)
        for case in self.cases.values():
            if case["consumer_id"] == consumer_id and case["status"] == CASE_OPEN and not case["locked"]:
                self._lock_case(case["case_id"], anomaly["reason"], anomaly["at"])

    def _consumer_of_contract(self, contract_id: str) -> str:
        return self._get_contract(contract_id)["consumer_id"]

    @_command
    def mark_session_delivered(
        self, *, actor: Actor, contract_id: str, session_id: str, delivered_at: str
    ) -> dict[str, Any]:
        """课时实际交付（只追加；用于退款计算扣除已消费价值）。"""
        self._require_active_performer(actor)
        contract = self._get_contract(contract_id)
        known = {s["session_id"] for s in contract["terms"].get("sessions", [])}
        if session_id not in known:
            raise NotFoundError(f"合同 {contract_id} 无此课时：{session_id}")
        if any(s["session_id"] == session_id and s.get("delivered")
               for s in contract["terms"]["sessions"]):
            # 完整重放/重试保持一次
            return self.journal.get(
                self.dedupe[("session", f"{contract_id}:{session_id}")]).body
        payload = {"contract_id": contract_id, "session_id": session_id,
                   "delivered_at": delivered_at, "delivered_by": actor.person_id}
        return self._record(
            self._event(EV_SESSION_DELIVERED, "service_contract", contract_id,
                        self._next_version("service_contract", contract_id),
                        f"登记课时交付 {session_id}", payload, occurred_at=delivered_at),
            dedupe_key=("session", f"{contract_id}:{session_id}"),
        )

    # ---- 1. 承诺与合同 ----------------------------------------------------

    @_command
    def publish_promise(
        self,
        *,
        actor: Actor,
        promise_id: str,
        store_id: str,
        campaign: str,
        claims: list[str],
        guarantee: dict[str, Any] | None,
        materials: list[dict[str, Any]],
        valid_from: str,
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        """冻结广告素材与销售话术版本及其有效期（“急速暴瘦/不瘦退款”即在此留痕）。"""
        self._require_active_performer(actor)
        if promise_id in self.promises:
            raise ImmutableRecordError("承诺标识已存在；改版请使用新的 promise_id，旧版保留")
        for m in materials:
            for key in ("material_id", "kind", "source", "fingerprint"):
                if not m.get(key):
                    raise ValueError(f"素材缺少 {key}")
        payload = {
            "promise_id": promise_id,
            "store_id": store_id,
            "campaign": campaign,
            "claims": list(claims),
            "guarantee": guarantee,
            "materials": [dict(m) for m in materials],
            "valid_from": valid_from,
            "valid_until": valid_until,
            "published_by": actor.person_id,
        }
        return self._record(
            self._event(EV_PROMISE_PUBLISHED, "marketing_promise", promise_id,
                        self._next_version("marketing_promise", promise_id),
                        f"冻结营销承诺 {campaign}", payload, occurred_at=valid_from)
        )

    @_command
    def sign_contract(
        self,
        *,
        actor: Actor,
        contract_id: str,
        consumer_id: str,
        store_id: str,
        promise_id: str,
        signed_at: str,
        terms: dict[str, Any],
        document: dict[str, Any],
        sales_person_id: str,
    ) -> dict[str, Any]:
        """签约：冻结合同版本，并快照签署时刻处于有效期的承诺。"""
        self._require_active_performer(actor)
        if contract_id in self.contracts:
            raise ImmutableRecordError("合同标识已存在；修订需新版本，不得改写已签合同")
        promise = self.promises.get(promise_id)
        if promise is None:
            raise NotFoundError(f"承诺不存在：{promise_id}")
        adopted = self._adopted_promise(promise, signed_at)
        if adopted is None:
            raise BackendError(f"承诺 {promise_id} 在签约时刻 {signed_at} 不在有效期内")
        payload = {
            "contract_id": contract_id,
            "consumer_id": consumer_id,
            "store_id": store_id,
            "promise_id": promise_id,
            "adopted_promise": {
                "promise_id": promise["promise_id"],
                "campaign": promise["campaign"],
                "claims": list(promise["claims"]),
                "guarantee": promise.get("guarantee"),
                "materials": [dict(m) for m in promise["materials"]],
                "valid_from": promise["valid_from"],
            },
            "terms": dict(terms),
            "document": dict(document),  # source/version/fingerprint
            "signed_at": signed_at,
            "sales_person_id": sales_person_id,  # 销售离职也不影响已冻结身份
            "signed_by": actor.person_id,
        }
        body = self._record(
            self._event(EV_CONTRACT_SIGNED, "service_contract", contract_id,
                        self._next_version("service_contract", contract_id),
                        f"签署合同 {contract_id}", payload, occurred_at=signed_at)
        )
        # 跨门店续营：消费者名下未结争议随新合同继承到新门店
        for case in self.cases.values():
            if case["consumer_id"] == consumer_id and case["status"] == CASE_OPEN:
                tr = self._event(
                    EV_DISPUTE_TRANSFERRED,
                    "dispute_case",
                    case["case_id"],
                    self._next_version("dispute_case", case["case_id"]),
                    "跨门店续营，未结争议继承至新门店",
                    {
                        "case_id": case["case_id"],
                        "consumer_id": consumer_id,
                        "new_store_id": store_id,
                        "new_contract_id": contract_id,
                        "reason": "reenrollment_inheritance",
                    },
                    occurred_at=signed_at,
                )
                self._record(tr)
        return body

    @staticmethod
    def _adopted_promise(promise: dict[str, Any], at: str) -> dict[str, Any] | None:
        from datetime import datetime

        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        start = datetime.fromisoformat(promise["valid_from"].replace("Z", "+00:00"))
        if dt < start:
            return None
        if promise.get("valid_until"):
            end = datetime.fromisoformat(promise["valid_until"].replace("Z", "+00:00"))
            if dt >= end:
                return None
        return promise

    @_command
    def freeze_payment_plan(self, *, contract_id: str, plan: list[dict[str, Any]]) -> dict[str, Any]:
        contract = self._get_contract(contract_id)
        if "payment_plan" in contract:
            raise ImmutableRecordError("付款计划已冻结；变更只能通过后续协议版本")
        for item in plan:
            for key in ("installment_id", "due_at", "amount_cents"):
                if key not in item:
                    raise ValueError(f"付款计划条目缺少 {key}")
        payload = {"contract_id": contract_id, "plan": [dict(x) for x in plan]}
        return self._record(
            self._event(EV_PAYMENT_PLAN_FROZEN, "service_contract", contract_id,
                        self._next_version("service_contract", contract_id), "冻结分期付款计划", payload)
        )

    # ---- 2. 付款（回执去重 + 标识冲突锁案） --------------------------------

    @_command
    def record_payment(
        self,
        *,
        actor: Actor,
        contract_id: str,
        payment_id: str,
        amount_cents: int,
        paid_at: str,
        provider: str,
        receipt_fingerprint: str,
    ) -> dict[str, Any]:
        self._require_active_performer(actor)
        if amount_cents <= 0:
            raise ValueError("付款金额必须为正整数（分）")
        consumer_id = self._consumer_of_contract(contract_id)
        existing = self.payment_index.get(payment_id)
        if existing is not None:
            if (
                existing["amount_cents"] == amount_cents
                and existing["receipt_fingerprint"] == receipt_fingerprint
                and existing["provider"] == provider
                and existing["contract_id"] == contract_id
            ):
                return self.journal.get(self.dedupe[("payment", payment_id)]).body  # 完整重传：只记一次
            anomaly = {
                "kind": "payment",
                "identifier": payment_id,
                "reason": f"支付回执 {payment_id} 的金额/指纹/提供方与首次冻结不一致",
                "at": paid_at,
                "existing": {
                    "amount_cents": existing["amount_cents"],
                    "fingerprint": existing["receipt_fingerprint"],
                    "provider": existing["provider"],
                },
                "received": {
                    "amount_cents": amount_cents,
                    "fingerprint": receipt_fingerprint,
                    "provider": provider,
                },
            }
            self._flag_anomaly(consumer_id, anomaly)
            raise CaseLockedError(anomaly["reason"])
        payload = {
            "payment_id": payment_id,
            "contract_id": contract_id,
            "amount_cents": int(amount_cents),
            "paid_at": paid_at,
            "provider": provider,
            "receipt_fingerprint": receipt_fingerprint,
            "recorded_by": actor.person_id,
        }
        body = self._event(
            EV_PAYMENT_RECEIVED, "service_contract", contract_id,
            self._next_version("service_contract", contract_id), f"登记付款 {payment_id}", payload,
            occurred_at=paid_at,
        )
        return self._record(body, dedupe_key=("payment", payment_id))

    # ---- 3. 测量与安全事件（原始记录不可改写） -----------------------------

    @_command
    def record_measurement(
        self,
        *,
        actor: Actor,
        contract_id: str,
        measurement_id: str,
        taken_at: str,
        weight_kg: float,
    ) -> dict[str, Any]:
        from .freeze import ROLE_COACH

        self._require_active_performer(actor)
        if actor.role != ROLE_COACH:
            raise RoleErrorLocal("只有教练角色可以登记阶段测量")
        self._get_contract(contract_id)
        for m in self.measurements.get(contract_id, []):
            if m["measurement_id"] == measurement_id:
                if m["weight_kg"] != weight_kg or m["taken_at"] != taken_at:
                    raise ImmutableRecordError(
                        f"原始测量 {measurement_id} 已冻结，不得改写；可追加补充说明"
                    )
                return self.journal.get(self.dedupe[("measurement", measurement_id)]).body
        payload = {
            "measurement_id": measurement_id,
            "contract_id": contract_id,
            "taken_at": taken_at,
            "weight_kg": float(weight_kg),
            "taken_by": actor.person_id,
        }
        return self._record(
            self._event(EV_MEASUREMENT_RECORDED, "service_contract", contract_id,
                        self._next_version("service_contract", contract_id),
                        f"冻结阶段测量 {measurement_id}", payload, occurred_at=taken_at),
            dedupe_key=("measurement", measurement_id),
        )

    # 补充说明挂回其原属聚合，信封 aggregate_type 仍取契约内的聚合类型
    _NOTE_AGG = {
        "contract": "service_contract",
        "measurement": "service_contract",
        "payment": "service_contract",
        "incident": "safety_incident",
        "case": "dispute_case",
    }

    @_command
    def add_staff_note(
        self,
        *,
        actor: Actor,
        subject_type: str,
        subject_id: str,
        note: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """业绩人员补充说明：只能追加，永不改动原记录。"""
        self._require_active_performer(actor)
        if actor.role not in PERFORMANCE_ROLES:
            raise RoleErrorLocal("只有业绩人员可以追加业务补充说明")
        agg_type = self._NOTE_AGG.get(subject_type)
        if agg_type is None:
            raise ValueError(f"补充说明对象类型非法：{subject_type}")
        key = f"note:{subject_type}:{subject_id}:{len(self.notes) + 1}"
        payload = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "note": note,
            "note_id": key,
            "added_by": actor.person_id,
            "actor_role": actor.role,
        }
        return self._record(
            self._event(EV_STAFF_NOTE_ADDED, agg_type, subject_id,
                        self._next_version(agg_type, subject_id),
                        "业绩人员追加补充说明（不改写原记录）", payload,
                        occurred_at=at)
        )

    @_command
    def record_safety_incident(
        self,
        *,
        actor: Actor,
        contract_id: str,
        incident_id: str,
        occurred_at: str,
        severity: str,
        description: str,
    ) -> dict[str, Any]:
        self._require_active_performer(actor)
        self._get_contract(contract_id)
        for i in self.incidents.get(contract_id, []):
            if i["incident_id"] == incident_id:
                raise ImmutableRecordError("安全事件已冻结，不得改写；可追加说明")
        payload = {
            "incident_id": incident_id,
            "contract_id": contract_id,
            "occurred_at": occurred_at,
            "severity": severity,
            "description": description,
            "reported_by": actor.person_id,
            "reporter_role": actor.role,
        }
        return self._record(
            self._event(EV_SAFETY_INCIDENT_RECORDED, "safety_incident", incident_id,
                        self._next_version("safety_incident", incident_id),
                        f"冻结安全事件 {incident_id}", payload, occurred_at=occurred_at)
        )

    @_command
    def record_withdrawal(
        self, *, actor: Actor, contract_id: str, at: str, reason: str, kind: str = "withdrawal"
    ) -> dict[str, Any]:
        """请假/退营事实冻结（kind=leave|withdrawal）。"""
        self._require_active_performer(actor)
        payload = {"contract_id": contract_id, "at": at, "reason": reason, "kind": kind,
                   "recorded_by": actor.person_id}
        return self._record(
            self._event(EV_WITHDRAWAL_RECORDED, "service_contract", contract_id,
                        self._next_version("service_contract", contract_id),
                        f"冻结{'退营' if kind == 'withdrawal' else '请假'}事实",
                        payload, occurred_at=at)
        )

    @_command
    def register_staff(self, *, person_id: str, role: str, store_id: str | None = None) -> dict[str, Any]:
        payload = {"person_id": person_id, "role": role, "store_id": store_id}
        body = self._event(EV_STAFF_REGISTERED, "staff", person_id,
                           self._next_version("staff", person_id), f"登记员工 {person_id}", payload)
        return self._record(body, dedupe_key=("staff", person_id))

    @_command
    def mark_staff_departed(self, *, person_id: str, at: str) -> dict[str, Any]:
        """销售离职：其冻结的合同与证言继续有效，但本人不能再操作。"""
        body = self._event(EV_STAFF_DEPARTED, "staff", person_id,
                           self._next_version("staff", person_id),
                           f"员工离职 {person_id}", {"person_id": person_id, "at": at}, occurred_at=at)
        return self._record(body)

    # ---- 4. 争议、证据与调解 ----------------------------------------------

    @_command
    def open_dispute(
        self,
        *,
        case_id: str,
        consumer_id: str,
        store_id: str,
        contract_ids: list[str],
        reason: str,
        claimed_at: str,
        summary: str,
    ) -> dict[str, Any]:
        """开案。迟到投诉同样受理——退款候选按 claimed_at 适用规则计算，不因时间驳回。"""
        if case_id in self.cases:
            raise ImmutableRecordError("案件标识已存在")
        if reason not in domain.REASONS:
            raise ValueError(f"退款原因必须是 {domain.REASONS}")
        for cid in contract_ids:
            self._get_contract(cid)
        stores = {self._get_contract(cid)["store_id"] for cid in contract_ids} | {store_id}
        payload = {
            "case_id": case_id,
            "consumer_id": consumer_id,
            "store_id": store_id,
            "involved_stores": sorted(stores),
            "contract_ids": list(contract_ids),
            "reason": reason,
            "claimed_at": claimed_at,
            "summary": summary,
        }
        body = self._record(
            self._event(EV_DISPUTE_OPENED, "dispute_case", case_id,
                        self._next_version("dispute_case", case_id),
                        f"受理争议 {case_id}（{reason}）", payload, occurred_at=claimed_at)
        )
        # 开案前若已存在该消费者的回执标识异常，立即锁定
        if consumer_id in self.pending_lock:
            for anomaly in list(self.anomalies.get(consumer_id, [])):
                self._lock_case(case_id, anomaly["reason"], claimed_at)
        return body

    @_command
    def hold_evidence(
        self,
        *,
        case_id: str,
        evidence_id: str,
        kind: str,
        source: str,
        provider: str,
        filename: str,
        fingerprint: str,
        held_at: str,
        submitted_by: str,
        retention_until: str,
        visible_to: set[str] | None = None,
    ) -> dict[str, Any]:
        """冻结投诉证据并设定保全期限；案件锁定也允许保全（不得丢失）。"""
        case = self._get_case(case_id, writable=False)
        existing = self.evidence_index.get(evidence_id)
        vis = set(visible_to or BOTH_PARTIES)
        if existing is not None:
            if (
                existing["fingerprint"] == fingerprint
                and existing["provider"] == provider
                and existing["filename"] == filename
                and existing["case_id"] == case_id
            ):
                return self.journal.get(self.dedupe[("evidence", evidence_id)]).body  # 完整重传一次
            anomaly = {
                "kind": "evidence",
                "identifier": evidence_id,
                "reason": f"证据回执 {evidence_id} 的文件指纹或提供方与首次保全不一致",
                "at": held_at,
                "existing": {"fingerprint": existing["fingerprint"], "provider": existing["provider"]},
                "received": {"fingerprint": fingerprint, "provider": provider},
            }
            self._flag_anomaly(case["consumer_id"], anomaly)
            raise CaseLockedError(anomaly["reason"])
        payload = {
            "evidence_id": evidence_id,
            "case_id": case_id,
            "kind": kind,
            "source": source,
            "provider": provider,
            "filename": filename,
            "fingerprint": fingerprint,
            "held_at": held_at,
            "submitted_by": submitted_by,
            "retention_until": retention_until,
            "visible_to": sorted(vis),
        }
        return self._record(
            self._event(EV_EVIDENCE_HELD, "evidence", evidence_id,
                        self._next_version("evidence", evidence_id),
                        f"冻结证据 {filename}（保全至 {retention_until}）", payload, occurred_at=held_at),
            dedupe_key=("evidence", evidence_id),
        )

    @_command
    def add_mediation_note(
        self,
        *,
        actor: Actor,
        case_id: str,
        note: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        require_role("mediate", actor.role)
        case = self._get_case(case_id)
        self._check_version(case, expected_version)
        payload = {"case_id": case_id, "note": note, "specialist_id": actor.person_id}
        return self._record(
            self._event(EV_MEDIATION_NOTE_ADDED, "dispute_case", case_id,
                        self._next_version("dispute_case", case_id),
                        "争议专员调解记录（仅基于双方可见材料）", payload)
        )

    @_command
    def visible_materials(self, case_id: str) -> dict[str, list[dict[str, Any]]]:
        """争议专员可调取的材料：仅双方可见范围；监管内部材料不在其列。"""
        self._get_case(case_id, writable=False)
        ev = [e for e in self.evidence.get(case_id, []) if set(BOTH_PARTIES) <= set(e["visible_to"])]
        return {"evidence": ev}

    # ---- 5. 退款候选、批准、结算 ------------------------------------------

    @_command
    def calculate_candidate(
        self, *, case_id: str, reason: str | None = None, claimed_at: str | None = None
    ) -> dict[str, Any]:
        """按签约时承诺计算可退款候选；每个候选连同明细行冻结，供消费者 API 展示。"""
        case = self._get_case(case_id)
        reason = reason or case["reason"]
        claimed_at = claimed_at or case["claimed_at"]
        contract_id = case["primary_contract_id"]
        contract0 = self._get_contract(contract_id)
        calc_contract = {
            "signed_at": contract0["signed_at"],
            "promise_id": contract0["promise_id"],
            **contract0["terms"],
        }
        # “不瘦退款”承诺以签约时快照的营销承诺为准；合同条款可覆盖
        calc_contract.setdefault("guarantee", contract0["adopted_promise"].get("guarantee"))
        payments = [
            p for p in self.payments.get(contract_id, [])
            if domain.parse_at(p["paid_at"]) <= domain.parse_at(claimed_at)
        ]
        measurements = list(self.measurements.get(contract_id, []))
        incidents = [
            i for i in self.incidents.get(contract_id, [])
            if domain.parse_at(i["occurred_at"]) <= domain.parse_at(claimed_at)
        ]
        delivered = [
            {"session_id": s["session_id"]}
            for s in contract0["terms"].get("sessions", [])
            if s.get("delivered") and domain.parse_at(s.get("delivered_at", s["scheduled_for"]))
            <= domain.parse_at(claimed_at)
        ]
        calc = domain.calculate_refund(
            reason=reason,
            contract=calc_contract,
            payments=payments,
            delivered_sessions=delivered,
            measurements=measurements,
            incidents=incidents,
            claimed_at=claimed_at,
        )
        candidate_id = f"CAND-{case_id}-{case['version'] + 1}"
        payload = {
            "case_id": case_id,
            "candidate_id": candidate_id,
            "contract_id": contract_id,
            "contract_document_fingerprint": contract0["document"]["fingerprint"],
            "adopted_promise_id": contract0["promise_id"],
            "payment_ids": [p["payment_id"] for p in payments],
            "measurement_ids": [m["measurement_id"] for m in measurements],
            "incident_ids": [i["incident_id"] for i in incidents],
            **calc,
        }
        self._record(
            self._event(EV_REFUND_CANDIDATE_CALCULATED, "dispute_case", case_id,
                        self._next_version("dispute_case", case_id),
                        f"计算退款候选 {candidate_id}", payload)
        )
        return payload

    @_command
    def approve_refund(
        self,
        *,
        actor: Actor,
        case_id: str,
        candidate_id: str,
        refund_id: str,
        agreement_fingerprint: str,
        agreement_source: str,
        requires_record_deletion: bool = False,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """批准退款：必须是批准角色，且不得与调解专员、监管答复人为同一人。

        和解金额只能等于候选金额；多付/少付在结算后用冲正或补付调整。
        以删除不良记录为条件的和解直接拒绝。
        """
        require_role("approve_refund", actor.role)
        case = self._get_case(case_id)
        self._check_version(case, expected_version)
        if requires_record_deletion:
            raise SettlementConditionError(
                "和解不得约定删除原始测量、安全事件或投诉证据；该条件违法且不可落账"
            )
        if refund_id in self.refunds:
            raise CaseStateError(f"退款单 {refund_id} 已存在，不得重复批准")
        if case["refund_ids"]:
            raise CaseStateError(
                f"案件 {case_id} 已批准退款 {case['refund_ids']}；调整只能冲正/补付，不能二次和解"
            )
        candidate = self._find_candidate(case_id, candidate_id)
        specialist_id = self._last_specialist(case_id)
        if specialist_id and specialist_id == actor.person_id:
            raise RoleErrorLocal("退款批准人不得与调解专员为同一人")
        payload = {
            "refund_id": refund_id,
            "case_id": case_id,
            "candidate_id": candidate_id,
            "amount_cents": candidate["amount_cents"],
            "approver_id": actor.person_id,
            "specialist_id": specialist_id,
            "agreement": {
                "source": agreement_source,
                "fingerprint": agreement_fingerprint,
                "record_deletion_required": False,
                "does_not_close_regulatory_facts": True,
            },
        }
        return self._record(
            self._event(EV_REFUND_APPROVED, "refund_entry", refund_id,
                        self._next_version("refund_entry", refund_id),
                        f"批准退款 {refund_id}（候选 {candidate_id}）", payload)
        )

    def _find_candidate(self, case_id: str, candidate_id: str) -> dict[str, Any]:
        for e in self.case_events.get(case_id, []):
            if e["event_type"] == EV_REFUND_CANDIDATE_CALCULATED and e["payload"]["candidate_id"] == candidate_id:
                return e["payload"]
        raise NotFoundError(f"候选不存在：{candidate_id}")

    def _last_specialist(self, case_id: str) -> str | None:
        for e in reversed(self.case_events.get(case_id, [])):
            if e["event_type"] == EV_MEDIATION_NOTE_ADDED:
                return e["payload"]["specialist_id"]
        return None

    @_command
    def settle_installment(
        self,
        *,
        actor: Actor,
        refund_id: str,
        installment_id: str,
        amount_cents: int,
        paid_at: str,
        provider: str,
        receipt_fingerprint: str,
    ) -> dict[str, Any]:
        """分期退款出账：installment_id 幂等，恢复/重试不会重复付款。"""
        require_role("approve_refund", actor.role)
        refund = self.refunds.get(refund_id)
        if refund is None:
            raise NotFoundError(f"退款单不存在：{refund_id}")
        if installment_id in refund["installments"]:
            inst = refund["installments"][installment_id]
            if (
                inst["amount_cents"] == amount_cents
                and inst["receipt_fingerprint"] == receipt_fingerprint
                and inst["provider"] == provider
            ):
                return self.journal.get(
                    self.dedupe[("refund_pay", f"{refund_id}:{installment_id}")]).body
            raise CaseStateError(f"分期 {installment_id} 已出账且回执不一致，须走冲正/补付")
        if amount_cents <= 0:
            raise ValueError("出账金额必须为正数")
        # 允许按回执实际金额出账；多付不删除、不改写，事后只能追加冲正调减
        payload = {
            "refund_id": refund_id,
            "installment_id": installment_id,
            "amount_cents": int(amount_cents),
            "paid_at": paid_at,
            "provider": provider,
            "receipt_fingerprint": receipt_fingerprint,
        }
        return self._record(
            self._event(EV_REFUND_SETTLED, "refund_entry", refund_id,
                        self._next_version("refund_entry", refund_id),
                        f"分期退款出账 {installment_id}",
                        payload, occurred_at=paid_at),
            dedupe_key=("refund_pay", f"{refund_id}:{installment_id}"),
        )

    @_command
    def reverse_settlement(
        self, *, actor: Actor, refund_id: str, installment_id: str, amount_cents: int,
        reason: str, at: str,
    ) -> dict[str, Any]:
        """冲正：已付款和解的唯一调减方式（不删除原出账事实，追加红冲）。"""
        require_role("approve_refund", actor.role)
        refund = self.refunds.get(refund_id)
        if refund is None:
            raise NotFoundError(f"退款单不存在：{refund_id}")
        inst = refund["installments"].get(installment_id)
        if inst is None:
            raise NotFoundError(f"分期不存在：{installment_id}")
        already_reversed = int(inst.get("reversed", 0))
        if amount_cents <= 0 or amount_cents > inst["amount_cents"] - already_reversed:
            raise ValueError("冲正金额超出该分期可冲正额度")
        reversal_id = f"REV-{installment_id}-{already_reversed + 1}"
        payload = {
            "refund_id": refund_id,
            "installment_id": installment_id,
            "reversal_id": reversal_id,
            "amount_cents": int(amount_cents),
            "reason": reason,
            "at": at,
            "by": actor.person_id,
        }
        body = self._event(EV_SETTLEMENT_REVERSAL, "refund_entry", refund_id,
                           self._next_version("refund_entry", refund_id),
                           f"冲正 {installment_id}", payload, occurred_at=at)
        result = self._record(body, dedupe_key=("reversal", reversal_id))
        return result

    @_command
    def topup_settlement(
        self, *, actor: Actor, refund_id: str, installment_id: str, amount_cents: int,
        paid_at: str, provider: str, receipt_fingerprint: str, reversal_id: str | None = None,
    ) -> dict[str, Any]:
        """补付：冲正后或少付后的唯一调增方式。"""
        require_role("approve_refund", actor.role)
        refund = self.refunds.get(refund_id)
        if refund is None:
            raise NotFoundError(f"退款单不存在：{refund_id}")
        if amount_cents <= 0:
            raise ValueError("补付金额必须为正数")
        outstanding = refund["approved_cents"] - refund["settled_cents"]
        if amount_cents > outstanding:
            raise CaseStateError(f"补付 {amount_cents} 超过候选未结 {outstanding}")
        payload = {
            "refund_id": refund_id,
            "installment_id": installment_id,
            "amount_cents": int(amount_cents),
            "paid_at": paid_at,
            "provider": provider,
            "receipt_fingerprint": receipt_fingerprint,
            "reversal_id": reversal_id,
        }
        return self._record(
            self._event(EV_SETTLEMENT_TOPUP, "refund_entry", refund_id,
                        self._next_version("refund_entry", refund_id),
                        f"补付 {installment_id}", payload, occurred_at=paid_at),
            dedupe_key=("topup", f"{refund_id}:{installment_id}"),
        )

    # ---- 6. 监管调查与结案 -------------------------------------------------

    @_command
    def open_investigation(self, *, case_id: str, authority: str, reference: str, opened_at: str) -> dict[str, Any]:
        case = self._get_case(case_id, writable=False)
        if case["investigation"] and case["investigation"]["status"] == INV_OPEN:
            raise CaseStateError("监管调查已在进行中")
        payload = {"case_id": case_id, "authority": authority, "reference": reference, "opened_at": opened_at}
        return self._record(
            self._event(EV_INVESTIGATION_OPENED, "dispute_case", case_id,
                        self._next_version("dispute_case", case_id),
                        f"监管调查立案 {authority}/{reference}", payload, occurred_at=opened_at)
        )

    @_command
    def close_investigation(self, *, case_id: str, at: str) -> dict[str, Any]:
        case = self._get_case(case_id, writable=False)
        if not case["investigation"] or case["investigation"]["status"] != INV_OPEN:
            raise CaseStateError("没有进行中的监管调查")
        payload = {"case_id": case_id, "at": at}
        return self._record(
            self._event(EV_INVESTIGATION_CLOSED, "dispute_case", case_id,
                        self._next_version("dispute_case", case_id),
                        "监管调查结案", payload, occurred_at=at)
        )

    @_command
    @_command
    def file_regulatory_response(
        self, *, actor: Actor, case_id: str, response_fingerprint: str, summary: str, filed_at: str
    ) -> dict[str, Any]:
        require_role("file_regulatory_response", actor.role)
        case = self._get_case(case_id, writable=False)
        if actor.person_id == self._last_approver(case_id) or actor.person_id == self._last_specialist(case_id):
            raise RoleErrorLocal("监管答复人不得与退款批准人或调解专员为同一人")
        payload = {
            "case_id": case_id,
            "response_fingerprint": response_fingerprint,
            "summary": summary,
            "regulator_id": actor.person_id,
        }
        return self._record(
            self._event(EV_REGULATORY_RESPONSE_FILED, "dispute_case", case_id,
                        self._next_version("dispute_case", case_id),
                        "提交监管答复", payload, occurred_at=filed_at)
        )

    def _last_approver(self, case_id: str) -> str | None:
        for rid in self.cases[case_id]["refund_ids"]:
            return self.refunds[rid]["approver_id"]
        return None

    @_command
    def close_case(self, *, actor: Actor, case_id: str, closed_at: str,
                   expected_version: int | None = None) -> dict[str, Any]:
        """结案：监管调查未结时拒绝——结算不能关闭仍在调查的事实。"""
        require_role("close_case", actor.role)
        case = self._get_case(case_id)
        self._check_version(case, expected_version)
        inv = case.get("investigation")
        if inv and inv["status"] == INV_OPEN:
            raise CaseStateError(
                f"案件 {case_id} 的监管调查 {inv['reference']} 尚未结案，不能借退款结算关闭案件"
            )
        payload = {"case_id": case_id, "closed_at": closed_at, "closed_by": actor.person_id,
                   "evidence_retention_survives": True}
        return self._record(
            self._event(EV_CASE_CLOSED, "dispute_case", case_id,
                        self._next_version("dispute_case", case_id),
                        "争议结案（证据保全期限继续有效）", payload, occurred_at=closed_at)
        )


