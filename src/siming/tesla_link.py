"""特斯拉（Tesla Model Y 后驱标续 LFP）剩余电量、续航与充电联动模块（Directive #4 + F07 · tesla_link.py）：
针对官方 iOS Tesla App 快捷指令仅开放控制动作、未开放『读取电量』动作的事实，提供三条真实可用的零休眠耗电（0 Vampire Drain）链路：
1. 通道 A（零配置即刻可用）：打开 Tesla App / 锁屏小组件时『轻点手机背面截屏』（或 QQ 发 `特斯拉电量 78`），
   `superagent_triage` 自动识别 Model Y 电量 `%` / 表显 `km` 写入 `situation['tesla:main']`，并结合 `OwnTracks` 行车位移自动扣减估算剩余电量；
2. 通道 B（中国区官方 Fleet API 全自动免唤醒）：`https://fleet-api.prd.cn.vn.cloud.tesla.cn`（`siming tesla auth` 自动完成 PKCE 授权与 `refresh_token` 续期），
   配合 iPhone 快捷指令『蓝牙断开车载 Tesla → POST /siming/in/tesla {"event":"parked"}』（无需调 Tesla App 动作！），
   趁下车未休眠瞬间零唤醒拉取精准 `battery_level / battery_range / charging_state`；平时轮询严格先验 `state == 'online'` 绝不强行唤醒休眠车！
3. 车型与补能画像校准：
   - 车型：**Electric Vehicle（默认按 60kWh LFP 标续模型，可在 `policy.yaml` `vehicle` 自定义）**，满电表显约 `435 km`（`4.35 km/%`，冬季暖风实跑按 `3.4 km/%` 保守折算）；
   - 补能习惯：支持无固定家充场景，在外出游玩目的地或常用周边超充站顺路补能；LFP 电池建议定期顺路充至 `100%` 校准 BMS。
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from siming import db as _db_mod
from siming.crypto import read_secret, write_secret
from siming.db import SimingDB, iso_now, logical_today, now_local, parse_iso
from siming.event_chain import get_situation, set_situation
from siming.modules import is_module_enabled

_ORIG_NOW_LOCAL = now_local


def _cur_now():
    return now_local() if now_local is not _ORIG_NOW_LOCAL else _db_mod.now_local()

TESLA_CN_AUTH_URL = "https://auth.tesla.cn/oauth2/v3/authorize"
TESLA_CN_TOKEN_URL = "https://auth.tesla.cn/oauth2/v3/token"
TESLA_CN_FLEET_BASE = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"

MODEL_Y_RWD_KM_PER_PCT = 4.35
MODEL_Y_RWD_WINTER_REAL_KM_PER_PCT = 3.40
# 默认示例常用车库坐标（生产环境请在 ~/.config/siming/policy.yaml `tesla.home_garage_coords` 配置真实坐标）
HOME_GARAGE_COORDS = (39.906500, 116.409500)


def _direct_cn_open(req: urllib.request.Request, timeout: float = 10.0):
    """中国区 *.tesla.cn 强制直连不走境外代理，防止 TLS 抖动与跨洋延迟。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def _to_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        s = str(v).replace("%", "").replace("km", "").replace("公里", "").strip()
        return round(float(s), 1)
    except (TypeError, ValueError):
        return None


def _to_bool(v: Any) -> Optional[bool]:
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "charging", "complete", "stopped", "已插枪", "充电中", "超级充电中", "是"):
        return True
    if s in ("0", "false", "no", "off", "disconnected", "未插枪", "否"):
        return False
    return None


def ingest_tesla_state(
    db: SimingDB,
    raw_payload: str | dict[str, Any],
    source: str = "ios_tesla",
    received_at: Optional[str] = None,
    notify_low_soc: bool = True,
) -> dict[str, Any]:
    """解析来自 `/siming/in/tesla`、Tesla App 截图或中国区 Fleet API 的车辆状态并写入 `situation['tesla:main']`。
    若收到 `{"event": "parked"}`（iPhone 蓝牙断开车载触发），当场趁车辆尚未休眠调用中国区 Fleet API 零唤醒拉取实时电量！
    """
    db.init_db()
    if isinstance(raw_payload, dict):
        data = raw_payload
    else:
        txt = (raw_payload or "").strip()
        if txt.lower() in ("parked", "bluetooth_disconnect", "bt_off"):
            data = {"event": "parked"}
        else:
            try:
                data = json.loads(txt) if txt.startswith("{") else {"soc": txt}
            except Exception:
                data = {"soc": txt}

    soc = _to_float(data.get("soc") if data.get("soc") is not None else (data.get("battery_level") or data.get("battery")))
    range_km = _to_float(data.get("range_km") if data.get("range_km") is not None else data.get("range"))

    # 若仅上报了蓝牙下车事件（无直接电量数值），趁车还醒着立即尝试触发一次中国区 Fleet API 读取
    if soc is None and range_km is None and str(data.get("event") or "").lower() in ("parked", "bluetooth_disconnect", "bt_off"):
        fleet_res = poll_tesla_cn_fleet_if_configured(db, force=True)
        if fleet_res.get("ok") and fleet_res.get("soc_pct") is not None:
            return fleet_res
        return {"ok": True, "parked_ping": True, "fleet_configured": bool(fleet_res.get("configured"))}

    # Model Y 标续 LFP 双向换算（满电表显约 435 km -> 4.35 km/%）
    if range_km is None and soc is not None:
        range_km = round(soc * MODEL_Y_RWD_KM_PER_PCT, 1)
    elif soc is None and range_km is not None:
        soc = round(min(100.0, range_km / MODEL_Y_RWD_KM_PER_PCT), 1)

    chg_state = str(data.get("charging_state") or data.get("charging") or "").strip()
    plugged = _to_bool(data.get("plugged_in") if data.get("plugged_in") is not None else data.get("plugged"))
    if plugged is None and chg_state:
        plugged = chg_state.lower() not in ("disconnected", "nopower", "未连接", "未充电")

    temp_c = _to_float(data.get("inside_temp_c") if data.get("inside_temp_c") is not None else data.get("temp"))
    out_temp_c = _to_float(data.get("outside_temp_c"))
    car_lat = _to_float(data.get("car_lat"))
    car_lng = _to_float(data.get("car_lng"))
    # 坐标保留 6 位小数精度（_to_float 会 round 到 1 位，坐标需单独读取原浮点）
    try:
        if data.get("car_lat") not in (None, ""):
            car_lat = round(float(data["car_lat"]), 6)
        if data.get("car_lng") not in (None, ""):
            car_lng = round(float(data["car_lng"]), 6)
    except (TypeError, ValueError):
        pass
    shift_state = str(data.get("shift_state") or "").strip()
    climate_on = bool(_to_bool(data.get("is_climate_on"))) if data.get("is_climate_on") is not None else False
    ts = received_at or iso_now()

    prev_sit = get_situation(db, "tesla:main")
    prev_val = prev_sit["value"] if prev_sit else {}

    snap = {
        "model": "Model Y 后驱标续(LFP)",
        "soc_pct": soc,
        "range_km": range_km,
        "charging_state": chg_state or ("Charging" if plugged else "Disconnected"),
        "plugged_in": bool(plugged) if plugged is not None else False,
        "inside_temp_c": temp_c if temp_c is not None else prev_val.get("inside_temp_c"),
        "outside_temp_c": out_temp_c if out_temp_c is not None else prev_val.get("outside_temp_c"),
        "is_climate_on": climate_on,
        "shift_state": shift_state,
        "car_lat": car_lat if car_lat is not None else prev_val.get("car_lat"),
        "car_lng": car_lng if car_lng is not None else prev_val.get("car_lng"),
        "estimated": bool(data.get("estimated", False)),
        "updated_at": ts,
        "source": source,
    }
    set_situation(db, "tesla:main", snap, source=source, as_of=ts)

    low_alerted = False
    if notify_low_soc and soc is not None and soc <= 22.0 and not snap["plugged_in"] and is_module_enabled(db, "outing_amap_ev"):
        try:
            ref_dt = parse_iso(ts)
        except Exception:
            ref_dt = _cur_now()
        day_key = logical_today(ref_dt).isoformat()
        if db.get_meta("last_tesla_low_soc_day") != day_key:
            db.set_meta("last_tesla_low_soc_day", day_key)
            from siming.push import push_message

            push_message(
                db,
                text=(
                    f"🔋 **电车低电量提醒**：当前剩余 **{int(soc)}%**（表显约 **{int(range_km or 0)} km**），未插枪。\n"
                    "• 建议近期顺路前往周边超充站或外出目的地补能（LFP 磷酸铁锂电池建议定期充满 `100%` 校准）！"
                ),
                level="normal",
                rule_id="tesla_low_soc",
                ref_id=f"tesla-soc-{day_key}",
                now=ref_dt,
            )
            low_alerted = True

    return {"ok": True, "low_alerted": low_alerted, **snap}


