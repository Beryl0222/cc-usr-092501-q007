"""测试构造工具：搭建标准承诺/合同/付款场景。"""

from __future__ import annotations

from pathlib import Path

from src.journal import EventJournal
from src.services import Actor, Backend

SALES = Actor("S-001", "sales", "STORE-1")
COACH = Actor("C-001", "coach", "STORE-1")
SPECIALIST = Actor("SP-001", "specialist")
SPECIALIST_2 = Actor("SP-002", "specialist")
APPROVER = Actor("AP-001", "approver")
APPROVER_2 = Actor("AP-002", "approver")
REGULATOR = Actor("RG-001", "regulator")


def make_backend(tmp: Path | str) -> Backend:
    return Backend(EventJournal(str(Path(tmp) / "journal.jsonl")))


def register_people(b: Backend) -> None:
    b.register_staff(person_id="S-001", role="sales", store_id="STORE-1")
    b.register_staff(person_id="C-001", role="coach", store_id="STORE-1")


def standard_terms(*, sessions: int = 10, unit_price: int = 10000,
                   cooling: int = 7, admin_fee: int = 5000) -> dict:
    return {
        "price_cents": sessions * unit_price,
        "cooling_off_days": cooling,
        "admin_fee_cents": admin_fee,
        "sessions": [
            {
                "session_id": f"S{i:02d}",
                "scheduled_for": f"2026-09-{3 + i:02d}T18:00:00+08:00",
                "unit_price_cents": unit_price,
                "delivered": False,
            }
            for i in range(1, sessions + 1)
        ],
    }


def publish_standard_promise(b: Backend, *, guarantee: dict | None = None,
                             valid_until: str | None = None, store_id: str = "STORE-1",
                             promise_id: str = "PROMO-0901") -> str:
    b.publish_promise(
        actor=SALES,
        promise_id=promise_id,
        store_id=store_id,
        campaign="急速暴瘦·不瘦退款",
        claims=["21 天急速暴瘦 10 公斤", "不瘦退款"],
        guarantee=guarantee,
        materials=[
            {"material_id": "AD-1", "kind": "ad_creative", "source": "feed/0901.mp4",
             "fingerprint": "fp-ad-1"},
            {"material_id": "SCRIPT-1", "kind": "sales_script", "source": "store/STORE-1/script-v3",
             "fingerprint": "fp-script-1"},
        ],
        valid_from="2026-09-01T00:00:00+08:00",
        valid_until=valid_until,
    )
    return promise_id


def sign_standard_contract(
    b: Backend,
    *,
    consumer_id: str = "CONSUMER-1",
    contract_id: str = "K-001",
    store_id: str = "STORE-1",
    promise_id: str = "PROMO-0901",
    signed_at: str = "2026-09-03T10:00:00+08:00",
    terms: dict | None = None,
) -> dict:
    return b.sign_contract(
        actor=SALES,
        contract_id=contract_id,
        consumer_id=consumer_id,
        store_id=store_id,
        promise_id=promise_id,
        signed_at=signed_at,
        terms=terms or standard_terms(),
        document={"source": "store/STORE-1/contract-v7.pdf", "version": "v7",
                  "fingerprint": "fp-contract-001"},
        sales_person_id="S-001",
    )


def pay_standard(b: Backend, *, contract_id: str = "K-001",
                 amounts=((60000, "PAY-1", "2026-09-03T10:05:00+08:00"),
                          (40000, "PAY-2", "2026-09-04T10:05:00+08:00"))) -> None:
    for amount, pid, at in amounts:
        b.record_payment(
            actor=SALES, contract_id=contract_id, payment_id=pid, amount_cents=amount,
            paid_at=at, provider="wechatpay", receipt_fingerprint=f"fp-{pid}",
        )


def mark_delivered(b: Backend, contract_id: str, sessions: list[tuple[str, str]]) -> None:
    """登记课时交付 [(session_id, delivered_at), ...]。"""
    for sid, at in sessions:
        b.mark_session_delivered(actor=COACH, contract_id=contract_id,
                                 session_id=sid, delivered_at=at)
