"""QQ 入站自然语言理解与泛化路由引擎（qq_nlu.py）。

三层泛化架构（守住 D-19 纯代码安全红线与零误劫持底线）：
- Layer 1 · 统一规范化与话语守卫（NormalizedUtterance）：
  剥离 `[Voice]`、客套称呼/前置语（司命/帮我/麻烦/请问/能不能/我想…）、查询动词（看看/查一下/告诉我…）与句末语气词（吧/呀/啊/呢/哈/一下…），
  同时严格识别问句、闲聊/第三方转述、概念科普、工程讨论/排障复盘等非指令语境，杜绝误劫持。
- Layer 2 · 声明式意图目录 + 概念帧槽位匹配 + 轻模糊纠错：
  36+ 预置只读视图统一声明在 `PRESET_VIEW_SPECS`，支持精确别名、多词概念帧（词序无关）与高阈值拼写/同音错别字容错；
  高频参数化动作（美团历史/复购/推荐/下单/选店/双拼、滴滴打车、查账/Sure、模块开关、挂起提醒、单据销账/取消、特斯拉、照护、保价、补货、入住、引用回复等）
  全部支持自然词序、中文数字、无空格与口语变体。
- Layer 3 · 可插拔语义兜底分类器（安全白名单限定只读视图与 A2 预览建卡，绝不直接执行 A2 审批/扣款）。
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from siming.db import SimingDB

# ---------------------------------------------------------------------------
# Layer 1 · 规范化与话语守卫
# ---------------------------------------------------------------------------

_VOICE_PREFIX_RE = re.compile(r"^\s*\[Voice\]\s*", re.IGNORECASE)
_STMT_TRAIL_RE = re.compile(r"[。！!~～\s]+$")
_QUERY_TRAIL_RE = re.compile(r"[。！？!?~～\s]+$")
_QUESTION_MARK_RE = re.compile(r"[?？]\s*$")

_LEAD_ADDRESS_RE = re.compile(
    r"^(?:(?:嘿|嗨|喂|你好|您好|哈喽|hello|hi|司命|小司命|助理)[，,、\s]*)+",
    re.IGNORECASE,
)
_LEAD_POLITE_RE = re.compile(
    r"^(?:(?:那个|嗯+|呃+|诶|请问|麻烦(?:你)?|劳驾|请你|请(?=查|看|帮|替|给|关|开|把|列|告|发|点|叫|打|提醒|问|搜)|"
    r"你能(?:不能)?|你可(?:以|不可以)|能不能|能否|可不可以|可以|"
    r"帮我|替我|给我|帮忙|顺手|顺便|赶紧|快点|现在|立刻|马上|我想|我要|我准备|我打算|我想要|"
    r"想(?=看|查|点|吃|喝|买|打|叫|知道|问|关|开|把|用)|要(?=看|查|点|吃|喝|买|打|叫|关|开|把))[，,\s]*)+",
    re.IGNORECASE,
)
_TRAIL_PARTICLE_RE = re.compile(
    r"(?:[，,\s]*(?:谢谢|多谢|感谢|好吗|行吗|可以吗|好不好|行不行|成不成|对吧|"
    r"(?<![酒网贴清氧餐水啡])吧|呀|啊|呢|哈|哦|噢|啦|咯|呗|嘛|(?<![什怎多这那])么|一下儿?|(?<=[看查点算问试发改关开记删销搜瞄瞧翻])下))+$"
)
_LEAD_QUERY_VERB_RE = re.compile(
    r"^(?:(?:查一下|查下|查一查|查询一下|查询|查看一下|查看|看看|看下|看一下|瞄一眼|瞧一眼|翻一下|翻翻|"
    r"列出|列一下|列出来|告诉我|汇报一下|报一下|显示(?:一下)?|调出|打开看看|发一下|发我|给我发|给我看|帮我看(?:一下|下)?|帮我查(?:一下|下)?|"
    r"问一下|问下|了解一下|核对一下|盘点一下|统计一下|汇总一下)[，,:\s：]*)+"
)
_TRAIL_QUERY_SUFFIX_RE = re.compile(
    r"(?:(?:怎么样|咋样|如何|啥情况|什么情况|情况如何|情况怎么样|进度怎么样|进度如何|状态怎么样|状态如何|"
    r"有哪些|有什么|都有啥|都有什么|是多少|有多少|还剩多少|还有多少|还有吗|有吗|吗|了没|了吗|没|的情况|的状态|的进度|列表|汇总|清单)?)$"
)

# 概念科普 / 工程规划 / 故障复盘 / 闲聊叙事守卫
_GENERAL_CONCEPT_RE = re.compile(
    r"(?:什么是|啥是|什么叫|科普(?:一下)?|解释(?:一下)?|介绍(?:一下)?|怎么理解|如何理解|是什么意思|是什么东西|是什么概念)"
)
_META_ENGINEERING_RE = re.compile(
    r"(?:以后再优化|以后再改|以后再说|这块以后|怎么实现|如何实现|写个代码|写一段|重构一下|为什么报错|"
    r"没有成功|没成功|失败了.*修复|修复一下|修复这个|被拒绝|我提了.*没有|我刚才说的是|我说的.+指的是|一般是每周)"
)
_THIRD_PARTY_STORY_RE = re.compile(
    r"^(?:我?(?:朋友|同事|同学|家人|老板|对象)说|听说|昨天(?:我去|在)|刚才(?:有个|看到))"
)
_WRITE_OR_MUTATE_CUE_RE = re.compile(
    r"(?:^(?:关闭|关掉|停用|开启|打开|启用|暂停|删除|删掉|移除|取消|更新|设置|设为|绑定|登记|添加|新增|修改|改成|改到|改约|推迟|派单|试跑|忘掉|记下|忽略|打包|销掉|消掉|核销|划掉)|"
    r"(?:改到|改约|推迟到|顺延到|挪到|移出|关掉|关了|打开|设为|改为|绑定为|绑定到|加购|下单|支付|付款|提醒我))"
)

_CN_DIGIT_SIMPLE = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5"}


@dataclass(frozen=True)
class NormalizedUtterance:
    raw: str
    stmt: str
    query: str
    core: str
    body: str
    topic: str
    compact: str
    had_question: bool
    has_query_verb: bool
    is_meta: bool
    is_multiline: bool


def normalize_qq_input(raw_text: str) -> NormalizedUtterance:
    """将 QQ 文本/语音输入归一化为多视角结构，同时保留原始标点以供安全守卫判断。"""
    raw = _VOICE_PREFIX_RE.sub("", (raw_text or "").strip()).strip()
    # 折叠连续空格/全角空格（保留换行用于多行识别）
    raw = re.sub(r"[ \t\u3000]+", " ", raw).strip()
    stmt = _STMT_TRAIL_RE.sub("", raw).strip()
    query = _QUERY_TRAIL_RE.sub("", raw).strip()
    had_question = bool(_QUESTION_MARK_RE.search(raw))
    is_multiline = "\n" in raw and len([ln for ln in raw.splitlines() if ln.strip()]) >= 2

    # 剥离客套呼唤、礼貌前缀与句末语气词
    core = query
    for _ in range(3):
        prev = core
        core = _LEAD_ADDRESS_RE.sub("", core).strip()
        core = _LEAD_POLITE_RE.sub("", core).strip()
        core = _TRAIL_PARTICLE_RE.sub("", core).strip()
        core = core.strip("，,、:：")
        if core == prev:
            break
    core = core or query

    body = _LEAD_QUERY_VERB_RE.sub("", core).strip()
    has_query_verb = body != core or bool(
        re.search(r"(?:看看|看下|看一下|查一下|查下|查看|列出|告诉我|多少|几|哪|啥|什么|怎么样|咋样|如何|状态|列表|清单)", query)
    )
    body = re.sub(r"^(?:一下|下|我的?|一下我的?|当前(?:的)?|现在(?:的)?|最近(?:的)?)\s*", "", body).strip()
    body = _TRAIL_PARTICLE_RE.sub("", body).strip()
    body = body or core

    topic = _TRAIL_QUERY_SUFFIX_RE.sub("", body).strip()
    topic = re.sub(r"^(?:一下|下|我的?|当前(?:的)?|现在(?:的)?|最近(?:的)?)\s*", "", topic).strip()
    topic = topic.rstrip("的 ") or body

    compact = re.sub(r"[\s，,。！？!?、:：;；~～]+", "", query).lower()
    is_meta = bool(
        _GENERAL_CONCEPT_RE.search(query)
        or _META_ENGINEERING_RE.search(query)
        or _THIRD_PARTY_STORY_RE.search(query)
    )
    return NormalizedUtterance(
        raw=raw,
        stmt=stmt,
        query=query,
        core=core,
        body=body,
        topic=topic,
        compact=compact,
        had_question=had_question,
        has_query_verb=has_query_verb,
        is_meta=is_meta,
        is_multiline=is_multiline,
    )


def _cn_to_small_int(tok: str) -> Optional[int]:
    s = (tok or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    mp = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s in mp:
        return mp[s]
    if s == "十":
        return 10
    if s.startswith("十") and len(s) == 2 and s[1] in mp:
        return 10 + mp[s[1]]
    if len(s) >= 2 and s[0] in mp and s[1] == "十":
        return mp[s[0]] * 10 + (mp.get(s[2], 0) if len(s) >= 3 else 0)
    return None


def extract_location_override(text: str) -> tuple[str, str]:
    """从任意位置提取「送到家 / 点回家 / 送到公司 / 在公司 / 送到新房」为 location_override，并返回剥离地点后的干净文本。"""
    s = (text or "").strip()
    if not s:
        return "", ""
    loc = ""
    rules = (
        (
            r"(?:送到家里?|寄到家里?|发到家里?|点回家里?|点到家里?|送回家里?|回家里?|到家里?|在家里?|"
            r"(?:送到|寄到|发到|点到|到|在)示例小区A(?:公园)?(?:7号楼)?)",
            "家",
        ),
        (
            r"(?:送到公司|寄到公司|发到公司|点到公司|到公司|回公司|在公司|"
            r"(?:送到|寄到|发到|点到|到|在)(?:办公室|单位|示例写字楼(?:D座)?))",
            "公司",
        ),
        (
            r"(?:送到新房|寄到新房|发到新房|点到新房|到新房|在新房|"
            r"(?:送到|寄到|发到|点到|到|在)示例小区B(?:街|里)(?:3号楼)?)",
            "新房",
        ),
    )
    cleaned = s
    for pat, alias in rules:
        m = re.search(pat, cleaned)
        if m:
            loc = alias
            cleaned = (cleaned[: m.start()] + " " + cleaned[m.end() :]).strip()
            break
    cleaned = re.sub(r"^(?:一个|一份|一杯|一碗|一盒|一桶|个|份)\s*(?=.{2,})", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,、")
    return loc, cleaned


# ---------------------------------------------------------------------------
# Layer 2A · 声明式预置只读视图目录（36+ 视图统一泛化匹配）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewSpec:
    intent: str
    description: str
    exact_aliases: tuple[str, ...]
    # 每组概念帧为 (组1词集, 组2词集, ...)：消息同时命中全部词集即判定匹配（词序无关）
    concept_frames: tuple[tuple[tuple[str, ...], ...], ...] = ()
    # 排除词：命中则不按此只读视图处理
    forbid_re: Optional[str] = None
    max_len: int = 34
    allow_fuzzy: bool = True


PRESET_VIEW_SPECS: tuple[ViewSpec, ...] = (
    ViewSpec(
        intent="morning_full",
        description="展开今日司命晨札全文/详版",
        exact_aliases=("晨札全文", "早报全文", "展开晨札", "看晨札全文", "看全文", "晨札详版", "全文"),
        concept_frames=(
            (("晨札", "早报"), ("全文", "展开", "详版", "详细", "完整")),
        ),
        forbid_re=r"(?:论文|文章|代码|文档|pdf|小说)",
    ),
    ViewSpec(
        intent="morning_today",
        description="查看今日晨札/今日盘点",
        exact_aliases=("今天", "今日盘点", "晨札", "司命今天", "今日晨札", "今天晨札", "今天早报", "今日早报", "今日摘要", "今日安排"),
        concept_frames=(
            (("今天", "今日"), ("晨札", "早报", "盘点", "有什么事", "有啥事", "安排", "重点", "摘要", "日程")),
        ),
        forbid_re=r"(?:吃什么|吃啥|点|外卖|晚饭|午饭|早饭|提醒我|打针|天气|花了|消费|加班|累|睡)",
    ),
    ViewSpec(
        intent="todo_list",
        description="查看未结待办与待取快递/包裹",
        exact_aliases=(
            "待办", "看看待办", "我的待办", "待办列表", "待办事项", "未结待办", "未结事项",
            "有什么待办", "有哪些待办", "还有啥没拿", "哪些没取", "还有什么没拿", "还有啥没取",
            "待取快递", "待取包裹", "取件码", "快递取件码", "未取快递", "有哪些快递",
        ),
        concept_frames=(
            (("待办", "未结", "事项"), ("看", "查", "列", "有", "哪些", "什么", "啥", "列表", "清单", "剩")),
            (("快递", "包裹", "取件", "取件码", "外卖柜"), ("没拿", "没取", "待取", "有几个", "有哪些", "到了吗", "几个", "查", "看")),
        ),
        forbid_re=r"(?:销掉|消掉|核销|关闭|完成|已办|取消|取了|拿了|提醒我|S\d{2,6})",
    ),
    ViewSpec(
        intent="care_status",
        description="查看日常照护面板（周期用药打针/针头/口罩/酒精棉片余量）",
        exact_aliases=("照护", "日常照护", "照护面板", "耗材", "耗材余量", "补货雷达", "打针", "打针状态", "耗材状态", "口罩余量", "针头余量"),
        concept_frames=(
            (("口罩", "针头", "棉片", "酒精棉片", "耗材"), ("还剩", "还有多少", "剩多少", "余量", "够不够", "够用", "库存", "多少", "几个", "几只", "看", "查")),
            (("周期用药", "周期用药", "打针", "药笔"), ("还剩几针", "剩几针", "几针", "什么时候打", "下次", "状态", "记录", "开药", "看", "查")),
        ),
        forbid_re=r"(?:\d|[一二两三四五六七八九十百]+\s*(?:只|个|支|片|针|盒|笔)|打了|打完|跳过|不打|开药了|补药|挂号了|约了|提醒我|买|下单|加购)",
    ),
    ViewSpec(
        intent="install_checklist",
        description="查看 12.10 新房家电送装清单与预约缺口",
        exact_aliases=("送装清单", "12.10清单", "安装清单", "家电安装清单", "送装计划", "12.10送装", "12月10号清单", "送装进度"),
        concept_frames=(
            (("12.10", "12月10", "送装", "家电安装"), ("清单", "安排", "计划", "装什么", "装啥", "进度", "表", "核对", "看", "查")),
        ),
        forbid_re=r"(?:冲突|作战|初验|外箱|改到|改约|推迟|提醒我)",
    ),
    ViewSpec(
        intent="arrival_check",
        description="查看新房家电到货外箱初验清单",
        exact_aliases=("到货初验", "外箱初验", "验货清单", "初验清单", "初验单", "外箱验收"),
        concept_frames=(
            (("外箱", "到货", "开箱"), ("初验", "验货", "验收", "清单", "检查")),
        ),
        forbid_re=r"(?:已验|完好|验好了|没问题)",
    ),
    ViewSpec(
        intent="onsite_plan",
        description="查看 12.09/12.10 现场分组作战单",
        exact_aliases=("现场作战单", "12.09作战单", "12.10作战单", "分组安装单", "现场分组单", "作战单"),
        concept_frames=(
            (("现场", "12.09", "12月9", "分组"), ("作战", "安装单", "梯队", "分组")),
        ),
    ),
    ViewSpec(
        intent="install_conflicts",
        description="检查新房送装工序冲突",
        exact_aliases=("安装冲突", "工序冲突", "12.10冲突", "送装冲突", "施工冲突"),
        concept_frames=(
            (("安装", "工序", "送装", "12.10", "燃气", "橱柜", "吊顶"), ("冲突", "打架", "撞车", "前置")),
        ),
        forbid_re=r"(?:改到|改约|推迟|就绪|未就绪)",
    ),
    ViewSpec(
        intent="modules_list",
        description="查看司命全部功能模块开关状态",
        exact_aliases=("模块", "模块列表", "功能列表", "功能开关", "模块开关", "模块状态", "功能状态"),
        concept_frames=(
            (("模块", "功能"), ("列表", "开关", "状态", "哪些", "开着", "关着", "一览")),
        ),
        forbid_re=r"(?:关闭|关掉|停用|开启|打开|启用)",
    ),
    ViewSpec(
        intent="price_watch_list",
        description="查看网购保价监控列表",
        exact_aliases=("保价", "保价列表", "保价监控", "京东保价", "价保列表", "价保监控", "保价情况"),
        concept_frames=(
            (("保价", "价保"), ("列表", "监控", "哪些", "情况", "降价", "商品", "看", "查")),
        ),
        forbid_re=r"(?:http|item\.jd|移除|取消|删|移出|别盯|已保价|退差|加保价|盯保价)",
    ),
    ViewSpec(
        intent="reimburse_domestic",
        description="查看本月国内电子发票与滴滴行程单配对盘点",
        exact_aliases=("打车报销", "国内发票", "国内发票盘点", "行程单配对", "滴滴发票", "国内报销发票"),
        concept_frames=(
            (("打车", "滴滴", "国内"), ("发票", "行程单", "配对", "盘点", "报销单")),
        ),
        forbid_re=r"(?:打包|组包|已交单|已提单|CW01)",
    ),
    ViewSpec(
        intent="reimburse_cursor",
        description="查看 Nous 报销游标与未核销还款进度",
        exact_aliases=("报销游标", "nous游标", "海外报销游标", "nous报销进度", "报销进度"),
        concept_frames=(
            (("报销", "nous"), ("游标", "到哪了", "进度", "起点", "未核销")),
        ),
        forbid_re=r"(?:打包|已交单|账龄|公私)",
    ),
    ViewSpec(
        intent="receivables_list",
        description="查看待回款列表（报销与退差到账追踪）",
        exact_aliases=("待回款", "待回款列表", "回款状态", "报销回款", "回款追踪", "待到账"),
        concept_frames=(
            (("回款", "退差", "待到账", "报销款"), ("列表", "状态", "哪些", "没到账", "追踪", "还有", "看", "查")),
        ),
        forbid_re=r"(?:\d+(?:\.\d{1,2})?\s*(?:元|块)?$|登记|添加)",
    ),
    ViewSpec(
        intent="waits_list",
        description="查看当前等待触发的挂起提醒列表",
        exact_aliases=("挂起列表", "挂起", "挂起项", "挂起任务", "提醒列表", "我的提醒", "waits"),
        concept_frames=(
            (("挂起", "提醒", "延后", "闹钟"), ("列表", "有哪些", "哪些", "设了", "记了", "多少条", "看", "查")),
        ),
        forbid_re=r"(?:提醒我|取消|删|记下|忽略|W\d{2,6})",
    ),
    ViewSpec(
        intent="didi_status",
        description="查询当前滴滴打车订单状态",
        exact_aliases=("打车状态", "滴滴状态", "叫车状态", "滴滴订单", "打车订单"),
        concept_frames=(
            (("滴滴", "打车", "叫车", "司机", "网约车"), ("状态", "到哪了", "叫好了吗", "接单了吗", "进度", "订单")),
        ),
        forbid_re=r"(?:取消|去|回家|报销|发票|花了|消费|密钥|key)",
    ),
    ViewSpec(
        intent="luckin_status",
        description="查询瑞幸咖啡订单状态与取餐码",
        exact_aliases=("瑞幸状态", "咖啡状态", "瑞幸取餐码", "咖啡取餐码", "瑞幸订单"),
        concept_frames=(
            (("瑞幸", "咖啡"), ("状态", "取餐码", "好了吗", "做好了吗", "进度", "订单")),
        ),
        forbid_re=r"(?:点|来一杯|来杯|买|下单|花了|消费|密钥|token)",
    ),
    ViewSpec(
        intent="location_status",
        description="查看当前融合定位与 Wi-Fi 地点映射",
        exact_aliases=("我的位置", "定位状态", "当前位置", "当前定位", "wifi映射", "我在哪", "我现在在哪"),
        concept_frames=(
            (("位置", "定位", "wifi"), ("当前", "现在", "我的", "在哪", "状态", "映射", "准不准", "看", "查")),
        ),
        forbid_re=r"(?:绑定|记住|标记|设为|映射为|改)",
    ),
    ViewSpec(
        intent="attention_status",
        description="查看夜猫子作息与注意力免打扰状态",
        exact_aliases=("作息", "作息状态", "注意力状态", "睡眠状态", "夜猫子状态", "免打扰状态"),
        concept_frames=(
            (("作息", "注意力", "睡眠", "夜猫子"), ("状态", "模式", "免打扰", "看", "查")),
        ),
        forbid_re=r"(?:睡了|醒了|起床|晚安|早安|关|开)",
    ),
    ViewSpec(
        intent="dev_links_check",
        description="检查研发链路（aTrust/9222/gpu-server/AI额度/Nous状态）",
        exact_aliases=(
            "检查链路", "检查工作链路", "链路诊断", "链路状态", "额度", "ai额度", "配额", "订阅额度",
            "nous状态", "检查nous", "工作链路", "研发链路",
        ),
        concept_frames=(
            (("链路", "工作链路", "研发链路", "atrust", "gpu-server", "9222"), ("检查", "诊断", "状态", "通不通", "正常", "看", "查")),
            (("额度", "配额", "claude", "codex", "nous"), ("还剩", "余量", "多少", "状态", "检查", "看", "查")),
        ),
        forbid_re=r"(?:游标|报销|花了|消费|算力|余额|资产)",
    ),
    ViewSpec(
        intent="routines_panel",
        description="查看司命学习到的个人规律面板",
        exact_aliases=("规律", "我的规律", "你记了我哪些规律", "查看规律", "规律列表", "习惯规律", "生活规律"),
        concept_frames=(
            (("规律", "习惯"), ("哪些", "记了", "学了", "列表", "面板", "看", "查")),
        ),
        forbid_re=r"(?:暂停|删除|删掉|恢复|开启|R-)",
    ),
    ViewSpec(
        intent="tesla_status",
        description="查看特斯拉 Model Y 电量、续航与车况",
        exact_aliases=("特斯拉", "车电量", "电车状态", "电车电量", "tesla", "车况", "特斯拉电量", "特斯拉状态", "车续航", "电车续航", "model y"),
        concept_frames=(
            (("特斯拉", "tesla", "model y", "modely", "电车", "车子", "车"), ("电量", "多少电", "剩多少电", "续航", "车况", "状态", "温度", "插枪")),
        ),
        forbid_re=r"(?:\d{2,3}\s*%|更新|设为|改成|唤醒|刷新|强刷|空调|暖车|备车|开车去|自驾去|车务|养车|花了)",
    ),
    ViewSpec(
        intent="novel_spend",
        description="查看抖音小说/短剧微额消费统计",
        exact_aliases=("小说消费", "抖音小说", "短剧消费", "小说账单"),
        concept_frames=(
            (("小说", "短剧"), ("消费", "花了多少", "花销", "支出", "账单", "统计")),
        ),
    ),
    ViewSpec(
        intent="platform_sessions",
        description="检查本机 9222 浏览器各平台登录态与 Cookie 健康",
        exact_aliases=("登录态", "钥匙检查", "会话状态", "平台状态", "cookie状态", "登录状态"),
        concept_frames=(
            (("登录态", "登录状态", "cookie", "会话", "钥匙"), ("检查", "状态", "过期", "有效", "掉没掉", "看", "查")),
        ),
        forbid_re=r"(?:验证码|代登|短信登录)",
    ),
    ViewSpec(
        intent="haos_status",
        description="查看新房 HAOS 全屋智能状态",
        exact_aliases=("新房智能", "haos", "全屋智能", "智能家居", "haos状态", "全屋智能状态"),
        concept_frames=(
            (("新房", "全屋", "haos", "米家"), ("智能", "家居", "扫地机", "设备状态")),
        ),
        forbid_re=r"(?:甲醛|空气|空调|照片|水电|送装|安装)",
    ),
    ViewSpec(
        intent="compute_assets",
        description="查看多平台 AI 算力资产余额与消耗水位",
        exact_aliases=("算力资产", "算力水位", "算力总控", "算力余额", "api余额", "ai算力", "算力账本"),
        concept_frames=(
            (("算力", "api余额", "模型余额", "token余额"), ("资产", "水位", "总控", "余额", "状态", "看", "查", "还有多少")),
        ),
        forbid_re=r"(?:优化|以后|改|开发|实现)",
    ),
    ViewSpec(
        intent="sub_audit",
        description="查看全渠道订阅体检与僵尸订阅/刺客审计",
        exact_aliases=("订阅体检", "订阅刺客", "僵尸订阅", "自动续费", "订阅列表", "订阅审计", "周期扣费"),
        concept_frames=(
            (("订阅", "自动续费", "连续包月", "包月会员"), ("体检", "刺客", "僵尸", "乱扣", "扣费", "审计", "有哪些", "列表", "看", "查")),
        ),
        forbid_re=r"(?:关闭|关掉|停用|开启|打开|止损卡|别再发)",
    ),
    ViewSpec(
        intent="health_cockpit",
        description="查看个人健康驾驶舱与常用医院复诊摘要",
        exact_aliases=("健康摘要", "复诊摘要", "健康驾驶舱", "化验单摘要", "健康报告", "体检摘要"),
        concept_frames=(
            (("健康", "复诊", "化验单", "体检", "指标"), ("摘要", "驾驶舱", "报告", "趋势", "总结", "看", "查")),
        ),
        forbid_re=r"(?:挂号|约了|预约)",
    ),
    ViewSpec(
        intent="vehicle_cfo",
        description="查看 Model Y 车务成本、停车/ETC/超充与 LFP 校准报告",
        exact_aliases=("车务成本", "车务报告", "车务账房", "养车成本", "养车报告", "车务"),
        concept_frames=(
            (("车务", "养车", "车险", "停车费", "超充费", "lfp校准"), ("成本", "报告", "账房", "费用", "花了多少", "汇总", "看", "查")),
        ),
    ),
    ViewSpec(
        intent="home_air",
        description="查看新房甲醛进度与远程新风策略",
        exact_aliases=("新房空气", "放甲醛", "甲醛进度", "新房甲醛", "空气质量", "甲醛状态"),
        concept_frames=(
            (("新房", "房子", "示例小区B", "示例小区B", "新风"), ("甲醛", "空气", "散味", "达标", "通风")),
            (("甲醛",), ("进度", "怎么样", "咋样", "多少", "达标", "散味", "状态", "看", "查")),
        ),
    ),
    ViewSpec(
        intent="homelab_ops",
        description="查看家庭实验室、RB5009、NAS 与三地灾备状态",
        exact_aliases=("家庭实验室", "nas状态", "三地灾备", "绿联nas", "rb5009", "灾备状态", "homelab"),
        concept_frames=(
            (("nas", "绿联", "rb5009", "光猫", "家庭实验室", "灾备"), ("状态", "健康", "备份", "硬盘", "smart", "看", "查")),
        ),
    ),
    ViewSpec(
        intent="bambu_status",
        description="查看 Bambu 3D 打印状态与耗材库",
        exact_aliases=("3d打印", "拓竹", "拓竹状态", "bambu", "打印状态", "打印进度", "3d打印状态"),
        concept_frames=(
            (("3d打印", "拓竹", "bambu", "打印机"), ("状态", "进度", "打什么", "打啥", "耗材", "线材", "看", "查")),
        ),
    ),
    ViewSpec(
        intent="home_passport",
        description="查看住宅水电物业与固定资产护照总览",
        exact_aliases=("住宅护照", "水电物业", "家庭资产护照"),
        concept_frames=(
            (("住宅", "水电物业", "滤芯", "尘袋"), ("护照", "账期", "寿命", "总览", "看", "查")),
        ),
    ),
    ViewSpec(
        intent="msim_diary",
        description="查看 M-SIM 跨模型自进化日记榜单",
        exact_aliases=("进化日记", "msim", "自进化日记", "跨模型经验", "进化榜单"),
        concept_frames=(
            (("进化", "msim", "跨模型"), ("日记", "榜单", "经验", "记录", "看", "查")),
        ),
    ),
    ViewSpec(
        intent="trip_summary",
        description="查看差旅与出境游行程包",
        exact_aliases=("行程包", "差旅行程", "出境游", "我的行程", "差旅清单"),
        concept_frames=(
            (("行程包", "差旅", "出境游", "旅游清单"), ("看", "查", "列表", "摘要", "状态", "我的")),
        ),
    ),
    ViewSpec(
        intent="social_summary",
        description="查看桌游社交、人情记忆与众筹追踪汇总",
        exact_aliases=("桌游社交", "人情记忆", "众筹追踪", "桌游众筹", "桌游局"),
        concept_frames=(
            (("桌游", "人情", "众筹"), ("社交", "记忆", "追踪", "尾款", "补款", "汇总", "看", "查")),
        ),
        forbid_re=r"(?:桌游柜|装得下|容量|买)",
    ),
    ViewSpec(
        intent="replenish_list",
        description="查看家庭与公司常买日用品周期清单",
        exact_aliases=("常买", "常买清单", "补货清单", "常买补货", "消耗品清单", "周期清单", "日用品", "日用品清单", "补货周期", "日用品补货"),
        concept_frames=(
            (("常买", "消耗品", "日用品"), ("清单", "列表", "周期", "补货", "有哪些", "快没了", "看", "查")),
        ),
        forbid_re=r"(?:添加|移除|删除|加购|加购物车|直接买|已买|买过了|口罩|针头|棉片)",
    ),
    ViewSpec(
        intent="speculate_plans",
        description="查看美团投机推演各家菜品与券后价预案",
        exact_aliases=("看看方案", "看方案", "推演方案", "推演结果", "推演好了吗", "外卖方案", "这几家点啥", "点啥菜", "预案"),
        concept_frames=(
            (("推演", "外卖方案", "点餐方案", "菜品方案"), ("方案", "结果", "好了吗", "出来了吗", "怎么样", "看", "查")),
        ),
    ),
)

_VIEW_EXACT_INDEX: dict[str, ViewSpec] = {}
for _vs in PRESET_VIEW_SPECS:
    for _al in _vs.exact_aliases:
        _VIEW_EXACT_INDEX[_al.lower()] = _vs


def _char_bigrams(s: str) -> set[str]:
    clean = re.sub(r"\s+", "", s.lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _fuzzy_alias_score(probe: str, alias: str) -> float:
    p = re.sub(r"\s+", "", probe.lower())
    a = re.sub(r"\s+", "", alias.lower())
    if not p or not a or len(p) < 2 or len(a) < 2:
        return 0.0
    if p == a:
        return 1.0
    # 长度差过大直接放弃，防止短词误撞长词
    if abs(len(p) - len(a)) > max(2, len(a) // 2):
        return 0.0
    seq = difflib.SequenceMatcher(None, p, a).ratio()
    bp, ba = _char_bigrams(p), _char_bigrams(a)
    jacc = len(bp & ba) / float(max(1, len(bp | ba)))
    # 汉字集合重叠率（容忍单字错别字，如 算力自产 vs 算力资产 -> 3/4=0.75，长度相等 seq=0.75）
    pc = set(re.findall(r"[\u4e00-\u9fff]", p))
    ac = set(re.findall(r"[\u4e00-\u9fff]", a))
    ch_ov = (len(pc & ac) / float(max(len(pc), len(ac)))) if (pc and ac and len(p) == len(a) and len(p) >= 4) else 0.0
    return max(seq, jacc, ch_ov)


def match_preset_view(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """匹配 36+ 预置只读视图：精确别名 → 概念帧（词序无关）→ 高置信拼写容错。"""
    if not u.query or u.is_multiline or u.is_meta:
        return None
    if len(u.query) > 48:
        return None

    low_candidates = [
        c
        for c in dict.fromkeys(
            (
                u.query.lower(),
                u.core.lower(),
                u.body.lower(),
                u.topic.lower(),
                u.compact,
            )
        )
        if c
    ]

    # 1. 精确别名命中（兼容「帮我看看算力资产」「查一下特斯拉」「看看我的待办吧」等任意包装）
    for cand in low_candidates:
        spec = _VIEW_EXACT_INDEX.get(cand)
        if spec is None:
            continue
        if len(u.query) > spec.max_len:
            continue
        if spec.forbid_re and re.search(spec.forbid_re, u.query, re.IGNORECASE):
            continue
        # 若整句明显是写/修改/执行命令（且不是裸别名），绝不按只读视图拦截
        if cand != u.query.lower() and _WRITE_OR_MUTATE_CUE_RE.search(u.query):
            continue
        return {"intent": spec.intent, "via": "exact_alias", "matched": cand, "confidence": 1.0}

    # 写操作/非查询短语不走概念帧与模糊匹配
    if _WRITE_OR_MUTATE_CUE_RE.search(u.query):
        return None

    q_low = u.query.lower()

    # 2. 概念帧命中（任意词序 + 表达变体，如「新房里的甲醛散得咋样了」「我那几个订阅有没有乱扣费」）
    best_frame: Optional[tuple[float, ViewSpec]] = None
    for spec in PRESET_VIEW_SPECS:
        if len(u.query) > spec.max_len:
            continue
        if spec.forbid_re and re.search(spec.forbid_re, u.query, re.IGNORECASE):
            continue
        for frame in spec.concept_frames:
            if all(any(term in q_low for term in grp) for grp in frame):
                # 概念帧越具体、命中词越长分数越高
                hit_chars = sum(max((len(t) for t in grp if t in q_low), default=0) for grp in frame)
                sc = len(frame) * 10.0 + hit_chars
                if best_frame is None or sc > best_frame[0]:
                    best_frame = (sc, spec)
    if best_frame is not None:
        return {"intent": best_frame[1].intent, "via": "concept_frame", "confidence": 0.92}

    # 3. 高阈值拼写/错别字模糊容错（仅限短句、非闲聊、有清晰唯一胜出项）
    if len(u.topic) <= 12 and len(u.query) <= 22:
        scored: list[tuple[float, ViewSpec, str]] = []
        probes = [p for p in dict.fromkeys((u.topic.lower(), u.body.lower(), u.core.lower())) if 3 <= len(p) <= 12]
        for spec in PRESET_VIEW_SPECS:
            if not spec.allow_fuzzy:
                continue
            if spec.forbid_re and re.search(spec.forbid_re, u.query, re.IGNORECASE):
                continue
            best_s = 0.0
            best_a = ""
            for pr in probes:
                for al in spec.exact_aliases:
                    if len(al) < 3:
                        continue
                    sc = _fuzzy_alias_score(pr, al)
                    if sc > best_s:
                        best_s = sc
                        best_a = al
            if best_s >= 0.74:
                scored.append((best_s, spec, best_a))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            top_sc, top_spec, top_al = scored[0]
            second_sc = scored[1][0] if len(scored) > 1 else 0.0
            if top_sc >= 0.75 and (top_sc - second_sc >= 0.08 or len(scored) == 1):
                return {"intent": top_spec.intent, "via": "fuzzy_alias", "matched": top_al, "confidence": round(top_sc, 3)}

    return None


# ---------------------------------------------------------------------------
# Layer 2B · 高频参数化意图与槽位解析器
# ---------------------------------------------------------------------------


def parse_sleep_wake(u: NormalizedUtterance) -> Optional[str]:
    """识别入睡/醒来口令（兼容自然变体如「先去睡了」「刚起床」「早上好」，严格拒绝问句与长叙事）。"""
    if not u.stmt or u.had_question or u.is_multiline or len(u.stmt) > 16:
        return None
    if re.search(r"[，,。！？!?]", u.stmt):
        # 允许「晚安，睡了」这类极短双词，禁止长叙述句
        if len(u.stmt) > 10:
            return None
    s = re.sub(r"^(?:我)?(?:先|刚|准备|要|准备去|要去|去|马上|这就|已经)?", "", u.stmt).strip()
    s = re.sub(r"(?:了|啦|咯|哈|呀|啊|哦|噢|喽|去)+$", "", s).strip()
    if u.stmt in ("睡了", "晚安", "准备睡了", "我要睡了") or s in ("睡", "睡觉", "晚安", "去睡", "休息", "入睡") and (
        "睡" in u.stmt or "晚安" in u.stmt
    ):
        if not re.search(r"(没睡|不睡|不想睡|睡不着|睡醒|睡够|睡饱)", u.stmt):
            return "sleep"
    if u.stmt in ("醒了", "早", "早安", "起床了") or (
        s in ("醒", "睡醒", "起床", "起来", "早", "早安", "早上好", "上午好")
        and not re.search(r"(没醒|还没醒|不想起|没起)", u.stmt)
    ):
        return "awake"
    return None


def parse_meal_ack(u: NormalizedUtterance) -> Optional[dict[str, str]]:
    """识别饮食确认短语（「吃过了」「已经吃过晚饭了」「今晚不点外卖了」），严格不劫持「晚饭吃完了」或长闲聊句。"""
    if not u.stmt or u.had_question or u.is_multiline or len(u.stmt) > 22:
        return None
    # 明确排除多从句叙事（如「今天加班真累，但已经吃过了」）与「晚饭吃完了」
    if re.search(r"[，,;；]|但是|不过|虽然|因为|所以|吃完了", u.stmt):
        return None
    m1 = re.fullmatch(
        r"(?:我)?(?:刚才|刚刚|已经|刚|今天)?(中午|午饭|午餐|晚上|今晚|晚饭|晚餐|饭)?(?:已经|刚|刚刚)?(?:吃过了?|吃饱了)(?:晚饭|晚餐|午饭|午餐|饭)?(?:啦|咯|哈|哦|了)?",
        u.stmt,
    )
    if m1 and ("吃过" in u.stmt or "吃饱" in u.stmt):
        # 提取餐窗提示（兼容前后置：如「已经吃过晚饭了」-> 晚饭）
        m_w = re.search(r"(中午|午饭|午餐|晚上|今晚|晚饭|晚餐)", u.stmt)
        return {"window": m_w.group(1) if m_w else ""}
    m2 = re.fullmatch(
        r"(?:我)?(今晚|今天晚上|晚上|晚饭|晚餐|中午|今天中午|午饭|午餐|今天)(?:已经)?(?:不点了|不点外卖了|不用点外卖了|不用点餐了|不吃了|不用提醒(?:我)?吃饭了)(?:啦|咯|哈|哦)?",
        u.stmt,
    )
    if m2:
        win = m2.group(1)
        return {"window": "" if win == "今天" else win}
    return None


def parse_module_toggle(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """识别模块开关（支持「关闭保价」「把订阅刺客关掉」「别再发止损卡了」「重新打开自动备车」）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta or len(u.stmt) > 36:
        return None

    s = _LEAD_ADDRESS_RE.sub("", u.stmt).strip()
    s = _LEAD_POLITE_RE.sub("", s).strip()
    s = _TRAIL_PARTICLE_RE.sub("", s).strip()

    # 1. 显式「别再发/不要再推 X」-> 映射关闭模块
    m_stop = re.match(r"^(?:别再发|不要再发|别再推|不要再推|别发|不要发|屏蔽)(?:那个|这个)?(.{2,20}?)(?:了|卡片|提醒)?$", s)
    if m_stop:
        return {"enabled": False, "target": m_stop.group(1).strip(), "explicit_module_kw": False}

    # 2. 「把 X 关掉/开启」
    m_ba = re.match(
        r"^把\s*(.{2,24}?)\s*(?:模块|功能|提醒|推送|监控)?\s*(关闭|关掉|关了|关一下|停用|停掉|暂停|开启|打开|启用|恢复|重新打开|重新开启)(?:了|掉)?$",
        s,
    )
    if m_ba:
        act = m_ba.group(2)
        en = act in ("开启", "打开", "启用", "恢复", "重新打开", "重新开启")
        return {"enabled": en, "target": m_ba.group(1).strip(), "explicit_module_kw": ("模块" in s)}

    # 3. 动宾式：「关闭/关掉/停用/开启/打开/启用 [模块] X」（兼容有无空格）
    m_vo = re.match(
        r"^(?:重新)?(关闭|关掉|停用|暂停|开启|打开|启用)(?:一下)?\s*(模块)?\s*(.+)$",
        s,
    )
    if m_vo:
        act = m_vo.group(1)
        has_mod_kw = bool(m_vo.group(2))
        target = re.sub(r"^(?:一下|那个|这个)\s*", "", m_vo.group(3).strip())
        target = re.sub(r"(?:模块|功能|提醒|推送|了)$", "", target).strip() or m_vo.group(3).strip()
        if not target or len(target) > 24:
            return None
        # 避免抢「关闭空调」「关掉空调」「暂停规律 R-xx」「恢复推送 rule」
        if re.search(r"(?:空调|规律|推送|备车空调$)", u.stmt) and not has_mod_kw and "自动备车" not in target:
            if "空调" in target or "规律" in target or "推送" in target:
                return None
        en = act in ("开启", "打开", "启用")
        return {"enabled": en, "target": target, "explicit_module_kw": has_mod_kw}

    return None


