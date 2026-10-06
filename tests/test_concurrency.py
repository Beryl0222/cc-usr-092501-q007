"""并发和解、故障恢复不重复付款/不丢保全、消费者视图。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import CampDomain
from src.state import load_state, refund_net_paid_cents
from src.store import ConflictError, EventStore
from tests.scenario import build_domain, role, sign_standard_contract


def _service_case_ready(dom) -> str:
    sign_standard_contract(dom)
    dom.receive_installment(role("sal"), contract_id="c1", seq=1, amount_cents=100_000,
                            paid_at="2026-03-01T10:00:00+08:00")
    for k in range(10):
        dom.record_service_session(
            role("coach"), contract_id="c1", session_id=f"s{k}",
            scheduled_at="2026-03-01T10:00:00+08:00", delivered=False)
    return "c1"


class ConcurrentSettlementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = str(Path(self.tmp.name) / "events.jsonl")
        dom = build_domain(self.tmp.name)
        _service_case_ready(dom)
        dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                      case_id="case-1")
        dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        dom.approve_refund(role("appr"), case_id="case-1", approval_id="ap-1",
                           candidate_id="cand-1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_refund_payments_never_exceed_approval(self) -> None:
        # 指定场景：并发和解——两个支付会话同时尝试各付全额 100000。
        results: list[Exception | str] = []

        def payer(payment_id: str) -> None:
            dom = CampDomain(EventStore(self.log))
            try:
                dom.pay_refund_installment(role("payer"), case_id="case-1",
                                           payment_id=payment_id, amount_cents=100_000)
                results.append("paid")
            except Exception as error:  # noqa: BLE001 — 记录线程内失败
                results.append(error)

        t1 = threading.Thread(target=payer, args=("pay-concurrent-1",))
        t2 = threading.Thread(target=payer, args=("pay-concurrent-2",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        successes = [r for r in results if r == "paid"]
        self.assertEqual(len(successes), 1, f"应恰好一笔成功：{results}")
        state = load_state(EventStore(self.log))
        self.assertEqual(refund_net_paid_cents(state.refunds["refund-case-1"]), 100_000)

    def test_concurrent_case_open_only_one_wins(self) -> None:
        # 同一合同不能因为并发开案产生两个未结案件
        log2 = str(Path(self.tmp.name) / "events2.jsonl")
        dom = build_domain(log2)
        sign_standard_contract(dom, contract_id="cc")
        outcomes = []

        def opener(case_id: str) -> None:
            d = CampDomain(EventStore(log2))
            try:
                d.open_case(role("spec"), contract_id="cc", reason_category="regret",
                            case_id=case_id)
                outcomes.append("opened")
            except Exception as error:  # noqa: BLE001
                outcomes.append(error)

        threads = [threading.Thread(target=opener, args=(f"case-x-{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("opened"), 1, outcomes)


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = str(Path(self.tmp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rebuild_after_restart_keeps_payments_and_holds(self) -> None:
        # 指定场景：故障恢复后不得重复付款或丢失证据保全期限。
        dom = build_domain(self.log)
        _service_case_ready(dom)
        dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                      case_id="case-1")
        dom.register_receipt(role("sal"), case_id="case-1", receipt_id="ev-1", kind="evidence",
                             provider="consumer", fingerprint="sha256:ev1",
                             submitted_by="consumer-1")
        dom.hold_evidence(role("spec"), case_id="case-1", hold_id="hold-1",
                          receipt_id="ev-1", retain_until="2027-03-01T00:00:00+08:00")
        dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        dom.approve_refund(role("appr"), case_id="case-1", approval_id="ap-1",
                           candidate_id="cand-1")
        dom.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-1",
                                   amount_cents=60_000)

        # 模拟进程崩溃后重启：新建 store/domain 从日志重放
        revived = CampDomain(EventStore(self.log))
        # 保全期限仍在
        holds = revived.expiring_holds()
        self.assertTrue(any(h["hold_id"] == "hold-1" for h in holds))
        # 已付款额仍在，重复支付同一标识被拒
        self.assertEqual(refund_net_paid_cents(revived.state.refunds["refund-case-1"]), 60_000)
        from src.store import DomainError
        with self.assertRaises(DomainError):
            revived.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-1",
                                           amount_cents=60_000)
        # 恢复后可继续把剩余 40000 付完并结算
        revived.pay_refund_installment(role("payer"), case_id="case-1", payment_id="pay-2",
                                       amount_cents=40_000)
        revived.close_settlement(role("settler"), case_id="case-1")
        self.assertEqual(revived.state.cases["case-1"]["status"], "settled")

    def test_corrupt_or_gapped_log_is_rejected(self) -> None:
        dom = build_domain(self.log)
        sign_standard_contract(dom)
        # 人为损坏：追加版本断档的行，恢复必须报错而不是静默吞掉
        import json
        with open(self.log, "a", encoding="utf-8") as fh:
            bad = {"event_id": "x", "event_type": "CASE_OPENED", "aggregate_type": "dispute_case",
                   "aggregate_id": "ghost", "occurred_at": "2026-01-01T00:00:00+08:00",
                   "version": 99, "summary": "断档", "actor": {"id": "spec", "role": "dispute_specialist"},
                   "data": {}}
            fh.write(json.dumps(bad, ensure_ascii=False) + "\n")
        from src.store import DomainError
        with self.assertRaises(DomainError):
            EventStore(self.log)


class ConsumerViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dom = build_domain(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_consumer_sees_adopted_contract_and_each_calculation(self) -> None:
        sign_standard_contract(self.dom)
        self.dom.receive_installment(role("sal"), contract_id="c1", seq=1, amount_cents=100_000,
                                     paid_at="2026-03-01T10:00:00+08:00")
        for k in range(10):
            self.dom.record_service_session(
                role("coach"), contract_id="c1", session_id=f"s{k}",
                scheduled_at="2026-03-01T10:00:00+08:00", delivered=False)
        self.dom.open_case(role("spec"), contract_id="c1", reason_category="service",
                           case_id="case-1")
        self.dom.compute_refund_candidate(role("spec"), case_id="case-1", candidate_id="cand-1")
        view = self.dom.consumer_view("consumer-1")
        contract = view["contracts"][0]
        # 消费者能看到签约时采用的承诺版本（指纹/来源/口径）
        self.assertEqual(contract["adopted_promise"]["content_hash"], "sha256:ad001")
        self.assertEqual(contract["adopted_promise"]["source_ref"],
                         "ads/2026/slimfast-v9.png")
        case = contract["cases"][0]
        self.assertEqual(case["refund_candidates"][0]["amount_cents"], 100_000)
        self.assertIn("basis", case["refund_candidates"][0])


if __name__ == "__main__":
    unittest.main()
