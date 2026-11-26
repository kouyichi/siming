"""W3-2 · Hindsight 自动共享 Wiki（Tell one agent, every agent knows）。

受 Linger Hindsight 启发：
- 离线扫描全机共享记忆（`60-共享记忆/*.md`、`inbox/`）、核心原则拍板记录（`docs/DECISIONS.md`）、
  行为规则提案（`_规则待审.md`）、DB 记忆（`routines.md` / `hypotheses` / `curiosity` / `feedback` / `events`）
  与多 Agent 会话日志；
- 自动抽取四类长期知识：`preference`（偏好）、`decision`（架构决策）、`taboo`（项目红线/禁区）、
  `verified_fix`（已验证根因修复），完整保留未截断原始出处（verbatim provenance）与真实日期；
- 高精度检测真实矛盾、跨时间覆写与待重新拍板的原则冲突（剔除署名误匹配、OCR 购物截图等噪音）；
- 在交给用户裁决前，自动提炼人话「裁决简报（decision_brief）」（裁决问题、核心分歧、双方精炼主张、
  具体按钮文案与推荐理由），支持本机快速 LLM 语义核验与 SQLite 持久化缓存，离线/单测零依赖确定性降级；
- 编译写入 Obsidian Vault（`shared_memory_dir / "hindsight-wiki"`）与 SQLite `wiki_pages` 表。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from siming import db as siming_db
from siming.crypto import contains_otp, redact_sensitive
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, parse_iso
from siming.paths import SimingPaths, _reject_slash_tmp

_EXTRA_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED-KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b"), "[REDACTED-JWT]"),
    (
        re.compile(
            r"((?:支付密码|交易密码|取款密码|银行卡密码|密保|卡密|PIN码?|payment\s*password)[\s:：为是=]*)\d{4,8}\b",
            re.IGNORECASE,
        ),
        r"\1[REDACTED-PWD]",
    ),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED-PHONE]"),
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        "[REDACTED-PRIVATE-KEY]",
    ),
)
_FRONTMATTER_KEY_RE = re.compile(
    r"^(name|type|title|description|related|tags|aliases|updated|created|date|source|node_type|source_agent|originSessionId|modified|metadata)\s*[:：]",
    re.IGNORECASE,
)
_BROKEN_BOLD_LABEL_RE = re.compile(r"^(?:how\s+to\s+apply|why)\s*[:：]\s*\*{0,2}\s*", re.IGNORECASE)
_MD_LIST_PREFIX_RE = re.compile(r"^(?:[-•]\s+|\*\s+|#+\s+|\d+[.)]\s+)+")
_UPDATE_FOOTER_RE = re.compile(
    r"^[（(]\s*20\d{2}-\d{2}-\d{2}[^）)]{0,80}(?:记录|更新|补录|整理)[^）)]{0,80}[）)]\.?$"
)
_RELATED_LINE_RE = re.compile(r"^(?:Related|相关(?:卡片)?)\s*[:：]", re.IGNORECASE)
_OCR_ORDER_NOISE_RE = re.compile(
    r"(?:全部订单[\s\S]{0,120}(?:待收货|待发货|运送中|确认收货|包裹工具|申请售后)"
    r"|(?:删除订单|再次购[买头]|默认不可退)[\s\S]{0,80}(?:合计|实付款|去评价|申请售后|确认收货)"
    r"|^\d{2}:\d{2}\s*\n(?:[\d+]+\s*\n)?Q\s*商品名)",
    re.IGNORECASE,
)


def _sanitize_wiki_text(text: str, paths: Optional[SimingPaths] = None) -> str:
    """对写入 Wiki 的事实与出处执行严格脱敏（API Key / Bearer / JWT / OTP / 支付密码 / 手机号 / 本地 secrets）。"""
    if not text:
        return ""
    out = redact_sensitive(text, paths=paths)
    for pat, repl in _EXTRA_SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _clean_markdown_line(line: str) -> str:
    """清理列表符号与标题井号，规范化 How to apply / Why 标签，避免出现残缺加粗 `How to apply:**`。"""
    s = (line or "").strip()
    s = _MD_LIST_PREFIX_RE.sub("", s).strip()
    if s.startswith("**"):
        m_bold = re.match(r"^\*\*([^*]{1,40}[:：])\*\*\s*(.+)$", s)
        if m_bold:
            s = f"{m_bold.group(1)} {m_bold.group(2)}".strip()
    s = _BROKEN_BOLD_LABEL_RE.sub("", s).strip()
    s = re.sub(r"\*{0,2}\bHow\s+to\s+apply\s*[:：]\*{0,2}\s*", "执行方式：", s, flags=re.IGNORECASE)
    s = re.sub(r"\*{0,2}\bWhy\s*[:：]\*{0,2}\s*", "原因：", s, flags=re.IGNORECASE)
    return s


def _strip_markdown_for_human(text: str) -> str:
    """将 Markdown 标记清理为干净人话文本，用于摘要与裁决简报展示。"""
    s = (text or "").strip()
    s = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_noise_utterance(text: str) -> bool:
    """过滤电商订单列表 OCR 截图、纯更新日期注脚、Related 链接行等非长期事实噪音。"""
    s = (text or "").strip()
    if not s or len(s) < 4:
        return True
    if _FRONTMATTER_KEY_RE.match(s):
        return True
    if _UPDATE_FOOTER_RE.match(s):
        return True
    if _RELATED_LINE_RE.match(s):
        return True
    if _OCR_ORDER_NOISE_RE.search(s):
        return True
    # 超过 8 行且含大量超短 UI 碎片行的多半是手机截屏 OCR 垃圾
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if len(lines) >= 8:
        short_lines = sum(1 for ln in lines if len(ln) <= 8)
        if short_lines / len(lines) >= 0.6:
            return True
    return False


CATEGORY_META: dict[str, tuple[str, str]] = {
    "preference": ("preferences", "用户偏好与工程习惯 (Preferences)"),
    "decision": ("decisions", "架构与技术决策 (Decisions)"),
    "taboo": ("taboos", "项目禁区与红线 (Taboos)"),
    "verified_fix": ("verified-fixes", "已验证根因与修复 (Verified Fixes)"),
}

TOPIC_LABELS: dict[str, str] = {
    "tmp_directory": "临时目录规范",
    "package_manager": "包管理工具",
    "test_runner": "测试工具与习惯",
    "database": "数据库选型",
    "proxy_dns": "网络代理与分流",
    "model_routing": "AI 模型与网关",
    "theme_ui": "界面主题偏好",
    "git_workflow": "Git 工作流",
    "approval_policy": "审批与安全红线",
    "price_watch": "网购保价策略",
    "meituan_order": "美团外卖下单",
    "appliance_buy": "新房家电选购",
    "home_network": "新房网络规划",
    "push_policy": "通知与推送策略",
    "ui_interaction": "交互界面形式",
    "x_monitor": "X / 社交监控",
    "dev_cli": "本机开发与终端",
    "policy_conflict": "核心原则待拍板",
    "rule_proposal": "行为规则提案",
    "general": "日常与工程约定",
}

_TABOO_RE = re.compile(
    r"(禁止|严禁|绝不|千万别|不要用|禁写|切忌|避坑|不许(?:擅自)?|do\s+not\s+use|never\s+use|forbidden|must\s+not|banned)",
    re.IGNORECASE,
)
_FIX_RE = re.compile(
    r"(已修复|根因是|根因在于|实测有效|最终修复|解决办法|解决方案|修好了|verified\s+fix|root\s+cause|fixed\s+by|workaround\s+works|resolved\s+by)",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"(决定采用|架构选型|统一使用|方案定为|约定使用|切换为|采用|拍板|定稿|已定|最终方案|"
    r"新决定|最新要求|覆盖(?:上文|旧|八月)|覆写|作废|撤回.{0,14}授权|授权[「“\"『]|取消.{0,12}(?:限制|约束|单)|"
    r"不再考虑|改为|改定|取代|初步定下|已下单|扩权|废除|去掉.{0,16}换成|降为替补|"
    r"规划结论|终版关键裁决|不做网页看板|"
    r"decided\s+to|architecture\s+decision|we\s+will\s+use|standardize\s+on)",
    re.IGNORECASE,
)
_PREF_RE = re.compile(
    r"(偏好|喜欢用|习惯用|优先(?:用|直航|走|选|考虑)?|默认(?:用|走|选|开|关)?|风格要求|尽量(?:用|走|选)?|"
    r"还是先|改用|以后.{0,12}(?:都|优先|还是|先走|直接|改用|不要|别)|prefer|always\s+use|favor\s+|default\s+to)",
    re.IGNORECASE,
)

_NEGATIVE_POLARITY_RE = re.compile(
    r"(禁止|严禁|不要|绝不|避免|关闭|禁用|取消|撤回|作废|不再|废除|不许|不允许|停用|"
    r"不做|不外发|do\s+not|don't|never|avoid|disable|forbid|ban|no\s+longer)",
    re.IGNORECASE,
)
_POSITIVE_POLARITY_RE = re.compile(
    r"(启用|开启|偏好|喜欢|优先|采用|统一使用|默认|必须使用|还是先|先走|改用|允许|授权|支持|"
    r"prefer|always\s+use|enable|use\s+|adopt)",
    re.IGNORECASE,
)
_OVERRIDE_SIGNAL_RE = re.compile(
    r"(覆盖(?:上文|旧|八月)|覆写|作废|撤回.{0,14}授权|取消.{0,12}(?:限制|约束|单)|不再考虑|"
    r"取代|改定|改为|改用|扩权|废除|降为替补|去掉.{0,16}换成|停用|已取消|已淘汰|推翻|新决定|最新要求|定稿型号)",
    re.IGNORECASE,
)

# 剥离笔记署名/工具流转注脚，避免 `（claude）`/`（codex）` 把家电/网卡/价保笔记误判为 model_routing
_ATTRIBUTION_STRIP_RE = re.compile(
    r"(?:[（(]\s*(?:20\d{2}-\d{2}-\d{2}[，,\s]*)?(?:claude|codex|hermes|agy|pi|gemini)(?:\s*(?:记录|更新|补))?\s*[）)]"
    r"|(?:20\d{2}-\d{2}-\d{2}[，,\s]+)(?:claude|codex|hermes|agy)\b"
    r"|claude-[a-z0-9_-]+\.service"
    r"|Codex\s+Astra\s*摸底|Claude\s*起草\s*\+\s*`?codex\s+exec`?)",
    re.IGNORECASE,
)

# 规范化常见工程与生活主题（具体业务域优先于宽泛工具域，防止误落入 model_routing）
_TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tmp_directory", re.compile(r"(/tmp\b|agent-tmp|TMPDIR|临时目录)", re.IGNORECASE)),
    ("package_manager", re.compile(r"\b(pixi|uv|poetry|pip|conda|pnpm|npm|yarn|bun)\b", re.IGNORECASE)),
    ("test_runner", re.compile(r"\b(pytest|unittest|vitest|jest|frozen_clock)\b", re.IGNORECASE)),
    ("database", re.compile(r"\b(sqlite|postgres|postgresql|mysql|wal|duckdb)\b", re.IGNORECASE)),
    ("theme_ui", re.compile(r"(dark\s+mode|light\s+mode|深色模式|浅色模式|界面主题|主题切换)", re.IGNORECASE)),
    ("git_workflow", re.compile(r"\b(worktree|rebase|merge|force\s+push|squash)\b", re.IGNORECASE)),
    ("price_watch", re.compile(r"(价保|保价|auto_claim|buy-price-watch)", re.IGNORECASE)),
    ("meituan_order", re.compile(r"(美团|poi_id_str|搜索页比价|直接进店|外卖出餐|外卖下单)", re.IGNORECASE)),
    (
        "appliance_buy",
        re.compile(
            r"(扫地机|洗衣机|热水器|烟灶|烟机|燃气表|微蒸烤|浴霸|电视|冰箱|米家\s*MJ|老板双子星|凯度|科沃斯|林内|西门子.*洗烘|LG\s*小旋风|98\s*寸|85\s*寸)",
            re.IGNORECASE,
        ),
    ),
    (
        "home_network",
        re.compile(
            r"(RB5009|ER3228|TL-SE\d+|TL-AC100|7AP5100|光猫|弱电箱|小机柜|24U|R740|VLAN|路由器|核心交换|面板\s*AP)",
            re.IGNORECASE,
        ),
    ),
    ("push_policy", re.compile(r"(主动推送|每日\s*5\s*条|小时熔断|推送上限|隐式降级|晨札)", re.IGNORECASE)),
    (
        "ui_interaction",
        re.compile(r"(网页看板|控制塔|内嵌键盘|Inline\s*Keyboard|QQ\s*原生.*按钮|纯文本问句)", re.IGNORECASE),
    ),
    (
        "approval_policy",
        re.compile(
            r"(\bA2\b|\bA3\b|审批|支付密码|代输密码|自动执行|白名单|auto-approve|验证码.*(?:手机|外发)|瞬时加密保险箱)",
            re.IGNORECASE,
        ),
    ),
    ("x_monitor", re.compile(r"(x-dm-watch|XChat|Limcc55|Lim66cc|私信监控)", re.IGNORECASE)),
    ("proxy_dns", re.compile(r"(sing-box|atrust|hysteria2|7890|9090|分流重构|代理\s*VLAN|NXDOMAIN|\bdns\b)", re.IGNORECASE)),
    (
        "model_routing",
        re.compile(
            r"(magpie|opus\s*5|sonnet\s*5|haiku\s*5|factory/droid|nousportal|回退组|供应商|主模型|权限分类器|"
            r"kimi\s*cli|kimi-code|deepseek\s*v\d|ollama|redpill|pi\s+runtime|model-map|affinity)",
            re.IGNORECASE,
        ),
    ),
    ("dev_cli", re.compile(r"(herdr|\bagy\b|ultracode|输入回显|dsh-purge|dsh-web|hypr-xwayland|omarchy|voxtype)", re.IGNORECASE)),
)

# 单一明确主题：同主题直接可判极性/选型矛盾；其余宽主题要求共享具体实体，杜绝笛卡尔积假冲突
_NARROW_TOPICS = frozenset(
    {
        "tmp_directory",
        "package_manager",
        "test_runner",
        "database",
        "theme_ui",
        "git_workflow",
        "meituan_order",
        "push_policy",
        "ui_interaction",
    }
)

_CHOICE_TOKENS: dict[str, tuple[str, ...]] = {
    "package_manager": ("pixi", "uv", "poetry", "pip", "conda", "pnpm", "npm", "yarn", "bun"),
    "test_runner": ("pytest", "unittest", "vitest", "jest"),
    "database": ("sqlite", "postgres", "postgresql", "mysql", "duckdb"),
    "theme_ui": ("dark mode", "light mode", "深色模式", "浅色模式"),
    "git_workflow": ("rebase", "merge", "squash"),
    "home_network": ("rb5009", "er3228et", "r740"),
}

# 具体实体簇：用于识别同一对象在不同表述/不同时间点上的真实变更或冲突
_CONCRETE_ENTITY_GROUPS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("price_auto_claim", "京东自动一键价保", re.compile(r"(?:自动一键价保|自动保价|auto_claim|价保.*授权|保价.*授权)", re.IGNORECASE)),
    ("washer_cabinet", "洗衣机选型与柜位尺寸", re.compile(r"(?:洗衣机|LG\s*小旋风|西门子.*洗烘|600\s*(?:橱柜|深))", re.IGNORECASE)),
    ("robot_camera", "扫地机摄像头限制", re.compile(r"(?:扫地机|禁摄|摄像头)", re.IGNORECASE)),
    ("range_hood", "烟灶套装选型", re.compile(r"(?:烟灶|烟机|MJ12|MJ01D|老板双子星)", re.IGNORECASE)),
    ("tv_size", "客厅电视尺寸", re.compile(r"(?:电视|98\s*寸|85\s*寸|L98MC|L85MC)", re.IGNORECASE)),
    ("gas_meter", "燃气表规格（G2.5 vs G4）", re.compile(r"(?:燃气表|G2\.5|\bG4\b)", re.IGNORECASE)),
    ("water_heater", "热水器（零冷水/回水管）", re.compile(r"(?:热水器|零冷水|回水管)", re.IGNORECASE)),
    ("steam_oven", "微蒸烤一体机选型", re.compile(r"(?:微蒸烤|水波炉)", re.IGNORECASE)),
    ("main_router", "新房主路由器", re.compile(r"(?:路由器|主路由|路由改为|路由\s*\*{0,2}RB5009|RB5009|ER3228|TL-AC100)", re.IGNORECASE)),
    ("server_rack", "书房机柜与 R740", re.compile(r"(?:机柜|R740|24U)", re.IGNORECASE)),
    ("claude_provider", "Claude 供应商切换边界", re.compile(r"(?:不许换供应商|双供应商|擅自换.{0,12}供应商|不允许.{0,18}供应商|扩权.{0,20}供应商)", re.IGNORECASE)),
    ("magpie_affinity", "Magpie 组内亲和性", re.compile(r"(?:亲和性|affinity)", re.IGNORECASE)),
    ("web_dashboard", "网页控制塔 / 看板原则", re.compile(r"(?:网页看板|控制塔|Control\s*Tower)", re.IGNORECASE)),
    ("push_cap", "主动推送频控与熔断", re.compile(r"(?:每日\s*5\s*条|推送上限|小时熔断|主动推送)", re.IGNORECASE)),
    ("sms_otp_boundary", "手机短信验证码转发边界", re.compile(r"(?:验证码.*(?:手机|外发)|瞬时加密保险箱|otp_inbox)", re.IGNORECASE)),
    ("meituan_path", "美团下单路径", re.compile(r"(?:poi_id_str|搜索页|直航|直接进店)", re.IGNORECASE)),
    ("x_dm_account", "X 私信监控账号", re.compile(r"(?:Limcc55|Lim66cc|私信监控)", re.IGNORECASE)),
    # D-35(D4): 当前美团支付调用方式已由本人永久标记为非安全风险，不再纳入冲突/风险扫描
    ("indie_game_version", "《独游项目》设计稿版本", re.compile(r"(?:独游项目|DESIGN\.md|v0\.[234])", re.IGNORECASE)),
)
_ENTITY_PAT_BY_KEY: dict[str, re.Pattern[str]] = {k: pat for k, _, pat in _CONCRETE_ENTITY_GROUPS}

_STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "will", "should",
    "use", "using", "prefer", "always", "never", "not", "our", "we", "you", "are", "was", "were",
    "claude", "codex", "hermes", "agy", "md", "json", "html", "http", "https", "true", "false",
    "我们", "不要", "禁止", "偏好", "决定", "采用", "使用", "默认", "优先", "已经", "通过",
    "以后", "还是", "先走", "直接", "可以", "应该", "需要", "尽量", "如果", "因为", "所以",
    "用户", "本机", "文件", "订单", "授权", "真实", "参数", "方案", "服务", "选择", "要求",
    "覆盖", "出现", "重启", "会话", "话里", "登录", "录时", "系统", "配置", "记录", "更新",
    "测试", "验证", "问题", "方法", "说明", "工具", "页面", "功能", "支持", "处理", "状态",
    "方式", "内容", "时间", "操作", "相关", "主要", "目前", "之前", "之后", "现在", "其他",
    "所有", "任意", "进行", "或者", "以及", "必须", "不能", "无法", "没有", "不是", "但是",
    "虽然", "而且", "并且", "同时", "另外", "此外", "正在", "仍然", "商品", "实付", "任务",
    "工作", "当前", "普通", "自动", "手动", "确认", "规则", "通知", "提醒", "消息", "界面",
    "项目", "目录", "路径", "数据", "信息", "结果", "情况", "部分", "全部", "继续", "保留",
    "新增", "修改", "删除", "查看", "运行", "执行", "启动", "安装", "部署", "发布", "线上",
    "本地", "手机", "电脑", "桌面", "窗口", "后台", "前台", "一次", "两次", "每轮", "每天",
}
_WORD_RE = re.compile(r"[a-zA-Z0-9_/-]{2,}|[\u4e00-\u9fff]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_CLAUSE_SPLIT_RE = re.compile(r"[,，;；。!！\n]+")
_CLAUSE_NEG_RE = re.compile(
    r"(?:禁止|严禁|不要|绝不|千万别|别再?|不用|避免|禁用|取消|撤回|切忌|不许|不允许|不再|do\s+not|don't|never|avoid|disable|forbid|ban|instead\s+of|rather\s+than)",
    re.IGNORECASE,
)


def _strip_attribution_for_topic(text: str) -> str:
    return _ATTRIBUTION_STRIP_RE.sub(" ", text or "")


def _infer_topic(text: str) -> str:
    cleaned = _strip_attribution_for_topic(text)
    for topic, pat in _TOPIC_PATTERNS:
        if pat.search(cleaned):
            return topic
    tokens = [
        w.lower()
        for w in _WORD_RE.findall(cleaned)
        if w.lower() not in _STOP_WORDS and not re.fullmatch(r"\d+(?:[-./]\d+)*", w)
    ]
    if tokens:
        return "_".join(tokens[:2])[:48]
    return "general"


def _extract_concrete_entities(text: str) -> dict[str, str]:
    """返回 {entity_key: human_label}，用于判断两条事实是否真正围绕同一具体对象。"""
    cleaned = _strip_attribution_for_topic(text)
    found: dict[str, str] = {}
    for key, label, pat in _CONCRETE_ENTITY_GROUPS:
        if pat.search(cleaned):
            found[key] = label
    return found


def _infer_polarity(text: str, category: str) -> str:
    if category == "taboo":
        return "negative"
    has_neg = bool(_NEGATIVE_POLARITY_RE.search(text))
    has_pos = bool(_POSITIVE_POLARITY_RE.search(text))
    if has_neg and has_pos:
        return "contrastive"
    if has_neg:
        return "negative"
    if has_pos:
        return "positive"
    return "neutral"


def _extract_choice_tokens(topic: str, text: str) -> set[str]:
    low = (text or "").lower()
    choices = _CHOICE_TOKENS.get(topic, ())
    out: set[str] = set()
    for c in choices:
        if not c.isascii():
            if c in low:
                out.add(c)
        elif c == "rb5009":
            if re.search(r"(?<![a-z0-9])rb5009", low):
                out.add(c)
        elif re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", low):
            out.add(c)
    return out


def _subject_tokens(text: str) -> set[str]:
    cleaned = _strip_attribution_for_topic(text)
    out: set[str] = set()
    for w in _WORD_RE.findall(cleaned):
        wl = w.lower()
        if wl not in _STOP_WORDS and not re.fullmatch(r"\d+(?:[-./]\d+)*", wl):
            out.add(wl)
    for run in _CJK_RUN_RE.findall(cleaned):
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in _STOP_WORDS and not any(ch in "的不别了是的有在和与或就也都很先走用把给被向对从到里中上下前后" for ch in bg):
                out.add(bg)
    return out


def _split_clause_polarity(text: str) -> tuple[set[str], set[str]]:
    """把对比偏好句（如『优先直航 poi_id_str，不要走搜索页』）拆分为正向目标词集合与负向禁止词集合。"""
    pos_tokens: set[str] = set()
    neg_tokens: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(text or ""):
        c = clause.strip()
        if not c:
            continue
        m_neg = _CLAUSE_NEG_RE.search(c)
        if m_neg:
            before = c[: m_neg.start()].strip()
            after = c[m_neg.end() :].strip()
            if before:
                pos_tokens.update(_subject_tokens(before))
            if after:
                neg_tokens.update(_subject_tokens(after))
        else:
            pos_tokens.update(_subject_tokens(c))
    return pos_tokens, neg_tokens


def _extract_inline_date(text: str, fallback_ts: str, fallback_year: int = 2026) -> str:
    """尽量从正文提取真实事件日期，确保旧/新顺序符合真实时间线而非蒸馏时间。"""
    s = text or ""
    hour = 12
    if any(k in s[:40] for k in ("凌晨", "00:", "01:", "02:")):
        hour = 2
    elif any(k in s[:40] for k in ("上午", "早上", "早间")):
        hour = 9
    elif any(k in s[:40] for k in ("下午", "午后")):
        hour = 15
    elif any(k in s[:40] for k in ("晚", "夜里", "半夜")):
        hour = 21

    # 优先看句首 60 字里的完整日期 YYYY-MM-DD
    m_full = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s[:80]) or re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
    if m_full:
        try:
            dt = datetime(int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3)), hour, 0, tzinfo=SHANGHAI_TZ)
            return iso_now(dt)
        except ValueError:
            pass
    # 其次看句首 32 字里的 MM-DD（如 `11-09 晚又下单`）
    m_md = re.search(r"(?<![\d.])(\d{2})-(\d{2})(?![\d.])", s[:40])
    if m_md:
        mo, dy = int(m_md.group(1)), int(m_md.group(2))
        if 1 <= mo <= 12 and 1 <= dy <= 31:
            try:
                dt = datetime(fallback_year, mo, dy, hour, 0, tzinfo=SHANGHAI_TZ)
                return iso_now(dt)
            except ValueError:
                pass
    return fallback_ts


def _friendly_source_name(source: str, doc_title: str = "") -> str:
    """把内部 source 路径/ID 转成可读中文出处名。"""
    if doc_title and not _FRONTMATTER_KEY_RE.match(doc_title):
        clean_title = _strip_markdown_for_human(doc_title)[:32]
        m_ln = re.search(r"#L(\d+)", source or "")
        return f"{clean_title}（第 {m_ln.group(1)} 行）" if m_ln else clean_title
    src = (source or "").strip()
    if not src:
        return "本机记忆"
    if src.startswith("DECISIONS.md"):
        return f"项目决策录 ({src})"
    if src.startswith("_规则待审.md"):
        return f"规则待审清单 ({src})"
    if src.startswith("routines.md:"):
        return "规律库 (routines.md)"
    if src.startswith("curiosity:"):
        return "用户求证回答"
    if src.startswith("feedback:"):
        return "用户反馈纠偏"
    if src.startswith("hypotheses:"):
        return "行为观察假设"
    if src.startswith("events:"):
        return "近期投递/事件记录"
    name_map = {
        "home-appliance-research-202610.md": "新房电器选购调研",
        "home-network-plan-202611.md": "新房全屋网络规划",
        "claude-factory-model-preference.md": "Claude 模型与供应商偏好",
        "claude-magpie-auto-fallback.md": "Magpie 自动回退配置",
        "personal-agent-plan.md": "司命个人助手规划",
        "qq-interactive-confirm-cards.md": "QQ 按钮交互偏好",
        "local-terminal-input-echo.md": "本机终端回显偏好",
        "network-proxy-quickref.md": "网络代理与分流速查",
        "x-monitor-source.md": "X / 社交监控配置",
        "indie-game-ideation.md": "《独游项目》游戏策划",
        "dsh-purge-local.md": "dsh-purge 本机部署",
    }
    base = src.split("#")[0].split("/")[-1]
    m_ln = re.search(r"#L(\d+)", src)
    if base in name_map:
        return f"{name_map[base]}（第 {m_ln.group(1)} 行）" if m_ln else name_map[base]
    return src


def human_topic_label(topic: str, existing: Optional[dict[str, Any]] = None, incoming: Optional[dict[str, Any]] = None) -> str:
    """将内部 topic key 映射为干净人话中文分类，绝不暴露 `80_订单` / `qq_用户在` / `model_routing`。"""
    t = (topic or "").strip()
    if t in TOPIC_LABELS:
        return TOPIC_LABELS[t]
    combined = f"{(existing or {}).get('statement', '')} {(incoming or {}).get('statement', '')}"
    entities = _extract_concrete_entities(combined)
    if entities:
        return next(iter(entities.values()))
    inferred = _infer_topic(combined)
    if inferred in TOPIC_LABELS:
        return TOPIC_LABELS[inferred]
    # 清理动态 topic 里的数字、下划线和碎片
    pretty = re.sub(r"[_/]+", " · ", t).strip(" ·")
    if pretty and not re.search(r"^\d", pretty) and len(pretty) <= 18:
        return pretty
    return "日常与工程约定"


def _source_file_base(source: str) -> str:
    s = (source or "").strip()
    if "#" in s:
        s = s.split("#", 1)[0]
    return s.split("/")[-1].lower()


def extract_facts_from_utterances(
    records: list[dict[str, Any]],
    paths: Optional[SimingPaths] = None,
) -> list[dict[str, Any]]:
    """从多 Agent 会话记录中抽取结构化事实，完整保留脱敏后的原始出处（verbatim provenance，不截断）。"""
    facts: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_text_keys: set[tuple[str, str]] = set()

    for idx, rec in enumerate(records or []):
        raw_text = str(
            rec.get("text")
            or rec.get("content")
            or rec.get("utterance")
            or rec.get("message")
            or ""
        ).strip()
        if _is_noise_utterance(raw_text):
            continue
        text = _sanitize_wiki_text(raw_text, paths=paths).strip()
        if _is_noise_utterance(text):
            continue

        explicit_cat = str(rec.get("category") or "").strip().lower()
        category = ""
        if explicit_cat in CATEGORY_META:
            category = explicit_cat
        elif _TABOO_RE.search(text):
            category = "taboo"
        elif _FIX_RE.search(text):
            category = "verified_fix"
        elif _DECISION_RE.search(text):
            category = "decision"
        elif _PREF_RE.search(text):
            category = "preference"
        else:
            continue

        topic = str(rec.get("topic") or "").strip() or _infer_topic(text)
        polarity = _infer_polarity(text, category)
        source = _sanitize_wiki_text(str(rec.get("source") or f"utterance#{idx + 1}").strip(), paths=paths)
        doc_title = _sanitize_wiki_text(str(rec.get("doc_title") or "").strip(), paths=paths)
        agent = str(rec.get("agent") or "unknown").strip()
        timestamp = str(rec.get("timestamp") or rec.get("ts") or iso_now()).strip()

        norm_key = (category, re.sub(r"\s+", " ", text).strip())
        if norm_key in seen_text_keys:
            continue
        seen_text_keys.add(norm_key)

        dedup_key = hashlib.sha256(f"{category}|{topic}|{text}".encode("utf-8")).hexdigest()[:16]
        if dedup_key in seen_hashes:
            continue
        seen_hashes.add(dedup_key)

        facts.append(
            {
                "fact_id": f"F-{dedup_key[:10]}",
                "category": category,
                "topic": topic,
                "polarity": polarity,
                "statement": text[:1200],
                "verbatim": text,
                "source": source,
                "doc_title": doc_title,
                "agent": agent,
                "timestamp": timestamp,
            }
        )

    return facts


def conflict_fingerprint(conflict: dict[str, Any]) -> str:
    """稳定冲突指纹：仅依赖 (kind, sorted(existing.fact_id, incoming.fact_id))，跨蒸馏轮次保持不变。"""
    kind = str(conflict.get("kind") or "contradiction").strip().lower()
    ex = conflict.get("existing") or {}
    inc = conflict.get("incoming") or {}
    a = str(ex.get("fact_id") or hashlib.sha256(str(ex.get("statement") or "").encode("utf-8")).hexdigest()[:10])
    b = str(inc.get("fact_id") or hashlib.sha256(str(inc.get("statement") or "").encode("utf-8")).hexdigest()[:10])
    lo, hi = (a, b) if a <= b else (b, a)
    digest = hashlib.sha256(f"{kind}|{lo}|{hi}".encode("utf-8")).hexdigest()[:12]
    return f"CF-{digest}"


def _conflict_content_hash(conflict: dict[str, Any]) -> str:
    ex = conflict.get("existing") or {}
    inc = conflict.get("incoming") or {}
    blob = "|".join(
        [
            str(conflict.get("kind") or ""),
            str(conflict.get("topic") or ""),
            str(ex.get("verbatim") or ex.get("statement") or ""),
            str(inc.get("verbatim") or inc.get("statement") or ""),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _ensure_conflict_decisions_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_conflict_decisions (
            conflict_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT 'contradiction',
            topic TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL,
            existing_fact_id TEXT NOT NULL DEFAULT '',
            incoming_fact_id TEXT NOT NULL DEFAULT '',
            winner_fact_id TEXT NOT NULL DEFAULT '',
            loser_fact_id TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            existing_snapshot_json TEXT NOT NULL DEFAULT '{}',
            incoming_snapshot_json TEXT NOT NULL DEFAULT '{}',
            decided_at TEXT NOT NULL,
            snooze_until TEXT
        )
        """
    )