def parse_meituan_history_list(u: NormalizedUtterance) -> Optional[dict[str, str]]:
    """识别美团历史常点/老样子列表查询（「找一个我之前买过的菜单吧 不要新的」「看看以前点过的店」「常点外卖有哪些」）。"""
    if not u.query or u.is_multiline or u.is_meta or len(u.query) > 48:
        return None
    q = u.query
    exact = {
        "历史菜单", "常点菜单", "以前买过的", "之前买过的", "买过的菜单",
        "历史外卖", "老样子菜单", "常点外卖", "常点的店", "以前点过的店",
        "之前点过的外卖", "之前吃过的外卖", "历史点单", "历史订单菜单",
    }
    if q in exact or u.body in exact or u.topic in exact:
        loc, _ = extract_location_override(q)
        return {"location_override": loc}

    # 排除具体点名某家店复购（交由 history_reorder）和查账（花了多少）
    if re.search(r"(?:花了多少|多少钱|消费|支出|账单|(?:复购|再来一单|第)\s*[1-5一二三四五]\s*(?:单|家|个|号))", q):
        return None
    has_hist = bool(re.search(r"(?:以前|之前|历史|老样子|常点|买过|点过|吃过|老店)", q))
    has_food = bool(
        re.search(
            r"(?:菜单|外卖|美团|老样子|常点|点过的|买过的|吃过的|老店|不要新的|不想试新|不换新|不想换新)",
            q,
        )
    )
    has_list_or_pick_cue = bool(
        re.search(
            r"(?:找一个|找个|列一下|列出来|看看|看下|有哪些|有啥|什么|啥|不要新的|不想试新|菜单|历史|常点|老店)",
            q,
        )
    )
    if has_hist and has_food and has_list_or_pick_cue:
        # 若句里明确包含「再来一单/复购/原样下单 + 店名」或增删菜改单，让位给直接复购
        if re.search(r"(?:再来一单|复购|原样下单|原样点|再点一次|加上|再加|多加|加个|加份|去掉|不要(?!新的|新店|换新))", q):
            return None
        loc, _ = extract_location_override(q)
        return {"location_override": loc}
    return None


