"""从事件日志重放出来的聚合状态（只读投影）。

状态本身不是事实来源，事件日志才是。任何字段都只能由对应事件推进，
不存在"修改测量/修改退款原因"的命令路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class State:
    # 广告与话术承诺：冻结来源（素材/话术引用与内容指纹）与有效期
    promises: dict[str, dict] = field(default_factory=dict)
    # 员工（含离职状态，离职不抹除历史）
    staff: dict[str, dict] = field(default_factory=dict)
    # 合同：签约时冻结的承诺快照、分期、服务记录、原始测量与安全事件
    contracts: dict[str, dict] = field(default_factory=dict)
    # 争议案件
    cases: dict[str, dict] = field(default_factory=dict)
    # 证据/支付回执：标识全局唯一，完整重传只算一次
    receipts: dict[str, dict] = field(default_factory=dict)
    # 退款台账：候选、批准、分期支付、冲正、补付、结算
    refunds: dict[str, dict] = field(default_factory=dict)
    # 月度核对报告
    reports: dict[str, dict] = field(default_factory=dict)
    # 合同 → 案件（含跨店转入）
    contract_cases: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def case_for_contract(self, contract_id: str) -> dict | None:
        for case_id in self.contract_cases.get(contract_id, []):
            case = self.cases[case_id]
            if case.get("status") != "settled":
                return case
        return None


def _patch(target: dict, event: dict, mutable: set[str]) -> None:
    for key, value in event["data"].items():
        if key in mutable:
            target[key] = value


def apply(state: State, event: dict) -> None:
    """把单个事件推进到状态投影。"""
    et = event["event_type"]
    aid = event["aggregate_id"]
    data = event.get("data", {})

    if et == "PROMISE_PUBLISHED":
        state.promises[aid] = {
            "promise_id": aid,
            "store_id": data["store_id"],
            "source_kind": data["source_kind"],          # ad_material / sales_script
            "source_ref": data["source_ref"],            # 素材或话术的来源定位
            "content_hash": data["content_hash"],        # 冻结内容指纹
            "claims": list(data["claims"]),              # 例如"急速暴瘦""不瘦退款"
            "refund_terms": dict(data["refund_terms"]),  # 签约时采用的退款口径
            "valid_from": data["valid_from"],
            "valid_to": data["valid_to"],
            "retired": False,
            "published_at": event["occurred_at"],
        }
    elif et == "PROMISE_RETIRED":
        state.promises[aid]["retired"] = True
        state.promises[aid]["retired_at"] = event["occurred_at"]

    elif et == "STAFF_REGISTERED":
        state.staff[aid] = {
            "staff_id": aid,
            "store_id": data["store_id"],
            "name": data["name"],
            "role": data["role"],
            "active": True,
            "registered_at": event["occurred_at"],
        }
    elif et == "STAFF_DEACTIVATED":
        state.staff[aid]["active"] = False
        state.staff[aid]["deactivated_at"] = event["occurred_at"]

    elif et == "CONTRACT_SIGNED":
        state.contracts[aid] = {
            "contract_id": aid,
            "store_id": data["store_id"],
            "consumer_id": data["consumer_id"],
            "sales_staff_id": data["sales_staff_id"],
            "signed_at": data["signed_at"],
            "promise_snapshot": dict(data["promise_snapshot"]),  # 关键：快照，不随后续修订变化
            "terms": dict(data["terms"]),
            "installments": [],   # 已收到的学员付款
            "paid_total_cents": 0,
            "sessions": [],
            "measurements": [],   # 原始测量，只追加
            "safety_incidents": [],  # 安全事件，只追加
            "leaves": [],
            "withdrawal": None,
            "notes": [],
            "linked_case_ids": [],
            "continued_from_case_id": data.get("continued_from_case_id"),
        }
        state.contract_cases[aid]  # noqa: B018 — 确保索引存在
    elif et == "CONTRACT_NOTE_APPENDED":
        contract = state.contracts[aid]
        contract["notes"].append(
            {"note_id": data["note_id"], "text": data["text"], "author_id": event["actor"]["id"],
             "at": event["occurred_at"]}
        )
    elif et == "INSTALLMENT_RECEIVED":
        contract = state.contracts[aid]
        contract["installments"].append(
            {"seq": data["seq"], "amount_cents": data["amount_cents"],
             "paid_at": data["paid_at"], "receipt_id": data.get("receipt_id")}
        )
        contract["paid_total_cents"] += data["amount_cents"]
    elif et == "MEASUREMENT_RECORDED":
        state.contracts[aid]["measurements"].append(
            {"measurement_id": data["measurement_id"], "taken_at": data["taken_at"],
             "weight_kg": data["weight_kg"], "recorded_by": event["actor"]["id"],
             "note": data.get("note", "")}
        )
    elif et == "SAFETY_INCIDENT_RECORDED":
        state.contracts[aid]["safety_incidents"].append(
            {"incident_id": data["incident_id"], "occurred_at": data["occurred_at"],
             "description": data["description"], "severity": data["severity"],
             "reported_by": event["actor"]["id"], "health_attribution": data.get("health_attribution", False)}
        )
    elif et == "SERVICE_SESSION_RECORDED":
        state.contracts[aid]["sessions"].append(
            {"session_id": data["session_id"], "scheduled_at": data["scheduled_at"],
             "delivered": data["delivered"], "note": data.get("note", "")}
        )
    elif et == "LEAVE_REQUESTED":
        state.contracts[aid]["leaves"].append(
            {"leave_id": data["leave_id"], "from": data["from"], "to": data["to"],
             "reason": data["reason"]}
        )
    elif et == "WITHDRAWAL_REQUESTED":
        state.contracts[aid]["withdrawal"] = {
            "requested_at": data["requested_at"], "reason_category": data["reason_category"],
            "reason_text": data.get("reason_text", "")}

    elif et == "CASE_OPENED":
        state.cases[aid] = {
            "case_id": aid,
            "contract_ids": [data["contract_id"]],
            "current_store_id": data["store_id"],
            "consumer_id": data["consumer_id"],
            "opened_at": event["occurred_at"],
            "reason_category": data["reason_category"],  # health / service / regret，登记后不可改
            "reason_text": data.get("reason_text", ""),
            "linked_incident_id": data.get("linked_incident_id"),
            "status": "open",
            "locked": False,
            "lock_reasons": [],
            "receipt_ids": [],
            "evidence_holds": [],
            "mediations": [],
            "investigations": [],   # [{id, status: open/closed, ...}]
            "refund_id": data["refund_id"],
            "store_history": [{"store_id": data["store_id"], "at": event["occurred_at"]}],
        }
        state.contract_cases[data["contract_id"]].append(aid)
        state.contracts[data["contract_id"]]["linked_case_ids"].append(aid)
        state.refunds[data["refund_id"]] = {
            "refund_id": data["refund_id"], "case_id": aid,
            "candidates": [], "approved": None, "payments": [],
            "reversals": [], "topups": [], "settlement": None}
    elif et == "RECEIPT_REGISTERED":
        state.receipts[aid] = {
            "receipt_id": aid, "case_id": data["case_id"], "kind": data["kind"],
            "provider": data["provider"], "fingerprint": data["fingerprint"],
            "amount_cents": data.get("amount_cents"), "visibility": data["visibility"],
            "submitted_by": data["submitted_by"], "registered_at": event["occurred_at"],
            "tainted": False, "first_event_id": event["event_id"]}
        state.cases[data["case_id"]]["receipt_ids"].append(aid)
    elif et == "RECEIPT_TAINTED":
        state.receipts[aid]["tainted"] = True
        state.receipts[aid]["taint"] = {
            "at": event["occurred_at"], "changed_fields": data["changed_fields"],
            "second_event_id": event["event_id"]}
    elif et == "EVIDENCE_HELD":
        case = state.cases[aid]
        case["evidence_holds"].append(
            {"hold_id": data["hold_id"], "receipt_id": data["receipt_id"],
             "retain_until": data["retain_until"], "reason": data.get("reason", ""),
             "ordered_by": event["actor"]["id"], "at": event["occurred_at"]})
    elif et == "CASE_LOCKED":
        case = state.cases[aid]
        case["locked"] = True
        case["lock_reasons"].append(
            {"reason": data["reason"], "detail": data.get("detail", ""),
             "at": event["occurred_at"]})
    elif et == "CASE_TRANSFERRED":
        case = state.cases[aid]
        case["current_store_id"] = data["to_store_id"]
        case["store_history"].append({"store_id": data["to_store_id"], "at": event["occurred_at"]})
        if data.get("new_contract_id") and data["new_contract_id"] not in case["contract_ids"]:
            case["contract_ids"].append(data["new_contract_id"])
            state.contract_cases[data["new_contract_id"]].append(aid)
            state.contracts[data["new_contract_id"]]["linked_case_ids"].append(aid)
            state.contracts[data["new_contract_id"]]["continued_from_case_id"] = aid
    elif et == "MEDIATION_RECORDED":
        state.cases[aid]["mediations"].append(
            {"mediation_id": data["mediation_id"], "at": event["occurred_at"],
             "agreed_amount_cents": data.get("agreed_amount_cents"),
             "consumer_present": data["consumer_present"], "outcome": data["outcome"],
             "notes": data.get("notes", ""), "mediator_id": event["actor"]["id"]})
    elif et == "REGULATORY_INVESTIGATION_OPENED":
        state.cases[aid]["investigations"].append(
            {"investigation_id": data["investigation_id"], "status": "open",
             "opened_at": event["occurred_at"], "authority": data.get("authority", ""),
             "responses": []})
    elif et == "REGULATORY_RESPONSE_FILED":
        inv = next(i for i in state.cases[aid]["investigations"]
                   if i["investigation_id"] == data["investigation_id"])
        inv["responses"].append(
            {"response_id": data["response_id"], "at": event["occurred_at"],
             "content_ref": data["content_ref"], "filed_by": event["actor"]["id"]})
    elif et == "REGULATORY_INVESTIGATION_CLOSED":
        inv = next(i for i in state.cases[aid]["investigations"]
                   if i["investigation_id"] == data["investigation_id"])
        inv["status"] = "closed"
        inv["closed_at"] = event["occurred_at"]

    elif et == "REFUND_CANDIDATE_COMPUTED":
        refund = state.refunds[aid]
        refund["candidates"].append(
            {"candidate_id": data["candidate_id"], "at": event["occurred_at"],
             "reason_category": data["reason_category"], "amount_cents": data["amount_cents"],
             "basis": data["basis"]})
    elif et == "REFUND_APPROVED":
        refund = state.refunds[aid]
        refund["approved"] = {
            "approval_id": data["approval_id"], "candidate_id": data["candidate_id"],
            "amount_cents": data["amount_cents"], "at": event["occurred_at"],
            "approver_id": event["actor"]["id"]}
    elif et == "REFUND_INSTALLMENT_PAID":
        state.refunds[aid]["payments"].append(
            {"payment_id": data["payment_id"], "amount_cents": data["amount_cents"],
             "at": event["occurred_at"], "receipt_id": data.get("receipt_id")})
    elif et == "REFUND_PAYMENT_REVERSED":
        state.refunds[aid]["reversals"].append(
            {"reversal_id": data["reversal_id"], "payment_id": data["payment_id"],
             "amount_cents": data["amount_cents"], "at": event["occurred_at"],
             "reason": data.get("reason", "")})
    elif et == "REFUND_TOPUP_PAID":
        state.refunds[aid]["topups"].append(
            {"topup_id": data["topup_id"], "reversal_id": data.get("reversal_id"),
             "amount_cents": data["amount_cents"], "at": event["occurred_at"]})
    elif et == "SETTLEMENT_CLOSED":
        refund = state.refunds[aid]
        refund["settlement"] = {"at": event["occurred_at"], "closed_by": event["actor"]["id"]}
        state.cases[refund["case_id"]]["status"] = "settled"

    elif et == "MONTHLY_RECONCILIATION_RUN":
        state.reports[aid] = {"report_id": aid, **data, "at": event["occurred_at"]}

    else:  # pragma: no cover - 信封校验已拦截未知类型
        raise ValueError(f"未知事件类型：{et}")


def load_state(store) -> State:
    state = State()
    for event in store.events():
        apply(state, event)
    return state


def refund_net_paid_cents(refund: dict) -> int:
    paid = sum(p["amount_cents"] for p in refund["payments"])
    reversed_ = sum(r["amount_cents"] for r in refund["reversals"])
    topups = sum(t["amount_cents"] for t in refund["topups"])
    return paid - reversed_ + topups
