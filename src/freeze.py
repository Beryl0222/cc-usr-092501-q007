"""角色分权。

调解、退款批准、监管答复三类处置动作必须由不同角色完成；业绩人员（销售/教练）
只能追加补充说明，不能改写原始测量或安全事件（该约束在 :mod:`src.services` 中
配合只追加日志落实）。
"""

from __future__ import annotations

from typing import Any

# ---- 角色 -----------------------------------------------------------------

ROLE_SALES = "sales"            # 业绩人员：销售
ROLE_COACH = "coach"            # 业绩人员：教练（测量执行人）
ROLE_SPECIALIST = "specialist"  # 争议专员：调解
ROLE_APPROVER = "approver"      # 退款批准人
ROLE_REGULATOR = "regulator"    # 监管答复人
ROLE_CONSUMER = "consumer"      # 消费者

PERFORMANCE_ROLES = frozenset({ROLE_SALES, ROLE_COACH})


class RoleError(PermissionError):
    """当前角色无权执行该处置动作（分权约束）。"""


# 每个处置动作允许的角色；三个关键动作刻意互不重叠，且禁止同一自然人跨角色办理。
_ACTION_ROLES: dict[str, frozenset[str]] = {
    "mediate": frozenset({ROLE_SPECIALIST}),
    "approve_refund": frozenset({ROLE_APPROVER}),
    "file_regulatory_response": frozenset({ROLE_REGULATOR}),
    "close_case": frozenset({ROLE_SPECIALIST}),
}


def require_role(action: str, actor_role: str) -> None:
    allowed: Any = _ACTION_ROLES.get(action)
    if allowed is None or actor_role not in allowed:
        raise RoleError(f"角色 {actor_role} 无权执行动作 {action}（允许：{sorted(allowed or [])}）")
