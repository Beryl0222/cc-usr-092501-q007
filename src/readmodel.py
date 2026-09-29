"""只读视图：消费者 API、门店待办、专员材料范围与月度核对命令。

所有视图都从冻结事件重放出的后端状态读取，不做任何写入；门店视图强制按
``involved_stores`` 过滤，门店账号只能看到本店待办。
"""

from __future__ import annotations

from typing import Any

from . import domain
from .services import (
    CASE_CLOSED_STATUS,
    EV_REFUND_CANDIDATE_CALCULATED,
    INV_OPEN,
    Backend,
)


def _case_event_payloads(backend: Backend, case_id: str, event_type: str) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in backend.case_events.get(case_id, [])
        if e["event_type"] == event_type
    ]


# ---- 消费者 API -----------------------------------------------------------


def consumer_view(backend: Backend, consumer_id: str) -> dict[str, Any]:
    """展示该消费者每个案件所采用的合同、每笔退款计算明细与结算调整。"""
    cases_out: list[dict[str, Any]] = []
    for case in backend.cases.values():
        if case["consumer_id"] != consumer_id:
            continue
        contract_ids = case["contract_ids"]
        contracts_out = []
        for cid in contract_ids:
            c = backend.contracts[cid]
            contracts_out.append(
                {
                    "contract_id": cid,
                    "store_id": c["store_id"],
                    "signed_at": c["signed_at"],
                    "sales_person_id": c["sales_person_id"],
                    "document": c["document"],
                    "adopted_promise": c["adopted_promise"],
                    "payment_plan": c.get("payment_plan"),
                    "payments": [
                        {
                            "payment_id": p["payment_id"],
                            "amount_cents": p["amount_cents"],
                            "paid_at": p["paid_at"],
                            "provider": p["provider"],
                            "receipt_fingerprint": p["receipt_fingerprint"],
                        }
                        for p in backend.payments.get(cid, [])
                    ],
                }
            )
        candidates = _case_event_payloads(backend, case["case_id"], EV_REFUND_CANDIDATE_CALCULATED)
        refunds_out = [_refund_view(backend, rid) for rid in case["refund_ids"]]
        evidence_out = [
            {
                "evidence_id": e["evidence_id"],
                "kind": e["kind"],
                "source": e["source"],
                "filename": e["filename"],
                "fingerprint": e["fingerprint"],
                "held_at": e["held_at"],
                "retention_until": e["retention_until"],
            }
            for e in backend.evidence.get(case["case_id"], [])
            if "consumer" in e["visible_to"]
        ]
        cases_out.append(
            {
                "case_id": case["case_id"],
                "status": case["status"],
                "locked": case["locked"],
                "lock_reasons": case["lock_reasons"],
                "reason": case["reason"],
                "claimed_at": case["claimed_at"],
                "involved_stores": sorted(case["involved_stores"]),
                "investigation": case["investigation"],
                "contracts": contracts_out,
                "candidates": candidates,
                "refunds": refunds_out,
                "evidence": evidence_out,
            }
        )
    return {"consumer_id": consumer_id, "cases": sorted(cases_out, key=lambda c: c["claimed_at"])}


def _refund_view(backend: Backend, refund_id: str) -> dict[str, Any]:
    r = backend.refunds[refund_id]
    net_paid = r["settled_cents"]
    return {
        "refund_id": refund_id,
        "candidate_id": r["candidate_id"],
        "approved_cents": r["approved_cents"],
        "paid_net_cents": net_paid,
        "status": r["status"],
        "approver_id": r["approver_id"],
        "agreement": r["agreement"],
        "installments": [
            {
                "installment_id": iid,
                **{k: v for k, v in inst.items()},
            }
            for iid, inst in sorted(r["installments"].items())
        ],
        "balanced": net_paid == r["approved_cents"],
    }


# ---- 门店待办 -------------------------------------------------------------


def store_todo(backend: Backend, store_id: str) -> dict[str, Any]:
    """门店账号只看到本店待办：本店涉案且未结的事项。"""
    todos: list[dict[str, Any]] = []
    for case in backend.cases.values():
        if store_id not in case["involved_stores"]:
            continue
        items: list[str] = []
        if case["status"] == CASE_CLOSED_STATUS:
            continue
        if case["locked"]:
            items.append(f"案件锁定待处置：{case['lock_reasons']}")
        if not case["refund_ids"]:
            items.append("等待专员计算退款候选/调解")
        else:
            for rid in case["refund_ids"]:
                r = backend.refunds[rid]
                if r["settled_cents"] < r["approved_cents"]:
                    items.append(
                        f"退款 {rid} 待支付 {r['approved_cents'] - r['settled_cents']} 分"
                    )
                elif r["settled_cents"] > r["approved_cents"]:
                    items.append(f"退款 {rid} 多付 {r['settled_cents'] - r['approved_cents']} 分待冲正")
        inv = case.get("investigation")
        if inv and inv["status"] == INV_OPEN:
            items.append(f"监管调查进行中 {inv['authority']}/{inv['reference']}")
        for e in backend.evidence.get(case["case_id"], []):
            items.append(f"证据 {e['evidence_id']} 保全至 {e['retention_until']}")
        todos.append(
            {
                "case_id": case["case_id"],
                "consumer_id": case["consumer_id"],
                "home_store_id": case["home_store_id"],
                "reason": case["reason"],
                "locked": case["locked"],
                "items": items,
            }
        )
    return {"store_id": store_id, "todos": sorted(todos, key=lambda t: t["case_id"])}


