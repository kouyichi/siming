"""gpu_dev_twin：离席研发分身（本机 Agent 卡权限超 5 分钟主动提醒 + 跨项目「十秒续工卡」）。
1. 被动全局守护（无需提前手敲 `siming watch`）：每轮 tick 0-token 检查本机 `herdr` 所有 Agent 面板，一旦发现某个面板处于 `blocked / waiting_input / approval_needed` 连续满 5 分钟且你未正盯着该面板操作，单次推 QQ 提醒（同一轮阻塞绝不重复唠叨）；
2. 跨项目「十秒续工卡」（QQ 发 `续 独游项目` / `十秒续工 personas` 或终端 `siming resume <项目>`）：毫秒级汇总 Git 分支与未提交改动、`HANDOFF.md` / `TODO` 下一步锚点、最近一次 Claude/Codex 会话原话与一键恢复命令、以及关联挂起项。
"""

from __future__ import annotations

import glob
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from siming.attention import probe_desktop_session_activity
from siming.config import load_policy
from siming.crypto import redact_sensitive, sanitize_terminal_tail
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso
from siming.modules import is_module_enabled
from siming.task_watch import _BLOCKED_AGENT_STATES, _default_pane_tail_reader, _snapshot_herdr_panes

PROJECT_ALIASES: dict[str, str] = {
    "独游项目": "indie_game",
    "indie_game": "indie_game",
    "司命": "siming",
    "siming": "siming",
    "装修": "deco",
    "新房": "deco",
    "deco": "deco",
    "人设": "personas",
    "persona": "personas",
    "personas": "personas",
    "桌游规则": "boardgame_kb",
    "规则": "boardgame_kb",
    "boardgame_kb": "boardgame_kb",
    "线上桌游": "reboard",
    "reboard": "reboard",
    "保价": "buy-track",
    "家电": "buy-track",
    "紫微": "ziwei-doushu",
    "紫薇": "ziwei-doushu",
}


def scan_stuck_herdr_agents(
    db: SimingDB,
    min_stuck_sec: int = 300,
    herdr_runner: Optional[Callable[[list[str]], dict[str, Any]]] = None,
    pane_reader: Optional[Callable[[str], str]] = None,
    desktop_probe: Optional[Callable[[], dict[str, Any]]] = None,
    now_override: Optional[datetime] = None,
) -> dict[str, Any]:
    """扫描未显式登记 `siming watch` 的本机 herdr Agent 面板：若卡在权限确认或提问等待连续 >= 5 分钟，单次推送提醒。"""
    db.init_db()
    if not is_module_enabled(db, "gpu_dev_twin"):
        return {"checked": 0, "alerted": [], "skipped": "module_disabled"}

    reachable, pane_map = _snapshot_herdr_panes(db, herdr_runner=herdr_runner)
    if not reachable:
        return {"checked": 0, "alerted": [], "skipped": "herdr_unreachable"}

    # 排除已在 `siming watch` 活跃监听池中的面板，避免双发通知
    conn = db.connect()
    try:
        watched_panes = {
            str(r["target_pane"])
            for r in conn.execute(
                "SELECT target_pane FROM task_runs WHERE kind='herdr_watch' AND status IN ('RUNNING', 'BLOCKED')"
            ).fetchall()
            if r["target_pane"]
        }
    finally:
        conn.close()

    now = now_override or now_local()
    ts = iso_now(now)
    raw_state = db.get_meta("dev_twin_stuck_state") or "{}"
    try:
        prev_state: dict[str, dict[str, Any]] = json.loads(raw_state)
    except Exception:
        prev_state = {}

    desk = (desktop_probe or probe_desktop_session_activity)()
    user_at_keyboard = bool(desk.get("available") and not desk.get("locked") and float(desk.get("idle_sec", 9999)) < 35.0)
    read_tail = pane_reader or (lambda pid: _default_pane_tail_reader(db, pid, lines=10))

    next_state: dict[str, dict[str, Any]] = {}
    alerted: list[str] = []

    for pid, info in pane_map.items():
        if pid in watched_panes:
            continue
        ag_status = str(info.get("agent_status") or "").lower()
        if ag_status not in _BLOCKED_AGENT_STATES:
            continue

        seq_key = f"{info.get('state_change_seq', 0)}:{info.get('completion_seq', 0)}:{ag_status}"
        # 若用户当前正好聚焦盯着这个面板且键鼠刚动过，重置计时不打扰
        if bool(info.get("focused")) and user_at_keyboard:
            next_state[pid] = {"seq_key": seq_key, "first_seen": ts, "notified": False}
            continue

        prev = prev_state.get(pid)
        if not prev or prev.get("seq_key") != seq_key:
            next_state[pid] = {"seq_key": seq_key, "first_seen": ts, "notified": False}
            continue

        first_seen_s = str(prev.get("first_seen") or ts)
        already_notified = bool(prev.get("notified"))
        try:
            stuck_sec = max(0.0, (now - parse_iso(first_seen_s)).total_seconds())
        except Exception:
            stuck_sec = 0.0

        if stuck_sec >= min_stuck_sec and not already_notified:
            cwd = str(info.get("foreground_cwd") or info.get("cwd") or "")
            proj = Path(cwd).name if cwd else pid
            title = str(info.get("terminal_title_stripped") or info.get("label") or info.get("agent") or "Agent")
            tail_5 = sanitize_terminal_tail(read_tail(pid), max_lines=5, paths=db.paths)
            msg_lines = [
                f"⏸️ **Agent 卡在权限/确认已满 {int(stuck_sec // 60)} 分钟**：`{proj}` · {title} (`{pid}`)",
                f"• 目录：`{cwd or '-'}` ｜ 状态：`{ag_status}`",
            ]
            if tail_5:
                msg_lines.append("• 现场末尾 5 行：\n" + "\n".join(f"  {ln}" for ln in tail_5.splitlines()))
            msg_lines.append(f"💡 切回面板拍板即可继续；在手机想先看上下文可回 `续 {proj}`。")
            with db.transaction() as c:
                db.enqueue_effect(
                    c,
                    "send_qq",
                    {
                        "ref_id": f"stuck:{pid}",
                        "rule_id": "gpu_dev_twin",
                        "text": "\n".join(msg_lines),
                    },
                )
            next_state[pid] = {"seq_key": seq_key, "first_seen": first_seen_s, "notified": True}
            alerted.append(pid)
        else:
            next_state[pid] = {"seq_key": seq_key, "first_seen": first_seen_s, "notified": already_notified}

    if next_state != prev_state:
        db.set_meta("dev_twin_stuck_state", json.dumps(next_state, ensure_ascii=False))

    return {"checked": len(pane_map), "tracking_blocked": len(next_state), "alerted": alerted}