def deduct_tesla_soc_from_driving_km(db: SimingDB, driven_km: float) -> Optional[dict[str, Any]]:
    """当未配 Fleet API 时，若存在已有电量锚点且 `OwnTracks` 检测到驾车位移（>=2.5km），按冬季综合电耗动态扣减预估电量。"""
    if driven_km < 2.5:
        return None
    sit = get_situation(db, "tesla:main")
    if not sit:
        return None
    val = dict(sit["value"])
    if val.get("soc_pct") is None or val.get("plugged_in"):
        return None
    delta_pct = round(driven_km / MODEL_Y_RWD_WINTER_REAL_KM_PER_PCT, 1)
    new_soc = round(max(1.0, float(val["soc_pct"]) - delta_pct), 1)
    return ingest_tesla_state(
        db,
        {"soc": new_soc, "charging_state": "Disconnected", "estimated": True},
        source="owntracks_drive_est",
    )


def _refresh_tesla_cn_token_if_needed(db: SimingDB, cfg: dict[str, Any]) -> str:
    token = str(cfg.get("access_token") or "").strip()
    exp_at = float(cfg.get("expires_epoch") or 0)
    refresh_tok = str(cfg.get("refresh_token") or "").strip()
    client_id = str(cfg.get("client_id") or "").strip()
    if token and (exp_at == 0 or time.time() < exp_at - 180):
        return token
    if not (refresh_tok and client_id):
        return token

    form = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_tok,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TESLA_CN_TOKEN_URL,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with _direct_cn_open(req, timeout=10.0) as resp:
        tok_data = json.loads(resp.read().decode("utf-8"))
    new_acc = str(tok_data.get("access_token") or "").strip()
    if new_acc:
        cfg["access_token"] = new_acc
        if tok_data.get("refresh_token"):
            cfg["refresh_token"] = tok_data["refresh_token"]
        cfg["expires_epoch"] = time.time() + float(tok_data.get("expires_in") or 28800)
        write_secret("tesla-fleet.json", json.dumps(cfg, ensure_ascii=False), db.paths)
        return new_acc
    return token


def poll_tesla_cn_fleet_if_configured(db: SimingDB, force: bool = False, allow_wake: bool = False) -> dict[str, Any]:
    """只读探测中国区 `fleet-api.prd.cn.vn.cloud.tesla.cn`（支持 refresh_token 自动续期）；
    - 默认每 30 分钟最多轮询 1 次且夜间 01:00–08:30 自动静默（月调用 < 900 次 ≈ $1.8，永久处于 $10/月免费额度内）；
    - 当 `force=True`（如 OwnTracks 检测到驾车停稳或蓝牙断开下车）立即触发；
    - 严格先检 `state == 'online'`，除非本人显式发 `唤醒特斯拉` (`allow_wake=True`)，绝不唤醒休眠车辆偷耗驻车电量！
    """
    import os
    from siming.db import parse_iso

    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": True, "skipped": "offline_test"}

    raw_sec = read_secret("tesla-fleet.json", db.paths)
    if not raw_sec:
        return {"ok": False, "configured": False}

    now = _cur_now()
    if not force and not allow_wake:
        if 1 <= now.hour < 8:
            return {"ok": True, "configured": True, "skipped": "night_sleep_window"}
        last_poll = db.get_meta("last_tesla_fleet_poll_at") or ""
        if last_poll:
            try:
                if (now - parse_iso(last_poll)).total_seconds() < 1800:
                    return {"ok": True, "configured": True, "skipped": "interval_30m"}
            except Exception:
                pass

    try:
        cfg = json.loads(raw_sec)
        token = _refresh_tesla_cn_token_if_needed(db, cfg)
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"token_error:{str(exc)[:120]}"}
    if not token:
        return {"ok": False, "configured": False}

    db.set_meta("last_tesla_fleet_poll_at", iso_now(now))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        req = urllib.request.Request(f"{TESLA_CN_FLEET_BASE}/api/1/vehicles", headers=headers)
        with _direct_cn_open(req, timeout=8.0) as resp:
            v_list = (json.loads(resp.read().decode("utf-8")).get("response") or [])
        if not v_list:
            return {"ok": True, "configured": True, "vehicles": 0}
        target_v = v_list[0]
        vid = str(target_v.get("vin") or target_v.get("id_s") or target_v.get("id") or "")
        if vid and len(vid) == 17 and cfg.get("vin") != vid:
            cfg["vin"] = vid
            write_secret("tesla-fleet.json", json.dumps(cfg, ensure_ascii=False), db.paths)
        vin_tail = vid[-4:] if vid else ""
        v_state = str(target_v.get("state") or "").lower()
        if v_state != "online":
            if not allow_wake:
                db.mark_source_ok("ios_tesla", detail=f"VIN*{vin_tail} state={v_state} (sleep-protected)")
                return {"ok": True, "configured": True, "vehicle_state": v_state, "skipped_wake": True, "vin_tail": vin_tail}
            # 本人显式请求或自动备车条件满足时单次唤醒读取
            req_w = urllib.request.Request(
                f"{TESLA_CN_FLEET_BASE}/api/1/vehicles/{vid}/wake_up",
                data=b"{}",
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
            with _direct_cn_open(req_w, timeout=12.0) as _:
                pass
            for _ in range(6):
                time.sleep(3.0)
                with _direct_cn_open(urllib.request.Request(f"{TESLA_CN_FLEET_BASE}/api/1/vehicles", headers=headers), timeout=8.0) as r_chk:
                    vl2 = (json.loads(r_chk.read().decode("utf-8")).get("response") or [])
                if vl2 and str(vl2[0].get("state") or "").lower() == "online":
                    break

        ep_q = urllib.parse.quote("charge_state;climate_state;drive_state;location_data")
        req_d = urllib.request.Request(f"{TESLA_CN_FLEET_BASE}/api/1/vehicles/{vid}/vehicle_data?endpoints={ep_q}", headers=headers)
        with _direct_cn_open(req_d, timeout=10.0) as resp_d:
            vd = (json.loads(resp_d.read().decode("utf-8")).get("response") or {})
        cs = vd.get("charge_state") or {}
        cl = vd.get("climate_state") or {}
        ds = vd.get("drive_state") or {}
        est_km = round(float(cs.get("battery_range") or 0) * 1.60934, 1) if cs.get("battery_range") else None
        ing = ingest_tesla_state(
            db,
            {
                "soc": cs.get("battery_level"),
                "range_km": est_km,
                "charging_state": cs.get("charging_state"),
                "inside_temp_c": cl.get("inside_temp"),
                "outside_temp_c": cl.get("outside_temp"),
                "is_climate_on": cl.get("is_climate_on"),
                "shift_state": ds.get("shift_state"),
                "car_lat": ds.get("latitude"),
                "car_lng": ds.get("longitude"),
            },
            source="tesla_cn_fleet_api",
        )
        db.mark_source_ok("ios_tesla", detail=f"VIN*{vin_tail} soc={ing.get('soc_pct')}% range={ing.get('range_km')}km")
        return ing
    except urllib.error.URLError as exc:
        return {"ok": False, "configured": True, "error": str(exc)[:160]}


def register_and_authorize_tesla_cn_fleet(
    db: SimingDB,
    client_id: str,
    client_secret: str,
    domain: str,
    callback_port: int = 18799,
) -> dict[str, Any]:
    """一键完成 `developer.tesla.cn` Partner 域名注册 + 本机浏览器 PKCE OAuth 授权落盘（0600）。"""
    # 1. Partner Account 注册
    p_form = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid vehicle_device_data",
            "audience": TESLA_CN_FLEET_BASE,
        }
    ).encode("utf-8")
    req_p = urllib.request.Request(
        TESLA_CN_TOKEN_URL,
        data=p_form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req_p, timeout=12.0) as resp_p:
        p_tok = json.loads(resp_p.read().decode("utf-8")).get("access_token", "")
    reg_body = json.dumps({"domain": domain.strip().replace("https://", "").rstrip("/")}).encode("utf-8")
    req_reg = urllib.request.Request(
        f"{TESLA_CN_FLEET_BASE}/api/1/partner_accounts",
        data=reg_body,
        headers={"Authorization": f"Bearer {p_tok}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req_reg, timeout=12.0).read()
    except urllib.error.HTTPError as e:
        if e.code not in (200, 201, 409):
            return {"ok": False, "error": f"partner_accounts 注册失败 HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"}

    # 2. 生成 PKCE 授权链接并在本机监听单次回调
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_hex(12)
    redirect_uri = f"http://localhost:{callback_port}/callback"
    auth_url = f"{TESLA_CN_AUTH_URL}?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid offline_access vehicle_device_data",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print(f"\n👉 请在浏览器打开以下特斯拉中国官方授权链接登录：\n{auth_url}\n（正在监听 {redirect_uri} 回调，限时 5 分钟...）", flush=True)

    got: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if qs.get("code"):
                got["code"] = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>✅ 司命已收到特斯拉中国区授权码，可关闭此页面。</h3>".encode("utf-8"))

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    srv = http.server.HTTPServer(("127.0.0.1", callback_port), _Handler)
    srv.timeout = 300
    srv.handle_request()
    srv.server_close()

    if not got.get("code"):
        return {"ok": False, "error": "未收到授权回调 code"}

    u_form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": got["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "audience": TESLA_CN_FLEET_BASE,
        }
    ).encode("utf-8")
    req_u = urllib.request.Request(
        TESLA_CN_TOKEN_URL,
        data=u_form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req_u, timeout=12.0) as resp_u:
        tok_obj = json.loads(resp_u.read().decode("utf-8"))

    saved = {
        "client_id": client_id,
        "client_secret": client_secret,
        "domain": domain,
        "access_token": tok_obj.get("access_token", ""),
        "refresh_token": tok_obj.get("refresh_token", ""),
        "expires_epoch": time.time() + float(tok_obj.get("expires_in") or 28800),
    }
    write_secret("tesla-fleet.json", json.dumps(saved, ensure_ascii=False), db.paths)
    return {"ok": True, "summary": "✅ 特斯拉中国区官方 Fleet API 已授权并加密落盘（支持自动续期与零唤醒读取）！"}


