"""承诺与争议处置后端的领域服务。

角色划分（调解、退款批准、监管答复必须由不同角色完成）：

- marketing          集团素材：发布/撤回广告与话术承诺（冻结来源与有效期）
- sales / store_manager / coach / safety_officer  门店侧：只能追加说明与原始记录
- dispute_specialist 争议专员：仅在双方可见范围内调取材料，可登记案件与保全
- mediator           调解
- refund_approver    退款批准（不得是本案调解人）
- refund_payer       退款支付、冲正、补付
- settlement_officer 结算关闭
- regulatory_officer 监管调查与答复（不得是本案调解人或批准人）
- auditor            月度核对
- consumer           消费者
- group_admin        集团管理员（系统角色，负责登记/停用员工，不参与业务处置）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from .state import State, load_state, parse_ts, refund_net_paid_cents
from .store import ConflictError, DomainError, EventStore, PermissionDenied
from .refund_rules import compute_candidate

# 需要在册且未离职才能以该身份行动的角色
STAFF_ROLES = {
    "marketing", "sales", "store_manager", "coach", "safety_officer",
    "dispute_specialist", "mediator", "refund_approver", "refund_payer",
    "settlement_officer", "regulatory_officer", "auditor",
}

# 系统引导角色：无需在员工册中登记（用于登记第一批员工）
SYSTEM_ROLES = {"group_admin"}

REASON_CATEGORIES = {"health", "service", "regret"}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class CampDomain:
    def __init__(self, store: EventStore):
        self.store = store
        self.state: State = load_state(store)

    def _refresh(self) -> None:
        """从日志重建投影（调用方须已持有命令锁）。"""
        self.state = load_state(self.store)

    # ------------------------------------------------------------------ 基础

    def _append(self, event_type: str, aggregate_type: str, aggregate_id: str,
                data: dict, actor: dict, summary: str, *,
                expected_version: int | None = None, event_id: str | None = None,
                occurred_at: str | None = None) -> dict:
        event = self.store.append(
            event_type, aggregate_type, aggregate_id, data, actor=actor, summary=summary,
            event_id=event_id or _new_id(event_type.lower()),
            expected_version=expected_version, occurred_at=occurred_at)
        from .state import apply
        apply(self.state, event)
        return event

    def _actor(self, actor: dict) -> dict:
        if not isinstance(actor, dict) or not actor.get("id") or not actor.get("role"):
            raise PermissionDenied("actor 必须包含 id 与 role")
        role = actor["role"]
        if role in STAFF_ROLES:
            staff = self.state.staff.get(actor["id"])
            if staff is None:
                raise PermissionDenied(f"员工未登记：{actor['id']}")
            if not staff["active"]:
                raise PermissionDenied(f"员工已离职，不能再执行业务动作：{actor['id']}")
            if staff["role"] != role:
                raise PermissionDenied(
                    f"员工 {actor['id']} 角色为 {staff['role']}，不能以 {role} 身份操作")
        elif role in SYSTEM_ROLES:
            pass
        elif role != "consumer":
            raise PermissionDenied(f"未知角色：{role}")
        return {"id": actor["id"], "role": role}

    def _require_roles(self, actor: dict, roles: set[str]) -> dict:
        actor = self._actor(actor)
        if actor["role"] not in roles:
            raise PermissionDenied(f"{actor['role']} 无权执行该操作，需要：{'/'.join(sorted(roles))}")
        return actor

    def _contract(self, contract_id: str) -> dict:
        contract = self.state.contracts.get(contract_id)
        if contract is None:
            raise DomainError(f"合同不存在：{contract_id}")
        return contract

    def _case(self, case_id: str) -> dict:
        case = self.state.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在：{case_id}")
        return case

    def _refund_for_case(self, case: dict) -> dict:
        return self.state.refunds[case["refund_id"]]

    def _open_investigations(self, case: dict) -> list[dict]:
        return [i for i in case["investigations"] if i["status"] == "open"]

    def _mediator_ids(self, case: dict) -> set[str]:
        return {m["mediator_id"] for m in case["mediations"]}

    # ------------------------------------------------------------------ 承诺

    def publish_promise(self, actor: dict, *, promise_id: str | None = None, store_id: str,
                        source_kind: str, source_ref: str, content_hash: str, claims: list[str],
                        refund_terms: dict, valid_from: str, valid_to: str) -> dict:
        """登记广告素材/销售话术：来源、指纹与有效期一并冻结。"""
        actor = self._require_roles(actor, {"marketing"})
        if source_kind not in {"ad_material", "sales_script"}:
            raise DomainError("source_kind 必须是 ad_material 或 sales_script")
        if not content_hash or not source_ref:
            raise DomainError("承诺必须冻结来源定位与内容指纹")
        if parse_ts(valid_to) <= parse_ts(valid_from):
            raise DomainError("有效期止必须晚于起")
        promise_id = promise_id or _new_id("promise")
        if promise_id in self.state.promises:
            raise DomainError(f"承诺标识已存在：{promise_id}")
        return self._append(
            "PROMISE_PUBLISHED", "marketing_promise", promise_id,
            {"store_id": store_id, "source_kind": source_kind, "source_ref": source_ref,
             "content_hash": content_hash, "claims": list(claims),
             "refund_terms": refund_terms, "valid_from": valid_from, "valid_to": valid_to},
            actor, f"冻结{source_kind}承诺：{'、'.join(claims)}")

    def retire_promise(self, actor: dict, promise_id: str) -> dict:
        """撤回承诺不删除历史：已签约合同继续使用签约时快照。"""
        actor = self._require_roles(actor, {"marketing"})
        promise = self.state.promises.get(promise_id)
        if promise is None:
            raise DomainError(f"承诺不存在：{promise_id}")
        if promise["retired"]:
            raise DomainError("承诺已撤回")
        return self._append("PROMISE_RETIRED", "marketing_promise", promise_id, {},
                            actor, "撤回承诺，已签合同不受影响")

    # ------------------------------------------------------------------ 人员

    def register_staff(self, actor: dict, *, staff_id: str, store_id: str, name: str,
                       role: str) -> dict:
        actor = self._require_roles(actor, {"group_admin", "store_manager"})
        if role not in STAFF_ROLES:
            raise DomainError(f"不支持的员工角色：{role}")
        if staff_id in self.state.staff:
            raise DomainError(f"员工已登记：{staff_id}")
        return self._append(
            "STAFF_REGISTERED", "staff_member", staff_id,
            {"store_id": store_id, "name": name, "role": role},
            actor, f"登记员工 {name}（{role}）")

    def deactivate_staff(self, actor: dict, staff_id: str) -> dict:
        """销售/教练离职：停用其后续操作，但历史事件署名全部保留。"""
        actor = self._require_roles(actor, {"group_admin", "store_manager"})
        staff = self.state.staff.get(staff_id)
        if staff is None:
            raise DomainError(f"员工不存在：{staff_id}")
        if not staff["active"]:
            raise DomainError("员工已离职")
        return self._append("STAFF_DEACTIVATED", "staff_member", staff_id, {},
                            actor, f"员工离职：{staff['name']}")

    # ------------------------------------------------------------------ 合同

    def sign_contract(self, actor: dict, *, contract_id: str, store_id: str, consumer_id: str,
                      sales_staff_id: str, signed_at: str, promise_id: str,
                      terms: dict) -> dict:
        """签约：把当时有效的承诺内容整体快照进合同，之后承诺撤回/改写都不影响。"""
        actor = self._require_roles(actor, {"sales", "store_manager"})
        promise = self.state.promises.get(promise_id)
        if promise is None:
            raise DomainError(f"承诺不存在：{promise_id}")
        sales = self.state.staff.get(sales_staff_id)
        if sales is None or not sales["active"] or sales["role"] != "sales":
            raise DomainError("签约销售必须是在册未离职的销售人员")
        signed = parse_ts(signed_at)
        if promise["retired"] or not (parse_ts(promise["valid_from"]) <= signed <= parse_ts(promise["valid_to"])):
            raise DomainError("签约时承诺已撤回或不在有效期内，不能据此签约")
        if contract_id in self.state.contracts:
            raise DomainError(f"合同标识已存在：{contract_id}")
        snapshot = {
            "promise_id": promise_id,
            "source_kind": promise["source_kind"],
            "source_ref": promise["source_ref"],
            "content_hash": promise["content_hash"],
            "claims": list(promise["claims"]),
            "refund_terms": dict(promise["refund_terms"]),
            "target_weight_kg": promise["refund_terms"].get("target_weight_kg"),
            "frozen_at": signed_at,
        }
        return self._append(
            "CONTRACT_SIGNED", "service_contract", contract_id,
            {"store_id": store_id, "consumer_id": consumer_id, "sales_staff_id": sales_staff_id,
             "signed_at": signed_at, "promise_snapshot": snapshot, "terms": dict(terms)},
            actor, f"签订合同并冻结承诺快照 {promise_id}", occurred_at=signed_at)

    def append_contract_note(self, actor: dict, *, contract_id: str, note_id: str,
                             text: str) -> dict:
        """业绩人员只能补充说明。该路径永远触碰不到测量与安全事件。"""
        actor = self._require_roles(actor, {"sales", "store_manager", "dispute_specialist"})
        self._contract(contract_id)
        if any(n["note_id"] == note_id for n in self.state.contracts[contract_id]["notes"]):
            raise DomainError(f"备注标识重复：{note_id}")
        return self._append(
            "CONTRACT_NOTE_APPENDED", "service_contract", contract_id,
            {"note_id": note_id, "text": text}, actor, "追加合同说明（不改动任何原始记录）")

    def receive_installment(self, actor: dict, *, contract_id: str, seq: int,
                            amount_cents: int, paid_at: str, receipt_id: str | None = None) -> dict:
        actor = self._require_roles(actor, {"sales", "store_manager"})
        contract = self._contract(contract_id)
        if amount_cents <= 0:
            raise DomainError("付款金额必须为正")
        if any(i["seq"] == seq for i in contract["installments"]):
            raise DomainError(f"分期序号重复：{seq}")
        if receipt_id and receipt_id in self.state.receipts:
            raise DomainError(f"支付回执已登记，禁止重复入账：{receipt_id}")
        return self._append(
            "INSTALLMENT_RECEIVED", "service_contract", contract_id,
            {"seq": seq, "amount_cents": amount_cents, "paid_at": paid_at,
             "receipt_id": receipt_id}, actor, f"收到第 {seq} 期付款")

    def record_measurement(self, actor: dict, *, contract_id: str, measurement_id: str,
                           taken_at: str, weight_kg: float, note: str = "") -> dict:
        """原始测量只追加，系统中不存在修改/删除测量的命令。"""
        actor = self._require_roles(actor, {"coach", "safety_officer"})
        contract = self._contract(contract_id)
        if any(m["measurement_id"] == measurement_id for m in contract["measurements"]):
            raise DomainError(f"测量标识重复：{measurement_id}")
        if weight_kg <= 0:
            raise DomainError("体重数据无效")
        return self._append(
            "MEASUREMENT_RECORDED", "service_contract", contract_id,
            {"measurement_id": measurement_id, "taken_at": taken_at, "weight_kg": weight_kg,
             "note": note}, actor, f"记录原始测量 {weight_kg}kg")

    def record_safety_incident(self, actor: dict, *, contract_id: str, incident_id: str,
                               occurred_at: str, description: str, severity: str,
                               health_attribution: bool = False) -> dict:
        """安全事件只追加，销售与门店均无法改写或删除。"""
        actor = self._require_roles(actor, {"coach", "safety_officer"})
        contract = self._contract(contract_id)
        if severity not in {"minor", "moderate", "serious", "critical"}:
            raise DomainError("严重级别无效")
        if any(i["incident_id"] == incident_id for i in contract["safety_incidents"]):
            raise DomainError(f"安全事件标识重复：{incident_id}")
        return self._append(
            "SAFETY_INCIDENT_RECORDED", "service_contract", contract_id,
            {"incident_id": incident_id, "occurred_at": occurred_at, "description": description,
             "severity": severity, "health_attribution": health_attribution},
            actor, f"登记安全事件（{severity}）")

    def record_service_session(self, actor: dict, *, contract_id: str, session_id: str,
                               scheduled_at: str, delivered: bool, note: str = "") -> dict:
        actor = self._require_roles(actor, {"coach", "store_manager"})
        contract = self._contract(contract_id)
        if any(s["session_id"] == session_id for s in contract["sessions"]):
            raise DomainError(f"服务记录标识重复：{session_id}")
        return self._append(
            "SERVICE_SESSION_RECORDED", "service_contract", contract_id,
            {"session_id": session_id, "scheduled_at": scheduled_at, "delivered": delivered,
             "note": note}, actor, "登记服务履行情况")

    def request_leave(self, actor: dict, *, contract_id: str, leave_id: str, from_: str,
                      to: str, reason: str) -> dict:
        actor = self._require_roles(actor, {"consumer", "sales", "store_manager"})
        self._contract(contract_id)
        return self._append(
            "LEAVE_REQUESTED", "service_contract", contract_id,
            {"leave_id": leave_id, "from": from_, "to": to, "reason": reason},
            actor, "登记请假")

    def request_withdrawal(self, actor: dict, *, contract_id: str, requested_at: str,
                           reason_category: str, reason_text: str = "") -> dict:
        """退营原因登记后不可修改（没有更新路径，更正只能另案说明）。"""
        actor = self._require_roles(actor, {"consumer", "sales", "store_manager"})
        contract = self._contract(contract_id)
        if reason_category not in REASON_CATEGORIES:
            raise DomainError("退营原因必须是 health/service/regret")
        if contract["withdrawal"] is not None:
            raise DomainError("退营请求已登记，原因不可改写")
        return self._append(
            "WITHDRAWAL_REQUESTED", "service_contract", contract_id,
            {"requested_at": requested_at, "reason_category": reason_category,
             "reason_text": reason_text}, actor, f"登记退营（{reason_category}）",
            occurred_at=requested_at)

    # ------------------------------------------------------------------ 案件

    def open_case(self, actor: dict, *, case_id: str | None = None, contract_id: str,
                  reason_category: str, reason_text: str = "",
                  linked_incident_id: str | None = None) -> dict:
        """登记投诉/争议。迟到的投诉同样受理：计算依据永远是签约时快照。

        命令在全局临界区内执行：并发的两个开案/和解请求只有一个能提交。
        """
        actor = self._require_roles(actor, {"dispute_specialist", "consumer"})
        contract = self._contract(contract_id)
        if reason_category not in REASON_CATEGORIES:
            raise DomainError("争议原因必须是 health/service/regret")
        if self.state.case_for_contract(contract_id) is not None:
            raise DomainError("该合同已有未结案件")
        incident = None
        if reason_category == "health":
            if not linked_incident_id:
                raise DomainError("健康原因案件必须关联安全事件")
            incident = next((i for i in contract["safety_incidents"]
                             if i["incident_id"] == linked_incident_id), None)
            if incident is None:
                raise DomainError("关联的安全事件不存在或不属于该合同")
        case_id = case_id or _new_id("case")
        if case_id in self.state.cases:
            raise DomainError(f"案件标识已存在：{case_id}")
        refund_id = f"refund-{case_id}"
        event = self._append(
            "CASE_OPENED", "dispute_case", case_id,
            {"contract_id": contract_id, "store_id": contract["store_id"],
             "consumer_id": contract["consumer_id"], "reason_category": reason_category,
             "reason_text": reason_text, "linked_incident_id": linked_incident_id,
             "refund_id": refund_id},
            actor, f"登记{reason_category}争议案件")
        return event

    def register_receipt(self, actor: dict, *, case_id: str, receipt_id: str, kind: str,
                         provider: str, fingerprint: str, submitted_by: str,
                         amount_cents: int | None = None,
                         visibility: str = "mutual") -> dict:
        """登记支付/证据回执。

        - 标识相同且金额、指纹、提供方完全一致：视为完整重传，幂等返回首条，不重复计。
        - 标识相同但任一关键字段变化：回执污染并立即锁定案件。
        """
        actor = self._require_roles(actor, {"consumer", "dispute_specialist", "sales",
                                            "store_manager", "refund_payer"})
        case = self._case(case_id)
        if kind not in {"payment", "evidence"}:
            raise DomainError("回执类型必须是 payment 或 evidence")
        if visibility not in {"mutual", "internal"}:
            raise DomainError("可见范围必须是 mutual（双方可见）或 internal")
        existing = self.state.receipts.get(receipt_id)
        if existing is not None:
            changed = []
            if existing["fingerprint"] != fingerprint:
                changed.append("fingerprint")
            if existing["provider"] != provider:
                changed.append("provider")
            if existing["amount_cents"] != amount_cents:
                changed.append("amount_cents")
            if not changed:
                # 完整重传保持一次：不产生新事件。
                return {"deduped": True, "receipt_id": receipt_id,
                        "first_event_id": existing["first_event_id"]}
            # 同标识异内容：污染回执并立即锁定案件（同一命令内落盘，
            # 故障恢复重放后两个事实同时成立）。
            self._append(
                "RECEIPT_TAINTED", "evidence_receipt", receipt_id,
                {"case_id": case_id, "changed_fields": changed,
                 "old": {"provider": existing["provider"], "fingerprint": existing["fingerprint"],
                         "amount_cents": existing["amount_cents"]},
                 "new": {"provider": provider, "fingerprint": fingerprint,
                         "amount_cents": amount_cents}},
                actor, f"回执 {receipt_id} 关键字段变化（{'、'.join(changed)}）")
            return self._append(
                "CASE_LOCKED", "dispute_case", case_id,
                {"reason": "receipt_tainted", "detail": f"{receipt_id}: {'、'.join(changed)}"},
                actor, f"回执 {receipt_id} 关键字段变化，案件立即锁定")
        data = {"case_id": case_id, "kind": kind, "provider": provider,
                "fingerprint": fingerprint, "amount_cents": amount_cents,
                "visibility": visibility, "submitted_by": submitted_by}
        return self._append("RECEIPT_REGISTERED", "evidence_receipt", receipt_id, data,
                            actor, f"登记{kind}回执 {receipt_id}")

    def hold_evidence(self, actor: dict, *, case_id: str, hold_id: str, receipt_id: str,
                      retain_until: str, reason: str = "") -> dict:
        """设定证据保全期限。保全信息只存在于事件日志，故障恢复后原样重建。"""
        actor = self._require_roles(actor, {"dispute_specialist", "safety_officer",
                                            "regulatory_officer", "auditor"})
        case = self._case(case_id)
        if receipt_id not in case["receipt_ids"]:
            raise DomainError("回执不属于该案件")
        if any(h["hold_id"] == hold_id for h in case["evidence_holds"]):
            raise DomainError(f"保全标识重复：{hold_id}")
        return self._append(
            "EVIDENCE_HELD", "dispute_case", case_id,
            {"hold_id": hold_id, "receipt_id": receipt_id, "retain_until": retain_until,
             "reason": reason}, actor, f"证据 {receipt_id} 保全至 {retain_until}")

    def lock_case(self, actor: dict, *, case_id: str, reason: str, detail: str = "") -> dict:
        actor = self._require_roles(actor, {"regulatory_officer", "safety_officer",
                                            "dispute_specialist"})
        case = self._case(case_id)
        event = self._append(
            "CASE_LOCKED", "dispute_case", case_id,
            {"reason": reason, "detail": detail}, actor, f"案件锁定：{reason}")
        return event

    def transfer_case(self, actor: dict, *, case_id: str, to_store_id: str,
                      new_contract_id: str | None = None) -> dict:
        """跨门店续营：未结争议随学员/新合同继承到新门店，结算逃不掉。"""
        actor = self._require_roles(actor, {"dispute_specialist", "store_manager"})
        case = self._case(case_id)
        if case["status"] == "settled":
            raise DomainError("已结案件无需转移")
        if new_contract_id:
            contract = self.state.contracts.get(new_contract_id)
            if contract is None:
                raise DomainError(f"新合同不存在：{new_contract_id}")
            if contract["consumer_id"] != case["consumer_id"]:
                raise DomainError("续营合同必须属于同一消费者")
            if case in [self.state.cases[c] for c in self.state.contract_cases[new_contract_id]
                        if c in self.state.cases and self.state.cases[c]["status"] != "settled"]:
                raise DomainError("该合同已继承此案件")
        return self._append(
            "CASE_TRANSFERRED", "dispute_case", case_id,
            {"to_store_id": to_store_id, "new_contract_id": new_contract_id},
            actor, f"未结争议跨店继承至 {to_store_id}")

    # ------------------------------------------------------------------ 调解

    def record_mediation(self, actor: dict, *, case_id: str, mediation_id: str,
                         outcome: str, consumer_present: bool,
                         agreed_amount_cents: int | None = None, notes: str = "",
                         expected_case_version: int | None = None) -> dict:
        actor = self._require_roles(actor, {"mediator"})
        case = self._case(case_id)
        if any(m["mediation_id"] == mediation_id for m in case["mediations"]):
            raise DomainError(f"调解记录标识重复：{mediation_id}")
        if agreed_amount_cents is not None and agreed_amount_cents < 0:
            raise DomainError("调解金额不能为负")
        return self._append(
            "MEDIATION_RECORDED", "dispute_case", case_id,
            {"mediation_id": mediation_id, "outcome": outcome,
             "consumer_present": consumer_present, "agreed_amount_cents": agreed_amount_cents,
             "notes": notes}, actor, f"记录调解结果：{outcome}",
            expected_version=expected_case_version)

    # ------------------------------------------------------------------ 退款

    def compute_refund_candidate(self, actor: dict, *, case_id: str,
                                 candidate_id: str | None = None) -> dict:
        """按签约时承诺与不可改写记录计算退款候选。重算会留下历史候选。"""
        actor = self._require_roles(actor, {"dispute_specialist", "refund_approver", "auditor"})
        case = self._case(case_id)
        contract = self.state.contracts[case["contract_ids"][0]]
        incident = None
        if case["reason_category"] == "health":
            incident = next(i for i in contract["safety_incidents"]
                            if i["incident_id"] == case["linked_incident_id"])
        candidate = compute_candidate(contract, case["reason_category"], linked_incident=incident)
        return self._append(
            "REFUND_CANDIDATE_COMPUTED", "refund_entry", case["refund_id"],
            {"candidate_id": candidate_id or _new_id("cand"),
             "reason_category": candidate.reason_category,
             "amount_cents": candidate.amount_cents, "basis": candidate.basis},
            actor, f"计算退款候选 {candidate.amount_cents // 100} 元（{candidate.reason_category}）")

    def approve_refund(self, actor: dict, *, case_id: str, approval_id: str,
                       candidate_id: str,
                       expected_refund_version: int | None = None) -> dict:
        """批准退款：只能批准系统计算出的候选金额；批准人不得是本案调解人。"""
        actor = self._require_roles(actor, {"refund_approver"})
        case = self._case(case_id)
        if case["locked"]:
            raise DomainError("案件已锁定，在锁定原因消除前不得批准退款")
        if actor["id"] in self._mediator_ids(case):
            raise PermissionDenied("退款批准人不得兼任本案调解人")
        refund = self._refund_for_case(case)
        if refund["approved"] is not None:
            raise DomainError("退款已批准")
        candidate = next((c for c in refund["candidates"] if c["candidate_id"] == candidate_id), None)
        if candidate is None:
            raise DomainError("候选计算不存在，必须先按签约时承诺计算")
        return self._append(
            "REFUND_APPROVED", "refund_entry", refund["refund_id"],
            {"approval_id": approval_id, "candidate_id": candidate_id,
             "amount_cents": candidate["amount_cents"]},
            actor, f"批准退款 {candidate['amount_cents'] // 100} 元",
            expected_version=expected_refund_version)

    def pay_refund_installment(self, actor: dict, *, case_id: str, payment_id: str,
                               amount_cents: int, receipt_id: str | None = None,
                               expected_refund_version: int | None = None) -> dict:
        """分期支付退款。崩溃恢复后 payment_id 幂等，净付不得超过批准额。"""
        actor = self._require_roles(actor, {"refund_payer"})
        case = self._case(case_id)
        if case["locked"]:
            raise DomainError("案件已锁定，停止付款")
        refund = self._refund_for_case(case)
        if refund["approved"] is None:
            raise DomainError("退款未经批准，不得支付")
        if amount_cents <= 0:
            raise DomainError("支付金额必须为正")
        if any(p["payment_id"] == payment_id for p in refund["payments"]):
            raise DomainError(f"支付标识重复，禁止重复付款：{payment_id}")
        if receipt_id and receipt_id in self.state.receipts:
            raise DomainError(f"支付回执已登记，禁止重复付款：{receipt_id}")
        approved = refund["approved"]["amount_cents"]
        if refund_net_paid_cents(refund) + amount_cents > approved:
            raise DomainError("分期付款累计净额将超过批准金额")
        return self._append(
            "REFUND_INSTALLMENT_PAID", "refund_entry", refund["refund_id"],
            {"payment_id": payment_id, "amount_cents": amount_cents, "receipt_id": receipt_id},
            actor, f"支付退款分期 {amount_cents // 100} 元",
            expected_version=expected_refund_version)

    def reverse_payment(self, actor: dict, *, case_id: str, reversal_id: str,
                        payment_id: str, amount_cents: int, reason: str = "",
                        expected_refund_version: int | None = None) -> dict:
        """冲正：只能针对已发生的支付，冲正总额不超过该笔支付。"""
        actor = self._require_roles(actor, {"refund_payer"})
        case = self._case(case_id)
        refund = self._refund_for_case(case)
        payment = next((p for p in refund["payments"] if p["payment_id"] == payment_id), None)
        if payment is None:
            raise DomainError("被冲正的支付不存在")
        if amount_cents <= 0:
            raise DomainError("冲正金额必须为正")
        already = sum(r["amount_cents"] for r in refund["reversals"]
                      if r["payment_id"] == payment_id)
        if already + amount_cents > payment["amount_cents"]:
            raise DomainError("冲正总额不能超过原支付金额")
        return self._append(
            "REFUND_PAYMENT_REVERSED", "refund_entry", refund["refund_id"],
            {"reversal_id": reversal_id, "payment_id": payment_id,
             "amount_cents": amount_cents, "reason": reason},
            actor, f"冲正支付 {payment_id} {amount_cents // 100} 元",
            expected_version=expected_refund_version)

    def pay_topup(self, actor: dict, *, case_id: str, topup_id: str, amount_cents: int,
                  reversal_id: str | None = None,
                  expected_refund_version: int | None = None) -> dict:
        """补付：冲正后重新付出，净付仍不得超过批准金额。"""
        actor = self._require_roles(actor, {"refund_payer"})
        case = self._case(case_id)
        if case["locked"]:
            raise DomainError("案件已锁定，停止付款")
        refund = self._refund_for_case(case)
        if refund["approved"] is None:
            raise DomainError("退款未经批准，不得补付")
        if amount_cents <= 0:
            raise DomainError("补付金额必须为正")
        if any(t["topup_id"] == topup_id for t in refund["topups"]):
            raise DomainError(f"补付标识重复：{topup_id}")
        if reversal_id and not any(r["reversal_id"] == reversal_id for r in refund["reversals"]):
            raise DomainError("补付必须对应一笔已登记的冲正")
        if refund_net_paid_cents(refund) + amount_cents > refund["approved"]["amount_cents"]:
            raise DomainError("补付后累计净额将超过批准金额")
        return self._append(
            "REFUND_TOPUP_PAID", "refund_entry", refund["refund_id"],
            {"topup_id": topup_id, "reversal_id": reversal_id, "amount_cents": amount_cents},
            actor, f"补付退款 {amount_cents // 100} 元",
            expected_version=expected_refund_version)

    def close_settlement(self, actor: dict, *, case_id: str,
                         expected_refund_version: int | None = None) -> dict:
        """关闭结算的硬条件：净额等于批准额、案件未锁定、没有进行中的监管调查。

        已付款和解只能通过冲正/补付调到恰好批准额，不能借结算掩盖仍在调查的事实。
        """
        actor = self._require_roles(actor, {"settlement_officer"})
        case = self._case(case_id)
        refund = self._refund_for_case(case)
        if refund["settlement"] is not None or case["status"] == "settled":
            raise DomainError("案件已结算")
        if refund["approved"] is None:
            raise DomainError("退款未批准，不能结算")
        net = refund_net_paid_cents(refund)
        if net != refund["approved"]["amount_cents"]:
            raise DomainError(
                f"净付 {net // 100} 元与批准 {refund['approved']['amount_cents'] // 100} 元不符，"
                "请通过冲正或补付调整后再结算")
        if case["locked"]:
            raise DomainError("案件处于锁定状态，不能结算")
        open_inv = self._open_investigations(case)
        if open_inv:
            raise DomainError(
                f"监管调查 {open_inv[0]['investigation_id']} 仍在进行，不能借结算关闭事实")
        return self._append(
            "SETTLEMENT_CLOSED", "refund_entry", refund["refund_id"], {},
            actor, "净额核对一致且无未结调查，结算关闭",
            expected_version=expected_refund_version)

    # ------------------------------------------------------------------ 监管

    def open_investigation(self, actor: dict, *, case_id: str, investigation_id: str,
                           authority: str = "") -> dict:
        actor = self._require_roles(actor, {"regulatory_officer"})
        case = self._case(case_id)
        if any(i["investigation_id"] == investigation_id for i in case["investigations"]):
            raise DomainError("调查标识重复")
        return self._append(
            "REGULATORY_INVESTIGATION_OPENED", "dispute_case", case_id,
            {"investigation_id": investigation_id, "authority": authority},
            actor, f"监管立案 {investigation_id}")

    def file_regulatory_response(self, actor: dict, *, case_id: str, investigation_id: str,
                                 response_id: str, content_ref: str) -> dict:
        """监管答复只能由监管角色提交，且不得是本案调解人或退款批准人。"""
        actor = self._require_roles(actor, {"regulatory_officer"})
        case = self._case(case_id)
        inv = next((i for i in case["investigations"]
                    if i["investigation_id"] == investigation_id), None)
        if inv is None or inv["status"] != "open":
            raise DomainError("调查不存在或已关闭")
        if actor["id"] in self._mediator_ids(case):
            raise PermissionDenied("监管答复人不得兼任本案调解人")
        refund = self._refund_for_case(case)
        if refund["approved"] and refund["approved"]["approver_id"] == actor["id"]:
            raise PermissionDenied("监管答复人不得兼任本案退款批准人")
        if any(r["response_id"] == response_id for r in inv["responses"]):
            raise DomainError("答复标识重复")
        return self._append(
            "REGULATORY_RESPONSE_FILED", "dispute_case", case_id,
            {"investigation_id": investigation_id, "response_id": response_id,
             "content_ref": content_ref}, actor, f"提交监管答复 {response_id}")

    def close_investigation(self, actor: dict, *, case_id: str, investigation_id: str) -> dict:
        actor = self._require_roles(actor, {"regulatory_officer"})
        case = self._case(case_id)
        inv = next((i for i in case["investigations"]
                    if i["investigation_id"] == investigation_id), None)
        if inv is None or inv["status"] != "open":
            raise DomainError("调查不存在或已关闭")
        return self._append(
            "REGULATORY_INVESTIGATION_CLOSED", "dispute_case", case_id,
            {"investigation_id": investigation_id}, actor, f"监管调查结案 {investigation_id}")

    # ------------------------------------------------------------------ 核对

    def run_monthly_reconciliation(self, actor: dict, *, report_id: str, month: str) -> dict:
        """月度核对：承诺兑现、退款台账、未结调查、锁定案件、证据保全到期。"""
        actor = self._require_roles(actor, {"auditor"})
        findings = self.reconciliation_findings(month)
        return self._append(
            "MONTHLY_RECONCILIATION_RUN", "audit_report", report_id,
            {"month": month, **findings}, actor, f"{month} 月度承诺/退款/调查核对")

    def reconciliation_findings(self, month: str) -> dict:
        promise_fulfillment = []
        for contract in self.state.contracts.values():
            snapshot = contract["promise_snapshot"]
            if "不瘦退款" in snapshot.get("claims", []) or snapshot.get("target_weight_kg") is not None:
                target = snapshot.get("target_weight_kg")
                latest = contract["measurements"][-1] if contract["measurements"] else None
                promise_fulfillment.append({
                    "contract_id": contract["contract_id"],
                    "promise_id": snapshot["promise_id"],
                    "target_weight_kg": target,
                    "latest_weight_kg": latest["weight_kg"] if latest else None,
                    "unmet": bool(latest and target is not None and latest["weight_kg"] > target),
                })
        refund_rows = []
        overpaid = []
        for refund in self.state.refunds.values():
            net = refund_net_paid_cents(refund)
            approved = refund["approved"]["amount_cents"] if refund["approved"] else None
            row = {"refund_id": refund["refund_id"], "case_id": refund["case_id"],
                   "approved_cents": approved, "net_paid_cents": net,
                   "settled": refund["settlement"] is not None,
                   "payment_count": len(refund["payments"]),
                   "reversal_count": len(refund["reversals"]),
                   "topup_count": len(refund["topups"])}
            refund_rows.append(row)
            if approved is not None and net > approved:
                overpaid.append(row)
        open_investigations = [
            {"case_id": cid,
             "investigation_ids": [i["investigation_id"] for i in c["investigations"]
                                   if i["status"] == "open"]}
            for cid, c in self.state.cases.items() if self._open_investigations(c)]
        locked_cases = [cid for cid, c in self.state.cases.items() if c["locked"]]
        return {
            "promise_fulfillment": promise_fulfillment,
            "refunds": refund_rows,
            "overpaid_refunds": overpaid,
            "open_investigations": open_investigations,
            "locked_cases": locked_cases,
            "expiring_evidence_holds": self.expiring_holds(month_end=month),
        }

    def expiring_holds(self, *, month_end: str | None = None) -> list[dict]:
        """保全期限落在给定月末之前（含）的保全，防止恢复后丢失保全期限。"""
        rows = []
        for case in self.state.cases.values():
            for hold in case["evidence_holds"]:
                if month_end is None or parse_ts(hold["retain_until"]) <= _month_end(month_end):
                    rows.append({"case_id": case["case_id"], **hold})
        return rows

    # ------------------------------------------------------------------ 视图

    def materials(self, case_id: str, actor: dict) -> list[dict]:
        """按角色与可见范围调取材料：争议专员只能拿到双方可见材料。"""
        actor = self._actor(actor)
        case = self._case(case_id)
        role = actor["role"]
        receipts = [self.state.receipts[rid] for rid in case["receipt_ids"]]
        if role == "regulatory_officer" or role == "auditor":
            return [dict(r) for r in receipts]
        if role == "dispute_specialist":
            return [dict(r) for r in receipts if r["visibility"] == "mutual"]
        if role == "consumer":
            if actor["id"] != case["consumer_id"]:
                raise PermissionDenied("只能查看本人案件材料")
            return [dict(r) for r in receipts
                    if r["visibility"] == "mutual" or r["submitted_by"] == actor["id"]]
        # 门店角色：只能看本店且双方可见
        staff = self.state.staff.get(actor["id"])
        if staff and case["current_store_id"] == staff["store_id"]:
            return [dict(r) for r in receipts if r["visibility"] == "mutual"]
        raise PermissionDenied("无权调取该案件材料")

    def consumer_view(self, consumer_id: str) -> dict:
        """消费者 API：采用的合同（含冻结承诺）与每笔退款计算及依据。"""
        contracts = []
        for cid, contract in self.state.contracts.items():
            if contract["consumer_id"] != consumer_id:
                continue
            case_ids = [c for c in self.state.contract_cases.get(cid, [])]
            cases_view = []
            for case_id in case_ids:
                case = self.state.cases[case_id]
                refund = self.state.refunds[case["refund_id"]]
                cases_view.append({
                    "case_id": case_id, "reason_category": case["reason_category"],
                    "status": case["status"], "locked": case["locked"],
                    "store_id": case["current_store_id"],
                    "refund_candidates": [dict(c) for c in refund["candidates"]],
                    "approved": refund["approved"],
                    "net_paid_cents": refund_net_paid_cents(refund),
                    "open_investigations": [i["investigation_id"]
                                            for i in case["investigations"] if i["status"] == "open"],
                })
            contracts.append({
                "contract_id": cid, "signed_at": contract["signed_at"],
                "store_id": contract["store_id"],
                "adopted_promise": contract["promise_snapshot"],
                "paid_total_cents": contract["paid_total_cents"],
                "cases": cases_view,
            })
        return {"consumer_id": consumer_id, "contracts": contracts}

    def store_todo(self, actor: dict) -> list[dict]:
        """门店视角：只能看到当前归属本店的未结案件，看不到证据原文范围外内容。"""
        actor = self._require_roles(actor, {"sales", "store_manager", "coach", "safety_officer"})
        staff = self.state.staff[actor["id"]]
        todo = []
        for case in self.state.cases.values():
            if case["current_store_id"] != staff["store_id"] or case["status"] == "settled":
                continue
            todo.append({
                "case_id": case["case_id"], "reason_category": case["reason_category"],
                "locked": case["locked"], "opened_at": case["opened_at"],
                "open_investigations": len(self._open_investigations(case)),
                "material_count_mutual": sum(
                    1 for rid in case["receipt_ids"]
                    if self.state.receipts[rid]["visibility"] == "mutual"),
            })
        return todo


def _month_end(month: str) -> datetime:
    tz = timezone(timedelta(hours=8))  # 月度口径以门店所在 +08:00 为准
    year, m = map(int, month.split("-"))
    if m == 12:
        return datetime(year + 1, 1, 1, tzinfo=tz)
    return datetime(year, m + 1, 1, tzinfo=tz)


# 每个公开命令都串行执行：进入时重放日志以包含其它进程已落盘的事实，
# 退出时锁自动释放。这样两个会话并发和解时，只有一个能提交；故障恢复后
# 重建出的状态与日志一致，支付幂等标识拦截重复付款。
_MUTATING_OR_QUERY = {
    name for name in vars(CampDomain)
    if not name.startswith("_") and callable(getattr(CampDomain, name))
}


def _guarded(method):
    def wrapper(self, *args, **kwargs):
        with self.store.exclusive():
            self.store.resync_if_changed()
            self.state = load_state(self.store)
            return method(self, *args, **kwargs)

    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


for _name in _MUTATING_OR_QUERY:
    setattr(CampDomain, _name, _guarded(getattr(CampDomain, _name)))
