"""日用品周期补货中枢 (`replenish_cart`)：消费记录周期推算、控制塔台账管理、到期 QQ 弹按钮确认与美团/京东一键直买。

能力闭环：
1. 消费记录推算：基于 `shop_order_items` 与 `ledger_txns`（自动剔除退款/取消/VOID）自动匹配历史购买记录、计算真实补货间隔与日耗速率；
2. 塔台可视化管理：为控制塔提供 `/api/replenish` 读写接口，支持分场景（家/公司）、分渠道（美团直买付款 / 京东跨端购物车）、一键校准/延后/暂停/重算周期；
3. 到期主动弹 QQ + 点确认直接买：主节拍巡检临期日用品并推送带原生内嵌按钮的 QQ 卡片（`[🛒 确认直接买]` / `[✅ 我已买过]` / `[⏳ 还有余量 +14天]` / `[⏸️ 暂不提醒]`）；
   - 美团闪购渠道：点「确认直接买」立即后台 9222 切至 `家/公司` 地址实时加购核价，弹出标准 `meituan_submit_pay` 实付确认卡（`[💳 确认支付 Axx]`），二次确认后自动提交支付并推进下次补货周期；
   - 京东渠道：点「确认加京东车」签发 `jd_add_cart` 确认卡，确认后经 9222 `InitCart.aspx` 加入跨端同步购物车并返回 `openapp.jdmobile://` 结算直达链接，同时推进周期起点。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from siming import db as _db_mod
from siming.approval import create_approval
from siming.cdp import inspect_url_in_background_tab
from siming.config import load_policy
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso

JD_MOBILE_CART_DEEPLINK = 'openapp.jdmobile://virtual?params=%7B%22category%22%3A%22jump%22%2C%22des%22%3A%22cart%22%7D'
REPLENISH_ITEMS_META_KEY = "replenish_items"
REPLENISH_APPROVAL_MAP_KEY = "replenish_approval_map"

VALID_CHANNELS = ("meituan", "jd", "taobao", "sams", "hema")
CHANNEL_META: dict[str, dict[str, str]] = {
    "meituan": {"short": "美团直买", "label": "美团闪购直买", "icon": "🛵"},
    "jd": {"short": "京东", "label": "京东跨端购物车", "icon": "📦"},
    "taobao": {"short": "淘宝", "label": "淘宝 / 天猫复购", "icon": "🧡"},
    "sams": {"short": "山姆", "label": "山姆会员商店囤货", "icon": "🏬"},
    "hema": {"short": "盒马", "label": "盒马鲜生复购", "icon": "🦛"},
}
VALID_LOCATIONS = ("家", "公司")
VALID_TIERS = ("core", "recommended", "movein")

# 核心 + 消费记录挖掘 + 新房待启用日用品模板（附历史消费匹配规则）
DEFAULT_DAILY_CATALOG: tuple[dict[str, Any], ...] = (
    # ==================== 1. 用户指定核心日用品（9 项，家/公司拆清） ====================
    {
        "item_id": "rpl_laundry_pods",
        "name": "洗衣凝珠",
        "category": "清洁洗护",
        "location": "家",
        "spec": "约 50–60 颗/盒（若山姆大盒 100 颗可改 120 天）",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "洗衣凝珠",
        "sku": "",
        "qty": 1,
        "cycle_days": 60,
        "lead_days": 5,
        "target_yuan": 55.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(洗衣凝珠|凝珠|洗衣液|碧浪|奥妙|蓝月亮洗衣|立白洗衣)",
        "exclude_re": r"(洗衣机|干洗|清洗剂|洗碗)",
        "default_evidence": "账本中无单品明细（可能在山姆/京东大单内囤货）；按单人/双人每周洗 2–3 次估算 50–60 颗装约 60 天",
        "notes": "可按实际购买盒装颗数在塔台微调周期（50颗≈60天 / 100颗≈120天）",
    },
    {
        "item_id": "rpl_dettol",
        "name": "滴露消毒液",
        "category": "清洁洗护",
        "location": "家",
        "spec": "Dettol/滴露 安心百分百消毒液 1.8L（盒马常购，约 95 天）",
        "channel": "hema",
        "meituan_shop": "",
        "meituan_query": "",
        "sku": "",
        "qty": 1,
        "cycle_days": 95,
        "lead_days": 7,
        "target_yuan": 79.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": False,
        "match_re": r"(滴露|Dettol)",
        "exclude_re": r"(洗衣机清洗剂|洗碗)",
        "default_evidence": "消费记录 4 笔（05-04 750ml → 06-21 750ml 间隔 48 天；06-22 1.8L → 11-01 盒马 1.8L 间隔 132 天），稳定日耗 ~18ml/天",
        "notes": "固定在盒马买 1.8L（上次 11-01 盒马 ¥79），预计用到 2 月初；到期提醒去盒马 App 复购",
    },
    {
        "item_id": "rpl_toilet_paper",
        "name": "卷纸",
        "category": "纸品湿巾",
        "location": "家",
        "spec": "维达蓝色经典有芯卷纸 4层140g×10卷",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "维达蓝色经典有芯卷纸 10卷",
        "sku": "",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 7,
        "target_yuan": 24.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(卷纸|卫生纸|厕纸)",
        "exclude_re": r"(厨房)",
        "default_evidence": "美团小象超市 2 笔精确记录：04-18 & 07-17 均买维达 10 卷，实测间隔 90 天（约 9 天/卷）",
        "notes": "07-17 后 08-16 去过山姆可能顺手囤过大提；校准一次余量即可开启自动倒计时",
    },
    {
        "item_id": "rpl_tissue_home",
        "name": "抽纸（家）",
        "category": "纸品湿巾",
        "location": "家",
        "spec": "山姆大提抽纸（Member's Mark / 维达整提装，配送/自提回家）",
        "channel": "sams",
        "meituan_shop": "",
        "meituan_query": "",
        "sku": "",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 7,
        "target_yuan": 50.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": False,
        "match_re": r"(山姆.*抽纸|Member.?s Mark.*抽纸)",
        "exclude_re": r"(洗碗|湿巾|湿纸巾|厨房|退款)",
        "default_evidence": "家里抽纸固定在山姆会员商店囤大提装（山姆账单无单品明细，最近一次记录 08-16）；整提按常规估算约 90 天",
        "notes": "家里抽纸在山姆买（公司抽纸仍走美团即购超市）；回「已买 抽纸（家）」或在塔台点「刚买过」校准起点",
    },
    {
        "item_id": "rpl_tissue_office",
        "name": "抽纸（公司）",
        "category": "纸品湿巾",
        "location": "公司",
        "spec": "维达抽纸 130抽×6包/提 S码（配送到公司）",
        "channel": "meituan",
        "meituan_shop": "即购超市",
        "meituan_query": "维达抽纸 6包",
        "sku": "",
        "qty": 1,
        "cycle_days": 55,
        "lead_days": 5,
        "target_yuan": 22.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(维达抽纸|抽纸)",
        "exclude_re": r"(洗碗|湿巾|湿纸巾|厨房|名创|退款)",
        "daytime_only": True,
        "default_evidence": "05-21(周四 12:04) 与 11-18(周三 12:54) 均在工作日午间下单即购超市维达 6 包；工位 1 提约用 55 天",
        "notes": "配送地址自动切「公司」（示例写字楼）",
    },
    {
        "item_id": "rpl_humidifier_filter",
        "name": "加湿器滤芯",
        "category": "家电滤芯",
        "location": "家",
        "spec": "蒸发式加湿器抗菌滤芯（待填机型/京东SKU）",
        "channel": "jd",
        "meituan_shop": "",
        "meituan_query": "加湿器滤芯",
        "sku": "",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 10,
        "target_yuan": 69.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(加湿器.*滤芯|加湿.*滤网)",
        "exclude_re": r"^$",
        "default_evidence": "消费记录暂无单品明细；北京供暖季（11–3 月）水质偏硬，蒸发滤芯常规更换周期约 90 天",
        "notes": "请把加湿器型号或京东商品链接填进塔台（或直接 QQ 发我），绑定 SKU 后到期可一键加京东车",
    },
    {
        "item_id": "rpl_trash_bags",
        "name": "垃圾袋",
        "category": "垃圾收纳",
        "location": "家",
        "spec": "青崎抽绳加厚手提式大号（淘宝常购，100只约90天；¥75囤货装约180天）",
        "channel": "taobao",
        "meituan_shop": "",
        "meituan_query": "",
        "sku": "",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 7,
        "target_yuan": 75.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": False,
        "match_re": r"(垃圾袋)",
        "exclude_re": r"(垃圾桶|垃圾夹)",
        "default_evidence": "支付宝查到 2025-08-17 在淘宝囤过青崎抽绳加厚垃圾袋（¥75 大包）；常规 100 只/盒按每天 1–1.5 个约 90 天",
        "notes": "固定在淘宝买；如平时按 ¥75 大包囤货可把周期调为 150–180 天，在塔台点「刚买过」即启倒计时",
    },
    {
        "item_id": "rpl_bottled_water",
        "name": "瓶装水",
        "category": "饮用水",
        "location": "家",
        "spec": "农夫山泉饮用天然水 1.5L×12瓶（整箱 18L；若门店为 1.5L×6瓶 可按两提折算）",
        "channel": "meituan",
        "meituan_shop": "即购超市",
        "meituan_query": "农夫山泉饮用天然水 1.5L 12瓶",
        "sku": "",
        "qty": 1,
        "cycle_days": 18,
        "lead_days": 3,
        "target_yuan": 39.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(农夫山泉.*(?:1\.5L|2L|天然水)|百岁山矿泉水)",
        "exclude_re": r"(食用冰|12L|桶装)",
        "default_evidence": "美团 6 笔高频复购（05-02/23、06-01/12/28、08-19），稳定日耗 ~1.0L/天；已按本人要求切换为 1.5L 整箱规格并在附近商超比价择优",
        "notes": "已指定 1.5L 规格；补货下单时自动对比附近商超券后到手价并择优勾选最大额闪购/美团红包",
    },
    {
        "item_id": "rpl_barrel_water",
        "name": "桶装水",
        "category": "饮用水",
        "location": "家",
        "spec": "农夫山泉 12L×3桶 一次性大桶装（36L，淘宝常购，配小熊抽水器）",
        "channel": "taobao",
        "meituan_shop": "",
        "meituan_query": "",
        "sku": "",
        "qty": 1,
        "cycle_days": 35,
        "lead_days": 4,
        "target_yuan": 51.0,
        "tier": "core",
        "enabled": True,
        "auto_buy": False,
        "match_re": r"(农夫山泉.*12L|12L\*3桶|桶装.*饮用|大桶水)",
        "exclude_re": r"(抽水器|水泵|酸辣粉|酸奶)",
        "default_evidence": "06-28 淘宝购农夫山泉 12L×3桶(36L)+2L×8瓶(16L) 共 52L，叠加 07-06 买小熊抽水器，恰好 52 天后 08-19 再买水 → 稳定 1.0L/天，3 桶约 35 天",
        "notes": "固定在淘宝买（农夫山泉 12L×3 桶约 ¥51）；回「已买 桶装水」或在塔台点「刚买过」校准最近一次购买日",
    },
    # ==================== 2. 消费记录挖掘的高频常买日用品 ====================
    {
        "item_id": "rpl_mask",
        "name": "医用外科口罩",
        "category": "照护医疗",
        "location": "家",
        "spec": "海氏海诺一次性医用外科口罩 50只/盒",
        "channel": "meituan",
        "meituan_shop": "乐购达",
        "meituan_query": "海氏海诺一次性医用外科口罩 50只",
        "sku": "",
        "qty": 1,
        "cycle_days": 48,
        "lead_days": 5,
        "target_yuan": 24.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(医用外科口罩|海氏海诺.*口罩|口罩.*50只)",
        "exclude_re": r"^$",
        "default_evidence": "美团 5 次规律复购（05-12/07-02/08-21/10-05/11-22），间隔 [51, 50, 45, 48] 天，精准 48 天/盒",
        "notes": "与 daily_care 口罩余量同步；刚在 11-22 乐购达五道口店补过 1 盒",
    },
    {
        "item_id": "rpl_wet_wipes",
        "name": "卫生湿巾 / 湿厕纸",
        "category": "纸品湿巾",
        "location": "家",
        "spec": "心相印卫生湿纸巾 80片/包",
        "channel": "meituan",
        "meituan_shop": "即购超市",
        "meituan_query": "心相印卫生湿纸巾 80片",
        "sku": "",
        "qty": 1,
        "cycle_days": 60,
        "lead_days": 5,
        "target_yuan": 15.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(心相印.*湿|卫生湿巾|卫生湿纸巾|湿厕纸)",
        "exclude_re": r"(便携)",
        "default_evidence": "美团 2 笔记录（05-02 小象、11-18 即购超市心相印 80 片），居家常规 1 包约 60 天",
        "notes": "11-18 刚补货，与抽纸同店即购超市可合单免凑起送",
    },
    {
        "item_id": "rpl_alcohol_pad",
        "name": "医用酒精棉片",
        "category": "照护医疗",
        "location": "家",
        "spec": "海氏海诺医用酒精消毒棉片 50片/盒 ×2",
        "channel": "meituan",
        "meituan_shop": "京亿康医疗器械",
        "meituan_query": "海氏海诺医用酒精消毒棉片 50片",
        "sku": "",
        "qty": 2,
        "cycle_days": 180,
        "lead_days": 10,
        "target_yuan": 33.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(酒精.*棉片|消毒棉片)",
        "exclude_re": r"(棉签|湿巾)",
        "default_evidence": "美团京亿康 3 笔复购（04-29 ×1、07-27 ×1、11-11 ×2），约 0.51 片/天；11-11 买了 2 盒(100片)约够 180 天",
        "notes": "周期用药注射与日常消毒常备",
    },
    {
        "item_id": "rpl_needle",
        "name": "注射笔针头",
        "category": "照护医疗",
        "location": "家",
        "spec": "新优锐 32G×4mm 一次性注射笔针头 7支/盒 ×2",
        "channel": "meituan",
        "meituan_shop": "养生堂药店",
        "meituan_query": "新优锐 注射笔用针头 7支",
        "sku": "",
        "qty": 2,
        "cycle_days": 92,
        "lead_days": 10,
        "target_yuan": 39.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(注射笔.*针头|针头.*7支)",
        "exclude_re": r"^$",
        "default_evidence": "美团养生堂药店 2 笔（08-18 ×1盒、09-30 ×2盒=14支），每周 1 支，当前余 5 支预计 12-31 见底",
        "notes": "联动 daily_care 针头校准余量",
    },
    {
        "item_id": "rpl_shampoo",
        "name": "洗发水",
        "category": "个护日用",
        "location": "家",
        "spec": "海飞丝清爽控油洗发水套装",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "海飞丝清爽控油洗发水",
        "sku": "",
        "qty": 1,
        "cycle_days": 95,
        "lead_days": 7,
        "target_yuan": 99.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(海飞丝.*洗发水|洗发水|洗发露)",
        "exclude_re": r"(退款|理发|美发|阿迪达斯|华住|全季)",
        "default_evidence": "支付宝 2 笔精确复购（03-11 ¥99.9 → 06-15 ¥99.9），实测间隔 96 天",
        "notes": "06-15 后距今已超 160 天，校准余量后即可启动自动提醒",
    },
    {
        "item_id": "rpl_body_wash",
        "name": "沐浴露 / 三合一洗护",
        "category": "个护日用",
        "location": "家",
        "spec": "阿迪达斯男士三合一沐浴露洗发洗面套装",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "阿迪达斯男士三合一沐浴露",
        "sku": "",
        "qty": 1,
        "cycle_days": 85,
        "lead_days": 7,
        "target_yuan": 60.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(阿迪达斯.*沐浴|沐浴露)",
        "exclude_re": r"(退款|香皂|华住|全季)",
        "default_evidence": "支付宝 2 笔精确复购（03-23 ¥63.8 → 06-15 ¥59.9），实测间隔 84 天",
        "notes": "06-15 后距今已超 160 天，校准余量后开启自动提醒",
    },
    {
        "item_id": "rpl_hand_soap",
        "name": "自动洗手机补充液",
        "category": "个护日用",
        "location": "家",
        "spec": "米家自动洗手机泡沫抑菌补充液 320ml×3瓶",
        "channel": "meituan",
        "meituan_shop": "京东便利店",
        "meituan_query": "小米米家自动洗手机补充液",
        "sku": "",
        "qty": 1,
        "cycle_days": 120,
        "lead_days": 7,
        "target_yuan": 55.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(洗手机补充液|泡沫.*洗手液|洗手液.*替换)",
        "exclude_re": r"(起泡沫机|卡皮巴拉)",
        "default_evidence": "05-15 美团京东便利店购米家 320ml×3瓶，单瓶约用 40 天，3 瓶装约 120 天",
        "notes": "3 瓶整套周期较长，已满 190 天建议看一眼余量",
    },
    {
        "item_id": "rpl_toothpaste",
        "name": "牙膏",
        "category": "个护日用",
        "location": "家",
        "spec": "高露洁美白牙膏 120g",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "高露洁美白牙膏 120g",
        "sku": "",
        "qty": 1,
        "cycle_days": 60,
        "lead_days": 5,
        "target_yuan": 22.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(牙膏)",
        "exclude_re": r"^$",
        "default_evidence": "05-17 711 购高露洁 120g，日常早晚刷牙约 60 天/支",
        "notes": "顺手可与电动牙刷头一起校准",
    },
    {
        "item_id": "rpl_toothbrush_head",
        "name": "牙刷 / 电动牙刷头",
        "category": "个护日用",
        "location": "家",
        "spec": "牙刷 / 电动牙刷替换刷头（每季度换 1 支）",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "软毛牙刷",
        "sku": "",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 7,
        "target_yuan": 35.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(牙刷|刷头)",
        "exclude_re": r"(马桶刷)",
        "default_evidence": "口腔卫生标准周期：每 90 天更换一次刷头/牙刷",
        "notes": "如用飞利浦/徕芬等电动牙刷，可在塔台改填对应京东刷头 SKU",
    },
    {
        "item_id": "rpl_minoxidil",
        "name": "蔓迪米诺地尔泡沫剂",
        "category": "个护日用",
        "location": "家",
        "spec": "蔓迪二代米诺地尔生发泡沫剂（11-15 购入 ¥196）",
        "channel": "jd",
        "meituan_shop": "叮当快药",
        "meituan_query": "蔓迪米诺地尔泡沫剂",
        "sku": "",
        "qty": 1,
        "cycle_days": 60,
        "lead_days": 7,
        "target_yuan": 196.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(蔓迪|米诺地尔)",
        "exclude_re": r"^$",
        "default_evidence": "2026-11-15 淘宝官方旗舰店购入 ¥196，常规 1–2 瓶装约用 60 天",
        "notes": "如单瓶 30 天用完可在塔台改周期；支持补填京东 SKU 或改走美团叮当快药",
    },
    {
        "item_id": "rpl_washer_cleaner",
        "name": "洗衣机槽清洗剂",
        "category": "清洁洗护",
        "location": "家",
        "spec": "滴露滚筒/波轮洗衣机除菌清洗剂",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "滴露洗衣机清洗剂",
        "sku": "",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 7,
        "target_yuan": 24.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(洗衣机.*清洗剂|槽清洁)",
        "exclude_re": r"^$",
        "default_evidence": "2026-06-09 购滴露洗衣机清洗剂（¥23.54），常规建议每 90 天清一次筒槽",
        "notes": "新房双洗衣机到位后更常用",
    },
    {
        "item_id": "rpl_sink_mesh",
        "name": "厨房水槽过滤网",
        "category": "垃圾收纳",
        "location": "家",
        "spec": "一次性厨房水槽滤网 100只装",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "厨房水槽过滤网",
        "sku": "",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 7,
        "target_yuan": 18.0,
        "tier": "recommended",
        "enabled": True,
        "auto_buy": True,
        "match_re": r"(水槽过滤网|水槽滤网)",
        "exclude_re": r"(不锈钢)",
        "default_evidence": "2026-05-11 购入一次性厨房水槽过滤网（¥26.58），约 90 天补一次",
        "notes": "按实际下厨频率可调周期",
    },
    # ==================== 3. 12.10 新房家电入住后耗材（默认暂停，入住后一键开启） ====================
    {
        "item_id": "rpl_ecovacs_pack",
        "name": "科沃斯 T90S Pro 尘袋/清洁液",
        "category": "家电滤芯",
        "location": "家",
        "spec": "科沃斯 X12/T90S 基站配件包（尘袋+滤芯+清洁液）",
        "channel": "jd",
        "meituan_shop": "",
        "meituan_query": "",
        "sku": "100012345679",
        "qty": 1,
        "cycle_days": 90,
        "lead_days": 10,
        "target_yuan": 99.0,
        "tier": "movein",
        "enabled": False,
        "auto_buy": True,
        "match_re": r"(科沃斯.*配件包|科沃斯.*尘袋|科沃斯.*清洁液)",
        "exclude_re": r"^$",
        "default_evidence": "新房科沃斯 T90S Pro 上下水扫地机（京东 SKU 100012345679），入住示例小区B后启用",
        "notes": "默认暂停，2027 春季入住示例小区B后在塔台点启用即可",
    },
    {
        "item_id": "rpl_dishwasher_pods",
        "name": "洗碗机洗碗块 & 软水盐",
        "category": "清洁洗护",
        "location": "家",
        "spec": "亮碟洗碗凝珠 + 软水盐（美的 S67Pro 专用）",
        "channel": "meituan",
        "meituan_shop": "小象超市",
        "meituan_query": "亮碟洗碗凝珠",
        "sku": "",
        "qty": 1,
        "cycle_days": 60,
        "lead_days": 7,
        "target_yuan": 79.0,
        "tier": "movein",
        "enabled": False,
        "auto_buy": True,
        "match_re": r"(洗碗块|洗碗凝珠|亮碟|软水盐)",
        "exclude_re": r"^$",
        "default_evidence": "新房美的 S67Pro 洗碗机常备耗材，入住示例小区B后启用",
        "notes": "默认暂停，入住新房后启用",
    },
    {
        "item_id": "rpl_water_purifier_filter",
        "name": "厨下净水器滤芯",
        "category": "家电滤芯",
        "location": "家",
        "spec": "小米厨下反渗透净水器 PPC / RO 滤芯",
        "channel": "jd",
        "meituan_shop": "",
        "meituan_query": "",
        "sku": "",
        "qty": 1,
        "cycle_days": 365,
        "lead_days": 15,
        "target_yuan": 199.0,
        "tier": "movein",
        "enabled": False,
        "auto_buy": True,
        "match_re": r"(净水器.*滤芯|RO滤芯|PPC滤芯)",
        "exclude_re": r"^$",
        "default_evidence": "新房厨下净水器年度换芯，入住启用并填对应滤芯 SKU",
        "notes": "默认暂停，入住新房后启用",
    },
)

_CATALOG_BY_ID: dict[str, dict[str, Any]] = {str(x["item_id"]): dict(x) for x in DEFAULT_DAILY_CATALOG}


def _now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return _db_mod.now_local()
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def _extract_jd_sku(sku_or_url: str) -> str:
    s = (sku_or_url or "").strip()
    m_url = re.search(r"item\.jd\.com/(\d{6,18})", s)
    if m_url:
        return m_url.group(1)
    m_num = re.fullmatch(r"\d{6,18}", s)
    return m_num.group(0) if m_num else ""


def _make_item_id(name: str, sku: str = "", location: str = "家") -> str:
    base = re.sub(r"[^a-z0-9]+", "_", f"{name}_{location}_{sku}".lower()).strip("_")
    if base and len(base) >= 3:
        return f"rpl_{base[:24]}"
    digest = hashlib.sha1(f"{name}|{location}|{sku}".encode("utf-8")).hexdigest()[:10]
    return f"rpl_{digest}"


def _normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """将旧版或精简字段标准化为完整日用品补货条目（100% 向后兼容旧数据）。"""
    d = dict(raw)
    name = str(d.get("name") or "").strip()[:48]
    sku = _extract_jd_sku(str(d.get("sku") or ""))
    loc = str(d.get("location") or "家").strip()
    if loc not in VALID_LOCATIONS:
        loc = "公司" if "公司" in loc or "工位" in loc else "家"

    ch = str(d.get("channel") or "").strip().lower()
    if ch not in VALID_CHANNELS:
        ch = "jd" if sku and not d.get("meituan_query") else "meituan"

    item_id = str(d.get("item_id") or "").strip()
    if not item_id:
        item_id = _make_item_id(name or sku or "item", sku=sku, location=loc)

    try:
        cyc = max(3, min(730, int(d.get("cycle_days") or 30)))
    except (TypeError, ValueError):
        cyc = 30
    try:
        lead = max(1, min(30, int(d.get("lead_days") if d.get("lead_days") is not None else min(7, max(2, cyc // 8)))))
    except (TypeError, ValueError):
        lead = 5
    try:
        qty = max(1, min(99, int(d.get("qty") or 1)))
    except (TypeError, ValueError):
        qty = 1

    target_y = d.get("target_yuan")
    try:
        target_y_f = round(float(target_y), 2) if target_y not in (None, "") else None
    except (TypeError, ValueError):
        target_y_f = None

    last_b = str(d.get("last_bought") or "").strip()[:10]
    next_d = str(d.get("next_due") or "").strip()[:10]
    if last_b and not next_d:
        try:
            next_d = (date.fromisoformat(last_b) + timedelta(days=cyc)).isoformat()
        except ValueError:
            pass
    elif not next_d:
        next_d = (_now().date() + timedelta(days=cyc)).isoformat()

    tier = str(d.get("tier") or "core").strip().lower()
    if tier not in VALID_TIERS:
        tier = "core"

    return {
        "item_id": item_id,
        "name": name or f"商品({sku or item_id})",
        "category": str(d.get("category") or "日用消耗").strip()[:24],
        "location": loc,
        "spec": str(d.get("spec") or "").strip()[:120],
        "channel": ch,
        "meituan_shop": str(d.get("meituan_shop") or "").strip()[:48],
        "meituan_query": str(d.get("meituan_query") or (name if ch == "meituan" else "")).strip()[:80],
        "sku": sku,
        "qty": qty,
        "cycle_days": cyc,
        "lead_days": lead,
        "target_yuan": target_y_f,
        "last_bought": last_b,
        "next_due": next_d,
        "enabled": bool(d.get("enabled", True)),
        "auto_buy": bool(d["auto_buy"]) if "auto_buy" in d and d["auto_buy"] is not None else (ch in ("meituan", "jd")),
        "tier": tier,
        "evidence": str(d.get("evidence") or "").strip()[:260],
        "evidence_count": int(d.get("evidence_count") or 0),
        "confidence": str(d.get("confidence") or ("high" if int(d.get("evidence_count") or 0) >= 2 else "estimate")),
        "needs_calibration": bool(d.get("needs_calibration", False)),
        "last_reminded_at": str(d.get("last_reminded_at") or "").strip(),
        "snoozed_until": str(d.get("snoozed_until") or "").strip()[:10],
        "notes": str(d.get("notes") or "").strip()[:220],
        "history_samples": list(d.get("history_samples") or [])[:8],
        "updated_at": str(d.get("updated_at") or iso_now()),
    }


def _load_replenish_items(db: SimingDB) -> list[dict[str, Any]]:
    raw = db.get_meta(REPLENISH_ITEMS_META_KEY) or "[]"
    try:
        arr = json.loads(raw)
        if not isinstance(arr, list):
            return []
        return [_normalize_item(x) for x in arr if isinstance(x, dict)]
    except Exception:
        return []


def _save_replenish_items(db: SimingDB, items: list[dict[str, Any]]) -> None:
    normed = [_normalize_item(x) for x in items]
    db.set_meta(REPLENISH_ITEMS_META_KEY, json.dumps(normed, ensure_ascii=False))


def _find_item(items: list[dict[str, Any]], query: str) -> Optional[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return None
    ql = q.lower()
    sku_q = _extract_jd_sku(q)
    # 1. Exact item_id / sku / exact name
    for it in items:
        if it["item_id"].lower() == ql:
            return it
        if sku_q and it["sku"] == sku_q:
            return it
        if it["name"].lower() == ql:
            return it
    # 2. Substring match on name (preferring longer/closer match)
    cands = [it for it in items if ql in it["name"].lower() or it["name"].lower() in ql]
    if cands:
        cands.sort(key=lambda x: abs(len(x["name"]) - len(q)))
        return cands[0]
    return None


def _load_approval_map(db: SimingDB) -> dict[str, Any]:
    raw = db.get_meta(REPLENISH_APPROVAL_MAP_KEY) or "{}"
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return {}
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, list):
                out[str(k).upper()] = [str(x) for x in v if x]
            elif v:
                out[str(k).upper()] = str(v)
        return out
    except Exception:
        return {}


def _record_approval_item_link(db: SimingDB, token: str, item_id: Any) -> None:
    tok = (token or "").strip().upper()
    if not tok or not item_id:
        return
    if isinstance(item_id, list):
        val: Any = [str(x).strip() for x in item_id if str(x).strip()]
        if not val:
            return
    else:
        val = str(item_id).strip()
        if not val:
            return
    m = _load_approval_map(db)
    m[tok] = val
    # Keep most recent 60 entries
    if len(m) > 60:
        for old_k in list(m.keys())[:-60]:
            m.pop(old_k, None)
    db.set_meta(REPLENISH_APPROVAL_MAP_KEY, json.dumps(m, ensure_ascii=False))


def on_approval_applied_for_replenish(db: SimingDB, token: str, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """审批口令（如 meituan_submit_pay / jd_add_cart）成功执行后，若关联了日用品补货项（含合单多品），自动刷新周期起点。"""
    tok = (token or "").strip().upper()
    if not tok:
        return None
    m = _load_approval_map(db)
    linked = m.pop(tok, None)
    if not linked:
        return None
    db.set_meta(REPLENISH_APPROVAL_MAP_KEY, json.dumps(m, ensure_ascii=False))
    bought_iso = _now(now).date().isoformat()
    if isinstance(linked, list):
        last_res: Optional[dict[str, Any]] = None
        for iid in linked:
            last_res = mark_replenish_bought(db, str(iid), bought_date=bought_iso, now=now)
        return last_res
    return mark_replenish_bought(db, str(linked), bought_date=bought_iso, now=now)


# ---------------------------------------------------------------------------
# 1. 消费记录周期推算与自动同步
# ---------------------------------------------------------------------------


def _query_history_records(db: SimingDB) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conn = db.connect()
    try:
        soi = [
            dict(r)
            for r in conn.execute(
                """
                SELECT platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty
                FROM shop_order_items
                WHERE COALESCE(order_status, '') NOT LIKE '%退%'
                  AND COALESCE(order_status, '') NOT LIKE '%取消%'
                ORDER BY ordered_at ASC
                """
            ).fetchall()
        ]
        txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT occurred_at, amount_cents, merchant, notes
                FROM ledger_txns
                WHERE kind = '消费'
                  AND COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
                ORDER BY occurred_at ASC
                """
            ).fetchall()
        ]
    except Exception:
        return [], []
    finally:
        conn.close()
    return soi, txns