def check_tesla_range_for_trip(db: SimingDB, one_way_km: float, dest_name: str = "目的地", dest_label: str = "") -> dict[str, Any]:
    """出行前按 EV LFP 标续模型（冬季 1.35x 冗余 + 50km 保底）核算续航，不够时推荐周边或沿途超充。"""
    label = dest_label or dest_name or "目的地"
    sit = get_situation(db, "tesla:main")
    round_trip_km = round(one_way_km * 2.0, 1)
    safe_need_km = round(round_trip_km * 1.35 + 50.0, 1)
    if not sit:
        adv = f"（尚未记录电车当前电量；打开 Tesla App 轻点背面截屏一次，或发 `特斯拉电量 75` 即可激活续航精算）"
        return {"known": False, "round_trip_km": round_trip_km, "advice": adv, "summary": adv}
    val = sit["value"]
    soc = val.get("soc_pct")
    rng = float(val.get("range_km") or 0.0)
    est_tag = "（含里程估算）" if val.get("estimated") else ""
    enough = rng >= safe_need_km
    if enough:
        adv = f"⚡ 电车续航充裕{est_tag}：当前 **{soc}%**（表显约 `{rng:.0f}km`），往返 {label} 约 `{round_trip_km:.0f}km`，无需中途补能。"
    else:
        adv = (
            f"🔌 **建议顺路超充**：当前电车剩余 **{soc}%**{est_tag}（表显 `{rng:.0f}km`），往返 {label} 含冬季暖风安全余量建议 `{safe_need_km:.0f}km`——"
            "可顺路在周边超级充电站或目的地周边补电（LFP 电池可顺手充满 100%）！"
        )
    return {"known": True, "enough": enough, "soc_pct": soc, "range_km": rng, "round_trip_km": round_trip_km, "advice": adv, "summary": adv}


def format_tesla_status(db: SimingDB) -> str:
    sit = get_situation(db, "tesla:main")
    has_fleet = bool(read_secret("tesla-fleet.json", db.paths))
    lines = ["🚗 **司命 · 特斯拉 EV 状态**（新能源免限行 · 周边超充 / 外出顺路充）"]
    if sit:
        v = sit["value"]
        plug_s = "🔌 已插枪/超充中" if v.get("plugged_in") else "🅿️ 未插枪"
        est_s = " *(按行车轨迹自动折算)*" if v.get("estimated") else ""
        tmp_s = f" · 车内 `{v['inside_temp_c']}°C`" if v.get("inside_temp_c") is not None else ""
        loc_s = ""
        if v.get("car_lat") is not None and v.get("car_lng") is not None:
            d_g = _dist_km(float(v["car_lat"]), float(v["car_lng"]), *HOME_GARAGE_COORDS)
            loc_s = " · 📍 常用车库" if d_g <= 0.25 else f" · 📍 外场停放（距常用车库 `{d_g:.1f}km`）"
        lines.append(
            f"• 最新车况：电量 **{v.get('soc_pct')}%** · 表显约 **{v.get('range_km')} km**{est_s} · {plug_s}{tmp_s}{loc_s}（更新于 `{sit['as_of'][:16]}`）"
        )
    else:
        lines.append("• 最新车况：暂未记录初始电量锚点")
    lines.append(
        f"• 数据通道：中国区官方 Fleet API = {'🟢 已接通' if has_fleet else '⚪ 未配（可选执行 siming tesla --auth）'} ｜ "
        "平时打开 Tesla App **轻点手机背面截屏**（或发 `特斯拉电量 80`）即可秒更电量，开车后 `OwnTracks` 会自动按里程扣减估算！"
    )
    return "\n".join(lines)


def submit_tesla_sso_totp(db: SimingDB, passcode: str, port: int = 9222) -> dict[str, Any]:
    """纯代码 0.2s 极速向本机 9222 待命的 Tesla SSO MFA 页面填入 6 位动态码（规避大模型对话延迟导致 30s TOTP 过期）。"""
    tid = (db.get_meta("pending_tesla_totp_tid") or "").strip()
    if not tid:
        return {"ok": False, "reply": "当前没有等待动态码的 Tesla 登录会话。"}
    code = "".join(ch for ch in (passcode or "") if ch.isdigit())
    if len(code) != 6:
        return {"ok": False, "reply": "请输入 6 位数字动态验证码。"}

    from siming.cdp import LocalWsCdpClient, resolve_chromium_cdp_base

    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reply": "未连接到本机 9222 Chromium。"}
    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
    try:
        with LocalWsCdpClient(page_ws, timeout=12.0) as p:
            js = f"""(() => {{
                const inp = document.querySelector('#form-input-passcode');
                const btn = document.querySelector('#form-submit');
                if (!inp || !btn) return 'missing:' + location.href;
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, {json.dumps(code)});
                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                btn.click();
                return 'submitted';
            }})()"""
            p.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
            time.sleep(4.5)
            ev = p.call(
                "Runtime.evaluate",
                {"expression": "JSON.stringify({u: location.href, b: (document.body&&document.body.innerText||'').slice(0,600)})", "returnByValue": True},
            )
            val = json.loads(((ev.get("result") or {}).get("value")) or "{}")
            u = str(val.get("u") or "")
            if "developer.tesla.cn" in u and "auth.tesla.cn" not in u:
                db.set_meta("pending_tesla_totp_tid", "")
                return {"ok": True, "url": u, "reply": "✅ Tesla 动态码校验通过，已成功登录 `developer.tesla.cn`！回到会话说一声即可继续全自动建应用。"}
            return {"ok": False, "url": u, "reply": "⚠️ 动态码未通过或刚过 30 秒窗口，请等验证器跳下一组新 6 位数字再发一次。"}
    except Exception as exc:
        return {"ok": False, "reply": f"填入失败：{str(exc)[:120]}"}