def resolve_project_directory(db: SimingDB, query: str = "", work_root: Optional[Path] = None) -> Optional[tuple[str, Path]]:
    """解析项目别名或目录名至真实路径；若留空则自动选 ~/Work 下最近有改动的活跃项目。"""
    policy = load_policy(db.paths)
    wl = {str(k): Path(str(v)).expanduser() for k, v in (policy.get("whitelisted_repos") or {}).items()}
    w_root = work_root or (Path.home() / "Work")

    q = (query or "").strip()
    if q:
        mapped = PROJECT_ALIASES.get(q.lower()) or PROJECT_ALIASES.get(q) or q
        if work_root is not None:
            cand_override = w_root / mapped
            if cand_override.is_dir():
                return cand_override.name, cand_override
        if mapped in wl and wl[mapped].is_dir():
            return mapped, wl[mapped]
        cand_exact = w_root / mapped
        if cand_exact.is_dir():
            return cand_exact.name, cand_exact
        if w_root.is_dir():
            q_low = mapped.lower()
            matches = [d for d in sorted(w_root.iterdir()) if d.is_dir() and q_low in d.name.lower()]
            if matches:
                return matches[0].name, matches[0]
        return None

    # 未指定项目名时：找 ~/Work 下最近修改过的项目目录
    if not w_root.is_dir():
        return None
    best: Optional[tuple[float, str, Path]] = None
    for d in w_root.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            mt = d.stat().st_mtime
            git_head = d / ".git" / "logs" / "HEAD"
            if git_head.is_file():
                mt = max(mt, git_head.stat().st_mtime)
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, d.name, d)
    return (best[1], best[2]) if best else None


