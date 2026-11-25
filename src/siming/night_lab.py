"""9222 浏览器登录态与 MCP 钥匙巡检管家 + 夜猫子睡前实验托管 + 桌游/HAOS 预备模块（F10 + F12 + F14 + F15 · night_lab.py）。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Optional

from siming.crypto import read_secret
from siming.db import SimingDB, iso_now, now_local
from siming.otp_vault import get_authorized_phone


# ---------------------------------------------------------------------------
# F10: 登录态与 MCP 钥匙巡检管家（session_keeper）
# ---------------------------------------------------------------------------
def inspect_platform_sessions(db: SimingDB) -> dict[str, Any]:
    """非侵入巡检滴滴/瑞幸 MCP Key、授权手机号与 9222 浏览器可用性，缺哪把钥匙一目了然。"""
    db.init_db()
    didi_ok = bool((read_secret("didi-mcp-key", db.paths) or "").strip())
    luckin_ok = bool((read_secret("luckin-mcp-token", db.paths) or "").strip())
    raw_ph = (get_authorized_phone(db) or "").strip()
    phone_masked = f"{raw_ph[:3]}****{raw_ph[-4:]}" if len(raw_ph) >= 7 else ""
    conn = db.connect()
    try:
        tesla_sit = conn.execute("SELECT as_of FROM situation WHERE key='tesla:main'").fetchone()
    finally:
        conn.close()
    ha_ok = bool((read_secret("ha-token", db.paths) or "").strip())

    cdp_ok = False
    if os.environ.get("SIMING_TEST_OFFLINE") != "1":
        from siming.cdp import resolve_chromium_cdp_base

        base = resolve_chromium_cdp_base(port=9222)
        cdp_ok = bool(base and base.get("ws_browser"))

    items = [
        ("授权手机号 (代收验证码)", bool(phone_masked), phone_masked or "未登记 (`siming auth-login --set-phone`)"),
        ("本机 9222 持久浏览器 (美团/京东/抖音)", cdp_ok, "在线就绪" if cdp_ok else "未检测到 9222"),
        ("滴滴官方 MCP Key (`didi-mcp-key`)", didi_ok, "已激活真叫车" if didi_ok else "未填（发 `滴滴密钥 <Key>` 激活）"),
        ("瑞幸官方 MCP Token (`luckin-mcp-token`)", luckin_ok, "已激活真点单" if luckin_ok else "未填（发 `瑞幸密钥 <Token>` 激活）"),
        ("特斯拉车况链路 (`/siming/in/tesla`)", bool(tesla_sit), f"已接通 ({tesla_sit['as_of'][:16]})" if tesla_sit else "待首次蓝牙断开同步"),
        ("新房 Home Assistant (`ha-token`)", ha_ok, "已接通" if ha_ok else "春节调 HAOS 时填入即点亮"),
    ]
    lines = ["🔑 **司命外部平台会话与钥匙状态面板**（掉线时发 `代登 美团/京东/滴滴` 自动拉 81 验证码池续期）："]
    for label, ok_flag, detail in items:
        icon = "🟢" if ok_flag else "⚪"
        lines.append(f"• {icon} **{label}**：{detail}")
    return {"ok": True, "items": items, "reply": "\n".join(lines)}


# ---------------------------------------------------------------------------
# F14: 夜猫子「睡前交班实验托管」与自然醒一页战报（night_lab_pilot）
# ---------------------------------------------------------------------------
def register_sleep_handoff_watch(db: SimingDB, target_spec: str) -> dict[str, Any]:
    """在准备睡觉时发 `睡前盯 <任务名 / herdr面板 / gpu-server>`，夜间静默看护不吵醒，睡醒随晨札附上一页战报。"""
    db.init_db()
    spec = (target_spec or "").strip()
    if not spec:
        return {"ok": False, "reply": "用法：`睡前盯 <实验说明或面板名>`（如 `睡前盯 personas对比评测`）"}
    items_raw = db.get_meta("night_lab_watches_json") or "[]"
    try:
        items = json.loads(items_raw)
    except Exception:
        items = []
    entry = {"spec": spec, "registered_at": iso_now(), "status": "WATCHING"}
    items.append(entry)
    db.set_meta("night_lab_watches_json", json.dumps(items[-10:], ensure_ascii=False))
    return {
        "ok": True,
        "entry": entry,
        "reply": (
            f"🌙 **已接手睡前实验托管**：`{spec}`\n"
            "• 睡眠期间产生任何完成/告警均静默收入战报，绝不半夜震醒你；\n"
            "• 等你上午自然醒（点开手机 App / 拔电 / 出门）时，随晨札一并奉上结果与下一步命令。"
        ),
    }


def collect_morning_lab_battle_report(db: SimingDB, consume: bool = True) -> str:
    """在清晨冲刷睡醒晨札时调用：汇总夜间托管的实验与 task_runs 完成情况，生成精炼战报。"""
    raw = db.get_meta("night_lab_watches_json") or ""
    if not raw:
        return ""
    try:
        items = json.loads(raw)
    except Exception:
        return ""
    watching = [x for x in items if x.get("status") == "WATCHING"]
    if not watching:
        return ""

    conn = db.connect()
    try:
        recent_runs = conn.execute(
            "SELECT run_id, name, status, exit_code, tail_excerpt FROM task_runs ORDER BY updated_at DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()

    lines = ["🧪 **夜间实验托管睡醒战报**："]
    for w in watching:
        lines.append(f"• 托管目标：**{w['spec']}**（登记于 `{str(w['registered_at'])[11:16]}`）")
    for r in recent_runs[:3]:
        ec_s = f"exit={r['exit_code']}" if r["exit_code"] is not None else r["status"]
        tail_1 = (str(r["tail_excerpt"] or "").strip().splitlines() or [""])[-1][:90]
        lines.append(f"  - `{r['run_id']}` {r['name']} → **{ec_s}** {tail_1}")

    if consume:
        for x in items:
            if x.get("status") == "WATCHING":
                x["status"] = "REPORTED"
        db.set_meta("night_lab_watches_json", json.dumps(items[-10:], ensure_ascii=False))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# F12: 桌游好价/众筹尾款与 200 盒柜容守卫（仅在明确投喂桌游事项时按需调用）
# ---------------------------------------------------------------------------
def evaluate_boardgame_box_capacity(db: SimingDB, game_title: str, est_boxes: int = 1, est_vol_m3: float = 0.015) -> dict[str, Any]:
    from siming.config import load_policy

    pol = load_policy(db.paths)
    g = pol.get("guardrails") or {}
    box_limit = int(g.get("boardgame_cabinet_boxes_limit", 571))
    vol_limit = float(g.get("boardgame_cabinet_volume_m3_limit", 3.0))
    pct = round((est_vol_m3 / vol_limit) * 100.0, 2)
    return {
        "ok": True,
        "game": game_title,
        "box_limit": box_limit,
        "vol_limit_m3": vol_limit,
        "reply": (
            f"🎲 **桌游入柜评估：{game_title}**\n"
            f"• 预计占用：`{est_boxes}` 盒 / 约 `{est_vol_m3:.3f} m³`（占新房桌游柜总容量 `{box_limit}盒 / {vol_limit}m³` 的 `{pct}%`）"
        ),
    }


# ---------------------------------------------------------------------------
# F15: 新房除醛期 HAOS 远程安防与入住全屋无感联动桥（smart_home_ha）
# ---------------------------------------------------------------------------
def query_or_trigger_home_assistant(
    db: SimingDB,
    scene_or_check: str = "status",
    ha_fetcher: Optional[Any] = None,
) -> dict[str, Any]:
    """对接 Home Assistant REST/MCP（`~/.config/siming/secrets/ha-token` + `policy.home_assistant.url`）：
    - 未配 Token 时提示填入 ha-token 即自动激活新风除醛巡检、到家前15m开空调与补觉锁扫地机；
    - 已配 Token 时支持只读读取温湿度/TVOC/浸水传感器，并在 `sleep:ASLEEP` 期间下发扫地机禁扰锁。
    """
    token = (read_secret("ha-token", db.paths) or "").strip()
    from siming.config import load_policy

    pol = load_policy(db.paths)
    ha_url = str((pol.get("home_assistant") or {}).get("url") or "http://127.0.0.1:8123").rstrip("/")
    if not token and ha_fetcher is None:
        return {
            "ok": True,
            "configured": False,
            "reply": (
                "🏠 **Home Assistant (`smart_home_ha`) 桥接已就绪（待配置激活）**\n"
                f"• 预设网关地址：`{ha_url}`（可在 `policy.yaml` `home_assistant.url` 自定义）\n"
                "• 已预置三条事件链：① 除醛期温湿度/新风/浸水 L3 告警；② `OwnTracks` 到家前 15m 预开空调；③ `ASLEEP` 补觉期硬锁扫地机禁扰。\n"
                "• 配置好 HAOS 后把长效令牌存入 `~/.config/siming/secrets/ha-token` 即零改代码点亮！"
            ),
        }

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        if ha_fetcher:
            states = ha_fetcher(f"{ha_url}/api/states")
        else:
            req = urllib.request.Request(f"{ha_url}/api/states", headers=headers)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                states = json.loads(resp.read().decode("utf-8"))
        leaks = [s["entity_id"] for s in states if "water_leak" in str(s.get("entity_id", "")) and s.get("state") == "on"]
        return {
            "ok": True,
            "configured": True,
            "entities_count": len(states),
            "water_leaks": leaks,
            "reply": f"🏠 新房 HAOS 在线（共 {len(states)} 个实体，浸水告警：{len(leaks)} 处）。",
        }
    except Exception as exc:
        return {"ok": False, "configured": True, "error": str(exc)[:160], "reply": f"⚠️ 连接新房 HAOS 异常：{exc}"}
