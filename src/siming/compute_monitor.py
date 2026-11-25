"""司命控制塔 · 算力资源监控聚合与实时同步引擎（AI 配额/余额/流量 + 多 Runtime + 本机 CPU/RAM/GPU + 远端训练）。

数据源（全部只读探测 + 诚实降级，零外部第三方依赖）：
1. AI 算力配额与余额池：
   - Magpie 本地网关（`http://127.0.0.1:3425/v1/magpie/quotas` + `~/.config/magpie/quotas.json`）；
   - `ai-usagebar`（`~/.local/bin/ai-usagebar usage --json` + `~/.cache/ai-usagebar/*/usage.json`）；
   - `ai-quota` 本地配额 API（`http://127.0.0.1:18101/api/ai-quota`）；
   - Siming `compute_assets` 资产台账（含护栏告警与熔断开关）；
   - Nous 付款锁与 AUTO REFILL 哨兵（`self_heal.evaluate_nous_billing_sentinel`）。
2. AI 流量、Token 与账单趋势：
   - Magpie `~/.config/magpie/usage.jsonl` 尾部增量扫描（24h / 7d 按小时/日/厂商/模型聚合）；
   - OmniRoute `~/.omniroute/storage.sqlite` 只读统计与健康检查；
   - Siming `usage` 表与 `ledger_txns` 月度 AI 算力支出（CNY / USD 严格隔离）；
   - Linger/Pi 多 Runtime 路由表与熔断状态（`multi_runtime`）。
3. 本机硬件算力 & 远端训练值守：
   - Linux `/proc/stat`、`/proc/meminfo`、`/proc/loadavg`、`/sys/class/hwmon`、`/sys/class/drm`（AMD/Intel GPU）与 `nvidia-smi`（NVIDIA GPU）；
   - `overnight_handoffs`（`gpu-server` 夜间训练/评测交班）与 `task_runs`（长任务/herdr 监听）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from siming import db as _db_mod
from siming.compute_assets import (
    DEFAULT_FALLBACK_MODELS,
    evaluate_compute_guardrails,
    upsert_compute_asset,
)
from siming.config import load_policy
from siming.crypto import redact_sensitive
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, parse_iso
from siming.multi_runtime import get_runtime_profiles, select_runtime

_CACHE_LOCK = threading.Lock()
_LIVE_CACHE: dict[str, Any] = {
    "fetched_at_mono": 0.0,
    "fetched_at_iso": "",
    "magpie": None,
    "usagebar": None,
    "ai_quota": None,
    "redpill": None,
    "services": None,
    "warming": False,
}
_LIVE_CACHE_TTL_SEC = 60.0

_CPU_STAT_LOCK = threading.Lock()
_PREV_CPU_SAMPLE: Optional[tuple[int, int]] = None

_BALANCE_RE = re.compile(r"([¥$€£])?\s*([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(CNY|RMB|USD|EUR|GBP|credits|魔粒)?", re.IGNORECASE)

_AI_LEDGER_RE = re.compile(
    r"(nous|deepseek|深度求索|aliyun|阿里云|dashscope|kimi|月之暗面|moonshot|claude|anthropic|openai|chatgpt|redpill|codex|magpie|cursor|factory\s*ai|autodl|openrouter|zhipu|智谱|minimax|siliconflow|硅基流动|算力)",
    re.IGNORECASE,
)

_PROVIDER_CANONICAL: dict[str, str] = {
    "openai": "codex",
    "codex": "codex",
    "chatgpt": "codex",
    "deepseek": "deepseek",
    "moonshot": "kimi",
    "moonshot-cn": "kimi",
    "kimi": "kimi",
    "nous": "nous",
    "nousresearch": "nous",
    "antigravity": "antigravity",
    "claude": "claude",
    "anthropic": "claude",
    "factory": "factory",
    "custom:factory-droid": "factory",
    "factory-droid": "factory",
    "droid": "factory",
    "workbuddy": "workbuddy",
    "openrouter": "openrouter",
    "zhipu": "zhipu",
    "zai": "zhipu",
    "ollama-cloud": "ollama-cloud",
    "ollama": "ollama",
    "minimax-cn": "minimax",
    "minimax": "minimax",
    "modelscope": "modelscope",
    "siliconflow": "siliconflow",
    "aliyun": "aliyun",
    "redpill": "redpill",
}

_PROVIDER_DISPLAY: dict[str, str] = {
    "codex": "Codex (OpenAI)",
    "deepseek": "DeepSeek",
    "kimi": "Kimi (Moonshot)",
    "nous": "Nous Research",
    "antigravity": "Antigravity",
    "claude": "Claude Code",
    "factory": "Factory Droid",
    "workbuddy": "WorkBuddy",
    "openrouter": "OpenRouter",
    "zhipu": "智谱 GLM",
    "ollama-cloud": "Ollama Cloud",
    "ollama": "Ollama",
    "minimax": "MiniMax",
    "modelscope": "ModelScope",
    "siliconflow": "SiliconFlow",
    "aliyun": "阿里云百炼",
    "redpill": "RedPill",
}

# 归属为公费 CW08 的默认提供商（仅首次自动入库时使用，已有记录保留用户自定义）
_DEFAULT_CW08_PROVIDERS = frozenset({"nous", "deepseek"})


def _live_sources_allowed() -> bool:
    if os.environ.get("SIMING_TOWER_LIVE_COMPUTE") == "1":
        return True
    return os.environ.get("SIMING_TEST_OFFLINE") != "1"


def _canon_provider(raw: str) -> str:
    low = (raw or "").strip().lower()
    return _PROVIDER_CANONICAL.get(low, low or "unknown")


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_balance_str(raw: Any) -> tuple[Optional[float], str]:
    """解析余额字符串，如 '¥1173.15' / '$95.08' / '36675.74 credits' / '0 魔粒'。"""
    if raw is None:
        return None, ""
    if isinstance(raw, (int, float)):
        return round(float(raw), 4), ""
    s = str(raw).strip()
    if not s:
        return None, ""
    m = _BALANCE_RE.search(s)
    if not m:
        return None, ""
    sym, num_s, unit = m.group(1) or "", m.group(2) or "", m.group(3) or ""
    try:
        val = round(float(num_s.replace(",", "")), 4)
    except ValueError:
        return None, ""
    unit_u = unit.upper()
    if sym == "¥" or unit_u in ("CNY", "RMB"):
        return val, "CNY"
    if sym == "$" or unit_u == "USD":
        return val, "USD"
    if sym == "€" or unit_u == "EUR":
        return val, "EUR"
    if sym == "£" or unit_u == "GBP":
        return val, "GBP"
    if unit_u == "CREDITS":
        return val, "CREDITS"
    if unit == "魔粒":
        return val, "魔粒"
    return val, ""


def _fmt_balance(val: Optional[float], currency: str) -> str:
    if val is None:
        return ""
    ccy = (currency or "").upper()
    if ccy == "CNY":
        return f"¥{val:,.2f}"
    if ccy == "USD":
        return f"${val:,.2f}"
    if ccy == "EUR":
        return f"€{val:,.2f}"
    if ccy == "CREDITS":
        return f"{val:,.2f} credits"
    if currency:
        return f"{val:,.2f} {currency}"
    return f"{val:,.2f}"


def _window_severity(used_pct: Optional[float], raw_sev: str = "") -> str:
    rs = (raw_sev or "").strip().lower()
    if rs in ("critical", "high", "err", "error"):
        return "critical"
    if rs in ("warn", "warning", "mid", "medium"):
        return "warn"
    if used_pct is None:
        return "unknown"
    if used_pct >= 92.0:
        return "critical"
    if used_pct >= 75.0:
        return "warn"
    return "ok"


def _http_get_json_local(url: str, timeout: float = 2.0) -> tuple[bool, int, Any, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            code = int(resp.status)
            body = resp.read().decode("utf-8", errors="replace")
            return True, code, json.loads(body), ""
    except urllib.error.HTTPError as exc:
        return False, int(exc.code), None, f"HTTP {exc.code}"
    except Exception as exc:
        return False, 0, None, str(exc)[:160]


# ===========================================================================
# 1. Local Host Hardware Telemetry (Zero external deps: /proc, /sys, ps)
# ===========================================================================


def _read_proc_cpu_times() -> Optional[tuple[int, int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    parts = first.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        vals = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    return idle, total


def _sample_cpu_usage_pct() -> Optional[float]:
    global _PREV_CPU_SAMPLE
    with _CPU_STAT_LOCK:
        t1 = _read_proc_cpu_times()
        if t1 is None:
            return None
        if _PREV_CPU_SAMPLE is None:
            time.sleep(0.05)
            t2 = _read_proc_cpu_times()
            if t2 is None:
                _PREV_CPU_SAMPLE = t1
                return None
            prev = t1
            curr = t2
        else:
            prev = _PREV_CPU_SAMPLE
            curr = t1
        _PREV_CPU_SAMPLE = curr
        d_total = curr[1] - prev[1]
        d_idle = curr[0] - prev[0]
        if d_total <= 0:
            return 0.0
        pct = max(0.0, min(100.0, (1.0 - (d_idle / d_total)) * 100.0))
        return round(pct, 1)


def _read_cpu_info() -> dict[str, Any]:
    model = ""
    physical_cores = 0
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if not model and ln.startswith("model name"):
                    model = ln.split(":", 1)[1].strip()
                elif not physical_cores and ln.startswith("cpu cores"):
                    try:
                        physical_cores = int(ln.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                if model and physical_cores:
                    break
    except OSError:
        pass
    logical = os.cpu_count() or 1
    try:
        l1, l5, l15 = os.getloadavg()
    except OSError:
        l1, l5, l15 = 0.0, 0.0, 0.0
    return {
        "model": model or "Unknown CPU",
        "cores_logical": logical,
        "cores_physical": physical_cores or logical,
        "usage_pct": _sample_cpu_usage_pct(),
        "load_1m": round(l1, 2),
        "load_5m": round(l5, 2),
        "load_15m": round(l15, 2),
        "temp_c": None,
    }


def _read_mem_info() -> dict[str, Any]:
    kv: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ":" not in ln:
                    continue
                k, rest = ln.split(":", 1)
                tok = rest.strip().split()
                if tok:
                    try:
                        kv[k.strip()] = int(tok[0])  # KiB
                    except ValueError:
                        pass
    except OSError:
        pass

    tot_k = kv.get("MemTotal", 0)
    avail_k = kv.get("MemAvailable", kv.get("MemFree", 0))
    cached_k = kv.get("Cached", 0) + kv.get("Buffers", 0)
    used_k = max(0, tot_k - avail_k)
    sw_tot_k = kv.get("SwapTotal", 0)
    sw_free_k = kv.get("SwapFree", 0)
    sw_used_k = max(0, sw_tot_k - sw_free_k)
    gib = 1024.0 * 1024.0
    return {
        "total_gib": round(tot_k / gib, 2),
        "used_gib": round(used_k / gib, 2),
        "available_gib": round(avail_k / gib, 2),
        "cached_gib": round(cached_k / gib, 2),
        "used_pct": round((used_k / tot_k) * 100.0, 1) if tot_k > 0 else 0.0,
        "swap_total_gib": round(sw_tot_k / gib, 2),
        "swap_used_gib": round(sw_used_k / gib, 2),
        "swap_used_pct": round((sw_used_k / sw_tot_k) * 100.0, 1) if sw_tot_k > 0 else 0.0,
    }


def _read_hwmon_sensors() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    root = Path("/sys/class/hwmon")
    if not root.is_dir():
        return out
    for hw in sorted(root.iterdir()):
        try:
            name = (hw / "name").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        info: dict[str, Any] = {"name": name}
        for tf in ("temp1_input", "temp2_input"):
            p = hw / tf
            if p.is_file():
                try:
                    info["temp_c"] = round(int(p.read_text(encoding="utf-8").strip()) / 1000.0, 1)
                    break
                except (OSError, ValueError):
                    pass
        for pf in ("power1_average", "power1_input"):
            p = hw / pf
            if p.is_file():
                try:
                    info["power_w"] = round(int(p.read_text(encoding="utf-8").strip()) / 1_000_000.0, 1)
                    break
                except (OSError, ValueError):
                    pass
        out[name] = info
    return out


def _collect_local_gpus(hwmon: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    # 1. NVIDIA via nvidia-smi (if installed)
    nv_bin = shutil.which("nvidia-smi")
    if nv_bin:
        try:
            proc = subprocess.run(
                [
                    nv_bin,
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for line in proc.stdout.strip().splitlines():
                    cols = [c.strip() for c in line.split(",")]
                    if len(cols) < 6:
                        continue
                    v_used = _safe_float(cols[3]) or 0.0
                    v_tot = _safe_float(cols[4]) or 0.0
                    gpus.append(
                        {
                            "index": cols[0],
                            "vendor": "nvidia",
                            "name": cols[1],
                            "util_pct": _safe_float(cols[2]),
                            "vram_used_gib": round(v_used / 1024.0, 2),
                            "vram_total_gib": round(v_tot / 1024.0, 2),
                            "vram_used_pct": round((v_used / v_tot) * 100.0, 1) if v_tot > 0 else 0.0,
                            "temp_c": _safe_float(cols[5]),
                            "power_w": _safe_float(cols[6]) if len(cols) > 6 else None,
                            "power_limit_w": _safe_float(cols[7]) if len(cols) > 7 else None,
                        }
                    )
        except Exception:
            pass

    # 2. Linux DRM sysfs GPUs (AMD amdgpu / Intel)
    drm = Path("/sys/class/drm")
    if drm.is_dir():
        for card in sorted(drm.glob("card[0-9]*")):
            if "-" in card.name:
                continue
            dev = card / "device"
            busy_p = dev / "gpu_busy_percent"
            vram_u_p = dev / "mem_info_vram_used"
            vram_t_p = dev / "mem_info_vram_total"
            if not (busy_p.is_file() or vram_t_p.is_file()):
                continue
            util_pct = None
            try:
                if busy_p.is_file():
                    util_pct = round(float(busy_p.read_text(encoding="utf-8").strip()), 1)
            except (OSError, ValueError):
                pass
            vram_u = vram_t = 0
            try:
                if vram_u_p.is_file():
                    vram_u = int(vram_u_p.read_text(encoding="utf-8").strip())
                if vram_t_p.is_file():
                    vram_t = int(vram_t_p.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass
            gtt_u = gtt_t = 0
            try:
                if (dev / "mem_info_gtt_used").is_file():
                    gtt_u = int((dev / "mem_info_gtt_used").read_text(encoding="utf-8").strip())
                if (dev / "mem_info_gtt_total").is_file():
                    gtt_t = int((dev / "mem_info_gtt_total").read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass

            sclk_mhz = None
            try:
                if (dev / "pp_dpm_sclk").is_file():
                    for ln in (dev / "pp_dpm_sclk").read_text(encoding="utf-8").splitlines():
                        if "*" in ln:
                            m = re.search(r"(\d+)\s*Mhz", ln, re.IGNORECASE)
                            if m:
                                sclk_mhz = int(m.group(1))
                            break
            except OSError:
                pass

            vendor_name = "gpu"
            temp_c = None
            power_w = None
            hw_dir = dev / "hwmon"
            if hw_dir.is_dir():
                for child in sorted(hw_dir.iterdir()):
                    try:
                        vendor_name = (child / "name").read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                    for tf in ("temp1_input", "temp2_input"):
                        if (child / tf).is_file():
                            try:
                                temp_c = round(int((child / tf).read_text(encoding="utf-8").strip()) / 1000.0, 1)
                                break
                            except (OSError, ValueError):
                                pass
                    for pf in ("power1_average", "power1_input"):
                        if (child / pf).is_file():
                            try:
                                power_w = round(int((child / pf).read_text(encoding="utf-8").strip()) / 1_000_000.0, 1)
                                break
                            except (OSError, ValueError):
                                pass
                    break
            if temp_c is None and "amdgpu" in hwmon:
                temp_c = hwmon["amdgpu"].get("temp_c")
                power_w = power_w if power_w is not None else hwmon["amdgpu"].get("power_w")

            gib = 1024.0 ** 3
            gpus.append(
                {
                    "index": card.name,
                    "vendor": vendor_name,
                    "name": f"{vendor_name.upper()} ({card.name})",
                    "util_pct": util_pct,
                    "vram_used_gib": round(vram_u / gib, 2),
                    "vram_total_gib": round(vram_t / gib, 2),
                    "vram_used_pct": round((vram_u / vram_t) * 100.0, 1) if vram_t > 0 else 0.0,
                    "gtt_used_gib": round(gtt_u / gib, 2) if gtt_t > 0 else None,
                    "gtt_total_gib": round(gtt_t / gib, 2) if gtt_t > 0 else None,
                    "temp_c": temp_c,
                    "power_w": power_w,
                    "sclk_mhz": sclk_mhz,
                }
            )
    return gpus


def _collect_disks(hwmon: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen_devs: set[int] = set()
    disks: list[dict[str, Any]] = []
    nvme_temp = (hwmon.get("nvme") or {}).get("temp_c")
    for mp in ("/", "/home"):
        p = Path(mp)
        if not p.exists():
            continue
        try:
            st_dev = os.stat(mp).st_dev
            if st_dev in seen_devs:
                continue
            seen_devs.add(st_dev)
            du = shutil.disk_usage(mp)
        except OSError:
            continue
        gib = 1024.0 ** 3
        disks.append(
            {
                "mount": mp,
                "total_gib": round(du.total / gib, 1),
                "used_gib": round(du.used / gib, 1),
                "free_gib": round(du.free / gib, 1),
                "used_pct": round((du.used / du.total) * 100.0, 1) if du.total > 0 else 0.0,
                "temp_c": nvme_temp if mp == "/" else None,
            }
        )
    return disks


def _collect_top_processes(limit: int = 8) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,pcpu,pmem,rss,comm,args", "--sort=-pcpu"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out: list[dict[str, Any]] = []
    lines = proc.stdout.splitlines()[1:]
    for ln in lines:
        parts = ln.strip().split(None, 5)
        if len(parts) < 5:
            continue
        pid_s, cpu_s, mem_s, rss_s, comm = parts[:5]
        if comm == "ps":
            continue
        args = parts[5] if len(parts) > 5 else comm
        safe_cmd = redact_sensitive(args)[:110]
        try:
            out.append(
                {
                    "pid": int(pid_s),
                    "cpu_pct": round(float(cpu_s), 1),
                    "mem_pct": round(float(mem_s), 1),
                    "rss_mb": round(int(rss_s) / 1024.0, 1),
                    "comm": comm[:32],
                    "cmd": safe_cmd,
                }
            )
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def collect_host_resources() -> dict[str, Any]:
    """采集本机 CPU / 内存 / Swap / GPU / 磁盘 / 温度 / Top 进程（纯标准库，零额外依赖）。"""
    hwmon = _read_hwmon_sensors()
    cpu = _read_cpu_info()
    for sensor_key in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
        if sensor_key in hwmon and hwmon[sensor_key].get("temp_c") is not None:
            cpu["temp_c"] = hwmon[sensor_key]["temp_c"]
            break
    memory = _read_mem_info()
    gpus = _collect_local_gpus(hwmon)
    disks = _collect_disks(hwmon)
    uptime_sec = 0.0
    try:
        uptime_sec = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        pass
    return {
        "hostname": socket.gethostname(),
        "uptime_sec": int(uptime_sec),
        "cpu": cpu,
        "memory": memory,
        "gpus": gpus,
        "disks": disks,
        "top_processes": _collect_top_processes(limit=8),
    }


# ===========================================================================
# 2. Live AI Quota & Balance Collectors (Magpie + ai-usagebar + ai-quota)
# ===========================================================================


def _fmt_local_dt(raw: Any, *, date_only: bool = False) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    dt = _parse_iso_ts(s)
    if dt is not None:
        return dt.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")
    return s[:10] if date_only else s[:16].replace("T", " ")


def _normalize_magpie_item(item: dict[str, Any]) -> dict[str, Any]:
    raw_prov = str(item.get("provider") or "").strip().lower()
    prov = _canon_provider(raw_prov)
    name = str(item.get("name") or _PROVIDER_DISPLAY.get(prov) or raw_prov or "Unknown").strip()
    kind = str(item.get("kind") or ("subscription" if item.get("windows") else "balance")).strip().lower()
    plan = str(item.get("plan") or "").strip()
    user = str(item.get("user") or "").strip()
    err = str(item.get("error") or "").strip()
    bal_raw = item.get("balance")
    bal_num, ccy = _parse_balance_str(bal_raw)
    read_at = str(item.get("readAt") or item.get("at") or "").strip()

    windows: list[dict[str, Any]] = []
    for w in item.get("windows") or []:
        if not isinstance(w, dict):
            continue
        used = _safe_float(w.get("used"))
        rem = _safe_float(w.get("remaining"))
        if rem is None and used is not None:
            rem = max(0.0, round(100.0 - used, 2))
        if used is None and rem is not None:
            used = max(0.0, round(100.0 - rem, 2))
        disp = str(w.get("display") or (f"{used:.1f}%" if used is not None else "-"))
        detail_parts: list[str] = []
        if w.get("resetsAt"):
            detail_parts.append(f"重置 {_fmt_local_dt(w['resetsAt'])}")
        if w.get("runsOutAt"):
            detail_parts.append(f"预计耗尽 {_fmt_local_dt(w['runsOutAt'], date_only=True)}")
        holds = w.get("holds") if isinstance(w.get("holds"), dict) else None
        if holds and holds.get("cost"):
            detail_parts.append(f"周期路由 ${float(holds['cost']):.2f}")
        windows.append(
            {
                "name": str(w.get("name") or "window"),
                "used_pct": round(used, 1) if used is not None else None,
                "remaining_pct": round(rem, 1) if rem is not None else None,
                "display": disp,
                "resets_at": str(w.get("resetsAt") or ""),
                "resets_at_local": _fmt_local_dt(w.get("resetsAt")),
                "detail": " · ".join(detail_parts),
                "severity": _window_severity(used),
            }
        )

    resets_obj = None
    if isinstance(item.get("resets"), dict) and item["resets"].get("count"):
        next_raw = str(item["resets"].get("until") or "")
        resets_obj = {
            "count": int(item["resets"].get("count") or 0),
            "next_expires_at": next_raw,
            "next_expires_local": _fmt_local_dt(next_raw),
        }

    eff_kind = "hybrid" if (windows and bal_num is not None) else ("subscription" if windows else kind)
    pool_key = f"{prov}:{user}" if user else prov
    return {
        "pool_id": f"magpie:{pool_key}",
        "provider": prov,
        "raw_provider": raw_prov,
        "display_name": name,
        "account_label": user or "default",
        "kind": eff_kind,
        "plan": plan,
        "source": "magpie",
        "balance": bal_num,
        "currency": ccy,
        "balance_display": str(bal_raw).strip() if bal_raw else _fmt_balance(bal_num, ccy),
        "windows": windows,
        "reset_credits": resets_obj,
        "extra_notes": [],
        "updated_at": read_at,
        "stale": False,
        "error": err,
    }


def fetch_magpie_quotas(
    *,
    allow_http: bool = True,
    timeout: float = 2.5,
    config_dir: Optional[Path] = None,
    gateway_url: str = "http://127.0.0.1:3425/v1/magpie/quotas",
) -> dict[str, Any]:
    """拉取 Magpie 本地网关实时配额，不可达或超时时无痛回退至 `~/.config/magpie/quotas.json`。"""
    items: list[dict[str, Any]] = []
    source_mode = "unavailable"
    http_ok = False
    http_code = 0
    error = ""

    if allow_http:
        ok, code, data, err = _http_get_json_local(gateway_url, timeout=timeout)
        http_ok = ok
        http_code = code
        if ok and data is not None:
            raw_list = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(raw_list, list):
                items = [_normalize_magpie_item(x) for x in raw_list if isinstance(x, dict)]
                source_mode = "live_http"
        else:
            error = err

    if not items:
        cfg_dir = config_dir or (Path.home() / ".config" / "magpie")
        q_file = cfg_dir / "quotas.json"
        if q_file.is_file():
            try:
                raw_map = json.loads(q_file.read_text(encoding="utf-8"))
                if isinstance(raw_map, dict):
                    for key, entry in raw_map.items():
                        if not isinstance(entry, dict):
                            continue
                        q = entry.get("quota") if isinstance(entry.get("quota"), dict) else {}
                        if not q:
                            continue
                        merged = dict(q)
                        if "/" in key and not merged.get("user"):
                            suffix = key.split("/", 1)[1]
                            if "@" in suffix:
                                merged["user"] = suffix
                        merged.setdefault("readAt", entry.get("at") or "")
                        norm = _normalize_magpie_item(merged)
                        norm["stale"] = True
                        items.append(norm)
                    source_mode = "disk_cache"
            except Exception as exc:
                error = error or str(exc)[:160]

    return {
        "ok": bool(items) or http_ok,
        "running": http_ok,
        "status_code": http_code,
        "mode": source_mode,
        "items": items,
        "error": error,
    }


def _normalize_usagebar_entry(entry: dict[str, Any]) -> dict[str, Any]:
    raw_id = str(entry.get("id") or entry.get("name") or "").strip().lower()
    prov = _canon_provider(raw_id)
    disp_name = str(entry.get("display_name") or _PROVIDER_DISPLAY.get(prov) or raw_id).strip()
    plan = str(entry.get("plan") or "").strip()
    stale = bool(entry.get("stale", False))
    err = str(entry.get("error") or "").strip()
    fetched_at = str(entry.get("fetched_at") or "").strip()

    windows: list[dict[str, Any]] = []
    # 从 sections 提取带上下文分组（如 Session / Weekly）的 metrics 与余额/说明文本
    bal_num: Optional[float] = None
    bal_ccy = ""
    bal_disp = ""
    extra_notes: list[str] = []

    current_group = ""
    seen_win_keys: set[tuple[str, str]] = set()
    for sec in entry.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        stype = sec.get("type")
        lbl = str(sec.get("label") or "").strip()
        val = str(sec.get("value") or "").strip()
        if stype == "text":
            if lbl and not val:
                current_group = lbl
                continue
            low_l = lbl.lower()
            if any(k in low_l for k in ("total usable credits", "balance")):
                bn, bc = _parse_balance_str(val)
                if bn is not None:
                    bal_num, bal_ccy, bal_disp = bn, bc, val
            elif lbl and val and "warning" not in low_l:
                extra_notes.append(f"{lbl}: {val}")
        elif stype == "block":
            body = [str(x).strip() for x in (sec.get("body") or []) if str(x).strip()]
            if lbl.lower() == "credits" and body and bal_num is None:
                bn, _ = _parse_balance_str(body[0])
                if bn is not None:
                    bal_num, bal_ccy, bal_disp = bn, "CREDITS", f"{body[0]} credits"
            elif body and lbl.lower() not in ("api", "reset credits"):
                extra_notes.append(f"{lbl}: {' · '.join(body[:2])}")
        elif stype == "metric":
            pct = _safe_float(sec.get("percent"))
            rem = max(0.0, round(100.0 - pct, 1)) if pct is not None else None
            wname = f"{current_group} · {lbl}" if (current_group and lbl and current_group.lower() not in lbl.lower()) else (lbl or current_group or "quota")
            wkey = (wname, str(sec.get("reset_at") or ""))
            if wkey not in seen_win_keys:
                seen_win_keys.add(wkey)
                windows.append(
                    {
                        "name": wname,
                        "used_pct": round(pct, 1) if pct is not None else None,
                        "remaining_pct": rem,
                        "display": str(sec.get("value") or (f"{pct:.0f}%" if pct is not None else "-")),
                        "resets_at": str(sec.get("reset_at") or ""),
                        "resets_at_local": _fmt_local_dt(sec.get("reset_at")),
                        "detail": str(sec.get("detail") or ""),
                        "severity": _window_severity(pct, str(sec.get("severity") or "")),
                    }
                )

    if not windows:
        for m in entry.get("metrics") or []:
            if not isinstance(m, dict):
                continue
            pct = _safe_float(m.get("percent"))
            rem = max(0.0, round(100.0 - pct, 1)) if pct is not None else None
            windows.append(
                {
                    "name": str(m.get("label") or "quota"),
                    "used_pct": round(pct, 1) if pct is not None else None,
                    "remaining_pct": rem,
                    "display": str(m.get("value") or (f"{pct:.0f}%" if pct is not None else "-")),
                    "resets_at": str(m.get("reset_at") or ""),
                    "resets_at_local": _fmt_local_dt(m.get("reset_at")),
                    "detail": str(m.get("detail") or ""),
                    "severity": _window_severity(pct, str(m.get("severity") or "")),
                }
            )

    resets_obj = None
    rc = entry.get("reset_credits")
    if isinstance(rc, dict) and rc.get("available"):
        creds = rc.get("credits") or []
        next_exp = str(creds[0].get("expires_at") or "") if creds and isinstance(creds[0], dict) else ""
        resets_obj = {
            "count": int(rc.get("available") or 0),
            "next_expires_at": next_exp,
            "next_expires_local": _fmt_local_dt(next_exp),
        }

    eff_kind = "hybrid" if (windows and bal_num is not None) else ("subscription" if windows else "balance")
    return {
        "pool_id": f"usagebar:{prov}",
        "provider": prov,
        "raw_provider": raw_id,
        "display_name": disp_name,
        "account_label": "default",
        "kind": eff_kind,
        "plan": plan,
        "source": "ai-usagebar",
        "balance": bal_num,
        "currency": bal_ccy,
        "balance_display": bal_disp or _fmt_balance(bal_num, bal_ccy),
        "windows": windows,
        "reset_credits": resets_obj,
        "extra_notes": extra_notes[:4],
        "updated_at": fetched_at,
        "stale": stale,
        "error": err,
    }


def _read_usagebar_disk_cache(cache_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    root = cache_dir or (Path.home() / ".cache" / "ai-usagebar")
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []

    def _mtime_iso(p: Path) -> str:
        try:
            return datetime.fromtimestamp(p.stat().st_mtime, tz=SHANGHAI_TZ).isoformat(timespec="seconds")
        except OSError:
            return ""

    # deepseek
    p_ds = root / "deepseek" / "usage.json"
    if p_ds.is_file():
        try:
            d = json.loads(p_ds.read_text(encoding="utf-8"))
            bal = _safe_float(d.get("balance"))
            ccy = str(d.get("currency") or "CNY").upper()
            out.append(
                {
                    "pool_id": "usagebar:deepseek",
                    "provider": "deepseek",
                    "raw_provider": "deepseek",
                    "display_name": "DeepSeek",
                    "account_label": "default",
                    "kind": "balance",
                    "plan": "DeepSeek",
                    "source": "ai-usagebar-cache",
                    "balance": bal,
                    "currency": ccy,
                    "balance_display": _fmt_balance(bal, ccy),
                    "windows": [],
                    "reset_credits": None,
                    "extra_notes": [f"topped-up {_fmt_balance(_safe_float(d.get('topped_up')), ccy)}"],
                    "updated_at": _mtime_iso(p_ds),
                    "stale": False,
                    "error": "",
                }
            )
        except Exception:
            pass

    # antigravity
    p_ag = root / "antigravity" / "usage.json"
    if p_ag.is_file():
        try:
            d = json.loads(p_ag.read_text(encoding="utf-8"))
            wins: list[dict[str, Any]] = []
            for label, pk, rk in (
                ("Session · Gemini", "session_pct", "session_reset"),
                ("Weekly · Gemini", "weekly_pct", "weekly_reset"),
                ("Weekly · Claude & GPT OSS", "tp_weekly_pct", "tp_weekly_reset"),
            ):
                pct = _safe_float(d.get(pk))
                if pct is None:
                    continue
                wins.append(
                    {
                        "name": label,
                        "used_pct": round(pct, 1),
                        "remaining_pct": max(0.0, round(100.0 - pct, 1)),
                        "display": f"{pct:.0f}%",
                        "resets_at": str(d.get(rk) or ""),
                        "resets_at_local": _fmt_local_dt(d.get(rk)),
                        "detail": f"重置 {_fmt_local_dt(d.get(rk))}" if d.get(rk) else "",
                        "severity": _window_severity(pct),
                    }
                )
            out.append(
                {
                    "pool_id": "usagebar:antigravity",
                    "provider": "antigravity",
                    "raw_provider": "antigravity",
                    "display_name": "Antigravity",
                    "account_label": str(d.get("account") or "default"),
                    "kind": "subscription",
                    "plan": str(d.get("plan") or "Antigravity"),
                    "source": "ai-usagebar-cache",
                    "balance": None,
                    "currency": "",
                    "balance_display": "",
                    "windows": wins,
                    "reset_credits": None,
                    "extra_notes": [],
                    "updated_at": _mtime_iso(p_ag),
                    "stale": False,
                    "error": "",
                }
            )
        except Exception:
            pass

    # openai (codex)
    p_oa = root / "openai" / "usage.json"
    if p_oa.is_file():
        try:
            d = json.loads(p_oa.read_text(encoding="utf-8"))
            wins = []
            rl = d.get("rate_limit") if isinstance(d.get("rate_limit"), dict) else {}
            pw = rl.get("primary_window") if isinstance(rl.get("primary_window"), dict) else None
            if pw and pw.get("used_percent") is not None:
                pct = float(pw["used_percent"])
                wins.append(
                    {
                        "name": "Codex weekly",
                        "used_pct": round(pct, 1),
                        "remaining_pct": max(0.0, round(100.0 - pct, 1)),
                        "display": f"{pct:.0f}%",
                        "resets_at": "",
                        "detail": "",
                        "severity": _window_severity(pct),
                    }
                )
            creds = d.get("credits") if isinstance(d.get("credits"), dict) else {}
            bal = _safe_float(creds.get("balance"))
            rc = d.get("rate_limit_reset_credits") if isinstance(d.get("rate_limit_reset_credits"), dict) else {}
            rc_list = rc.get("credits") or []
            next_exp_oa = str(rc_list[0].get("expires_at") or "") if rc_list else ""
            resets_obj = (
                {
                    "count": int(rc.get("available_count") or 0),
                    "next_expires_at": next_exp_oa,
                    "next_expires_local": _fmt_local_dt(next_exp_oa),
                }
                if rc.get("available_count")
                else None
            )
            out.append(
                {
                    "pool_id": "usagebar:codex",
                    "provider": "codex",
                    "raw_provider": "openai",
                    "display_name": "Codex (OpenAI)",
                    "account_label": "default",
                    "kind": "hybrid" if (wins and bal is not None) else "subscription",
                    "plan": f"ChatGPT {str(d.get('plan_type') or 'pro').capitalize()}",
                    "source": "ai-usagebar-cache",
                    "balance": bal,
                    "currency": "CREDITS" if bal is not None else "",
                    "balance_display": _fmt_balance(bal, "CREDITS") if bal is not None else "",
                    "windows": wins,
                    "reset_credits": resets_obj,
                    "extra_notes": [],
                    "updated_at": _mtime_iso(p_oa),
                    "stale": False,
                    "error": "",
                }
            )
        except Exception:
            pass
    return out


def fetch_usagebar_quotas(
    *,
    live: bool = False,
    timeout: float = 8.0,
    cache_dir: Optional[Path] = None,
    bin_path: Optional[str] = None,
) -> dict[str, Any]:
    """查询 `ai-usagebar` 实时或磁盘缓存多厂商配额与余额（含 Antigravity / Nous / DeepSeek / Codex）。"""
    exe = bin_path or shutil.which("ai-usagebar") or str(Path.home() / ".local" / "bin" / "ai-usagebar")
    if live and exe and Path(exe).is_file():
        try:
            proc = subprocess.run(
                [exe, "usage", "--json"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                entries = data.get("entries") or []
                items = [_normalize_usagebar_entry(e) for e in entries if isinstance(e, dict)]
                return {"ok": True, "mode": "live_cli", "items": items, "error": ""}
        except Exception as exc:
            cached = _read_usagebar_disk_cache(cache_dir=cache_dir)
            return {"ok": bool(cached), "mode": "disk_cache_fallback", "items": cached, "error": str(exc)[:160]}

    cached = _read_usagebar_disk_cache(cache_dir=cache_dir)
    return {"ok": bool(cached), "mode": "disk_cache" if cached else "unavailable", "items": cached, "error": ""}


def fetch_ai_quota_local(db: SimingDB, timeout: float = 1.5) -> dict[str, Any]:
    """读取本机 `ai-quota.service`（Codex / Claude Code 配额 API）。"""
    policy = load_policy(db.paths)
    url = str((policy.get("coding_plan") or {}).get("ai_quota_url") or "http://127.0.0.1:18101/api/ai-quota")
    ok, code, data, err = _http_get_json_local(url, timeout=timeout)
    if not ok or not isinstance(data, dict):
        return {
            "ok": False,
            "running": False,
            "status_code": code,
            "url": url,
            "error": err,
            "codex_available": None,
            "codex_remaining": None,
            "claude_available": None,
            "claude_remaining": None,
            "items": [],
        }

    codex = data.get("codex") if isinstance(data.get("codex"), dict) else {}
    codex_p = codex.get("primary") if isinstance(codex.get("primary"), dict) else {}
    claude = data.get("claude") if isinstance(data.get("claude"), dict) else {}
    claude_s = claude.get("session") if isinstance(claude.get("session"), dict) else {}
    return {
        "ok": True,
        "running": True,
        "status_code": code,
        "url": url,
        "generated_at": str(data.get("generated_at") or ""),
        "codex_available": bool(codex.get("available")),
        "codex_remaining": _safe_float(codex_p.get("remaining_percent")),
        "codex_label": str(codex.get("remaining_label") or ""),
        "claude_available": bool(claude.get("available")),
        "claude_remaining": _safe_float(claude_s.get("remaining_percent")),
        "claude_label": str(claude.get("remaining_label") or ""),
        "claude_error": str(((claude.get("auth") or {}) if isinstance(claude.get("auth"), dict) else {}).get("error") or ""),
        "items": data.get("items") or [],
    }


_DEFAULT_REDPILL_BILLING_URL = ""

_REDPILL_BALANCE_JS = """(async () => {
  try {
    const r = await fetch('/api/billing/balance', {credentials: 'include'});
    const status = r.status;
    if (!r.ok) {
      return JSON.stringify({ok: false, status, error: 'HTTP ' + status});
    }
    const bal = await r.json();
    let sub = null;
    try {
      const sr = await fetch('/api/membership/subscription', {credentials: 'include'});
      if (sr.ok) sub = await sr.json();
    } catch (_) {}
    return JSON.stringify({ok: true, status, balance: bal, subscription: sub});
  } catch (e) {
    return JSON.stringify({ok: false, status: 0, error: String(e)});
  }
})()"""


def _redpill_org_label(billing_url: str) -> str:
    m = re.search(r"/orgs/([^/?#]+)", billing_url or "")
    if not m:
        return "default"
    slug = m.group(1).strip()
    # 缩短末尾超长数字 ID，保持卡片简洁（完整 URL 保留在 billing_url 中）
    short = re.sub(r"-\d{12,}$", "", slug)
    return (short or slug)[:32]


def _normalize_redpill_payload(
    raw: dict[str, Any],
    *,
    billing_url: str,
    mode: str,
) -> Optional[dict[str, Any]]:
    bal_obj = raw.get("balance") if isinstance(raw.get("balance"), dict) else {}
    bal_val = _safe_float(bal_obj.get("balance_usd"))
    if bal_val is None:
        return None
    bal_round = round(bal_val, 2)
    sub_obj = raw.get("subscription") if isinstance(raw.get("subscription"), dict) else {}
    sub_inner = sub_obj.get("subscription") if isinstance(sub_obj.get("subscription"), dict) else None
    plan_name = str((sub_inner or {}).get("plan_name") or (sub_inner or {}).get("name") or "").strip() or "Pay-as-you-go"
    notes: list[str] = []
    if bal_obj.get("can_top_up") is False:
        notes.append("暂不支持线上充值")
    m_org = re.search(r"/orgs/([^/?#]+)", billing_url or "")
    if m_org:
        notes.append(f"Org: {m_org.group(1)}")
    return {
        "pool_id": "redpill:org",
        "provider": "redpill",
        "raw_provider": "redpill",
        "display_name": "RedPill",
        "account_label": _redpill_org_label(billing_url),
        "kind": "balance",
        "plan": plan_name,
        "source": f"redpill-{mode}",
        "balance": bal_round,
        "currency": "USD",
        "balance_display": _fmt_balance(bal_round, "USD"),
        "windows": [],
        "reset_credits": None,
        "extra_notes": notes,
        "billing_url": billing_url,
        "updated_at": iso_now(),
        "stale": False,
        "error": "",
    }


def fetch_redpill_billing(
    db: SimingDB,
    *,
    live: bool = False,
    billing_url: str = "",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """通过本机主 Chromium (9222) 登录态只读拉取 RedPill 组织余额（`/api/billing/balance`）。

    优先复用已打开的 redpill.ai 标签页（零新建标签、~150ms）；仅在 `live=True` 且未打开标签时
    静默后台开标签读取并自动关闭（遇到登录/风控门禁立即停手交接，绝不绕过）。
    """
    policy = load_policy(db.paths)
    cm_cfg = policy.get("compute_monitor") if isinstance(policy.get("compute_monitor"), dict) else {}
    url = str(billing_url or cm_cfg.get("redpill_billing_url") or _DEFAULT_REDPILL_BILLING_URL).strip()
    if not url:
        return {"ok": False, "mode": "unconfigured", "items": [], "error": ""}

    try:
        from siming.cdp import LocalWsCdpClient, inspect_url_in_background_tab, list_chromium_tabs
    except Exception as exc:
        return {"ok": False, "mode": "unavailable", "items": [], "error": str(exc)[:160]}

    # 1. 快速路径：复用已打开的 redpill.ai 标签页
    try:
        tabs = list_chromium_tabs()
    except Exception:
        tabs = []
    rp_tabs = [t for t in tabs if "redpill.ai" in str(t.get("url") or "").lower()]
    rp_tabs.sort(key=lambda t: (0 if "/billing" in str(t.get("url") or "").lower() else 1))
    for tab in rp_tabs:
        ws_url = str(tab.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            continue
        try:
            with LocalWsCdpClient(ws_url, timeout=timeout) as ws:
                ev = ws.call(
                    "Runtime.evaluate",
                    {"expression": _REDPILL_BALANCE_JS, "awaitPromise": True, "returnByValue": True},
                )
            raw_str = str(((ev.get("result") or {}).get("value")) or "")
            parsed = json.loads(raw_str) if raw_str.startswith("{") else {}
            if parsed.get("ok"):
                item = _normalize_redpill_payload(parsed, billing_url=url, mode="cdp")
                if item:
                    return {"ok": True, "mode": "cdp_tab", "items": [item], "error": ""}
        except Exception:
            continue

    # 2. 后台标签回退（仅显式 live 刷新或后台预热时执行，避免阻塞冷读）
    if live:
        try:
            bg = inspect_url_in_background_tab(url, _REDPILL_BALANCE_JS, wait_sec=2.5, close_when_clean=True)
            if bg.get("ok"):
                raw_str = str(bg.get("data") or "")
                parsed = json.loads(raw_str) if raw_str.startswith("{") else {}
                if parsed.get("ok"):
                    item = _normalize_redpill_payload(parsed, billing_url=url, mode="cdp")
                    if item:
                        return {"ok": True, "mode": "cdp_bg", "items": [item], "error": ""}
                return {"ok": False, "mode": "cdp_bg_err", "items": [], "error": str(parsed.get("error") or "balance_unavailable")}
            if bg.get("gated"):
                reason = str(((bg.get("gate") or {}).get("reason")) or "需要登录 RedPill")
                return {"ok": False, "mode": "cdp_gated", "items": [], "error": reason}
            return {"ok": False, "mode": "unavailable", "items": [], "error": str(bg.get("error") or "")}
        except Exception as exc:
            return {"ok": False, "mode": "unavailable", "items": [], "error": str(exc)[:160]}

    return {"ok": False, "mode": "cache_only", "items": [], "error": ""}


def _probe_local_services(
    db: SimingDB,
    *,
    magpie_res: dict[str, Any],
    ai_quota_res: dict[str, Any],
    omniroute_url: str = "http://127.0.0.1:20128/api/health",
    allow_http: bool = True,
) -> list[dict[str, Any]]:
    omni_ok = False
    omni_code = 0
    omni_err = ""
    if allow_http:
        omni_ok, omni_code, _, omni_err = _http_get_json_local(omniroute_url, timeout=1.0)
    return [
        {
            "id": "magpie",
            "name": "Magpie Gateway",
            "endpoint": "127.0.0.1:3425",
            "running": bool(magpie_res.get("running")),
            "mode": str(magpie_res.get("mode") or ""),
            "status_code": int(magpie_res.get("status_code") or 0),
            "detail": str(magpie_res.get("error") or ("在线" if magpie_res.get("running") else "缓存模式")),
        },
        {
            "id": "ai-quota",
            "name": "ai-quota API",
            "endpoint": "127.0.0.1:18101",
            "running": bool(ai_quota_res.get("running")),
            "mode": "live_http" if ai_quota_res.get("running") else "offline",
            "status_code": int(ai_quota_res.get("status_code") or 0),
            "detail": str(ai_quota_res.get("error") or ("在线" if ai_quota_res.get("running") else "不可达")),
        },
        {
            "id": "omniroute",
            "name": "OmniRoute",
            "endpoint": "127.0.0.1:20128",
            "running": omni_ok,
            "mode": "live_http" if omni_ok else "offline",
            "status_code": omni_code,
            "detail": omni_err or ("在线" if omni_ok else "未运行"),
        },
    ]


# ===========================================================================
# 3. AI Traffic, Token Telemetry & Monthly Ledger Spend
# ===========================================================================


def _parse_iso_ts(raw: str) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(SHANGHAI_TZ)
    except Exception:
        return None


def collect_magpie_usage_stats(
    *,
    usage_file: Optional[Path] = None,
    now: Optional[datetime] = None,
    max_tail_bytes: int = 32 * 1024 * 1024,
) -> dict[str, Any]:
    """高效按块反向扫描 `~/.config/magpie/usage.jsonl`，确保完整覆盖近 7 天请求/Token/延迟。"""
    p = usage_file or (Path.home() / ".config" / "magpie" / "usage.jsonl")
    cur = now or _db_mod.now_local()
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=SHANGHAI_TZ)
    cutoff_24h = cur - timedelta(hours=24)
    cutoff_7d = cur - timedelta(days=7)

    empty = {
        "available": False,
        "requests_24h": 0,
        "ok_24h": 0,
        "tokens_in_24h": 0,
        "tokens_out_24h": 0,
        "tokens_cache_24h": 0,
        "avg_latency_ms_24h": 0.0,
        "requests_7d": 0,
        "tokens_total_7d": 0,
        "by_provider": [],
        "by_model": [],
        "hourly_24h": [],
        "daily_7d": [],
    }
    if not p.is_file():
        return empty

    try:
        sz = p.stat().st_size
        chunk_bytes = 4 * 1024 * 1024
        read_start = sz
        chunks: list[bytes] = []
        with p.open("rb") as fh:
            if sz <= chunk_bytes:
                raw = fh.read().decode("utf-8", errors="replace")
                lines = raw.splitlines()
            else:
                total_read = 0
                while read_start > 0 and total_read < max_tail_bytes:
                    step = min(chunk_bytes, read_start, max_tail_bytes - total_read)
                    read_start -= step
                    fh.seek(read_start)
                    buf = fh.read(step)
                    chunks.append(buf)
                    total_read += len(buf)
                    # Check earliest complete line timestamp in this chunk to stop early once < cutoff_7d
                    nl_idx = buf.find(b"\n")
                    sample_slice = buf[nl_idx + 1 :] if (read_start > 0 and nl_idx != -1) else buf
                    first_nl = sample_slice.find(b"\n")
                    first_line = (sample_slice[:first_nl] if first_nl != -1 else sample_slice).decode("utf-8", errors="replace").strip()
                    if first_line.startswith("{"):
                        try:
                            first_dt = _parse_iso_ts(str(json.loads(first_line).get("t") or ""))
                            if first_dt is not None and first_dt < cutoff_7d:
                                break
                        except ValueError:
                            pass
                chunks.reverse()
                raw = b"".join(chunks).decode("utf-8", errors="replace")
                lines = raw.splitlines()[1:] if read_start > 0 else raw.splitlines()
    except OSError:
        return empty

    # Pre-seed 24 hourly buckets and 7 daily buckets
    hourly_keys: list[str] = []
    hourly_map: dict[str, dict[str, int]] = {}
    for offset in range(23, -1, -1):
        hk = (cur - timedelta(hours=offset)).strftime("%m-%d %H:00")
        hourly_keys.append(hk)
        hourly_map[hk] = {"requests": 0, "tokens": 0, "errors": 0}

    daily_keys: list[str] = []
    daily_map: dict[str, dict[str, int]] = {}
    for offset in range(6, -1, -1):
        dk = (cur - timedelta(days=offset)).strftime("%Y-%m-%d")
        daily_keys.append(dk)
        daily_map[dk] = {"requests": 0, "tokens": 0, "errors": 0}

    prov_agg: dict[str, dict[str, Any]] = {}
    model_agg: dict[str, dict[str, Any]] = {}

    req_24h = ok_24h = tin_24h = tout_24h = tcache_24h = 0
    lat_sum_24h = 0.0
    lat_cnt_24h = 0
    req_7d = tok_7d = 0

    for ln in lines:
        ln = ln.strip()
        if not ln or not ln.startswith("{"):
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        dt = _parse_iso_ts(str(o.get("t") or ""))
        if dt is None or dt < cutoff_7d:
            continue

        status = int(o.get("status") or 0)
        is_ok = 200 <= status < 400
        t_in = int(o.get("in") or 0)
        t_out = int(o.get("out") or 0)
        t_cache = int(o.get("cache_read") or 0) + int(o.get("cache_write") or 0)
        t_all = t_in + t_out + t_cache
        ms = _safe_float(o.get("ms")) or 0.0
        prov = _canon_provider(str(o.get("provider") or "unknown"))
        model = str(o.get("served") or o.get("model") or "unknown").strip() or "unknown"

        req_7d += 1
        tok_7d += t_all
        dk = dt.strftime("%Y-%m-%d")
        if dk in daily_map:
            daily_map[dk]["requests"] += 1
            daily_map[dk]["tokens"] += t_all
            if not is_ok:
                daily_map[dk]["errors"] += 1

        pa = prov_agg.setdefault(
            prov,
            {"provider": prov, "requests_7d": 0, "requests_24h": 0, "ok_7d": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cache": 0, "lat_sum": 0.0, "lat_cnt": 0},
        )
        pa["requests_7d"] += 1
        if is_ok:
            pa["ok_7d"] += 1
        pa["tokens_in"] += t_in
        pa["tokens_out"] += t_out
        pa["tokens_cache"] += t_cache
        if ms > 0:
            pa["lat_sum"] += ms
            pa["lat_cnt"] += 1

        ma = model_agg.setdefault(
            model,
            {"model": model, "provider": prov, "requests_7d": 0, "requests_24h": 0, "ok_7d": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cache": 0, "lat_sum": 0.0, "lat_cnt": 0},
        )
        ma["requests_7d"] += 1
        if is_ok:
            ma["ok_7d"] += 1
        ma["tokens_in"] += t_in
        ma["tokens_out"] += t_out
        ma["tokens_cache"] += t_cache
        if ms > 0:
            ma["lat_sum"] += ms
            ma["lat_cnt"] += 1

        if dt >= cutoff_24h:
            req_24h += 1
            if is_ok:
                ok_24h += 1
            tin_24h += t_in
            tout_24h += t_out
            tcache_24h += t_cache
            if ms > 0:
                lat_sum_24h += ms
                lat_cnt_24h += 1
            pa["requests_24h"] += 1
            ma["requests_24h"] += 1
            hk = dt.strftime("%m-%d %H:00")
            if hk in hourly_map:
                hourly_map[hk]["requests"] += 1
                hourly_map[hk]["tokens"] += t_all
                if not is_ok:
                    hourly_map[hk]["errors"] += 1

    by_provider = []
    for pa in sorted(prov_agg.values(), key=lambda x: (x["requests_24h"], x["requests_7d"]), reverse=True):
        by_provider.append(
            {
                "provider": pa["provider"],
                "display_name": _PROVIDER_DISPLAY.get(pa["provider"], pa["provider"]),
                "requests_24h": pa["requests_24h"],
                "requests_7d": pa["requests_7d"],
                "success_rate_pct": round((pa["ok_7d"] / max(1, pa["requests_7d"])) * 100.0, 1),
                "tokens_in": pa["tokens_in"],
                "tokens_out": pa["tokens_out"],
                "tokens_cache": pa["tokens_cache"],
                "tokens_total": pa["tokens_in"] + pa["tokens_out"] + pa["tokens_cache"],
                "avg_latency_ms": round(pa["lat_sum"] / max(1, pa["lat_cnt"]), 0) if pa["lat_cnt"] else 0.0,
            }
        )

    by_model = []
    for ma in sorted(model_agg.values(), key=lambda x: (x["requests_24h"], x["requests_7d"]), reverse=True)[:12]:
        by_model.append(
            {
                "model": ma["model"],
                "provider": ma["provider"],
                "requests_24h": ma["requests_24h"],
                "requests_7d": ma["requests_7d"],
                "success_rate_pct": round((ma["ok_7d"] / max(1, ma["requests_7d"])) * 100.0, 1),
                "tokens_in": ma["tokens_in"],
                "tokens_out": ma["tokens_out"],
                "tokens_cache": ma["tokens_cache"],
                "tokens_total": ma["tokens_in"] + ma["tokens_out"] + ma["tokens_cache"],
                "avg_latency_ms": round(ma["lat_sum"] / max(1, ma["lat_cnt"]), 0) if ma["lat_cnt"] else 0.0,
            }
        )

    return {
        "available": True,
        "requests_24h": req_24h,
        "ok_24h": ok_24h,
        "success_rate_24h_pct": round((ok_24h / max(1, req_24h)) * 100.0, 1) if req_24h else 100.0,
        "tokens_in_24h": tin_24h,
        "tokens_out_24h": tout_24h,
        "tokens_cache_24h": tcache_24h,
        "tokens_total_24h": tin_24h + tout_24h + tcache_24h,
        "avg_latency_ms_24h": round(lat_sum_24h / max(1, lat_cnt_24h), 0) if lat_cnt_24h else 0.0,
        "requests_7d": req_7d,
        "tokens_total_7d": tok_7d,
        "by_provider": by_provider,
        "by_model": by_model,
        "hourly_24h": [{"hour": hk, **hourly_map[hk]} for hk in hourly_keys],
        "daily_7d": [{"day": dk, **daily_map[dk]} for dk in daily_keys],
    }


def collect_omniroute_usage_stats(
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """只读读取 OmniRoute `storage.sqlite` 24h & 7d 统计（绝不写入、绝不读取密钥表）。"""
    p = db_path or (Path.home() / ".omniroute" / "storage.sqlite")
    if not p.is_file():
        return {"available": False, "requests_24h": 0, "ok_24h": 0, "requests_7d": 0, "by_provider": []}

    cur = (now or _db_mod.now_local()).astimezone(timezone.utc)
    iso_24h = (cur - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    iso_7d = (cur - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            r24 = conn.execute(
                """
                SELECT COUNT(*) AS reqs,
                       COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS ok_reqs,
                       COALESCE(SUM(tokens_input), 0) AS tin,
                       COALESCE(SUM(tokens_output), 0) AS tout
                FROM usage_history WHERE timestamp >= ?
                """,
                (iso_24h,),
            ).fetchone()
            rows_7d = conn.execute(
                """
                SELECT provider,
                       COUNT(*) AS reqs,
                       COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS ok_reqs,
                       COALESCE(SUM(tokens_input), 0) AS tin,
                       COALESCE(SUM(tokens_output), 0) AS tout,
                       ROUND(AVG(COALESCE(latency_ms, 0)), 0) AS avg_ms,
                       MAX(timestamp) AS last_ts
                FROM usage_history
                WHERE timestamp >= ?
                GROUP BY provider
                ORDER BY reqs DESC
                LIMIT 10
                """,
                (iso_7d,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {"available": False, "error": str(exc)[:160], "requests_24h": 0, "ok_24h": 0, "requests_7d": 0, "by_provider": []}

    by_prov = [
        {
            "provider": str(r["provider"] or "unknown"),
            "requests_7d": int(r["reqs"] or 0),
            "ok_7d": int(r["ok_reqs"] or 0),
            "success_rate_pct": round((int(r["ok_reqs"] or 0) / max(1, int(r["reqs"] or 0))) * 100.0, 1),
            "tokens_in": int(r["tin"] or 0),
            "tokens_out": int(r["tout"] or 0),
            "avg_latency_ms": float(r["avg_ms"] or 0.0),
            "last_ts": str(r["last_ts"] or ""),
        }
        for r in rows_7d
    ]
    return {
        "available": True,
        "requests_24h": int(r24["reqs"] or 0) if r24 else 0,
        "ok_24h": int(r24["ok_reqs"] or 0) if r24 else 0,
        "tokens_in_24h": int(r24["tin"] or 0) if r24 else 0,
        "tokens_out_24h": int(r24["tout"] or 0) if r24 else 0,
        "requests_7d": sum(x["requests_7d"] for x in by_prov),
        "by_provider": by_prov,
    }


def _collect_ledger_ai_spend(db: SimingDB) -> dict[str, Any]:
    """从 `ledger_txns` 提取近 6 个月真实 AI 算力账单（严格隔离 CNY 与 USD，剔除划转与 VOID）。"""
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT occurred_at, amount_cents, currency, kind, merchant, notes
            FROM ledger_txns
            WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
              AND kind IN ('消费', '退款')
            ORDER BY occurred_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    by_month_cny: dict[str, int] = defaultdict(int)
    by_month_usd: dict[str, int] = defaultdict(int)
    vendors_cny: dict[str, dict[str, int]] = defaultdict(lambda: {"cents": 0, "count": 0})
    vendors_usd: dict[str, dict[str, int]] = defaultdict(lambda: {"cents": 0, "count": 0})
    cur_ym = _db_mod.now_local().strftime("%Y-%m")
    months_seen: set[str] = set()

    def _vendor_bucket(merchant: str, notes: str) -> str:
        hay = f"{merchant} {notes}".lower()
        for pat, label in (
            ("nous", "Nous Research"),
            ("deepseek", "DeepSeek"),
            ("深度求索", "DeepSeek"),
            ("月之暗面", "Kimi (Moonshot)"),
            ("kimi", "Kimi (Moonshot)"),
            ("moonshot", "Kimi (Moonshot)"),
            ("factory", "Factory AI"),
            ("claude", "Claude / Anthropic"),
            ("anthropic", "Claude / Anthropic"),
            ("openai", "OpenAI / ChatGPT"),
            ("chatgpt", "OpenAI / ChatGPT"),
            ("阿里云", "阿里云百炼"),
            ("aliyun", "阿里云百炼"),
            ("dashscope", "阿里云百炼"),
            ("redpill", "RedPill"),
            ("openrouter", "OpenRouter"),
            ("cursor", "Cursor"),
        ):
            if pat in hay:
                return label
        return (merchant or "其他 AI 算力")[:32]

    matched_rows: list[tuple[str, str, int, str]] = []
    for r in rows:
        merchant = str(r["merchant"] or "")
        notes = str(r["notes"] or "")
        if not _AI_LEDGER_RE.search(f"{merchant} {notes}"):
            continue
        ym = str(r["occurred_at"] or "")[:7]
        if len(ym) != 7 or ym[4] != "-":
            continue
        ccy = str(r["currency"] or "CNY").strip().upper()
        if ccy == "RMB":
            ccy = "CNY"
        amt = int(r["amount_cents"] or 0)
        delta = amt if str(r["kind"]) == "消费" else -amt
        vb = _vendor_bucket(merchant, notes)
        months_seen.add(ym)
        matched_rows.append((ym, ccy, delta, vb))
        if ccy == "USD":
            by_month_usd[ym] += delta
        elif ccy == "CNY":
            by_month_cny[ym] += delta

    sorted_months = sorted(months_seen)[-6:]
    if not sorted_months:
        sorted_months = [cur_ym]
    window_set = set(sorted_months)
    for ym, ccy, delta, vb in matched_rows:
        if ym not in window_set:
            continue
        if ccy == "USD":
            vendors_usd[vb]["cents"] += delta
            if delta > 0:
                vendors_usd[vb]["count"] += 1
        elif ccy == "CNY":
            vendors_cny[vb]["cents"] += delta
            if delta > 0:
                vendors_cny[vb]["count"] += 1

    monthly = [
        {
            "month": ym,
            "cny": round(max(0, by_month_cny.get(ym, 0)) / 100.0, 2),
            "usd": round(max(0, by_month_usd.get(ym, 0)) / 100.0, 2),
        }
        for ym in sorted_months
    ]
    cur_m = sorted_months[-1]
    return {
        "current_month": cur_m,
        "current_month_cny": round(max(0, by_month_cny.get(cur_m, 0)) / 100.0, 2),
        "current_month_usd": round(max(0, by_month_usd.get(cur_m, 0)) / 100.0, 2),
        "total_6m_cny": round(sum(max(0, by_month_cny.get(m, 0)) for m in sorted_months) / 100.0, 2),
        "total_6m_usd": round(sum(max(0, by_month_usd.get(m, 0)) for m in sorted_months) / 100.0, 2),
        "monthly": monthly,
        "vendors_cny": [
            {"vendor": k, "amount": round(v["cents"] / 100.0, 2), "count": v["count"]}
            for k, v in sorted(vendors_cny.items(), key=lambda x: x[1]["cents"], reverse=True)
            if v["cents"] > 0
        ][:8],
        "vendors_usd": [
            {"vendor": k, "amount": round(v["cents"] / 100.0, 2), "count": v["count"]}
            for k, v in sorted(vendors_usd.items(), key=lambda x: x[1]["cents"], reverse=True)
            if v["cents"] > 0
        ][:8],
    }


# ===========================================================================
# 4. Pool Merger, Sync to compute_assets, Circuit Control & Main Payload
# ===========================================================================


def _load_manual_open_circuits(db: SimingDB) -> set[str]:
    opened: set[str] = set()
    conn = db.connect()
    try:
        for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'runtime_circuit:%'").fetchall():
            if str(r["value"] or "").strip().upper() == "OPEN":
                opened.add(_canon_provider(str(r["key"]).split(":", 1)[1]))
        for r in conn.execute("SELECT provider FROM compute_assets WHERE UPPER(status) = 'CIRCUIT_OPEN'").fetchall():
            opened.add(_canon_provider(str(r["provider"] or "")))
    finally:
        conn.close()
    return opened


def _merge_pools(
    *,
    magpie_items: list[dict[str, Any]],
    usagebar_items: list[dict[str, Any]],
    ai_quota: dict[str, Any],
    db_assets: list[dict[str, Any]],
    open_circuits: set[str],
    nous_sentinel: dict[str, Any],
    extra_items: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """按 provider 合并去重 Magpie / ai-usagebar / ai-quota / compute_assets，保留最鲜活完整数据。"""
    by_key: dict[str, dict[str, Any]] = {}
    asset_by_prov: dict[str, dict[str, Any]] = {}
    for a in db_assets:
        p = _canon_provider(str(a.get("provider") or ""))
        asset_by_prov.setdefault(p, a)

    # 1. 先载入 Magpie（覆盖最广：Codex / WorkBuddy / Factory / DeepSeek / Kimi / OpenRouter / Zhipu / Ollama Cloud）
    factory_seen = False
    for it in magpie_items:
        prov = it["provider"]
        # Magpie 可能返回同一 Factory 账号的 OAuth 与 API key 两条完全一致额度，合并保留首选用户账号
        if prov == "factory":
            if factory_seen:
                continue
            factory_seen = True
            k = "factory"
        else:
            k = prov
        item_copy = dict(it)
        item_copy["extra_notes"] = list(it.get("extra_notes") or [])
        by_key[k] = item_copy

    # 2. 叠加 ai-usagebar（补充 Antigravity / Nous Research，并用更新鲜的余额或窗口富化）
    for it in usagebar_items:
        prov = it["provider"]
        if prov == "factory" and "factory" in by_key and not by_key["factory"].get("stale"):
            continue
        existing = by_key.get(prov)
        if existing is None:
            item_copy = dict(it)
            item_copy["extra_notes"] = list(it.get("extra_notes") or [])
            by_key[prov] = item_copy
            continue
        # 若已有记录陈旧、无余额，或 usagebar 采集时间更新鲜，则用更新鲜的数据覆盖
        ex_ts = _parse_iso_ts(str(existing.get("updated_at") or ""))
        it_ts = _parse_iso_ts(str(it.get("updated_at") or ""))
        it_is_newer = (not it.get("stale")) and (
            existing.get("stale")
            or (ex_ts is not None and it_ts is not None and it_ts > ex_ts)
        )
        if it_is_newer:
            if it.get("balance") is not None:
                existing["balance"] = it["balance"]
                existing["currency"] = it["currency"]
                existing["balance_display"] = it["balance_display"]
            if it.get("windows") and (existing.get("stale") or not existing.get("windows")):
                existing["windows"] = it["windows"]
            if it["source"] not in str(existing.get("source") or ""):
                existing["source"] = f"{existing['source']}+{it['source']}"
            existing["stale"] = False
            existing["updated_at"] = it.get("updated_at") or existing.get("updated_at")
        elif existing.get("balance") is None and it.get("balance") is not None:
            existing["balance"] = it["balance"]
            existing["currency"] = it["currency"]
            existing["balance_display"] = it["balance_display"]
        if it.get("extra_notes"):
            bal_mismatch = (
                existing.get("balance") is not None
                and it.get("balance") is not None
                and abs(float(existing["balance"]) - float(it["balance"])) > 0.005
            )
            notes = list(existing.get("extra_notes") or [])
            for n in it["extra_notes"]:
                if bal_mismatch and str(n).lower().startswith("breakdown:"):
                    continue
                if n not in notes:
                    notes.append(n)
            existing["extra_notes"] = notes[:4]

    # 2b. 叠加直连账单探针（如 RedPill 组织余额）
    for it in extra_items or []:
        prov = it["provider"]
        item_copy = dict(it)
        item_copy["extra_notes"] = list(it.get("extra_notes") or [])
        by_key[prov] = item_copy

    # 3. 补充 ai-quota 中的 Claude Code 状态（若尚无 claude 卡片）
    if ai_quota.get("ok") and "claude" not in by_key:
        cl_avail = bool(ai_quota.get("claude_available"))
        cl_rem = _safe_float(ai_quota.get("claude_remaining"))
        cl_wins: list[dict[str, Any]] = []
        for raw_it in ai_quota.get("items") or []:
            if isinstance(raw_it, dict) and "claude" in str(raw_it.get("name") or "").lower():
                rem_p = _safe_float(raw_it.get("remaining_percent"))
                used_p = round(100.0 - rem_p, 1) if rem_p is not None else None
                cl_wins.append(
                    {
                        "name": str(raw_it.get("name")),
                        "used_pct": used_p,
                        "remaining_pct": rem_p,
                        "display": str(raw_it.get("label") or "unknown"),
                        "resets_at": "",
                        "detail": str(raw_it.get("detail") or ""),
                        "severity": _window_severity(used_p),
                    }
                )
        by_key["claude"] = {
            "pool_id": "ai_quota:claude",
            "provider": "claude",
            "raw_provider": "claude",
            "display_name": "Claude Code",
            "account_label": "local-cli",
            "kind": "subscription",
            "plan": "Coding Plan",
            "source": "ai-quota",
            "balance": None,
            "currency": "",
            "balance_display": "",
            "windows": cl_wins,
            "reset_credits": None,
            "extra_notes": [ai_quota.get("claude_label") or ("可用" if cl_avail else "暂未暴露实时配额")],
            "updated_at": str(ai_quota.get("generated_at") or ""),
            "stale": False,
            "error": str(ai_quota.get("claude_error") or "") if not cl_avail else "",
        }

    # 4. 补齐 compute_assets 表中人工登记但未被外部探针覆盖的平台（如 aliyun / redpill / custom）
    for a in db_assets:
        prov = _canon_provider(str(a.get("provider") or ""))
        bal = _safe_float(a.get("balance"))
        ccy = str(a.get("currency") or "CNY").upper()
        try:
            a_meta = json.loads(str(a.get("meta_json") or "{}"))
            if not isinstance(a_meta, dict):
                a_meta = {}
        except ValueError:
            a_meta = {}
        a_billing_url = str(a_meta.get("billing_url") or "").strip()
        if prov in by_key:
            p_obj = by_key[prov]
            p_obj["asset_id"] = str(a.get("asset_id") or "")
            p_obj["billing_scope"] = str(a.get("billing_scope") or "")
            p_obj["daily_spend"] = float(a.get("daily_spend") or 0.0)
            p_obj["avg_daily_spend"] = float(a.get("avg_daily_spend") or 0.0)
            p_obj["plan_expires_at"] = str(a.get("plan_expires_at") or "")
            p_obj["fallback_model"] = str(a.get("fallback_model") or "")
            p_obj["db_status"] = str(a.get("status") or "OK")
            if a_billing_url and not p_obj.get("billing_url"):
                p_obj["billing_url"] = a_billing_url
            if p_obj.get("balance") is None and bal is not None:
                p_obj["balance"] = bal
                p_obj["currency"] = ccy
                p_obj["balance_display"] = _fmt_balance(bal, ccy)
        else:
            by_key[f"db:{a['asset_id']}"] = {
                "pool_id": f"db:{a['asset_id']}",
                "asset_id": str(a["asset_id"]),
                "provider": prov,
                "raw_provider": str(a.get("provider") or ""),
                "display_name": _PROVIDER_DISPLAY.get(prov, str(a.get("provider") or "").upper()),
                "account_label": str(a.get("account_label") or "default"),
                "kind": "balance",
                "plan": str(a.get("plan_name") or ""),
                "source": "compute_assets",
                "balance": bal,
                "currency": ccy,
                "balance_display": _fmt_balance(bal, ccy),
                "windows": [],
                "reset_credits": None,
                "extra_notes": [],
                "billing_url": a_billing_url,
                "updated_at": str(a.get("updated_at") or ""),
                "stale": False,
                "error": "",
                "billing_scope": str(a.get("billing_scope") or "personal"),
                "daily_spend": float(a.get("daily_spend") or 0.0),
                "avg_daily_spend": float(a.get("avg_daily_spend") or 0.0),
                "plan_expires_at": str(a.get("plan_expires_at") or ""),
                "fallback_model": str(a.get("fallback_model") or ""),
                "db_status": str(a.get("status") or "OK"),
            }

    # 5. 计算每张卡的综合状态、熔断标志与 Nous 哨兵信息
    pools: list[dict[str, Any]] = []
    priority_order = {
        "codex": 0,
        "antigravity": 1,
        "nous": 2,
        "deepseek": 3,
        "kimi": 4,
        "factory": 5,
        "workbuddy": 6,
        "claude": 7,
        "aliyun": 8,
        "redpill": 9,
        "openrouter": 10,
    }
    for p in by_key.values():
        prov = p["provider"]
        is_circuit_open = (prov in open_circuits) or (str(p.get("db_status") or "").upper() == "CIRCUIT_OPEN")
        p["circuit_open"] = is_circuit_open
        if prov == "nous" and nous_sentinel:
            notes_list = list(p.get("extra_notes") or [])
            if nous_sentinel.get("is_locked"):
                msg = "🚨 Nous 付款锁/AUTO REFILL 异常"
                if msg not in notes_list:
                    notes_list.insert(0, msg)
            else:
                msg = "AUTO REFILL 哨兵正常"
                if msg not in notes_list:
                    notes_list.append(msg)
            p["extra_notes"] = notes_list

        # 确定综合状态
        max_win_sev = "ok"
        for w in p.get("windows") or []:
            sv = w.get("severity")
            if sv == "critical":
                max_win_sev = "critical"
                break
            if sv == "warn":
                max_win_sev = "warn"

        status = "OK"
        if is_circuit_open:
            status = "CIRCUIT_OPEN"
        elif prov == "nous" and nous_sentinel.get("is_locked"):
            status = "CRITICAL"
        elif p.get("error") and p.get("balance") is None and not p.get("windows"):
            status = "UNAVAILABLE"
        elif max_win_sev == "critical":
            status = "CRITICAL"
        elif str(p.get("db_status") or "").upper() in ("BURN_SPIKE", "LOW_BALANCE", "EXPIRING"):
            status = str(p["db_status"]).upper()
        elif max_win_sev == "warn":
            status = "WARN"
        elif p.get("balance") is not None and p.get("currency") in ("CNY", "USD"):
            low_th = 30.0 if p["currency"] == "USD" else 200.0
            if float(p["balance"]) < low_th and prov in ("deepseek", "nous", "kimi", "aliyun", "redpill"):
                status = "LOW_BALANCE"

        p["status"] = status
        p.setdefault("billing_scope", "work_cw08" if prov in _DEFAULT_CW08_PROVIDERS else "personal")
        pools.append(p)

    pools.sort(
        key=lambda x: (
            0 if x["status"] in ("CIRCUIT_OPEN", "CRITICAL") else (1 if x["status"] in ("WARN", "LOW_BALANCE", "EXPIRING") else 2),
            priority_order.get(x["provider"], 50),
            x["display_name"],
        )
    )
    return pools


def _collect_alerts(
    pools: list[dict[str, Any]],
    guardrails: dict[str, Any],
    nous_sentinel: dict[str, Any],
    host: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _add(key: str, severity: str, category: str, title: str, detail: str) -> None:
        if key in seen_keys:
            return
        seen_keys.add(key)
        alerts.append({"key": key, "severity": severity, "category": category, "title": title, "detail": detail})

    if nous_sentinel.get("is_locked"):
        _add(
            "nous:lock",
            "critical",
            "billing",
            "Nous 付款锁 / AUTO REFILL 断档",
            str((nous_sentinel.get("latest_fail") or {}).get("summary") or "检测到付款受限或 AUTO REFILL 关闭"),
        )

    for p in pools:
        prov = p["provider"]
        name = p["display_name"]
        if p.get("circuit_open"):
            _add(f"circuit:{prov}", "critical", "circuit", f"{name} 处于熔断 (CIRCUIT_OPEN)", "自动路由将跳过该平台并切换健康备用模型")
        for w in p.get("windows") or []:
            used = w.get("used_pct")
            rem = w.get("remaining_pct")
            if rem is None and used is not None:
                rem = max(0.0, round(100.0 - float(used), 1))
            reset_disp = w.get("resets_at_local") or _fmt_local_dt(w.get("resets_at"))
            if used is not None and used >= 95.0:
                _add(
                    f"win:{prov}:{w['name']}",
                    "critical",
                    "quota",
                    f"{name} · {w['name']} 额度已耗尽 (余量 {rem:.0f}%)",
                    w.get("detail") or (f"重置时间 {reset_disp}" if reset_disp else "等待窗口重置"),
                )
            elif used is not None and used >= 80.0:
                _add(
                    f"win:{prov}:{w['name']}",
                    "warn",
                    "quota",
                    f"{name} · {w['name']} 额度偏紧 (余量 {rem:.1f}% · 已用 {used:.1f}%)",
                    w.get("detail") or "",
                )
        if p["status"] == "LOW_BALANCE":
            _add(
                f"lowbal:{prov}",
                "warn",
                "balance",
                f"{name} 余额偏低 ({p.get('balance_display')})",
                "建议关注额度或在算力账本核对消耗速度",
            )

    for g_alert in guardrails.get("alerts") or []:
        g_prov = _canon_provider(str(g_alert.get("provider") or ""))
        g_kind = str(g_alert.get("kind") or "")
        if g_kind == "low_balance" and f"lowbal:{g_prov}" in seen_keys:
            continue
        if g_kind == "burn_spike" and f"circuit:{g_prov}" in seen_keys:
            continue
        _add(
            f"guard:{g_alert.get('asset_id')}:{g_kind}",
            "critical" if g_kind == "burn_spike" else "warn",
            "guardrail",
            f"{g_alert.get('provider')}/{g_alert.get('account_label')} 护栏触发",
            str(g_alert.get("message") or ""),
        )

    # Host hardware alerts (only genuine thresholds)
    mem_pct = float((host.get("memory") or {}).get("used_pct") or 0.0)
    if mem_pct >= 90.0:
        _add("host:mem", "warn", "hardware", f"本机内存占用过高 ({mem_pct:.1f}%)", f"可用 {(host.get('memory') or {}).get('available_gib', 0)} GiB")
    cpu_temp = (host.get("cpu") or {}).get("temp_c")
    if isinstance(cpu_temp, (int, float)) and cpu_temp >= 90.0:
        _add("host:cpu_temp", "warn", "hardware", f"本机 CPU 温度偏高 ({cpu_temp:.1f}°C)", "注意散热与后台高负载进程")
    for dk in host.get("disks") or []:
        if float(dk.get("used_pct") or 0.0) >= 88.0:
            _add(f"host:disk:{dk['mount']}", "warn", "hardware", f"磁盘 {dk['mount']} 空间告警 ({dk['used_pct']}%)", f"剩余 {dk['free_gib']} GiB")

    alerts.sort(key=lambda a: (0 if a["severity"] == "critical" else 1, a["category"], a["title"]))
    return alerts


def sync_live_compute_assets(
    db: SimingDB,
    *,
    pools: Optional[list[dict[str, Any]]] = None,
    allow_live: Optional[bool] = None,
) -> dict[str, Any]:
    """将实时探测到的真货币 API 余额（CNY/USD）幂等同步至 `compute_assets` 表，保留人工自定义归属与套餐信息。"""
    db.init_db()
    can_live = _live_sources_allowed() if allow_live is None else allow_live
    if pools is None:
        if not can_live:
            return {"ok": True, "synced": 0, "skipped": "offline", "providers": []}
        mag = fetch_magpie_quotas(allow_http=True, timeout=6.0)
        ub = fetch_usagebar_quotas(live=True, timeout=8.0)
        aq = fetch_ai_quota_local(db, timeout=1.5)
        rp = fetch_redpill_billing(db, live=True, timeout=5.0)
        conn = db.connect()
        try:
            db_assets = [dict(r) for r in conn.execute("SELECT * FROM compute_assets").fetchall()]
        finally:
            conn.close()
        pools = _merge_pools(
            magpie_items=mag["items"],
            usagebar_items=ub["items"],
            ai_quota=aq,
            db_assets=db_assets,
            open_circuits=_load_manual_open_circuits(db),
            nous_sentinel={},
            extra_items=rp.get("items") or [],
        )

    conn = db.connect()
    try:
        existing_rows = {
            _canon_provider(str(r["provider"])): dict(r)
            for r in conn.execute("SELECT * FROM compute_assets").fetchall()
        }
    finally:
        conn.close()

    synced_providers: list[str] = []
    for p in pools:
        prov = p.get("provider") or ""
        bal = p.get("balance")
        ccy = (p.get("currency") or "").upper()
        # 仅同步真实可核对的货币余额（CNY / USD），避免把 credits/魔粒 或未知余额写入货币台账
        if bal is None or ccy not in ("CNY", "USD"):
            continue
        if p.get("source") == "compute_assets":
            continue
        # 跳过纯 0 余额的未启用免费账号（如 zhipu 0 / ollama-cloud 0），除非库里原本就有记录
        if float(bal) <= 0.0 and prov not in existing_rows and prov not in ("deepseek", "nous", "kimi"):
            continue

        ex = existing_rows.get(prov)
        if ex:
            ex_meta = {}
            try:
                ex_meta = json.loads(str(ex.get("meta_json") or "{}"))
            except ValueError:
                ex_meta = {}
            ex_meta["live_source"] = p.get("source", "")
            ex_meta["live_synced_at"] = iso_now()
            if p.get("billing_url"):
                ex_meta["billing_url"] = str(p["billing_url"])
            upsert_compute_asset(
                db,
                provider=str(ex["provider"]),
                account_label=str(ex["account_label"] or "live"),
                balance=float(bal),
                currency=ccy,
                daily_spend=float(ex.get("daily_spend") or 0.0),
                avg_daily_spend=float(ex.get("avg_daily_spend") or 0.0),
                plan_name=str(ex.get("plan_name") or p.get("plan") or ""),
                plan_expires_at=str(ex.get("plan_expires_at") or ""),
                billing_scope=str(ex.get("billing_scope") or ("work_cw08" if prov in _DEFAULT_CW08_PROVIDERS else "personal")),
                fallback_model=str(ex.get("fallback_model") or ""),
                meta=ex_meta,
            )
            # 若原先处于人工熔断状态则维持 CIRCUIT_OPEN
            if str(ex.get("status") or "").upper() == "CIRCUIT_OPEN" or ex_meta.get("manual_circuit_open"):
                with db.transaction() as wconn:
                    wconn.execute(
                        "UPDATE compute_assets SET status = 'CIRCUIT_OPEN' WHERE asset_id = ?",
                        (ex["asset_id"],),
                    )
        else:
            new_meta: dict[str, Any] = {"live_source": p.get("source", ""), "live_synced_at": iso_now()}
            if p.get("billing_url"):
                new_meta["billing_url"] = str(p["billing_url"])
            upsert_compute_asset(
                db,
                provider=prov,
                account_label=str(p.get("account_label") or "live"),
                balance=float(bal),
                currency=ccy,
                daily_spend=0.0,
                avg_daily_spend=0.0,
                plan_name=str(p.get("plan") or p.get("display_name") or ""),
                plan_expires_at="",
                billing_scope="work_cw08" if prov in _DEFAULT_CW08_PROVIDERS else "personal",
                fallback_model=DEFAULT_FALLBACK_MODELS.get(prov, ("deepseek", "deepseek-chat"))[1],
                meta=new_meta,
            )
        synced_providers.append(prov)

    if synced_providers:
        try:
            db.mark_source_ok("compute_monitor", detail=f"synced={len(synced_providers)} ({','.join(synced_providers[:6])})")
        except Exception:
            pass
    return {"ok": True, "synced": len(synced_providers), "providers": synced_providers}


def set_provider_circuit(db: SimingDB, provider: str, is_open: bool) -> dict[str, Any]:
    """一键熔断或恢复指定算力平台（同时作用于 `multi_runtime` 路由与 `compute_assets` 表）。"""
    db.init_db()
    prov = _canon_provider(provider)
    if not prov or prov == "unknown" or not re.fullmatch(r"[a-z0-9_.-]{1,48}", prov):
        return {"ok": False, "error": f"无效的 provider: {provider}"}

    ts = iso_now()
    state_val = "OPEN" if is_open else "CLOSED"
    with db.transaction() as conn:
        db.set_meta(f"runtime_circuit:{prov}", state_val, conn=conn)
        rows = conn.execute("SELECT asset_id, provider, meta_json FROM compute_assets").fetchall()
        updated_assets: list[str] = []
        for r in rows:
            if _canon_provider(str(r["provider"] or "")) != prov:
                continue
            try:
                meta = json.loads(str(r["meta_json"] or "{}"))
                if not isinstance(meta, dict):
                    meta = {}
            except ValueError:
                meta = {}
            meta["manual_circuit_open"] = bool(is_open)
            new_st = "CIRCUIT_OPEN" if is_open else "OK"
            conn.execute(
                "UPDATE compute_assets SET status = ?, meta_json = ?, updated_at = ? WHERE asset_id = ?",
                (new_st, json.dumps(meta, ensure_ascii=False), ts, r["asset_id"]),
            )
            updated_assets.append(str(r["asset_id"]))

    if not is_open:
        evaluate_compute_guardrails(db, notify=False)

    return {
        "ok": True,
        "provider": prov,
        "circuit_open": bool(is_open),
        "state": state_val,
        "updated_assets": updated_assets,
        "updated_at": ts,
    }


def _fetch_external_bundle_parallel(db: SimingDB, *, live: bool) -> dict[str, Any]:
    """并行拉取 Magpie / ai-usagebar / ai-quota / RedPill / OmniRoute 健康，最短墙钟耗时。"""
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_mag = pool.submit(fetch_magpie_quotas, allow_http=True, timeout=6.0 if live else 2.0)
        f_ub = pool.submit(fetch_usagebar_quotas, live=live, timeout=8.0)
        f_aq = pool.submit(fetch_ai_quota_local, db, 1.5)
        f_rp = pool.submit(fetch_redpill_billing, db, live=live, timeout=5.0)
        mag = f_mag.result()
        ub = f_ub.result()
        aq = f_aq.result()
        rp = f_rp.result()
    services = _probe_local_services(db, magpie_res=mag, ai_quota_res=aq, allow_http=True)
    return {
        "fetched_at_mono": time.monotonic(),
        "fetched_at_iso": iso_now(),
        "magpie": mag,
        "usagebar": ub,
        "ai_quota": aq,
        "redpill": rp,
        "services": services,
    }


def _trigger_background_warm(db: SimingDB) -> None:
    with _CACHE_LOCK:
        if _LIVE_CACHE["warming"]:
            return
        _LIVE_CACHE["warming"] = True

    def _job() -> None:
        try:
            bundle = _fetch_external_bundle_parallel(db, live=True)
            with _CACHE_LOCK:
                _LIVE_CACHE.update(bundle)
        except Exception:
            pass
        finally:
            with _CACHE_LOCK:
                _LIVE_CACHE["warming"] = False

    threading.Thread(target=_job, name="siming-compute-warm", daemon=True).start()


def build_compute_monitor_payload(
    db: SimingDB,
    *,
    live: bool = False,
    sync_assets: bool = False,
    host_collector: Optional[Callable[[], dict[str, Any]]] = None,
    magpie_collector: Optional[Callable[[], dict[str, Any]]] = None,
    usagebar_collector: Optional[Callable[[], dict[str, Any]]] = None,
    ai_quota_collector: Optional[Callable[[], dict[str, Any]]] = None,
    redpill_collector: Optional[Callable[[], dict[str, Any]]] = None,
    magpie_usage_file: Optional[Path] = None,
    omniroute_db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """构建控制塔「⚡ 算力资源监控」全量载荷。"""
    db.init_db()
    can_live = _live_sources_allowed()
    has_custom_injections = any(
        x is not None
        for x in (magpie_collector, usagebar_collector, ai_quota_collector, redpill_collector, magpie_usage_file, omniroute_db_path)
    )

    cache_state = "offline"
    if has_custom_injections:
        mag = magpie_collector() if magpie_collector else {"ok": False, "running": False, "mode": "offline", "items": [], "error": ""}
        ub = usagebar_collector() if usagebar_collector else {"ok": False, "mode": "offline", "items": [], "error": ""}
        aq = ai_quota_collector() if ai_quota_collector else {"ok": False, "running": False, "items": []}
        rp = redpill_collector() if redpill_collector else {"ok": False, "mode": "offline", "items": [], "error": ""}
        services = _probe_local_services(db, magpie_res=mag, ai_quota_res=aq, allow_http=False)
        cache_state = "live"
    elif not can_live:
        mag = {"ok": False, "running": False, "mode": "offline", "items": [], "error": ""}
        ub = {"ok": False, "mode": "offline", "items": [], "error": ""}
        aq = {"ok": False, "running": False, "items": []}
        rp = {"ok": False, "mode": "offline", "items": [], "error": ""}
        services = _probe_local_services(db, magpie_res=mag, ai_quota_res=aq, allow_http=False)
        cache_state = "offline"
    else:
        now_mono = time.monotonic()
        with _CACHE_LOCK:
            cached_fresh = (
                _LIVE_CACHE["magpie"] is not None
                and (now_mono - float(_LIVE_CACHE["fetched_at_mono"])) < _LIVE_CACHE_TTL_SEC
            )
            cached_snap = dict(_LIVE_CACHE) if _LIVE_CACHE["magpie"] is not None else None
        if live:
            bundle = _fetch_external_bundle_parallel(db, live=True)
            with _CACHE_LOCK:
                _LIVE_CACHE.update(bundle)
            mag, ub, aq, rp, services = bundle["magpie"], bundle["usagebar"], bundle["ai_quota"], bundle["redpill"], bundle["services"]
            cache_state = "live"
        elif cached_fresh and cached_snap is not None:
            mag = cached_snap["magpie"]
            ub = cached_snap["usagebar"]
            aq = cached_snap["ai_quota"]
            rp = cached_snap.get("redpill") or {"ok": False, "mode": "offline", "items": [], "error": ""}
            services = cached_snap["services"]
            cache_state = "cached"
        else:
            # 首次/过期冷读：先毫秒级读取本地磁盘缓存 + 超快 ai-quota (10ms) + 已开标签 RedPill，同时后台线程拉取全量实时数据
            mag = fetch_magpie_quotas(allow_http=False)
            ub = fetch_usagebar_quotas(live=False)
            aq = fetch_ai_quota_local(db, timeout=1.0)
            rp = fetch_redpill_billing(db, live=False, timeout=2.0)
            services = _probe_local_services(db, magpie_res=mag, ai_quota_res=aq, allow_http=True)
            _trigger_background_warm(db)
            cache_state = "warming"

    # DB tables: compute_assets, usage, overnight_handoffs, task_runs
    conn = db.connect()
    try:
        db_assets = [dict(r) for r in conn.execute("SELECT * FROM compute_assets ORDER BY billing_scope, provider").fetchall()]
        usage_agg = conn.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tin, COALESCE(SUM(tokens_out),0) AS tout, COALESCE(SUM(cost_cny),0.0) AS cost FROM usage"
        ).fetchone()
        recent_usage = [
            dict(r)
            for r in conn.execute(
                "SELECT id, ts, model, runner, tokens_in, tokens_out, cost_cny, ref_id FROM usage ORDER BY ts DESC LIMIT 12"
            ).fetchall()
        ]
        handoffs = []
        for r in conn.execute("SELECT * FROM overnight_handoffs ORDER BY updated_at DESC LIMIT 12").fetchall():
            d = dict(r)
            try:
                d["metrics"] = json.loads(str(d.pop("metrics_json", "{}") or "{}"))
            except ValueError:
                d["metrics"] = {}
            try:
                d["whitelist"] = json.loads(str(d.pop("whitelist_json", "[]") or "[]"))
            except ValueError:
                d["whitelist"] = []
            handoffs.append(d)
        task_runs = [
            dict(r)
            for r in conn.execute(
                "SELECT run_id, kind, name, status, pid, cwd, target_pane, exit_code, started_at, finished_at FROM task_runs ORDER BY started_at DESC LIMIT 12"
            ).fetchall()
        ]
    finally:
        conn.close()

    # Nous 付款锁哨兵（纯只读）
    nous_sentinel: dict[str, Any] = {"ok": True, "is_locked": False}
    try:
        from siming.self_heal import evaluate_nous_billing_sentinel

        ns = evaluate_nous_billing_sentinel(db, notify=False)
        nous_sentinel = {
            "ok": bool(ns.get("ok", True)),
            "is_locked": bool(ns.get("is_locked")),
            "case_id": str(ns.get("case_id") or ""),
            "latest_fail": ns.get("latest_fail"),
            "latest_ok": ns.get("latest_ok"),
        }
    except Exception:
        pass

    open_circuits = _load_manual_open_circuits(db)
    rp_items = rp.get("items") or []
    pools = _merge_pools(
        magpie_items=mag.get("items") or [],
        usagebar_items=ub.get("items") or [],
        ai_quota=aq,
        db_assets=db_assets,
        open_circuits=open_circuits,
        nous_sentinel=nous_sentinel,
        extra_items=rp_items,
    )

    sync_result: Optional[dict[str, Any]] = None
    if sync_assets:
        sync_result = sync_live_compute_assets(db, pools=pools, allow_live=True)
        conn = db.connect()
        try:
            db_assets = [dict(r) for r in conn.execute("SELECT * FROM compute_assets ORDER BY billing_scope, provider").fetchall()]
        finally:
            conn.close()
        pools = _merge_pools(
            magpie_items=mag.get("items") or [],
            usagebar_items=ub.get("items") or [],
            ai_quota=aq,
            db_assets=db_assets,
            open_circuits=_load_manual_open_circuits(db),
            nous_sentinel=nous_sentinel,
            extra_items=rp_items,
        )

    guardrails = evaluate_compute_guardrails(db, notify=False)
    host = host_collector() if host_collector is not None else collect_host_resources()
    alerts = _collect_alerts(pools, guardrails, nous_sentinel, host)

    # AI usage telemetry
    if magpie_usage_file is not None or can_live:
        magpie_usage = collect_magpie_usage_stats(usage_file=magpie_usage_file)
    else:
        magpie_usage = collect_magpie_usage_stats(usage_file=Path("/nonexistent-siming-offline-usage.jsonl"))
    if omniroute_db_path is not None or can_live:
        omni_usage = collect_omniroute_usage_stats(db_path=omniroute_db_path)
    else:
        omni_usage = collect_omniroute_usage_stats(db_path=Path("/nonexistent-siming-offline-omni.sqlite"))

    ledger_spend = _collect_ledger_ai_spend(db)

    # Multi-runtime routing profiles & active recommendations
    profiles = get_runtime_profiles(db)
    quota_for_rt = aq if aq.get("ok") else {"ok": False, "claude_available": True, "codex_available": True}
    heavy_pick = select_runtime(db, task_kind="coding", complexity="high", quota_snapshot=quota_for_rt)
    light_pick = select_runtime(db, task_kind="triage", complexity="low", quota_snapshot=quota_for_rt)

    # Summary KPIs (CNY and USD strictly separated!)
    tot_cny = round(sum(float(p["balance"]) for p in pools if p.get("balance") is not None and p.get("currency") == "CNY"), 2)
    tot_usd = round(sum(float(p["balance"]) for p in pools if p.get("balance") is not None and p.get("currency") == "USD"), 2)
    req_24h = int(magpie_usage.get("requests_24h") or 0) + int(omni_usage.get("requests_24h") or 0)
    ok_24h = int(magpie_usage.get("ok_24h") or 0) + int(omni_usage.get("ok_24h") or 0)
    tok_24h = (
        int(magpie_usage.get("tokens_total_24h") or 0)
        + int(omni_usage.get("tokens_in_24h") or 0)
        + int(omni_usage.get("tokens_out_24h") or 0)
    )
    gpus = host.get("gpus") or []
    primary_gpu = gpus[0] if gpus else None

    return {
        "ok": True,
        "generated_at": iso_now(),
        "cache_state": cache_state,
        "summary": {
            "total_balance_cny": tot_cny,
            "total_balance_usd": tot_usd,
            "pool_count": len(pools),
            "db_asset_count": len(db_assets),
            "alert_count": len(alerts),
            "critical_count": sum(1 for a in alerts if a["severity"] == "critical"),
            "circuit_open_count": sum(1 for p in pools if p.get("circuit_open")),
            "requests_24h": req_24h,
            "success_rate_24h_pct": round((ok_24h / max(1, req_24h)) * 100.0, 1) if req_24h else 100.0,
            "tokens_24h": tok_24h,
            "host_cpu_pct": (host.get("cpu") or {}).get("usage_pct"),
            "host_cpu_temp_c": (host.get("cpu") or {}).get("temp_c"),
            "host_mem_pct": (host.get("memory") or {}).get("used_pct"),
            "host_mem_used_gib": (host.get("memory") or {}).get("used_gib"),
            "host_mem_total_gib": (host.get("memory") or {}).get("total_gib"),
            "host_gpu_pct": primary_gpu.get("util_pct") if primary_gpu else None,
            "host_gpu_temp_c": primary_gpu.get("temp_c") if primary_gpu else None,
            "host_gpu_power_w": primary_gpu.get("power_w") if primary_gpu else None,
        },
        "alerts": alerts,
        "pools": pools,
        "compute_assets": db_assets,
        "guardrails": {
            "checked": guardrails.get("checked", 0),
            "circuit_open": guardrails.get("circuit_open", []),
            "low_balance": guardrails.get("low_balance", []),
            "expiring": guardrails.get("expiring", []),
        },
        "ai_quota": aq,
        "nous_sentinel": nous_sentinel,
        "services": services,
        "usage_telemetry": {
            "magpie": magpie_usage,
            "omniroute": omni_usage,
            "siming": {
                "calls": int(usage_agg["calls"] or 0) if usage_agg else 0,
                "tokens_in": int(usage_agg["tin"] or 0) if usage_agg else 0,
                "tokens_out": int(usage_agg["tout"] or 0) if usage_agg else 0,
                "cost_cny": round(float(usage_agg["cost"] or 0.0), 4) if usage_agg else 0.0,
                "recent": recent_usage,
            },
        },
        "ledger_spend": ledger_spend,
        "runtimes": {
            "profiles": [
                {
                    "name": p["name"],
                    "kind": p["kind"],
                    "provider": p["provider"],
                    "model": p["model"],
                    "cost_tier": p["cost_tier"],
                    "cost_per_1k_cny": p["cost_per_1k_cny"],
                    "best_for": p["best_for"],
                    "circuit_open": p["circuit_open"],
                }
                for p in profiles
            ],
            "active_heavy": {
                "runtime": heavy_pick["runtime"],
                "model": heavy_pick["model"],
                "reason": heavy_pick["reason"],
                "fallback_chain": heavy_pick["fallback_chain"],
            },
            "active_light": {
                "runtime": light_pick["runtime"],
                "model": light_pick["model"],
                "reason": light_pick["reason"],
                "fallback_chain": light_pick["fallback_chain"],
            },
        },
        "host": host,
        "remote_jobs": {
            "overnight_handoffs": handoffs,
            "task_runs": task_runs,
        },
        "sync_result": sync_result,
    }


def reset_compute_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _LIVE_CACHE.update(
            {
                "fetched_at_mono": 0.0,
                "fetched_at_iso": "",
                "magpie": None,
                "usagebar": None,
                "ai_quota": None,
                "redpill": None,
                "services": None,
                "warming": False,
            }
        )