def _extract_doc_handoff_lines(proj_dir: Path) -> list[str]:
    """从项目内的 HANDOFF.md / TODO.md / PROGRESS.md / AUDIT.md / README.md 提取最精炼的现状与下一步锚点。"""
    for fname in ("HANDOFF.md", "TODO.md", "PROGRESS.md", "AUDIT.md", "README.md"):
        p = proj_dir / fname
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:16000]
        except OSError:
            continue
        lines = [ln.strip() for ln in text.splitlines()]
        # 优先截取包含「下一步 / 待办 / Next / TODO / 当前状态 / Handoff」小节下的前 3 条要点
        section_hits: list[str] = []
        in_sec = False
        for ln in lines:
            if ln.startswith("#"):
                if in_sec and section_hits:
                    break
                in_sec = bool(re.search(r"(下一步|待办|未完|现状|当前|next|todo|handoff|roadmap|status)", ln, re.IGNORECASE))
                continue
            if in_sec and ln.startswith(("-", "*", "1.", "2.", "3.", "•", "[ ]")):
                clean_b = re.sub(r"^[-*•]\s*(\[.\]\s*)?", "", ln).strip()
                if clean_b:
                    section_hits.append(f"({fname}) {clean_b[:95]}")
                if len(section_hits) >= 3:
                    return section_hits

        # 回退取文档开头前 2 条非空要点或标题
        fallback: list[str] = []
        for ln in lines:
            if not ln or ln.startswith("---"):
                continue
            clean = ln.lstrip("#-*• ").strip()
            if len(clean) >= 6:
                fallback.append(f"({fname}) {clean[:95]}")
            if len(fallback) >= 2:
                return fallback
    return []