# ---------------------------------------------------------------------------
# EC-5: 出门意图同步 + 五重门控自动备车开空调事件链（tesla_auto_climate）
# ---------------------------------------------------------------------------
VIRTUAL_KEY_PAIRING_URL = "https://tesla.cn/_ak/<your-domain>"


def _resolve_garage_coords(db: SimingDB) -> tuple[float, float]:
    try:
        from siming.config import load_policy
        coords = (load_policy(db.paths).get("tesla") or {}).get("home_garage_coords") or []
        if isinstance(coords, (list, tuple)) and len(coords) == 2:
            return (float(coords[0]), float(coords[1]))
    except Exception:
        pass
    return HOME_GARAGE_COORDS


def _dist_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math

    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def control_tesla_climate(
    db: SimingDB,
    turn_on: bool = True,
    target_temp_c: float = 21.5,
    cli_runner: Optional[Any] = None,
) -> dict[str, Any]:
    """通过官方 Vehicle Command Protocol (`~/.local/bin/tesla-control` ECDSA 私钥端到端签名) 开关电车空调并设定温度。
    若车辆尚未配对虚拟钥匙，自动识别并返回一键配对链接（可在 `policy.yaml` `tesla.virtual_key_pairing_url` 自定义）。
    """
    import os
    import subprocess
    from pathlib import Path
    from siming.crypto import write_secret_file

    if cli_runner is not None:
        res = cli_runner(turn_on=turn_on, target_temp_c=target_temp_c)
        if isinstance(res, dict) and res.get("ok"):
            sit = get_situation(db, "tesla:main")
            if sit:
                v = dict(sit["value"])
                v["is_climate_on"] = bool(turn_on)
                set_situation(db, "tesla:main", v, source="tesla_climate_cmd")
            if not turn_on:
                db.set_meta("tesla_auto_climate_guard_json", "")
        return res

    raw_sec = read_secret("tesla-fleet.json", db.paths)
    if not raw_sec:
        return {"ok": False, "error": "未配置 tesla-fleet.json"}
    cfg = json.loads(raw_sec)
    token = _refresh_tesla_cn_token_if_needed(db, cfg)
    vin = str(cfg.get("vin") or "").strip()
    if not vin:
        p_res = poll_tesla_cn_fleet_if_configured(db, force=True, allow_wake=True)
        cfg = json.loads(read_secret("tesla-fleet.json", db.paths) or "{}")
        vin = str(cfg.get("vin") or "").strip()
        if not vin:
            return {"ok": False, "error": f"未能读取车辆 VIN ({p_res.get('error', '')})"}

    priv_key = db.paths.secrets_dir / "tesla-fleet-private.pem"
    tc_bin = Path.home() / ".local" / "bin" / "tesla-control"
    if not priv_key.is_file() or not tc_bin.is_file():
        return {"ok": False, "error": "缺少 tesla-fleet-private.pem 或 ~/.local/bin/tesla-control"}

    tok_file = db.paths.tmp_root / f"tc-tok-{secrets.token_hex(6)}"
    write_secret_file(tok_file, token)
    env = {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "all_proxy": "",
    }
    base_cmd = [str(tc_bin), "-vin", vin, "-key-file", str(priv_key), "-token-file", str(tok_file), "-command-timeout", "12s"]
    try:
        # 1. 确保车辆处于唤醒态
        subprocess.run([*base_cmd, "wake"], env=env, capture_output=True, text=True, timeout=22)
        # 2. 下发加密签名的空调控制指令
        sub_args = ["climate-on"] if turn_on else ["climate-off"]
        proc = subprocess.run([*base_cmd, *sub_args], env=env, capture_output=True, text=True, timeout=20)
        out_err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        if proc.returncode != 0:
            low_e = out_err.lower()
            if any(k in low_e for k in ("low_battery", "battery_level", "battery too low", "soc too low", "insufficient battery")):
                return {
                    "ok": False,
                    "low_battery_vehicle_limit": True,
                    "error": f"特斯拉车机底层低电量保护（低于 20% 时车辆硬件会拒绝远程开空调）：{out_err[:160]}",
                }
            if any(
                k in low_e
                for k in (
                    "not in whitelist",
                    "not paired",
                    "public key",
                    "virtual key",
                    "key_not_found",
                    "unknown key",
                    "key rejected",
                    "unauthorized",
                    "unregistered",
                )
            ):
                return {
                    "ok": False,
                    "needs_virtual_key": True,
                    "pairing_url": VIRTUAL_KEY_PAIRING_URL,
                    "error": "车辆端尚未添加司命虚拟钥匙",
                }
            return {"ok": False, "error": out_err[:200]}

        if turn_on and target_temp_c:
            subprocess.run([*base_cmd, "climate-set-temp", f"{target_temp_c:.1f}C"], env=env, capture_output=True, text=True, timeout=15)

        sit = get_situation(db, "tesla:main")
        if sit:
            v = dict(sit["value"])
            v["is_climate_on"] = bool(turn_on)
            set_situation(db, "tesla:main", v, source="tesla_climate_cmd")
        if not turn_on:
            db.set_meta("tesla_auto_climate_guard_json", "")
        return {"ok": True, "turn_on": turn_on, "target_temp_c": target_temp_c}
    finally:
        tok_file.unlink(missing_ok=True)


def _strip_voice_prefix(text: str) -> str:
    import re

    return re.sub(r"^\[Voice\]\s*", "", (text or "").strip(), flags=re.IGNORECASE).strip()


