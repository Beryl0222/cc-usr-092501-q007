"""纯领域规则：按签约时冻结的承诺与合同计算退款候选。

三类退款原因采用不同规则，全部以“分”为整数单位，避免浮点误差：

- ``health`` 健康原因：关联安全事件全额；其余按未履行价值与剩余服务比例核算。
- ``service_unperformed`` 服务未履行：到期未交付课时的单价之和；若签约时冻结的
  “不瘦退款”承诺经阶段测量确认未达标，取承诺金额（全额）与之的较大者。
- ``regret`` 普通反悔：冷静期内退已付减去已消费课时与约定管理费；超期候选为 0。

计算只读取事实快照，不做任何持久化；每个候选都返回可追溯的明细行与依据事件。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

REASON_HEALTH = "health"
REASON_SERVICE_UNPERFORMED = "service_unperformed"
REASON_REGRET = "regret"
REASONS = (REASON_HEALTH, REASON_SERVICE_UNPERFORMED, REASON_REGRET)

DEFAULT_COOLING_OFF_DAYS = 7


def parse_at(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _line(code: str, description: str, amount: int, basis: list[str]) -> dict[str, Any]:
    return {"code": code, "description": description, "amount_cents": amount, "basis": list(basis)}


def _delivered_value(contract: dict[str, Any], delivered_session_ids: set[str]) -> int:
    return sum(
        int(s.get("unit_price_cents", 0))
        for s in contract.get("sessions", [])
        if s["session_id"] in delivered_session_ids
    )


def _guarantee_triggered(contract: dict[str, Any], measurements: list[dict[str, Any]]) -> tuple[bool, str | None]:
    guarantee = contract.get("guarantee")
    if not guarantee:
        return False, None
    by_date = parse_at(guarantee["by_date"])
    target = float(guarantee["target_weight_kg"])
    qualifying = [m for m in measurements if parse_at(m["taken_at"]) <= by_date]
    if not qualifying:
        return False, None
    latest = max(qualifying, key=lambda m: parse_at(m["taken_at"]))
    if float(latest["weight_kg"]) > target:
        return True, latest["measurement_id"]
    return False, None


def calculate_refund(
    *,
    reason: str,
    contract: dict[str, Any],
    payments: list[dict[str, Any]],
    delivered_sessions: list[dict[str, Any]],
    measurements: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    claimed_at: str,
) -> dict[str, Any]:
    """返回 ``{"amount_cents", "rule", "lines"}`` 的退款候选。

    ``contract`` 必须是签约时冻结的合同条款快照；``payments`` 为已冻结付款事实。
    """
    if reason not in REASONS:
        raise ValueError(f"未知退款原因：{reason}（允许 {REASONS}）")

    claim_dt = parse_at(claimed_at)
    paid_total = sum(int(p["amount_cents"]) for p in payments)
    delivered_ids = {s["session_id"] for s in delivered_sessions}
    delivered_value = _delivered_value(contract, delivered_ids)
    payment_ids = [p["payment_id"] for p in payments]

    triggered, basis_measurement = _guarantee_triggered(contract, measurements)
    guarantee = contract.get("guarantee") or {}
    guarantee_amount = (
        paid_total
        if guarantee.get("refund_kind") == "full"
        else int(guarantee.get("amount_cents", 0))
    )

    lines: list[dict[str, Any]] = []
    amount = 0

    if reason == REASON_HEALTH:
        linked_incident = next(
            (i for i in incidents
             if i.get("linked_to_case", True) and parse_at(i["occurred_at"]) <= claim_dt),
            None,
        )
        if linked_incident is not None:
            amount = paid_total
            lines.append(
                _line(
                    "HEALTH_INCIDENT_FULL",
                    "训练关联安全事件，按健康原因全额退还已付款",
                    amount,
                    payment_ids + [linked_incident["incident_id"]],
                )
            )
        else:
            refundable = max(0, paid_total - delivered_value)
            amount = refundable
            lines.append(
                _line(
                    "HEALTH_WITHDRAWAL_PRORATED",
                    "健康原因退营：已付款扣除已交付课时价值",
                    refundable,
                    payment_ids + [s for s in delivered_ids],
                )
            )

    elif reason == REASON_SERVICE_UNPERFORMED:
        due_sessions = [
            s
            for s in contract.get("sessions", [])
            if parse_at(s["scheduled_for"]) <= claim_dt and s["session_id"] not in delivered_ids
        ]
        unperformed = sum(int(s["unit_price_cents"]) for s in due_sessions)
        # 退款不超过“已付且尚未消费”的部分；承诺兜底可在下方另行补足
        unperformed = min(unperformed, max(0, paid_total - delivered_value))
        lines.append(
            _line(
                "UNPERFORMED_SESSIONS",
                "到期未交付课时按合同单价退还",
                unperformed,
                payment_ids + [s["session_id"] for s in due_sessions],
            )
        )
        amount = unperformed
        if triggered:
            g_amount = min(guarantee_amount, paid_total)
            if g_amount > amount:
                lines.append(
                    _line(
                        "GUARANTEE_MONEY_BACK",
                        "签约时“不瘦退款”承诺经测量确认未达标，按承诺补足",
                        g_amount - amount,
                        [f"promise:{contract.get('promise_id', '')}", basis_measurement or ""],
                    )
                )
                amount = g_amount

    else:  # REASON_REGRET
        cooling_days = int(contract.get("cooling_off_days", DEFAULT_COOLING_OFF_DAYS))
        signed_dt = parse_at(contract["signed_at"])
        admin_fee = int(contract.get("admin_fee_cents", 0))
        if claim_dt <= signed_dt + timedelta(days=cooling_days):
            amount = max(0, paid_total - delivered_value - admin_fee)
            lines.append(
                _line(
                    "COOLING_OFF_REFUND",
                    f"冷静期（{cooling_days} 日）内反悔：退还已付款扣除已消费课时与管理费",
                    amount,
                    payment_ids + list(delivered_ids),
                )
            )
        else:
            lines.append(
                _line(
                    "NO_COOLING_OFF_REFUND",
                    "超过冷静期的普通反悔，无可计算退款（和解让利须另行审批留痕）",
                    0,
                    payment_ids,
                )
            )

    return {
        "rule": reason,
        "claimed_at": claimed_at,
        "paid_total_cents": paid_total,
        "delivered_value_cents": delivered_value,
        "guarantee_triggered": triggered,
        "amount_cents": int(max(0, amount)),
        "lines": lines,
    }
