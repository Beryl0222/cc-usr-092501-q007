"""退款候选规则、分期支付、冲正/补付与结算约束。"""

from __future__ import annotations

import tempfile
import unittest

from src.state import refund_net_paid_cents
from src.store import DomainError, PermissionDenied
from tests.scenario import build_domain, role, sign_standard_contract


def _pay_and_service(dom, *, delivered=4, planned=10, price=100_000, payments=(50_000, 50_000)):
    sign_standard_contract(dom, planned_sessions=planned, price_cents=price)
    for seq, amount in enumerate(payments, 1):
        dom.receive_installment(role("sal"), contract_id="c1", seq=seq, amount_cents=amount,
                                paid_at=f"2026-03-0{seq}T10:00:00+08:00",
                                receipt_id=f"in-receipt-{seq}")
    for k in range(planned):
        dom.record_service_session(
            role("coach"), contract_id="c1", session_id=f"s{k}",
            scheduled_at=f"2026-03-{k + 1:02d}T10:00:00+08:00", delivered=k < delivered)
    return "c1"


class RefundRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dom = build_domain(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_health_reason_with_attributed_incident_is_full_refund(self) -> None:
        _pay_and_service(self.dom, delivered=8)
        self.dom.record_safety_incident(role("safety"), contract_id="c1", incident_id="i1",
                                        occurred_at="2026-03-20T09:00:00+08:00",
                                        description="训练诱发横纹肌溶解", severity="serious",
                                        health_attribution=True)
        self.dom.request_withdrawal(role("sal"), contract_id="c1",
                                    requested_at="2026-03-21T09:00:00+08:00",
                                    reason_category="health")
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="health",
                           linked_incident_id="i1", case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        candidate = self.dom.state.refunds["refund-case-1"]["candidates"][-1]
        self.assertEqual(candidate["amount_cents"], 100_000)  # 已付全额
        self.assertEqual(candidate["basis"]["rule"], "health_full_or_proportional")

    def test_service_unfulfilled_is_proportional(self) -> None:
        # 交付 4/10，未履行 60%
        _pay_and_service(self.dom, delivered=4)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        candidate = self.dom.state.refunds["refund-case-1"]["candidates"][-1]
        self.assertEqual(candidate["amount_cents"], 60_000)

    def test_result_claim_unmet_means_full_refund_for_service_case(self) -> None:
        # 交付了全部 10 次，但"不瘦退款"目标未达成（最新测量仍高于目标 60kg）
        _pay_and_service(self.dom, delivered=10)
        self.dom.record_measurement(role("coach"), contract_id="c1", measurement_id="m1",
                                    taken_at="2026-03-02T09:00:00+08:00", weight_kg=85.0)
        self.dom.record_measurement(role("coach"), contract_id="c1", measurement_id="m2",
                                    taken_at="2026-04-20T09:00:00+08:00", weight_kg=72.0)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        self.assertEqual(self.dom.state.refunds["refund-case-1"]["candidates"][-1]["amount_cents"],
                         100_000)

    def test_regret_uses_schedule_frozen_at_signing(self) -> None:
        # 7 天冷静期内反悔：阶梯表 100% 退
        _pay_and_service(self.dom, delivered=9)
        self.dom.request_withdrawal(role("sal"), contract_id="c1",
                                    requested_at="2026-03-05T10:00:00+08:00",
                                    reason_category="regret")
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="regret",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        self.assertEqual(self.dom.state.refunds["refund-case-1"]["candidates"][-1]["amount_cents"],
                         100_000)

    def test_regret_after_schedule_falls_back_to_consumed_rate(self) -> None:
        # 超过 30 天：无阶梯命中，已交付 9/10，无手续费口径（script 承诺没给 schedule）
        sign_standard_contract(self.dom, promise_id="promise-script")
        self.dom.receive_installment(role("sal"), contract_id="c1", seq=1, amount_cents=100_000,
                                     paid_at="2026-03-01T10:00:00+08:00")
        for k in range(10):
            self.dom.record_service_session(
                role("coach"), contract_id="c1", session_id=f"s{k}",
                scheduled_at=f"2026-03-{k + 1:02d}T10:00:00+08:00", delivered=k < 9)
        self.dom.request_withdrawal(role("sal"), contract_id="c1",
                                    requested_at="2026-05-01T10:00:00+08:00",
                                    reason_category="regret")
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="regret",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        # 未消费 10%，手续费 10%：100000 * 0.1 * 0.9 = 9000
        self.assertEqual(self.dom.state.refunds["refund-case-1"]["candidates"][-1]["amount_cents"],
                         9_000)

    def test_health_case_requires_linked_incident(self) -> None:
        _pay_and_service(self.dom)
        with self.assertRaises(DomainError):
            self.dom.open_case(role("spec"), contract_id="c1", reason_category="health",
                               case_id="case-x")

    # ---- 指定场景：分期退款 + 冲正/补付 -------------------------------

    def test_installment_refund_and_reversal_topup(self) -> None:
        _pay_and_service(self.dom, delivered=4)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        self.dom.approve_refund(role("appr"), case_id="case-1", approval_id="ap-1",
                                candidate_id="cand-1")
        # 分两期退 30000 + 30000
        self.dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-1",
                                        amount_cents=30_000)
        self.dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-2",
                                        amount_cents=30_000)
        refund = self.dom.state.refunds["refund-case-1"]
        self.assertEqual(refund_net_paid_cents(refund), 60_000)

        # 累计净额不得超过批准额
        with self.assertRaises(DomainError):
            self.dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-3",
                                            amount_cents=1_000)
        # 同一支付标识不得重复付款
        with self.assertRaises(DomainError):
            self.dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-1",
                                            amount_cents=30_000)

        # 误付冲正，再补付到批准额
        self.dom.reverse_payment(role("payer"), case_id="case-1", reversal_id="rev-1",
                                 payment_id="pay-2", amount_cents=30_000, reason="账号错误")
        self.assertEqual(refund_net_paid_cents(self.dom.state.refunds["refund-case-1"]), 30_000)
        # 冲正不能超过原支付
        with self.assertRaises(DomainError):
            self.dom.reverse_payment(role("payer"), case_id="case-1", reversal_id="rev-2",
                                     payment_id="pay-2", amount_cents=100)
        self.dom.pay_topup(role("payer"), case_id="case-1", topup_id="top-1",
                           amount_cents=30_000, reversal_id="rev-1")
        self.assertEqual(refund_net_paid_cents(self.dom.state.refunds["refund-case-1"]), 60_000)
        # 补付也不能突破批准额
        with self.assertRaises(DomainError):
            self.dom.pay_topup(role("payer"), case_id="case-1", topup_id="top-2",
                               amount_cents=100)

    def test_cannot_pay_before_approval(self) -> None:
        _pay_and_service(self.dom, delivered=4)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        with self.assertRaises(DomainError):
            self.dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="p",
                                            amount_cents=100)

    def test_approver_cannot_be_mediator(self) -> None:
        _pay_and_service(self.dom)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        self.dom.record_mediation(role("med"), case_id="case-1", mediation_id="med-1",
                                  outcome="partial", consumer_present=True,
                                  agreed_amount_cents=10_000)
        # 调解人自己批准退款：拒绝
        with self.assertRaises(PermissionDenied):
            self.dom.approve_refund(role("med"), case_id="case-1", approval_id="ap",
                                    candidate_id="cand-1")

    def test_settlement_blocked_by_open_investigation_and_mismatch(self) -> None:
        _pay_and_service(self.dom, delivered=4)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        self.dom.approve_refund(role("appr"), case_id="case-1", approval_id="ap-1",
                                candidate_id="cand-1")
        self.dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-1",
                                        amount_cents=60_000)
        # 监管立案中：结算被拒，不能借结算掩盖仍在调查的事实
        self.dom.open_investigation(role("reg"), case_id="case-1", investigation_id="inv-1")
        with self.assertRaises(DomainError):
            self.dom.close_settlement(role("settler"), case_id="case-1")
        # 调查结案后净额一致才能结算
        self.dom.file_regulatory_response(role("reg"), case_id="case-1",
                                          investigation_id="inv-1", response_id="resp-1",
                                          content_ref="letters/resp-1.pdf")
        self.dom.close_investigation(role("reg"), case_id="case-1", investigation_id="inv-1")
        self.dom.close_settlement(role("settler"), case_id="case-1")
        self.assertEqual(self.dom.state.cases["case-1"]["status"], "settled")

    def test_settlement_requires_exact_net_amount(self) -> None:
        _pay_and_service(self.dom, delivered=4)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        self.dom.approve_refund(role("appr"), case_id="case-1", approval_id="ap-1",
                                candidate_id="cand-1")
        # 只付了一部分就想结算：拒绝，必须冲正/补付调到恰好
        self.dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-1",
                                        amount_cents=40_000)
        with self.assertRaises(DomainError):
            self.dom.close_settlement(role("settler"), case_id="case-1")


if __name__ == "__main__":
    unittest.main()