# ---- 月度核对 -------------------------------------------------------------


def monthly_reconciliation(backend: Backend, month: str, *, retention_warning_days: int = 30) -> dict[str, Any]:
    """月度命令：核对承诺兑现、退款差额与未结调查。

    ``month`` 形如 ``2026-09``。返回零个 findings 即全部对平；findings 每条都
    指向具体案件/退款/承诺，便于监管复核。
    """
    from datetime import datetime, timedelta

    month_start = datetime.fromisoformat(month + "-01T00:00:00+08:00")
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    month_end = next_month

    findings: list[dict[str, Any]] = []
    guarantee_rows: list[dict[str, Any]] = []

    # 1) 承诺兑现：本月签约且承诺到期点落在本月底前的“不瘦退款”
    for contract in backend.contracts.values():
        if not (month_start <= domain.parse_at(contract["signed_at"]) < month_end):
            continue
        guarantee = contract["adopted_promise"].get("guarantee")
        if not guarantee:
            continue
        by_date = domain.parse_at(guarantee["by_date"])
        row: dict[str, Any] = {
            "contract_id": contract["contract_id"],
            "promise_id": contract["promise_id"],
            "guarantee_by": guarantee["by_date"],
            "due_in_month": by_date < month_end,
        }
        if by_date < month_end:
            meas = backend.measurements.get(contract["contract_id"], [])
            qualified = [m for m in meas if domain.parse_at(m["taken_at"]) <= by_date]
            if not qualified:
                row["outcome"] = "missing_measurement"
                findings.append({"code": "GUARANTEE_NO_MEASUREMENT", **row})
            else:
                latest = max(qualified, key=lambda m: domain.parse_at(m["taken_at"]))
                row["latest_weight_kg"] = latest["weight_kg"]
                if float(latest["weight_kg"]) > float(guarantee["target_weight_kg"]):
                    row["outcome"] = "target_missed"
                    related_cases = [
                        c for c in backend.cases.values()
                        if contract["contract_id"] in c["contract_ids"]
                    ]
                    refunded = any(
                        any(backend.refunds[rid]["settled_cents"] > 0 for rid in c["refund_ids"])
                        for c in related_cases
                    )
                    if not refunded:
                        findings.append({"code": "GUARANTEE_MISSED_NO_REFUND", **row})
                else:
                    row["outcome"] = "met"
        guarantee_rows.append(row)

    # 2) 退款：批准额与净付额必须一致；多付未冲正、少付未补付都报出
    for refund_id, r in backend.refunds.items():
        if r["settled_cents"] != r["approved_cents"]:
            findings.append(
                {
                    "code": "REFUND_NOT_BALANCED",
                    "refund_id": refund_id,
                    "case_id": r["case_id"],
                    "approved_cents": r["approved_cents"],
                    "paid_net_cents": r["settled_cents"],
                    "delta_cents": r["settled_cents"] - r["approved_cents"],
                    "status": r["status"],
                }
            )

    # 3) 未结调查与锁定案件
    open_investigations: list[dict[str, Any]] = []
    locked_open_cases: list[dict[str, Any]] = []
    for case in backend.cases.values():
        inv = case.get("investigation")
        if inv and inv["status"] == INV_OPEN:
            open_investigations.append(
                {"case_id": case["case_id"], "authority": inv["authority"],
                 "reference": inv["reference"], "opened_at": inv["opened_at"]}
            )
        if case["locked"] and case["status"] != CASE_CLOSED_STATUS:
            locked_open_cases.append({"case_id": case["case_id"], "reasons": case["lock_reasons"]})
    if open_investigations:
        findings.append({"code": "OPEN_INVESTIGATIONS", "cases": open_investigations})
    if locked_open_cases:
        findings.append({"code": "LOCKED_CASES_PENDING", "cases": locked_open_cases})

    # 4) 证据保全期限临近（仅提示，绝不因任何业务动作删除证据）
    horizon = month_end + timedelta(days=retention_warning_days)
    retention: list[dict[str, Any]] = []
    for case in backend.cases.values():
        for e in backend.evidence.get(case["case_id"], []):
            until = domain.parse_at(e["retention_until"])
            if until < horizon:
                retention.append(
                    {"case_id": case["case_id"], "evidence_id": e["evidence_id"],
                     "retention_until": e["retention_until"]}
                )

    return {
        "month": month,
        "guarantees": guarantee_rows,
        "open_investigations": open_investigations,
        "locked_open_cases": locked_open_cases,
        "retention_expiring": retention,
        "findings": findings,
        "balanced": not findings,
    }
