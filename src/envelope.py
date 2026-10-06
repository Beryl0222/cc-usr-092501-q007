"""领域事件信封的基础校验。

事件是已经发生的事实：只追加、不可原地改写。任何修订都必须以新事件表达，
原始事件继续用于追溯。
"""

from __future__ import annotations

from datetime import datetime

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = {
    # 承诺与人员
    "PROMISE_PUBLISHED",
    "PROMISE_RETIRED",
    "STAFF_REGISTERED",
    "STAFF_DEACTIVATED",
    # 合同履行（原始测量与安全事件只追加）
    "CONTRACT_SIGNED",
    "CONTRACT_NOTE_APPENDED",
    "INSTALLMENT_RECEIVED",
    "MEASUREMENT_RECORDED",
    "SAFETY_INCIDENT_RECORDED",
    "SERVICE_SESSION_RECORDED",
    "LEAVE_REQUESTED",
    "WITHDRAWAL_REQUESTED",
    # 争议案件
    "CASE_OPENED",
    "RECEIPT_REGISTERED",
    "RECEIPT_TAINTED",
    "EVIDENCE_HELD",
    "CASE_LOCKED",
    "CASE_TRANSFERRED",
    "MEDIATION_RECORDED",
    # 退款：候选计算 → 批准 → 支付/冲正/补付 → 结算
    "REFUND_CANDIDATE_COMPUTED",
    "REFUND_APPROVED",
    "REFUND_INSTALLMENT_PAID",
    "REFUND_PAYMENT_REVERSED",
    "REFUND_TOPUP_PAID",
    "SETTLEMENT_CLOSED",
    # 监管
    "REGULATORY_INVESTIGATION_OPENED",
    "REGULATORY_RESPONSE_FILED",
    "REGULATORY_INVESTIGATION_CLOSED",
    # 核对
    "MONTHLY_RECONCILIATION_RUN",
}

AGGREGATE_TYPES = {
    "marketing_promise",
    "staff_member",
    "service_contract",
    "dispute_case",
    "evidence_receipt",
    "refund_entry",
    "audit_report",
}


def validate_event(record: object) -> list[str]:
    if not isinstance(record, dict):
        return ["事件必须是 JSON 对象"]
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (
        not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] < 1
    ):
        errors.append("version 必须是正整数")
    if "event_type" in record and record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知事件类型：{record['event_type']}")
    if "aggregate_type" in record and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    if "occurred_at" in record:
        try:
            parsed = datetime.fromisoformat(str(record["occurred_at"]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                errors.append("occurred_at 必须包含时区")
        except ValueError:
            errors.append("occurred_at 必须是 ISO 8601 时间")
    if "actor" in record:
        actor = record["actor"]
        if not isinstance(actor, dict) or not actor.get("id") or not actor.get("role"):
            errors.append("actor 必须包含 id 与 role")
    return errors
