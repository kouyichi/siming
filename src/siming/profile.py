"""P2 生活档案单一真源（profile.py）：
把散落在源码里的个人住址、坐标、关键里程碑日期、车辆与医疗参数收口到 `~/.config/siming/profile.yaml`，
未单独配置时平滑回退到 `policy.yaml` 或中性默认值，使搬家/换车/开源脱敏只需维护一份配置。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from siming.config import load_policy
from siming.paths import SimingPaths, get_paths

DEFAULT_PROFILE: dict[str, Any] = {
    "profile_version": 1,
    "timezone": "Asia/Shanghai",
    "places": {
        "home": {"name": "", "lat": None, "lng": None, "wifi_ssids": []},
        "office": {"name": "", "lat": None, "lng": None, "wifi_ssids": []},
        "new_home": {"name": "", "lat": None, "lng": None, "movein_date": ""},
    },
    "milestones": {
        "appliance_install_date": "",
        "movein_date": "",
    },
    "vehicle": {
        "kind": "ev",
        "model": "",
        "km_per_pct": 4.35,
        "auto_climate_min_soc_pct": 25.0,
        "auto_climate_cold_c": 12.0,
        "auto_climate_hot_c": 29.0,
    },
    "cabinet": {
        "boxes_limit": 571,
        "volume_m3_limit": 7.06,
    },
    "calendar": {
        "caldav_url": "https://caldav.icloud.com",
        "account": "",
        "primary_calendar": "Siming",
        "mode": "caldav_rw",
    },
}


def profile_path(paths: Optional[SimingPaths] = None) -> Path:
    p = paths or get_paths()
    return p.config_dir / "profile.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def load_profile(paths: Optional[SimingPaths] = None) -> dict[str, Any]:
    """加载 `~/.config/siming/profile.yaml`，自动兼容并融合 `policy.yaml` 的旧字段。"""
    p = paths or get_paths()
    merged = _deep_merge({}, DEFAULT_PROFILE)
    policy = load_policy(p)

    # 兼容 policy.yaml 已有字段
    if isinstance(policy.get("places"), dict) and policy["places"].get("家"):
        merged["places"]["home"]["name"] = str(policy["places"]["家"])
    if isinstance(policy.get("vehicle"), dict):
        merged["vehicle"] = _deep_merge(merged["vehicle"], policy["vehicle"])
    if isinstance(policy.get("guardrails"), dict):
        g = policy["guardrails"]
        if g.get("boardgame_cabinet_boxes_limit"):
            merged["cabinet"]["boxes_limit"] = int(g["boardgame_cabinet_boxes_limit"])
        if g.get("boardgame_cabinet_volume_m3_limit"):
            merged["cabinet"]["volume_m3_limit"] = float(g["boardgame_cabinet_volume_m3_limit"])
    if isinstance(policy.get("calendar"), dict):
        merged["calendar"] = _deep_merge(merged["calendar"], policy["calendar"])

    fp = profile_path(p)
    if fp.is_file():
        try:
            raw = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                merged = _deep_merge(merged, raw)
        except Exception:
            pass
    return merged


def save_profile(profile: dict[str, Any], paths: Optional[SimingPaths] = None) -> Path:
    p = paths or get_paths()
    p.ensure_dirs()
    fp = profile_path(p)
    fp.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return fp
