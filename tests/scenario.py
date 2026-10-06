"""构造一个可运行的标准场景，供各回归测试复用。

角色齐全：集团营销、门店销售/教练、争议专员、调解、批准、支付、结算、监管、审计。
广告口径"急速暴瘦""不瘦退款"被冻结为承诺 p1。
"""

from __future__ import annotations

from pathlib import Path

from src.domain import CampDomain
from src.store import EventStore

ADMIN = {"id": "admin", "role": "group_admin"}

STAFF = {
    "mkt": ("集团营销", "marketing", "hq"),
    "sal": ("销售小王", "sales", "store-1"),
    "sal2": ("销售小李", "sales", "store-2"),
    "mgr": ("一店店长", "store_manager", "store-1"),
    "mgr2": ("二店店长", "store_manager", "store-2"),
    "coach": ("教练小张", "coach", "store-1"),
    "safety": ("安全员", "safety_officer", "store-1"),
    "spec": ("争议专员", "dispute_specialist", "hq"),
    "med": ("调解人甲", "mediator", "hq"),
    "appr": ("退款批准人", "refund_approver", "hq"),
    "payer": ("退款支付员", "refund_payer", "hq"),
    "settler": ("结算员", "settlement_officer", "hq"),
    "reg": ("监管答复人", "regulatory_officer", "hq"),
    "auditor": ("审计员", "auditor", "hq"),
}


def role(staff_id: str) -> dict:
    return {"id": staff_id, "role": STAFF[staff_id][1]}


def build_domain(tmp_path) -> CampDomain:
    path = Path(tmp_path)
    log_path = path if path.suffix == ".jsonl" else path / "events.jsonl"
    dom = CampDomain(EventStore(log_path))
    for sid, (name, r, store) in STAFF.items():
        dom.register_staff(ADMIN, staff_id=sid, store_id=store, name=name, role=r)
    # 广告素材承诺：冻结来源、指纹、有效期与退款口径
    dom.publish_promise(
        role("mkt"), promise_id="promise-ad", store_id="store-1",
        source_kind="ad_material", source_ref="ads/2026/slimfast-v9.png",
        content_hash="sha256:ad001", claims=["急速暴瘦", "不瘦退款"],
        refund_terms={
            "service_fee_rate": 0.1,
            "service_default_cap_rate": 1,
            "result_claim_full_refund": True,
            "target_weight_kg": 60.0,
            "regret_schedule": [
                {"within_days": 7, "refund_rate": 1.0},
                {"within_days": 30, "refund_rate": 0.7},
            ],
        },
        valid_from="2026-01-01T00:00:00+08:00",
        valid_to="2026-12-31T23:59:59+08:00")
    # 销售话术承诺（与广告不同版本，各自冻结）
    dom.publish_promise(
        role("mkt"), promise_id="promise-script", store_id="store-1",
        source_kind="sales_script", source_ref="scripts/verbal-v3.pdf",
        content_hash="sha256:scr003", claims=["不瘦退款"],
        refund_terms={"service_fee_rate": 0.1, "service_default_cap_rate": 1,
                      "result_claim_full_refund": True, "target_weight_kg": 60.0},
        valid_from="2026-01-01T00:00:00+08:00",
        valid_to="2026-12-31T23:59:59+08:00")
    return dom


def sign_standard_contract(dom: CampDomain, contract_id: str = "c1", consumer: str = "consumer-1",
                           promise_id: str = "promise-ad", sales: str = "sal",
                           store: str = "store-1", signed_at: str = "2026-03-01T10:00:00+08:00",
                           planned_sessions: int = 10, price_cents: int = 100_000) -> str:
    dom.sign_contract(
        role(sales), contract_id=contract_id, store_id=store, consumer_id=consumer,
        sales_staff_id=sales, signed_at=signed_at, promise_id=promise_id,
        terms={"planned_sessions": planned_sessions, "price_cents": price_cents})
    return contract_id