def _match_history_for_spec(
    spec: dict[str, Any],
    soi: list[dict[str, Any]],
    txns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inc_s = str(spec.get("match_re") or "")
    if not inc_s:
        return []
    inc = re.compile(inc_s, re.IGNORECASE)
    exc_s = str(spec.get("exclude_re") or "")
    exc = re.compile(exc_s, re.IGNORECASE) if exc_s else None
    daytime_only = bool(spec.get("daytime_only", False))

    hits: list[dict[str, Any]] = []
    seen_day_sig: set[tuple[str, str]] = set()

    for r in soi:
        name = str(r.get("item_name") or "")
        if not inc.search(name) or (exc and exc.search(name)):
            continue
        at_full = str(r.get("ordered_at") or "")
        day_s = at_full[:10]
        if daytime_only:
            try:
                dt_obj = parse_iso(at_full)
                if dt_obj.weekday() >= 5 or not (9 <= dt_obj.hour <= 15):
                    continue
            except Exception:
                pass
        sig = (day_s, name[:24])
        if sig in seen_day_sig:
            continue
        seen_day_sig.add(sig)
        hits.append(
            {
                "date": day_s,
                "at": at_full[:16].replace("T", " "),
                "source": f"美团·{r.get('shop') or ''}",
                "item": name[:64],
                "qty": int(r.get("qty") or 1),
                "yuan": round(int(r.get("order_amount_cents") or 0) / 100.0, 2),
            }
        )

    for r in txns:
        merch = str(r.get("merchant") or "")
        notes = str(r.get("notes") or "")
        hay = f"{merch} {notes}".strip()
        if not inc.search(hay) or (exc and exc.search(hay)):
            continue
        day_s = str(r.get("occurred_at") or "")[:10]
        if any(h["date"] == day_s for h in hits):
            continue
        if daytime_only:
            try:
                dt_obj = parse_iso(str(r.get("occurred_at") or ""))
                if dt_obj.weekday() >= 5 or not (9 <= dt_obj.hour <= 15):
                    continue
            except Exception:
                pass
        src_lbl = merch.split(" · ", 1)[0][:18] or "账本"
        hits.append(
            {
                "date": day_s,
                "at": str(r.get("occurred_at") or "")[:16].replace("T", " "),
                "source": src_lbl,
                "item": merch[:64],
                "qty": 1,
                "yuan": round(int(r.get("amount_cents") or 0) / 100.0, 2),
            }
        )

    hits.sort(key=lambda x: x["date"])
    return hits


def _infer_cycle_from_hits(spec: dict[str, Any], hits: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """结合历史消费命中记录推算周期、最近购买日、置信度与是否需要人工校准。"""
    base_cyc = int(spec.get("cycle_days") or 30)
    iid = str(spec.get("item_id") or "")

    if not hits:
        return {
            "cycle_days": base_cyc,
            "last_bought": "",
            "next_due": (today + timedelta(days=base_cyc)).isoformat(),
            "evidence": str(spec.get("default_evidence") or "暂无单品消费记录，按日常经验周期预设"),
            "evidence_count": 0,
            "confidence": "estimate",
            "needs_calibration": True,
            "history_samples": [],
        }

    # 按独立日期去重计算真实间隔
    uniq_dates: list[date] = []
    for h in hits:
        try:
            d = date.fromisoformat(h["date"])
            if not uniq_dates or d != uniq_dates[-1]:
                uniq_dates.append(d)
        except ValueError:
            continue

    last_d = uniq_dates[-1]
    gaps = [(uniq_dates[i] - uniq_dates[i - 1]).days for i in range(1, len(uniq_dates)) if (uniq_dates[i] - uniq_dates[i - 1]).days >= 5]

    inferred_cyc = base_cyc
    if iid == "rpl_dettol":
        # 滴露按规格自适应：1.8L 约 95 天，750ml 约 45 天
        last_item = str(hits[-1].get("item") or "")
        inferred_cyc = 95 if "1.8" in last_item else 45
    elif iid == "rpl_alcohol_pad":
        last_qty = int(hits[-1].get("qty") or 1)
        inferred_cyc = min(240, max(60, 90 * last_qty))
    elif iid == "rpl_needle":
        last_qty = int(hits[-1].get("qty") or 1)
        inferred_cyc = min(140, max(35, 49 * last_qty))
    elif len(gaps) >= 2:
        sg = sorted(gaps)
        mid = len(sg) // 2
        med = (sg[mid - 1] + sg[mid]) // 2 if len(sg) % 2 == 0 else sg[mid]
        # 若中位值合理（在默认周期 0.4x–2.2x 之间），直接采纳实测中位数
        if max(7, int(base_cyc * 0.4)) <= med <= min(365, int(base_cyc * 2.2)):
            inferred_cyc = int(med)
    elif len(gaps) == 1:
        g0 = gaps[0]
        if max(7, int(base_cyc * 0.5)) <= g0 <= min(365, int(base_cyc * 1.8)):
            inferred_cyc = int(g0)

    next_d = last_d + timedelta(days=inferred_cyc)
    days_since = (today - last_d).days
    # 若最近一笔可见消费已超周期 + 15 天（很可能期间在线下山姆/无明细大单补过），标为待校准防首轮误轰炸
    needs_cal = days_since > inferred_cyc + 15

    # 动态生成可读推算依据
    dates_brief = " → ".join(d.strftime("%m-%d") for d in uniq_dates[-4:])
    if gaps:
        gap_s = "/".join(str(g) for g in gaps[-4:])
        ev = f"命中 {len(hits)} 笔消费（{dates_brief}，间隔 {gap_s} 天）· 推算周期 {inferred_cyc} 天"
        if spec.get("default_evidence") and len(hits) <= 2:
            ev = str(spec["default_evidence"])
        conf = "high" if len(uniq_dates) >= 2 else "medium"
    else:
        ev = str(spec.get("default_evidence") or f"命中 1 笔消费（{last_d.isoformat()}）· 按规格推算约 {inferred_cyc} 天")
        conf = "medium"

    return {
        "cycle_days": inferred_cyc,
        "last_bought": last_d.isoformat(),
        "next_due": next_d.isoformat(),
        "evidence": ev,
        "evidence_count": len(hits),
        "confidence": conf,
        "needs_calibration": needs_cal,
        "history_samples": hits[-6:],
    }


def seed_daily_replenish_catalog(
    db: SimingDB,
    *,
    overwrite: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """根据用户真实消费记录初始化/刷新日用品周期补货清单（默认不覆盖用户已在塔台手动校准的条目）。"""
    db.init_db()
    cur = _now(now)
    today = cur.date()
    soi, txns = _query_history_records(db)
    existing = _load_replenish_items(db)
    by_id = {x["item_id"]: x for x in existing}
    by_name = {x["name"]: x for x in existing}

    added = 0
    updated = 0
    kept = 0
    out_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for spec in DEFAULT_DAILY_CATALOG:
        iid = str(spec["item_id"])
        hits = _match_history_for_spec(spec, soi, txns)
        inf = _infer_cycle_from_hits(spec, hits, today)

        prev = by_id.get(iid) or by_name.get(str(spec["name"]))
        if prev and not overwrite:
            # 保留用户手动改过的周期/渠道/SKU/启用状态，仅在发现更新的真实消费记录时推进 last_bought 并刷新历史依据
            merged = dict(prev)
            merged["item_id"] = iid
            merged["evidence"] = inf["evidence"]
            merged["evidence_count"] = inf["evidence_count"]
            merged["confidence"] = inf["confidence"]
            merged["history_samples"] = inf["history_samples"]
            if inf["last_bought"] and (not merged["last_bought"] or inf["last_bought"] > merged["last_bought"]):
                merged["last_bought"] = inf["last_bought"]
                try:
                    merged["next_due"] = (
                        date.fromisoformat(inf["last_bought"]) + timedelta(days=int(merged["cycle_days"]))
                    ).isoformat()
                except ValueError:
                    pass
                merged["needs_calibration"] = False
                merged["snoozed_until"] = ""
                merged["updated_at"] = iso_now(cur)
                updated += 1
            else:
                kept += 1
            out_items.append(_normalize_item(merged))
            seen_ids.add(iid)
            continue

        entry = _normalize_item(
            {
                **spec,
                "cycle_days": inf["cycle_days"],
                "last_bought": inf["last_bought"],
                "next_due": inf["next_due"],
                "evidence": inf["evidence"],
                "evidence_count": inf["evidence_count"],
                "confidence": inf["confidence"],
                "needs_calibration": inf["needs_calibration"] if spec.get("enabled", True) else False,
                "history_samples": inf["history_samples"],
                "updated_at": iso_now(cur),
            }
        )
        # 若针头有 daily_care 手动校准余量，精确同步其 next_due！
        if iid == "rpl_needle":
            try:
                from siming.daily_care import estimate_supplies

                sup = next((s for s in estimate_supplies(db, cur) if s.get("key") == "needle" and s.get("run_out")), None)
                if sup and sup.get("run_out"):
                    entry["next_due"] = str(sup["run_out"])
                    entry["needs_calibration"] = False
            except Exception:
                pass
        elif iid == "rpl_mask":
            try:
                from siming.daily_care import estimate_supplies

                sup = next((s for s in estimate_supplies(db, cur) if s.get("key") == "mask" and s.get("run_out")), None)
                if sup and sup.get("run_out"):
                    entry["next_due"] = str(sup["run_out"])
                    entry["needs_calibration"] = False
            except Exception:
                pass

        out_items.append(entry)
        seen_ids.add(iid)
        if prev:
            updated += 1
        else:
            added += 1

    # 保留用户自行添加的非内置条目
    for prev in existing:
        if prev["item_id"] not in seen_ids and prev["name"] not in {x["name"] for x in out_items}:
            out_items.append(prev)
            kept += 1

    _save_replenish_items(db, out_items)
    return {
        "ok": True,
        "total": len(out_items),
        "added": added,
        "updated": updated,
        "kept": kept,
        "items": out_items,
    }


def sync_replenish_from_recent_orders(db: SimingDB, now: Optional[datetime] = None) -> dict[str, Any]:
    """轻量增量同步：若 `shop_order_items` / `ledger_txns` 出现比当前 `last_bought` 更新的消费记录，自动推进周期避免重复提醒。"""
    items = _load_replenish_items(db)
    if not items:
        return {"ok": True, "advanced": []}
    cur = _now(now)
    soi, txns = _query_history_records(db)
    if not soi and not txns:
        return {"ok": True, "advanced": []}

    advanced: list[str] = []
    changed = False
    for it in items:
        spec = _CATALOG_BY_ID.get(it["item_id"])
        if not spec:
            continue
        hits = _match_history_for_spec(spec, soi, txns)
        if not hits:
            continue
        newest_d = hits[-1]["date"]
        if newest_d and (not it["last_bought"] or newest_d > it["last_bought"]):
            it["last_bought"] = newest_d
            try:
                it["next_due"] = (date.fromisoformat(newest_d) + timedelta(days=int(it["cycle_days"]))).isoformat()
            except ValueError:
                pass
            it["needs_calibration"] = False
            it["snoozed_until"] = ""
            it["last_reminded_at"] = ""
            it["history_samples"] = hits[-6:]
            it["evidence_count"] = len(hits)
            it["updated_at"] = iso_now(cur)
            advanced.append(it["item_id"])
            changed = True

    if changed:
        _save_replenish_items(db, items)
    return {"ok": True, "advanced": advanced}


# ---------------------------------------------------------------------------
# 2. CRUD & 状态操作（向后兼容旧签名 + 支持全字段塔台管理）
# ---------------------------------------------------------------------------


def upsert_replenish_item(
    db: SimingDB,
    name: str,
    sku_or_url: str = "",
    cycle_days: int = 30,
    target_yuan: Optional[float] = None,
    qty: int = 1,
    last_bought_date: str = "",
    *,
    item_id: str = "",
    category: str = "",
    location: str = "",
    spec: str = "",
    channel: str = "",
    meituan_shop: str = "",
    meituan_query: str = "",
    lead_days: Optional[int] = None,
    enabled: Optional[bool] = None,
    auto_buy: Optional[bool] = None,
    tier: str = "",
    notes: str = "",
    needs_calibration: Optional[bool] = None,
) -> dict[str, Any]:
    db.init_db()
    clean_name = (name or "").strip()[:48]
    if not clean_name:
        return {"ok": False, "error": "商品名称不能为空"}

    raw_sku_arg = (sku_or_url or "").strip()
    sku = _extract_jd_sku(raw_sku_arg)
    eff_channel = (channel or "").strip().lower()
    if not eff_channel:
        eff_channel = "jd" if raw_sku_arg else "meituan"
    if eff_channel not in VALID_CHANNELS:
        return {
            "ok": False,
            "error": f"不支持的购买渠道：{eff_channel}（可选 {' / '.join(VALID_CHANNELS)}）",
        }

    # 兼容原有严格校验：若走京东渠道且传了非空链接/SKU 但格式非法，直接报错
    if raw_sku_arg and not sku and eff_channel == "jd":
        return {"ok": False, "error": f"请提供有效的京东商品链接或数字 SKU（收到：{sku_or_url}）"}
    if not raw_sku_arg and eff_channel == "jd" and not item_id and not meituan_query and not spec:
        return {"ok": False, "error": f"请提供有效的京东商品链接或数字 SKU（收到：{sku_or_url}）"}

    cyc = max(3, min(730, int(cycle_days)))
    today_d = _now().date()
    items = _load_replenish_items(db)

    prev: Optional[dict[str, Any]] = None
    if item_id:
        prev = next((x for x in items if x["item_id"] == item_id), None)
    if prev is None and sku:
        prev = next((x for x in items if x["sku"] == sku), None)
    if prev is None:
        prev = next((x for x in items if x["name"] == clean_name), None)

    bought_d = (last_bought_date or (prev["last_bought"] if prev else "") or today_d.isoformat()).strip()
    try:
        d_obj = date.fromisoformat(bought_d)
    except ValueError:
        d_obj = today_d
        bought_d = d_obj.isoformat()

    due_d = (d_obj + timedelta(days=cyc)).isoformat()
    eff_loc = (location or (prev["location"] if prev else "家")).strip()
    if eff_loc not in VALID_LOCATIONS:
        eff_loc = "公司" if "公司" in eff_loc else "家"

    eff_id = (item_id or (prev["item_id"] if prev else "") or _make_item_id(clean_name, sku=sku, location=eff_loc)).strip()
    base = dict(prev) if prev else {}
    entry = _normalize_item(
        {
            **base,
            "item_id": eff_id,
            "name": clean_name,
            "category": category or base.get("category") or "日用消耗",
            "location": eff_loc,
            "spec": spec if spec != "" else base.get("spec", ""),
            "channel": eff_channel,
            "meituan_shop": meituan_shop if meituan_shop != "" else (base.get("meituan_shop", "") if eff_channel == "meituan" else ""),
            "meituan_query": meituan_query if meituan_query != "" else ((base.get("meituan_query") or clean_name) if eff_channel == "meituan" else ""),
            "sku": sku if raw_sku_arg else base.get("sku", ""),
            "cycle_days": cyc,
            "lead_days": lead_days if lead_days is not None else base.get("lead_days", min(7, max(2, cyc // 8))),
            "qty": max(1, int(qty)),
            "target_yuan": round(float(target_yuan), 2) if target_yuan is not None else base.get("target_yuan"),
            "last_bought": bought_d,
            "next_due": due_d,
            "enabled": enabled if enabled is not None else base.get("enabled", True),
            "auto_buy": auto_buy if auto_buy is not None else (eff_channel in ("meituan", "jd")),
            "tier": tier or base.get("tier") or "core",
            "notes": notes if notes != "" else base.get("notes", ""),
            "needs_calibration": needs_calibration if needs_calibration is not None else False,
            "updated_at": iso_now(),
        }
    )

    kept = [
        x
        for x in items
        if x["item_id"] != eff_id
        and (not entry["sku"] or x["sku"] != entry["sku"])
        and x["name"] != clean_name
    ]
    kept.append(entry)
    _save_replenish_items(db, kept)
    db.record_human_use("replenish_cart", "upsert", ref_id=entry["sku"] or eff_id)

    t_str = f" · 参考价 ¥{entry['target_yuan']:.0f}" if entry["target_yuan"] else ""
    ch_short = (CHANNEL_META.get(entry["channel"]) or {}).get("short") or entry["channel"]
    sku_part = f"SKU `{entry['sku']}` · " if entry["sku"] else f"{ch_short} · "
    return {
        "ok": True,
        "entry": entry,
        "summary": (
            f"🛒 已加入常买周期清单：**{clean_name}**（{sku_part}{entry['location']} · 每 {cyc} 天补货 · 下次预计 `{due_d}`{t_str}）。"
            f"需要补货时发 `补货 {clean_name}` 或 `已买 {clean_name}` 即可！"
        ),
    }


def remove_replenish_item(db: SimingDB, target: str) -> dict[str, Any]:
    q = (target or "").strip().lower()
    items = _load_replenish_items(db)
    kept = [
        x
        for x in items
        if x["item_id"].lower() != q and q not in f"{x.get('sku', '')} {x.get('name', '')}".lower()
    ]
    if len(kept) == len(items):
        return {"ok": False, "error": f"常买清单中未找到「{target}」"}
    _save_replenish_items(db, kept)
    db.record_human_use("replenish_cart", "remove", ref_id=target[:40])
    return {"ok": True, "summary": f"🗑️ 已从常买周期清单移除匹配「{target}」的条目。"}


def mark_replenish_bought(
    db: SimingDB,
    target: str,
    bought_date: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """登记某项日用品刚买过：重置 last_bought，按周期推进 next_due，并清除待校准/暂缓状态。"""
    db.init_db()
    cur = _now(now)
    items = _load_replenish_items(db)
    it = _find_item(items, target)
    if not it:
        return {"ok": False, "error": f"常买清单中未找到「{target}」"}

    b_str = (bought_date or cur.date().isoformat()).strip()[:10]
    try:
        b_d = date.fromisoformat(b_str)
    except ValueError:
        b_d = cur.date()
        b_str = b_d.isoformat()

    nxt_d = (b_d + timedelta(days=int(it["cycle_days"]))).isoformat()
    it["last_bought"] = b_str
    it["next_due"] = nxt_d
    it["needs_calibration"] = False
    it["snoozed_until"] = ""
    it["last_reminded_at"] = ""
    it["updated_at"] = iso_now(cur)
    _save_replenish_items(db, items)
    db.record_human_use("replenish_cart", "mark_bought", ref_id=it["item_id"])
    return {
        "ok": True,
        "item": it,
        "summary": f"✅ 已登记 **{it['name']}**（{it['location']}）于 `{b_str}` 补货，下次预计补货日已推进至 `{nxt_d}`（周期 {it['cycle_days']} 天）。",
    }


def snooze_replenish_item(
    db: SimingDB,
    target: str,
    days: int = 14,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """家里还有余量：顺延补货日 +days 天并清除待校准。"""
    db.init_db()
    cur = _now(now)
    today = cur.date()
    items = _load_replenish_items(db)
    it = _find_item(items, target)
    if not it:
        return {"ok": False, "error": f"常买清单中未找到「{target}」"}

    add_d = max(1, min(180, int(days)))
    try:
        cur_due = date.fromisoformat(str(it["next_due"]))
    except ValueError:
        cur_due = today
    base_d = max(today, cur_due)
    new_due = (base_d + timedelta(days=add_d)).isoformat()
    it["next_due"] = new_due
    it["snoozed_until"] = (today + timedelta(days=add_d)).isoformat()
    it["needs_calibration"] = False
    it["last_reminded_at"] = ""
    it["updated_at"] = iso_now(cur)
    _save_replenish_items(db, items)
    db.record_human_use("replenish_cart", "snooze", ref_id=it["item_id"])
    return {
        "ok": True,
        "item": it,
        "summary": f"⏳ 收到，**{it['name']}** 补货提醒已延后 {add_d} 天，下次预计 `{new_due}` 再提醒。",
    }


def toggle_replenish_item(
    db: SimingDB,
    target: str,
    enabled: bool,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """启用或暂停某项日用品的周期补货提醒。"""
    db.init_db()
    cur = _now(now)
    items = _load_replenish_items(db)
    it = _find_item(items, target)
    if not it:
        return {"ok": False, "error": f"常买清单中未找到「{target}」"}

    it["enabled"] = bool(enabled)
    it["updated_at"] = iso_now(cur)
    _save_replenish_items(db, items)
    db.record_human_use("replenish_cart", "toggle", ref_id=it["item_id"])
    state_s = "已开启周期提醒" if it["enabled"] else "已暂停提醒（随时可在塔台恢复）"
    return {
        "ok": True,
        "item": it,
        "summary": f"🎛️ **{it['name']}** {state_s}。",
    }


# ---------------------------------------------------------------------------
# 3. 状态计算、报告与控制塔 Payload
# ---------------------------------------------------------------------------


def _enrich_item_status(it: dict[str, Any], today: date) -> dict[str, Any]:
    row = dict(it)
    cyc = max(1, int(row.get("cycle_days") or 30))
    lead = max(1, int(row.get("lead_days") or 5))
    try:
        due_d = date.fromisoformat(str(row.get("next_due") or ""))
        days_left = (due_d - today).days
    except ValueError:
        days_left = cyc

    if not row.get("enabled", True):
        status = "paused"
        status_label = "已暂停"
    elif row.get("needs_calibration"):
        status = "calibrate"
        status_label = "待校准"
    elif days_left <= 0:
        status = "due"
        status_label = "已到期"
    elif days_left <= lead:
        status = "soon"
        status_label = "临期补货"
    else:
        status = "ok"
        status_label = "余量充裕"

    rem_ratio = max(0.0, min(1.0, days_left / float(cyc))) if status not in ("paused", "calibrate") else 0.0
    monthly_est = round((float(row["target_yuan"]) * int(row.get("qty") or 1)) * 30.0 / cyc, 2) if row.get("target_yuan") else 0.0
    ch = str(row.get("channel") or "meituan")
    row.update(
        {
            "days_left": days_left,
            "status": status,
            "status_label": status_label,
            "remaining_pct": round(rem_ratio * 100.0, 1),
            "monthly_est_yuan": monthly_est,
            "channel_label": (CHANNEL_META.get(ch) or {}).get("label") or ch,
            "ready_to_buy": bool(
                (ch == "meituan" and row.get("meituan_query"))
                or (ch == "jd" and row.get("sku"))
            ),
        }
    )
    return row


def build_replenish_tower_payload(db: SimingDB, now: Optional[datetime] = None) -> dict[str, Any]:
    """为司命统一网页控制塔构建日用品周期补货全景数据。"""
    db.init_db()
    cur = _now(now)
    today = cur.date()
    raw_items = _load_replenish_items(db)
    enriched = [_enrich_item_status(x, today) for x in raw_items]

    status_rank = {"due": 0, "soon": 1, "calibrate": 2, "ok": 3, "paused": 4}
    tier_rank = {"core": 0, "recommended": 1, "movein": 2}
    enriched.sort(key=lambda x: (status_rank.get(x["status"], 9), tier_rank.get(x["tier"], 9), int(x["days_left"])))

    due_cnt = sum(1 for x in enriched if x["status"] in ("due", "soon"))
    overdue_cnt = sum(1 for x in enriched if x["status"] == "due")
    cal_cnt = sum(1 for x in enriched if x["status"] == "calibrate")
    enabled_cnt = sum(1 for x in enriched if x["enabled"])
    core_cnt = sum(1 for x in enriched if x["tier"] == "core")
    monthly_total = round(sum(float(x["monthly_est_yuan"]) for x in enriched if x["enabled"]), 2)

    care_supplies: list[dict[str, Any]] = []
    try:
        from siming.daily_care import estimate_supplies

        care_supplies = estimate_supplies(db, cur)
    except Exception:
        care_supplies = []

    from siming.modules import is_module_enabled

    return {
        "ok": True,
        "generated_at": iso_now(cur),
        "module_enabled": is_module_enabled(db, "replenish_cart"),
        "summary": {
            "total": len(enriched),
            "enabled_count": enabled_cnt,
            "paused_count": len(enriched) - enabled_cnt,
            "core_count": core_cnt,
            "due_count": due_cnt,
            "overdue_count": overdue_cnt,
            "calibrate_count": cal_cnt,
            "monthly_est_yuan": monthly_total,
            "home_count": sum(1 for x in enriched if x["location"] == "家"),
            "office_count": sum(1 for x in enriched if x["location"] == "公司"),
        },
        "items": enriched,
        "daily_care_supplies": care_supplies,
    }


def format_replenish_report(db: SimingDB, now: Optional[datetime] = None) -> str:
    items = _load_replenish_items(db)
    today = _now(now).date()
    if not items:
        return (
            "🛒 **家庭常买消耗品周期清单**（当前为空）\n"
            "• 添加示例：`常买 添加 抽纸整箱 https://item.jd.com/100012345678.html 45`（名称 + 京东链接/SKU + 消耗天数）\n"
            "• 直接代加购物车：`加购物车 100012345678`（后台通过 9222 填入跨端同步京东购物车，手机一点即付）"
        )
    enriched = [_enrich_item_status(x, today) for x in items]
    status_rank = {"due": 0, "soon": 1, "calibrate": 2, "ok": 3, "paused": 4}
    enriched.sort(key=lambda x: (status_rank.get(x["status"], 9), int(x["days_left"])))

    due_cnt = sum(1 for x in enriched if x["status"] in ("due", "soon"))
    cal_cnt = sum(1 for x in enriched if x["status"] == "calibrate")
    lines = [
        f"🛒 **日用品周期补货雷达**（共 {len(enriched)} 项 · 🔴待补 {due_cnt} · 🟡待校准 {cal_cnt}）"
    ]
    for it in enriched:
        if it["status"] == "paused":
            badge = "⚪ 暂停"
        elif it["status"] == "due":
            badge = "🔴 已到期"
        elif it["status"] == "soon":
            badge = "🟠 临期"
        elif it["status"] == "calibrate":
            badge = "🟡 待校准"
        else:
            badge = "🟢 充裕"
        loc_s = f"@{it['location']}"
        ch_code = str(it.get("channel") or "meituan")
        if ch_code == "meituan":
            ch_s = "美团直买"
        elif ch_code == "jd":
            ch_s = f"京东 SKU `{it['sku']}`" if it["sku"] else "京东(待填SKU)"
        else:
            ch_s = (CHANNEL_META.get(ch_code) or {}).get("short") or ch_code
        price_s = f" · 约¥{it['target_yuan']:.0f}" if it.get("target_yuan") else ""
        if it["status"] == "calibrate":
            tail = f"上次 `{it['last_bought'] or '无记录'}` · 周期 {it['cycle_days']} 天（回 `已买 {it['name']}` 校准）"
        else:
            tail = f"预计 `{it['next_due']}`（余 {it['days_left']} 天 · 周期 {it['cycle_days']} 天）"
        lines.append(f"• {badge} **{it['name']}**（{loc_s} · {ch_s} ×{it['qty']}{price_s}）— {tail}")
    lines.append("👉 立即补货：`补货 <名称>` ｜ 已自购重置：`已买 <名称>` ｜ 京东加车：`加购物车 <名称或SKU>`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. 到期 QQ 弹确认卡 + 一键直接买执行器
# ---------------------------------------------------------------------------


def build_replenish_reminder_keyboard(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """为日用品到期提醒构造 QQ 原生确认按钮（点「确认直接买」即走美团实时核价下单或京东加车）。"""
    from siming.qq_cards import make_button, make_keyboard

    iid = str(item.get("item_id") or "").strip()
    if not iid:
        return None
    gid = f"sm_rpl_{iid[:18]}"
    ch = str(item.get("channel") or "meituan")
    if ch not in ("meituan", "jd"):
        return make_keyboard(
            [
                [
                    make_button(
                        "bought",
                        "✅ 我已买过",
                        f"siming:replenish_bought:{iid}",
                        visited_label="已重置周期",
                        style=1,
                        group_id=gid,
                    ),
                    make_button(
                        "snooze",
                        "⏳ 还有余量 +14天",
                        f"siming:replenish_snooze:{iid}:14",
                        visited_label="已延后14天",
                        style=0,
                        group_id=gid,
                    ),
                ],
                [
                    make_button(
                        "pause",
                        "⏸️ 暂不提醒",
                        f"siming:replenish_pause:{iid}",
                        visited_label="已暂停提醒",
                        style=0,
                        group_id=gid,
                    ),
                ],
            ]
        )
    buy_label = "🛒 确认直接买" if ch == "meituan" else "🛒 确认加京东车"
    return make_keyboard(
        [
            [
                make_button(
                    "buy",
                    buy_label,
                    f"siming:replenish_buy:{iid}",
                    visited_label="正在备单…",
                    style=1,
                    group_id=gid,
                ),
                make_button(
                    "bought",
                    "✅ 我已买过",
                    f"siming:replenish_bought:{iid}",
                    visited_label="已重置周期",
                    style=0,
                    group_id=gid,
                ),
            ],
            [
                make_button(
                    "snooze",
                    "⏳ 还有余量 +14天",
                    f"siming:replenish_snooze:{iid}:14",
                    visited_label="已延后14天",
                    style=0,
                    group_id=gid,
                ),
                make_button(
                    "pause",
                    "⏸️ 暂不提醒",
                    f"siming:replenish_pause:{iid}",
                    visited_label="已暂停提醒",
                    style=0,
                    group_id=gid,
                ),
            ],
        ]
    )


def format_replenish_due_card(
    item: dict[str, Any],
    today: Optional[date] = None,
    *,
    coupon_strategy: Optional[dict[str, Any]] = None,
) -> str:
    td = today or _now().date()
    en = _enrich_item_status(item, td)
    iid = en["item_id"]
    ch = en["channel"]
    if ch == "meituan" and en.get("meituan_shop"):
        ch_label = f"美团闪购直买（优先 **{en['meituan_shop']}** → 配送 **{en['location']}**）"
    elif ch == "meituan":
        ch_label = f"美团闪购直买（配送 **{en['location']}**）"
    elif ch == "jd":
        ch_label = f"京东跨端购物车（SKU `{en['sku']}`）" if en.get("sku") else "京东（待补填 SKU）"
    elif ch == "taobao":
        ch_label = f"淘宝 / 天猫复购（配送 **{en['location']}**）"
    elif ch == "sams":
        ch_label = f"山姆会员商店（App 极速达 / 门店囤货 → **{en['location']}**）"
    elif ch == "hema":
        ch_label = f"盒马鲜生 App 复购（配送 **{en['location']}**）"
    else:
        ch_label = str(en.get("channel_label") or ch)
    price_s = f" · 参考价约 ¥{en['target_yuan']:.0f}" if en.get("target_yuan") else ""
    if en["status"] == "calibrate":
        due_desc = f"上次记录 `{en['last_bought'] or '无'}`，待校准余量"
    elif en["days_left"] < 0:
        due_desc = f"已超过预计补货日 `{en['next_due']}`（逾期 {abs(en['days_left'])} 天）"
    elif en["days_left"] == 0:
        due_desc = f"今天正好到补货周期（`{en['next_due']}`）"
    else:
        due_desc = f"预计 `{en['next_due']}` 用完（仅剩 {en['days_left']} 天）"

    lines = [
        f"🛒 **日用品补货提醒** [`RPL:{iid}`]",
        f"• 商品：**{en['name']}**（📍 {en['location']} · ×{en['qty']}{price_s}）",
    ]
    if en.get("spec"):
        lines.append(f"• 规格：{en['spec']}")
    lines.append(f"• 周期：每 {en['cycle_days']} 天 · {due_desc}")
    lines.append(f"• 渠道：{ch_label}")
    if ch == "meituan" and isinstance(coupon_strategy, dict):
        for s_ln in (coupon_strategy.get("summary_lines") or [])[:2]:
            lines.append(f"• 🎫 红包策略：{s_ln}")
    if ch == "meituan":
        lines.append("👉 点下方「🛒 确认直接买」（或引用本条回 `确认`），我将自动优选最优红包/代金券核验实付价并弹付款确认！")
    elif ch == "jd":
        lines.append("👉 点下方「🛒 确认加京东车」（或引用本条回 `确认`）即可加入手机京东同步购物车；家里还有点「延后14天」。")
    else:
        ch_short = (CHANNEL_META.get(ch) or {}).get("short") or ch
        lines.append(f"👉 请在 **{ch_short}** App 复购；下单后点「✅ 我已买过」（或回 `已买 {en['name']}`）重置下个周期，家里还有点「延后14天」。")
    return "\n".join(lines)


def send_replenish_due_reminder(
    db: SimingDB,
    target: str,
    *,
    now: Optional[datetime] = None,
    bypass_quiet: bool = False,
) -> dict[str, Any]:
    """向 QQ 推送指定日用品的交互式补货确认卡（供到期巡检或塔台「发QQ确认买」按钮调用）。"""
    db.init_db()
    cur = _now(now)
    items = _load_replenish_items(db)
    it = _find_item(items, target)
    if not it:
        return {"ok": False, "error": f"未找到日用品「{target}」"}

    from siming.router import send_qq_message

    coupon_strategy: Optional[dict[str, Any]] = None
    if str(it.get("channel") or "meituan") == "meituan":
        try:
            from siming.meituan_coupons import plan_replenish_coupon_strategy

            coupon_strategy = plan_replenish_coupon_strategy(db, it, items, now=cur)
        except Exception:
            coupon_strategy = None

    text = format_replenish_due_card(it, today=cur.date(), coupon_strategy=coupon_strategy)
    kb = build_replenish_reminder_keyboard(it)
    ref_id = f"rpl:{it['item_id']}:{cur.date().isoformat()}"
    res = send_qq_message(
        db,
        text=text,
        ref_id=ref_id,
        rule_id="replenish_cart",
        bypass_quiet=bypass_quiet,
        sub_meta={"kind": "replenish_due", "item_id": it["item_id"]},
        keyboard=kb,
        dedup_window=timedelta(hours=0) if bypass_quiet else timedelta(hours=24),
    )
    if res.get("delivered") or res.get("queued_for_morning"):
        it["last_reminded_at"] = iso_now(cur)
        it["updated_at"] = iso_now(cur)
        _save_replenish_items(db, items)
    return {"ok": bool(res.get("ok")), "item_id": it["item_id"], "name": it["name"], "send_result": res, "text": text}


def evaluate_replenish_due(
    db: SimingDB,
    *,
    now: Optional[datetime] = None,
    notify: bool = True,
    force_window: bool = False,
    max_per_run: int = 2,
) -> dict[str, Any]:
    """主节拍周期巡检：先增量同步最新订单推进周期，再检查临期/到期日用品并推送 QQ 交互确认卡。"""
    from siming.modules import is_module_enabled

    db.init_db()
    cur = _now(now)
    if not force_window and not is_module_enabled(db, "replenish_cart"):
        return {"ok": True, "skipped": "module_disabled", "due": [], "pushed": []}

    sync_res = sync_replenish_from_recent_orders(db, now=cur)
    items = _load_replenish_items(db)
    if not items:
        return {"ok": True, "due": [], "pushed": [], "synced": sync_res.get("advanced", [])}

    # 默认仅在白天清醒窗（09:30–22:30）主动推送，避免深夜打扰
    hhmm = cur.strftime("%H:%M")
    if notify and not force_window and not ("09:30" <= hhmm <= "22:30"):
        return {"ok": True, "skipped": "outside_window", "due": [], "pushed": [], "synced": sync_res.get("advanced", [])}

    today = cur.date()
    due_candidates: list[dict[str, Any]] = []
    for it in items:
        if not it.get("enabled", True) or it.get("needs_calibration"):
            continue
        snooze_s = str(it.get("snoozed_until") or "")
        if snooze_s:
            try:
                if date.fromisoformat(snooze_s) > today:
                    continue
            except ValueError:
                pass
        en = _enrich_item_status(it, today)
        if en["status"] not in ("due", "soon"):
            continue
        last_rem = str(it.get("last_reminded_at") or "")
        if last_rem:
            try:
                if (cur - parse_iso(last_rem)) < timedelta(hours=72):
                    continue
            except Exception:
                pass
        due_candidates.append(en)

    due_candidates.sort(key=lambda x: (int(x["days_left"]), 0 if x["tier"] == "core" else 1))

    pushed: list[str] = []
    if notify:
        for cand in due_candidates[: max(1, int(max_per_run))]:
            sr = send_replenish_due_reminder(db, cand["item_id"], now=cur, bypass_quiet=False)
            if sr.get("ok") and (
                (sr.get("send_result") or {}).get("delivered")
                or (sr.get("send_result") or {}).get("queued_for_morning")
            ):
                pushed.append(cand["item_id"])

    return {
        "ok": True,
        "synced": sync_res.get("advanced", []),
        "due": [x["item_id"] for x in due_candidates],
        "pushed": pushed,
    }


def stage_replenish_bundle_buy(
    db: SimingDB,
    targets: list[str],
    *,
    meituan_stage_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """同店美团合单补货：把多个日用品合并进同一家商超购物车，共享 1 笔运费并触发更高档闪购/超市大额神券。"""
    db.init_db()
    items = _load_replenish_items(db)
    matched: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for t in targets:
        it = _find_item(items, t)
        if it and it["item_id"] not in seen_ids and str(it.get("channel") or "meituan") == "meituan":
            seen_ids.add(it["item_id"])
            matched.append(it)
    if len(matched) < 2:
        return {"ok": False, "reply": "⚠️ 合单补货至少需要 2 项美团渠道日用品（如 `补货 瓶装水 + 卫生湿巾`）。"}

    loc = str(matched[0].get("location") or "家").strip()
    mt_shop = next((str(x.get("meituan_shop") or "").strip() for x in matched if x.get("meituan_shop")), "")
    explicit_dishes = [
        {"name": str(x.get("meituan_query") or x["name"]).strip(), "qty": max(1, int(x.get("qty") or 1))}
        for x in matched
    ]
    if meituan_stage_fn is None:
        from siming.meituan_agent import stage_meituan_h5_order

        meituan_stage_fn = stage_meituan_h5_order

    names_s = " + ".join(x["name"] for x in matched)
    order_req = f"{mt_shop} {' '.join(d['name'] for d in explicit_dishes)}".strip()
    res = meituan_stage_fn(
        db,
        order_req,
        location_override=loc,
        explicit_shop=mt_shop,
        explicit_dishes=explicit_dishes,
    )
    item_ids = [x["item_id"] for x in matched]
    if res.get("approval_token"):
        _record_approval_item_link(db, str(res["approval_token"]), item_ids)
    db.record_human_use("replenish_cart", "stage_meituan_bundle", ref_id=",".join(item_ids)[:60])
    reply = str(res.get("reply") or "")
    if res.get("ok") and reply:
        reply = f"🛒 **美团同店合单补货**（{names_s} · 共享 1 笔运费并择优叠加满减神券）\n{reply}"
    return {
        **res,
        "reply": reply,
        "bundled": True,
        "channel": "meituan",
        "item_ids": item_ids,
        "item_names": [x["name"] for x in matched],
        "location": loc,
    }


def stage_replenish_buy(
    db: SimingDB,
    target: str,
    *,
    qty: Optional[int] = None,
    meituan_stage_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """统一日用品补货直买入口（QQ 点「确认直接买」或发「补货 <品名>」/「补货 品1 + 品2」触发）：
    - 美团渠道：调用 `stage_meituan_h5_order` 在指定 `家/公司` 地址实时搜店加购核价、择优勾选最优红包/代金券，签发 `meituan_submit_pay` 付款许可确认卡（带原生 `[💳 确认支付 Axx]` 按钮），并附带合单/多件省钱策略；
      若指定优先门店休息/无货，自动回退按商品名在当前配送地址搜附近商超；
    - 京东渠道：若有 SKU 则调用 `stage_jd_cart_add` 签发跨端加车确认卡；若缺 SKU 且配置了 `meituan_query` 则提示或回退。
    """
    db.init_db()
    raw_q = re.sub(r"^(?:帮我|替我)?(?:立即)?(?:合单)?(?:直接买|补货|买一下|买点|买)\s*", "", (target or "").strip()).strip()

    # 检测多品合单补货（如「补货 瓶装水 + 卫生湿巾」或「合单补货 卷纸 和 洗衣凝珠」）
    if re.search(r"[+＋、,，]|\s+[和与]\s+", raw_q):
        parts = [p.strip() for p in re.split(r"[+＋、,，]+|\s+[和与]\s+", raw_q) if p.strip()]
        if len(parts) >= 2:
            items_all = _load_replenish_items(db)
            matched_multi = [_find_item(items_all, p) for p in parts]
            if all(m is not None and str(m.get("channel") or "meituan") == "meituan" for m in matched_multi):
                return stage_replenish_bundle_buy(db, parts, meituan_stage_fn=meituan_stage_fn)

    m_qty = re.search(r"\s+[xX×*]?(\d{1,2})\s*(?:件|箱|包|袋|盒|瓶|桶|提)?$", raw_q)
    if m_qty:
        if qty is None:
            qty = max(1, int(m_qty.group(1)))
        raw_q = raw_q[: m_qty.start()].strip()

    items = _load_replenish_items(db)
    it = _find_item(items, raw_q)
    if not it:
        # 兼容直接传京东链接/SKU
        if _extract_jd_sku(raw_q):
            return stage_jd_cart_add(db, raw_q, qty=qty or 1)
        return {
            "ok": False,
            "reply": f"❌ 日用品清单中未找到「{raw_q}」。发 `常买` 查看已建档清单，或在塔台「日用品补货」页新增。",
        }

    eff_qty = max(1, int(qty if qty is not None else it.get("qty") or 1))
    ch = str(it.get("channel") or "meituan")

    if ch == "jd":
        if it.get("sku"):
            res = stage_jd_cart_add(db, it["sku"], qty=eff_qty)
            if res.get("approval_token"):
                _record_approval_item_link(db, str(res["approval_token"]), it["item_id"])
            res["channel"] = "jd"
            res["item_id"] = it["item_id"]
            return res
        return {
            "ok": False,
            "needs_sku": True,
            "item_id": it["item_id"],
            "reply": (
                f"⚠️ **{it['name']}** 设为京东渠道，但还没有绑定具体型号/京东 SKU。\n"
                f"• 把京东商品链接发给我（如 `常买 添加 {it['name']} https://item.jd.com/xxx.html {it['cycle_days']}`），"
                "或在控制塔「🛒 日用品补货」点编辑填入 SKU / 切换为美团闪购直买，之后就能点确认直接买了！"
            ),
        }

    if ch in ("taobao", "sams", "hema"):
        meta = CHANNEL_META.get(ch) or {}
        ch_short = meta.get("short") or ch
        ch_icon = meta.get("icon") or "🛒"
        spec_line = f"\n• 规格：{it['spec']}" if it.get("spec") else ""
        price_line = f" · 参考价约 ¥{it['target_yuan']:.0f}" if it.get("target_yuan") else ""
        last_line = f"\n• 上次购买：`{it['last_bought']}`" if it.get("last_bought") else ""
        db.record_human_use("replenish_cart", f"stage_{ch}_guide", ref_id=it["item_id"])
        return {
            "ok": True,
            "manual_channel": True,
            "channel": ch,
            "item_id": it["item_id"],
            "item_name": it["name"],
            "location": str(it.get("location") or "家"),
            "reply": (
                f"{ch_icon} **{it['name']}** 固定在 **{ch_short}** 购买（不走美团自动下单）：{spec_line}"
                f"\n• 份数：×{eff_qty}{price_line}{last_line}"
                f"\n👉 请直接在 **{ch_short}** App 复购，买完回 `已买 {it['name']}`（或在控制塔点「刚买过」）即可推进下个周期。"
            ),
        }

    # 美团闪购直买通道：先确保账号闪购/超市神券缓存新鲜，再跨附近商超比价（券后到手价最低优先）实时加购核验！
    mt_query = str(it.get("meituan_query") or it["name"]).strip()
    mt_shop = str(it.get("meituan_shop") or "").strip()
    loc = str(it.get("location") or "家").strip()

    use_default_stage = meituan_stage_fn is None
    if use_default_stage:
        from siming.meituan_agent import stage_meituan_h5_order

        meituan_stage_fn = stage_meituan_h5_order

        # 在线模式自动刷新过期或缺少 flash_coupons 的账户红包缓存，确保补货能用到最新闪购/超市神券
        try:
            from siming.meituan_speculate import fetch_meituan_account_coupons, load_cached_account_coupons

            if load_cached_account_coupons(db, max_age_min=45.0, require_flash=True) is None:
                fetch_meituan_account_coupons(db, force_refresh=True)
        except Exception:
            pass

    if use_default_stage:
        # 跨店全网比价优选：不锁死单店，按「商品价 + 配送费 - 满减/最优闪购神券」选附近券后最省钱商超！
        res = meituan_stage_fn(
            db,
            mt_query,
            location_override=loc,
            explicit_shop="",
            explicit_dishes=[{"name": mt_query, "qty": eff_qty}],
            preferred_shop=mt_shop,
            prefer_lowest_net_price=True,
        )
        # 若泛搜未命中，回退指定优先商超重试
        if not res.get("ok") and not res.get("gated") and not res.get("over_cap") and mt_shop:
            res_fb = meituan_stage_fn(
                db,
                f"{mt_shop} {mt_query}".strip(),
                location_override=loc,
                explicit_shop=mt_shop,
                explicit_dishes=[{"name": mt_query, "qty": eff_qty}],
            )
            if res_fb.get("ok") or res_fb.get("gated") or res_fb.get("over_cap"):
                res = res_fb
    else:
        order_req = f"{mt_shop} {mt_query}".strip() if mt_shop else mt_query
        res = meituan_stage_fn(
            db,
            order_req,
            location_override=loc,
            explicit_shop=mt_shop,
            explicit_dishes=[{"name": mt_query, "qty": eff_qty}],
        )
        if not res.get("ok") and not res.get("gated") and not res.get("over_cap") and mt_shop:
            res_fb = meituan_stage_fn(
                db,
                mt_query,
                location_override=loc,
                explicit_shop="",
                explicit_dishes=[{"name": mt_query, "qty": eff_qty}],
            )
            if res_fb.get("ok") or res_fb.get("gated") or res_fb.get("over_cap"):
                res = res_fb

    if res.get("approval_token"):
        _record_approval_item_link(db, str(res["approval_token"]), it["item_id"])

    # 测算同店合单/多件凑券省钱建议，附在确认卡下方方便用户决定是否合单或加量更省
    coupon_strategy: Optional[dict[str, Any]] = None
    try:
        from siming.meituan_coupons import plan_replenish_coupon_strategy

        coupon_strategy = plan_replenish_coupon_strategy(db, it, items, qty=eff_qty)
    except Exception:
        coupon_strategy = None

    reply_out = str(res.get("reply") or "")
    if res.get("ok") and coupon_strategy and coupon_strategy.get("summary_lines"):
        extra_tips = [
            ln
            for ln in coupon_strategy["summary_lines"]
            if any(k in ln for k in ("合单", "倒省钱", "囤货降单价", "凑单"))
        ]
        if extra_tips and reply_out:
            tip_block = "\n".join(f"• {ln}" for ln in extra_tips[:2])
            if "\n👉" in reply_out:
                head, tail = reply_out.rsplit("\n👉", 1)
                reply_out = f"{head}\n{tip_block}\n👉{tail}"
            else:
                reply_out = f"{reply_out}\n{tip_block}"

    db.record_human_use("replenish_cart", "stage_meituan_buy", ref_id=it["item_id"])
    return {
        **res,
        "reply": reply_out,
        "coupon_strategy": coupon_strategy,
        "channel": "meituan",
        "item_id": it["item_id"],
        "item_name": it["name"],
        "location": loc,
    }


def stage_jd_cart_add(
    db: SimingDB,
    query_or_sku: str,
    qty: int = 1,
) -> dict[str, Any]:
    """根据常买清单名称或京东 SKU/链接生成 15m 有效 `apply Axx` 跨端加购物车确认卡。"""
    db.init_db()
    raw_q = re.sub(r"^(?:帮我|替我)?(?:加购物车|加入购物车|补货|京东加购|加京东购物车)\s*", "", (query_or_sku or "").strip()).strip()
    raw_q = re.sub(r"^把\s*(.+?)\s*(?:加到|加入|放进)(?:京东)?购物车$", r"\1", raw_q).strip()
    m_qty = re.search(r"\s+[xX×*]?(\d{1,2})\s*(?:件|箱|包|袋|盒)?$", raw_q)
    if m_qty:
        qty = max(1, int(m_qty.group(1)))
        raw_q = raw_q[: m_qty.start()].strip()

    sku = _extract_jd_sku(raw_q)
    item_name = f"京东商品 ({sku})" if sku else raw_q
    est_yuan: Optional[float] = None
    matched_id = ""

    for it in _load_replenish_items(db):
        if (sku and it.get("sku") == sku) or (raw_q and (raw_q.lower() == it["item_id"].lower() or raw_q.lower() in str(it.get("name") or "").lower())):
            sku = str(it.get("sku") or "")
            item_name = str(it["name"])
            matched_id = str(it["item_id"])
            if not m_qty:
                qty = int(it.get("qty") or 1)
            if it.get("target_yuan"):
                est_yuan = float(it["target_yuan"]) * qty
            break

    if not sku:
        return {
            "ok": False,
            "reply": f"❌ 未识别到「{raw_q}」对应的京东 SKU。请直接传京东商品链接/SKU，或先用 `常买 添加 <名> <链接> <周期天数>` 建档。",
        }

    policy = load_policy(db.paths)
    max_yuan = float((policy.get("spending_caps") or {}).get("jd_replenish_yuan", 500.0))
    if est_yuan is not None and est_yuan > max_yuan:
        return {
            "ok": False,
            "over_cap": True,
            "reply": f"🛑 预估总额 ¥{est_yuan:.0f} 超过常买补货单次安全上限（¥{max_yuan:.0f}），已拦截。",
        }

    params = {"sku": sku, "name": item_name, "qty": qty, "est_yuan": est_yuan, "item_id": matched_id}
    app = create_approval(
        db,
        action="jd_add_cart",
        target_id=f"JD-{sku[:14]}",
        params=params,
        ttl_minutes=15,
    )
    if matched_id:
        _record_approval_item_link(db, app["token"], matched_id)
    db.record_human_use("replenish_cart", "stage_cart", ref_id=app["token"])
    est_s = f"（参考好价约 ¥{est_yuan:.0f}）" if est_yuan else ""
    reply = (
        f"🛒 **京东跨端购物车代填确认卡** [`{app['token']}`]\n"
        f"• 商品：**{item_name}**（SKU `{sku}` × **{qty}** 件）{est_s}\n"
        f"• 动作：经本机 9222 浏览器后台加入你的京东同步购物车（**不自动扣款结算**）\n"
        f"👉 15 分钟内发 `apply {app['token']}`（或引用本条回 `确认`），加好后手机点一下京东购物车即可付款！"
    )
    return {"ok": True, "approval_token": app["token"], "sku": sku, "reply": reply}


def execute_jd_cart_add(
    params: dict[str, Any],
    db: SimingDB,
    cdp_exec_fn: Optional[Callable[[str], dict[str, Any]]] = None,
) -> str:
    """纯代码审批通过后调用京东官方 `InitCart.aspx` 加入跨端购物车，并刷新该常买品周期起点。"""
    sku = str(params.get("sku") or _extract_jd_sku(str(params.get("url") or "")))
    name = str(params.get("name") or sku)
    qty = max(1, int(params.get("qty") or 1))
    init_cart_url = f"https://gate.jd.com/InitCart.aspx?pid={sku}&pcount={qty}&ptype=1"

    def _live_add(u: str) -> dict[str, Any]:
        js = "JSON.stringify({url: location.href, title: document.title || '', ok: /成功加入购物车|购物车|addToCart/i.test((document.body && document.body.innerText)||'')})"
        return inspect_url_in_background_tab(u, js, wait_sec=2.4, close_when_clean=True)

    res = (cdp_exec_fn or _live_add)(init_cart_url)

    # 若命中常买周期清单，顺手推进下次补货日期
    items = _load_replenish_items(db)
    today_d = _now().date()
    target_iid = str(params.get("item_id") or "")
    changed = False
    for it in items:
        if (target_iid and it["item_id"] == target_iid) or (sku and str(it.get("sku")) == sku):
            it["last_bought"] = today_d.isoformat()
            it["next_due"] = (today_d + timedelta(days=int(it.get("cycle_days") or 30))).isoformat()
            it["needs_calibration"] = False
            it["snoozed_until"] = ""
            it["last_reminded_at"] = ""
            it["updated_at"] = iso_now()
            changed = True
    if changed:
        _save_replenish_items(db, items)

    if res.get("gated"):
        reason = (res.get("gate") or {}).get("reason") or "需登录京东网页端"
        return (
            f"已在桌面主浏览器后台打开「{name}」加购页（提示：{reason}，标签已保留供你点一下）；"
            f"你也可以直接点链接在手机加购：https://item.jd.com/{sku}.html"
        )
    if res.get("ok"):
        return (
            f"✅ 已将 **{name}**（×{qty}）加入你的京东跨端同步购物车！\n"
            f"手机点此直达京东购物车结算：{JD_MOBILE_CART_DEEPLINK}"
        )
    return f"本机浏览器暂未就绪，点此直达商品页一键加购：https://item.jd.com/{sku}.html"
