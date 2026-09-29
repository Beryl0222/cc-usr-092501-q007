"""退款规则纯函数测试：健康原因、服务未履行、普通反悔。"""

from __future__ import annotations

import unittest

from src import domain


def contract(**over):
    base = {
        "signed_at": "2026-09-01T10:00:00+08:00",
        "promise_id": "P-1",
        "price_cents": 100000,
        "cooling_off_days": 7,
        "admin_fee_cents": 5000,
        "sessions": [
            {"session_id": f"S{i:02d}",
             "scheduled_for": f"2026-09-{2 + i:02d}T18:00:00+08:00",
             "unit_price_cents": 10000}
            for i in range(1, 11)
        ],
    }
    base.update(over)
    return base


def payments(*amounts):
    return [{"payment_id": f"P{i}", "amount_cents": a} for i, a in enumerate(amounts, 1)]


def delivered(ids):
    return [{"session_id": sid} for sid in ids]


class RefundRuleTest(unittest.TestCase):
    def test_regret_inside_cooling_off(self) -> None:
        out = domain.calculate_refund(
            reason="regret", contract=contract(), payments=payments(100000),
            delivered_sessions=delivered(["S01"]), measurements=[], incidents=[],
            claimed_at="2026-09-05T10:00:00+08:00")
        # 100000 - 10000(已消费) - 5000(管理费)
        self.assertEqual(out["amount_cents"], 85000)
        self.assertEqual(out["lines"][0]["code"], "COOLING_OFF_REFUND")

    def test_regret_after_cooling_off_is_zero(self) -> None:
        out = domain.calculate_refund(
            reason="regret", contract=contract(), payments=payments(100000),
            delivered_sessions=delivered(["S01"]), measurements=[], incidents=[],
            claimed_at="2026-09-20T10:00:00+08:00")
        self.assertEqual(out["amount_cents"], 0)
        self.assertEqual(out["lines"][0]["code"], "NO_COOLING_OFF_REFUND")

    def test_service_unperformed_counts_only_due_and_undelivered(self) -> None:
        out = domain.calculate_refund(
            reason="service_unperformed", contract=contract(), payments=payments(100000),
            delivered_sessions=delivered(["S01", "S02"]), measurements=[], incidents=[],
            claimed_at="2026-09-08T10:00:00+08:00")
        # 截至 9-08 10:00：S01..S05 到期（S06 在当天 18:00 尚未到期），2 节已交 → 3 节未交
        self.assertEqual(out["amount_cents"], 30000)

    def test_guarantee_full_refund_when_target_missed(self) -> None:
        c = contract(guarantee={
            "refund_kind": "full", "target_weight_kg": 70.0,
            "by_date": "2026-09-24T23:59:59+08:00"})
        out = domain.calculate_refund(
            reason="service_unperformed", contract=c, payments=payments(100000),
            delivered_sessions=delivered(["S01"]),
            measurements=[{"measurement_id": "M1", "taken_at": "2026-09-24T09:00:00+08:00",
                           "weight_kg": 75.0}],
            incidents=[], claimed_at="2026-09-25T10:00:00+08:00")
        self.assertTrue(out["guarantee_triggered"])
        self.assertEqual(out["amount_cents"], 100000)  # 未履行 80000 + 承诺补足 20000

    def test_guarantee_met_means_no_topup(self) -> None:
        c = contract(guarantee={
            "refund_kind": "full", "target_weight_kg": 70.0,
            "by_date": "2026-09-24T23:59:59+08:00"})
        out = domain.calculate_refund(
            reason="service_unperformed", contract=c, payments=payments(100000),
            delivered_sessions=delivered(["S01"]),
            measurements=[{"measurement_id": "M1", "taken_at": "2026-09-24T09:00:00+08:00",
                           "weight_kg": 69.5}],
            incidents=[], claimed_at="2026-09-25T10:00:00+08:00")
        self.assertFalse(out["guarantee_triggered"])
        self.assertGreater(out["amount_cents"], 0)

    def test_health_incident_refunds_everything_paid(self) -> None:
        out = domain.calculate_refund(
            reason="health", contract=contract(), payments=payments(60000, 40000),
            delivered_sessions=delivered(["S01", "S02", "S03"]),
            measurements=[],
            incidents=[{"incident_id": "INC-1", "occurred_at": "2026-09-07T10:00:00+08:00"}],
            claimed_at="2026-09-08T10:00:00+08:00")
        self.assertEqual(out["amount_cents"], 100000)
        self.assertEqual(out["lines"][0]["code"], "HEALTH_INCIDENT_FULL")

    def test_health_without_incident_is_prorated(self) -> None:
        out = domain.calculate_refund(
            reason="health", contract=contract(), payments=payments(100000),
            delivered_sessions=delivered(["S01", "S02"]),
            measurements=[], incidents=[],
            claimed_at="2026-09-08T10:00:00+08:00")
        self.assertEqual(out["amount_cents"], 80000)
        self.assertEqual(out["lines"][0]["code"], "HEALTH_WITHDRAWAL_PRORATED")

    def test_incident_after_claim_is_not_counted(self) -> None:
        out = domain.calculate_refund(
            reason="health", contract=contract(), payments=payments(100000),
            delivered_sessions=[], measurements=[],
            incidents=[{"incident_id": "INC-1", "occurred_at": "2026-09-20T10:00:00+08:00"}],
            claimed_at="2026-09-08T10:00:00+08:00")
        self.assertEqual(out["lines"][0]["code"], "HEALTH_WITHDRAWAL_PRORATED")

    def test_unknown_reason_rejected(self) -> None:
        with self.assertRaises(ValueError):
            domain.calculate_refund(
                reason="mood", contract=contract(), payments=[], delivered_sessions=[],
                measurements=[], incidents=[], claimed_at="2026-09-08T10:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