def _ensure_conflict_briefs_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_conflict_briefs (
            conflict_id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL DEFAULT '',
            is_real_conflict INTEGER NOT NULL DEFAULT 1,
            domain_label TEXT NOT NULL DEFAULT '',
            decision_question TEXT NOT NULL DEFAULT '',
            conflict_point TEXT NOT NULL DEFAULT '',
            old_summary TEXT NOT NULL DEFAULT '',
            new_summary TEXT NOT NULL DEFAULT '',
            old_choice_label TEXT NOT NULL DEFAULT '',
            new_choice_label TEXT NOT NULL DEFAULT '',
            recommendation TEXT NOT NULL DEFAULT 'manual',
            recommendation_reason TEXT NOT NULL DEFAULT '',
            brief_source TEXT NOT NULL DEFAULT 'heuristic',
            model_used TEXT NOT NULL DEFAULT '',
            conflict_snapshot_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_conflict_briefs)").fetchall()}
    if "conflict_snapshot_json" not in cols:
        conn.execute("ALTER TABLE wiki_conflict_briefs ADD COLUMN conflict_snapshot_json TEXT NOT NULL DEFAULT '{}'")


def _load_conflict_decisions(db: SimingDB) -> tuple[dict[str, dict[str, Any]], set[str], set[str]]:
    """返回 (decisions_by_id, suppressed_fact_ids, resolved_conflict_ids)。"""
    conn = db.connect()
    try:
        _ensure_conflict_decisions_table(conn)
        rows = conn.execute("SELECT * FROM wiki_conflict_decisions").fetchall()
    finally:
        conn.close()
    decisions: dict[str, dict[str, Any]] = {}
    suppressed_facts: set[str] = set()
    resolved_ids: set[str] = set()
    for r in rows:
        d = dict(r)
        cid = str(d["conflict_id"])
        decisions[cid] = d
        dec = str(d.get("decision") or "")
        if dec in ("keep_new", "keep_old", "both_valid"):
            resolved_ids.add(cid)
            loser = str(d.get("loser_fact_id") or "").strip()
            if dec in ("keep_new", "keep_old") and loser:
                suppressed_facts.add(loser)
    return decisions, suppressed_facts, resolved_ids


