"""治理不变量：分权、可见范围、标识冲突锁案、监管阻断结案、跨店继承、篡改检测。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.journal import EventJournal, TamperError
from src.readmodel import consumer_view, monthly_reconciliation, store_todo
from src.services import (
    Actor,
    Backend,
    CaseLockedError,
    CaseStateError,
    RoleError,
    SettlementConditionError,
)

from tests.helpers import (
    APPROVER,
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
)

ROOT = Path(__file__).parents[1]


def _basic_case(b: Backend, *, reason: str = "health", claimed_at: str = "2026-09-10T10:00:00+08:00",
                case_id: str = "CASE-1", consumer_id: str = "CONSUMER-1",
                contract_id: str = "K-001", store_id: str = "STORE-1") -> str:
    b.open_dispute(
        case_id=case_id, consumer_id=consumer_id, store_id=store_id,
        contract_ids=[contract_id], reason=reason, claimed_at=claimed_at,
        summary="争议",
    )
    return case_id


class RoleSeparationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        _basic_case(self.b)
        self.cand = self.b.calculate_candidate(case_id="CASE-1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mediator_approver_regulator_must_differ(self) -> None:
        # 销售/教练不能调解、不能批准
        with self.assertRaises(RoleError):
            self.b.add_mediation_note(actor=SALES, case_id="CASE-1", note="x")
        with self.assertRaises(RoleError):
            self.b.approve_refund(
                actor=SPECIALIST, case_id="CASE-1", candidate_id=self.cand["candidate_id"],
                refund_id="R-X", agreement_fingerprint="a", agreement_source="a.pdf")

        self.b.add_mediation_note(actor=SPECIALIST, case_id="CASE-1", note="调解意见")
        # 同一人即使挂批准角色也不能批自己调解的案子
        same_person_approver = Actor("SP-001", "approver")
        with self.assertRaises(RoleError):
            self.b.approve_refund(
                actor=same_person_approver, case_id="CASE-1",
                candidate_id=self.cand["candidate_id"], refund_id="R-X",
                agreement_fingerprint="a", agreement_source="a.pdf")

        self.b.approve_refund(
            actor=APPROVER, case_id="CASE-1", candidate_id=self.cand["candidate_id"],
            refund_id="R-1", agreement_fingerprint="a", agreement_source="a.pdf")

        # 监管答复不得由批准人或调解人提交
        with self.assertRaises(RoleError):
            self.b.file_regulatory_response(
                actor=Actor("AP-001", "regulator"), case_id="CASE-1",
                response_fingerprint="r", summary="答复", filed_at="2026-09-11T10:00:00+08:00")
        ok = self.b.file_regulatory_response(
            actor=REGULATOR, case_id="CASE-1",
            response_fingerprint="r", summary="答复", filed_at="2026-09-11T10:00:00+08:00")
        self.assertEqual(ok["event_type"], "REGULATORY_RESPONSE_FILED")

    def test_sales_cannot_rewrite_measurement(self) -> None:
        with self.assertRaises(RoleError):
            self.b.record_measurement(
                actor=SALES, contract_id="K-001", measurement_id="M-9",
                taken_at="2026-09-08T09:00:00+08:00", weight_kg=80.0)
        self.b.record_measurement(
            actor=COACH, contract_id="K-001", measurement_id="M-9",
            taken_at="2026-09-08T09:00:00+08:00", weight_kg=80.0)
        # 只能追加说明
        note = self.b.add_staff_note(
            actor=SALES, subject_type="measurement", subject_id="M-9",
            note="当天穿着较重，仅作补充，不改原值", at="2026-09-09T09:00:00+08:00")
        self.assertEqual(note["event_type"], "STAFF_NOTE_ADDED")


class VisibleScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        _basic_case(self.b)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_specialist_only_sees_both_party_materials(self) -> None:
        self.b.hold_evidence(
            case_id="CASE-1", evidence_id="EV-OPEN", kind="chat_log",
            source="consumer/upload", provider="consumer-app", filename="chat.zip",
            fingerprint="fp-open", held_at="2026-09-10T11:00:00+08:00",
            submitted_by="CONSUMER-1", retention_until="2028-09-10T00:00:00+08:00",
            visible_to={"consumer", "store"})
        self.b.hold_evidence(
            case_id="CASE-1", evidence_id="EV-INTERNAL", kind="internal_memo",
            source="regulator/intake", provider="authority", filename="memo.pdf",
            fingerprint="fp-secret", held_at="2026-09-10T12:00:00+08:00",
            submitted_by="REG-AUTH", retention_until="2031-09-10T00:00:00+08:00",
            visible_to={"regulator"})
        ids = {e["evidence_id"] for e in self.b.visible_materials("CASE-1")["evidence"]}
        self.assertEqual(ids, {"EV-OPEN"})

    def test_store_todo_is_scoped_to_store(self) -> None:
        # STORE-2 看不到 STORE-1 的待办
        self.assertEqual(store_todo(self.b, "STORE-2")["todos"], [])
        todos = store_todo(self.b, "STORE-1")["todos"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["case_id"], "CASE-1")


class IdentifierConflictLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_payment_same_id_changed_amount_locks_case(self) -> None:
        _basic_case(self.b)
        cand = self.b.calculate_candidate(case_id="CASE-1")
        with self.assertRaises(CaseLockedError):
            self.b.record_payment(
                actor=SALES, contract_id="K-001", payment_id="PAY-1",
                amount_cents=40000,  # 首次是 60000
                paid_at="2026-09-03T10:05:00+08:00", provider="wechatpay",
                receipt_fingerprint="fp-PAY-1-CHANGED")
        case = self.b.cases["CASE-1"]
        self.assertTrue(case["locked"])
        # 锁定后处置命令一律拒绝
        with self.assertRaises(CaseLockedError):
            self.b.add_mediation_note(actor=SPECIALIST, case_id="CASE-1", note="x")
        with self.assertRaises(CaseLockedError):
            self.b.approve_refund(
                actor=APPROVER, case_id="CASE-1", candidate_id=cand["candidate_id"],
                refund_id="R-1", agreement_fingerprint="a", agreement_source="a.pdf")

        # 但证据仍可继续保全，保全期限不丢失
        self.b.hold_evidence(
            case_id="CASE-1", evidence_id="EV-1", kind="receipt",
            source="consumer/upload", provider="consumer-app", filename="r.zip",
            fingerprint="fp-ev1", held_at="2026-09-10T13:00:00+08:00",
            submitted_by="CONSUMER-1", retention_until="2029-09-10T00:00:00+08:00")
        self.assertEqual(self.b.evidence["CASE-1"][0]["retention_until"],
                         "2029-09-10T00:00:00+08:00")

        # 锁定状态跨恢复保持
        recovered = Backend(EventJournal(str(Path(self.tmp.name) / "journal.jsonl")))
        self.assertTrue(recovered.cases["CASE-1"]["locked"])

    def test_evidence_same_id_changed_fingerprint_locks_case(self) -> None:
        _basic_case(self.b)
        self.b.hold_evidence(
            case_id="CASE-1", evidence_id="EV-1", kind="receipt",
            source="consumer/upload", provider="consumer-app", filename="r.zip",
            fingerprint="fp-original", held_at="2026-09-10T13:00:00+08:00",
            submitted_by="CONSUMER-1", retention_until="2029-09-10T00:00:00+08:00")
        # 完整重传只记一次
        again = self.b.hold_evidence(
            case_id="CASE-1", evidence_id="EV-1", kind="receipt",
            source="consumer/upload", provider="consumer-app", filename="r.zip",
            fingerprint="fp-original", held_at="2026-09-10T13:00:00+08:00",
            submitted_by="CONSUMER-1", retention_until="2029-09-10T00:00:00+08:00")
        self.assertEqual(again["event_id"], "EVIDENCE_HELD:EV-1:v1")
        self.assertEqual(len(self.b.evidence["CASE-1"]), 1)
        # 文件指纹变了：锁案
        with self.assertRaises(CaseLockedError):
            self.b.hold_evidence(
                case_id="CASE-1", evidence_id="EV-1", kind="receipt",
                source="consumer/upload", provider="consumer-app", filename="r.zip",
                fingerprint="fp-swapped", held_at="2026-09-11T13:00:00+08:00",
                submitted_by="CONSUMER-1", retention_until="2029-09-10T00:00:00+08:00")
        self.assertTrue(self.b.cases["CASE-1"]["locked"])


class InvestigationAndCrossStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_settlement_cannot_close_open_investigation(self) -> None:
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        _basic_case(self.b)
        self.b.open_investigation(
            case_id="CASE-1", authority="市监局", reference="REG-2026-0901",
            opened_at="2026-09-11T09:00:00+08:00")
        cand = self.b.calculate_candidate(case_id="CASE-1")
        self.b.add_mediation_note(actor=SPECIALIST, case_id="CASE-1", note="调解")
        self.b.approve_refund(
            actor=APPROVER, case_id="CASE-1", candidate_id=cand["candidate_id"],
            refund_id="R-1", agreement_fingerprint="agr", agreement_source="s.pdf")
        self.b.settle_installment(
            actor=APPROVER, refund_id="R-1", installment_id="INST-1",
            amount_cents=cand["amount_cents"], paid_at="2026-09-12T12:00:00+08:00",
            provider="bank", receipt_fingerprint="r1")
        # 退款已结清，但调查未结 → 不能关案
        with self.assertRaises(CaseStateError):
            self.b.close_case(
                actor=SPECIALIST, case_id="CASE-1",
                closed_at="2026-09-12T18:00:00+08:00")
        self.b.file_regulatory_response(
            actor=REGULATOR, case_id="CASE-1", response_fingerprint="resp",
            summary="监管答复", filed_at="2026-09-13T10:00:00+08:00")
        # 答复不等于调查结案；仍需明确结案
        with self.assertRaises(CaseStateError):
            self.b.close_case(
                actor=SPECIALIST, case_id="CASE-1",
                closed_at="2026-09-13T18:00:00+08:00")
        self.b.close_investigation(case_id="CASE-1", at="2026-09-14T10:00:00+08:00")
        closed = self.b.close_case(
            actor=SPECIALIST, case_id="CASE-1",
            closed_at="2026-09-14T11:00:00+08:00")
        self.assertEqual(closed["payload"]["evidence_retention_survives"], True)

    def test_deletion_condition_rejected(self) -> None:
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        _basic_case(self.b)
        cand = self.b.calculate_candidate(case_id="CASE-1")
        with self.assertRaises(SettlementConditionError):
            self.b.approve_refund(
                actor=APPROVER, case_id="CASE-1", candidate_id=cand["candidate_id"],
                refund_id="R-1", agreement_fingerprint="agr", agreement_source="s.pdf",
                requires_record_deletion=True)
        # 没有产生批准事件
        self.assertNotIn("R-1", self.b.refunds)

    def test_open_dispute_inherits_across_store_reenrollment(self) -> None:
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)
        _basic_case(self.b)
        case_version = self.b.cases["CASE-1"]["version"]

        # 消费者到 STORE-2 续营，签新合同
        publish_standard_promise(self.b, store_id="STORE-2", promise_id="PROMO-0902")
        sign_standard_contract(
            self.b, consumer_id="CONSUMER-1", contract_id="K-002",
            store_id="STORE-2", promise_id="PROMO-0902",
            signed_at="2026-09-15T10:00:00+08:00")
        case = self.b.cases["CASE-1"]
        self.assertIn("STORE-2", case["involved_stores"])
        self.assertIn("K-002", case["contract_ids"])
        self.assertGreater(case["version"], case_version)
        # 两店待办都能看到同一案件
        self.assertEqual({t["case_id"] for t in store_todo(self.b, "STORE-2")["todos"]},
                         {"CASE-1"})

    def test_anomaly_before_dispute_auto_locks_new_case(self) -> None:
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        # 首笔付款正常入账
        self.b.record_payment(
            actor=SALES, contract_id="K-001", payment_id="PAY-1", amount_cents=60000,
            paid_at="2026-09-03T10:05:00+08:00", provider="wechatpay",
            receipt_fingerprint="fp-PAY-1")
        # 开案前出现同一支付标识的金额/指纹冲突
        with self.assertRaises(CaseLockedError):
            self.b.record_payment(
                actor=SALES, contract_id="K-001", payment_id="PAY-1",
                amount_cents=999, paid_at="2026-09-03T10:05:00+08:00",
                provider="wechatpay", receipt_fingerprint="changed")
        _basic_case(self.b)
        self.assertTrue(self.b.cases["CASE-1"]["locked"])


class JournalTamperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)
        publish_standard_promise(self.b)
        sign_standard_contract(self.b)
        pay_standard(self.b)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_modified_and_deleted_lines_detected(self) -> None:
        path = Path(self.tmp.name) / "journal.jsonl"
        raw = path.read_text(encoding="utf-8").splitlines()
        # 原地改写一条承诺事件（模拟改话术）→ 内容指纹失败
        target = next(i for i, line in enumerate(raw) if "急速暴瘦" in line)
        altered = raw.copy()
        altered[target] = altered[target].replace("急速暴瘦", "平稳减重")
        path.write_text("\n".join(altered) + "\n", encoding="utf-8")
        with self.assertRaises(TamperError):
            EventJournal(path)

        # 删除中间一行 → 链指针断裂
        path.write_text("\n".join(raw[:1] + raw[2:]) + "\n", encoding="utf-8")
        with self.assertRaises(TamperError):
            EventJournal(path)


class ConsumerViewAndReconcileCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_backend(self.tmp.name)
        register_people(self.b)
        publish_standard_promise(
            self.b, guarantee={
                "refund_kind": "full", "target_weight_kg": 70.0,
                "by_date": "2026-09-24T23:59:59+08:00"})
        sign_standard_contract(self.b)
        pay_standard(self.b)
        mark_delivered(self.b, "K-001", [("S01", "2026-09-04T18:00:00+08:00")])
        self.b.record_measurement(
            actor=COACH, contract_id="K-001", measurement_id="M-1",
            taken_at="2026-09-24T09:00:00+08:00", weight_kg=75.0)
        _basic_case(self.b, reason="service_unperformed",
                    claimed_at="2026-09-25T10:00:00+08:00")
        self.cand = self.b.calculate_candidate(case_id="CASE-1")
        self.b.add_mediation_note(actor=SPECIALIST, case_id="CASE-1", note="调解")
        self.b.approve_refund(
            actor=APPROVER, case_id="CASE-1", candidate_id=self.cand["candidate_id"],
            refund_id="R-1", agreement_fingerprint="agr", agreement_source="s.pdf")
        self.b.settle_installment(
            actor=APPROVER, refund_id="R-1", installment_id="INST-1",
            amount_cents=50000, paid_at="2026-09-26T12:00:00+08:00",
            provider="bank", receipt_fingerprint="r1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_consumer_view_shows_adopted_contract_and_calculations(self) -> None:
        view = consumer_view(self.b, "CONSUMER-1")
        self.assertEqual(len(view["cases"]), 1)
        case = view["cases"][0]
        self.assertEqual(case["contracts"][0]["adopted_promise"]["campaign"], "急速暴瘦·不瘦退款")
        self.assertEqual(case["candidates"][0]["candidate_id"], self.cand["candidate_id"])
        self.assertTrue(all("lines" in c for c in case["candidates"]))
        self.assertEqual(case["refunds"][0]["paid_net_cents"], 50000)
        self.assertFalse(case["refunds"][0]["balanced"])

    def test_monthly_reconcile_flags_unpaid_and_missed_guarantee(self) -> None:
        report = monthly_reconciliation(self.b, "2026-09")
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("REFUND_NOT_BALANCED", codes)
        # 未达标承诺已进入退款流程（虽然未付清），不再报“未退款”
        self.assertNotIn("GUARANTEE_MISSED_NO_REFUND", codes)

        # 付清后对平
        self.b.settle_installment(
            actor=APPROVER, refund_id="R-1", installment_id="INST-2",
            amount_cents=50000, paid_at="2026-09-27T12:00:00+08:00",
            provider="bank", receipt_fingerprint="r2")
        report2 = monthly_reconciliation(self.b, "2026-09")
        self.assertNotIn("REFUND_NOT_BALANCED", {f["code"] for f in report2["findings"]})

    def test_cli_commands(self) -> None:
        journal = str(Path(self.tmp.name) / "journal.jsonl")

        result = subprocess.run(
            [sys.executable, "-m", "src.cli", "verify", journal],
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("事件链完整", result.stdout)

        result = subprocess.run(
            [sys.executable, "-m", "src.cli", "consumer", journal, "CONSUMER-1"],
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["consumer_id"], "CONSUMER-1")

        result = subprocess.run(
            [sys.executable, "-m", "src.cli", "todo", journal, "STORE-1"],
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CASE-1", result.stdout)

        result = subprocess.run(
            [sys.executable, "-m", "src.cli", "reconcile", journal, "2026-09"],
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 3, result.stdout)  # 尚有未付分期
        self.assertIn("REFUND_NOT_BALANCED", result.stdout)


if __name__ == "__main__":
    unittest.main()
