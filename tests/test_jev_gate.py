"""JEV (`typesafe/jev-1.13`) QQ 入站前置过滤门控单元测试与桥接集成测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from siming.db import SimingDB
from siming.jev_gate import (
    JEV_ROUTE_CRITERIA,
    JevGateError,
    evaluate_siming_takeover,
    is_tier0_siming_bypass,
    load_jev_gate_config,
    validate_jev_choice,
)
from siming.modules import set_module_enabled
from siming.router import handle_inbound_qq


class _FakeHTTPResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeHTTPResp":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _jev_resp(
    choice: str,
    probs: dict[str, float],
    *,
    confidence: float = 0.95,
    model: str = "typesafe/jev-1.13-20260917",
) -> dict[str, Any]:
    return {
        "model": model,
        "answers": {
            "route": {
                "type": "choice",
                "choice": choice,
                "probabilities": probs,
                "confidence": confidence,
            }
        },
    }


def test_tier0_bypass_never_calls_jev_network() -> None:
    """D-19 安全口令、状态打卡词、显式编号与引用回复必须 0ms 纯代码直通，绝不依赖外部 JEV 网络。"""
    calls = 0

    def _forbid_open(*_a: Any, **_kw: Any) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("Tier-0 bypass 不应发起任何 JEV HTTP 请求")

    tier0_cases = [
        ("apply A25", "", "", None),
        ("确认支付", "", "", None),
        ("取消 A19", "", "", None),
        ("派单 siming: 补一条测例", "", "", None),
        ("解压码 123456", "", "", None),
        ("睡了", "", "", None),
        ("早安", "", "", None),
        ("吃过了", "", "", None),
        ("把 S12 销掉", "", "", None),
        ("记下 W08", "", "", None),
        ("司命，帮我看下待办", "", "", None),
        ("第1家", "", "", None),
        ("复购第2单", "", "", None),
        ("确认", "msg-1", "美团付款确认卡 apply A12", None),
        ("", "", "", ["/tmp/a.jpg"]),
    ]
    for txt, rid, rtxt, media in tier0_cases:
        ok_b, tag = is_tier0_siming_bypass(txt, reply_to_id=rid, reply_to_text=rtxt, media=media)
        assert ok_b is True and tag, f"未走 Tier-0 直通: {txt!r}"
        dec = evaluate_siming_takeover(
            txt,
            reply_to_id=rid,
            reply_to_text=rtxt,
            media=media,
            api_key="test-key",
            opener=_forbid_open,
        )
        assert dec["verdict"] == "bypass"
        assert dec["should_siming_handle"] is True
    assert calls == 0


def test_jev_protocol_validation_and_failopen_on_bad_responses() -> None:
    """强协议校验拦截模型替换、概率和越界、非 argmax 与网络错误，并安全降级回退本地 NLU。"""
    # 1) 正常响应通过校验
    valid = validate_jev_choice(
        _jev_resp("siming_meituan", {"siming_meituan": 0.96, "siming_other": 0.02, "hermes_chat": 0.02}, confidence=0.94),
        JEV_ROUTE_CRITERIA,
    )
    assert valid["choice"] == "siming_meituan"

    # 2) 非法响应一律抛 JevGateError，evaluate_siming_takeover 捕获并安全降级 fallback
    bad_payloads = [
        _jev_resp("siming_meituan", {"siming_meituan": 0.9, "siming_other": 0.05, "hermes_chat": 0.05}, model="openai/gpt-4o"),
        _jev_resp("siming_meituan", {"siming_meituan": 0.2, "siming_other": 0.1, "hermes_chat": 0.7}),  # choice 非 argmax
        _jev_resp("siming_meituan", {"siming_meituan": 0.6, "siming_other": 0.6, "hermes_chat": 0.0}),  # 概率和 > 1.02
        _jev_resp("siming_meituan", {"siming_meituan": 1.0, "siming_other": 0.0}),  # 缺选项
        {"error": {"message": "rate_limit"}},
    ]
    for bad in bad_payloads:
        with pytest.raises(JevGateError):
            validate_jev_choice(bad, JEV_ROUTE_CRITERIA)
        dec = evaluate_siming_takeover(
            "推荐点晚上吃啥",
            api_key="test-key",
            opener=lambda _req, timeout=3.5: _FakeHTTPResp(bad),
        )
        assert dec["verdict"] == "fallback"
        assert dec["should_siming_handle"] is True

    # 3) HTTP 500 与超时均安全降级
    def _raise_500(_req: Any, timeout: float = 3.5) -> Any:
        raise urllib.error.HTTPError("https://openrouter.ai", 500, "err", {}, io.BytesIO(b"err"))

    d500 = evaluate_siming_takeover("推荐点晚上吃啥", api_key="test-key", opener=_raise_500)
    assert d500["verdict"] == "fallback" and d500["should_siming_handle"] is True and d500["reason"] == "jev_http_500"

    def _raise_to(_req: Any, timeout: float = 3.5) -> Any:
        raise TimeoutError("read timed out")

    dto = evaluate_siming_takeover("推荐点晚上吃啥", api_key="test-key", opener=_raise_to)
    assert dto["verdict"] == "fallback" and dto["should_siming_handle"] is True


def test_three_band_thresholds_and_router_integration(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证三段式阈值：高置信 chat 阻断司命、中置信透传本地 NLU、高置信 siming_meituan 兜底口语只读推荐。"""
    set_module_enabled(sandbox_db, "meituan_agent", True)

    # 1) 高置信 hermes_chat (>= 0.92) -> 即使原文看似带关键词，也被 JEV 前置拦下，不劫持对话
    dec_chat = evaluate_siming_takeover(
        "今天外卖送得好慢啊",
        api_key="test-key",
        opener=lambda _r, timeout=3.5: _FakeHTTPResp(
            _jev_resp("hermes_chat", {"siming_meituan": 0.0, "siming_other": 0.0, "hermes_chat": 1.0}, confidence=1.0)
        ),
    )
    assert dec_chat["verdict"] == "hermes_chat" and dec_chat["should_siming_handle"] is False

    r_blocked = handle_inbound_qq(sandbox_db, "帮我看看待办", jev_decision=dec_chat)
    assert r_blocked["handled"] is False
    assert r_blocked["jev"]["verdict"] == "hermes_chat"

    # 2) 中置信 hermes_chat (0.88 < 0.92，如「今天有什么事」) -> 不确定区，保守放行给司命本地 NLU
    dec_unc = evaluate_siming_takeover(
        "今天有什么事",
        api_key="test-key",
        opener=lambda _r, timeout=3.5: _FakeHTTPResp(
            _jev_resp("hermes_chat", {"siming_meituan": 0.0, "siming_other": 0.12, "hermes_chat": 0.88}, confidence=0.80)
        ),
    )
    assert dec_unc["verdict"] == "uncertain" and dec_unc["should_siming_handle"] is True
    r_pass = handle_inbound_qq(sandbox_db, "今天有什么事", jev_decision=dec_unc)
    assert r_pass["handled"] is True

    # 3) 高置信 siming_meituan (>= 0.78) 兜底全新口语只读推荐（「晚上想吃点热乎的粉」「今天不想做饭了」）
    rec_seen: list[str] = []
    monkeypatch.setattr(
        "siming.meituan_agent.recommend_meituan_meals",
        lambda db, hint="", location_override="", **_kw: (
            rec_seen.append(hint)
            or {
                "ok": True,
                "picks": [{"shop": "测试米粉店"}],
                "reply": "🍽️ **美团外卖推荐（晚餐）**\n1. **测试米粉店** — 约 ¥28\n👉 回 `看看方案` 或 `确认第1家`",
            }
        ),
    )
    dec_mt = evaluate_siming_takeover(
        "晚上想吃点热乎的粉",
        api_key="test-key",
        opener=lambda _r, timeout=3.5: _FakeHTTPResp(
            _jev_resp("siming_meituan", {"siming_meituan": 0.86, "siming_other": 0.02, "hermes_chat": 0.12}, confidence=0.80)
        ),
    )
    assert dec_mt["verdict"] == "siming_meituan" and dec_mt["is_meituan"] is True
    r_mt = handle_inbound_qq(sandbox_db, "晚上想吃点热乎的粉", jev_decision=dec_mt)
    assert r_mt["handled"] is True
    assert rec_seen == ["晚上想吃点热乎的粉"]
    assert r_mt.get("keyboard") is not None

    # 4) 即使 jev_decision 被伪造为 siming_meituan，吐槽/能力追问绝不误触发推荐
    rec_seen.clear()
    r_comp = handle_inbound_qq(sandbox_db, "南京大排档 昨天的不好吃", jev_decision=dec_mt)
    assert r_comp["handled"] is False and rec_seen == []

    # 5) 复用 jev_decision 时 router 不得重复调用 jev_evaluator
    eval_calls = 0

    def _counting_eval(*_a: Any, **_kw: Any) -> dict[str, Any]:
        nonlocal eval_calls
        eval_calls += 1
        return dec_mt

    handle_inbound_qq(sandbox_db, "晚上想吃点热乎的粉", jev_decision=dec_mt, jev_evaluator=_counting_eval)
    assert eval_calls == 0