def _load_conflict_briefs(db: SimingDB) -> dict[str, dict[str, Any]]:
    conn = db.connect()
    try:
        _ensure_conflict_briefs_table(conn)
        rows = conn.execute("SELECT * FROM wiki_conflict_briefs").fetchall()
    finally:
        conn.close()
    return {str(r["conflict_id"]): dict(r) for r in rows}


def _save_conflict_brief(
    db: SimingDB,
    conflict: dict[str, Any],
    brief: dict[str, Any],
    *,
    ts: Optional[str] = None,
) -> None:
    cid = str(conflict.get("conflict_id") or conflict_fingerprint(conflict))
    chash = _conflict_content_hash(conflict)
    now_ts = ts or iso_now()
    snap = {
        "conflict_id": cid,
        "kind": str(conflict.get("kind") or "contradiction"),
        "topic": str(conflict.get("topic") or "general"),
        "reason": str(conflict.get("reason") or ""),
        "existing": conflict.get("existing") or {},
        "incoming": conflict.get("incoming") or {},
        "pages": conflict.get("pages") or [],
    }
    with db.transaction() as conn:
        _ensure_conflict_briefs_table(conn)
        conn.execute(
            """
            INSERT INTO wiki_conflict_briefs (
                conflict_id, content_hash, is_real_conflict, domain_label,
                decision_question, conflict_point, old_summary, new_summary,
                old_choice_label, new_choice_label, recommendation, recommendation_reason,
                brief_source, model_used, conflict_snapshot_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conflict_id) DO UPDATE SET
                content_hash = excluded.content_hash,
                is_real_conflict = excluded.is_real_conflict,
                domain_label = excluded.domain_label,
                decision_question = excluded.decision_question,
                conflict_point = excluded.conflict_point,
                old_summary = excluded.old_summary,
                new_summary = excluded.new_summary,
                old_choice_label = excluded.old_choice_label,
                new_choice_label = excluded.new_choice_label,
                recommendation = excluded.recommendation,
                recommendation_reason = excluded.recommendation_reason,
                brief_source = excluded.brief_source,
                model_used = excluded.model_used,
                conflict_snapshot_json = excluded.conflict_snapshot_json,
                updated_at = excluded.updated_at
            """,
            (
                cid,
                chash,
                1 if brief.get("is_real_conflict", True) else 0,
                str(brief.get("domain_label") or "")[:40],
                str(brief.get("decision_question") or "")[:180],
                str(brief.get("conflict_point") or "")[:240],
                str(brief.get("old_summary") or "")[:240],
                str(brief.get("new_summary") or "")[:240],
                str(brief.get("old_choice_label") or "")[:48],
                str(brief.get("new_choice_label") or "")[:48],
                str(brief.get("recommendation") or "manual")[:24],
                str(brief.get("recommendation_reason") or "")[:240],
                str(brief.get("brief_source") or "heuristic")[:24],
                str(brief.get("model_used") or "")[:64],
                json.dumps(snap, ensure_ascii=False),
                now_ts,
            ),
        )


def _extract_focused_excerpt(text: str, focus_terms: list[str], max_len: int = 86) -> str:
    """围绕核心分歧关键词提炼 1-2 句最相关人话摘要，避免把 500 字长段落整坨塞进裁决区。"""
    clean = _strip_markdown_for_human(text)
    if not clean:
        return "（无内容）"
    # 剥离常见日期+作者前缀（如 `2026-11-09 核对京东/淘宝订单（claude）：`）让摘要直奔重点
    body = re.sub(r"^(?:用户\s*)?20\d{2}-\d{2}-\d{2}\s*(?:凌晨|上午|下午|晚)?\s*[^：:]{0,18}[：:]\s*", "", clean).strip() or clean
    if len(body) <= max_len:
        return body

    clauses = [c.strip() for c in re.split(r"[。；;\n]+", body) if c.strip()]
    if not clauses:
        return body[:max_len].rstrip("，,、 ") + "…"

    low_terms = [t.lower() for t in focus_terms if t]
    scored: list[tuple[int, int, str]] = []
    for idx, cl in enumerate(clauses):
        cl_low = cl.lower()
        score = sum(3 for t in low_terms if t in cl_low)
        if _OVERRIDE_SIGNAL_RE.search(cl):
            score += 2
        if _NEGATIVE_POLARITY_RE.search(cl) or _POSITIVE_POLARITY_RE.search(cl):
            score += 1
        scored.append((score, -idx, cl))
    scored.sort(reverse=True)
    best = scored[0][2]
    if len(best) <= max_len:
        # 若还余空间且有第二相关句，补半句上下文
        if len(scored) > 1 and scored[1][0] > 0 and len(best) + len(scored[1][2]) + 1 <= max_len + 16:
            return f"{best}；{scored[1][2]}"
        return best
    # 若单句很长，在分号/逗号边界里挑命中关键词的子句
    sub_clauses = [sc.strip() for sc in re.split(r"[，,、]+", best) if sc.strip()]
    picked: list[str] = []
    for sc in sub_clauses:
        if any(t in sc.lower() for t in low_terms) or not picked:
            picked.append(sc)
        if sum(len(x) for x in picked) >= max_len - 8:
            break
    joined = "，".join(picked)
    if len(joined) > max_len:
        return joined[: max_len - 1].rstrip("，,、 ") + "…"
    return joined


def _short_choice_label(summary: str, fallback: str, max_len: int = 18) -> str:
    s = re.sub(r"^(?:用户|我们|以后|优先|默认|决定|偏好|新决定|旧说法|新说法)[：:\s]*", "", _strip_markdown_for_human(summary)).strip()
    s = re.split(r"[。；;（(]", s)[0].strip("，,、：: ")
    if not s:
        return fallback
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip("，,、 ") + "…"