def parse_explicit_tesla_climate_command(text: str) -> Optional[dict[str, Any]]:
    """识别主人显式下达的特斯拉备车/开空调/关空调口令（兼容 QQ 语音转写 `[Voice] ...` 与自然口语），
    同时严格过滤问句、报错/修复讨论、未来定时安排、模块开关与室内空调语句，避免误触发。"""
    import re

    clean = _strip_voice_prefix(text)
    if not clean or len(clean) > 120 or "\n" in clean:
        return None
    bare = re.sub(r"[。！!~～\s]+$", "", clean).strip()
    if not bare:
        return None

    # 排除问句、排障/复盘描述、提醒语、模块开关及家里/新房空调
    if re.search(
        r"[?？]|(?:备车|开空调|空调|暖车).{0,4}(?:吗|呢|了没|没开|开了没|了吗)"
        r"|(?:为什么|为啥|怎么|能否|能不能|可不可以|要不要|是不是|有没有).{0,12}(?:备车|开空调|打开空调|关空调|关闭空调|暖车)"
        r"|(?:被拒绝|被拒|没成功|失败了?|修复|提醒我|别忘了|模块|开关)"
        r"|(?:已经(?:开始|开启|打开)?(?:备车|开空调|暖车))"
        r"|(?:家里|客厅|卧室|书房|新房|房间|haos|米家).{0,6}空调",
        bare,
        re.IGNORECASE,
    ):
        return None

    # 1) 显式关空调 / 取消备车
    if re.fullmatch(
        r"(?:(?:帮我|替我|给我|现在|马上|直接|先)?(?:关空调|关闭空调|关一下空调|停空调|特斯拉关空调|关车空调|关一下车空调|关掉空调|关掉车空调|"
        r"把(?:车|特斯拉)?(?:里|内)?(?:的)?空调(?:关掉|关闭|关了|关一下|停了)))(?:吧|呀|啊|哈|一下)?",
        bare,
        re.IGNORECASE,
    ):
        return {"action": "off", "soft_cancel": False}
    if re.fullmatch(
        r"(?:(?:先|暂)?(?:取消备车|停止备车|停掉备车|不用备车了?|别备车了?|不备车了?|不要备车了?))(?:吧|呀|啊|哈)?",
        bare,
    ):
        return {"action": "off", "soft_cancel": True}

    # 2) 排除对备车/开空调/暖车的显式否定（注意保留『别管电量，开始备车』这类否定前置词而非否定动词本身的情况）
    if re.search(
        r"(?:不要|别|不用|不必|无需|暂不|先不|不|停止|取消|关闭|关掉|暂停)\s*(?:再|要|用)?(?:自动)?(?:开始|给我|帮我|替我)?(?:备车|开空调|打开空调|开启空调|暖车|预热|制冷)",
        bare,
    ):
        return None

    # 排除明确的未来预约时间（如『明天8点备车』『下午3点备车』），除非显式说了『现在/马上/立即』
    if re.search(r"(?:明天|后天|今晚|明早|下周|\d{1,2}\s*[点:：]\s*(?:半|\d{1,2})?)", bare) and not re.search(
        r"(?:现在|马上|立即|立刻)", bare
    ):
        return None

    # 3) 显式备车 / 开空调 / 暖车指令（支持『备车 22.5』『开始备车』『我需要去的地方，电量是够的，你开始备车。』等）
    m_bare = re.match(
        r"^(?:开空调|打开空调|开启空调|特斯拉开空调|开一下空调|备车|开始备车|立即备车|现在备车|直接备车|强制备车|暖车|开始暖车|预热车?)(?:\s*(\d{2}(?:\.\d)?)\s*(?:度|°c|℃|c)?)?$",
        bare,
        re.IGNORECASE,
    )
    matched_on = bool(m_bare)
    target_temp: Optional[float] = None
    if m_bare and m_bare.group(1):
        try:
            t_v = float(m_bare.group(1))
            if 15.0 <= t_v <= 28.0:
                target_temp = t_v
        except ValueError:
            pass

    if not matched_on:
        has_precond_cmd = bool(
            re.search(
                r"(?:开始|帮我|替我|给我|请你?|你|现在|马上|立即|立刻|直接|强制|强行|继续|照常|还是|先|提前)\s*(?:给我|帮我|替我|先|提前)?(?:开始)?(?:备车|暖(?:一下|下|一暖)?车|预热(?:一下)?(?:车|车辆|特斯拉)?)"
                r"|(?:(?:把|给)(?:车|特斯拉)(?:提前)?(?:预热|暖一下|暖一暖|降温))"
                r"|(?:^|[，,、\s])(?:开始)?(?:备车|暖(?:一下|下|一暖)?车)(?:吧|呀|啊|咯)?$",
                bare,
            )
        )
        has_car_ac_cmd = bool(
            re.search(
                r"(?:帮我|替我|给我|请你?|你|现在|马上|立即|立刻|直接|先|提前|把(?:车|特斯拉)?(?:里|内)?(?:的)?)?(?:打开|开启|开一下|开个|开)(?:特斯拉|Model\s*Y|车里?|车内)?(?:的)?(?:空调|暖风|冷风)"
                r"|(?:给(?:车|特斯拉|Model\s*Y)|把(?:车|特斯拉|Model\s*Y)(?:里|内)?)(?:的)?(?:空调打开|空调开启|空调开一下|开空调|开暖风|开冷风|制热|制冷|暖一下|降温)",
                bare,
                re.IGNORECASE,
            )
        )
        matched_on = has_precond_cmd or has_car_ac_cmd

    if not matched_on:
        return None

    if target_temp is None:
        m_temp = re.search(r"(?:调到|设到|设为|设定|开到|温度)?\s*(\d{2}(?:\.\d)?)\s*(?:度|°c|℃)", bare, re.IGNORECASE)
        if m_temp:
            try:
                t_v = float(m_temp.group(1))
                if 15.0 <= t_v <= 28.0:
                    target_temp = t_v
            except ValueError:
                pass

    return {"action": "on", "target_temp_c": target_temp, "override_low_soc": True}