_REORDER_MOD_SPLIT_RE = re.compile(
    r"(?:[\s，,、]+|(?:(?<=单)|(?<=次)|(?<=子)|(?<=的)|(?<=档)|(?<=店)|(?<=吧)))"
    r"(?=(?:加上|再加|多加|另加|顺便加|额外加|加一个|加个|加一份|加份|加一道|加道|加一碗|加碗|加一杯|加杯|配一个|配个|配一份|配份|补一个|补个|补一份|补份|去掉|不要|不加|减掉|免去|去一个|去个|换掉))"
)
_REORDER_EXTRA_PREFIX_RE = re.compile(
    r"^(?:加上|再加|多加|另加|顺便加|额外加|补上|配上|加|补|配)\s*"
)
_REORDER_EXCLUDE_PREFIX_RE = re.compile(
    r"^(?:去掉|不要|不加|减掉|免去|换掉|去|少要|别要|别加)\s*"
)
_REORDER_QTY_UNIT_RE = re.compile(
    r"^([一二两三四五\d])?\s*(?:个|份|道|碗|杯|盒|盘|串|瓶|罐|桶|袋|套)\s*(.+)$"
)
_REORDER_HARMLESS_TAIL_RE = re.compile(
    r"^(?:来一份|来一单|来个|点一份|点一单|点一下|再来一单|再点一次|原样下单|原样|来|点|下单|吧|呗|哈|啊|呀|就行|好了)*$"
)
_REORDER_NON_ORDER_TAIL_RE = re.compile(
    r"(?:不好吃|难吃|太咸|太辣|太慢|没到|退了|退款|投诉|差评|拉肚子|坏了|凉了|洒了|撒了|少了|漏了|贵了|多少钱|花了|为什么|怎么|吗|？|\?)"
)


