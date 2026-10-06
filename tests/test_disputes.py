"""争议案件：证据回执、保全、可见范围、迟到投诉、跨店继承、三权分立。"""

from __future__ import annotations

import tempfile
import unittest

from src.store import DomainError, PermissionDenied
from tests.scenario import ADMIN, build_domain, role, sign_standard_contract


class DisputeFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dom = build_domain(self.tmp.name)
        sign_standard_contract(self.dom)
        self.dom.receive_installment(role("sal"), contract_id="c1", seq=1, amount_cents=100_000,
                                     paid_at="2026-03-01T10:00:00+08:00")
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- 回执完整重传保持一次 ------------------------------------------

    def test_identical_retransmission_counts_once(self) -> None:
        first = self.dom.register_receipt(
            role("sal"), case_id="case-1", receipt_id="rcpt-1", kind="payment",
            provider="wechatpay", fingerprint="sha256:f1", submitted_by="consumer-1",
            amount_cents=100_000)
        self.assertNotIn("deduped", first)
        second = self.dom.register_receipt(
            role("sal"), case_id="case-1", receipt_id="rcpt-1", kind="payment",
            provider="wechatpay", fingerprint="sha256:f1", submitted_by="consumer-1",
            amount_cents=100_000)
        self.assertTrue(second["deduped"])
        case = self.dom.state.cases["case-1"]
        self.assertEqual(case["receipt_ids"].count("rcpt-1"), 1)
        self.assertFalse(self.dom.state.receipts["rcpt-1"]["tainted"])

    # ---- 标识相同而金额/指纹/提供方变化：立即锁定 -----------------------

    def test_same_id_changed_fingerprint_locks_case(self) -> None:
        self.dom.register_receipt(
            role("sal"), case_id="case-1", receipt_id="rcpt-1", kind="payment",
            provider="wechatpay", fingerprint="sha256:f1", submitted_by="consumer-1",
            amount_cents=100_000)
        self.dom.register_receipt(
            role("sal"), case_id="case-1", receipt_id="rcpt-1", kind="payment",
            provider="wechatpay", fingerprint="sha256:TAMPERED", submitted_by="consumer-1",
            amount_cents=100_000)
        self.assertTrue(self.dom.state.receipts["rcpt-1"]["tainted"])
        self.assertTrue(self.dom.state.cases["case-1"]["locked"])
        # 锁定后退款批准与支付全部停止
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        with self.assertRaises(DomainError):
            self.dom.approve_refund(role("appr"), case_id="case-1", approval_id="ap",
                                    candidate_id="cand-1")

    def test_same_id_changed_amount_locks_case(self) -> None:
        self.dom.register_receipt(
            role("sal"), case_id="case-1", receipt_id="rcpt-9", kind="payment",
            provider="alipay", fingerprint="sha256:f9", submitted_by="consumer-1",
            amount_cents=100_000)
        self.dom.register_receipt(
            role("sal"), case_id="case-1", receipt_id="rcpt-9", kind="payment",
            provider="alipay", fingerprint="sha256:f9", submitted_by="consumer-1",
            amount_cents=90_000)
        self.assertTrue(self.dom.state.cases["case-1"]["locked"])

    # ---- 争议专员只在双方可见范围内调取材料 -----------------------------

    def test_specialist_only_sees_mutual_materials(self) -> None:
        self.dom.register_receipt(
            role("spec"), case_id="case-1", receipt_id="ev-open", kind="evidence",
            provider="consumer", fingerprint="sha256:e1", submitted_by="consumer-1",
            visibility="mutual")
        self.dom.register_receipt(
            role("mgr"), case_id="case-1", receipt_id="ev-secret", kind="evidence",
            provider="store", fingerprint="sha256:e2", submitted_by="store-1",
            visibility="internal")
        seen = self.dom.materials("case-1", role("spec"))
        self.assertEqual({r["receipt_id"] for r in seen}, {"ev-open"})
        # 监管可以看全部
        all_seen = self.dom.materials("case-1", role("reg"))
        self.assertEqual({r["receipt_id"] for r in all_seen}, {"ev-open", "ev-secret"})
        # 无关节店角色看不到
        with self.assertRaises(PermissionDenied):
            self.dom.materials("case-1", role("mgr2"))

    # ---- 指定场景：迟到投诉 --------------------------------------------

    def test_late_complaint_still_accepted_using_frozen_snapshot(self) -> None:
        # 独立域：签约一年后才投诉，承诺中途撤回也不影响受理与计算依据
        dom = build_domain(self.tmp.name + "/late")
        sign_standard_contract(dom, contract_id="late-c")
        dom.receive_installment(role("sal"), contract_id="late-c", seq=1, amount_cents=100_000,
                                paid_at="2026-03-01T10:00:00+08:00")
        dom.retire_promise(role("mkt"), "promise-ad")
        for k in range(10):
            dom.record_service_session(
                role("coach"), contract_id="late-c", session_id=f"s{k}",
                scheduled_at="2026-03-01T10:00:00+08:00", delivered=False)
        case = dom.open_case(
            role("spec"), contract_id="late-c", reason_category="service", case_id="late-1")
        dom.compute_refund_candidate(role("spec"), case_id="late-1", candidate_id="cand-late")
        candidate = dom.state.refunds[case["data"]["refund_id"]]["candidates"][-1]
        # 一次未交付：未履行比例 100%
        self.assertEqual(candidate["amount_cents"], 100_000)
        self.assertEqual(
            dom.state.contracts["late-c"]["promise_snapshot"]["content_hash"], "sha256:ad001")

    # ---- 跨门店续营继承未结争议 ----------------------------------------

    def test_cross_store_continuation_inherits_open_case(self) -> None:
        # 在二店续营签新合同
        sign_standard_contract(self.dom, contract_id="c2", sales="sal2", store="store-2",
                               signed_at="2026-05-01T10:00:00+08:00")
        self.dom.transfer_case(role("spec"), case_id="case-1", to_store_id="store-2",
                               new_contract_id="c2")
        case = self.dom.state.cases["case-1"]
        self.assertEqual(case["current_store_id"], "store-2")
        self.assertIn("c2", case["contract_ids"])
        # 新合同带出同一未结案件
        self.assertEqual(self.dom.state.case_for_contract("c2")["case_id"], "case-1")
        # 二店待办里出现该案件，一店不再看到
        todo2 = self.dom.store_todo(role("mgr2"))
        self.assertIn("case-1", [t["case_id"] for t in todo2])
        todo1 = self.dom.store_todo(role("mgr"))
        self.assertNotIn("case-1", [t["case_id"] for t in todo1])

    def test_transfer_requires_same_consumer(self) -> None:
        sign_standard_contract(self.dom, contract_id="c-other", consumer="consumer-2",
                               sales="sal2", store="store-2")
        with self.assertRaises(DomainError):
            self.dom.transfer_case(role("spec"), case_id="case-1", to_store_id="store-2",
                                   new_contract_id="c-other")

    # ---- 证据保全期限在恢复后仍然存在 ----------------------------------

    def test_evidence_hold_and_reconciliation(self) -> None:
        self.dom.register_receipt(
            role("sal"), case_id="case-1", receipt_id="rcpt-1", kind="evidence",
            provider="consumer", fingerprint="sha256:f1", submitted_by="consumer-1")
        self.dom.hold_evidence(role("spec"), case_id="case-1", hold_id="hold-1",
                               receipt_id="rcpt-1", retain_until="2026-10-31T23:59:59+08:00",
                               reason="投诉调查取证")
        self.dom.run_monthly_reconciliation(role("auditor"), report_id="rep-2026-10",
                                            month="2026-10")
        report = self.dom.state.reports["rep-2026-10"]
        holds = report["expiring_evidence_holds"]
        self.assertTrue(any(h["hold_id"] == "hold-1" for h in holds))

    # ---- 监管答复与调解/批准角色隔离 -----------------------------------

    def test_regulatory_responder_separation_of_duties(self) -> None:
        self.dom.record_mediation(role("med"), case_id="case-1", mediation_id="med-1",
                                  outcome="partial", consumer_present=True)
        self.dom.open_investigation(role("reg"), case_id="case-1", investigation_id="inv-1")
        # 调解人不能提交监管答复
        with self.assertRaises(PermissionDenied):
            self.dom.file_regulatory_response(role("med"), case_id="case-1",
                                              investigation_id="inv-1", response_id="r1",
                                              content_ref="x")
        # 监管专员本人可以
        self.dom.file_regulatory_response(role("reg"), case_id="case-1",
                                          investigation_id="inv-1", response_id="r1",
                                          content_ref="letters/r1.pdf")

    def test_regulatory_responder_cannot_be_approver(self) -> None:
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="c1")
        self.dom.approve_refund(role("appr"), case_id="case-1", approval_id="ap1",
                                candidate_id="c1")
        self.dom.open_investigation(role("reg"), case_id="case-1", investigation_id="inv-1")
        with self.assertRaises(PermissionDenied):
            self.dom.file_regulatory_response(role("appr"), case_id="case-1",
                                              investigation_id="inv-1", response_id="r1",
                                              content_ref="x")

    # ---- 退营原因不可改写 ----------------------------------------------

    def test_withdrawal_reason_is_immutable(self) -> None:
        self.dom.request_withdrawal(role("sal"), contract_id="c1",
                                    requested_at="2026-04-01T09:00:00+08:00",
                                    reason_category="regret", reason_text="不想练了")
        with self.assertRaises(DomainError):
            self.dom.request_withdrawal(role("sal"), contract_id="c1",
                                        requested_at="2026-04-02T09:00:00+08:00",
                                        reason_category="health", reason_text="改成健康原因")


if __name__ == "__main__":
    unittest.main()