def test_siming_bridge_jev_prefilter_skips_cli_on_chat_and_elevates_meituan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 siming-bridge 插件：
    1) JEV 判定 hermes_chat -> 直接返回 None，根本不拉起 siming 子进程；
    2) JEV 判定 siming_meituan -> 即使全新口语不在正则里，也抬升 300s 超时并把 jev_decision 透传进 stdin；
    3) 敏感指令 apply A99 -> 绝不调用 JEV，直接走纯代码 fail-closed。
    """
    plugin_py = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("siming_bridge_jev_integration_test", plugin_py)
    assert spec and spec.loader
    pmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pmod)
    assert pmod._JEV_GATE is not None

    cli_calls: list[tuple[float, dict[str, Any]]] = []

    async def _fake_cli(_args: list[str], timeout_sec: float = 25.0, stdin_bytes: Any = None) -> tuple[int, str]:
        payload = json.loads(stdin_bytes.decode("utf-8")) if stdin_bytes else {}
        cli_calls.append((float(timeout_sec), payload))
        return 0, json.dumps({"handled": True, "reply": "OK"}, ensure_ascii=False)

    monkeypatch.setattr(pmod, "_run_siming_cli", _fake_cli)

    # Case 1: JEV 高置信 hermes_chat -> 跳过 CLI
    monkeypatch.setattr(
        pmod._JEV_GATE,
        "evaluate_siming_takeover",
        lambda *_a, **_kw: {
            "should_siming_handle": False,
            "verdict": "hermes_chat",
            "is_meituan": False,
            "probabilities": {"siming_meituan": 0.0, "siming_other": 0.0, "hermes_chat": 1.0},
            "confidence": 1.0,
            "reason": "jev_hermes_chat_reject",
        },
    )
    ret_chat = asyncio.run(
        pmod._on_post_gateway_admission(
            platform="qqbot",
            text="你有自动支付流程",
            message_id="m-chat-1",
            source={"chat_id": "U1"},
        )
    )
    assert ret_chat is None
    assert cli_calls == []

    # Case 2: JEV 判定 siming_meituan -> 抬升 300s 超时 + 透传 jev_decision
    mt_dec = {
        "should_siming_handle": True,
        "verdict": "siming_meituan",
        "is_meituan": True,
        "probabilities": {"siming_meituan": 0.88, "siming_other": 0.02, "hermes_chat": 0.10},
        "confidence": 0.82,
        "reason": "jev_siming_meituan",
    }
    monkeypatch.setattr(pmod._JEV_GATE, "evaluate_siming_takeover", lambda *_a, **_kw: mt_dec)
    ret_mt = asyncio.run(
        pmod._on_post_gateway_admission(
            platform="qqbot",
            text="晚上想吃点热乎的粉",
            message_id="m-mt-1",
            source={"chat_id": "U1"},
        )
    )
    assert ret_mt == {"action": "handled", "reply": "OK"}
    assert len(cli_calls) == 1
    assert cli_calls[0][0] == 300.0
    assert cli_calls[0][1]["jev_decision"]["verdict"] == "siming_meituan"

    # Case 3: 敏感指令 apply A99 永不调 JEV
    jev_called = False

    def _boom(*_a: Any, **_kw: Any) -> Any:
        nonlocal jev_called
        jev_called = True
        raise AssertionError("apply 不应调用 JEV")

    monkeypatch.setattr(pmod._JEV_GATE, "evaluate_siming_takeover", _boom)
    ret_app = asyncio.run(
        pmod._on_post_gateway_admission(
            platform="qqbot",
            text="apply A99",
            message_id="m-app-1",
            source={"chat_id": "U1"},
        )
    )
    assert ret_app == {"action": "handled", "reply": "OK"}
    assert jev_called is False


def test_load_jev_gate_config_reads_yaml_file(tmp_path: Path) -> None:
    p = tmp_path / "policy.yaml"
    p.write_text(
        "other: 1\njev_gate:\n  enabled: false\n  timeout_sec: 2.5\n  chat_reject_min_prob: 0.95\nnext_key: 2\n",
        encoding="utf-8",
    )
    cfg = load_jev_gate_config(policy_path=p)
    assert cfg["enabled"] is False
    assert cfg["timeout_sec"] == 2.5
    assert cfg["chat_reject_min_prob"] == 0.95
