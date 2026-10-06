"""退款候选计算。

候选金额只由签约时冻结的承诺快照（contract.promise_snapshot）、合同条款、
已付款与已履行服务决定，不读取任何事后被改写过的"当前版本"，因此销售口头承诺
与广告口径在签约那一刻就被固定下来。

三类原因，规则不同：

- health（健康原因）：全额退款 + 未消费课时不扣手续费，证据要求安全事件或
  健康归因。安全责任不因退营而消失。
- service（服务未履行）：未履行比例 × 已付款，按承诺快照中的违约金封顶；
  "急速暴瘦"这类结果承诺未达成时，比例可上浮到 100%（见 promise 口径）。
- regret（普通反悔）：按签约时承诺的冷静期/阶梯退费表计算，已消费课时按
  单价扣除，并扣除签约时承诺的手续费率。
"""

from __future__ import annotations

from dataclasses import dataclass

CENTS = 100


@dataclass(frozen=True)
class RefundCandidate:
    reason_category: str
    amount_cents: int
    basis: dict


def _promise_terms(promise_snapshot: dict) -> dict:
    terms = dict(promise_snapshot.get("refund_terms", {}))
    terms.setdefault("service_fee_rate", 0)          # 普通反悔手续费率
    terms.setdefault("service_default_cap_rate", 1)  # 违约金封顶（默认全退）
    terms.setdefault("result_claim_full_refund", False)
    return terms


def _sessions_status(contract: dict) -> tuple[int, int]:
    """返回（已排课总数，已交付数）。"""
    sessions = contract["sessions"]
    total = len(sessions)
    delivered = sum(1 for s in sessions if s["delivered"])
    return total, delivered


def compute_candidate(contract: dict, reason_category: str, *, linked_incident: dict | None = None) -> RefundCandidate:
    paid = contract["paid_total_cents"]
    total, delivered = _sessions_status(contract)
    terms = _promise_terms(contract["promise_snapshot"])
    contract_terms = contract.get("terms", {})
    total_sessions_planned = max(total, int(contract_terms.get("planned_sessions", total) or 0))

    if reason_category == "health":
        # 健康原因：未交付课时全退，已交付课时按健康归因决定是否扣费。
        if linked_incident is None:
            raise ValueError("健康原因退营必须关联安全事件")
        if linked_incident.get("health_attribution") or linked_incident["severity"] in ("serious", "critical"):
            amount = paid  # 训练营有健康责任：全额
            note = "安全事件健康归因成立，已付款全额退还"
        else:
            undelivered = total_sessions_planned - delivered
            amount = paid * undelivered // max(total_sessions_planned, 1)
            note = "按未交付课时比例退还，不扣手续费"
        return RefundCandidate("health", amount, {
            "rule": "health_full_or_proportional", "paid_cents": paid,
            "delivered_sessions": delivered, "planned_sessions": total_sessions_planned,
            "incident_id": linked_incident["incident_id"], "note": note})

    if reason_category == "service":
        # 服务未履行：未履行比例退款，违约金按承诺快照封顶。
        if total_sessions_planned == 0:
            unfulfilled_rate = 1
        else:
            unfulfilled = total_sessions_planned - delivered
            unfulfilled_rate = unfulfilled / total_sessions_planned
        amount = round(paid * unfulfilled_rate)
        cap_rate = float(terms["service_default_cap_rate"])
        # 结果承诺（如"不瘦退款"）在承诺快照中标记后，未达成即可主张全额。
        if terms.get("result_claim_full_refund") and _result_claim_unmet(contract):
            amount = paid
            cap_note = "签约时承诺的结果保证（如不瘦退款）未达成，按承诺全额"
        else:
            amount = min(amount, round(paid * cap_rate)) if cap_rate < 1 else amount
            cap_note = f"按未履行比例 {unfulfilled_rate:.2%}，违约金封顶口径 {cap_rate:.0%}"
        return RefundCandidate("service", int(amount), {
            "rule": "service_unfulfilled_proportional", "paid_cents": paid,
            "delivered_sessions": delivered, "planned_sessions": total_sessions_planned,
            "unfulfilled_rate": round(unfulfilled_rate, 4), "note": cap_note,
            "result_claim": contract["promise_snapshot"].get("claims", [])})

    if reason_category == "regret":
        # 普通反悔：执行签约时承诺的阶梯退费表；无表时仅退未交付部分并扣手续费。
        schedule = terms.get("regret_schedule")  # [{within_days, refund_rate}, ...]
        if total_sessions_planned == 0:
            consumed_rate = 0.0
        else:
            consumed_rate = delivered / total_sessions_planned
        if schedule:
            rate = _schedule_rate(schedule, contract, consumed_rate)
        else:
            fee_rate = float(terms["service_fee_rate"])
            rate = max(0.0, (1 - consumed_rate) * (1 - fee_rate))
        amount = round(paid * rate)
        return RefundCandidate("regret", int(amount), {
            "rule": "regret_schedule", "paid_cents": paid,
            "consumed_rate": round(consumed_rate, 4), "applied_refund_rate": round(rate, 4),
            "note": "按签约时承诺的冷静期/阶梯退费表"})

    raise ValueError(f"未知退款原因：{reason_category}")


def _result_claim_unmet(contract: dict) -> bool:
    """依据不可改写的原始测量判断结果承诺是否未达成。

    "急速暴瘦"类承诺在快照中给出目标体重（target_weight_kg）；以基线与最近一次
    原始测量比较。测量只追加，销售无法事后改写。
    """
    target = contract["promise_snapshot"].get("target_weight_kg")
    measurements = contract["measurements"]
    if target is None or not measurements:
        return False
    latest = measurements[-1]["weight_kg"]
    return latest > target


def _schedule_rate(schedule: list[dict], contract: dict, consumed_rate: float) -> float:
    """按退营请求距签约的天数命中阶梯；超过最后一档则按未消费比例。"""
    from .state import parse_ts
    withdrawal = contract.get("withdrawal")
    if withdrawal is None:
        raise ValueError("普通反悔必须先登记退营请求")
    days = (parse_ts(withdrawal["requested_at"]) - parse_ts(contract["signed_at"])).days
    chosen = None
    for row in sorted(schedule, key=lambda r: r["within_days"]):
        if days <= row["within_days"]:
            chosen = row
            break
    if chosen is None:
        return max(0.0, 1 - consumed_rate)
    return float(chosen["refund_rate"])