def _parse_reorder_modifications(mod_text: str) -> tuple[list[dict[str, Any]], list[str], bool]:
    """解析复购句尾的增删菜子句（如「加上芫荽那个凉菜」「多加两份米饭 不要鸭肠」）。

    返回 `(extra_dishes, exclude_dishes, parsed_cleanly)`。
    """
    raw = (mod_text or "").strip(" ，,、。！!")
    if not raw:
        return [], [], True
    if _REORDER_HARMLESS_TAIL_RE.fullmatch(raw):
        return [], [], True
    if _REORDER_NON_ORDER_TAIL_RE.search(raw):
        return [], [], False

    # 将连续增删指令拆成独立片段（如「加上芫荽凉菜 不要鸭肠」「多加份米饭，再加个煎饺」）
    segments = [
        seg.strip(" ，,、。")
        for seg in re.split(
            r"(?:[\s，,、;；]+(?=(?:加上|再加|多加|另加|顺便加|额外加|补上|配上|加一个|加个|加一份|加份|加一道|加道|加|补|配|去掉|不要|不加|减掉|免去|换掉|少要|别要|别加)))",
            raw,
        )
        if seg.strip(" ，,、。")
    ]

    extra: list[dict[str, Any]] = []
    exclude: list[str] = []
    parsed_any = False
    for seg in segments:
        if _REORDER_HARMLESS_TAIL_RE.fullmatch(seg):
            continue
        is_exc = bool(_REORDER_EXCLUDE_PREFIX_RE.match(seg))
        is_add = bool(_REORDER_EXTRA_PREFIX_RE.match(seg))
        if not is_exc and not is_add:
            return [], [], False
        body = _REORDER_EXCLUDE_PREFIX_RE.sub("", seg, count=1).strip() if is_exc else _REORDER_EXTRA_PREFIX_RE.sub("", seg, count=1).strip()
        if not body:
            return [], [], False
        # 支持用「和/与/+」连接多项增删菜
        sub_parts = [p.strip() for p in re.split(r"(?:[+＋、，,]|(?<=[^\s])[和与](?=[^\s]))", body) if p.strip()]
        for part in sub_parts:
            part = re.sub(r"(?:吧|呗|哈|啊|呀|就行|好了)+$", "", part).strip()
            if not part:
                continue
            qty = 1
            m_q = _REORDER_QTY_UNIT_RE.match(part)
            if m_q and len(m_q.group(2).strip()) >= 2:
                q_tok = (m_q.group(1) or "").strip()
                if q_tok:
                    q_val = _cn_to_small_int(q_tok)
                    if q_val and 1 <= q_val <= 10:
                        qty = q_val
                part = m_q.group(2).strip()
            part = re.sub(r"^(?:一份|一个|一道|一碗|一杯|份|个|道)\s*(?=.{2,})", "", part).strip()
            if len(part) < 2 or len(part) > 30:
                continue
            if is_exc:
                exclude.append(part)
            else:
                extra.append({"name": part, "qty": qty})
            parsed_any = True

    return extra, exclude, parsed_any


