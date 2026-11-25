"""主动求证引擎（Directive #3 · curiosity.py）：
当司命对主人的新停留地点、习惯突变或陌生周期扣款不理解时，主动生成低负担的一句话求证问题，
并在主人随口回复后当场写入 `saved_places` / `routines.md` / 偏好记忆，越用越懂你。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Optional

from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.ledger.reimburse import load_saved_places, set_saved_place


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


# 默认示例地理锚点（生产环境优先从 policy.yaml `daily_care.regimes` 与 `tesla.home_garage_coords` 动态读取）
KNOWN_ANCHORS: list[tuple[str, float, float, float]] = [
    ("家(示例小区A)", 39.904200, 116.407400, 280.0),
    ("公司(示例写字楼)", 39.908000, 116.407000, 280.0),
    ("车库", 39.906500, 116.409500, 180.0),
    ("新房(示例小区B)", 39.860000, 116.320000, 350.0),
    ("常用医院", 39.915000, 116.418000, 350.0),
]


def enqueue_curiosity_question(
    db: SimingDB,
    topic: str,
    question: str,
    context: Optional[dict[str, Any]] = None,
    dedup_key: str = "",
) -> Optional[str]:
    """幂等登记一条待向主人求证的轻量问题（同一 dedup_key 30 天内只问一次）。"""
    db.init_db()
    ctx = context or {}
    dk = dedup_key or f"{topic}:{question[:48]}"
    qid = "Q-" + hashlib.sha1(dk.encode("utf-8")).hexdigest()[:10]
    ts = iso_now()
    with db.transaction() as conn:
        ex = conn.execute("SELECT qid FROM curiosity_questions WHERE qid=?", (qid,)).fetchone()
        if ex:
            return None
        conn.execute(
            """
            INSERT INTO curiosity_questions (qid, topic, question, context_json, status, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
            """,
            (qid, topic, question.strip(), json.dumps(ctx, ensure_ascii=False), ts),
        )
    return qid


def detect_unknown_dwell_and_ask(db: SimingDB, min_dwell_min: float = 35.0) -> Optional[dict[str, Any]]:
    """检查 OwnTracks 是否在一个既不是家也不是公司/已知地标的坐标连续静止停留满 35 分钟，若是则主动礼貌询问一次。"""
    db.init_db()
    raw_cur = db.get_meta("latest_ios_location") or ""
    if not raw_cur:
        return None
    try:
        cur = json.loads(raw_cur)
    except Exception:
        return None

    lat, lng = cur.get("lat"), cur.get("lng")
    if lat is None or lng is None:
        return None
    if cur.get("place_alias"):
        db.set_meta("dwell_candidate_json", "")
        return None

    lat_f, lng_f = float(lat), float(lng)
    # 排除已知地标半径内
    for _name, a_lat, a_lng, rad_m in KNOWN_ANCHORS:
        if _haversine_m(lat_f, lng_f, a_lat, a_lng) <= rad_m:
            db.set_meta("dwell_candidate_json", "")
            return None

    # 同时排除用户已存的自定义坐标地点
    custom_raw = db.get_meta("custom_geo_places_json") or "{}"
    try:
        custom_geo = json.loads(custom_raw)
    except Exception:
        custom_geo = {}
    for c_name, c_info in custom_geo.items():
        if _haversine_m(lat_f, lng_f, float(c_info["lat"]), float(c_info["lng"])) <= 260.0:
            db.set_meta("dwell_candidate_json", "")
            return None

    upd_s = str(cur.get("updated_at") or "")
    if not upd_s:
        return None
    now_dt = parse_iso(upd_s)

    cand_raw = db.get_meta("dwell_candidate_json") or ""
    cand: dict[str, Any] = {}
    if cand_raw:
        try:
            cand = json.loads(cand_raw)
        except Exception:
            cand = {}

    if (
        cand.get("lat") is not None
        and _haversine_m(lat_f, lng_f, float(cand["lat"]), float(cand["lng"])) <= 180.0
    ):
        dwell_min = (now_dt - parse_iso(str(cand["first_seen"]))).total_seconds() / 60.0
        if dwell_min >= min_dwell_min:
            grid_key = f"dwell:{round(lat_f, 3)},{round(lng_f, 3)}"
            wifi_hint = f"（Wi-Fi `{cur['wifi']}`）" if cur.get("wifi") else ""
            q_txt = (
                f"🤔 我看你在 `{lat_f:.4f}, {lng_f:.4f}`{wifi_hint} 停留了约 {int(dwell_min)} 分钟，"
                "我还不太确定这是哪里——如果是常去地点，直接回一句 `记住地点 <名称>`（如 `记住地点 健身房` 或 `记住地点 父母家`），"
                "只是临时路过回 `忽略` 就行！"
            )
            qid = enqueue_curiosity_question(
                db,
                topic="place_dwell",
                question=q_txt,
                context={"lat": lat_f, "lng": lng_f, "wifi": cur.get("wifi") or "", "dwell_min": round(dwell_min, 1)},
                dedup_key=grid_key,
            )
            if qid:
                from siming.push import push_message

                push_message(db, text=q_txt, level="normal", rule_id="curiosity_ask", ref_id=qid)
                with db.transaction() as conn:
                    conn.execute("UPDATE curiosity_questions SET status='ASKED', asked_at=? WHERE qid=?", (iso_now(), qid))
                return {"asked": True, "qid": qid, "question": q_txt}
        return None

    db.set_meta(
        "dwell_candidate_json",
        json.dumps({"lat": lat_f, "lng": lng_f, "first_seen": upd_s, "wifi": cur.get("wifi") or ""}, ensure_ascii=False),
    )
    return None


def dismiss_curiosity_question(db: SimingDB, qid: str = "") -> str:
    """忽略/关闭一条待求证问题（点击「临时路过（忽略）」或回复「忽略」）。"""
    db.init_db()
    clean_qid = (qid or "").strip()
    with db.transaction() as conn:
        if clean_qid:
            cur = conn.execute(
                "UPDATE curiosity_questions SET status='DISMISSED', answer='忽略', answered_at=? WHERE qid=? AND status IN ('PENDING','ASKED')",
                (iso_now(), clean_qid),
            )
            if cur.rowcount > 0:
                return "👍 已忽略该临时停留点，后续不再询问。"
            return "👍 该求证项已处理过。"
        row = conn.execute(
            "SELECT qid FROM curiosity_questions WHERE status IN ('ASKED','PENDING') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "👍 当前没有待处理的求证问题。"
        conn.execute(
            "UPDATE curiosity_questions SET status='DISMISSED', answer='忽略', answered_at=? WHERE qid=?",
            (iso_now(), row["qid"]),
        )
    return "👍 已忽略该临时停留点，后续不再询问。"


def try_handle_curiosity_reply(db: SimingDB, text: str) -> Optional[str]:
    """处理用户对司命主动询问的直觉回复（如 `记住地点 父母家`、`去朝阳大悦城`、`就在家` 或 `忽略`；兼容 QQ 语音）。"""
    clean_raw = re.sub(r"^\s*\[Voice\]\s*", "", (text or "").strip(), flags=re.IGNORECASE).strip()
    if not clean_raw:
        return None
    clean = re.sub(r"[。！!~～\s]+$", "", clean_raw).strip()
    if not clean:
        return None

    # 0) 优先响应活跃的『约定去哪里？』(trip_destination) 询问（严防误拦立即出门指令如『开车去/一会去/打车去』）
    db.init_db()
    conn_td = db.connect()
    try:
        td_row = conn_td.execute(
            "SELECT * FROM curiosity_questions WHERE topic='trip_destination' AND status IN ('ASKED','PENDING') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn_td.close()
    if td_row is not None:
        is_immediate_outing = bool(
            re.search(
                r"(?:开车去|自驾去|打车去|滴滴去|坐地铁去|步行去|走路去|现在去|马上去|一会去|待会去|准备出门|出行参谋|提醒我)",
                clean,
            )
        )
        m_local = re.match(r"^(?:就在家|在家|家里|线上|在线|不用出门|不出门)[。!！~～\s]*$", clean)
        m_dest = re.match(
            r"^(?:地点(?:是|在)?|地址(?:是|在)?|目的地(?:是|在)?|这次(?:去|在)|约在|定在|在|去)\s*[：:]?\s*(.{2,40})$",
            clean,
        )
        if not is_immediate_outing and (m_local or m_dest):
            from siming.outing_chain import bind_commitment_destination

            q_ctx = json.loads(td_row["context_json"] or "{}")
            wid = str(q_ctx.get("wait_id") or "")
            res = bind_commitment_destination(db, clean, wait_id=wid)
            if res.get("ok"):
                return str(res["reply"])

    if clean in ("忽略", "临时路过", "不用记", "算了忽略", "临时路过忽略"):
        db.init_db()
        conn = db.connect()
        try:
            q_row = conn.execute(
                "SELECT qid FROM curiosity_questions WHERE topic='place_dwell' AND status IN ('ASKED','PENDING') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if q_row:
            return dismiss_curiosity_question(db, qid=str(q_row["qid"]))
        return None
    m = re.match(
        r"^(?:(?:帮我|替我|给我)?(?:记住地点|标记地点|记住这里(?:是|叫)?|这里是|这里叫|地点是?)|把这里(?:记成|记为|标记为|标成))\s*[：:]?\s*([^\s，,。！!？?]{1,20})$",
        clean,
    )
    if not m:
        return None
    alias = m.group(1).strip()
    db.init_db()
    conn = db.connect()
    try:
        q_row = conn.execute(
            "SELECT * FROM curiosity_questions WHERE topic='place_dwell' AND status IN ('ASKED','PENDING') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    lat_f: Optional[float] = None
    lng_f: Optional[float] = None
    wifi_s = ""
    if q_row:
        ctx = json.loads(q_row["context_json"] or "{}")
        lat_f = ctx.get("lat")
        lng_f = ctx.get("lng")
        wifi_s = str(ctx.get("wifi") or "").strip()
    else:
        raw_cur = db.get_meta("latest_ios_location") or ""
        if raw_cur:
            cur = json.loads(raw_cur)
            lat_f, lng_f = cur.get("lat"), cur.get("lng")
            wifi_s = str(cur.get("wifi") or "").strip()

    if lat_f is None or lng_f is None:
        return "⚠️ 当前没有待标注的陌生坐标记录。"

    custom_raw = db.get_meta("custom_geo_places_json") or "{}"
    try:
        custom_geo = json.loads(custom_raw)
    except Exception:
        custom_geo = {}
    custom_geo[alias] = {"lat": float(lat_f), "lng": float(lng_f), "wifi": wifi_s, "saved_at": iso_now()}
    db.set_meta("custom_geo_places_json", json.dumps(custom_geo, ensure_ascii=False))
    set_saved_place(db, alias, f"{lat_f:.5f},{lng_f:.5f}")
    if wifi_s:
        from siming.context_loc import bind_wifi_to_place

        bind_wifi_to_place(db, wifi_s, alias)

    if q_row:
        with db.transaction() as c:
            c.execute(
                "UPDATE curiosity_questions SET status='ANSWERED', answer=?, answered_at=? WHERE qid=?",
                (alias, iso_now(), q_row["qid"]),
            )
    wifi_note = f"（并已绑定 Wi-Fi `{wifi_s}`）" if wifi_s else ""
    return f"📍 记住了！已把 `{lat_f:.4f}, {lng_f:.4f}` 标记为 **{alias}**{wifi_note}，以后你到/离这里我都能直接认出来。"


def list_pending_curiosity_questions(db: SimingDB, limit: int = 5) -> list[dict[str, Any]]:
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM curiosity_questions WHERE status IN ('PENDING', 'ASKED') ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
