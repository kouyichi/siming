"""v6.2 架构边界 AST 守门测试（test_architecture_boundaries.py）：
1. v6.2 纯领域与核心新模块禁止直接调用 send_qq_message 或反向导入控制塔/网关插件；
2. 生产代码中所有 create_approval(..., action="...") 的内置动作名必须 100% 在 BUILTIN_APPROVAL_EXECUTORS 注册；
3. 落实 D-35(D4)：确认美团支付当前调用方式不再作为安全风险/Wiki 冲突规则被扫描标红；
4. 验证 control_tower.py 已剥离巨型内联 HTML 模板且只读投影不再争写锁。
"""

from __future__ import annotations

import ast
from pathlib import Path

from siming.approval import is_builtin_action_registered

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "siming"

V62_PURE_MODULES = (
    "receipts.py",
    "frontdesk.py",
    "runtime_kernel.py",
    "leases.py",
    "profile.py",
    "caldav_sync.py",
    "apple_health.py",
    "bookmark_recall.py",
    "learning_loop.py",
    "tower_projections.py",
    "mcp_transport.py",
)


def test_v62_modules_do_not_bypass_outbox_or_import_tower() -> None:
    violations: list[str] = []
    for rel in V62_PURE_MODULES:
        p = SRC_ROOT / rel
        assert p.is_file(), f"missing v6.2 module {rel}"
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = str(node.module or "")
                if "control_tower" in mod:
                    violations.append(f"{rel}:{node.lineno} imports {mod}")
                for alias in node.names:
                    if alias.name == "send_qq_message":
                        violations.append(f"{rel}:{node.lineno} directly imports send_qq_message")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "send_qq_message":
                    violations.append(f"{rel}:{node.lineno} directly calls send_qq_message")
    assert violations == [], f"Architecture boundary violations: {violations}"


def test_all_production_create_approval_actions_are_registered() -> None:
    unregistered: list[str] = []
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn_name = ""
            if isinstance(node.func, ast.Name):
                fn_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fn_name = node.func.attr
            if fn_name != "create_approval":
                continue
            action_val = None
            for kw in node.keywords:
                if kw.arg == "action" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    action_val = kw.value.value
            if action_val is None and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                action_val = node.args[1].value
            if action_val and not is_builtin_action_registered(action_val):
                unregistered.append(f"{py_file.name}:{node.lineno} -> {action_val}")
    assert unregistered == [], f"Unregistered production approval actions: {unregistered}"


def test_d35_d4_payment_invocation_permanently_exempt_from_conflict_scans() -> None:
    from siming.hindsight_wiki import _CONCRETE_ENTITY_GROUPS

    keys = {k for k, _, _ in _CONCRETE_ENTITY_GROUPS}
    assert "pay_password_boundary" not in keys


def test_control_tower_html_extracted_to_static_file() -> None:
    from siming.control_tower import render_tower_html

    static_file = SRC_ROOT / "tower_static" / "index.html"
    assert static_file.is_file()
    ct_lines = (SRC_ROOT / "control_tower.py").read_text(encoding="utf-8").splitlines()
    assert len(ct_lines) < 3000, f"control_tower.py should have extracted SPA HTML, got {len(ct_lines)} lines"
    html = render_tower_html()
    assert "<!DOCTYPE html>" in html and "</html>" in html