def is_low_soc_override_followup(
    db: SimingDB,
    text: str,
    reply_to_text: Optional[str] = None,
    now: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """当刚触发『自动备车已跳过省电』（30 分钟内）或用户引用了跳过提示并回复『电量是够的 / 够用 / 开吧 / 确认』时，识别为人工放行备车。"""
    import re
    from siming.db import parse_iso

    clean = _strip_voice_prefix(text)
    if not clean or len(clean) > 100 or "\n" in clean:
        return None
    bare = re.sub(r"[。！!~～\s]+$", "", clean).strip()
    if not bare:
        return None

    # 排除问句、否定、排障反馈
    if re.search(
        r"[?？]|(?:吗|呢)$|(?:不够|不足|不用|不要|别开|不开|算了|取消|被拒绝|失败|修复|为什么|怎么)",
        bare,
    ):
        return None

    now_dt = now or _cur_now()
    quoted_low_soc = bool(
        reply_to_text and ("自动备车已跳过省电" in reply_to_text or "备车安全线" in reply_to_text)
    )
    ctx_obj: Optional[dict[str, Any]] = None
    raw_ctx = db.get_meta("tesla_low_soc_override_ctx") or ""
    if raw_ctx:
        try:
            parsed_ctx = json.loads(raw_ctx)
            created_dt = parse_iso(str(parsed_ctx.get("created_at") or ""))
            if 0 <= (now_dt - created_dt).total_seconds() <= 1800:
                ctx_obj = parsed_ctx
        except Exception:
            ctx_obj = None

    if not quoted_low_soc and not ctx_obj:
        return None

    is_enough_reply = bool(
        re.search(
            r"(?:电量.{0,6}(?:是?够|足够|充足|够用|没问题|可以)|(?:够用|够电|电够)(?:的|了)?|(?:还是|照常|继续)(?:开|开吧|暖车|备车))",
            bare,
        )
    )
    is_quoted_confirm = quoted_low_soc and bool(
        re.fullmatch(r"(?:确认|开|开吧|开启|备车|继续|行|好的?|可以|没问题)", bare)
    )
    if is_enough_reply or is_quoted_confirm:
        return ctx_obj or {"dest": "外出用车"}
    return None


def start_manual_tesla_precondition(
    db: SimingDB,
    target_temp_c: Optional[float] = None,
    dest_hint: str = "",
    override_reason: str = "",
    now: Optional[Any] = None,
    climate_runner: Optional[Any] = None,
) -> dict[str, Any]:
    """执行主人明确下达的手动备车/开空调指令：
    - 始终尊重本人显式决策，不因 25% 自动省电安全线或 90 分钟自动冷却窗而拒绝；
    - 未指定温度时按当前车内温度自动选择制热(21.5°C)或制冷(22.0°C)；
    - 同步写入 18 分钟未挂挡自动关停保护并清理低电量跳过上下文，防后续 OwnTracks 离家重复触发。
    """
    db.init_db()
    now_dt = now or _cur_now()
    sit = get_situation(db, "tesla:main")
    car_val = dict(sit["value"]) if sit else {}
    in_temp = car_val.get("inside_temp_c")
    soc = car_val.get("soc_pct")
    rng_km = car_val.get("range_km")

    if target_temp_c is None:
        is_hot = in_temp is not None and float(in_temp) >= 26.0
        eff_temp = 22.0 if is_hot else 21.5
        mode_icon = "❄️" if is_hot else "🔥"
        mode_word = "制冷降温" if is_hot else "制热暖车"
    else:
        eff_temp = round(float(target_temp_c), 1)
        is_hot = in_temp is not None and float(in_temp) >= eff_temp + 3.0
        mode_icon = "❄️" if is_hot else "🔥"
        mode_word = "制冷降温" if is_hot else "空调备车"

    c_res = control_tesla_climate(db, turn_on=True, target_temp_c=eff_temp, cli_runner=climate_runner)
    if not c_res.get("ok"):
        if c_res.get("needs_virtual_key"):
            return {
                "ok": False,
                "action": "needs_virtual_key",
                "pairing_url": VIRTUAL_KEY_PAIRING_URL,
                "reply": f"🔑 需在手机点一次官方虚拟钥匙配对链接加入 Model Y 白名单（仅首次 1 次）：\n{VIRTUAL_KEY_PAIRING_URL}",
            }
        return {
            "ok": False,
            "action": "command_failed",
            "error": c_res.get("error", ""),
            "reply": f"⚠️ 开启空调失败：{c_res.get('error', '')}",
        }

    ts_iso = iso_now(now_dt)
    db.set_meta("last_tesla_auto_climate_at", ts_iso)
    db.set_meta("tesla_low_soc_override_ctx", "")
    db.set_meta("last_climate_low_soc_key", "")
    db.set_meta(
        "tesla_auto_climate_guard_json",
        json.dumps(
            {"started_at": ts_iso, "dest": dest_hint or "手动备车", "target_c": eff_temp, "manual": True},
            ensure_ascii=False,
        ),
    )

    detail_parts: list[str] = []
    if in_temp is not None:
        detail_parts.append(f"车内 `{float(in_temp):.1f}°C` → 目标 **`{eff_temp:.1f}°C`**")
    else:
        detail_parts.append(f"目标温度 **`{eff_temp:.1f}°C`**")
    if soc is not None:
        rng_s = f"（表显约 `{float(rng_km):.0f} km`）" if rng_km is not None else ""
        detail_parts.append(f"剩余电量 **`{float(soc):.0f}%`**{rng_s}")

    low_note = ""
    if soc is not None and float(soc) < 25.0:
        low_note = "\n• ⚡ 已按你的明确指令跳过 `25%` 自动省电门限直接备车（行程结束后记得顺路补能）。"
    elif override_reason:
        low_note = f"\n• {override_reason}"

    reply = (
        f"{mode_icon} **已按指令开启 Model Y {mode_word}**\n"
        f"• {' ｜ '.join(detail_parts)}"
        f"{low_note}\n"
        "• 不开车时点下方按钮或回 `关空调` 立即关闭（18 分钟未挂挡发车也会自动关停省电）。"
    )
    return {
        "ok": True,
        "action": "climate_started",
        "manual": True,
        "target_temp_c": eff_temp,
        "soc_pct": soc,
        "reply": reply,
    }


def sync_mobile_outing_plan(
    db: SimingDB,
    text: str,
    source: str = "manual_qq",
    now: Optional[Any] = None,
    climate_runner: Optional[Any] = None,
) -> dict[str, Any]:
    """解析主人从手机同步的出门/行程信息（如『我准备出门去新房了』『下午3点去山姆』『一会开车去大钟寺充电』）：
    1. 自动区分『走路/地铁/打车』与『开车出门』；
    2. 写入 `expectations(kind='outing_drive')` 与 `situation['outing:active']`；
    3. 若判断为即将出门（<=12m）且满足温度不适+电量充足（或本人明确说明电量够/要求备车），当场触发备车开空调！
    """
    import re
    from datetime import timedelta
    from siming.cn_calendar import try_parse_explicit_datetime
    from siming.event_chain import upsert_expectation

    clean = _strip_voice_prefix(text)
    if not clean:
        return {"matched": False}
    # 多行截屏/群聊记录绝不是「我现在要开车去某地」直达出门指令，防止误吞约局截图！
    if len([ln for ln in clean.splitlines() if ln.strip()]) >= 3:
        return {"matched": False}
    if re.search(
        r"(提醒我买|去医院的前一天|什么时候去|要不要去|不想去|被拒绝|修复|一般是|每周|每月|目标是|指的是|预约去)",
        clean,
    ):
        return {"matched": False}

    # 识别典型出门/前往表达（排除查账、提醒我买东西、聊天气泡截屏与跨句规则讨论等非出行句）
    is_imminent = bool(re.search(r"(我?(?:现在|准备|马上|这就|一会|待会|刚)?(?:要)?(?:出门|下楼|出发|动身|去开车))", clean))
    m_go = re.search(
        r"(?:出门去|出发去|开车去|自驾去|准备去|一会去|待会去|马上去|坐地铁去|打车去|步行去|走路去)\s*([^\s，,。！？!?;；]{2,20})",
        clean,
    )
    if not m_go:
        for seg in re.split(r"[。！？!?;\n]+", clean):
            seg = seg.strip()
            if not seg:
                continue
            has_time_or_move_ctx = bool(
                is_imminent
                or re.search(
                    r"(?:今天|明天|后天|今晚|下午|上午|早上|晚上|中午|一会|待会|\d{1,2}\s*[:：点]|打车|滴滴|地铁|开车|自驾|走路|步行)",
                    seg,
                )
            )
            if has_time_or_move_ctx:
                m_go = re.search(r"(?<![需想说约])去\s*([^\s，,。！？!?;；]{2,20})", seg)
                if m_go:
                    break
    if not is_imminent and not m_go:
        return {"matched": False}

    raw_dest = (m_go.group(1) if m_go else "").strip()
    raw_dest = re.sub(r"(?:了|啦|呢|吧|哈|呀|吃饭|看电影|买东西|逛街|办事|充电)$", "", raw_dest).strip() or raw_dest
    invalid_dest_re = re.compile(
        r"^(?:的地方|地方|哪|哪里|哪儿|什么地方|什么|了就|了|一下|看看|干嘛|干啥|做什么|车里|车上|车内|开车|取车)$"
    )
    if raw_dest and invalid_dest_re.match(raw_dest):
        if not is_imminent:
            return {"matched": False}
        dest = "取车出发" if raw_dest in ("车里", "车上", "车内", "开车", "取车") else "外出办事"
    elif not is_imminent and not raw_dest:
        return {"matched": False}
    else:
        dest = raw_dest or "外出办事"

    explicit_non_drive = bool(re.search(r"(打车|滴滴|坐地铁|地铁|步行|走路|骑车|散步|取快递|扔垃圾)", clean))
    explicit_drive = bool(re.search(r"(开车|自驾|特斯拉|超充|充电|去新房|山姆|宜家|机场|南站|车里|车库|取车|备车)", clean))
    override_low_soc = bool(
        re.search(
            r"(?:电量.{0,6}(?:是?够|足够|充足|够用|没问题)|够电|强制备车|照常备车|继续备车|开始备车)",
            clean,
        )
        and not re.search(r"(?:电量不够|电量不足|别备车|不用备车|不要备车)", clean)
    )
    # 步行通勤期日常去『公司』默认步行，除非显式说『开车去公司』
    if dest in ("公司", "示例写字楼", "上班") and not explicit_drive:
        driving = False
    elif explicit_non_drive and not explicit_drive:
        driving = False
    else:
        driving = True

    now_dt = now or _cur_now()
    parsed = None
    try:
        parsed = try_parse_explicit_datetime(clean, ref_dt=now_dt, paths=db.paths)
    except Exception:
        parsed = None

    depart_dt = parsed[0] if parsed else now_dt
    # 若写的是到达时间（如『14:30到新房』），预留 35 分钟车程作为预计出门时刻
    if parsed and re.search(r"到$", clean) and depart_dt > now_dt + timedelta(minutes=40):
        depart_dt = depart_dt - timedelta(minutes=35)

    depart_iso = iso_now(depart_dt)
    plan_obj = {
        "dest": dest,
        "depart_at": depart_iso,
        "driving": driving,
        "raw": clean[:120],
        "synced_at": iso_now(now_dt),
    }
    set_situation(db, "outing:active", plan_obj, source=source, as_of=iso_now(now_dt))

    exp_id = ""
    if driving:
        exp_id = upsert_expectation(
            db,
            kind="outing_drive",
            chain_type="habit",
            title=f"开车出门前往 {dest}",
            target_at=depart_iso,
            flex_before_min=25,
            flex_after_min=60,
            place=dest,
            visibility="high",
            prepare_fn="tesla_link.evaluate_departure_auto_climate",
            counterparty={"driving": True, "dest": dest, "raw": clean[:120]},
            ttl_days=2,
        )

    mins_until = (depart_dt - now_dt).total_seconds() / 60.0
    climate_eval = None
    if driving and -15.0 <= mins_until <= 12.0:
        climate_eval = evaluate_departure_auto_climate(
            db,
            departed_now=True,
            trigger="mobile_sync_imminent",
            now=now_dt,
            climate_runner=climate_runner,
            override_low_soc=override_low_soc,
            notify=(source != "manual_qq"),
        )

    if not driving:
        reply = f"🚶 已记下出门动向：**{dest}**（识别为步行/公共交通，不唤醒特斯拉空调）。"
    elif climate_eval and climate_eval.get("action") == "climate_started":
        reply = climate_eval["message"]
    elif climate_eval and climate_eval.get("message"):
        reply = f"🚗 已同步出门行程：**前往 {dest}**\n{climate_eval['message']}"
    else:
        hm = depart_iso[11:16]
        reply = (
            f"🗓️ **已接入出门备车事件链**：预计 `{hm}` 自驾前往 **{dest}**\n"
            "• 当 `OwnTracks` 检测到你出门（或临出发前），只要车内过冷(`≤12°C`)/过热(`≥29°C`)且电量充足(`≥25%`)，我会自动打开 Model Y 空调！"
        )
    return {"matched": True, "driving": driving, "dest": dest, "depart_at": depart_iso, "exp_id": exp_id, "climate": climate_eval, "reply": reply}


def evaluate_departure_auto_climate(
    db: SimingDB,
    departed_now: bool = False,
    depart_place: str = "",
    trigger: str = "tick",
    now: Optional[Any] = None,
    climate_runner: Optional[Any] = None,
    override_low_soc: bool = False,
    notify: bool = True,
) -> dict[str, Any]:
    """EC-5 五重门控自动备车开空调事件链：
    0. 安全兜底：若此前自动开启过空调且满 18 分钟仍处于驻车未发车状态（P档/未挂挡），自动关停空调防忘关耗电；
    1. 模块与冷却窗：`tesla_auto_climate` 开启且距上次自动开空调 >= 90 分钟；
    2. 开车意图核验：有手机同步的驾车 `outing_drive` 预期处于出发窗口（或车停在外场且检测到主人返程走近车辆），严格排除示例小区A↔公司步行通勤；
    3. 电量安全门控：`soc_pct >= 25%`（已插枪或目的地含『充』放宽至 `>= 20%`）；
    4. 车内温度不适门控：`inside_temp_c <= 12.0°C`（冬季制热至 `21.5°C`）或 `>= 29.0°C`（夏季制冷至 `22.0°C`）。
    """
    import os
    from datetime import timedelta
    from siming.cn_calendar import day_info
    from siming.config import load_policy
    from siming.db import parse_iso
    from siming.modules import is_module_enabled
    from siming.push import push_message

    db.init_db()
    now_dt = now or _cur_now()

    # 0) 18 分钟未发车自动关停保护（防止下楼拿完快递又回屋导致空调白吹半小时）
    guard_raw = db.get_meta("tesla_auto_climate_guard_json") or ""
    if guard_raw:
        try:
            g_obj = json.loads(guard_raw)
            started_dt = parse_iso(str(g_obj["started_at"]))
            age_min = (now_dt - started_dt).total_seconds() / 60.0
            if age_min >= 18.0:
                if os.environ.get("SIMING_TEST_OFFLINE") != "1" and climate_runner is None:
                    poll_tesla_cn_fleet_if_configured(db, force=True, allow_wake=False)
                sit_now = get_situation(db, "tesla:main")
                sh = str((sit_now["value"] if sit_now else {}).get("shift_state") or "")
                if sh not in ("D", "R", "N"):
                    off_res = control_tesla_climate(db, turn_on=False, cli_runner=climate_runner)
                    if off_res.get("ok"):
                        db.set_meta("tesla_auto_climate_guard_json", "")
                        push_message(
                            db,
                            text="🛡️ **自动备车安全省电保护**：开启空调已满 18 分钟仍未检测到挂挡发车，已自动为你关闭 Model Y 空调。",
                            rule_id="tesla_auto_climate",
                            ref_id=f"climate-autostop-{now_dt.strftime('%Y%m%d%H')}",
                            level="normal",
                            now=now_dt,
                        )
                        return {"ok": True, "action": "auto_shutoff_18m"}
                else:
                    db.set_meta("tesla_auto_climate_guard_json", "")
        except Exception:
            db.set_meta("tesla_auto_climate_guard_json", "")

    if not is_module_enabled(db, "tesla_auto_climate"):
        return {"ok": True, "skipped": "module_disabled"}

    # 1) 90 分钟冷却防重（本人显式放行低电量备车时不阻塞）
    last_on = db.get_meta("last_tesla_auto_climate_at") or ""
    if last_on and not override_low_soc:
        try:
            if 0 <= (now_dt - parse_iso(last_on)).total_seconds() < 5400:
                return {"ok": True, "skipped": "cooldown_90m"}
        except Exception:
            pass

    # 2) 判定『出门 + 时间看起来是要开车做什么』
    driving_dest = ""
    matched_exp_id = ""
    sit_out = get_situation(db, "outing:active")
    if sit_out:
        ov = sit_out["value"]
        if ov.get("driving") is False:
            # 明确说了步行/地铁/打车且在 90 分钟内 -> 绝不误开空调！
            try:
                if abs((now_dt - parse_iso(str(ov["depart_at"]))).total_seconds()) <= 5400:
                    return {"ok": True, "skipped": "explicit_non_driving_outing"}
            except Exception:
                pass
        elif ov.get("driving") is True:
            try:
                dep_dt = parse_iso(str(ov["depart_at"]))
                diff_min = (dep_dt - now_dt).total_seconds() / 60.0
                # 若 OwnTracks 已检测到物理出门：允许提前 60m 到延后 90m 窗口；若仅定时节拍：在出发前 10m 内预备
                if (departed_now and -90.0 <= diff_min <= 60.0) or (not departed_now and -5.0 <= diff_min <= 10.0 and trigger != "tick"):
                    driving_dest = str(ov.get("dest") or "外出目的地")
            except Exception:
                pass

    if not driving_dest:
        conn = db.connect()
        try:
            rows = conn.execute(
                """
                SELECT exp_id, kind, title, place, target_at, prepared_json, counterparty_json
                FROM expectations
                WHERE status IN ('ACTIVE', 'SHIFTED')
                  AND kind IN ('outing_drive', 'outing', 'hospital_visit', 'install')
                ORDER BY target_at ASC
                """
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            try:
                cp_r = json.loads(r["counterparty_json"] or "{}")
                if cp_r.get("no_travel"):
                    continue
                prep_r = json.loads(r["prepared_json"] or "{}")
                t_dt = parse_iso(str(r["target_at"]))
                # 优先采用临出行路况算出的真实建议出发时刻；否则医院/新房/外出按提前 40 分钟估算
                if prep_r.get("leave_at"):
                    dep_est = parse_iso(str(prep_r["leave_at"]))
                else:
                    dep_est = t_dt - timedelta(minutes=40) if r["kind"] in ("outing", "hospital_visit", "install") else t_dt
                diff_m = (dep_est - now_dt).total_seconds() / 60.0
                if departed_now and -75.0 <= diff_m <= 50.0:
                    driving_dest = str(r["place"] or r["title"])
                    matched_exp_id = str(r["exp_id"])
                    break
            except Exception:
                continue

    sit_car = get_situation(db, "tesla:main")
    car_val = dict(sit_car["value"]) if sit_car else {}

    # 返程与走往车库智能识别：
    # - 场景 A（外场返程）：若车当前停在常用车库以外（距车库 > 0.6km，且距家/公司 > 0.8km），且检测到主人在车附近出门移动 -> 返程开车！
    # - 场景 B（直奔常用车库）：若车停在常用车库内，且 OwnTracks 检测到离开家/公司并走入车库 110m 圈内 -> 去车库取车！
    if not driving_dest and departed_now and car_val.get("car_lat") is not None and car_val.get("car_lng") is not None:
        clat, clng = float(car_val["car_lat"]), float(car_val["car_lng"])
        garage_coords = _resolve_garage_coords(db)
        d_garage = _dist_km(clat, clng, *garage_coords)
        d_home = _dist_km(clat, clng, 39.904200, 116.407400)
        d_office = _dist_km(clat, clng, 39.908000, 116.407000)
        raw_loc = db.get_meta("latest_ios_location") or ""
        try:
            l_obj = json.loads(raw_loc) if raw_loc else {}
        except Exception:
            l_obj = {}
        u_lat, u_lng = l_obj.get("lat"), l_obj.get("lng")
        if d_garage >= 0.6 and d_home >= 0.8 and d_office >= 0.8 and 8 <= now_dt.hour <= 23:
            if u_lat is not None and u_lng is not None and _dist_km(clat, clng, float(u_lat), float(u_lng)) <= 0.8:
                driving_dest = "外场返程"
        elif d_garage <= 0.25 and u_lat is not None and u_lng is not None:
            u_to_garage = _dist_km(float(u_lat), float(u_lng), *garage_coords)
            if u_to_garage <= 0.11 and 8 <= now_dt.hour <= 23:
                driving_dest = "车库取车出发"

    # 防误判铁律：步行通勤期工作日早晚常规上下班，无明确开车行程一律静默跳过！
    if not driving_dest:
        di = day_info(now_dt.date(), db.paths)
        if di.is_workday and depart_place in ("家", "公司", ""):
            return {"ok": True, "skipped": "no_driving_plan_workday_walk_guard"}
        return {"ok": True, "skipped": "no_synced_driving_outing"}

    # 3) 若车辆快照陈旧（>35m）且配置了真实 Fleet API，先唤醒刷新一次实时温度与电量
    if os.environ.get("SIMING_TEST_OFFLINE") != "1" and climate_runner is None:
        need_fresh = not sit_car
        if sit_car:
            try:
                need_fresh = (now_dt - parse_iso(str(sit_car["as_of"]))).total_seconds() > 2100
            except Exception:
                need_fresh = True
        if need_fresh:
            poll_tesla_cn_fleet_if_configured(db, force=True, allow_wake=True)
            sit_car = get_situation(db, "tesla:main")
            car_val = dict(sit_car["value"]) if sit_car else {}

    if not car_val or car_val.get("soc_pct") is None:
        return {"ok": False, "skipped": "unknown_vehicle_state"}
    if car_val.get("is_climate_on"):
        return {"ok": True, "skipped": "climate_already_on"}
    if str(car_val.get("shift_state") or "") in ("D", "R", "N"):
        return {"ok": True, "skipped": "already_driving"}

    # 4) 电量安全门控（默认 >=25%，插枪状态或目的地去充电放宽至 >=20%）
    pol = load_policy(db.paths)
    v_cfg = pol.get("vehicle") or {}
    min_soc = float(v_cfg.get("auto_climate_min_soc_pct", 25.0))
    if car_val.get("plugged_in") or ("充" in driving_dest):
        min_soc = 20.0
    soc = float(car_val["soc_pct"])
    in_temp = car_val.get("inside_temp_c")

    if soc < min_soc and not override_low_soc:
        skip_key = f"low_soc_skip:{logical_today(now_dt).isoformat()}:{driving_dest}"
        rng_km = float(car_val.get("range_km") or 0.0)
        rng_part = (
            f"（表显约 `{rng_km:.0f} km`，低于 `{min_soc:.0f}%` 自动备车安全线）"
            if rng_km > 0
            else f"（低于 `{min_soc:.0f}%` 自动备车安全线）"
        )
        msg = (
            f"🔋 **自动备车已跳过省电**：检测到你准备开车前往 **{driving_dest}**"
            + (f"（车内 `{in_temp}°C`）" if in_temp is not None else "")
            + f"，但 Model Y 当前剩余 **{soc:.0f}%** 电量{rng_part}，暂未自动开空调。\n"
            "• 若行程较近电量够用，点下方按钮或直接回 **「开始备车」** 立即开空调（建议顺路补能）。"
        )
        db.set_meta(
            "tesla_low_soc_override_ctx",
            json.dumps(
                {
                    "created_at": iso_now(now_dt),
                    "dest": driving_dest,
                    "soc_pct": soc,
                    "min_soc": min_soc,
                    "inside_temp_c": in_temp,
                },
                ensure_ascii=False,
            ),
        )
        if notify and db.get_meta("last_climate_low_soc_key") != skip_key:
            db.set_meta("last_climate_low_soc_key", skip_key)
            push_message(db, text=msg, rule_id="tesla_auto_climate", ref_id=skip_key, level="normal", now=now_dt)
        return {"ok": True, "action": "low_soc_skipped", "soc_pct": soc, "min_soc": min_soc, "message": msg}

    # 5) 车内温度不适门控（<=12°C 制热至 21.5°C；>=29°C 制冷至 22.0°C）
    cold_th = float(v_cfg.get("auto_climate_cold_c", 12.0))
    hot_th = float(v_cfg.get("auto_climate_hot_c", 29.0))
    if in_temp is None:
        return {"ok": True, "skipped": "unknown_inside_temp"}
    in_t = float(in_temp)
    if cold_th < in_t < hot_th:
        return {
            "ok": True,
            "skipped": "cabin_temp_comfortable",
            "inside_temp_c": in_t,
            "message": f"• 当前车内 `{in_t:.1f}°C` 体感适宜，无需提前开空调。",
        }

    is_cold = in_t <= cold_th
    target_c = 21.5 if is_cold else 22.0
    mode_icon = "🔥" if is_cold else "❄️"
    mode_word = "制热暖车" if is_cold else "制冷降温"

    cmd_res = control_tesla_climate(db, turn_on=True, target_temp_c=target_c, cli_runner=climate_runner)
    if not cmd_res.get("ok"):
        if cmd_res.get("needs_virtual_key"):
            vk_msg = (
                f"🔑 **差最后一步激活自动开空调**：司命已检测到你出门前往 **{driving_dest}**（车内 `{in_t:.1f}°C` · 电量 `{soc:.0f}%`），"
                f"但你的 Model Y 车机尚未添加司命虚拟钥匙。\n"
                f"👉 请在手机浏览器或微信点一次官方配对链接自动跳 Tesla App 确认（仅需配对 1 次，终身生效）：\n"
                f"{VIRTUAL_KEY_PAIRING_URL}"
            )
            if notify and db.get_meta("notified_tesla_vk_pair") != now_dt.date().isoformat():
                db.set_meta("notified_tesla_vk_pair", now_dt.date().isoformat())
                push_message(db, text=vk_msg, rule_id="tesla_auto_climate", ref_id="tesla-vk-pair", level="now", now=now_dt)
            return {"ok": False, "action": "needs_virtual_key", "pairing_url": VIRTUAL_KEY_PAIRING_URL, "message": vk_msg}
        return {"ok": False, "action": "command_failed", "error": cmd_res.get("error")}

    ts_iso = iso_now(now_dt)
    db.set_meta("last_tesla_auto_climate_at", ts_iso)
    db.set_meta("tesla_low_soc_override_ctx", "")
    db.set_meta("last_climate_low_soc_key", "")
    db.set_meta(
        "tesla_auto_climate_guard_json",
        json.dumps({"started_at": ts_iso, "dest": driving_dest, "target_c": target_c}, ensure_ascii=False),
    )
    if matched_exp_id:
        with db.transaction() as c:
            c.execute("UPDATE expectations SET status='PREPARED', updated_at=? WHERE exp_id=?", (ts_iso, matched_exp_id))

    low_override_line = (
        f"\n• ⚡ 已按你的明确指令跳过 `{min_soc:.0f}%` 自动省电门限直接备车（行程结束后记得顺路补能）。"
        if (override_low_soc and soc < min_soc)
        else ""
    )
    msg = (
        f"{mode_icon} **[自动备车已开启{mode_word}]** 检测到你出门前往 **{driving_dest}**\n"
        f"• 触发条件：车内 `{in_t:.1f}°C` → 目标 **`{target_c:.1f}°C`** ｜ 剩余电量 **`{soc:.0f}%`**（`{car_val.get('range_km', 0):.0f} km`）"
        f"{low_override_line}\n"
        "• 若临时改主意不开车，直接回 **「关空调」** 立即关闭（18 分钟未挂挡发车也会自动关停省电）。"
    )
    if notify:
        push_message(
            db,
            text=msg,
            rule_id="tesla_auto_climate",
            ref_id=f"climate-on-{now_dt.strftime('%Y%m%d%H%M')}",
            level="now",
            now=now_dt,
        )
    return {
        "ok": True,
        "action": "climate_started",
        "mode": "heat" if is_cold else "cool",
        "inside_temp_c": in_t,
        "target_temp_c": target_c,
        "soc_pct": soc,
        "dest": driving_dest,
        "message": msg,
    }