def parse_meituan_history_reorder(u: NormalizedUtterance, db: SimingDB) -> Optional[dict[str, Any]]:
    """识别历史整单按序号或店名一键复购（支持任意词序、错别字容错、地点覆盖、加菜/减菜增量修改与防误劫持）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta or len(u.stmt) > 72:
        return None
    if re.search(r"瑞幸", u.stmt):
        return None

    loc, clean_no_loc = extract_location_override(u.stmt)
    s = _LEAD_ADDRESS_RE.sub("", clean_no_loc).strip()
    s = _LEAD_POLITE_RE.sub("", s).strip()
    s = _TRAIL_PARTICLE_RE.sub("", s).strip()
    # 兼容非常用别名地址后缀（如「上一个南京大排档 送到银海」「把上次的素坤泰再点一次 寄到xx大厦」）
    if not loc:
        m_extra_loc = re.search(r"[\s，,]+(?:送到|寄到|发到|点到)([^\s，,。！!]{2,16})$", s)
        if m_extra_loc:
            loc = m_extra_loc.group(1).strip()
            s = s[: m_extra_loc.start()].strip()

    extra_dishes: list[dict[str, Any]] = []
    exclude_dishes: list[str] = []

    # 1. 按序号复购（支持增删菜：「复购第1单」「再来一单第2个 加一份米饭」「复购第1单 不要米饭」）
    m_idx = re.match(
        r"^(?:复购|再来一单|再点一次?|原样下单|原样复购)(?:一下)?\s*第?\s*([1-5一二三四五])\s*(?:单|家|个|号)?(?:[\s，,]*(.+))?$",
        s,
    )
    if m_idx:
        tail = (m_idx.group(2) or "").strip()
        ex_d, rm_d, ok_mod = _parse_reorder_modifications(tail)
        if ok_mod:
            return {
                "kind": "index",
                "target": f"复购第{m_idx.group(1)}单",
                "location_override": loc,
                "extra_dishes": ex_d,
                "exclude_dishes": rm_d,
            }

    from siming.meituan_agent import match_history_order_for_shop

    # 2. 店名在前 + 昨天/上次/老样子 + 可选增删菜（如「南京大排档吧 昨天的加上芫荽那个凉菜」「南京大排档 老样子多加份米饭」）
    m_shop_first = re.match(
        r"^(?:还是|就|那就|点|吃|要|来)?\s*([^\s，,。！？]{2,24}?)(?:吧|好了|就行|得了)?[\s，,]+"
        r"(?:还是|就|按|照)?(?:昨天的?|前天的?|上次的?|上一次的?|上一单的?|老样子|原样|再来一单|再点一次|复购(?:一下)?)"
        r"(?:那单|那个|那份|来|点)?([\s，,]*.*)$",
        s,
    )
    if m_shop_first:
        cand_sf = re.sub(r"(?:吧|呗|哈|啊|呀|外卖|那家|那单)$", "", m_shop_first.group(1).strip()).strip()
        tail_sf = (m_shop_first.group(2) or "").strip()
        if (
            len(cand_sf) >= 2
            and not re.search(r"(?:版本|会议|纪要|改动|问题|任务|脚本|代码|文档|PR|pr|bug|话题|方案)", cand_sf)
        ):
            ex_d, rm_d, ok_mod = _parse_reorder_modifications(tail_sf)
            if ok_mod and match_history_order_for_shop(db, cand_sf) is not None:
                return {
                    "kind": "shop",
                    "target": cand_sf,
                    "location_override": loc,
                    "extra_dishes": ex_d,
                    "exclude_dishes": rm_d,
                }

    # 3. 剥离句尾增删菜修饰后，按「把上次的 X 再点一次 / 再来一单 X / 上一个 X / 昨天的 X」匹配
    base_s = s
    m_mod_split = _REORDER_MOD_SPLIT_RE.search(s)
    if m_mod_split:
        left_part = s[: m_mod_split.start()].strip(" ，,、")
        right_part = s[m_mod_split.start() :].strip(" ，,、")
        ex_try, rm_try, ok_try = _parse_reorder_modifications(right_part)
        if left_part and ok_try and (ex_try or rm_try):
            base_s = left_part
            extra_dishes = ex_try
            exclude_dishes = rm_try

    candidate_shop = ""
    is_explicit_reorder = False

    m_ba = re.match(
        r"^(?:把|将)?(?:(?:上一次|上次|昨天|前天)(?:点的?|买的?|吃的?)?|上一单(?:的)?|之前(?:点的?|买的?)?)?\s*(.{2,30}?)\s*(?:再点一次|再来一单|再买一次|原样下单|复购(?:一下)?|老样子)$",
        base_s,
    )
    if m_ba:
        candidate_shop = m_ba.group(1).strip()
        is_explicit_reorder = True
    else:
        m_pre = re.match(
            r"^(?:还是|就|照|按)?(?:再来一单|复购|原样下单|原样再来一单|原样点|再点一次?|再买一次?|"
            r"按(?:上一次|上次|昨天|前天)(?:的)?(?:来|点)?|照(?:上一次|上次|昨天|前天)(?:的)?点?|老样子(?:来一份|点一份|点个|点|来)?|"
            r"(?:点|要|吃|来|下单)(?:一份|个)?(?:上一次|上次|昨天|前天)(?:那个|那单|的)?|"
            r"上一个|上一单|上一次(?:那个|那单|的)?|上次那个|上次的|昨天(?:那个|那单|点的|吃的|的)|前天(?:那个|那单|点的|吃的|的))(?:一下|一份|一单|个)?\s*(.{2,36})$",
            base_s,
        )
        if m_pre:
            candidate_shop = m_pre.group(1).strip()
            is_explicit_reorder = bool(
                re.match(
                    r"^(?:还是|就|照|按)?(?:再来一单|复购|原样|再点|再买|按上|照上|按昨天|照昨天|老样子)",
                    base_s,
                )
            )

    if not candidate_shop:
        return None

    candidate_shop = re.sub(r"^(?:的|那个|那家|那单|(?:来|点|买|要)?(?:一份|一单|一个|份|个)|来|点)\s*", "", candidate_shop).strip()
    candidate_shop = re.sub(r"(?:吧|呗|哈|啊|呀|外卖|那一单|那单|那家|老样子|再点一次|再来一单)$", "", candidate_shop).strip()
    if len(candidate_shop) < 2:
        return None
    # 拦截明显非外卖名词（如「上一个版本有什么改动」「上次那个会议纪要在哪」）
    if re.search(r"(?:版本|会议|纪要|改动|问题|任务|脚本|代码|文档|PR|pr|bug|话题|方案)", candidate_shop):
        return None

    if is_explicit_reorder or match_history_order_for_shop(db, candidate_shop) is not None:
        return {
            "kind": "shop",
            "target": candidate_shop,
            "location_override": loc,
            "extra_dishes": extra_dishes,
            "exclude_dishes": exclude_dishes,
        }
    return None


def parse_meituan_recommend(u: NormalizedUtterance) -> Optional[dict[str, str]]:
    """识别外卖/用餐推荐请求（如「推荐点晚上吃啥」「饿了不知道吃啥」「今晚吃什么好」「在公司推荐几家午饭」），排除非餐饮推荐与已吃完叙事。"""
    if not u.query or u.is_multiline or u.is_meta or len(u.query) > 64:
        return None
    q = u.query
    # 明确排除非餐饮推荐、宠物/医药问答、已吃完、查账、具体店/序号下单
    if re.search(r"(?:开源|项目|电影|书|游戏|工具|模型|代码|吃完了|吃过了|吃饱了|花了多少|多少钱|消费|账单|吃什么药|吃啥药|感冒吃|猫吃|狗吃|宠物吃|宝宝吃|孕妇)", q):
        return None
    if re.match(r"^(?:帮我|替我|给我)?(?:点外卖|美团下单|点餐|美团点|下单外卖)[\s，,:：]*第?\d", q):
        return None
    # 若明显在直接点具体商品（「点一杯冰美式」「点一份打抛饭」），不走推荐
    if re.search(r"(?:点|来|买|叫)(?:[一二两三四五\d]?)(?:杯|份|碗|个|盒|桶|袋|套|串|只)\s*[\u4e00-\u9fffA-Za-z]{2,}", q) and not re.search(
        r"(?:推荐|什么|啥|吃点啥|喝点啥)", q
    ):
        return None

    loc, _ = extract_location_override(q)
    # 1. 经典直达短语
    if re.match(
        r"^(?:(?:美团|外卖|晚餐|晚饭|午餐|午饭|早餐|早饭|夜宵|宵夜|下午茶)推荐|"
        r"(?:今晚|今天|晚上|中午|早上|晚饭|晚餐|午饭|午餐|早饭|早餐|夜宵|宵夜|现在|待会|一会)?(?:吃什么|吃啥|点什么|点啥|喝什么|喝啥|吃点什么|吃点啥))",
        u.core,
    ):
        return {"hint": q, "location_override": loc}

    # 2. 明确“吃啥/喝啥/吃点啥”口语问句（如「推荐点晚上吃啥」「帮我推荐下中午吃啥」「今天晚上吃啥好」）
    if re.search(r"(?:吃什么|吃啥|吃点什么|吃点啥|喝什么|喝啥|喝点什么|喝点啥|能吃啥|有啥吃|有什么吃|啥好吃|有什么好吃)", q):
        return {"hint": q, "location_override": loc}

    # 3. 泛化概念帧：餐饮/饥饿/时段上下文 + 推荐/纠结/寻店信号
    has_meal_ctx = bool(
        re.search(
            r"(?:晚饭|晚餐|午饭|午餐|中饭|早饭|早餐|夜宵|宵夜|下午茶|外卖|美团|吃的|好吃的|点饭|点餐|饭|饿了|肚子饿|没吃饭|还没吃|不想做饭)",
            q,
        )
    )
    has_rec_cue = bool(
        re.search(
            r"(?:推荐|吃什么|吃啥|点什么|点啥|喝什么|喝啥|能吃啥|有啥吃|有什么吃|吃点啥|吃点什么|还没吃|没吃饭|"
            r"不知道吃|没想好吃|挑一家|选一家|挑个|选个|来几家|有什么好吃|啥好吃)",
            q,
        )
    )
    if has_meal_ctx and has_rec_cue:
        return {"hint": q, "location_override": loc}
    if re.search(
        r"(?:推荐|挑|选)(?:点|个|下|一下|几家|几个)?(?:今天|今晚|晚上|中午|早上|周末)?(?:饭|吃的|好吃的|吃啥|吃什么|外卖|晚饭|晚餐|午饭|午餐|夜宵|宵夜|下午茶|店)",
        q,
    ):
        return {"hint": q, "location_override": loc}
    return None


def parse_meituan_spec_action(u: NormalizedUtterance, db: SimingDB) -> Optional[dict[str, str]]:
    """识别美团投机推演后的选店/确认下单（「确认第一家」「那就第2家吧」「就选第3个」「第二个，三丝炒米粉，滑蛋牛肉 确认下单」）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta or len(u.stmt) > 80:
        return None
    loc, clean_no_loc = extract_location_override(u.stmt)
    s = _LEAD_ADDRESS_RE.sub("", clean_no_loc).strip()
    s = _LEAD_POLITE_RE.sub("", s).strip()
    s = _TRAIL_PARTICLE_RE.sub("", s).strip()
    # 兼容非常用别名地址后缀（如「上一个南京大排档 送到银海」「把上次的素坤泰再点一次 寄到xx大厦」）
    if not loc:
        m_extra_loc = re.search(r"[\s，,]+(?:送到|寄到|发到|点到)([^\s，,。！!]{2,16})$", s)
        if m_extra_loc:
            loc = m_extra_loc.group(1).strip()
            s = s[: m_extra_loc.start()].strip()

    # 1. 带明确确认动词 + 序号：「确认第一家」「那就第2家吧」「就点第1个」「按方案点第2家」
    m_cf = re.match(
        r"^(?:确认|就点|就吃|就选|那就|直接点|确认点|按方案点|下单|选)(?:一下)?\s*第?\s*([1-5一二三四五])\s*(?:家|个|号)?$",
        s,
    )
    if m_cf:
        tok_i = m_cf.group(1)
        # 仅含弱前缀「那就/选」时要求当前确实存在活跃推演/推荐批次，避免误伤普通聊天
        weak_prefix = s.startswith(("那就", "选"))
        if weak_prefix:
            from siming.meituan_speculate import load_active_speculative_batch

            if not (load_active_speculative_batch(db) or db.get_meta("last_meituan_picks")):
                return None
        q_idx = f"第{tok_i}家" if tok_i in "一二三四五" else tok_i
        return {"mode": "confirm_idx", "target": q_idx, "location_override": loc}

    # 2. 确认动词 + 具体店铺名（必须命中当前活跃推演批次）
    m_nm = re.match(r"^(?:确认|就点|就吃|就选|直接点|确认点|按方案点)\s*([^\s，,。！？]{2,20})$", s)
    if m_nm:
        from siming.meituan_speculate import find_speculative_entry

        cand_name = m_nm.group(1).strip()
        if find_speculative_entry(db, cand_name, wait_if_planning_sec=0.0):
            return {"mode": "confirm_name", "target": cand_name, "location_override": loc}

    # 3. 预览/自定义菜品选第 N 家（如「第2家」「看看第1家」「第二个，三丝炒米粉，滑蛋牛肉，爽口萝卜 确认下单」）
    m_pv = re.match(r"^(?:看看|看下|选|点|要|那就)?第\s*([1-5一二三四五])\s*(?:家|个|号)(?:[\s,，、:：]+(.+))?$", s)
    if m_pv:
        from siming.meituan_speculate import load_active_speculative_batch

        if load_active_speculative_batch(db) or db.get_meta("last_meituan_picks"):
            q_target = re.sub(r"^(?:看看|看下|选|点|要|那就)\s*(?=第)", "", s).strip()
            return {"mode": "preview", "target": q_target, "location_override": loc}
    return None


_NATURAL_ORDER_REJECT_RE = re.compile(
    r"([?？吗]|推荐|吃什么|吃啥|喝什么|喝啥|查账|多少钱|花了|怎么样|为什么|怎么|哪家|哪个|有没有|能不能|是不是|没成功|失败|修复|朋友说|周末想去)"
)
_NATURAL_ORDER_NON_FOOD_RE = re.compile(
    r"(报告|清单|总结|账单|文档|合同|简历|计划|方案|代码|脚本|测试|截图|发票|草稿|数据|日志|材料|按钮|建议|意见|提醒|闹钟|模块|白开水|^水$)"
)
_NATURAL_ORDER_PHRASE_RE = re.compile(
    r"^(?:(?:我)?(?:好困|太困了|困了|有点困|好饿|太饿了|饿了|有点饿|嘴馋|口渴|想喝|想吃|想喝点|想吃点)[\s，,。！!]*)?"
    r"(?:再)?(?:帮我|替我|给我)?(?:在?美团|用美团)?(?:点|来|买|叫|下单)(?:[一二两三四五\d]?)(?:杯|份|碗|个|盒|桶|袋|套|串|只)\s*(.+)$"
)


def extract_natural_meituan_order(raw: str) -> Optional[str]:
    """泛化识别口语点餐/点咖啡句式（如「困了 给我点一杯苹果美式啥的」「帮我点个西瓜送到家」「饿了叫份炒米粉」），严格拦问句与非餐饮名词。"""
    s = _VOICE_PREFIX_RE.sub("", (raw or "").strip()).strip()
    if not s or len(s) > 48 or "\n" in s:
        return None
    if _NATURAL_ORDER_REJECT_RE.search(s):
        return None
    s_no_addr = _LEAD_ADDRESS_RE.sub("", s).strip()
    m = _NATURAL_ORDER_PHRASE_RE.match(s_no_addr)
    if not m:
        return None
    body = re.sub(r"(?:啥的|之类的|什么的|吧|呗|哈|啊|呀|[。！!])+$", "", m.group(1).strip()).strip()
    if len(body) < 2 or len(body) > 36:
        return None
    if _NATURAL_ORDER_NON_FOOD_RE.search(body):
        return None
    # 拦截动词短语（如「来个看看」「点个赞」「买个教训」）
    if re.match(r"^(?:赞|教训|寂寞|看看|试试|一下|东西|啥|什么)", body):
        return None
    return body