def _find_latest_session_for_project(proj_dir: Path, paths_obj: Any) -> Optional[dict[str, str]]:
    """只读定位该项目最近一次 Claude Code 或 Codex 会话 ID 与最后一条用户需求原话。"""
    home = Path.home()
    proj_str = str(proj_dir.resolve())
    slug = "-" + proj_str.strip("/").replace("/", "-").replace("_", "-")
    slug_exact = "-" + proj_str.strip("/").replace("/", "-")

    candidates: list[tuple[float, str, Path]] = []
    for s_dir in {home / ".claude/projects" / slug_exact, home / ".claude/projects" / slug}:
        if s_dir.is_dir():
            for fp in s_dir.glob("*.jsonl"):
                try:
                    candidates.append((fp.stat().st_mtime, "claude", fp))
                except OSError:
                    pass

    # 补充扫描近 7 天 Codex 会话（尾部含该 cwd）
    codex_root = home / ".codex/sessions"
    if codex_root.is_dir():
        for fp_s in glob.glob(str(codex_root / "**/*.jsonl"), recursive=True):
            fp = Path(fp_s)
            try:
                st = fp.stat()
                if (now_local().timestamp() - st.st_mtime) > 7 * 86400:
                    continue
                with fp.open("rb") as fh:
                    head_b = fh.read(4096).decode("utf-8", errors="replace")
                if proj_str in head_b:
                    candidates.append((st.st_mtime, "codex", fp))
            except OSError:
                continue

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    mtime, agent, best_fp = candidates[0]
    sess_id = best_fp.stem

    last_user_msg = ""
    try:
        sz = best_fp.stat().st_size
        with best_fp.open("rb") as fh:
            fh.seek(max(0, sz - 65536))
            tail_txt = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail_txt.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if agent == "claude" and obj.get("type") == "user" and not obj.get("isSidechain") and not obj.get("isMeta"):
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    last_user_msg = content.strip()
                    break
                if isinstance(content, list):
                    txts = [str(b.get("text") or "").strip() for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    joined = " ".join(t for t in txts if t)
                    if joined:
                        last_user_msg = joined
                        break
            elif agent == "codex" and obj.get("type") == "event_msg":
                pl = obj.get("payload") or {}
                if pl.get("type") == "user_message" and pl.get("message"):
                    last_user_msg = str(pl["message"]).strip()
                    break
    except Exception:
        pass

    ts_str = datetime.fromtimestamp(mtime, tz=SHANGHAI_TZ).strftime("%m-%d %H:%M")
    resume_cmd = f"claude --resume {sess_id}" if agent == "claude" else f"codex resume {sess_id}"
    return {
        "agent": agent,
        "session_id": sess_id,
        "updated_at": ts_str,
        "last_prompt": redact_sensitive(re.sub(r"\s+", " ", last_user_msg)[:110], paths_obj) if last_user_msg else "",
        "resume_cmd": resume_cmd,
    }


def build_ten_second_resume_card(
    db: SimingDB,
    project_query: str = "",
    work_root: Optional[Path] = None,
    herdr_runner: Optional[Callable[[list[str]], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """生成跨项目「十秒续工卡」（0 模型消耗、纯确定性聚合 Git / Handoff / 最近会话 / 活跃面板与挂起）。"""
    db.init_db()
    resolved = resolve_project_directory(db, project_query, work_root=work_root)
    if resolved is None:
        return {
            "ok": False,
            "reply": f"❌ 未在 `~/Work` 或白名单仓库中找到项目「{project_query}」（可试：`续 独游项目`、`续 personas`、`续 司命`、`续 装修`）。",
        }

    proj_name, proj_dir = resolved
    lines = [f"⚡ **十秒续工卡：`{proj_name}`**（`{proj_dir}`）"]

    # 1. Git 状态或非 Git 目录最近产物
    if (proj_dir / ".git").exists():
        br = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip() or "HEAD"
        last_c = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%h (%cr) %s"],
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        st_lines = [
            ln
            for ln in subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(proj_dir),
                capture_output=True,
                text=True,
                timeout=4,
            ).stdout.splitlines()
            if ln.strip()
        ]
        if st_lines:
            sample = "、".join(f"`{ln[3:].strip()}`" for ln in st_lines[:4])
            more = f" 等 {len(st_lines)} 个文件" if len(st_lines) > 4 else ""
            lines.append(f"• 🌿 **分支与改动**：`{br}` · 未提交变更 **{len(st_lines)}** 项（{sample}{more}）")
        else:
            lines.append(f"• 🌿 **分支与改动**：`{br}` · 工作区干净")
        if last_c:
            lines.append(f"• 📌 **最近提交**：{last_c[:105]}")
    else:
        recent_files: list[tuple[float, Path]] = []
        for p in proj_dir.rglob("*"):
            if p.is_file() and not any(part.startswith(".") or part == "__pycache__" or part == "node_modules" for part in p.parts):
                try:
                    recent_files.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        recent_files.sort(key=lambda x: x[0], reverse=True)
        top_f = "、".join(
            f"`{p.relative_to(proj_dir)}`({datetime.fromtimestamp(mt, tz=SHANGHAI_TZ).strftime('%m-%d')})"
            for mt, p in recent_files[:4]
        )
        lines.append(f"• 📂 **最新变动文件**：{top_f or '目录为空'}")

    # 2. Handoff / README 锚点
    anchors = _extract_doc_handoff_lines(proj_dir)
    if anchors:
        lines.append("• 🧭 **交接锚点**：")
        for a in anchors[:3]:
            lines.append(f"  - {a}")

    # 3. 最近一次 Claude / Codex 会话
    sess = _find_latest_session_for_project(proj_dir, db.paths)
    if sess:
        prompt_note = f" — 上次停在：「{sess['last_prompt']}」" if sess["last_prompt"] else ""
        lines.append(f"• 💬 **最近会话**：`{sess['agent']}` ({sess['updated_at']}){prompt_note}")
        lines.append(f"  - 续接命令：`cd {proj_dir} && {sess['resume_cmd']}`")

    # 4. 本目录活跃 herdr 面板与司命挂起项
    ok_h, pane_map = _snapshot_herdr_panes(db, herdr_runner=herdr_runner)
    if ok_h:
        matching_panes = [
            f"`{pid}`({info.get('agent') or 'shell'}:{info.get('agent_status') or 'open'})"
            for pid, info in pane_map.items()
            if str(proj_dir) in str(info.get("foreground_cwd") or info.get("cwd") or "")
        ]
        if matching_panes:
            lines.append(f"• 🖥️ **在开 herdr 面板**：{'、'.join(matching_panes[:4])}")

    conn = db.connect()
    try:
        w_rows = conn.execute(
            "SELECT wait_id, quote, trigger_kind, trigger_expr FROM waits WHERE status='WAITING' AND (mandate_json LIKE ? OR quote LIKE ?) LIMIT 3",
            (f"%{proj_name}%", f"%{proj_name}%"),
        ).fetchall()
    finally:
        conn.close()
    if w_rows:
        w_str = "；".join(f"[{r['wait_id']}] {str(r['quote'])[:36]}" for r in w_rows)
        lines.append(f"• ⏳ **关联挂起项**：{w_str}")

    if (proj_dir / ".git").exists():
        lines.append(f"👉 手机端远程推进可直接发：`派单 {proj_name}：<要改的一句话任务>`")

    db.record_human_use("gpu_dev_twin", "resume_card", ref_id=proj_name)
    return {
        "ok": True,
        "project": proj_name,
        "path": str(proj_dir),
        "session": sess,
        "reply": "\n".join(lines),
    }
