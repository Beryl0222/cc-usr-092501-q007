"""承诺冻结、合同快照与销售离职场景。"""

from __future__ import annotations

import unittest

from src.store import DomainError, PermissionDenied
from tests.scenario import ADMIN, build_domain, role, sign_standard_contract


class PromiseFreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dom = build_domain(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_contract_freezes_promise_snapshot_at_signing(self) -> None:
        sign_standard_contract(self.dom)
        contract = self.dom.state.contracts["c1"]
        self.assertEqual(contract["promise_snapshot"]["content_hash"], "sha256:ad001")
        self.assertIn("急速暴瘦", contract["promise_snapshot"]["claims"])
        self.assertIn("不瘦退款", contract["promise_snapshot"]["claims"])

    def test_retiring_promise_does_not_change_signed_contract(self) -> None:
        sign_standard_contract(self.dom)
        # 事后撤广告 / 换话术版本：已签合同快照不变
        self.dom.retire_promise(role("mkt"), "promise-ad")
        contract = self.dom.state.contracts["c1"]
        self.assertEqual(contract["promise_snapshot"]["content_hash"], "sha256:ad001")
        self.assertFalse(contract["promise_snapshot"].get("retired", False))
        # 撤回后不能再拿它签新合同
        with self.assertRaises(DomainError):
            sign_standard_contract(self.dom, contract_id="c2")

    def test_sales_can_append_note_but_not_touch_measurements(self) -> None:
        sign_standard_contract(self.dom)
        self.dom.append_contract_note(role("sal"), contract_id="c1", note_id="n1",
                                      text="学员口头说想加快进度")
        # 销售没有任何路径修改测量
        with self.assertRaises(PermissionDenied):
            self.dom.record_measurement(role("sal"), contract_id="c1", measurement_id="m1",
                                        taken_at="2026-03-05T09:00:00+08:00", weight_kg=80.0)
        # 教练记录的原始测量，销售也无法改写（无更新命令，重标识也被拒）
        self.dom.record_measurement(role("coach"), contract_id="c1", measurement_id="m1",
                                    taken_at="2026-03-05T09:00:00+08:00", weight_kg=80.0)
        with self.assertRaises(DomainError):
            self.dom.record_measurement(role("coach"), contract_id="c1", measurement_id="m1",
                                        taken_at="2026-03-05T09:00:00+08:00", weight_kg=70.0)

    def test_measurements_and_incidents_are_append_only(self) -> None:
        sign_standard_contract(self.dom)
        self.dom.record_measurement(role("coach"), contract_id="c1", measurement_id="m1",
                                    taken_at="2026-03-05T09:00:00+08:00", weight_kg=80.0)
        self.dom.record_safety_incident(role("safety"), contract_id="c1", incident_id="i1",
                                        occurred_at="2026-03-06T09:00:00+08:00",
                                        description="运动后晕厥", severity="serious",
                                        health_attribution=True)
        contract = self.dom.state.contracts["c1"]
        self.assertEqual(len(contract["measurements"]), 1)
        self.assertEqual(len(contract["safety_incidents"]), 1)

    # ---- 指定场景：销售离职 --------------------------------------------

    def test_sales_resignation_keeps_history_but_blocks_new_actions(self) -> None:
        sign_standard_contract(self.dom)
        self.dom.deactivate_staff(ADMIN, "sal")
        # 历史合同仍记录该销售，事件署名仍在
        self.assertEqual(self.dom.state.contracts["c1"]["sales_staff_id"], "sal")
        # 离职后不能再签单、不能再补充说明
        with self.assertRaises(PermissionDenied):
            sign_standard_contract(self.dom, contract_id="c2")
        with self.assertRaises(PermissionDenied):
            self.dom.append_contract_note(role("sal"), contract_id="c1", note_id="n2",
                                          text="离职后试图补写")
        # 离职销售也无法冒充其他角色
        with self.assertRaises(PermissionDenied):
            self.dom.append_contract_note({"id": "sal", "role": "store_manager"},
                                          contract_id="c1", note_id="n3", text="冒充店长")


if __name__ == "__main__":
    unittest.main()