def parse_meituan_direct_order(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """识别美团/瑞幸直接点单（「点外卖 西瓜」「帮我在美团点一份老乡鸡送到公司」「困了点两杯冰美式」「来杯瑞幸」）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta or len(u.stmt) > 64:
        return None

    loc, clean_no_loc = extract_location_override(u.stmt)
    s = _LEAD_ADDRESS_RE.sub("", clean_no_loc).strip()
    s_pol = _LEAD_POLITE_RE.sub("", s).strip()
    if not loc:
        m_extra_loc = re.search(r"[\s，,]+(?:送到|寄到|发到|点到)([^\s，,。！!]{2,16})$", s_pol)
        if m_extra_loc:
            loc = m_extra_loc.group(1).strip()
            s_pol = s_pol[: m_extra_loc.start()].strip()

    # 1. 瑞幸直达
    m_lk = re.match(
        r"^(?:(?:来|点|买|下单)(?:[一二两三\d]?杯)?瑞幸(?:咖啡)?|瑞幸(?:再来一单|点单|下单|来一杯|点一杯))(?:[\s，,:：]*(.*))?$",
        s_pol,
    )
    if m_lk:
        return {"is_luckin": True, "query": u.stmt, "location_override": loc}

    # 2. 显式外卖/美团点餐（支持「美团帮我点…」「帮我在美团点…」「点个外卖…」）
    m_exp = re.match(
        r"^(?:(?:在?美团|用美团)\s*(?:帮我|替我|给我)?(?:点外卖|下单|点餐|点一下|点个|点份|点杯|点|买)|"
        r"(?:点外卖|美团下单|点餐|美团点|下单外卖|点个外卖|叫个外卖|叫外卖))[\s，,:：]*(.+)$",
        s_pol,
    )
    if m_exp:
        q_raw = re.sub(r"(?:吧|呗|哈|啊|呀)+$", "", m_exp.group(1).strip()).strip()
        q_raw = re.sub(r"^(?:[一二两三四五\d]?)(?:杯|份|碗|个|盒|桶|袋|套)\s*(?=.{2,})", "", q_raw).strip()
        if q_raw and not re.search(r"^(?:推荐|什么|啥|吗)", q_raw):
            q_final = q_raw if (loc and len(q_raw.replace(" ", "")) >= 2) else (q_raw or u.stmt)
            return {"is_luckin": False, "query": q_final, "location_override": loc}

    # 3. 口语点餐/点咖啡
    nat = extract_natural_meituan_order(u.raw)
    if nat:
        loc_n, q_clean = extract_location_override(nat)
        eff_loc = loc_n or loc
        q_final = q_clean if (eff_loc and len(q_clean.replace(" ", "")) >= 2) else nat
        return {"is_luckin": False, "query": q_final, "location_override": eff_loc}

    return None


def parse_meituan_combo(u: NormalizedUtterance) -> Optional[dict[str, str]]:
    """识别跨店双拼点餐（「双拼点餐 A + B」「帮我双拼 A 和 B」「帮我双拼示例粉面和示例果切西瓜」「两家一起点 A + B」）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta:
        return None
    m = re.match(r"^(双拼点餐|跨店双拼|双拼外卖|两家一起点|双拼)([\s，,:：]*)(.+)$", u.core)
    if m:
        kw, sep, body = m.group(1), m.group(2), m.group(3).strip()
        # 裸「双拼」无空格时，必须显式含两店连接词（+/＋/和/加）才按双拼点餐接管，防误伤「双拼输入法」等词
        if kw == "双拼" and not sep and not any(c in body for c in ("+", "＋", "和", "加")):
            return None
        # 兼容中文「和 / 加」分隔两家店
        if "+" not in body and "＋" not in body and ("和" in body or "加" in body):
            parts = [p.strip() for p in re.split(r"\s*[和加]\s*", body, maxsplit=1) if p.strip()]
            if len(parts) == 2 and all(len(p) >= 2 for p in parts):
                body = f"{parts[0]} + {parts[1]}"
        return {"query": body}
    return None


def parse_didi_command(u: NormalizedUtterance) -> Optional[dict[str, str]]:
    """识别滴滴叫车 / 取消打车（支持「打个滴滴去公司」「帮我叫辆车回家」「从公司打车到机场」「取消刚才叫的车」）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta or len(u.stmt) > 48:
        return None
    s = u.core
    if re.fullmatch(r"(?:取消打车|取消叫车|取消滴滴|把(?:刚才)?(?:叫的|打的)?车取消(?:掉)?|不打车了)", s):
        return {"action": "cancel", "origin": "", "dest": ""}

    m = re.match(
        r"^(?:从([^\s打叫滴]{1,20}?)\s*)?(?:打车|叫车|打个车|打辆车|叫辆车|叫个车|打滴滴|打个滴滴|叫滴滴|叫个滴滴|滴滴打车|叫个?网约车)"
        r"\s*(?:(回家|回公司)|(?:去|到|往)\s*(.{2,36}))$",
        s,
    )
    if m:
        orig = (m.group(1) or "").strip()
        if m.group(2):
            dest = "家" if "家" in m.group(2) else "公司"
        else:
            dest = re.sub(r"(?:(?<![酒网贴清氧餐水啡])吧|呀|啊|一下)$", "", (m.group(3) or "").strip()).strip()
        if dest:
            return {"action": "ride", "origin": orig, "dest": dest}
    return None


_LEDGER_TIME_RE = re.compile(
    r"(?:本月|这个月|当月|上月|上个月|今年|本年|近7天|最近7天|最近一周|这周|本周|上次|最近一次|20\d{2}[-年/]\d{1,2}月份?|\d{1,2}月份?)"
)
_LEDGER_SPEND_CUE_RE = re.compile(r"(?:花了多少|花多少|花销|消费|支出|多少钱|一共花|总共花|花了|账单|开销)")
_LEDGER_KNOWN_CATS = (
    "外卖", "美团", "滴滴", "打车", "咖啡", "瑞幸", "山姆", "京东", "nous",
    "星巴克", "麦当劳", "肯德基", "淘宝", "抖音", "拼多多", "超市", "餐饮", "交通",
)


def parse_ledger_natural_query(u: NormalizedUtterance) -> Optional[dict[str, str]]:
    """泛化一句话查账（词序无关，支持任意商户 + 时间窗 + 消费动词，如「这个月外卖花了多少钱」「上个月在星巴克花了多少」「本月一共花了多少」）。"""
    if not u.query or u.is_multiline or u.is_meta or len(u.query) > 48:
        return None
    q = u.query
    # 排除「小说消费」「车务成本」「算力资产」等专用面板
    if re.search(r"(?:小说|短剧|车务|养车|净资产|总资产|家底|公私报销|账龄)", q):
        return None

    # 1. 显式「查账 …」/「账单查询 …」
    m_exp = re.match(r"^(?:查账|查一下账|查下账|账单查询)[\s，,:：]*(.+)$", u.core)
    if m_exp:
        return {"query": m_exp.group(1).strip()}

    # 2. 经典品类 + 时间窗（无须「花了多少」后缀，如「外卖 本月」「山姆 上次」「滴滴 10月」）
    q_low = u.body.lower()
    has_time = bool(_LEDGER_TIME_RE.search(q))
    has_spend = bool(_LEDGER_SPEND_CUE_RE.search(q))
    has_known_cat = any(c in q_low for c in _LEDGER_KNOWN_CATS)

    if has_time and (has_spend or has_known_cat):
        # 若只有时间 + 品类无消费词，排除明确非查账句子（如「本月点外卖」「上次美团不好吃」）
        if not has_spend and re.search(r"(?:点|买|吃|推荐|不错|好吃|难吃|改|约|提醒|到了|签收)", q):
            return None
        return {"query": u.body}

    # 3. 「最近一次/上次 + 已知品类/商户」或「查一下 <商户> 花了多少」
    if has_spend and re.search(r"(?:查|看|算|统计|多少|一共|总共)", q):
        if has_known_cat or re.search(r"(?:花了多少|一共花|总共花|消费了多少|支出了多少)", q):
            return {"query": u.body}

    return None


def parse_sure_query(u: NormalizedUtterance) -> Optional[dict[str, str]]:
    """识别 Sure 财务问答（支持「查 sure ...」与自然问句「看看我的净资产」「公私报销账龄怎么样」）。"""
    if not u.query or u.is_multiline or u.is_meta or len(u.query) > 56:
        return None
    m_exp = re.match(r"^(?:查\s*sure|sure\s*查账|财务查询|问\s*sure)[\s，,:：]+(.+)$", u.core, re.IGNORECASE)
    if m_exp:
        return {"question": m_exp.group(1).strip()}
    if re.search(r"(?:净资产|总资产|家底|net\s*worth|公私报销|报销账龄|公私账|固定资产总览)", u.query, re.IGNORECASE):
        if not _WRITE_OR_MUTATE_CUE_RE.search(u.query):
            return {"question": u.body}
    return None


def parse_case_or_wait_action(u: NormalizedUtterance, db: SimingDB) -> Optional[dict[str, Any]]:
    """识别单据销账/取消/降噪、兜底提议确认/忽略、推送恢复、原因解释、忘掉记忆（支持动宾与把字句/主谓句）。"""
    if not u.query or u.is_multiline or u.is_meta or len(u.query) > 64:
        return None

    s = u.core

    # 1. 兜底提议 confirm / dismiss（「记下 W12」「把 W12 记下来」「W12 忽略吧」「不要记 W12」）
    m_prop_1 = re.fullmatch(r"(记下来?|记一下|确认记下|要|保留|忽略|不用记|不要记|不要|算了)\s*(W\d{2,6})", s, re.IGNORECASE)
    m_prop_2 = re.fullmatch(r"(?:把\s*)?(W\d{2,6})\s*(记下来?|记一下|确认记下|忽略|不用记|不要记|算了)(?:了|掉)?", s, re.IGNORECASE)
    if (m_prop_1 or m_prop_2) and not u.had_question:
        if m_prop_1:
            verb, wid = m_prop_1.group(1), m_prop_1.group(2).upper()
        else:
            wid, verb = m_prop_2.group(1).upper(), m_prop_2.group(2)
        confirm = verb.startswith(("记", "确认", "要", "保留"))
        return {"kind": "wait_prop", "wait_id": wid, "confirm": confirm}

    # 2. 推送恢复（「恢复推送 rule」「解除 rule 熔断」「恢复 rule 的推送」）
    m_gr1 = re.fullmatch(r"(?:恢复推送|解除熔断|解除静默)\s+([A-Za-z0-9_:\-*.]+)", s)
    m_gr2 = re.fullmatch(r"(?:把\s*)?(?:解除|恢复)\s*([A-Za-z0-9_:\-*.]+)\s*(?:的)?(?:推送|熔断|静默)", s)
    if (m_gr1 or m_gr2) and not u.had_question:
        rule = (m_gr1.group(1) if m_gr1 else m_gr2.group(1)).strip()
        return {"kind": "guard_restore", "rule_id": rule}

    # 3. 指定编号单据销账/取消/降噪（「已办 S12」「把 S12 销掉」「S12 取了」「S12 办完了」「取消 W08」）
    if not u.had_question:
        m_d1 = re.fullmatch(
            r"(已办|办完了?|完成|搞定了?|销掉|消掉|核销|关闭|取了|拿了|已取|已拿|装好了?|取消|不用管了?|删掉|少推|别再提)\s*([SW]\d{2,6})",
            s,
            re.IGNORECASE,
        )
        m_d2 = re.fullmatch(
            r"(?:把\s*)?([SW]\d{2,6})\s*(?:标记为?)?(已办|办完了?|完成|搞定了?|销掉|消掉|核销|关闭|取了|拿了|已取|已拿|装好了?|取消|不用管了?|删掉|少推|别再提)(?:了|掉)?",
            s,
            re.IGNORECASE,
        )
        if m_d1 or m_d2:
            if m_d1:
                raw_act, tid = m_d1.group(1), m_d1.group(2).upper()
            else:
                tid, raw_act = m_d2.group(1).upper(), m_d2.group(2)
            if any(k in raw_act for k in ("取消", "不用管", "删")):
                norm_act = "取消"
            elif "别再提" in raw_act:
                norm_act = "别再提"
            elif "少推" in raw_act:
                norm_act = "少推"
            else:
                norm_act = "已办"
            return {"kind": "case_act", "target_id": tid, "action": norm_act}

    # 4. 为什么提醒 / 解释单据（「为什么提醒 S12」「S12 为什么提醒我」「解释一下 S12」「为什么发这条提醒」）
    m_why1 = re.match(r"^(?:为什么(?:发|推送?)?(?:提醒|这条提醒?)我?|解释(?:一下)?)\s*([SW]\d{2,6}|这条)?$", u.core, re.IGNORECASE)
    m_why2 = re.match(r"^([SW]\d{2,6})\s*(?:为什么(?:提醒|发|推送?)我?|是啥提醒|是什么提醒|怎么触发的)$", u.core, re.IGNORECASE)
    if m_why1 or m_why2:
        tid = (m_why1.group(1) if m_why1 else m_why2.group(1)) or ""
        return {"kind": "explain", "target_id": tid}

    # 5. 忘掉记忆（「忘掉 X」「把 X 忘掉」「忘记 X」）
    if not u.had_question:
        m_fg1 = re.match(r"^(?:忘掉|忘记)\s+(.+)$", s)
        m_fg2 = re.match(r"^把\s*(.{2,30}?)\s*(?:忘掉|忘记)(?:了)?$", s)
        if m_fg1 or m_fg2:
            kw = (m_fg1.group(1) if m_fg1 else m_fg2.group(1)).strip()
            return {"kind": "forget", "keyword": kw}

    # 6. 按待办名称单句销账（「把牛板筋的待办销掉」「销掉牛板筋」——仅当明确命中库里唯一开启待办时生效，绝不误伤闲聊！）
    if not u.had_question and not any(sep in u.stmt for sep in ("、", "，", ",", "；", ";", "并且", "顺便", "然后")):
        m_close_by_name = re.match(
            r"^(?:(?:销掉|消掉|核销|划掉|销一下|消一下)\s*(?:把|那个|这个)?(.{2,24})|"
            r"把\s*(?:那个|这个)?(.{2,24}?)\s*(?:的?(?:待办|单子|提醒|包裹|快递))?(?:销掉|消掉|核销|划掉|标记完成))$",
            s,
        )
        if m_close_by_name:
            target = (m_close_by_name.group(1) or m_close_by_name.group(2) or "").strip()
            target = re.sub(r"(?:的?(?:待办|单子|提醒|任务|事项|包裹|快递))$", "", target).strip()
            if target and not re.search(r"(?:念头|想法|打算|主意|计划|情绪|烦恼|压力)", target):
                conn = db.connect()
                try:
                    rows = conn.execute(
                        "SELECT case_id, title FROM cases WHERE status IN ('待处理', '待核实') ORDER BY updated_at DESC"
                    ).fetchall()
                finally:
                    conn.close()
                hits = [dict(r) for r in rows if target.lower() in str(r["title"] or "").lower()]
                if len(hits) == 1:
                    return {"kind": "case_act", "target_id": str(hits[0]["case_id"]), "action": "已办"}
                if len(hits) > 1:
                    cands = " / ".join(f"`{h['case_id']}`({h['title'][:18]})" for h in hits[:4])
                    return {
                        "kind": "disambiguate",
                        "reply": f"⚠️ 「{target}」命中了 {len(hits)} 条未结待办（{cands}），请点明编号（如 `已办 {hits[0]['case_id']}`）避免误销。",
                    }

    return None


def parse_defer_remind(u: NormalizedUtterance, db: SimingDB) -> Optional[dict[str, Any]]:
    """泛化自然语言提醒/延后建单（支持时间在前、动词在前、相对分钟/小时/天、日期、周几与「等…时候提醒我」）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta or len(u.stmt) > 80:
        return None
    # 多意图复合句让位给 compound_voice（如「销掉牛板筋、设明天9点提醒我抢号」「销掉那个念头吧、顺便提醒我买水」）
    if any(sep in u.stmt for sep in ("、", "；", ";")) or re.search(r"(?:销掉|消掉|核销|点份|点个|顺便提醒)", u.stmt):
        return None
    # 必须出现明确的提醒/挂起意图词
    if not re.search(r"(?:提醒我|叫我(?:一声)?|通知我|闹钟|再说|别忘了)", u.stmt):
        return None

    from siming.cn_calendar import try_parse_explicit_datetime

    cmd = u.stmt
    # 1. 尝试解析明确时间（含今天/明天/后天/下周/周X/X月X日/X天后/X小时后/半小时后/X分钟后/具体钟点）
    try:
        parsed_dt = try_parse_explicit_datetime(cmd, paths=db.paths)
    except ValueError as exc:
        return {"error": str(exc)}

    if parsed_dt is not None:
        # 防止把普通闲聊（「今天太累了别忘了早睡」之类的无明确动作/时间）误建，要求有「提醒我/叫我/通知我/闹钟」或「设/定/记」
        if re.search(r"(?:提醒我|叫我|通知我|设个?闹钟|定个?闹钟|闹钟|定个?提醒|设个?提醒|加个?提醒)", cmd):
            return {"when_arg": cmd, "quote": cmd}

    # 2. 无明确日期时间的条件型提醒：「等 <条件> (的时候|时|了|后)? (提醒我|再说|叫我)」
    m_cond = re.search(
        r"等(?:我要|需要|下次|到)?([^\s，。！？,!?]{2,18}?)(?:的时候|之时|时|了|以后|后)?(?:帮我)?(?:提醒我|再说|叫我|通知我)",
        cmd,
    )
    if m_cond:
        cond_word = m_cond.group(1).strip()
        if cond_word:
            return {"when_arg": f"intent:{re.escape(cond_word)}", "quote": cmd}

    return None


def parse_price_watch_mutation(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """泛化保价新增与移除（支持链接/名称/价格任意词序及「把 X 移出保价」）。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta:
        return None
    s = u.core
    # 移除保价
    m_rm1 = re.match(r"^(?:移除保价|取消保价|删保价|删除保价|别盯保价)\s*(.+)$", s)
    m_rm2 = re.match(r"^(?:把\s*)?(.{2,30}?)\s*(?:移出保价|取消保价|删掉保价|不盯保价了|别盯保价了)$", s)
    if m_rm1 or m_rm2:
        tgt = (m_rm1.group(1) if m_rm1 else m_rm2.group(1)).strip().rstrip("的 ")
        return {"action": "remove", "target": tgt}

    # 新增保价（经典格式 OR 含京东链接 + 价格 + 保价词）
    m_add = re.match(r"^(?:保价监控|加保价|盯保价)\s+(\S+)\s+(\S+)\s+([¥￥]?[0-9]+(?:\.[0-9]{1,2})?)$", u.stmt)
    if m_add:
        return {"action": "add", "name": m_add.group(1), "sku_or_url": m_add.group(2), "paid": m_add.group(3)}
    if re.search(r"(?:保价|价保|盯降价)", s):
        m_url = re.search(r"(https?://\S+|\b\d{7,16}\b)", s)
        m_price = re.search(r"(?:[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)|([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块))", s)
        if m_url and m_price:
            url_s = m_url.group(1)
            price_s = m_price.group(1) or m_price.group(2)
            rem = s.replace(url_s, " ").replace(m_price.group(0), " ")
            rem = re.sub(r"(?:帮我|替我|给我|盯一下|盯|加|新增|监控|保价|价保|这个链接|链接|商品|实付|花了|的)", " ", rem)
            rem = re.sub(r"\s+", " ", rem).strip(" ，,：:")
            return {"action": "add", "name": rem or f"商品{url_s[-6:]}", "sku_or_url": url_s, "paid": price_s}
    return None


def parse_replenish_action(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """泛化日用品常买清单添加/移除、已买校准、延后、一键补货直买与京东跨端加购物车。"""
    if not u.stmt or u.had_question or u.is_multiline or u.is_meta:
        return None
    m_add = re.match(r"^常买\s+添加\s+(\S+)\s+(\S+)\s+(\d{1,3})(?:\s+([0-9]+(?:\.[0-9]{1,2})?))?$", u.stmt)
    if m_add:
        return {
            "action": "add",
            "name": m_add.group(1),
            "sku_or_url": m_add.group(2),
            "cycle_days": int(m_add.group(3)),
            "target_yuan": float(m_add.group(4)) if m_add.group(4) else None,
        }
    m_rm = re.match(r"^常买\s+(?:移除|删除)\s+(.+)$", u.stmt)
    if m_rm:
        return {"action": "remove", "target": m_rm.group(1).strip()}

    # 京东加购物车（严格保留原有接口：「加购物车 X」「把 X 加入京东购物车」「京东加购 X」）
    m_cart1 = re.match(r"^(?:加购物车|加入购物车|京东加购|加京东购物车)\s*(.+)$", u.core)
    m_cart2 = re.match(r"^把\s*(.+?)\s*(?:加到|加入|放进)(?:京东)?购物车$", u.core)
    if m_cart1 or m_cart2:
        raw_target = (m_cart1.group(1) if m_cart1 else m_cart2.group(1)).strip()
        if raw_target and not re.search(r"(?:美团|外卖)", u.stmt):
            return {"action": "cart", "query": raw_target}

    # 统一补货直买（「补货 X」「立即补货 X」「直接买 X」）
    m_buy = re.match(r"^(?:(?:立即)?补货|直接买)\s+(.+)$", u.core)
    if m_buy:
        raw_target = m_buy.group(1).strip()
        if raw_target:
            return {"action": "buy", "query": raw_target}

    # 标记已自购重置周期（「已买 卷纸」「刚买过 滴露」）
    m_bought = re.match(r"^(?:已买|买过了|刚买过|刚买了)\s+(.+)$", u.core)
    if m_bought:
        return {"action": "bought", "target": m_bought.group(1).strip()}

    # 延后补货提醒（「延后补货 卷纸 14」）
    m_snz = re.match(r"^延后补货\s+(.+?)(?:\s+(\d{1,3})\s*天?)?$", u.core)
    if m_snz:
        return {"action": "snooze", "target": m_snz.group(1).strip(), "days": int(m_snz.group(2) or 14)}
    return None


def parse_movein_action(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """泛化新房入住总控动作：外箱初验登记、工序门禁、装修照片检索、售后备忘登记、家电售后护照查询。"""
    if not u.query or u.is_multiline or u.is_meta:
        return None
    s = u.core

    if not u.had_question:
        m_arr = re.match(r"^(?:已验外观|外箱完好|外箱都?验好了?|外观已验)(?:\s+(.+))?$", s)
        m_arr2 = re.match(r"^(.{2,20}?)(?:的)?(?:外箱完好|外观完好|外箱验好了)$", s)
        if m_arr:
            return {"action": "arrival_mark", "target": (m_arr.group(1) or "all").strip()}
        if m_arr2:
            return {"action": "arrival_mark", "target": m_arr2.group(1).strip()}

        m_gate1 = re.match(r"^工序(就绪|未就绪)\s*(\S+)$", s)
        m_gate2 = re.match(r"^(燃气|吊顶|橱柜|上下水|水电|台面|通气)(?:工序)?(?:已经|已)?(就绪|未就绪|好了|没好|还没好)(?:了)?$", s)
        if m_gate1:
            return {"action": "gate", "gate": m_gate1.group(2), "ready": m_gate1.group(1) == "就绪"}
        if m_gate2:
            return {"action": "gate", "gate": m_gate2.group(1), "ready": m_gate2.group(2) in ("就绪", "好了")}

        m_note = re.match(r"^登记售后\s+(\S+)\s+(.+)$", s)
        if m_note:
            return {"action": "note", "target": m_note.group(1), "note": m_note.group(2)}

    # 照片检索（「新房照片」「查水电图 主卧」「看看主卧的水电图」「找一下厨房装修照片」）
    m_ph1 = re.match(r"^(?:新房照片|查水电图|水电图|装修照片)(?:\s*(.+))?$", u.body)
    m_ph2 = re.match(r"^(.{2,12}?)(?:的)?(?:水电图|装修照片|新房照片|施工照)$", u.body)
    if m_ph1:
        return {"action": "photos", "query": (m_ph1.group(1) or "").strip()}
    if m_ph2:
        return {"action": "photos", "query": m_ph2.group(1).strip()}

    # 家电售后护照（「家电护照」「售后护照」「售后 林内」「林内保修」「查一下格力的售后」）
    if u.body in ("家电护照", "售后护照") or u.topic in ("家电护照", "售后护照"):
        return {"action": "passport", "query": ""}
    m_pp1 = re.match(r"^(?:售后|保修|家电护照|售后护照)[\s:：]*(.{2,24})$", u.body)
    m_pp2 = re.match(r"^(.{2,16}?)(?:的)?(?:售后|保修|保修期|三包|售后护照)$", u.body)
    if m_pp1:
        return {"action": "passport", "query": m_pp1.group(1).strip()}
    if m_pp2 and not re.search(r"(?:怎么|如何|什么|登记)", u.query):
        return {"action": "passport", "query": m_pp2.group(1).strip()}

    return None


def parse_tesla_mutation(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """泛化特斯拉唤醒、手动更新电量、出行参谋查询。"""
    if not u.stmt or u.is_multiline or u.is_meta:
        return None
    s = u.core
    if not u.had_question:
        if re.fullmatch(r"(?:唤醒特斯拉|刷新特斯拉|强刷车电量|刷新一下?特斯拉(?:电量)?|唤醒一下?特斯拉)", s, re.IGNORECASE):
            return {"action": "wake"}
        m_soc = re.match(
            r"^(?:特斯拉电量|更新车电量|更新特斯拉电量|车电量(?:更新为?|现在是?|剩)?|特斯拉(?:现在)?(?:电量|剩)|model\s*y\s*(?:电量|剩))\s*(\d{1,3})\s*%?"
            r"(?:[\s，,]+(?:续航)?\s*(\d{1,4})\s*(?:km|公里)?)?$",
            s,
            re.IGNORECASE,
        )
        if m_soc:
            return {"action": "set_soc", "soc": m_soc.group(1), "range_km": m_soc.group(2) or ""}

    m_out = re.match(r"^(?:出行参谋|开车去|自驾去|算一下开车去|看看开车去)\s*(.+)$", u.body)
    if m_out and len(m_out.group(1).strip()) >= 2:
        return {"action": "outing_plan", "dest": m_out.group(1).strip()}
    return None


def parse_misc_commands(u: NormalizedUtterance) -> Optional[dict[str, Any]]:
    """泛化其余快捷参数指令（十秒续工、收藏夹检索/试跑、睡前盯、联邦图谱、桌游柜、本命、Wi-Fi/常用地址、报销交单/CW01打包/待回款登记、规律修改）。"""
    if not u.query or u.is_multiline or u.is_meta:
        return None
    s = u.core
    b = u.body

    # 1. CW01 国内报销打包
    if not u.had_question and (
        s.lower() in ("打包cw01", "国内报销打包", "打车报销打包", "打包国内报销", "打包打车报销", "组包cw01")
        or (re.search(r"(?:打包|组包)", s) and re.search(r"(?:cw01|打车报销|国内报销|打车票|国内发票)", s, re.IGNORECASE))
    ):
        return {"action": "pack_cw01"}

    # 2. 报销交单
    if not u.had_question:
        m_sub = re.match(r"^(?:已交单|已提单|交了|交单)\s+(CW0[18]-[A-Z]+-\d{12})(?:\s+(\S+))?$", s, re.IGNORECASE)
        if m_sub:
            return {"action": "reimburse_submit", "batch_no": m_sub.group(1), "draft_ref": m_sub.group(2) or "latest"}

    # 3. 账本消歧
    if not u.had_question:
        m_tx = re.match(r"^消歧\s+(TX-[0-9a-zA-Z-]+)\s+(备注|配对|重复作废)(?:\s+(.+))?$", s, re.IGNORECASE)
        if m_tx:
            return {"action": "ledger_resolve", "tx_id": m_tx.group(1), "op": m_tx.group(2), "val": m_tx.group(3) or ""}

    # 4. 待回款登记
    if not u.had_question:
        m_rcv = re.match(r"^(?:登记)?(待回款|已保价)\s+(\S+)\s+([¥￥]?[0-9]+(?:\.[0-9]{1,2})?)(?:\s+(\d{4}))?$", s)
        if m_rcv:
            return {
                "action": "receivable_add",
                "is_pp": m_rcv.group(1) == "已保价",
                "title": m_rcv.group(2),
                "amount": m_rcv.group(3),
                "card": m_rcv.group(4) or "",
            }

    # 5. 常用地址 / Wi-Fi 绑定
    if not u.had_question:
        m_pl = re.match(r"^常用地址\s+设\s+(\S+)\s+(.+)$", s)
        if m_pl:
            return {"action": "saved_place", "alias": m_pl.group(1), "addr": m_pl.group(2)}
        m_wf1 = re.match(r"^绑定\s*wifi\s+(\S+)\s+(\S+)$", s, re.IGNORECASE)
        m_wf2 = re.match(r"^把\s*wifi\s+(\S+)\s*(?:绑定|映射)(?:为|成|到)\s*(\S+)$", s, re.IGNORECASE)
        if m_wf1 or m_wf2:
            m_w = m_wf1 or m_wf2
            assert m_w is not None
            return {"action": "wifi_bind", "ssid": m_w.group(1), "alias": m_w.group(2)}

    # 6. 验证码代登
    if not u.had_question:
        m_otp = re.match(r"^(验证码登录|代登|短信登录|填验证码)\s*(\S+)$", s)
        if m_otp:
            return {"action": "otp_login", "verb": m_otp.group(1), "platform": m_otp.group(2)}

    # 7. 十秒续工（「十秒续工」「续工 siming」「续 indie_game」「看看 siming 做到哪了」）
    m_res1 = re.match(r"^(?:十秒续工|续工)(?:一下)?(?:\s+([A-Za-z0-9_.\-\u4e00-\u9fff]+))?$|^续\s+([A-Za-z0-9_.\-\u4e00-\u9fff]+)$", b)
    m_res2 = re.match(r"^([A-Za-z0-9_.\-\u4e00-\u9fff]{2,16})\s*(?:做到哪了|改到哪了|进展到哪了|续工卡)$", b)
    if m_res1:
        return {"action": "resume", "project": (m_res1.group(1) or m_res1.group(2) or "").strip()}
    if m_res2 and m_res2.group(1).lower() in ("siming", "司命", "indie_game", "独游项目", "personas", "deco", "装修", "boardgame_kb", "buy-track"):
        return {"action": "resume", "project": m_res2.group(1).strip()}

    # 8. 规律修改
    if not u.had_question:
        m_rt = re.match(r"^(暂停规律|删除规律|删掉规律|恢复规律|开启规律)\s+(R-[A-Za-z0-9_-]+)$", s, re.IGNORECASE)
        if m_rt:
            return {"action": "routine_mutate", "verb": m_rt.group(1), "rid": m_rt.group(2)}

    # 9. 收藏夹 & 试跑
    m_bm = re.match(r"^(?:收藏夹|我的收藏)(?:\s+(.+))?$|^搜(?:一下)?收藏\s*(.+)$", b)
    if m_bm:
        return {"action": "bookmarks_list", "query": (m_bm.group(1) or m_bm.group(2) or "").strip()}
    if not u.had_question:
        m_try = re.match(r"^试跑(?:一下)?\s+(\S+)$", s)
        if m_try:
            return {"action": "bookmark_try", "target": m_try.group(1)}

    # 10. 睡前盯
    if not u.had_question:
        m_lab = re.match(r"^睡前盯(?:一下)?[\s，,:：]*(.+)$", s)
        if m_lab:
            return {"action": "sleep_watch", "target": m_lab.group(1).strip()}

    # 11. 桌游柜容量
    m_bg = re.match(r"^桌游柜(?:\s+(.+))?$", b)
    if m_bg:
        return {"action": "boardgame_cabinet", "item": (m_bg.group(1) or "新桌游").strip()}

    # 12. 联邦检索 / 全机图谱
    m_gr = re.match(r"^(?:全机图谱|联邦检索|跨应用查(?:一下|下)?)\s+(.+)$", b)
    if m_gr:
        return {"action": "graph_query", "query": m_gr.group(1).strip()}

    # 13. 本命紫微参考
    m_nat = re.match(r"^本命(?:\s*(\S+?))?(?:\s*(全文))?$", b)
    if m_nat:
        dim = (m_nat.group(1) or "").strip()
        full = bool(m_nat.group(2))
        if dim == "全文":
            dim, full = "", True
        return {"action": "natal", "dim": dim, "full": full}

    # 14. 晨札分卷全文（「全文 桌游」「展开晨札 紫微」）
    m_full = re.match(r"^(?:晨札全文|早报全文|展开晨札|全文)\s+(.+)$", b, re.IGNORECASE)
    if m_full:
        return {"action": "morning_full_section", "section": m_full.group(1).strip()}

    return None


def parse_knowledge_ask(u: NormalizedUtterance) -> Optional[str]:
    """泛化本机知识只读查询（「查一下次卧窗帘盒多宽」「帮我搜一下工长报价」），保持一般概念科普放行给 LLM。"""
    if not u.query or u.is_multiline:
        return None
    m = re.match(r"^(?:(?:帮我|替我|给我)?(?:查一下|查下|查一查|搜一下|搜下|查查)[\s，,:：]*|查\s+)(.+)$", u.query)
    if not m:
        return None
    ask_q = m.group(1).strip()
    is_explicit_cha = bool(re.match(r"^查\s+", u.query))
    if not is_explicit_cha and _GENERAL_CONCEPT_RE.search(ask_q):
        return None
    return ask_q


# ---------------------------------------------------------------------------
# Layer 2C · 引用回复泛化意图识别（用于 _handle_subscribed_reply）
# ---------------------------------------------------------------------------


def classify_quoted_reply(
    raw_text: str,
    target_kind: str,
    *,
    case_category: str = "",
) -> Optional[dict[str, Any]]:
    """对引用回复正文做泛化意图归类（兼容「已经拿了」「刚取回来」「都拿了」「办好了」「这个不用管了」「太吵了少推点」等）。"""
    u = normalize_qq_input(raw_text)
    t_stmt = u.stmt
    t_q = u.query
    if not t_q:
        return None

    # 1. 晨札引用 -> 展开全文
    if target_kind == "morning_brief":
        m_full = re.match(r"^(?:晨札|早报)?(?:全文|展开|详细|详版|看全文|完整版)(?:\s+(.+))?$", u.body, re.IGNORECASE)
        if m_full or re.search(r"(?:全文|展开看|看详版|详细版)", t_q):
            return {"action": "morning_full", "section": (m_full.group(1) if m_full and m_full.group(1) else "").strip()}
        return None

    # 2. 外箱初验引用
    if target_kind == "arrival_check":
        m_arr = re.match(r"^(?:已验外观|外箱完好|外箱都?验好了?|外观完好|没问题验好了)(?:\s+(.+))?$", u.core)
        if m_arr and not u.had_question:
            return {"action": "arrival_mark", "target": (m_arr.group(1) or "all").strip()}
        return None

    # 3. 问句绝不触发状态修改
    if u.had_question:
        return None

    s = u.core

    # 4. 取消 / 不用管
    if re.fullmatch(r"(?:(?:这个|这条|这单)?(?:取消|不用管了?|不用提醒了?|算了|撤回|作废|别管了|stop|cancel)(?:掉|了)?)", s, re.IGNORECASE):
        return {"action": "cancel"}

    # 5. 调频降噪
    if re.search(r"(?:少推|别再提|别老推|不要再提|不要再推|太吵)", s):
        act = "别再提" if re.search(r"(?:别再提|不要再提|不要再推)", s) else "少推"
        return {"action": "mute", "feedback_action": act}

    # 6. 保价单据专属
    if target_kind == "case" and case_category == "price_protect":
        if re.fullmatch(r"(?:(?:已经|刚)?(?:已保价|保价了|已申请(?:保价)?|申请保价了|申请了|退差了?|退差价了?))", s):
            return {"action": "price_claimed"}
        if re.fullmatch(r"(?:不看了|移出保价|不用盯了|别盯(?:这个)?了|不用监控了|静音)", s):
            return {"action": "price_mute"}

    # 7. 周期用药打针专属
    if target_kind == "case" and case_category == "med_injection":
        if re.fullmatch(
            r"(?:(?:我)?(?:刚才|刚刚|已经|刚)?(?:这周的?|今晚的?)?(?:针|周期用药)?(?:已经|刚)?(?:打了|打针了|已打|打完了?|打过了|打好了|已办|办完了?|办好了?|搞定了?))",
            s,
        ):
            return {"action": "med_inject", "skipped": False}
        if re.fullmatch(
            r"(?:(?:这周|本周|这针)?(?:先)?(?:跳过|跳过去|跳过这针|跳过本周|不打了?|先不打))",
            s,
        ):
            return {"action": "med_inject", "skipped": True}

    # 8. 周期用药开药专属
    if target_kind == "case" and case_category == "med_refill":
        if re.fullmatch(r"(?:(?:我)?(?:已经|刚)?(?:已约|约了|约好了?|预约了?|预约好了?|挂号了?|挂好了?|挂上号了?)(?:门诊)?)", s):
            return {"action": "med_booked"}
        m_rf = re.match(
            r"^(?:(?:我)?(?:今天|刚才|刚刚|已经|刚)?(?:去医院)?(?:已办|办完了?|办好了?|搞定了?|开药了?|拿药了?|开了药|开好药了?|补药了?|续开了?))"
            r"(?:[\s，,]*(\d{1,2}|[一二两三四五六七八九十])\s*(?:支|笔|盒)?)?$",
            s,
        )
        if m_rf:
            return {"action": "med_refill", "pens_token": m_rf.group(1) or ""}

    # 9. 通用已办 / 取件 / 签收 / 安装完成核销（排除「还没拿」「没取」「未签收」等否定）
    if not re.search(r"(?:没|未|还没|还没来得及|忘了|忘记)", s):
        if re.fullmatch(
            r"(?:(?:我)?(?:刚才|刚刚|已经|刚|都|全|全部)?(?:已办|办完了?|办好了?|弄好了?|处理好了?|处理完了?|已还|还了|还完了?|"
            r"搞定了?|已签收|签收了|收到了|取了|取完了?|取回来了?|拿了|拿到了|拿完了?|拿回来了?|已拿|已取|取餐了?|装好了?|安装好了?|安装完了?))",
            s,
        ):
            return {"action": "done"}

    return None


# ---------------------------------------------------------------------------
# Layer 3 · 可插拔安全语义兜底分类器（仅限只读视图与只读查询，严守 A2 红线）
# ---------------------------------------------------------------------------

SAFE_SEMANTIC_FALLBACK_INTENTS: frozenset[str] = frozenset(
    {vs.intent for vs in PRESET_VIEW_SPECS}
    | {"ledger_ask", "sure_ask", "meituan_history_list", "meituan_recommend"}
)


def build_semantic_intent_catalog() -> list[dict[str, str]]:
    """导出可供轻量语义分类器消费的统一意图目录（单真源）。"""
    catalog = [{"intent": vs.intent, "description": vs.description} for vs in PRESET_VIEW_SPECS]
    catalog.extend(
        [
            {"intent": "ledger_ask", "description": "一句话只读查账（品类/商户 + 时间窗消费统计），需返回 args.query"},
            {"intent": "sure_ask", "description": "财务 OS 净资产/公私报销账龄问答，需返回 args.question"},
            {"intent": "meituan_history_list", "description": "列出历史常点美团外卖整单以便复购"},
            {"intent": "meituan_recommend", "description": "美团附近外卖/用餐推荐，需返回 args.hint"},
        ]
    )
    return catalog


def classify_qq_intent_with_llm(
    db: SimingDB,
    u: NormalizedUtterance,
    *,
    llm_classifier: Optional[Callable[[str, list[dict[str, str]]], Optional[dict[str, Any]]]] = None,
) -> Optional[dict[str, Any]]:
    """Layer 3 可选语义意图兜底（默认不阻塞普通聊天；仅放行白名单内只读/安全预览意图，绝不触碰 A2 执行/付款）。"""
    if not u.query or u.is_multiline or u.is_meta or len(u.query) > 64:
        return None
    if _WRITE_OR_MUTATE_CUE_RE.search(u.query):
        return None

    catalog = build_semantic_intent_catalog()
    raw_res: Optional[dict[str, Any]] = None

    if llm_classifier is not None:
        try:
            raw_res = llm_classifier(u.query, catalog)
        except Exception:
            raw_res = None
    else:
        if os.environ.get("SIMING_TEST_OFFLINE") == "1" or os.environ.get("SIMING_DISABLE_LLM") == "1":
            return None
        try:
            from siming.config import load_policy

            pol = load_policy(db.paths)
            nlu_cfg = pol.get("qq_nlu") or {}
            if not bool(nlu_cfg.get("llm_fallback", False)):
                return None
            # 仅当文本包含至少一个与司命领域相关的轻量信号词时才唤醒，避免普通闲聊产生延迟
            if not re.search(
                r"(?:晨札|待办|快递|包裹|外卖|美团|吃饭|点餐|打车|滴滴|账单|花了|消费|报销|回款|净资产|算力|订阅|"
                r"特斯拉|电车|空调|新房|甲醛|安装|送装|家电|保修|保价|口罩|针头|打针|周期用药|挂号|拓竹|打印|nas|灾备|规律|收藏|桌游|行程|模块|作息|定位|额度|链路)",
                u.query,
                re.IGNORECASE,
            ):
                return None
            from siming.quota import run_coding_plan_prompt

            cat_txt = "\n".join(f"- {c['intent']}: {c['description']}" for c in catalog)
            prompt = (
                "你是司命个人助理的严格意图路由器。判断下面 QQ 消息是否明确对应以下只读预设功能之一。\n"
                "如果是普通闲聊、概念提问、写代码需求或不确定，请输出 {\"intent\": \"none\", \"confidence\": 0.0}。\n"
                "只输出一行合法 JSON：{\"intent\": \"...\", \"confidence\": 0.0~1.0, \"args\": {...}}。\n\n"
                f"可用意图：\n{cat_txt}\n\n用户消息：{u.query}"
            )
            r_llm = run_coding_plan_prompt(prompt, db=db, read_only=True, timeout_sec=8, ref_id="qq_nlu")
            if r_llm.get("ok") and r_llm.get("output"):
                m_j = re.search(r"\{[\s\S]*\}", str(r_llm["output"]))
                if m_j:
                    raw_res = json.loads(m_j.group(0))
        except Exception:
            raw_res = None

    if not isinstance(raw_res, dict):
        return None
    intent = str(raw_res.get("intent") or "").strip()
    try:
        conf = float(raw_res.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    if intent not in SAFE_SEMANTIC_FALLBACK_INTENTS or conf < 0.78:
        return None
    args = raw_res.get("args") if isinstance(raw_res.get("args"), dict) else {}
    return {"intent": intent, "args": args, "confidence": conf, "via": "llm_semantic"}