def build_conflict_decision_brief(conflict: dict[str, Any]) -> dict[str, Any]:
    """纯确定性人话裁决简报引擎：零网络、零外部依赖，把技术冲突翻译成一眼可拍板的裁决点。"""
    existing_brief = conflict.get("decision_brief")
    if isinstance(existing_brief, dict) and existing_brief.get("decision_question") and existing_brief.get("conflict_point"):
        return dict(existing_brief)

    kind = str(conflict.get("kind") or "contradiction").strip().lower()
    topic = str(conflict.get("topic") or "general").strip()
    ex = conflict.get("existing") or {}
    inc = conflict.get("incoming") or {}
    ex_text = str(ex.get("verbatim") or ex.get("statement") or "")
    inc_text = str(inc.get("verbatim") or inc.get("statement") or "")
    domain = human_topic_label(topic, ex, inc)

    # 1) 核心原则待拍板（DECISIONS.md）
    if kind == "policy_conflict":
        dec_id = str(conflict.get("decision_ref") or ex.get("decision_ref") or inc.get("decision_ref") or "").strip()
        old_sum = _extract_focused_excerpt(ex_text, [], max_len=90)
        new_sum = _extract_focused_excerpt(inc_text, ["现行", "建议", "评审", "风险", "暂缓"], max_len=110)
        q_prefix = f"【{dec_id}】" if dec_id else ""
        return {
            "is_real_conflict": True,
            "domain_label": "核心原则待拍板",
            "decision_question": f"{q_prefix}现行做法与原定原则冲突，最终按哪条口径执行？",
            "conflict_point": f"原定原则要求「{old_sum[:44]}」，但现行口径已改为「{new_sum[:44]}」，评审标记需你重新拍板。",
            "old_summary": old_sum,
            "new_summary": new_sum,
            "old_choice_label": f"退回原原则（{_short_choice_label(old_sum, '旧原则', 12)}）",
            "new_choice_label": f"维持现行（{_short_choice_label(new_sum, '新口径', 12)}）",
            "recommendation": "manual",
            "recommendation_reason": "涉及核心安全/节奏原则变更，评审已标为待你本人拍板，不替你默认决定。",
            "brief_source": "heuristic",
            "model_used": "",
        }

    # 2) 行为规则提案（_规则待审.md）
    if kind == "rule_proposal":
        title = str(inc.get("doc_title") or "新行为规则").strip()
        new_sum = _extract_focused_excerpt(inc_text, ["不要", "禁止", "修复", "避免", "应该"], max_len=100)
        return {
            "is_real_conflict": True,
            "domain_label": "行为规则提案",
            "decision_question": f"是否批准将「{title}」写入全局行为规则（AGENTS.md）？",
            "conflict_point": f"Agent 复盘后提议新增一条全局约束：{new_sum[:70]}",
            "old_summary": "保持现有 AGENTS.md 不变，不收录这条新规则。",
            "new_summary": new_sum,
            "old_choice_label": "暂不收录该规则",
            "new_choice_label": "批准并入 AGENTS.md",
            "recommendation": "manual",
            "recommendation_reason": "修改全局 AGENTS.md 属于长期行为约束，需你确认该教训值得固化为常驻规则。",
            "brief_source": "heuristic",
            "model_used": "",
        }

    # 3) 普通记忆矛盾 / 覆写 / 陈旧
    ex_entities = _extract_concrete_entities(ex_text)
    inc_entities = _extract_concrete_entities(inc_text)
    shared_entities = [lbl for k, lbl in ex_entities.items() if k in inc_entities]
    focus_label = shared_entities[0] if shared_entities else domain

    ex_choices = sorted(_extract_choice_tokens(topic, ex_text))
    inc_choices = sorted(_extract_choice_tokens(topic, inc_text))
    focus_terms: list[str] = []
    focus_terms.extend(ex_choices)
    focus_terms.extend(inc_choices)
    for k in ex_entities:
        if k in inc_entities:
            focus_terms.extend(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", ex_entities[k]))
    if conflict.get("flipped_terms"):
        focus_terms.extend(conflict["flipped_terms"])

    old_sum = _extract_focused_excerpt(ex_text, focus_terms, max_len=88)
    new_sum = _extract_focused_excerpt(inc_text, focus_terms, max_len=88)

    has_explicit_override = bool(_OVERRIDE_SIGNAL_RE.search(inc_text))
    old_ts = str(ex.get("timestamp") or "")
    new_ts = str(inc.get("timestamp") or "")
    newer_clear = bool(old_ts and new_ts and new_ts[:10] > old_ts[:10])

    if kind == "staleness":
        question = f"「{focus_label}」有两条相隔较久的记录，旧记录是否已经过时可以淘汰？"
        point = f"两条记录围绕「{focus_label}」时间相差较远，需要确认旧记录是否已被新做法替代。"
        rec = "keep_new"
        rec_reason = "新记录时间明显更新，如旧做法已不再使用建议直接保留新的。"
    elif ex_choices and inc_choices and ex_choices != inc_choices:
        a_lbl = "/".join(ex_choices)
        b_lbl = "/".join(inc_choices)
        question = f"「{focus_label}」到底选 {b_lbl} 还是继续用 {a_lbl}？"
        point = f"两边在「{focus_label}」上选型不同：旧记录偏向 {a_lbl}，新记录偏向 {b_lbl}。"
        rec = "keep_new" if (has_explicit_override or newer_clear) else "manual"
        rec_reason = (
            f"新记录明确改选了 {b_lbl}，如已切换完毕建议直接保留新说法。"
            if has_explicit_override
            else f"两条选型直接冲突（{a_lbl} vs {b_lbl}），请按你现在实际在用的方案拍板。"
        )
        old_choice_lbl = f"沿用 {a_lbl}"
        new_choice_lbl = f"改用 {b_lbl}"
        return {
            "is_real_conflict": True,
            "domain_label": domain,
            "decision_question": question,
            "conflict_point": point,
            "old_summary": old_sum,
            "new_summary": new_sum,
            "old_choice_label": old_choice_lbl[:24],
            "new_choice_label": new_choice_lbl[:24],
            "recommendation": rec,
            "recommendation_reason": rec_reason,
            "brief_source": "heuristic",
            "model_used": "",
        }
    elif has_explicit_override:
        question = f"「{focus_label}」已有新决定覆盖旧说法，确认按最新口径淘汰旧记录吗？"
        point = f"新记录针对「{focus_label}」明确给出了改口/取消/覆盖，与早期记录不一致。"
        rec = "keep_new"
        rec_reason = "新记录中出现了明确的「覆盖/作废/撤回/改为」表述，大概率是你后续更新了想法，建议采纳新说法。"
    elif conflict.get("flipped_terms"):
        ft = "、".join(conflict["flipped_terms"][:2])
        question = f"在「{focus_label}」上，到底该不该走「{ft}」？"
        point = f"两条记录对「{ft}」的态度正好相反：一边推荐采用，另一边明确反对。"
        rec = "keep_new" if newer_clear else "manual"
        rec_reason = (
            "新记录时间更晚，如果是你后来改了习惯可选保留新的；若视场景而定可选两条都留。"
            if newer_clear
            else "两条偏好直接相反，请确认现在实际希望 Agent 遵守哪一条。"
        )
    else:
        question = f"关于「{focus_label}」，旧说法与新说法互相冲突，以后按哪条为准？"
        point = f"围绕「{focus_label}」，一条支持/允许，另一条反对/禁止，需要明确唯一口径。"
        rec = "keep_new" if newer_clear else "manual"
        rec_reason = (
            "新记录时间更近，建议优先确认是否已改为新口径；若两条分属不同场景可选「两条都留」。"
            if newer_clear
            else "两条记录立场相反，建议根据当前实际习惯选定一条生效。"
        )

    return {
        "is_real_conflict": True,
        "domain_label": domain,
        "decision_question": question,
        "conflict_point": point,
        "old_summary": old_sum,
        "new_summary": new_sum,
        "old_choice_label": _short_choice_label(old_sum, "沿用旧说法", 20),
        "new_choice_label": _short_choice_label(new_sum, "采用新说法", 20),
        "recommendation": rec,
        "recommendation_reason": rec_reason,
        "brief_source": "heuristic",
        "model_used": "",
    }


def _render_wiki_page_markdown(
    title: str,
    ts: str,
    cat_facts: list[dict[str, Any]],
    cat_conflicts: list[dict[str, Any]],
) -> str:
    md_lines = [
        f"# {title}",
        "",
        f"- Last distilled: `{ts}`",
        f"- Entries: `{len(cat_facts)}` | Conflicts: `{len(cat_conflicts)}`",
        "",
    ]
    if not cat_facts:
        md_lines.append("_暂无记录。_")
        md_lines.append("")
    else:
        for f in cat_facts:
            md_lines.append(f"### [{f['fact_id']}] `{f['topic']}` ({f.get('agent', 'unknown')})")
            md_lines.append(f"- {f['statement']}")
            md_lines.append(
                f"> Verbatim: \"{f.get('verbatim', f['statement'])}\" — source: `{f.get('source', '')}` @ `{f.get('timestamp', '')}`"
            )
            md_lines.append("")
    if cat_conflicts:
        md_lines.append("## ⚠️ Conflicts & Staleness")
        md_lines.append("")
        for c in cat_conflicts:
            brief = build_conflict_decision_brief(c)
            md_lines.append(f"- **{brief['domain_label']}**（`{c['kind']}`）: {brief['decision_question']}")
            md_lines.append(f"  - 核心分歧：{brief['conflict_point']}")
            md_lines.append(f"  - 旧说法 (`{c['existing']['source']}`): {brief['old_summary']}")
            md_lines.append(f"  - 新说法 (`{c['incoming']['source']}`): {brief['new_summary']}")
        md_lines.append("")
    return "\n".join(md_lines).rstrip() + "\n"


def _has_override_on_entity(text: str, entity_keys: set[str]) -> bool:
    """检查覆写/作废/撤回词是否与目标实体出现在同一分句内，杜绝同段顺带提及别的对象导致串台。"""
    if not text or not _OVERRIDE_SIGNAL_RE.search(text):
        return False
    if not entity_keys:
        return True
    # 句首若整体声明「新决定（覆盖上文）」或「最新要求（覆盖...）」，视为整段覆写
    if re.search(r"^(?:用户\s*)?20\d{2}-\d{2}-\d{2}[^：:]{0,24}(?:新决定|最新要求|覆盖上文|覆写|扩权)", text.strip()):
        return True
    clauses = [c.strip() for c in re.split(r"[。；;\n]+", text) if c.strip()]
    for cl in clauses:
        if not _OVERRIDE_SIGNAL_RE.search(cl):
            continue
        for ek in entity_keys:
            pat = _ENTITY_PAT_BY_KEY.get(ek)
            if pat and pat.search(cl):
                return True
    return False


def detect_fact_conflicts(
    existing_facts: list[dict[str, Any]],
    new_facts: list[dict[str, Any]],
    stale_days: int = 90,
) -> list[dict[str, Any]]:
    """高精度检测同主题/同实体事实的矛盾、跨时间覆写与陈旧取代，杜绝跨领域假冲突与链式重复对。"""
    conflicts: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()

    for old in existing_facts or []:
        old_text = str(old.get("verbatim") or old.get("statement") or "").strip()
        if not old_text or _is_noise_utterance(old_text):
            continue
        old_topic = str(old.get("topic") or _infer_topic(old_text))
        old_pol = str(old.get("polarity") or _infer_polarity(old_text, str(old.get("category") or "")))
        old_choices = _extract_choice_tokens(old_topic, old_text)
        old_tokens = _subject_tokens(old_text)
        old_pos_toks, old_neg_toks = _split_clause_polarity(old_text)
        old_entities = _extract_concrete_entities(old_text)
        old_file = _source_file_base(str(old.get("source") or ""))
        old_id = str(old.get("fact_id") or hashlib.sha256(old_text.encode("utf-8")).hexdigest()[:10])
        old_ts = str(old.get("timestamp") or "")

        for new in new_facts or []:
            new_text = str(new.get("verbatim") or new.get("statement") or "").strip()
            if not new_text or _is_noise_utterance(new_text):
                continue
            if re.sub(r"\s+", " ", old_text) == re.sub(r"\s+", " ", new_text):
                continue
            new_topic = str(new.get("topic") or _infer_topic(new_text))
            new_pol = str(new.get("polarity") or _infer_polarity(new_text, str(new.get("category") or "")))
            new_choices = _extract_choice_tokens(new_topic, new_text)
            new_tokens = _subject_tokens(new_text)
            new_pos_toks, new_neg_toks = _split_clause_polarity(new_text)
            new_entities = _extract_concrete_entities(new_text)
            new_file = _source_file_base(str(new.get("source") or ""))
            new_id = str(new.get("fact_id") or hashlib.sha256(new_text.encode("utf-8")).hexdigest()[:10])
            if old_id == new_id:
                continue
            new_ts = str(new.get("timestamp") or "")

            same_topic = old_topic == new_topic and old_topic != "general"
            same_narrow_topic = same_topic and old_topic in _NARROW_TOPICS
            shared_entities = set(old_entities.keys()) & set(new_entities.keys())
            shared_subject_toks = old_tokens & new_tokens
            same_file = bool(old_file and new_file and old_file == new_file)

            # 核心精准门槛：必须满足「同单一窄主题」或「共享同一具体实体」或「同文档共享>=3高信息量词」
            if not (
                same_narrow_topic
                or bool(shared_entities)
                or (same_file and len(shared_subject_toks) >= 3)
            ):
                continue

            # 对比偏好反转：一方禁止的动作/路径（neg）被另一方作为正向偏好（pos）采用
            inverted_old_neg = {t for t in (old_neg_toks & new_pos_toks) if t not in _STOP_WORDS and len(t) >= 2}
            inverted_new_neg = {t for t in (new_neg_toks & old_pos_toks) if t not in _STOP_WORDS and len(t) >= 2}
            has_clause_reversal = bool(inverted_old_neg or inverted_new_neg)
            flipped_terms = sorted(inverted_old_neg | inverted_new_neg)[:3]

            # 判断时间先后（保证 existing=较旧，incoming=较新）
            swap_order = bool(old_ts and new_ts and old_ts > new_ts)
            if not swap_order and old_ts == new_ts and same_file:
                m_o = re.search(r"#L(\d+)", str(old.get("source") or ""))
                m_n = re.search(r"#L(\d+)", str(new.get("source") or ""))
                if m_o and m_n and int(m_o.group(1)) > int(m_n.group(1)):
                    swap_order = True

            first_f, second_f = (new, old) if swap_order else (old, new)
            first_id, second_id = (new_id, old_id) if swap_order else (old_id, new_id)
            first_txt, second_txt = (new_text, old_text) if swap_order else (old_text, new_text)
            first_pol, second_pol = (new_pol, old_pol) if swap_order else (old_pol, new_pol)

            kind = ""
            reason = ""
            topic_out = (
                new_topic
                if same_topic
                else (
                    old_topic
                    if old_topic in TOPIC_LABELS
                    else (new_topic if new_topic in TOPIC_LABELS else ("_".join(sorted(shared_subject_toks)[:2])[:48] or "general"))
                )
            )
            domain_lbl = human_topic_label(topic_out, first_f, second_f)
            focus_entity_key = next(iter(sorted(shared_entities))) if shared_entities else topic_out
            focus_ent_lbl = (
                old_entities[focus_entity_key]
                if shared_entities
                else domain_lbl
            )

            # 1) 同主题互斥工具/模式选型（如 pixi vs uv / 深色模式 vs 浅色模式 / RB5009 vs ER3228ET）
            if (
                (same_narrow_topic or bool(shared_entities))
                and old_choices
                and new_choices
                and old_choices != new_choices
                and old_pol != "negative"
                and new_pol != "negative"
            ):
                kind = "contradiction"
                a_c = "/".join(sorted(_extract_choice_tokens(topic_out, first_txt) or old_choices))
                b_c = "/".join(sorted(_extract_choice_tokens(topic_out, second_txt) or new_choices))
                reason = f"「{focus_ent_lbl}」选型冲突：旧记录选 {a_c}，新记录选 {b_c}"
            # 2) 明确跨时间覆写 / 撤回 / 取消 / 取代（覆写词必须与共享实体同句，避免长段误串）
            elif (
                (bool(shared_entities) and _has_override_on_entity(second_txt, shared_entities))
                or (same_narrow_topic and _OVERRIDE_SIGNAL_RE.search(second_txt))
            ):
                kind = "contradiction"
                reason = f"「{focus_ent_lbl}」有新决定覆盖旧说法，需确认淘汰旧记录"
            # 3) 对比偏好逆转（例如「优先直航 poi_id_str，不要走搜索页」vs「先走搜索页比价，不要直接进店」）
            elif has_clause_reversal and (same_narrow_topic or bool(shared_entities)):
                kind = "contradiction"
                reason = f"「{focus_ent_lbl}」操作偏好反转：围绕「{' / '.join(flipped_terms)}」推荐与反对正好相反"
            # 4) 极性相反（例如偏好/决定 vs 禁区，或允许 vs 禁止），需同窄主题或共享具体实体
            elif (
                (same_narrow_topic or bool(shared_entities))
                and ((first_pol == "positive" and second_pol == "negative") or (first_pol == "negative" and second_pol == "positive"))
            ):
                kind = "contradiction"
                reason = f"「{focus_ent_lbl}」立场相反：一条支持采用，另一条明确反对或禁止"
            else:
                # 5) 陈旧度检测（同窄主题或同具体实体，且新事实晚于旧事实超过 stale_days）
                if same_narrow_topic or bool(shared_entities):
                    try:
                        old_dt = parse_iso(str(first_f.get("timestamp") or ""))
                        new_dt = parse_iso(str(second_f.get("timestamp") or ""))
                        if new_dt - old_dt >= timedelta(days=int(stale_days)):
                            kind = "staleness"
                            reason = f"「{focus_ent_lbl}」旧记录距新记录已超过 {stale_days} 天，可能已被替代"
                    except Exception:
                        pass

            if kind:
                canon_a, canon_b = (old_id, new_id) if old_id < new_id else (new_id, old_id)
                pair_key = (kind, canon_a, canon_b)
                if pair_key not in seen_pairs:
                    seen_pairs.add(pair_key)
                    item: dict[str, Any] = {
                        "kind": kind,
                        "topic": topic_out,
                        "topic_label": domain_lbl,
                        "focus_entity": focus_entity_key,
                        "reason": reason,
                        "flipped_terms": flipped_terms,
                        "existing": {
                            "fact_id": first_id,
                            "statement": first_txt[:1200],
                            "verbatim": first_txt,
                            "source": str(first_f.get("source") or ""),
                            "doc_title": str(first_f.get("doc_title") or ""),
                            "source_label": _friendly_source_name(str(first_f.get("source") or ""), str(first_f.get("doc_title") or "")),
                            "timestamp": str(first_f.get("timestamp") or ""),
                            "category": str(first_f.get("category") or ""),
                            "agent": str(first_f.get("agent") or ""),
                        },
                        "incoming": {
                            "fact_id": second_id,
                            "statement": second_txt[:1200],
                            "verbatim": second_txt,
                            "source": str(second_f.get("source") or ""),
                            "doc_title": str(second_f.get("doc_title") or ""),
                            "source_label": _friendly_source_name(str(second_f.get("source") or ""), str(second_f.get("doc_title") or "")),
                            "timestamp": str(second_f.get("timestamp") or ""),
                            "category": str(second_f.get("category") or ""),
                            "agent": str(second_f.get("agent") or ""),
                        },
                    }
                    item["conflict_id"] = conflict_fingerprint(item)
                    item["decision_brief"] = build_conflict_decision_brief(item)
                    conflicts.append(item)

    # 链式去重：对同一旧事实 + 同一具体实体，若存在多条后续更新（A→B、A→C），仅保留与最新定稿（C）的对比，避免重复刷屏
    best_by_old_entity: dict[tuple[str, str], dict[str, Any]] = {}
    for item in conflicts:
        key = (str(item["existing"]["fact_id"]), str(item.get("focus_entity") or item["topic"]))
        prev = best_by_old_entity.get(key)
        if prev is None or str(item["incoming"].get("timestamp") or "") >= str(prev["incoming"].get("timestamp") or ""):
            best_by_old_entity[key] = item
    return list(best_by_old_entity.values())


def _read_session_records(session_files: list[Path | str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_path in session_files:
        p = Path(raw_path)
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        agent_hint = "unknown"
        low_name = str(p).lower()
        for candidate in ("claude", "codex", "agy", "hermes", "pi"):
            if candidate in low_name:
                agent_hint = candidate
                break

        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            mtime_dt = datetime.fromtimestamp(p.stat().st_mtime).astimezone(SHANGHAI_TZ)
            file_ts = iso_now(mtime_dt)
            file_year = mtime_dt.year
        except OSError:
            continue

        if suffix == ".jsonl":
            for lineno, line in enumerate(lines, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                text = ""
                if isinstance(obj.get("text"), str):
                    text = obj["text"]
                elif isinstance(obj.get("content"), str):
                    text = obj["content"]
                elif isinstance(obj.get("message"), dict) and isinstance(
                    obj["message"].get("content"), str
                ):
                    text = obj["message"]["content"]
                elif isinstance(obj.get("content"), list):
                    parts = [
                        str(blk.get("text") or "")
                        for blk in obj["content"]
                        if isinstance(blk, dict) and blk.get("text")
                    ]
                    text = "\n".join(parts)
                if not text.strip():
                    continue
                ts = str(obj.get("timestamp") or obj.get("created_at") or obj.get("ts") or "")
                agent = str(obj.get("agent") or agent_hint)
                records.append(
                    {
                        "text": text.strip(),
                        "source": f"{p.name}#L{lineno}",
                        "doc_title": p.stem,
                        "agent": agent,
                        "timestamp": ts or file_ts,
                    }
                )
        else:
            in_frontmatter = False
            doc_title = ""
            doc_updated_ts = file_ts
            for lineno, line in enumerate(lines, start=1):
                stripped_raw = line.strip()
                if lineno == 1 and stripped_raw == "---":
                    in_frontmatter = True
                    continue
                if in_frontmatter:
                    if stripped_raw in ("---", "..."):
                        in_frontmatter = False
                        continue
                    m_fm = re.match(r"^(title|name|updated|modified)\s*[:：]\s*(.+)$", stripped_raw, re.IGNORECASE)
                    if m_fm:
                        k, v = m_fm.group(1).lower(), m_fm.group(2).strip().strip("\"'")
                        if k == "title" or (k == "name" and not doc_title):
                            doc_title = v
                        elif k in ("updated", "modified"):
                            m_d = re.search(r"(20\d{2})-(\d{2})-(\d{2})", v)
                            if m_d:
                                try:
                                    doc_updated_ts = iso_now(
                                        datetime(int(m_d.group(1)), int(m_d.group(2)), int(m_d.group(3)), 12, 0, tzinfo=SHANGHAI_TZ)
                                    )
                                    file_year = int(m_d.group(1))
                                except ValueError:
                                    pass
                    continue
                if stripped_raw.startswith("# ") and not doc_title:
                    doc_title = stripped_raw.lstrip("# ").strip()
                    continue
                if stripped_raw.startswith("##"):
                    continue
                cleaned = _clean_markdown_line(line)
                if len(cleaned) < 6:
                    continue
                if _is_noise_utterance(cleaned):
                    continue
                line_ts = _extract_inline_date(cleaned, doc_updated_ts, fallback_year=file_year)
                records.append(
                    {
                        "text": cleaned,
                        "source": f"{p.name}#L{lineno}",
                        "doc_title": doc_title or p.stem,
                        "agent": agent_hint,
                        "timestamp": line_ts,
                    }
                )
    return records


def _load_existing_wiki_facts(db: SimingDB) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT sources_json FROM wiki_pages").fetchall()
    finally:
        conn.close()
    facts: list[dict[str, Any]] = []
    seen_norm: set[tuple[str, str]] = set()
    for r in rows:
        try:
            arr = json.loads(str(r["sources_json"] or "[]"))
            if isinstance(arr, list):
                for x in arr:
                    if not isinstance(x, dict):
                        continue
                    stmt_raw = _clean_markdown_line(str(x.get("statement") or ""))
                    verb_raw = _clean_markdown_line(str(x.get("verbatim") or stmt_raw))
                    # 优先用完整未截断 verbatim 恢复曾被 280 字腰斩的 statement
                    full_text = verb_raw if len(verb_raw) >= len(stmt_raw) else stmt_raw
                    if not full_text or _is_noise_utterance(full_text):
                        continue
                    sanitized = dict(x)
                    clean_full = _sanitize_wiki_text(full_text, paths=db.paths)
                    if _is_noise_utterance(clean_full):
                        continue
                    sanitized["statement"] = clean_full[:1200]
                    sanitized["verbatim"] = clean_full
                    sanitized["source"] = _sanitize_wiki_text(str(x.get("source") or ""), paths=db.paths)
                    cat = str(sanitized.get("category") or "preference")
                    norm_k = (cat, re.sub(r"\s+", " ", clean_full).strip())
                    if norm_k in seen_norm:
                        continue
                    seen_norm.add(norm_k)
                    # 重新校验 topic，修复旧版 `(claude)`/`(codex)` 误标成 model_routing 的历史脏数据
                    sanitized["topic"] = _infer_topic(clean_full)
                    sanitized["polarity"] = _infer_polarity(clean_full, cat)
                    facts.append(sanitized)
        except Exception:
            continue
    return facts


def _read_routines_records(db: SimingDB, ts: str) -> list[dict[str, Any]]:
    """从 routines.md 抽取主人确认/司命学到的个人生活规律作为偏好与约定事实。"""
    try:
        from siming.event_chain import routines_md_path

        rpath = routines_md_path(db)
    except Exception:
        rpath = db.paths.data_dir / "routines.md"
    if not rpath.is_file():
        return []
    try:
        raw = rpath.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    records: list[dict[str, Any]] = []
    yaml_m = re.search(r"```yaml\s*\n(.*?)```", raw, re.DOTALL)
    if yaml_m:
        try:
            import yaml

            doc = yaml.safe_load(yaml_m.group(1)) or {}
            updated = str(doc.get("updated_at") or ts)
            for item in doc.get("routines") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "").lower() in ("paused", "disabled", "archived"):
                    continue
                rid = str(item.get("id") or "").strip() or "routine"
                title = str(item.get("title") or "").strip()
                window = str(item.get("window") or "").strip()
                origin = str(item.get("origin") or "learned").strip()
                if not title:
                    continue
                txt = f"偏好规律 {title}" + (f"（窗口：{window}）" if window else "")
                records.append(
                    {
                        "text": txt,
                        "category": "preference",
                        "source": f"routines.md:{rid}",
                        "doc_title": "司命个人规律库",
                        "agent": f"routines({origin})",
                        "timestamp": updated,
                    }
                )
        except Exception:
            pass
    return records


def _collect_db_records(db: SimingDB, ts: str) -> list[dict[str, Any]]:
    """从 siming.db 抽取真实偏好、求证答案、假设、反馈与近期人工事件（过滤购物订单 OCR 截图垃圾）。"""
    records: list[dict[str, Any]] = []
    records.extend(_read_routines_records(db, ts))

    conn = db.connect()
    try:
        for r in conn.execute(
            "SELECT id, claim, status, created_at FROM hypotheses WHERE status != 'REJECTED' ORDER BY created_at DESC LIMIT 80"
        ).fetchall():
            claim = str(r["claim"] or "").strip()
            if len(claim) < 4 or contains_otp(claim) or _is_noise_utterance(claim):
                continue
            st = str(r["status"] or "OBSERVING").upper()
            records.append(
                {
                    "text": claim,
                    "category": "decision" if st == "CONFIRMED" else "preference",
                    "source": f"hypotheses:{r['id']}",
                    "doc_title": "行为观察假设",
                    "agent": "siming_hypothesis",
                    "timestamp": str(r["created_at"] or ts),
                }
            )

        for r in conn.execute(
            "SELECT qid, topic, question, answer, answered_at, created_at FROM curiosity_questions "
            "WHERE status = 'ANSWERED' AND trim(answer) != '' ORDER BY COALESCE(answered_at, created_at) DESC LIMIT 80"
        ).fetchall():
            ans = str(r["answer"] or "").strip()
            q = str(r["question"] or "").strip()
            topic = str(r["topic"] or "general").strip()
            if len(ans) < 2 or contains_otp(ans):
                continue
            combined = f"用户求证确认（{topic}）：{ans}" + (f"｜原问：{q[:120]}" if q else "")
            if _TABOO_RE.search(combined):
                inferred_cat = "taboo"
            elif _FIX_RE.search(combined):
                inferred_cat = "verified_fix"
            elif _DECISION_RE.search(combined):
                inferred_cat = "decision"
            else:
                inferred_cat = "preference"
            records.append(
                {
                    "text": combined,
                    "category": inferred_cat,
                    "topic": topic,
                    "source": f"curiosity:{r['qid']}",
                    "doc_title": "用户求证回答",
                    "agent": "user_confirmed",
                    "timestamp": str(r["answered_at"] or r["created_at"] or ts),
                }
            )

        for r in conn.execute(
            "SELECT id, target_id, rule_id, action, note, ts FROM feedback ORDER BY ts DESC LIMIT 120"
        ).fetchall():
            note = str(r["note"] or "").strip()
            if len(note) < 4 or note.startswith("wave0_cleanup") or contains_otp(note):
                continue
            act = str(r["action"] or "").strip()
            rule_id = str(r["rule_id"] or "").strip()
            rec: dict[str, Any] = {
                "text": f"[{rule_id}/{act}] {note}",
                "source": f"feedback:{r['id']}",
                "doc_title": "用户反馈纠偏",
                "agent": "user_feedback",
                "timestamp": str(r["ts"] or ts),
            }
            if act in ("纠正", "偏好", "规则", "correct", "prefer"):
                rec["category"] = "preference"
            records.append(rec)

        for r in conn.execute(
            """
            SELECT event_id, source, summary, payload_json, occurred_at
            FROM events
            WHERE source IN ('agent_sessions', 'qq_drop', 'desktop_drop', 'ios_drop', 'memos_vault', 'user_drop')
            ORDER BY occurred_at DESC
            LIMIT 200
            """
        ).fetchall():
            summary = str(r["summary"] or "").strip()
            payload_txt = ""
            try:
                pl = json.loads(str(r["payload_json"] or "{}"))
                if isinstance(pl, dict):
                    payload_txt = str(pl.get("utterance") or pl.get("text") or "").strip()
            except Exception:
                payload_txt = ""
            cand = payload_txt or summary
            if len(cand) < 6 or len(cand) > 600 or contains_otp(cand) or _is_noise_utterance(cand):
                continue
            if "你在隔离工作目录" in cand and "执行用户预授权任务" in cand:
                continue
            records.append(
                {
                    "text": cand,
                    "source": f"events:{r['source']}:{r['event_id']}",
                    "doc_title": "近期投递记录",
                    "agent": str(r["source"] or "event"),
                    "timestamp": str(r["occurred_at"] or ts),
                }
            )
    finally:
        conn.close()
    return records


def _collect_governance_sources(
    db: SimingDB,
    ts: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """抽取治理级待裁决项与权威最新决策，确保「列得全」：
    1. `docs/DECISIONS.md` 中「原则变更总表」标明 `待重新拍板` / `暂缓执行` / `复核` 的重大原则冲突；
    2. `docs/DECISIONS.md` 中已落地的原则变更作为权威 decision 事实（用于自动发现旧笔记漂移）；
    3. `60-共享记忆/_规则待审.md` 中各 Agent 提议并入 `AGENTS.md` 的待审规则。
    """
    extra_records: list[dict[str, Any]] = []
    gov_conflicts: list[dict[str, Any]] = []

    # 1. docs/DECISIONS.md
    dec_path = db.paths.repo_root / "docs" / "DECISIONS.md"
    if dec_path.is_file():
        try:
            raw_dec = dec_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw_dec = ""
        if raw_dec:
            # 切分 ### D-xx 详情段
            sec_map: dict[str, tuple[str, str]] = {}
            sec_matches = list(re.finditer(r"^###\s+(D-\d+)\s+(.+)$", raw_dec, re.MULTILINE))
            for i, m in enumerate(sec_matches):
                did = m.group(1).strip()
                dtitle = m.group(2).strip()
                start = m.end()
                end = sec_matches[i + 1].start() if i + 1 < len(sec_matches) else len(raw_dec)
                body = raw_dec[start:end].strip()
                # 截止到分隔线 ---
                if "\n---" in body:
                    body = body.split("\n---", 1)[0].strip()
                sec_map[did] = (dtitle, body)

            # 解析原则变更总表
            for m_row in re.finditer(r"^\|\s*([^|\n]+)\|\s*([^|\n]+)\|\s*([^|\n]+)\|\s*([^|\n]+)\|$", raw_dec, re.MULTILINE):
                old_col = m_row.group(1).strip()
                new_col = m_row.group(2).strip()
                ref_col = m_row.group(3).strip()
                st_col = m_row.group(4).strip()
                if old_col.startswith(("PLAN", "---")) or set(old_col) <= {"-", " "}:
                    continue
                m_did = re.search(r"D-\d+", ref_col)
                did = m_did.group(0) if m_did else ref_col
                dtitle, dbody = sec_map.get(did, ("", ""))

                old_clean = _sanitize_wiki_text(old_col, paths=db.paths)
                new_clean = _sanitize_wiki_text(new_col, paths=db.paths)
                detail_clean = _sanitize_wiki_text(dbody, paths=db.paths)

                if any(k in st_col for k in ("待重新拍板", "暂缓", "复核", "⚠️")):
                    full_new = new_clean
                    if dtitle:
                        full_new = f"{did} {dtitle}\n现行口径：{new_clean}"
                    if detail_clean:
                        full_new = f"{full_new}\n\n{detail_clean}"
                    old_fid = f"GOV-{hashlib.sha256(f'{did}|old'.encode()).hexdigest()[:10]}"
                    new_fid = f"GOV-{hashlib.sha256(f'{did}|new'.encode()).hexdigest()[:10]}"
                    # 抽取评审建议用于人话推荐理由
                    m_adv = re.search(r"评审建议[）)]*\s*[：:]\s*(.+)", detail_clean)
                    adv_txt = _strip_markdown_for_human(m_adv.group(1)) if m_adv else ""
                    old_short = _strip_markdown_for_human(old_clean)
                    new_short = _strip_markdown_for_human(new_clean)
                    brief = {
                        "is_real_conflict": True,
                        "domain_label": "核心原则待拍板",
                        "decision_question": f"【{did}】{dtitle.split('（')[0].strip() if dtitle else old_short[:26]}：维持现行口径，还是退回原定原则？",
                        "conflict_point": f"原原则是「{old_short}」，但现行口径已变为「{new_short}」（状态：{_strip_markdown_for_human(st_col)}）。",
                        "old_summary": old_short,
                        "new_summary": f"{new_short}" + (f"（评审建议：{adv_txt[:80]}）" if adv_txt else ""),
                        "old_choice_label": f"退回原原则（{_short_choice_label(old_short, '按旧原则', 11)}）",
                        "new_choice_label": f"维持现行（{_short_choice_label(new_short, '按现行', 11)}）",
                        "recommendation": "manual",
                        "recommendation_reason": adv_txt[:140] if adv_txt else f"DECISIONS.md 明确标注为「{_strip_markdown_for_human(st_col)}」，需要你本人重新裁决。",
                        "brief_source": "heuristic",
                        "model_used": "",
                    }
                    item = {
                        "kind": "policy_conflict",
                        "topic": "policy_conflict",
                        "topic_label": "核心原则待拍板",
                        "decision_ref": did,
                        "reason": brief["conflict_point"],
                        "existing": {
                            "fact_id": old_fid,
                            "statement": old_clean[:1200],
                            "verbatim": old_clean,
                            "source": f"DECISIONS.md:{did}(原原则)",
                            "doc_title": f"原定原则 ({did})",
                            "source_label": f"项目设计基线 · {did} 原原则",
                            "timestamp": "2026-11-16T12:00:00+08:00",
                            "category": "decision",
                            "agent": "decisions_baseline",
                        },
                        "incoming": {
                            "fact_id": new_fid,
                            "statement": full_new[:1200],
                            "verbatim": full_new,
                            "source": f"DECISIONS.md:{did}",
                            "doc_title": f"DECISIONS.md · {did}",
                            "source_label": f"项目决策录 · {did}（{_strip_markdown_for_human(st_col)}）",
                            "timestamp": "2026-11-23T12:00:00+08:00",
                            "category": "decision",
                            "agent": "review_audit",
                        },
                        "decision_brief": brief,
                    }
                    item["conflict_id"] = conflict_fingerprint(item)
                    gov_conflicts.append(item)
                elif any(k in st_col for k in ("已执行", "已落地")):
                    # 把已落地的新原则作为最新权威决策事实，用来捕捉旧文档（如 personal-agent-plan.md）里的过时说法
                    extra_records.append(
                        {
                            "text": f"2026-11-23 正式决策 {did}（覆盖旧原则「{old_clean}」）：{new_clean}",
                            "category": "decision",
                            "source": f"DECISIONS.md:{did}",
                            "doc_title": f"项目决策录 ({did})",
                            "agent": "decisions_official",
                            "timestamp": "2026-11-23T12:00:00+08:00",
                        }
                    )

    # 2. 60-共享记忆/_规则待审.md
    rule_review_path = db.paths.shared_memory_dir / "_规则待审.md"
    if rule_review_path.is_file():
        try:
            raw_rr = rule_review_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw_rr = ""
        if raw_rr:
            sec_matches = list(re.finditer(r"^##\s+(.+)$", raw_rr, re.MULTILINE))
            for i, m in enumerate(sec_matches):
                heading = m.group(1).strip()
                start = m.end()
                end = sec_matches[i + 1].start() if i + 1 < len(sec_matches) else len(raw_rr)
                body = _sanitize_wiki_text(raw_rr[start:end].strip(), paths=db.paths)
                if len(body) < 8:
                    continue
                m_date = re.search(r"(20\d{2}-\d{2}-\d{2})", heading)
                date_ts = f"{m_date.group(1)}T12:00:00+08:00" if m_date else ts
                m_title = re.search(r"[（(]([^）)]+)[）)]", heading)
                prop_title = m_title.group(1).strip() if m_title else heading
                m_agent = re.search(r"来源\s*([A-Za-z0-9_-]+)", heading)
                prop_agent = m_agent.group(1) if m_agent else "agent"

                slug_h = hashlib.sha256(f"rule_proposal|{heading}".encode()).hexdigest()[:10]
                old_fid = f"RP-OLD-{slug_h}"
                new_fid = f"RP-NEW-{slug_h}"
                old_text = "当前 ~/.agents/AGENTS.md 尚未收录该规则提案。"
                new_sum = _extract_focused_excerpt(body, ["不要", "禁止", "修复", "清理", "避免"], max_len=100)
                brief = {
                    "is_real_conflict": True,
                    "domain_label": "行为规则提案",
                    "decision_question": f"是否批准将「{prop_title}」写入全局行为规则（AGENTS.md）？",
                    "conflict_point": f"{prop_agent} 复盘后提议新增一条全局约束：{new_sum}",
                    "old_summary": "不新增该规则，维持当前 AGENTS.md 不变。",
                    "new_summary": new_sum,
                    "old_choice_label": "暂不收录",
                    "new_choice_label": "批准写入规则",
                    "recommendation": "manual",
                    "recommendation_reason": "该条目来自《规则待审》清单，等待你审核后并入 ~/.agents/AGENTS.md。",
                    "brief_source": "heuristic",
                    "model_used": "",
                }
                item = {
                    "kind": "rule_proposal",
                    "topic": "rule_proposal",
                    "topic_label": "行为规则提案",
                    "reason": brief["conflict_point"],
                    "existing": {
                        "fact_id": old_fid,
                        "statement": old_text,
                        "verbatim": old_text,
                        "source": "~/.agents/AGENTS.md",
                        "doc_title": "现有全局行为规则",
                        "source_label": "现有 AGENTS.md（未收录）",
                        "timestamp": date_ts,
                        "category": "taboo",
                        "agent": "agents_md",
                    },
                    "incoming": {
                        "fact_id": new_fid,
                        "statement": body[:1200],
                        "verbatim": f"{heading}\n\n{body}",
                        "source": f"_规则待审.md#{prop_title}",
                        "doc_title": prop_title,
                        "source_label": f"规则待审 · {heading}",
                        "timestamp": date_ts,
                        "category": "taboo",
                        "agent": prop_agent,
                    },
                    "decision_brief": brief,
                }
                item["conflict_id"] = conflict_fingerprint(item)
                gov_conflicts.append(item)

    return extra_records, gov_conflicts


def _discover_shared_memory_md_files(db: SimingDB, max_files: int = 200) -> list[Path]:
    """发现 Obsidian 共享记忆根目录下的经验笔记（排除 hindsight-wiki、目录索引与敏感文件）。"""
    skip_names = {"active_waits.md", "payment_guide.md", "readme.md", "memory.md", "_规则待审.md", "msim-evolution-diary.md"}
    root = db.paths.shared_memory_dir
    if not root.is_dir():
        return []
    found: list[Path] = []
    for p in sorted(root.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".md":
            continue
        if p.name.startswith(".") or p.name.lower() in skip_names:
            continue
        found.append(p)
        if len(found) >= max_files:
            break
    return found


def _read_recent_local_session_records(
    max_files: int = 6,
    tail_bytes: int = 98304,
    lookback_days: int = 7,
) -> tuple[int, list[dict[str, Any]]]:
    """轻量尾部抽样最近修改的本机 Claude/Codex/Hermes 会话日志（严控 <100KB/文件，保证 <150ms）。"""
    from siming.session_scanner import META_SKIP_RE, _extract_user_texts_from_jsonl_line

    home = Path.home()
    patterns = (
        ("claude", str(home / ".claude/projects/*/*.jsonl")),
        ("codex", str(home / ".codex/sessions/**/*.jsonl")),
        ("hermes", str(home / ".hermes/sessions/*.jsonl")),
    )
    cutoff = siming_db.now_local().timestamp() - lookback_days * 86400
    candidates: list[tuple[float, str, Path]] = []
    for agent, pat in patterns:
        for fp in glob.glob(pat, recursive=True):
            if "worktrees" in fp and "siming" in fp:
                continue
            p = Path(fp)
            try:
                st = p.stat()
            except OSError:
                continue
            if p.is_file() and st.st_size > 0 and st.st_mtime >= cutoff:
                candidates.append((st.st_mtime, agent, p))

    candidates.sort(key=lambda x: x[0], reverse=True)
    picked = candidates[:max_files]
    records: list[dict[str, Any]] = []

    for mtime, agent, path in picked:
        ts_file = iso_now(datetime.fromtimestamp(mtime).astimezone(SHANGHAI_TZ))
        try:
            with path.open("rb") as fh:
                sz = path.stat().st_size
                if sz > tail_bytes:
                    fh.seek(sz - tail_bytes)
                    raw = fh.read()
                    nl = raw.find(b"\n")
                    if nl >= 0:
                        raw = raw[nl + 1 :]
                else:
                    raw = fh.read()
        except OSError:
            continue

        for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
            for txt in _extract_user_texts_from_jsonl_line(agent, line):
                if len(txt) < 8 or len(txt) > 360 or contains_otp(txt) or META_SKIP_RE.search(txt):
                    continue
                if "你在隔离工作目录" in txt and "执行用户预授权任务" in txt:
                    continue
                if _is_noise_utterance(txt):
                    continue
                records.append(
                    {
                        "text": txt,
                        "source": f"{agent}:{path.name}#tail{idx}",
                        "doc_title": f"{agent} 近期会话",
                        "agent": agent,
                        "timestamp": ts_file,
                    }
                )
    return len(picked), records


def _load_local_magpie_gateway() -> tuple[str, str]:
    """只读读取本机 Magpie 网关地址与本地路由口令（绝不落盘、绝不打印）。"""
    env_url = os.environ.get("SIMING_CONFLICT_LLM_URL", "").strip()
    env_tok = os.environ.get("SIMING_CONFLICT_LLM_TOKEN", "").strip()
    if env_url and env_tok:
        return env_url.rstrip("/"), env_tok
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.is_file():
        return "", ""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        env = data.get("env") or {}
        base = str(env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
        tok = str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
        if base.startswith(("http://127.0.0.1:", "http://localhost:")) and tok:
            return base, tok
    except Exception:
        pass
    return "", ""


def _parse_llm_json_array(raw_text: str) -> list[dict[str, Any]]:
    s = (raw_text or "").strip()
    if not s:
        return []
    # 清理模型偶尔输出的代码块围栏或 <br>
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    m_fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.IGNORECASE)
    if m_fence:
        s = m_fence.group(1).strip()
    start = s.find("[")
    end = s.rfind("]")
    if start >= 0 and end > start:
        s = s[start : end + 1]
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except Exception:
        pass
    return []


def _call_magpie_brief_batch(
    batch: list[dict[str, Any]],
    *,
    base_url: str,
    token: str,
    timeout_sec: float = 45.0,
) -> tuple[str, list[dict[str, Any]]]:
    """批量调用本机 Magpie 网关做语义真伪复核与人话裁决简报提炼（直连 127.0.0.1，绕开代理）。"""
    import httpx

    items_payload = []
    for c in batch:
        ex = c.get("existing") or {}
        inc = c.get("incoming") or {}
        items_payload.append(
            {
                "conflict_id": c["conflict_id"],
                "kind": c.get("kind") or "contradiction",
                "old_date": str(ex.get("timestamp") or "")[:10],
                "old_source": str(ex.get("source_label") or ex.get("source") or ""),
                "old_text": str(ex.get("verbatim") or ex.get("statement") or "")[:1200],
                "new_date": str(inc.get("timestamp") or "")[:10],
                "new_source": str(inc.get("source_label") or inc.get("source") or ""),
                "new_text": str(inc.get("verbatim") or inc.get("statement") or "")[:1200],
            }
        )

    prompt = (
        "你是个人知识库的「冲突裁决秘书」。用户不想通读两段长原文，也不想看机器术语（绝不出现 positive/negative/model_routing 等英文代码词）。\n"
        "请逐条审阅下面 JSON 数组里的候选冲突，严格返回等长 JSON 数组（严禁输出 Markdown 围栏或 HTML 标签、严禁输出多余解释），每项包含字段：\n"
        "- conflict_id: 原样返回\n"
        "- is_real_conflict: 布尔值。如果两条内容根本风马牛不相及、只是碰巧出现同词或属于同一件事的补充细节（并无矛盾也无新旧覆盖淘汰关系），填 false；如果确实存在前后改口/覆盖/取消/互斥选型/原则冲突/待审提案，填 true（注意 kind 为 policy_conflict 或 rule_proposal 时必须为 true）。\n"
        "- domain_label: 4-8字纯中文领域名（如「网购保价」「新房家电」「新房网络」「AI 模型路由」「核心原则待拍板」「行为规则提案」）\n"
        "- decision_question: 一句话大白话裁决问题（<=42字），让用户不用读原文就能直接判断\n"
        "- conflict_point: 一句话说清两边的核心分歧点（<=80字）\n"
        "- old_summary: 旧说法的核心主张（<=65字，纯人话，无 Markdown 符号）\n"
        "- new_summary: 新说法的核心主张（<=65字，纯人话，无 Markdown 符号）\n"
        "- old_choice_label: 选旧说法的极简按钮文案（<=14字，自然中文短语，不半截断字）\n"
        "- new_choice_label: 选新说法的极简按钮文案（<=14字，自然中文短语，不半截断字）\n"
        "- recommendation: 只能是 keep_new / keep_old / both_valid / manual 之一\n"
        "- recommendation_reason: 一句话给用户的建议与理由（<=70字）；若 is_real_conflict 为 false，这里写明为什么实际不冲突。\n\n"
        f"待审条目：\n{json.dumps(items_payload, ensure_ascii=False)}"
    )

    models_to_try = ("group/claude-haiku-5-5-auto[1m]", "group/auto-deepseek-v4-1-flash")
    with httpx.Client(timeout=timeout_sec, trust_env=False) as client:
        for model_name in models_to_try:
            try:
                req_body: dict[str, Any] = {
                    "model": model_name,
                    "max_tokens": 8192,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if "claude" in model_name:
                    req_body["thinking"] = {"type": "disabled"}
                resp = client.post(
                    f"{base_url}/v1/messages",
                    headers={
                        "x-api-key": token,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=req_body,
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
                parts = [
                    str(blk.get("text") or "")
                    for blk in (body.get("content") or [])
                    if isinstance(blk, dict) and blk.get("type") == "text"
                ]
                parsed = _parse_llm_json_array("\n".join(parts))
                if parsed:
                    return model_name, parsed
            except Exception:
                continue
    return "", []


def enrich_conflicts_with_llm(
    db: SimingDB,
    conflicts: list[dict[str, Any]],
    *,
    force: bool = False,
    allow_llm: bool = True,
    batch_size: int = 5,
    llm_caller: Optional[Any] = None,
) -> dict[str, Any]:
    """为候选冲突批量做语义真伪复核与人话裁决简报提炼，持久化缓存到 `wiki_conflict_briefs`。

    - 缓存按 `(conflict_id, content_hash)` 命中时 0ms 跳过，不重复消耗模型；
    - 离线/测试（`SIMING_DISABLE_LLM=1` 或 `SIMING_TEST_OFFLINE=1`）自动静默降级为确定性人话简报；
    - `policy_conflict` / `rule_proposal` 绝不被误标为 false_positive。
    """
    from concurrent.futures import ThreadPoolExecutor

    db.init_db()
    cached = _load_conflict_briefs(db)
    ts = iso_now()

    to_process: list[dict[str, Any]] = []
    for c in conflicts:
        cid = str(c.get("conflict_id") or conflict_fingerprint(c))
        c["conflict_id"] = cid
        chash = _conflict_content_hash(c)
        hit = cached.get(cid)
        if (
            not force
            and hit
            and str(hit.get("content_hash") or "") == chash
            and (not allow_llm or str(hit.get("brief_source") or "") == "llm")
        ):
            continue
        to_process.append(c)

    llm_enabled = (
        allow_llm
        and os.environ.get("SIMING_DISABLE_LLM") != "1"
        and os.environ.get("SIMING_TEST_OFFLINE") != "1"
    )
    if llm_caller is not None:
        llm_enabled = allow_llm

    base_url, token = ("", "")
    if llm_enabled and llm_caller is None:
        base_url, token = _load_local_magpie_gateway()
        if not base_url or not token:
            llm_enabled = False

    chunks = [
        to_process[start : start + max(1, int(batch_size))]
        for start in range(0, len(to_process), max(1, int(batch_size)))
    ]

    def _run_chunk(chunk: list[dict[str, Any]]) -> tuple[str, dict[str, dict[str, Any]]]:
        if not llm_enabled or not chunk:
            return "", {}
        try:
            if llm_caller is not None:
                used_m, rows = llm_caller(chunk)
            else:
                used_m, rows = _call_magpie_brief_batch(chunk, base_url=base_url, token=token)
            by_id = {str(r.get("conflict_id") or "").strip(): r for r in rows if str(r.get("conflict_id") or "").strip()}
            return used_m, by_id
        except Exception:
            return "", {}

    if llm_enabled and len(chunks) > 1 and llm_caller is None:
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
            chunk_results = list(pool.map(_run_chunk, chunks))
    else:
        chunk_results = [_run_chunk(ch) for ch in chunks]

    llm_updated = 0
    heuristic_updated = 0
    filtered_count = 0
    model_used_last = ""

    for chunk, (used_model, llm_by_id) in zip(chunks, chunk_results):
        if used_model:
            model_used_last = used_model
        for c in chunk:
            cid = c["conflict_id"]
            fallback = build_conflict_decision_brief(c)
            llm_row = llm_by_id.get(cid)
            if llm_row and str(llm_row.get("decision_question") or "").strip():
                is_real = bool(llm_row.get("is_real_conflict", True))
                if str(c.get("kind") or "") in ("policy_conflict", "rule_proposal"):
                    is_real = True
                rec = str(llm_row.get("recommendation") or fallback["recommendation"]).strip().lower()
                if rec not in ("keep_new", "keep_old", "both_valid", "manual"):
                    rec = fallback["recommendation"]
                brief = {
                    "is_real_conflict": is_real,
                    "domain_label": _sanitize_wiki_text(str(llm_row.get("domain_label") or fallback["domain_label"]), paths=db.paths).strip(),
                    "decision_question": _sanitize_wiki_text(str(llm_row.get("decision_question") or fallback["decision_question"]), paths=db.paths).strip(),
                    "conflict_point": _sanitize_wiki_text(str(llm_row.get("conflict_point") or fallback["conflict_point"]), paths=db.paths).strip(),
                    "old_summary": _sanitize_wiki_text(str(llm_row.get("old_summary") or fallback["old_summary"]), paths=db.paths).strip(),
                    "new_summary": _sanitize_wiki_text(str(llm_row.get("new_summary") or fallback["new_summary"]), paths=db.paths).strip(),
                    "old_choice_label": _sanitize_wiki_text(str(llm_row.get("old_choice_label") or fallback["old_choice_label"]), paths=db.paths).strip(),
                    "new_choice_label": _sanitize_wiki_text(str(llm_row.get("new_choice_label") or fallback["new_choice_label"]), paths=db.paths).strip(),
                    "recommendation": rec,
                    "recommendation_reason": _sanitize_wiki_text(str(llm_row.get("recommendation_reason") or fallback["recommendation_reason"]), paths=db.paths).strip(),
                    "brief_source": "llm",
                    "model_used": used_model or "llm",
                }
                llm_updated += 1
                if not is_real:
                    filtered_count += 1
            else:
                brief = dict(fallback)
                brief["brief_source"] = "heuristic"
                brief["model_used"] = ""
                heuristic_updated += 1
            c["decision_brief"] = brief
            c["topic_label"] = brief["domain_label"]
            c["reason"] = brief["conflict_point"]
            _save_conflict_brief(db, c, brief, ts=ts)

    return {
        "ok": True,
        "processed": len(to_process),
        "llm_updated": llm_updated,
        "heuristic_updated": heuristic_updated,
        "filtered_noise": filtered_count,
        "model_used": model_used_last,
    }


def distill_hindsight_wiki(
    db: SimingDB,
    session_files: Optional[list[Path | str]] = None,
    wiki_dir: Optional[Path | str] = None,
    now: Optional[datetime] = None,
    include_db_sources: Optional[bool] = None,
    include_local_sessions: Optional[bool] = None,
    include_governance_sources: Optional[bool] = None,
    summarize_with_llm: Optional[bool] = None,
) -> dict[str, Any]:
    """扫描多 Agent 会话/笔记 + DB 记忆 + 原则待拍板/规则待审 -> 抽取四类事实 -> 高精度冲突检测与人话裁决点提炼 -> 写入共享 Wiki。"""
    db.init_db()
    ts = iso_now(now)
    target_dir = (
        Path(wiki_dir)
        if wiki_dir is not None
        else (db.paths.shared_memory_dir / "hindsight-wiki")
    )
    _reject_slash_tmp(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    explicit_files = session_files is not None
    use_db = (not explicit_files) if include_db_sources is None else bool(include_db_sources)
    use_governance = (not explicit_files) if include_governance_sources is None else bool(include_governance_sources)
    use_local_sessions = (
        (not explicit_files and os.environ.get("SIMING_TEST_OFFLINE") != "1")
        if include_local_sessions is None
        else bool(include_local_sessions)
    )
    use_llm = (
        (not explicit_files and os.environ.get("SIMING_TEST_OFFLINE") != "1" and os.environ.get("SIMING_DISABLE_LLM") != "1")
        if summarize_with_llm is None
        else bool(summarize_with_llm)
    )

    files: list[Path | str] = list(session_files or [])
    if not explicit_files:
        inbox_dir = db.paths.shared_inbox_dir
        if inbox_dir.is_dir():
            files.extend(
                sorted(
                    p
                    for p in inbox_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in (".jsonl", ".md", ".txt")
                )
            )
        files.extend(_discover_shared_memory_md_files(db, max_files=200))

    records = _read_session_records(files)
    db_record_count = 0
    if use_db:
        db_recs = _collect_db_records(db, ts)
        db_record_count = len(db_recs)
        records.extend(db_recs)

    gov_conflicts: list[dict[str, Any]] = []
    if use_governance:
        gov_recs, gov_conflicts = _collect_governance_sources(db, ts)
        records.extend(gov_recs)

    local_sessions_scanned = 0
    if use_local_sessions:
        try:
            local_sessions_scanned, sess_recs = _read_recent_local_session_records(max_files=6)
            records.extend(sess_recs)
        except Exception:
            local_sessions_scanned = 0

    new_facts = extract_facts_from_utterances(records, paths=db.paths)
    existing_facts = _load_existing_wiki_facts(db)

    # 结合人工裁决表：剔除已被裁定淘汰的事实（loser_fact_id），防止下次蒸馏死灰复燃
    _, suppressed_fact_ids, resolved_conflict_ids = _load_conflict_decisions(db)

    # 合并已有事实 + 新事实（按 (category, normalized_text) 与 fact_id 双重去重，避免同句出现两份 ID）
    merged_by_text: dict[tuple[str, str], dict[str, Any]] = {}
    for f in existing_facts + new_facts:
        fid = str(f.get("fact_id") or "")
        if not fid or fid in suppressed_fact_ids:
            continue
        txt_key = (str(f.get("category") or "preference"), re.sub(r"\s+", " ", str(f.get("verbatim") or f.get("statement") or "")).strip())
        if not txt_key[1]:
            continue
        prev = merged_by_text.get(txt_key)
        # 优先保留含更完整 verbatim 与 doc_title 的较新版本
        if prev is None or len(str(f.get("verbatim") or "")) >= len(str(prev.get("verbatim") or "")):
            merged_by_text[txt_key] = f

    all_facts_raw = list(merged_by_text.values())
    all_facts: list[dict[str, Any]] = []
    for cat in CATEGORY_META:
        cat_items = [f for f in all_facts_raw if f.get("category") == cat]
        cat_items.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
        all_facts.extend(cat_items[:300])

    # 对合并后的全量事实做高精度冲突检测，并合入治理级待拍板/规则提案项
    raw_conflicts = detect_fact_conflicts(all_facts, all_facts)
    existing_cids = {c["conflict_id"] for c in raw_conflicts}
    for gc in gov_conflicts:
        if gc["conflict_id"] not in existing_cids:
            raw_conflicts.append(gc)
            existing_cids.add(gc["conflict_id"])

    # 跳过已人工裁决的冲突对
    active_candidates = [c for c in raw_conflicts if c.get("conflict_id") not in resolved_conflict_ids]

    # 清理不再存在于当前候选集与历史裁决中的过期 brief 缓存，防止历史误报残留在「已筛除误报」里
    valid_brief_ids = {c["conflict_id"] for c in raw_conflicts} | resolved_conflict_ids
    with db.transaction() as conn:
        _ensure_conflict_briefs_table(conn)
        if valid_brief_ids:
            placeholders = ",".join("?" for _ in valid_brief_ids)
            conn.execute(f"DELETE FROM wiki_conflict_briefs WHERE conflict_id NOT IN ({placeholders})", tuple(valid_brief_ids))
        else:
            conn.execute("DELETE FROM wiki_conflict_briefs")

    # 批量生成/缓存人话裁决简报（在线模式下同步通过本机 Magpie 快速语义过滤伪冲突）
    brief_stats = enrich_conflicts_with_llm(db, active_candidates, force=False, allow_llm=use_llm)
    cached_briefs = _load_conflict_briefs(db)

    conflicts: list[dict[str, Any]] = []
    filtered_noise_cnt = 0
    for c in active_candidates:
        cid = c["conflict_id"]
        b_row = cached_briefs.get(cid)
        if b_row and int(b_row.get("is_real_conflict", 1)) == 0 and c.get("kind") not in ("policy_conflict", "rule_proposal"):
            filtered_noise_cnt += 1
            continue
        if b_row:
            brief = {
                "is_real_conflict": bool(b_row.get("is_real_conflict", 1)),
                "domain_label": str(b_row.get("domain_label") or c.get("topic_label") or ""),
                "decision_question": str(b_row.get("decision_question") or ""),
                "conflict_point": str(b_row.get("conflict_point") or c.get("reason") or ""),
                "old_summary": str(b_row.get("old_summary") or ""),
                "new_summary": str(b_row.get("new_summary") or ""),
                "old_choice_label": str(b_row.get("old_choice_label") or ""),
                "new_choice_label": str(b_row.get("new_choice_label") or ""),
                "recommendation": str(b_row.get("recommendation") or "manual"),
                "recommendation_reason": str(b_row.get("recommendation_reason") or ""),
                "brief_source": str(b_row.get("brief_source") or "heuristic"),
                "model_used": str(b_row.get("model_used") or ""),
            }
            c["decision_brief"] = brief
            c["topic_label"] = brief["domain_label"] or c.get("topic_label") or human_topic_label(c.get("topic", ""))
            c["reason"] = brief["conflict_point"] or c.get("reason") or ""
        conflicts.append(c)

    pages_written: list[str] = []
    with db.transaction() as conn:
        _ensure_conflict_decisions_table(conn)
        _ensure_conflict_briefs_table(conn)
        for cat, (slug, title) in CATEGORY_META.items():
            cat_facts = [f for f in all_facts if f.get("category") == cat]
            cat_ids = {f["fact_id"] for f in cat_facts}
            cat_conflicts = [
                c
                for c in conflicts
                if (
                    c["existing"]["fact_id"] in cat_ids
                    or c["incoming"]["fact_id"] in cat_ids
                    or (c.get("kind") == "policy_conflict" and cat == "decision")
                    or (c.get("kind") == "rule_proposal" and cat == "taboo")
                )
            ]

            content_md = _render_wiki_page_markdown(title, ts, cat_facts, cat_conflicts)
            page_path = target_dir / f"{slug}.md"
            page_path.write_text(content_md, encoding="utf-8")
            pages_written.append(str(page_path))

            conn.execute(
                """
                INSERT INTO wiki_pages (
                    slug, title, category, content_md, sources_json, conflicts_json,
                    updated_at, last_distilled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    content_md = excluded.content_md,
                    sources_json = excluded.sources_json,
                    conflicts_json = excluded.conflicts_json,
                    updated_at = excluded.updated_at,
                    last_distilled_at = excluded.last_distilled_at
                """,
                (
                    slug,
                    title,
                    cat,
                    content_md,
                    json.dumps(cat_facts, ensure_ascii=False),
                    json.dumps(cat_conflicts, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )

    total_scanned = len(files) + local_sessions_scanned
    index_lines = [
        "# Hindsight 共享记忆 Wiki 总览",
        "",
        f"- 最近蒸馏：`{ts}`",
        f"- 扫描文件：`{total_scanned}` 个 | DB 记忆源：`{db_record_count}` 条 | 新抽取事实：`{len(new_facts)}` 条 | 累计事实：`{len(all_facts)}` 条 | 待裁决冲突：`{len(conflicts)}` 处（已筛除误报 `{filtered_noise_cnt}` 处）",
        "",
        "## 页面目录",
        "",
    ]
    for cat, (slug, title) in CATEGORY_META.items():
        cnt = sum(1 for f in all_facts if f.get("category") == cat)
        index_lines.append(f"- [{title}](./{slug}.md) — `{cnt}` 条")
    if conflicts:
        index_lines.extend(["", "## ⚠️ 待裁决冲突清单（全量，已提炼裁决点）", ""])
        for c in conflicts:
            b = build_conflict_decision_brief(c)
            index_lines.append(f"- **[{b['domain_label']}]** {b['decision_question']} — {b['conflict_point']}")

    index_path = target_dir / "INDEX.md"
    index_path.write_text("\n".join(index_lines).rstrip() + "\n", encoding="utf-8")

    return {
        "ok": True,
        "wiki_dir": str(target_dir),
        "index_path": str(index_path),
        "files_scanned": total_scanned,
        "db_records_scanned": db_record_count,
        "local_sessions_scanned": local_sessions_scanned,
        "facts_extracted": len(new_facts),
        "total_facts": len(all_facts),
        "conflicts": conflicts,
        "filtered_noise": filtered_noise_cnt,
        "brief_stats": brief_stats,
        "pages_written": pages_written,
        "distilled_at": ts,
    }


def query_hindsight_wiki(
    db: SimingDB,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """检索共享 Wiki 页面与事实出处，返回按相关度排序的命中片段。"""
    db.init_db()
    q = (query or "").strip()
    if not q:
        return []
    tokens = _subject_tokens(q) or {q.lower()}
    bigrams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", q):
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in _STOP_WORDS:
                bigrams.add(bg)

    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM wiki_pages ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for r in rows:
        content = str(r["content_md"] or "")
        title = str(r["title"] or "")
        sources = json.loads(str(r["sources_json"] or "[]"))
        conflicts = json.loads(str(r["conflicts_json"] or "[]"))

        hay_low = f"{title}\n{content}".lower()
        hits = float(sum(2 for t in tokens if t in hay_low))
        hits += 0.5 * sum(1 for bg in bigrams if bg in hay_low)
        if q.lower() in hay_low:
            hits += 3.0
        if hits <= 0.0:
            continue

        search_terms = tokens | bigrams
        matched_lines = [
            ln.strip()
            for ln in content.splitlines()
            if ln.strip() and not ln.strip().startswith("#") and any(t in ln.lower() for t in search_terms)
        ]
        snippet = "\n".join(matched_lines[:4]) if matched_lines else content[:240]

        results.append(
            {
                "slug": str(r["slug"]),
                "title": title,
                "category": str(r["category"]),
                "score": float(hits),
                "snippet": snippet,
                "sources_count": len(sources),
                "conflicts_count": len(conflicts),
                "sources": sources,
                "conflicts": conflicts,
                "updated_at": str(r["updated_at"]),
            }
        )

    results.sort(key=lambda x: (x["score"], x["updated_at"]), reverse=True)
    return results[: max(1, int(limit))]


def format_wiki_status(db: SimingDB) -> str:
    """格式化 Hindsight 共享 Wiki 状态面板。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM wiki_pages ORDER BY slug ASC").fetchall()
    finally:
        conn.close()

    if not rows:
        return "📚 **Hindsight 共享 Wiki**：尚未蒸馏任何会话页面。"

    q = list_wiki_conflicts(db, include_resolved=False, include_filtered=False)
    total_facts = 0
    latest_ts = ""
    lines = ["📚 **Hindsight 自动共享 Wiki 状态**（Tell one agent, every agent knows）"]
    for r in rows:
        sources = json.loads(str(r["sources_json"] or "[]"))
        conflicts = json.loads(str(r["conflicts_json"] or "[]"))
        total_facts += len(sources)
        ts = str(r["last_distilled_at"] or "")
        if ts > latest_ts:
            latest_ts = ts
        warn_s = f" · ⚠️ {len(conflicts)} 冲突" if conflicts else ""
        lines.append(f"• `{r['slug']}` {r['title']}：{len(sources)} 条事实{warn_s}")

    lines.append(
        f"• 合计：`{len(rows)}` 页 / `{total_facts}` 条事实 / `{q['counts']['pending']}` 处待裁决冲突（最近蒸馏：`{latest_ts[:19]}`）"
    )
    return "\n".join(lines)


VALID_CONFLICT_DECISIONS = frozenset({"keep_new", "keep_old", "both_valid", "defer"})

_KIND_SORT_ORDER = {
    "policy_conflict": 0,
    "rule_proposal": 1,
    "contradiction": 2,
    "staleness": 3,
}


def _attach_brief_to_item(item: dict[str, Any], cached_briefs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cid = str(item.get("conflict_id") or "")
    b_row = cached_briefs.get(cid)
    if b_row and str(b_row.get("decision_question") or "").strip():
        brief = {
            "is_real_conflict": bool(b_row.get("is_real_conflict", 1)),
            "domain_label": str(b_row.get("domain_label") or ""),
            "decision_question": str(b_row.get("decision_question") or ""),
            "conflict_point": str(b_row.get("conflict_point") or ""),
            "old_summary": str(b_row.get("old_summary") or ""),
            "new_summary": str(b_row.get("new_summary") or ""),
            "old_choice_label": str(b_row.get("old_choice_label") or ""),
            "new_choice_label": str(b_row.get("new_choice_label") or ""),
            "recommendation": str(b_row.get("recommendation") or "manual"),
            "recommendation_reason": str(b_row.get("recommendation_reason") or ""),
            "brief_source": str(b_row.get("brief_source") or "heuristic"),
            "model_used": str(b_row.get("model_used") or ""),
        }
    else:
        brief = build_conflict_decision_brief(item)
    item["decision_brief"] = brief
    item["topic_label"] = brief["domain_label"] or human_topic_label(
        str(item.get("topic") or ""), item.get("existing"), item.get("incoming")
    )
    if brief.get("conflict_point"):
        item["reason"] = brief["conflict_point"]
    return item


def list_wiki_conflicts(
    db: SimingDB,
    *,
    include_resolved: bool = True,
    include_filtered: bool = True,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """跨所有 Wiki 页面去重汇总冲突审核队列（pending / deferred / resolved / filtered），每条附带人话裁决简报与完整原文。"""
    db.init_db()
    cur = now or siming_db.now_local()
    decisions, suppressed_facts, _ = _load_conflict_decisions(db)
    cached_briefs = _load_conflict_briefs(db)

    conn = db.connect()
    try:
        rows = conn.execute("SELECT slug, title, category, sources_json, conflicts_json FROM wiki_pages ORDER BY slug").fetchall()
    finally:
        conn.close()

    facts_by_id: dict[str, dict[str, Any]] = {}
    unique_conflicts: dict[str, dict[str, Any]] = {}
    for r in rows:
        slug = str(r["slug"])
        title = str(r["title"])
        cat = str(r["category"])
        try:
            srcs = json.loads(str(r["sources_json"] or "[]"))
        except Exception:
            srcs = []
        for f in srcs:
            if isinstance(f, dict) and f.get("fact_id"):
                facts_by_id[str(f["fact_id"])] = f
        try:
            cfs = json.loads(str(r["conflicts_json"] or "[]"))
        except Exception:
            cfs = []
        for c in cfs:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("conflict_id") or conflict_fingerprint(c))
            ex = dict(c.get("existing") or {})
            inc = dict(c.get("incoming") or {})
            ex_id = str(ex.get("fact_id") or "")
            inc_id = str(inc.get("fact_id") or "")
            if ex_id in suppressed_facts or inc_id in suppressed_facts:
                continue
            if cid not in unique_conflicts:
                unique_conflicts[cid] = {
                    "conflict_id": cid,
                    "kind": str(c.get("kind") or "contradiction"),
                    "topic": str(c.get("topic") or "general"),
                    "topic_label": str(c.get("topic_label") or ""),
                    "reason": str(c.get("reason") or ""),
                    "decision_ref": str(c.get("decision_ref") or ""),
                    "flipped_terms": list(c.get("flipped_terms") or []),
                    "existing": ex,
                    "incoming": inc,
                    "decision_brief": c.get("decision_brief"),
                    "pages": [{"slug": slug, "title": title, "category": cat}],
                }
            else:
                if not any(p["slug"] == slug for p in unique_conflicts[cid]["pages"]):
                    unique_conflicts[cid]["pages"].append({"slug": slug, "title": title, "category": cat})

    pending: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []

    for cid, item in unique_conflicts.items():
        for side in ("existing", "incoming"):
            fid = str(item[side].get("fact_id") or "")
            full = facts_by_id.get(fid)
            if full:
                item[side].setdefault("category", str(full.get("category") or ""))
                item[side].setdefault("agent", str(full.get("agent") or ""))
                item[side].setdefault("doc_title", str(full.get("doc_title") or ""))
                full_v = str(full.get("verbatim") or full.get("statement") or "")
                cur_v = str(item[side].get("verbatim") or item[side].get("statement") or "")
                if len(full_v) >= len(cur_v):
                    item[side]["verbatim"] = full_v
                    item[side]["statement"] = full_v[:1200]
            if not item[side].get("verbatim"):
                item[side]["verbatim"] = str(item[side].get("statement") or "")
            if not item[side].get("source_label"):
                item[side]["source_label"] = _friendly_source_name(
                    str(item[side].get("source") or ""), str(item[side].get("doc_title") or "")
                )

        _attach_brief_to_item(item, cached_briefs)

        # 若语义审校已判定为误报（且非治理级必审项），不进入 pending
        if not item["decision_brief"].get("is_real_conflict", True) and item["kind"] not in ("policy_conflict", "rule_proposal"):
            continue

        dec_row = decisions.get(cid)
        if dec_row:
            dec = str(dec_row.get("decision") or "")
            if dec in ("keep_new", "keep_old", "both_valid"):
                continue
            if dec == "defer":
                snz = str(dec_row.get("snooze_until") or "")
                still_snoozed = False
                if snz:
                    try:
                        still_snoozed = parse_iso(snz) > cur
                    except Exception:
                        still_snoozed = False
                if still_snoozed:
                    item["decision"] = "defer"
                    item["note"] = str(dec_row.get("note") or "")
                    item["decided_at"] = str(dec_row.get("decided_at") or "")
                    item["snooze_until"] = snz
                    deferred.append(item)
                    continue
        pending.append(item)

    if include_filtered:
        active_ids = {x["conflict_id"] for x in pending} | {x["conflict_id"] for x in deferred}
        for cid, b_row in cached_briefs.items():
            if int(b_row.get("is_real_conflict", 1)) != 0:
                continue
            if cid in active_ids or cid in decisions:
                continue
            try:
                snap = json.loads(str(b_row.get("conflict_snapshot_json") or "{}"))
            except Exception:
                snap = {}
            if not isinstance(snap, dict) or not snap.get("existing"):
                continue
            f_item = {
                "conflict_id": cid,
                "kind": str(snap.get("kind") or "contradiction"),
                "topic": str(snap.get("topic") or "general"),
                "reason": str(b_row.get("recommendation_reason") or snap.get("reason") or "AI 语义核验判定双方并非同一件事，不构成冲突"),
                "existing": dict(snap.get("existing") or {}),
                "incoming": dict(snap.get("incoming") or {}),
                "pages": list(snap.get("pages") or []),
            }
            for side in ("existing", "incoming"):
                if not f_item[side].get("verbatim"):
                    f_item[side]["verbatim"] = str(f_item[side].get("statement") or "")
                if not f_item[side].get("source_label"):
                    f_item[side]["source_label"] = _friendly_source_name(
                        str(f_item[side].get("source") or ""), str(f_item[side].get("doc_title") or "")
                    )
            _attach_brief_to_item(f_item, cached_briefs)
            filtered.append(f_item)
        filtered.sort(key=lambda x: str(x.get("conflict_id") or ""))

    if include_resolved:
        for cid, dec_row in decisions.items():
            dec = str(dec_row.get("decision") or "")
            if dec not in ("keep_new", "keep_old", "both_valid"):
                continue
            try:
                ex_snap = json.loads(str(dec_row.get("existing_snapshot_json") or "{}"))
            except Exception:
                ex_snap = {}
            try:
                inc_snap = json.loads(str(dec_row.get("incoming_snapshot_json") or "{}"))
            except Exception:
                inc_snap = {}
            for snap_side in (ex_snap, inc_snap):
                if isinstance(snap_side, dict):
                    if not snap_side.get("verbatim"):
                        snap_side["verbatim"] = str(snap_side.get("statement") or "")
                    if not snap_side.get("source_label"):
                        snap_side["source_label"] = _friendly_source_name(
                            str(snap_side.get("source") or ""), str(snap_side.get("doc_title") or "")
                        )
            r_item = {
                "conflict_id": cid,
                "kind": str(dec_row.get("kind") or "contradiction"),
                "topic": str(dec_row.get("topic") or ""),
                "decision": dec,
                "winner_fact_id": str(dec_row.get("winner_fact_id") or ""),
                "loser_fact_id": str(dec_row.get("loser_fact_id") or ""),
                "note": str(dec_row.get("note") or ""),
                "existing": ex_snap,
                "incoming": inc_snap,
                "decided_at": str(dec_row.get("decided_at") or ""),
            }
            _attach_brief_to_item(r_item, cached_briefs)
            resolved.append(r_item)
        resolved.sort(key=lambda x: str(x.get("decided_at") or ""), reverse=True)

    pending.sort(
        key=lambda x: (
            _KIND_SORT_ORDER.get(x["kind"], 9),
            0 if (x.get("decision_brief") or {}).get("recommendation") == "manual" else 1,
            x.get("topic_label") or x["topic"],
            x["conflict_id"],
        )
    )
    deferred.sort(key=lambda x: str(x.get("decided_at") or ""), reverse=True)

    by_kind: dict[str, int] = {}
    by_rec: dict[str, int] = {}
    domains: list[str] = []
    for x in pending:
        k = str(x.get("kind") or "contradiction")
        by_kind[k] = by_kind.get(k, 0) + 1
        rec = str((x.get("decision_brief") or {}).get("recommendation") or "manual")
        by_rec[rec] = by_rec.get(rec, 0) + 1
        dl = str(x.get("topic_label") or "")
        if dl and dl not in domains:
            domains.append(dl)

    return {
        "ok": True,
        "counts": {
            "pending": len(pending),
            "deferred": len(deferred),
            "resolved": len(resolved),
            "filtered": len(filtered),
            "total": len(pending) + len(deferred) + len(resolved),
            "by_kind": by_kind,
            "by_recommendation": by_rec,
        },
        "domains": sorted(domains),
        "pending": pending,
        "deferred": deferred,
        "resolved": resolved,
        "filtered": filtered,
    }


def summarize_wiki_conflicts(
    db: SimingDB,
    *,
    force: bool = True,
    use_llm: bool = True,
    llm_caller: Optional[Any] = None,
) -> dict[str, Any]:
    """重新为当前全部待审/暂缓冲突提炼人话裁决简报（支持 LLM 语义复核 + 过滤误报同步写回 wiki_pages）。"""
    db.init_db()
    q_before = list_wiki_conflicts(db, include_resolved=False, include_filtered=False)
    targets = list(q_before["pending"]) + list(q_before["deferred"])
    stats = enrich_conflicts_with_llm(db, targets, force=force, allow_llm=use_llm, llm_caller=llm_caller)

    # 若有条目被语义复核判为误报，同步从 wiki_pages.conflicts_json 中清理，保证侧栏与审核台计数一致
    cached_briefs = _load_conflict_briefs(db)
    false_ids = {
        cid
        for cid, row in cached_briefs.items()
        if int(row.get("is_real_conflict", 1)) == 0
    }
    if false_ids:
        ts = iso_now()
        with db.transaction() as conn:
            p_rows = conn.execute("SELECT slug, title, sources_json, conflicts_json FROM wiki_pages").fetchall()
            for pr in p_rows:
                slug = str(pr["slug"])
                title = str(pr["title"])
                srcs = json.loads(str(pr["sources_json"] or "[]"))
                cfs = json.loads(str(pr["conflicts_json"] or "[]"))
                kept = [
                    c
                    for c in cfs
                    if str(c.get("conflict_id") or conflict_fingerprint(c)) not in false_ids
                    or str(c.get("kind") or "") in ("policy_conflict", "rule_proposal")
                ]
                if len(kept) != len(cfs):
                    new_md = _render_wiki_page_markdown(title, ts, srcs, kept)
                    conn.execute(
                        "UPDATE wiki_pages SET content_md=?, conflicts_json=?, updated_at=? WHERE slug=?",
                        (new_md, json.dumps(kept, ensure_ascii=False), ts, slug),
                    )

    q_after = list_wiki_conflicts(db, include_resolved=True, include_filtered=True)
    return {"ok": True, "stats": stats, **q_after}


def restore_filtered_conflict(db: SimingDB, conflict_id: str) -> dict[str, Any]:
    """将被 AI 误筛除的冲突从「已筛除误报」恢复回「待裁决」队列。"""
    db.init_db()
    cid = (conflict_id or "").strip()
    ts = iso_now()
    with db.transaction() as conn:
        _ensure_conflict_briefs_table(conn)
        row = conn.execute("SELECT * FROM wiki_conflict_briefs WHERE conflict_id = ?", (cid,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"未找到已筛除记录 {cid}"}
        snap = json.loads(str(row["conflict_snapshot_json"] or "{}"))
        conn.execute(
            "UPDATE wiki_conflict_briefs SET is_real_conflict = 1, updated_at = ? WHERE conflict_id = ?",
            (ts, cid),
        )
        # 还原到对应 wiki_pages.conflicts_json
        if isinstance(snap, dict) and snap.get("existing"):
            pages = snap.get("pages") or [{"slug": "preferences"}]
            target_slugs = {str(p.get("slug") or "preferences") for p in pages}
            for pr in conn.execute("SELECT slug, title, sources_json, conflicts_json FROM wiki_pages").fetchall():
                slug = str(pr["slug"])
                if slug not in target_slugs:
                    continue
                cfs = json.loads(str(pr["conflicts_json"] or "[]"))
                if not any(str(c.get("conflict_id") or "") == cid for c in cfs):
                    cfs.append(snap)
                    srcs = json.loads(str(pr["sources_json"] or "[]"))
                    new_md = _render_wiki_page_markdown(str(pr["title"]), ts, srcs, cfs)
                    conn.execute(
                        "UPDATE wiki_pages SET content_md=?, conflicts_json=?, updated_at=? WHERE slug=?",
                        (new_md, json.dumps(cfs, ensure_ascii=False), ts, slug),
                    )
    counts = list_wiki_conflicts(db, include_resolved=True, include_filtered=True)["counts"]
    return {"ok": True, "conflict_id": cid, "restored": True, "counts": counts}


def resolve_wiki_conflict(
    db: SimingDB,
    conflict_id: str,
    decision: str,
    *,
    note: str = "",
    snooze_days: int = 7,
    wiki_dir: Optional[Path | str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """人工裁决单条 Wiki 冲突：keep_new / keep_old / both_valid / defer。

    - keep_new / keep_old：持久化胜出/淘汰事实，立即从 wiki_pages 剔除淘汰事实与关联冲突，且下次蒸馏不再复活；
    - both_valid：保留双方事实，永久抑制该冲突对；
    - defer：暂缓审阅（默认 7 天后重新浮出待审队列）。
    """
    db.init_db()
    cid = (conflict_id or "").strip()
    dec = (decision or "").strip().lower()
    if dec not in VALID_CONFLICT_DECISIONS:
        return {"ok": False, "error": f"无效裁决 {decision!r}，可选：keep_new / keep_old / both_valid / defer"}

    cur = now or siming_db.now_local()
    ts = iso_now(cur)
    cur_queue = list_wiki_conflicts(db, include_resolved=True, include_filtered=True, now=cur)
    target: Optional[dict[str, Any]] = None
    for bucket in (cur_queue["pending"], cur_queue["deferred"], cur_queue["resolved"], cur_queue["filtered"]):
        for item in bucket:
            if item["conflict_id"] == cid:
                target = item
                break
        if target is not None:
            break
    if target is None:
        return {"ok": False, "error": f"未找到冲突 {cid}"}

    ex = dict(target.get("existing") or {})
    inc = dict(target.get("incoming") or {})
    ex_id = str(ex.get("fact_id") or "")
    inc_id = str(inc.get("fact_id") or "")

    winner_id = ""
    loser_id = ""
    snooze_until: Optional[str] = None
    if dec == "keep_new":
        winner_id, loser_id = inc_id, ex_id
    elif dec == "keep_old":
        winner_id, loser_id = ex_id, inc_id
    elif dec == "defer":
        snooze_until = iso_now(cur + timedelta(days=max(1, int(snooze_days))))

    target_dir = Path(wiki_dir) if wiki_dir is not None else (db.paths.shared_memory_dir / "hindsight-wiki")
    pages_updated: list[str] = []

    with db.transaction() as conn:
        _ensure_conflict_decisions_table(conn)
        conn.execute(
            """
            INSERT INTO wiki_conflict_decisions (
                conflict_id, kind, topic, decision,
                existing_fact_id, incoming_fact_id, winner_fact_id, loser_fact_id,
                note, existing_snapshot_json, incoming_snapshot_json, decided_at, snooze_until
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conflict_id) DO UPDATE SET
                decision = excluded.decision,
                winner_fact_id = excluded.winner_fact_id,
                loser_fact_id = excluded.loser_fact_id,
                note = excluded.note,
                existing_snapshot_json = excluded.existing_snapshot_json,
                incoming_snapshot_json = excluded.incoming_snapshot_json,
                decided_at = excluded.decided_at,
                snooze_until = excluded.snooze_until
            """,
            (
                cid,
                str(target.get("kind") or "contradiction"),
                str(target.get("topic") or ""),
                dec,
                ex_id,
                inc_id,
                winner_id,
                loser_id,
                (note or "").strip()[:300],
                json.dumps(ex, ensure_ascii=False),
                json.dumps(inc, ensure_ascii=False),
                ts,
                snooze_until,
            ),
        )

        # 立即同步清理 wiki_pages 里的淘汰事实与已裁决冲突（无需等待下次夜间蒸馏）
        if dec in ("keep_new", "keep_old", "both_valid"):
            p_rows = conn.execute("SELECT slug, title, sources_json, conflicts_json FROM wiki_pages").fetchall()
            for pr in p_rows:
                slug = str(pr["slug"])
                title = str(pr["title"])
                try:
                    srcs = json.loads(str(pr["sources_json"] or "[]"))
                except Exception:
                    srcs = []
                try:
                    cfs = json.loads(str(pr["conflicts_json"] or "[]"))
                except Exception:
                    cfs = []

                new_srcs = [f for f in srcs if not (loser_id and str(f.get("fact_id") or "") == loser_id)]
                new_cfs = []
                for c in cfs:
                    c_fid = str(c.get("conflict_id") or conflict_fingerprint(c))
                    c_ex_id = str((c.get("existing") or {}).get("fact_id") or "")
                    c_in_id = str((c.get("incoming") or {}).get("fact_id") or "")
                    if c_fid == cid:
                        continue
                    if loser_id and (c_ex_id == loser_id or c_in_id == loser_id):
                        continue
                    new_cfs.append(c)

                if len(new_srcs) != len(srcs) or len(new_cfs) != len(cfs):
                    new_md = _render_wiki_page_markdown(title, ts, new_srcs, new_cfs)
                    conn.execute(
                        "UPDATE wiki_pages SET content_md=?, sources_json=?, conflicts_json=?, updated_at=? WHERE slug=?",
                        (
                            new_md,
                            json.dumps(new_srcs, ensure_ascii=False),
                            json.dumps(new_cfs, ensure_ascii=False),
                            ts,
                            slug,
                        ),
                    )
                    pages_updated.append(slug)
                    if target_dir.is_dir():
                        try:
                            (target_dir / f"{slug}.md").write_text(new_md, encoding="utf-8")
                        except OSError:
                            pass

    after_counts = list_wiki_conflicts(db, include_resolved=True, include_filtered=True, now=cur)["counts"]
    return {
        "ok": True,
        "conflict_id": cid,
        "decision": dec,
        "winner_fact_id": winner_id,
        "loser_fact_id": loser_id,
        "snooze_until": snooze_until,
        "pages_updated": pages_updated,
        "counts": after_counts,
    }


def reopen_wiki_conflict(db: SimingDB, conflict_id: str) -> dict[str, Any]:
    """撤销某条冲突裁决，使其在重新蒸馏或刷新后回到待审核状态。"""
    db.init_db()
    cid = (conflict_id or "").strip()
    with db.transaction() as conn:
        _ensure_conflict_decisions_table(conn)
        cur = conn.execute("DELETE FROM wiki_conflict_decisions WHERE conflict_id = ?", (cid,))
        deleted = cur.rowcount
    if not deleted:
        return {"ok": False, "error": f"未找到已裁决记录 {cid}"}
    counts = list_wiki_conflicts(db, include_resolved=True, include_filtered=True)["counts"]
    return {"ok": True, "conflict_id": cid, "reopened": True, "counts": counts}
