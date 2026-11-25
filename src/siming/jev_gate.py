"""JEV (`typesafe/jev-1.13`) QQ 入站前置过滤门控（jev_gate.py）。

职责（严格守住 D-19 纯代码安全红线与零阻塞底线）：
1. **Tier-0 纯代码直通（绝不依赖外部网络）**：
   - `apply Axx` / `确认支付` / `取消 Axx` / 引用审批或单据回复 / `派单` / `密码/解压码` / 密钥 / 显式编号（`Sxx`/`Wxx`/`TX-`/`SUB-`/`R-`）/ 精确状态打卡词（`睡了`/`早安`/`吃过了` 等）与纯媒体投递，0ms 直通司命纯代码路由，永不因网络异常失效。
2. **单题亚秒级 JEV 判别（`typesafe/jev-1.13` via OpenRouter Decisions API）**：
   - 仅发 1 道 3 选 1 原子决策题（`siming_meituan` / `siming_other` / `hermes_chat`），实测 ~0.9s、约 $0.000018/次。
   - 强协议校验：served model 前缀、选项完备性、有限概率 `[0, 1]`、概率和误差 ≤ 0.02、`choice` 严格为 argmax。
3. **三段式校准阈值 + 故障安全降级**：
   - 高置信 `hermes_chat`（`prob >= chat_reject_min_prob`，默认 `0.92`）：直接拦截，不进司命、原样交由 Hermes 通用对话，消除误劫持；
   - 高置信 `siming_meituan`（`prob >= siming_takeover_min_prob`，默认 `0.72`）：标记 `is_meituan=True`，在 `siming-bridge` 抬升 300s 长任务超时并支持 Layer 3 只读推荐兜底；
   - 中置信不确定区或任何网络/超时/校验异常：保守降级为 `should_siming_handle=True`，回退司命本地确定性 NLU，绝不阻断 QQ 消息。

注：本模块仅依赖 Python 标准库，可同时被 `siming`（pixi Python 3.12）与 `siming-bridge`（Hermes Gateway Python 3.14）零重复加载。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

MODEL = "typesafe/jev-1.13"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

DEFAULT_JEV_GATE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "model": MODEL,
    "endpoint": ENDPOINT,
    "timeout_sec": 3.5,
    "chat_reject_min_prob": 0.92,
    "siming_takeover_min_prob": 0.72,
    "meituan_readonly_rescue_min_prob": 0.78,
}

JEV_ROUTE_INSTRUCTIONS = (
    "判断用户的 QQ 消息是否应该交由本地个人生活助理「司命（siming）」直接接手执行，还是交由通用对话 AI（hermes）自由聊天/编程/讨论。\n"
    "核心判别原则：\n"
    "1. 只有当用户是在直接下达个人生活助理指令或查询本机个人台账时，才选司命对应类别。\n"
    "2. 如果用户是在：写代码/排障/修 bug/讨论架构或优化、询问系统能力或原理（「你有自动支付流程」「什么是订阅刺客」「路由逻辑怎么设计」）、"
    "闲聊/吐槽/叙事/转述他人/解释背景（「昨天那个不好吃」「外卖送得好慢」「我朋友说示例粉面不错」「今天加班真累，但已经吃过了」「晚饭吃完了」「我说的今天指的是周三」）、"
    "无明确个人助理执行目标的通用动作（「把灯关掉」「帮我点赞」「来一份月度报告」「确认一下明天的会议」）、"
    "或者询问与个人助理无关的知识/推荐（开源项目、感冒吃什么药、猫吃什么），一律选 hermes_chat。\n"
    "3. 餐饮/外卖点单、复购、加菜改单、要外卖推荐、不想做饭、查历史常点菜单，必须明确选 siming_meituan。"
)

JEV_ROUTE_CRITERIA: dict[str, str] = {
    "siming_meituan": (
        "用户正在要求美团/瑞幸外卖点餐、复购历史订单（含改菜/加菜/减菜，如「南京大排档吧 昨天的加上芫荽那个凉菜」「再来一单」「老样子」）、"
        "询问现在/今晚/中午吃什么或求外卖推荐（如「推荐点晚上吃啥」「饿了不知道吃啥」「不想做饭了」「帮我找个之前买过的菜单」）、"
        "选定推荐列表第几家、跨店双拼、或确认/取消美团点餐。"
    ),
    "siming_other": (
        "用户正在直接向个人助理司命下达其他本机助理指令或查询个人数据：滴滴打车/查打车状态、查账/花销/报销/回款/净资产、"
        "待办/快递取件码/销账（如「把S12销掉」「取了」）、设提醒/挂起任务（如「提醒我明天开会」「帮我记一下周六买菜」）、"
        "打针登记/耗材余量/照护（如「我刚打完针了」「口罩还剩多少」）、特斯拉电量/备车/开空调、新房送装/甲醛/水电图/售后护照、"
        "京东保价/常买日用品补货、算力余额/订阅体检/NAS/3D打印/模块开关、晨札/今日安排、睡了/醒了/吃过了状态打卡、查本机个人笔记资料（如「查一下次卧窗帘盒多宽」）。"
    ),
    "hermes_chat": (
        "普通闲聊、情绪吐槽、写代码/技术问答/概念科普/架构讨论/修 bug（如「今天点餐没成功」「南京大排档昨天的不好吃」「外卖送得好慢」「这块以后再优化」）、"
        "询问助手有没有某功能（「你有自动支付流程」）、已吃完饭的陈述或长叙事（「晚饭吃完了」「今天加班真累，但已经吃过了」「昨天去三里屯吃的火锅不错」）、"
        "非外卖推荐（开源项目/电影/书）、医药或宠物吃什么、转述或解释上下文（「我说的今天指的是周三，一般是每周三打针」）、"
        "通用办公/非司命动作（「把灯关掉」「帮我点一下赞」「来一份月度报告」「确认一下明天的会议」），或意图不明确需要自由对话回复。"
    ),
}

_VOICE_PREFIX_RE = re.compile(r"^\s*\[Voice\]\s*", re.IGNORECASE)
_TRAIL_PUNCT_RE = re.compile(r"[。！!~～\s]+$")

_TIER0_SENSITIVE_RE = re.compile(
    r"^(?:/?apply\s+A\d{2,6}\b|"
    r"(?:(?:帮我|请)?(?:确认支付|确认付款|确认下单|去支付|立即支付|确认并支付)(?:\s+A\d{2,6})?(?:吧)?)$|"
    r"(?:支付|付款|取消|放弃|作废)\s+A\d{2,6}$|"
    r"(?:取消支付|取消付款|取消订单|放弃下单|取消点单|不点了)$|"
    r"派单\s+|"
    r"(?:密码|解压码)\s*[：:]?\s*[A-Za-z0-9]{4,16}$|"
    r"(?:滴滴密钥|滴滴key|瑞幸密钥|瑞幸token|luckin[_\-\s]*key|绑定手机号|授权手机号)\s*[：:]?\s*\S+$|"
    r"(?:特斯拉码\s*)?\d{6}$|"
    r"/siming\b)",
    re.IGNORECASE,
)

_TIER0_EXPLICIT_ID_RE = re.compile(
    r"\b(?:A\d{2,6}|S\d{2,6}|W\d{2,6}|BND-[A-Z0-9]{4,16}|TX-[0-9A-Za-z-]+|SUB-[0-9A-Za-z-]+|R-[0-9A-Za-z_-]+|CW0[18]-[A-Z]+-\d{12})\b",
    re.IGNORECASE,
)

_TIER0_QUOTE_MARKER_RE = re.compile(
    r"(?:\b(?:apply\s+)?A\d{2,6}\b|\bBND-[A-Z0-9]{4,16}\b|\b[SW]\d{2,6}\b|RPL:[A-Za-z0-9_-]+|晨札|外箱初验|加密账单|解压码|确认卡|付款许可)",
    re.IGNORECASE,
)

_TIER0_EXACT_KEYWORDS: frozenset[str] = frozenset(
    {
        "睡了",
        "晚安",
        "准备睡了",
        "我要睡了",
        "醒了",
        "早",
        "早安",
        "起床了",
        "吃过了",
        "吃饱了",
        "今天",
        "晨札",
        "全文",
        "晨札全文",
        "待办",
        "模块",
        "保价",
        "常买",
        "照护",
        "挂起",
        "规律",
        "收藏夹",
        "十秒续工",
        "历史菜单",
        "常点菜单",
        "看看方案",
        "看方案",
        "外卖推荐",
        "晚餐推荐",
        "午餐推荐",
        "夜宵推荐",
        "美团推荐",
        "确认",
        "同意",
        "合入",
        "备车",
        "关空调",
        "特斯拉",
        "算力资产",
        "订阅体检",
        "报销游标",
        "待回款",
        "送装清单",
        "外箱初验",
        "现场作战单",
    }
)

_TIER0_SHORT_PICK_RE = re.compile(
    r"^(?:确认|就点|就吃|就选|那就|直接点|看看|复购|再来一单)?第?\s*[1-5一二三四五]\s*(?:家|个|号|单)(?:吧)?$"
)


class JevGateError(RuntimeError):
    """脱敏错误对象（绝不泄露 Authorization 或响应体密文）。"""

    def __init__(self, code: str, *, status: Optional[int] = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _strip_text(raw: str) -> str:
    s = _VOICE_PREFIX_RE.sub("", (raw or "").strip()).strip()
    return _TRAIL_PUNCT_RE.sub("", s).strip()


def is_tier0_siming_bypass(
    text: str,
    *,
    reply_to_id: str = "",
    reply_to_text: str = "",
    media: Optional[list[str]] = None,
) -> tuple[bool, str]:
    """判断消息是否属于 Tier-0 纯代码直通指令（0ms 跳过 JEV，永不因外部 API 故障失效）。"""
    clean = _strip_text(text)
    if not clean and media:
        return True, "media_only"
    if not clean:
        return False, ""
    if _TIER0_SENSITIVE_RE.search(clean):
        return True, "sensitive_cmd"
    if reply_to_id or (reply_to_text and _TIER0_QUOTE_MARKER_RE.search(reply_to_text)):
        return True, "quoted_siming_ctx"
    if _TIER0_EXPLICIT_ID_RE.search(clean):
        return True, "explicit_id"
    if clean.startswith(("司命", "小司命")) and len(clean) <= 36:
        return True, "explicit_address_siming"
    if clean in _TIER0_EXACT_KEYWORDS:
        return True, "exact_keyword"
    if _TIER0_SHORT_PICK_RE.fullmatch(clean):
        return True, "short_pick_index"
    return False, ""


def _read_policy_jev_section(policy_path: Optional[Path] = None) -> dict[str, Any]:
    """轻量读取 `~/.config/siming/policy.yaml` 的 `jev_gate` 配置（无 PyYAML 时纯 stdlib 降级解析）。"""
    p = policy_path or (Path.home() / ".config" / "siming" / "policy.yaml")
    if not p.is_file():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text) or {}
        sec = data.get("jev_gate")
        return dict(sec) if isinstance(sec, dict) else {}
    except Exception:
        pass

    # 无 PyYAML 环境下的轻量块解析（供 Hermes Gateway 插件直接复用）
    out: dict[str, Any] = {}
    in_sec = False
    for line in text.splitlines():
        if re.match(r"^jev_gate\s*:\s*$", line):
            in_sec = True
            continue
        if in_sec:
            if line and not line[0].isspace():
                break
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            k, v_raw = stripped.split(":", 1)
            k = k.strip()
            v_s = v_raw.split("#", 1)[0].strip().strip("'\"")
            low = v_s.lower()
            if low in ("true", "yes", "on"):
                out[k] = True
            elif low in ("false", "no", "off"):
                out[k] = False
            else:
                try:
                    out[k] = float(v_s) if "." in v_s else int(v_s)
                except ValueError:
                    out[k] = v_s
    return out


def load_jev_gate_config(
    policy: Optional[dict[str, Any]] = None,
    *,
    policy_path: Optional[Path] = None,
) -> dict[str, Any]:
    cfg = dict(DEFAULT_JEV_GATE_CONFIG)
    raw_sec: dict[str, Any] = {}
    if isinstance(policy, dict) and isinstance(policy.get("jev_gate"), dict):
        raw_sec = dict(policy["jev_gate"])
    elif policy is None:
        raw_sec = _read_policy_jev_section(policy_path=policy_path)
    for k, v in raw_sec.items():
        if v is not None:
            cfg[k] = v
    return cfg


def resolve_openrouter_api_key(env_path: Optional[Path] = None) -> str:
    """仅从环境变量或 `~/.hermes/.env` 只读提取 `OPENROUTER_API_KEY`（绝不打印或落盘）。"""
    env_k = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env_k:
        return env_k
    ep = env_path or (Path.home() / ".hermes" / ".env")
    if not ep.is_file():
        return ""
    try:
        for line in ep.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() == "OPENROUTER_API_KEY":
                return v.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def validate_jev_choice(
    response: Any,
    criteria: dict[str, str],
    *,
    question: str = "route",
    model: str = MODEL,
) -> dict[str, Any]:
    """严格校验 JEV 响应协议：模型前缀、选项全覆盖、合法概率分布与 argmax 一致性。"""
    if not isinstance(response, dict) or response.get("error"):
        raise JevGateError("invalid_response")
    served = str(response.get("model") or "")
    if not (served == model or served.startswith(model + "-")):
        raise JevGateError("unexpected_model")
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise JevGateError("missing_answers")
    answer = answers.get(question)
    if not isinstance(answer, dict):
        raise JevGateError("missing_question")
    probabilities = answer.get("probabilities")
    if (
        answer.get("type") != "choice"
        or answer.get("choice") not in criteria
        or not isinstance(probabilities, dict)
        or set(probabilities.keys()) != set(criteria.keys())
    ):
        raise JevGateError("invalid_choice_or_distribution")
    vals = list(probabilities.values()) + [answer.get("confidence")]
    if any(type(v) not in (int, float) or not math.isfinite(v) or not (0.0 <= float(v) <= 1.0) for v in vals):
        raise JevGateError("invalid_probability")
    if abs(sum(float(v) for v in probabilities.values()) - 1.0) > 0.02:
        raise JevGateError("probability_sum")
    if float(probabilities[answer["choice"]]) + 1e-6 < max(float(v) for v in probabilities.values()):
        raise JevGateError("choice_is_not_argmax")
    return {
        "choice": str(answer["choice"]),
        "confidence": float(answer["confidence"]),
        "probabilities": {str(k): float(v) for k, v in probabilities.items()},
        "model": served,
    }


def evaluate_siming_takeover(
    text: str,
    *,
    reply_to_id: str = "",
    reply_to_text: str = "",
    media: Optional[list[str]] = None,
    policy: Optional[dict[str, Any]] = None,
    policy_path: Optional[Path] = None,
    api_key: Optional[str] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """用 `typesafe/jev-1.13` 先过滤一遍 QQ 消息是否应由司命接手。

    返回字典包含：
    - `should_siming_handle`: bool（仅在高置信 `hermes_chat` 时为 False；异常/离线/不确定时保守回退 True 交给本地 NLU）
    - `verdict`: `bypass` | `siming_meituan` | `siming_other` | `hermes_chat` | `uncertain` | `fallback`
    - `is_meituan`: bool
    - `probabilities`: dict[str, float]
    - `confidence`: float
    - `model`: str
    - `latency_ms`: int
    - `reason`: str
    """
    bypass, tag = is_tier0_siming_bypass(
        text,
        reply_to_id=reply_to_id,
        reply_to_text=reply_to_text,
        media=media,
    )
    if bypass:
        return {
            "should_siming_handle": True,
            "verdict": "bypass",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 1.0,
            "model": "",
            "latency_ms": 0,
            "reason": f"tier0:{tag}",
        }

    clean_msg = (text or "").strip()
    if not clean_msg:
        return {
            "should_siming_handle": False,
            "verdict": "hermes_chat",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 1.0,
            "model": "",
            "latency_ms": 0,
            "reason": "empty_text",
        }

    cfg = load_jev_gate_config(policy=policy, policy_path=policy_path)
    if not bool(cfg.get("enabled", True)):
        return {
            "should_siming_handle": True,
            "verdict": "fallback",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 0.0,
            "model": "",
            "latency_ms": 0,
            "reason": "disabled_by_policy",
        }

    if opener is None and (
        os.environ.get("SIMING_TEST_OFFLINE") == "1" or os.environ.get("SIMING_DISABLE_JEV") == "1"
    ):
        return {
            "should_siming_handle": True,
            "verdict": "fallback",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 0.0,
            "model": "",
            "latency_ms": 0,
            "reason": "offline_disabled",
        }

    key = (api_key if api_key is not None else resolve_openrouter_api_key()).strip()
    if not key:
        return {
            "should_siming_handle": True,
            "verdict": "fallback",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 0.0,
            "model": "",
            "latency_ms": 0,
            "reason": "no_api_key",
        }

    model = str(cfg.get("model") or MODEL)
    endpoint = str(cfg.get("endpoint") or ENDPOINT)
    timeout_sec = max(1.0, min(10.0, float(cfg.get("timeout_sec") or 3.5)))
    chat_reject_min = float(cfg.get("chat_reject_min_prob") or 0.92)
    siming_min = float(cfg.get("siming_takeover_min_prob") or 0.72)

    state: dict[str, Any] = {"qq_message": clean_msg[:500]}
    if reply_to_text:
        state["quoted_message_excerpt"] = str(reply_to_text).strip()[:200]

    payload = {
        "model": model,
        "state": state,
        "questions": {
            "route": {
                "type": "choice",
                "instructions": JEV_ROUTE_INSTRUCTIONS,
                "criteria": JEV_ROUTE_CRITERIA,
            }
        },
    }

    t0 = time.monotonic()
    open_fn = opener or urllib.request.urlopen
    try:
        req = urllib.request.Request(
            endpoint,
            data=_canonical_json(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with open_fn(req, timeout=timeout_sec) as resp:
            raw_body = resp.read()
        data = json.loads(raw_body.decode("utf-8")) if isinstance(raw_body, (bytes, bytearray)) else json.loads(raw_body)
        ans = validate_jev_choice(data, JEV_ROUTE_CRITERIA, question="route", model=model)
    except urllib.error.HTTPError as exc:
        return {
            "should_siming_handle": True,
            "verdict": "fallback",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 0.0,
            "model": "",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": f"jev_http_{exc.code}",
        }
    except JevGateError as exc:
        return {
            "should_siming_handle": True,
            "verdict": "fallback",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 0.0,
            "model": "",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": f"jev_{exc.code}",
        }
    except Exception:
        return {
            "should_siming_handle": True,
            "verdict": "fallback",
            "is_meituan": False,
            "probabilities": {},
            "confidence": 0.0,
            "model": "",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": "jev_transport_error",
        }

    latency_ms = int((time.monotonic() - t0) * 1000)
    choice = ans["choice"]
    probs = ans["probabilities"]
    conf = ans["confidence"]
    served_model = ans["model"]

    if choice == "hermes_chat" and probs.get("hermes_chat", 0.0) >= chat_reject_min:
        return {
            "should_siming_handle": False,
            "verdict": "hermes_chat",
            "is_meituan": False,
            "probabilities": probs,
            "confidence": conf,
            "model": served_model,
            "latency_ms": latency_ms,
            "reason": "jev_hermes_chat_reject",
        }

    if choice == "siming_meituan" and probs.get("siming_meituan", 0.0) >= siming_min:
        return {
            "should_siming_handle": True,
            "verdict": "siming_meituan",
            "is_meituan": True,
            "probabilities": probs,
            "confidence": conf,
            "model": served_model,
            "latency_ms": latency_ms,
            "reason": "jev_siming_meituan",
        }

    if choice == "siming_other" and probs.get("siming_other", 0.0) >= siming_min:
        return {
            "should_siming_handle": True,
            "verdict": "siming_other",
            "is_meituan": False,
            "probabilities": probs,
            "confidence": conf,
            "model": served_model,
            "latency_ms": latency_ms,
            "reason": "jev_siming_other",
        }

    return {
        "should_siming_handle": True,
        "verdict": "uncertain",
        "is_meituan": choice == "siming_meituan",
        "probabilities": probs,
        "confidence": conf,
        "model": served_model,
        "latency_ms": latency_ms,
        "reason": "jev_uncertain_passthrough",
    }
