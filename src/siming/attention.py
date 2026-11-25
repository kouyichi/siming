"""Night-Owl 夜猫子动态作息注意力引擎（v1）：
- 取代死板的 `23:30–07:30` 固定宵禁，理解主人 `01:30–04:30` 高强度编码、上午补觉的真实节律；
- 多源活跃信号融合：
  1. 本人显式状态口令（QQ 发 `睡了`/`晚安` 或 `醒了`/`早`，及 iPhone 睡眠专注快捷指令投递 `sleep`/`awake`）；
  2. 桌面图形会话锁屏/空闲时长（`loginctl show-session` `LockedHint` / `IdleHint` / `IdleSinceHintMonotonic` + `hyprlock`）；
  3. 近期人机交互（Siming `feature_usage` 与编码 Agent 新发言 `last_agent_utterance_at`）；
- 四级注意力分流：
  • `L0`：日常银行卡流水等，静默落库入晨札；
  • `L1`：睡醒晨札（若凌晨晚睡且 08:15 仍在锁屏补觉，自动延后至本人醒来解锁/发消息或最晚 `11:45` 再推，绝不吵醒补觉！）；
  • `L2`：常规提醒与 Action Card（02:30 清醒写代码时即时送达；真睡着后静默转入 `MORNING_QUEUE`）；
  • `L3`：紧急破静（`urgent=True`，如单笔 ≥¥20,000 超大额刷卡、严重断网/付款锁告警），全天穿透。
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming import db as _db_mod
from siming.config import load_policy
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.event_chain import get_situation
from siming.modules import is_module_enabled

_ORIG_NOW_LOCAL = now_local


def _cur_now() -> datetime:
    return now_local() if now_local is not _ORIG_NOW_LOCAL else _db_mod.now_local()


def _hhmm_in_range(hhmm: str, start: str, end: str) -> bool:
    if start > end:
        return hhmm >= start or hhmm < end
    return start <= hhmm < end


def probe_desktop_session_activity() -> dict[str, Any]:
    """无侵入只读探测本机 Wayland/systemd 会话锁屏与空闲秒数（离线测试环境自动返回 unavailable 以保沙盒确定性）。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1" or os.environ.get("SIMING_DRY_RUN_SEND") == "1":
        return {"available": False}

    try:
        ls_proc = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if ls_proc.returncode != 0:
            return {"available": False}
        uid_str = str(os.getuid())
        sess_ids: list[str] = []
        for ln in (ls_proc.stdout or "").splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[1] == uid_str:
                sess_ids.append(parts[0])
        if not sess_ids:
            return {"available": False}

        best_idle_sec: Optional[float] = None
        any_unlocked_graphical = False
        any_locked = False

        now_mono_us = time.monotonic_ns() // 1000
        for sid in sess_ids[:3]:
            sh = subprocess.run(
                [
                    "loginctl",
                    "show-session",
                    sid,
                    "-p",
                    "Type",
                    "-p",
                    "State",
                    "-p",
                    "LockedHint",
                    "-p",
                    "IdleHint",
                    "-p",
                    "IdleSinceHintMonotonic",
                ],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if sh.returncode != 0:
                continue
            kv: dict[str, str] = {}
            for line in (sh.stdout or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
            s_type = kv.get("Type", "")
            if s_type not in ("wayland", "x11") and kv.get("State") != "active":
                continue
            locked = kv.get("LockedHint", "no") == "yes"
            idle_hint = kv.get("IdleHint", "no") == "yes"
            mono_us = int(kv.get("IdleSinceHintMonotonic") or "0")
            if locked:
                any_locked = True
            else:
                any_unlocked_graphical = True
            if not idle_hint or mono_us <= 0:
                idle_sec = 0.0 if not locked else 1800.0
            else:
                idle_sec = max(0.0, (now_mono_us - mono_us) / 1_000_000.0)
            if best_idle_sec is None or idle_sec < best_idle_sec:
                best_idle_sec = idle_sec

        hyprlock_running = False
        hl = subprocess.run(["pgrep", "-u", uid_str, "-x", "hyprlock"], capture_output=True, timeout=1.0)
        if hl.returncode == 0:
            hyprlock_running = True
            any_locked = True

        if best_idle_sec is None:
            return {"available": False}
        return {
            "available": True,
            "locked": hyprlock_running or (any_locked and not any_unlocked_graphical),
            "idle_sec": round(best_idle_sec, 1),
        }
    except Exception:
        return {"available": False}


def record_explicit_sleep_state(
    db: SimingDB,
    sleeping: bool,
    source: str = "qq",
    at_ts: Optional[str] = None,
) -> dict[str, Any]:
    """记录本人显式入睡/醒来信号（如 QQ 发 `睡了`/`醒了` 或 iOS 睡眠专注快捷指令 `sleep`/`awake`）。"""
    db.init_db()
    ts = at_ts or iso_now(_cur_now())
    state_val = "ASLEEP" if sleeping else "AWAKE"
    db.set_meta("attention_explicit_state", state_val)
    db.set_meta("attention_explicit_at", ts)
    db.set_meta("attention_explicit_source", source)
    if not sleeping:
        db.set_meta("last_human_active_at", ts)
    db.write_audit("A0", "attention_state_set", {"state": state_val, "source": source})
    if sleeping:
        return {
            "ok": True,
            "state": "ASLEEP",
            "summary": "🌙 已切入睡眠免打扰模式（L2 常规提醒自动暂存进睡醒晨札，仅 L3 超大额/紧急故障可穿透；醒来发一句 `早` 或解锁桌面即恢复）。",
        }
    return {
        "ok": True,
        "state": "AWAKE",
        "summary": "☀️ 早！已恢复清醒活跃模式，睡间暂存事项随晨札一并奉上。",
    }


def ingest_ios_sleep_signal(db: SimingDB, raw_state: str, received_at: Optional[str] = None) -> dict[str, Any]:
    """处理来自 iPhone `/siming/in/sleep` 的无感自动化信号（完全不依赖手动开关睡眠专注或手发 QQ）：
    - `sleep / asleep / bedtime / 睡眠开启`：直接置显式 ASLEEP；
    - `awake / wake / alarm_off / 起床 / 睡眠结束`：直接置显式 AWAKE 并冲刷睡醒晨札；
    - `charge_on`：记录手机插上充电器时刻（若在睡眠带且电脑+手机均静默 >=15m，自动判定入睡；边充电边玩手机或敲代码绝不误判！）；
    - `charge_off`：在 06:00–14:30 拔掉床头充电器时，无论此前是显式还是被动入睡，一律自动唤醒为 AWAKE；
    - `phone_active / active / app_open / unlock`：多选常用 App 打开时静默上报的手机活跃心跳——
      1) 凌晨插电躺在床上刷手机时，持续刷新 `last_ios_active_at` 防止被误判为已睡着；
      2) 早晨 06:30–14:30 若此前处于睡眠态（含被动睡眠），在被窝里点开任意常用 App 即刻判定睡醒并推送延后的晨札！
    """
    db.init_db()
    st = (raw_state or "").strip().lower()
    ts = received_at or iso_now()

    if st in ("sleep", "asleep", "bedtime", "睡眠开启", "on", "1"):
        res = record_explicit_sleep_state(db, sleeping=True, source="ios_sleep_focus", at_ts=ts)
        db.set_meta("ios_charger_connected_at", "")
        return res

    if st in ("alarm_off", "起床", "睡眠结束", "off", "0"):
        res = record_explicit_sleep_state(db, sleeping=False, source="ios_alarm_or_wake", at_ts=ts)
        db.set_meta("ios_charger_connected_at", "")
        db.set_meta("last_ios_active_at", ts)
        db.set_meta("ios_wakeup_pending_brief", "1")
        return res

    if st in ("charge_on", "charging", "充电"):
        db.set_meta("ios_charger_connected_at", ts)
        db.write_audit("A0", "ios_charger_hint", {"state": "charge_on", "ts": ts})
        return {"ok": True, "state": "CHARGE_ON_HINT", "summary": "已记录手机充电连接信号"}

    if st in ("charge_off", "unplugged", "拔电"):
        had_charger = bool(db.get_meta("ios_charger_connected_at"))
        try:
            sig_dt = parse_iso(ts)
        except Exception:
            sig_dt = _cur_now()
        was_asleep = evaluate_attention_state(db, now_override=sig_dt)["state"] == "ASLEEP"
        db.set_meta("ios_charger_connected_at", "")
        db.set_meta("last_ios_active_at", ts)
        if 6 <= sig_dt.hour <= 14 and (had_charger or was_asleep or db.get_meta("attention_explicit_state") == "ASLEEP"):
            res = record_explicit_sleep_state(db, sleeping=False, source="ios_charger_unplug", at_ts=ts)
            db.set_meta("ios_wakeup_pending_brief", "1")
            return res
        return {"ok": True, "state": "CHARGE_OFF_HINT", "summary": "已清除手机充电连接信号"}

    if st in ("loc_move", "depart_home"):
        had_charger = bool(db.get_meta("ios_charger_connected_at"))
        try:
            sig_dt = parse_iso(ts)
        except Exception:
            sig_dt = _cur_now()
        was_state = evaluate_attention_state(db, now_override=sig_dt)
        db.set_meta("last_ios_active_at", ts)
        db.set_meta("last_human_active_at", ts)
        if db.get_meta("attention_explicit_state") == "ASLEEP":
            db.set_meta("attention_explicit_state", "")
        if "06:00" <= sig_dt.strftime("%H:%M") <= "14:30" and (was_state["state"] == "ASLEEP" or had_charger):
            db.set_meta("ios_charger_connected_at", "")
            res = record_explicit_sleep_state(db, sleeping=False, source="ios_loc_depart", at_ts=ts)
            db.set_meta("ios_wakeup_pending_brief", "1")
            return res
        return {"ok": True, "state": "AWAKE_ACTIVE", "summary": "检测到真实离家/户外移动"}

    # 兼容用户把 App 打开自动化配为 ?state=awake 或 ?state=phone_active：
    # - 凌晨 00:30–05:59 躺在床上刷 App 时：刷新活跃心跳但不清空充电状态，放下手机静默满 15m 即可自然入睡；
    # - 06:00–14:30 早晨时段点开任意 App 时：直接判定睡醒并触发晨札冲刷！
    if st in ("awake", "wake", "phone_active", "active", "app_open", "unlock", "手机活跃"):
        had_charger = bool(db.get_meta("ios_charger_connected_at"))
        try:
            sig_dt = parse_iso(ts)
        except Exception:
            sig_dt = _cur_now()
        was_state = evaluate_attention_state(db, now_override=sig_dt)
        prev_ios_act = db.get_meta("last_ios_active_at") or ""
        long_phone_silence = True
        if prev_ios_act:
            try:
                long_phone_silence = (sig_dt - parse_iso(prev_ios_act)).total_seconds() >= 7200
            except Exception:
                long_phone_silence = True
        db.set_meta("last_ios_active_at", ts)
        db.set_meta("last_human_active_at", ts)
        if db.get_meta("attention_explicit_state") == "ASLEEP":
            db.set_meta("attention_explicit_state", "")
        hhmm = sig_dt.strftime("%H:%M")
        if "06:00" <= hhmm <= "14:30" and (was_state["state"] == "ASLEEP" or (had_charger and long_phone_silence)):
            res = record_explicit_sleep_state(db, sleeping=False, source="ios_phone_wake", at_ts=ts)
            db.set_meta("ios_wakeup_pending_brief", "1")
            return res
        return {"ok": True, "state": "AWAKE_ACTIVE", "summary": "已刷新手机亮屏使用心跳"}

    return record_explicit_sleep_state(db, sleeping=(st != "awake"), source="ios_shortcut")


def touch_human_activity(db: SimingDB, source: str = "interaction") -> None:
    """在检测到本人主动发 QQ 或在编码 Agent 中新发话时刷新最近活跃时间戳；若此前处于显式睡眠且本人主动发话则自动唤醒。"""
    cur_now = _cur_now()
    ts = iso_now(cur_now)
    db.set_meta("last_human_active_at", ts)
    if source in ("qq_inbound", "agent_utterance") and db.get_meta("attention_explicit_state") == "ASLEEP":
        exp_at = db.get_meta("attention_explicit_at") or ""
        try:
            # 刚发完「睡了」120 秒内的尾随消息不误唤醒，超过 2 分钟主动说话即自动解除睡眠态
            if not exp_at or (cur_now - parse_iso(exp_at)).total_seconds() > 120:
                db.set_meta("attention_explicit_state", "AWAKE")
                db.set_meta("attention_explicit_at", ts)
        except Exception:
            pass


def _recent_db_human_age_sec(db: SimingDB, now: datetime) -> Optional[float]:
    ages: list[float] = []
    for key in ("last_human_active_at", "last_agent_utterance_at", "last_ios_active_at"):
        val = db.get_meta(key)
        if val:
            try:
                diff = (now - parse_iso(val)).total_seconds()
                if diff >= -5.0:
                    ages.append(max(0.0, diff))
            except Exception:
                pass
    conn = db.connect()
    try:
        row = conn.execute("SELECT MAX(ts) AS last_ts FROM feature_usage WHERE actor='human'").fetchone()
        if row and row["last_ts"]:
            diff_u = (now - parse_iso(str(row["last_ts"]))).total_seconds()
            if diff_u >= -5.0:
                ages.append(max(0.0, diff_u))
    except Exception:
        pass
    finally:
        conn.close()
    return min(ages) if ages else None


def evaluate_attention_state(
    db: SimingDB,
    policy: Optional[dict[str, Any]] = None,
    desktop_probe: Optional[Callable[[], dict[str, Any]]] = None,
    now_override: Optional[datetime] = None,
) -> dict[str, Any]:
    """计算当前作息注意力状态：返回 `{state, should_hold_l2, reason, detail}`。"""
    db.init_db()
    pol = policy or load_policy(db.paths)
    now = now_override or _cur_now()
    hhmm = now.strftime("%H:%M")
    q_cfg = pol.get("quiet_hours") or {"start": "23:30", "end": "07:30"}
    q_start, q_end = str(q_cfg.get("start", "23:30")), str(q_cfg.get("end", "07:30"))
    static_quiet = _hhmm_in_range(hhmm, q_start, q_end)

    if not is_module_enabled(db, "night_owl"):
        return {
            "state": "STATIC_QUIET" if static_quiet else "AWAKE_ACTIVE",
            "should_hold_l2": static_quiet,
            "reason": "static_quiet_hours",
            "hhmm": hhmm,
        }

    no_cfg = pol.get("night_owl") or {}
    active_grace_min = float(no_cfg.get("active_grace_minutes", 35))
    sleep_idle_min = float(no_cfg.get("sleep_idle_minutes", 40))
    sleep_window_start = str(no_cfg.get("sleep_window_start", "00:30"))
    sleep_window_end = str(no_cfg.get("sleep_window_end", "12:00"))
    explicit_ttl_hours = float(no_cfg.get("explicit_sleep_ttl_hours", 11.0))

    # 1. 显式睡眠/清醒覆盖（含 `meta.attention_explicit_state` 与实时情境 `situation['sleep:state']`，11 小时 TTL 防忘记解除）
    exp_state = db.get_meta("attention_explicit_state") or ""
    exp_at_s = db.get_meta("attention_explicit_at") or ""
    exp_dt: Optional[datetime] = None
    exp_valid = False
    exp_age_h = 999.0
    if exp_state in ("ASLEEP", "AWAKE") and exp_at_s:
        try:
            exp_dt = parse_iso(exp_at_s)
            exp_age_h = (now - exp_dt).total_seconds() / 3600.0
            if 0 <= exp_age_h <= explicit_ttl_hours:
                exp_valid = True
        except Exception:
            exp_valid = False

    sit_sleep = get_situation(db, "sleep:state")
    sit_state = ""
    sit_dt: Optional[datetime] = None
    sit_src = "situation"
    sit_valid = False
    if sit_sleep and isinstance(sit_sleep.get("value"), dict):
        raw_st = str(sit_sleep["value"].get("state") or "").strip().lower()
        sit_src = str(sit_sleep.get("source") or "situation")
        as_of_s = str(sit_sleep.get("as_of") or "")
        if as_of_s:
            try:
                sit_dt = parse_iso(as_of_s)
            except Exception:
                sit_dt = None
        stale_sit = sit_dt is not None and ((now - sit_dt).total_seconds() / 3600.0) > explicit_ttl_hours
        if not stale_sit:
            if raw_st in ("sleep", "asleep", "sleeping", "bedtime", "nap"):
                sit_state = "ASLEEP"
                sit_valid = True
            elif raw_st in ("awake", "awake_active", "active"):
                sit_state = "AWAKE"
                sit_valid = True

    if exp_valid and sit_valid:
        # 两者皆有效时以更新时刻较新者为准；若无法比较则以显式口令优先
        use_sit = bool(sit_dt is not None and exp_dt is not None and sit_dt > exp_dt)
    else:
        use_sit = sit_valid and not exp_valid

    if use_sit:
        if sit_state == "ASLEEP":
            return {
                "state": "ASLEEP",
                "should_hold_l2": True,
                "reason": f"situation_sleep({sit_src})",
                "hhmm": hhmm,
            }
        return {
            "state": "AWAKE_ACTIVE",
            "should_hold_l2": False,
            "reason": f"situation_awake({sit_src})",
            "hhmm": hhmm,
        }

    if exp_valid:
        if exp_state == "ASLEEP":
            return {
                "state": "ASLEEP",
                "should_hold_l2": True,
                "reason": f"explicit_sleep({db.get_meta('attention_explicit_source') or 'manual'})",
                "hhmm": hhmm,
            }
        if exp_age_h <= 1.0:
            return {
                "state": "AWAKE_ACTIVE",
                "should_hold_l2": False,
                "reason": "explicit_awake_recent",
                "hhmm": hhmm,
            }

    probe = (desktop_probe or probe_desktop_session_activity)()
    human_age_sec = _recent_db_human_age_sec(db, now)

    # 若在离线沙盒测试且未注入 desktop_probe，则回退静态 quiet_hours 保证测试 100% 确定性
    if not probe.get("available") and desktop_probe is None:
        return {
            "state": "ASLEEP" if static_quiet else "AWAKE_ACTIVE",
            "should_hold_l2": static_quiet,
            "reason": "fallback_static_quiet",
            "hhmm": hhmm,
        }

    desk_locked = bool(probe.get("locked", False))
    desk_idle_sec = float(probe.get("idle_sec", 99999.0)) if probe.get("available") else 99999.0
    min_inactive_sec = min(desk_idle_sec, human_age_sec if human_age_sec is not None else 99999.0)
    in_sleep_band = _hhmm_in_range(hhmm, sleep_window_start, sleep_window_end)

    # 2. 手机床头充电信号优先融合：若在睡眠带插上了充电器，且电脑已锁屏或键鼠停止操作满 10 分钟（且最近 15 分钟内没有在床上刷手机或发消息），无需等满 35m 直接切入睡眠！
    chg_at = db.get_meta("ios_charger_connected_at") or ""
    if in_sleep_band and chg_at:
        try:
            chg_age_sec = (now - parse_iso(chg_at)).total_seconds()
            human_recent = human_age_sec is not None and human_age_sec < 900 and human_age_sec < chg_age_sec
            if 0 <= chg_age_sec <= 36000 and (desk_locked or desk_idle_sec >= 600) and not human_recent:
                return {
                    "state": "ASLEEP",
                    "should_hold_l2": True,
                    "reason": f"ios_charging_and_desk_idle(idle={int(desk_idle_sec)}s)",
                    "hhmm": hhmm,
                }
        except Exception:
            pass

    # 3. 即使在凌晨 02:30，只要桌面未锁屏且 35m 内有键鼠活跃，或刚在 QQ/Agent 里说过话 -> 判定为夜猫子清醒工作态！
    if (not desk_locked and desk_idle_sec <= active_grace_min * 60) or (
        human_age_sec is not None and human_age_sec <= active_grace_min * 60
    ):
        return {
            "state": "AWAKE_ACTIVE",
            "should_hold_l2": False,
            "reason": f"active_input(idle={int(min_inactive_sec)}s)",
            "hhmm": hhmm,
        }

    # 4. 在夜猫子睡眠保护窗（00:30–12:00，覆盖凌晨入睡与上午补觉）且连续空闲/锁屏 >= 40m -> 判定为真睡补觉态！
    if in_sleep_band and (desk_locked or min_inactive_sec >= sleep_idle_min * 60):
        return {
            "state": "ASLEEP",
            "should_hold_l2": True,
            "reason": f"night_owl_asleep(locked={desk_locked}, idle={int(min_inactive_sec)}s)",
            "hhmm": hhmm,
        }

    if static_quiet and min_inactive_sec >= sleep_idle_min * 60:
        return {
            "state": "ASLEEP",
            "should_hold_l2": True,
            "reason": f"quiet_idle({int(min_inactive_sec)}s)",
            "hhmm": hhmm,
        }

    return {
        "state": "AWAKE_IDLE",
        "should_hold_l2": False,
        "reason": f"daytime_idle(locked={desk_locked}, idle={int(min_inactive_sec)}s)",
        "hhmm": hhmm,
    }


def should_defer_morning_brief_for_sleep(
    db: SimingDB,
    policy: Optional[dict[str, Any]] = None,
    desktop_probe: Optional[Callable[[], dict[str, Any]]] = None,
    now_override: Optional[datetime] = None,
) -> tuple[bool, str]:
    """判断 08:15 晨札是否应因主人仍在补觉而延后投递（最晚不超过 `latest_morning_brief` 默认 11:45）。"""
    pol = policy or load_policy(db.paths)
    now = now_override or _cur_now()
    no_cfg = pol.get("night_owl") or {}
    latest_hhmm = str(no_cfg.get("latest_morning_brief", "11:45"))
    cur_hhmm = now.strftime("%H:%M")
    if cur_hhmm >= latest_hhmm:
        return False, f"reached_latest_deadline({latest_hhmm})"

    st = evaluate_attention_state(db, policy=pol, desktop_probe=desktop_probe, now_override=now)
    # 离线回退模式下保持原有 08:15 准点行为
    if st["reason"] == "fallback_static_quiet" or st["reason"] == "static_quiet_hours":
        return False, st["reason"]
    if st["state"] == "ASLEEP":
        return True, st["reason"]
    return False, st["reason"]


def format_attention_status(
    db: SimingDB,
    desktop_probe: Optional[Callable[[], dict[str, Any]]] = None,
) -> str:
    st = evaluate_attention_state(db, desktop_probe=desktop_probe)
    badge = "🌙 睡眠免打扰（L2 暂存晨札）" if st["should_hold_l2"] else "🟢 清醒在线（L2 即时可达）"
    return (
        f"🕰️ **Night-Owl 注意力状态**：{badge}\n"
        f"• 当前判定：`{st['state']}` · 依据：`{st['reason']}`（时刻 {st['hhmm']}）\n"
        "• 随时发 `睡了` 立即进入睡眠保护，发 `早` / `醒了`（或解锁桌面）即刻唤醒并冲刷暂存提醒。"
    )
