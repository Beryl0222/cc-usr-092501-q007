"""规定场景：销售离职、分期退款、迟到投诉、并发和解。"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
import tempfile

from src.journal import EventJournal
from src.readmodel import monthly_reconciliation, store_todo
from src.services import (
    Actor,
    Backend,
    CaseStateError,
    ConcurrentModificationError,
    ImmutableRecordError,
    RoleError,
)

from tests.helpers import (
    APPROVER,
    APPROVER_2,
    COACH,
    REGULATOR,
    SALES,
    SPECIALIST,
    SPECIALIST_2,
    make_backend,
    mark_delivered,
    pay_standard,
    publish_standard_promise,
    register_people,
    sign_standard_contract,
    standard_terms,
)


class SalesDepartureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_frozen_facts_survive_sales_leaving(self) -> None:
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        # 承诺版本改版必须使用新标识，不能覆盖（离职检查之前先确认）
        with self.assertRaises(ImmutableRecordError):
            publish_standard_promise(self.b)

        # 销售离职
        self.b.mark_staff_departed(person_id="S-001", at="2026-09-12T09:00:00+08:00")

        # 离职销售本人不能再做任何业绩操作
        with self.assertRaises(RoleError):
            self.b.record_payment(
                actor=SALES, contract_id="K-001", payment_id="PAY-3", amount_cents=1000,
                paid_at="2026-09-12T10:00:00+08:00", provider="wechatpay",
                receipt_fingerprint="fp-PAY-3",
            )

        # 其冻结的合同仍然是退款计算依据；新人接手不改变签约事实
        new_sales = Actor("S-002", "sales", "STORE-1")
        self.b.register_staff(person_id="S-002", role="sales", store_id="STORE-1")
        self.b.add_staff_note(
            actor=new_sales, subject_type="contract", subject_id="K-001",
            note="原销售离职，合同与口头承诺以冻结版本为准", at="2026-09-12T11:00:00+08:00",
        )
        self.b.open_dispute(
            case_id="CASE-1", consumer_id="CONSUMER-1", store_id="STORE-1",
            contract_ids=["K-001"], reason="regret",
            claimed_at="2026-09-08T10:00:00+08:00", summary="冷静期内退营",
        )
        cand = self.b.calculate_candidate(case_id="CASE-1")
        # 已付 100000，未消费课时，扣除管理费 5000
        self.assertEqual(cand["amount_cents"], 95000)
        contract = self.b.contracts["K-001"]
        self.assertEqual(contract["sales_person_id"], "S-001")
        self.assertEqual(contract["signed_by"], "S-001")


class InstallmentRefundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        self.b.open_dispute(
            case_id="CASE-1", consumer_id="CONSUMER-1", store_id="STORE-1",
            contract_ids=["K-001"], reason="regret",
            claimed_at="2026-09-05T10:00:00+08:00", summary="冷静期反悔",
        )
        self.cand = self.b.calculate_candidate(case_id="CASE-1")
        self.assertEqual(self.cand["amount_cents"], 95000)
        self.b.add_mediation_note(actor=SPECIALIST, case_id="CASE-1", note="双方认可候选金额")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_installments_crash_recovery_reversal_topup(self) -> None:
        v = self.b.cases["CASE-1"]["version"]
        self.b.approve_refund(
            actor=APPROVER, case_id="CASE-1", candidate_id=self.cand["candidate_id"],
            refund_id="R-1", agreement_fingerprint="agr-1", agreement_source="settlement/v1.pdf",
            expected_version=v,
        )

        # 首期 50000
        self.b.settle_installment(
            actor=APPROVER, refund_id="R-1", installment_id="INST-1", amount_cents=50000,
            paid_at="2026-09-09T12:00:00+08:00", provider="bank", receipt_fingerprint="rcpt-1",
        )

        # 故障恢复：用同一日志重建，首期不丢；重发首期不得重复付款
        recovered = Backend(EventJournal(str(Path(self.tmp.name) / "journal.jsonl")))
        recovered.journal.verify_chain()
        retry = recovered.settle_installment(
            actor=APPROVER, refund_id="R-1", installment_id="INST-1", amount_cents=50000,
            paid_at="2026-09-09T12:00:00+08:00", provider="bank", receipt_fingerprint="rcpt-1",
        )
        self.assertEqual(retry["event_id"], "REFUND_SETTLED:R-1:v2")
        pay_events = [e for e in recovered.journal.events() if e.body["event_type"] == "REFUND_SETTLED"]
        self.assertEqual(len(pay_events), 1)

        # 二期误付 50000（多付 5000），再冲正、补付到候选额
        recovered.settle_installment(
            actor=APPROVER, refund_id="R-1", installment_id="INST-2", amount_cents=50000,
            paid_at="2026-09-10T12:00:00+08:00", provider="bank", receipt_fingerprint="rcpt-2",
        )
        refund = recovered.refunds["R-1"]
        self.assertEqual(refund["settled_cents"], 100000)

        recovered.reverse_settlement(
            actor=APPROVER, refund_id="R-1", installment_id="INST-2", amount_cents=5000,
            reason="多付冲正", at="2026-09-11T12:00:00+08:00",
        )
        self.assertEqual(recovered.refunds["R-1"]["settled_cents"], 95000)

        # 再次恢复，状态一致
        recovered2 = Backend(EventJournal(str(Path(self.tmp.name) / "journal.jsonl")))
        self.assertEqual(recovered2.refunds["R-1"]["settled_cents"], 95000)
        self.assertEqual(recovered2.refunds["R-1"]["status"], "settled")

        report = monthly_reconciliation(recovered2, "2026-09")
        imbalance = [f for f in report["findings"] if f["code"] == "REFUND_NOT_BALANCED"]
        self.assertEqual(imbalance, [])


class LateComplaintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_late_regret_is_zero_but_servicedefect_still_pays(self) -> None:
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        # 只交付了 2 节
        mark_delivered(self.b, "K-001", [
            ("S01", "2026-09-04T18:00:00+08:00"),
            ("S02", "2026-09-05T18:00:00+08:00"),
        ])

        # 迟到投诉：一个月后才来，普通反悔 → 候选 0，但案件照常受理
        self.b.open_dispute(
            case_id="CASE-LATE", consumer_id="CONSUMER-1", store_id="STORE-1",
            contract_ids=["K-001"], reason="regret",
            claimed_at="2026-10-20T10:00:00+08:00", summary="一个月后反悔",
        )
        cand = self.b.calculate_candidate(case_id="CASE-LATE")
        self.assertEqual(cand["amount_cents"], 0)
        self.assertEqual(cand["lines"][0]["code"], "NO_COOLING_OFF_REFUND")

        # 同一争议按服务未履行重算：到期未交付课时仍应退
        cand2 = self.b.calculate_candidate(case_id="CASE-LATE", reason="service_unperformed")
        # 截至 10-20，10 节全部到期，2 节已交付 → 退 8 节
        self.assertEqual(cand2["amount_cents"], 80000)

    def test_money_back_guarantee_uses_frozen_measurement(self) -> None:
        guarantee = {
            "refund_kind": "full",
            "target_weight_kg": 70.0,
            "by_date": "2026-09-24T23:59:59+08:00",
        }
        publish_standard_promise(self.b, guarantee=guarantee)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        # 已交付 3 节：未履行价值 70000，低于承诺全额 100000，差额须由承诺补足
        mark_delivered(self.b, "K-001", [
            ("S01", "2026-09-04T18:00:00+08:00"),
            ("S02", "2026-09-05T18:00:00+08:00"),
            ("S03", "2026-09-06T18:00:00+08:00"),
        ])
        self.b.record_measurement(
            actor=COACH, contract_id="K-001", measurement_id="M-1",
            taken_at="2026-09-24T09:00:00+08:00", weight_kg=75.0,
        )
        # 销售事后想把体重改成 69——必须失败
        with self.assertRaises(ImmutableRecordError):
            self.b.record_measurement(
                actor=COACH, contract_id="K-001", measurement_id="M-1",
                taken_at="2026-09-24T09:00:00+08:00", weight_kg=69.0,
            )
        self.b.open_dispute(
            case_id="CASE-G", consumer_id="CONSUMER-1", store_id="STORE-1",
            contract_ids=["K-001"], reason="service_unperformed",
            claimed_at="2026-09-25T10:00:00+08:00", summary="未达标要求不瘦退款",
        )
        cand = self.b.calculate_candidate(case_id="CASE-G")
        self.assertTrue(cand["guarantee_triggered"])
        self.assertEqual(cand["amount_cents"], 100000)
        codes = {line["code"] for line in cand["lines"]}
        self.assertIn("GUARANTEE_MONEY_BACK", codes)


class ConcurrentSettlementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        self.b.open_dispute(
            case_id="CASE-1", consumer_id="CONSUMER-1", store_id="STORE-1",
            contract_ids=["K-001"], reason="regret",
            claimed_at="2026-09-06T10:00:00+08:00", summary="并发和解",
        )
        self.cand = self.b.calculate_candidate(case_id="CASE-1")
        self.b.add_mediation_note(actor=SPECIALIST, case_id="CASE-1", note="调解完成")
        self.version = self.b.cases["CASE-1"]["version"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_only_one_concurrent_approval_commits(self) -> None:
        barrier = threading.Barrier(2)
        results: list[object] = []

        def approve(approver: Actor, refund_id: str) -> None:
            barrier.wait()
            try:
                self.b.approve_refund(
                    actor=approver, case_id="CASE-1",
                    candidate_id=self.cand["candidate_id"], refund_id=refund_id,
                    agreement_fingerprint=f"agr-{refund_id}",
                    agreement_source="settlement.pdf", expected_version=self.version,
                )
                results.append(("ok", refund_id))
            except Exception as exc:  # noqa: BLE001 - 断言在主线程做
                results.append(("err", type(exc).__name__))

        t1 = threading.Thread(target=approve, args=(APPROVER, "R-1"))
        t2 = threading.Thread(target=approve, args=(APPROVER_2, "R-2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0][1], "ConcurrentModificationError")
        self.assertEqual(len(self.b.cases["CASE-1"]["refund_ids"]), 1)

        # 失败方以新版本重试即被拒：同一案件不能二次和解
        winner = oks[0][1]
        with self.assertRaises(CaseStateError):
            self.b.approve_refund(
                actor=APPROVER_2, case_id="CASE-1",
                candidate_id=self.cand["candidate_id"],
                refund_id="R-LOST", agreement_fingerprint="agr-x",
                agreement_source="settlement.pdf",
                expected_version=self.b.cases["CASE-1"]["version"],
            )

    def test_concurrent_installment_same_id_pays_once(self) -> None:
        self.b.approve_refund(
            actor=APPROVER, case_id="CASE-1", candidate_id=self.cand["candidate_id"],
            refund_id="R-9", agreement_fingerprint="agr-9", agreement_source="s.pdf",
            expected_version=self.version,
        )
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def pay() -> None:
            barrier.wait()
            try:
                self.b.settle_installment(
                    actor=APPROVER, refund_id="R-9", installment_id="INST-X",
                    amount_cents=95000, paid_at="2026-09-09T12:00:00+08:00",
                    provider="bank", receipt_fingerprint="rcpt-x",
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=pay) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        paid = [e for e in self.b.journal.events() if e.body["event_type"] == "REFUND_SETTLED"]
        self.assertEqual(len(paid), 1)
        self.assertEqual(self.b.refunds["R-9"]["settled_cents"], 95000)

    def test_installment_id_scoped_per_refund(self) -> None:
        # 同一消费者的两起独立案件（跨店续营），分期标识同名也必须各付一次
        self.b.approve_refund(
            actor=APPROVER, case_id="CASE-1", candidate_id=self.cand["candidate_id"],
            refund_id="R-A", agreement_fingerprint="agr-a", agreement_source="s.pdf",
            expected_version=self.version)
        publish_standard_promise(self.b, store_id="STORE-2", promise_id="PROMO-0902")
        sign_standard_contract(
            self.b, consumer_id="CONSUMER-1", contract_id="K-002",
            store_id="STORE-2", promise_id="PROMO-0902",
            signed_at="2026-09-15T10:00:00+08:00")
        self.b.record_payment(
            actor=SALES, contract_id="K-002", payment_id="PAY2-1", amount_cents=100000,
            paid_at="2026-09-15T10:05:00+08:00", provider="wechatpay",
            receipt_fingerprint="fp-PAY2-1")
        self.b.open_dispute(
            case_id="CASE-2", consumer_id="CONSUMER-1", store_id="STORE-2",
            contract_ids=["K-002"], reason="regret",
            claimed_at="2026-09-16T10:00:00+08:00", summary="新店争议")
        cand2 = self.b.calculate_candidate(case_id="CASE-2")
        self.b.add_mediation_note(actor=SPECIALIST_2, case_id="CASE-2", note="调解")
        self.b.approve_refund(
            actor=APPROVER, case_id="CASE-2", candidate_id=cand2["candidate_id"],
            refund_id="R-B", agreement_fingerprint="agr-b", agreement_source="s.pdf")

        self.b.settle_installment(
            actor=APPROVER, refund_id="R-A", installment_id="INST-SAME",
            amount_cents=95000, paid_at="2026-09-17T12:00:00+08:00",
            provider="bank", receipt_fingerprint="r-a")
        # 不同退款单，同一分期标识：不得被去重吞掉
        self.b.settle_installment(
            actor=APPROVER, refund_id="R-B", installment_id="INST-SAME",
            amount_cents=cand2["amount_cents"], paid_at="2026-09-18T12:00:00+08:00",
            provider="bank", receipt_fingerprint="r-b")
        self.assertEqual(self.b.refunds["R-A"]["settled_cents"], 95000)
        self.assertEqual(self.b.refunds["R-B"]["settled_cents"], cand2["amount_cents"])
        paid = [e for e in self.b.journal.events() if e.body["event_type"] == "REFUND_SETTLED"]
        self.assertEqual(len(paid), 2)


if __name__ == "__main__":
    unittest.main()
