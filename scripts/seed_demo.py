"""生成一条可用于本地演示的事件日志（覆盖承诺、测量、健康事件、争议与分期退款）。

用法：

    python3 -m scripts.seed_demo data/demo-journal.jsonl
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.journal import EventJournal  # noqa: E402
from src.services import Actor, Backend  # noqa: E402

SALES = Actor("S-001", "sales", "STORE-1")
COACH = Actor("C-001", "coach", "STORE-1")
SPECIALIST = Actor("SP-001", "specialist")
APPROVER = Actor("AP-001", "approver")
REGULATOR = Actor("RG-001", "regulator")


def build(path: str) -> Backend:
    b = Backend(EventJournal(path))
    b.register_staff(person_id="S-001", role="sales", store_id="STORE-1")
    b.register_staff(person_id="C-001", role="coach", store_id="STORE-1")

    sessions = [
        {"session_id": f"S{i:02d}",
         "scheduled_for": f"2026-09-{3 + i:02d}T18:00:00+08:00",
         "unit_price_cents": 10000, "delivered": False}
        for i in range(1, 11)
    ]
    b.publish_promise(
        actor=SALES, promise_id="PROMO-0901", store_id="STORE-1",
        campaign="急速暴瘦·不瘦退款",
        claims=["21 天急速暴瘦 10 公斤", "不瘦退款"],
        guarantee={"refund_kind": "full", "target_weight_kg": 70.0,
                   "by_date": "2026-09-24T23:59:59+08:00"},
        materials=[
            {"material_id": "AD-1", "kind": "ad_creative", "source": "feed/0901.mp4",
             "fingerprint": "fp-ad-1"},
            {"material_id": "SCRIPT-1", "kind": "sales_script",
             "source": "store/STORE-1/script-v3", "fingerprint": "fp-script-1"},
        ],
        valid_from="2026-09-01T00:00:00+08:00")
    b.sign_contract(
        actor=SALES, contract_id="K-001", consumer_id="CONSUMER-1", store_id="STORE-1",
        promise_id="PROMO-0901", signed_at="2026-09-03T10:00:00+08:00",
        terms={"price_cents": 100000, "cooling_off_days": 7, "admin_fee_cents": 5000,
               "sessions": sessions},
        document={"source": "store/STORE-1/contract-v7.pdf", "version": "v7",
                  "fingerprint": "fp-contract-001"},
        sales_person_id="S-001")
    b.freeze_payment_plan(contract_id="K-001", plan=[
        {"installment_id": "PLAN-1", "due_at": "2026-09-03T10:00:00+08:00", "amount_cents": 60000},
        {"installment_id": "PLAN-2", "due_at": "2026-09-04T10:00:00+08:00", "amount_cents": 40000},
    ])
    b.record_payment(actor=SALES, contract_id="K-001", payment_id="PAY-1",
                     amount_cents=60000, paid_at="2026-09-03T10:05:00+08:00",
                     provider="wechatpay", receipt_fingerprint="fp-PAY-1")
    b.record_payment(actor=SALES, contract_id="K-001", payment_id="PAY-2",
                     amount_cents=40000, paid_at="2026-09-04T10:05:00+08:00",
                     provider="wechatpay", receipt_fingerprint="fp-PAY-2")
    b.mark_session_delivered(actor=COACH, contract_id="K-001", session_id="S01",
                             delivered_at="2026-09-04T18:00:00+08:00")
    b.record_measurement(actor=COACH, contract_id="K-001", measurement_id="M-1",
                         taken_at="2026-09-10T09:00:00+08:00", weight_kg=82.0)
    b.record_safety_incident(
        actor=COACH, contract_id="K-001", incident_id="INC-1",
        occurred_at="2026-09-11T19:30:00+08:00", severity="medium",
        description="训练后头晕送医，医嘱停止高强度训练")
    b.record_withdrawal(actor=SALES, contract_id="K-001",
                        at="2026-09-12T10:00:00+08:00", reason="健康原因退营")

    b.open_dispute(case_id="CASE-0912-01", consumer_id="CONSUMER-1", store_id="STORE-1",
                   contract_ids=["K-001"], reason="health",
                   claimed_at="2026-09-12T10:30:00+08:00",
                   summary="训练相关健康事件后申请退营退款")
    b.hold_evidence(
        case_id="CASE-0912-01", evidence_id="EV-1", kind="medical_record",
        source="consumer/upload", provider="consumer-app", filename="clinic.pdf",
        fingerprint="fp-clinic-1", held_at="2026-09-12T11:00:00+08:00",
        submitted_by="CONSUMER-1", retention_until="2029-09-12T00:00:00+08:00")
    cand = b.calculate_candidate(case_id="CASE-0912-01")
    b.add_mediation_note(actor=SPECIALIST, case_id="CASE-0912-01",
                         note="健康事件证据双方可见，按健康原因候选金额调解")
    b.approve_refund(actor=APPROVER, case_id="CASE-0912-01",
                     candidate_id=cand["candidate_id"], refund_id="R-1",
                     agreement_fingerprint="fp-agreement-1",
                     agreement_source="settlement/CASE-0912-01/v1.pdf")
    b.settle_installment(actor=APPROVER, refund_id="R-1", installment_id="INST-1",
                         amount_cents=50000, paid_at="2026-09-13T12:00:00+08:00",
                         provider="bank", receipt_fingerprint="fp-rcpt-1")
    b.open_investigation(case_id="CASE-0912-01", authority="市监局",
                         reference="REG-2026-0912", opened_at="2026-09-13T15:00:00+08:00")
    b.file_regulatory_response(
        actor=REGULATOR, case_id="CASE-0912-01", response_fingerprint="fp-resp-1",
        summary="提交阶段性情况说明，调查继续", filed_at="2026-09-15T10:00:00+08:00")
    return b


def main(argv: list[str]) -> int:
    out = argv[1] if len(argv) > 1 else str(ROOT / "data" / "demo-journal.jsonl")
    path = Path(out)
    if path.exists():
        path.unlink()
    b = build(str(path))
    b.journal.verify_chain()
    print(f"已生成 {len(b.journal.events())} 条冻结事件：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
