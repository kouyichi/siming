"""紫微个人参考包：把 ziwei-doushu 开源仓提供的数据落到司命自己的数据目录，供晨札/查询引用。

数据来源（Renhuai123/ziwei-doushu）：
1. 本命 13 维解读：Release `v4.0-13d` 的 year-YYYY.jsonl.zst，按 ~/config/ziwei-birth.json 取出本人那一张盘
   （CC BY 4.0，引用须保留「紫微斗数开源解读数据集 v4.0」来源标注；按时辰序号排盘，未做真太阳时校正）；
2. 倪师《天纪·紫微斗数》原话 1848 条：仓内 docs/tianji-ziwei/entries.jsonl（CC BY-NC-SA 4.0，仅个人非商用）；
3. 《天纪》17 集逐帧精读公开摘要：仓内 research/nishi-video-reading/summaries/*.md。

落盘位置：<data_dir>/ziwei/（权限 0600，含个人命盘不入 git）。`siming ziwei sync` 重建。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from siming.db import SimingDB, iso_now

DEFAULT_REPO = Path.home() / "Work" / "ziwei-doushu"
DEFAULT_BIRTH_FILE = Path.home() / "config" / "ziwei-birth.json"
GH_REPO = "Renhuai123/ziwei-doushu"
V4_TAG = "v4.0-13d"

# 维度键 → (中文名, 对应本命宫位)
DIMENSIONS: dict[str, tuple[str, str]] = {
    "overview": ("命格", "命宫"),
    "personality": ("性格", "命宫"),
    "love": ("感情", "夫妻"),
    "career": ("事业", "官禄"),
    "wealth": ("财运", "财帛"),
    "health": ("健康", "疾厄"),
    "family": ("兄弟", "兄弟"),
    "children": ("子女", "子女"),
    "move": ("迁移", "迁移"),
    "friends": ("人际", "仆役"),
    "home": ("田宅", "田宅"),
    "spirit": ("福德", "福德"),
    "parents": ("父母", "父母"),
}
PALACE_TO_DIM: dict[str, str] = {pal: dim for dim, (_n, pal) in DIMENSIONS.items() if dim != "personality"}

# 查询别名：宫名/维度名/英文键都能命中
_DIM_ALIASES: dict[str, str] = {
    **{k: k for k in DIMENSIONS},
    **{name: k for k, (name, _p) in DIMENSIONS.items()},
    **{pal: k for pal, k in PALACE_TO_DIM.items()},
    "命": "overview", "总览": "overview", "命格": "overview", "性格": "personality",
    "夫妻": "love", "婚姻": "love", "工作": "career", "官禄": "career", "财": "wealth", "财帛": "wealth",
    "钱": "wealth", "疾厄": "health", "身体": "health", "交友": "friends", "仆役": "friends", "朋友": "friends",
    "家": "home", "房子": "home", "精神": "spirit", "兴趣": "spirit", "出行": "move",
}


def ref_dir(db: SimingDB) -> Path:
    return db.paths.data_dir / "ziwei"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def parse_dimension(md: str) -> dict[str, Any]:
    """把单维 Markdown 拆成 {summary, headline, advice, sections{标题: 正文}}。"""
    parts = re.split(r"\*\*【(.+?)】\*\*", md or "")
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip()] = parts[i + 1].strip()
    summary_key = next((k for k in sections if k.endswith("总览")), "")
    summary = sections.get(summary_key, "")
    paras = [p.strip() for p in summary.split("\n\n") if p.strip()]
    advice = next((p for p in paras if p.startswith(("建议", "跟", "别", "该", "多", "少"))), paras[-1] if len(paras) > 1 else "")
    return {
        "summary": summary,
        "headline": sections.get("一句话定调", "").strip(),
        "advice": advice,
        "sections": sections,
    }


def _first_sentence(text: str, max_len: int = 60) -> str:
    s = re.sub(r"\s+", "", text or "")
    m = re.match(r"(.+?[。！？；])", s)
    s = m.group(1) if m else s
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _download_year_record(year: int, key: str, cache_dir: Path) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    zst = cache_dir / f"year-{year}.jsonl.zst"
    if not zst.is_file():
        subprocess.run(
            ["gh", "release", "download", V4_TAG, "-R", GH_REPO, "-p", zst.name, "-p", "SHA256SUMS.txt", "-D", str(cache_dir), "--clobber"],
            check=True, capture_output=True, text=True, timeout=180,
        )
        sums = (cache_dir / "SHA256SUMS.txt").read_text(encoding="utf-8")
        want = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(zst.name)), "")
        got = hashlib.sha256(zst.read_bytes()).hexdigest()
        if want and want != got:
            zst.unlink(missing_ok=True)
            raise RuntimeError(f"{zst.name} sha256 校验失败")
    proc = subprocess.run(["zstd", "-dc", "--long=27", str(zst)], capture_output=True, timeout=180, check=True)
    needle = f'"k": "{key}"'.encode()
    needle2 = f'"k":"{key}"'.encode()
    for line in proc.stdout.splitlines():
        if needle in line or needle2 in line:
            return json.loads(line)
    raise RuntimeError(f"v4 数据集里找不到 {key}")


def sync_refpack(
    db: SimingDB,
    repo: Path = DEFAULT_REPO,
    birth_file: Path = DEFAULT_BIRTH_FILE,
    record_override: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """重建个人紫微参考包。record_override 供离线单测注入，跳过下载。"""
    out = ref_dir(db)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    birth = json.loads(birth_file.read_text(encoding="utf-8"))
    y, m, d = (int(x) for x in str(birth["solarDate"]).split("-"))
    key = f"{y:04d}-{m:02d}-{d:02d}-{int(birth['timeIndex'])}-{birth['gender']}"

    if record_override is not None:
        rec = record_override
    else:
        with tempfile.TemporaryDirectory(dir=str(db.paths.tmp_root) if db.paths.tmp_root.is_dir() else None) as td:
            rec = _download_year_record(y, key, Path(td))

    natal = {
        "key": rec.get("k", key),
        "ming_star": rec.get("ms"),
        "dims": {dim: parse_dimension(md) for dim, md in (rec.get("c") or {}).items()},
        "raw": rec.get("c") or {},
    }
    _write_private(out / "natal_v4.json", json.dumps(natal, ensure_ascii=False, indent=1))

    copied: dict[str, Any] = {}
    quotes_src = repo / "docs" / "tianji-ziwei" / "entries.jsonl"
    if quotes_src.is_file():
        shutil.copyfile(quotes_src, out / "tianji_quotes.jsonl")
        os.chmod(out / "tianji_quotes.jsonl", 0o600)
        copied["tianji_quotes"] = sum(1 for _ in quotes_src.open(encoding="utf-8"))
    sum_src = repo / "research" / "nishi-video-reading" / "summaries"
    if sum_src.is_dir():
        dst = out / "tianji_summaries"
        dst.mkdir(exist_ok=True)
        files = sorted(sum_src.glob("*.md"))
        for f in files:
            shutil.copyfile(f, dst / f.name)
        copied["tianji_summaries"] = len(files)

    repo_head = ""
    try:
        repo_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    manifest = {
        "synced_at": iso_now(),
        "birth_key": key,
        "repo": str(repo),
        "repo_head": repo_head,
        "sources": {
            "natal_v4.json": f"{GH_REPO} Release {V4_TAG}（紫微斗数开源解读数据集 v4.0，CC BY 4.0）",
            "tianji_quotes.jsonl": "docs/tianji-ziwei/entries.jsonl（倪海厦《天纪》原话，CC BY-NC-SA 4.0，仅个人非商用）",
            "tianji_summaries/": "research/nishi-video-reading/summaries（《天纪》17 集公开摘要）",
        },
        "counts": {"natal_dims": len(natal["dims"]), **copied},
    }
    _write_private(out / "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    _load_natal_file.cache_clear()
    _load_quotes_file.cache_clear()
    return {"ok": True, "dir": str(out), **manifest}


@lru_cache(maxsize=4)
def _load_natal_file(path_s: str, mtime: float) -> Optional[dict[str, Any]]:
    try:
        return json.loads(Path(path_s).read_text(encoding="utf-8"))
    except Exception:
        return None


def load_natal(db: SimingDB) -> Optional[dict[str, Any]]:
    p = ref_dir(db) / "natal_v4.json"
    if not p.is_file():
        return None
    return _load_natal_file(str(p), p.stat().st_mtime)



@lru_cache(maxsize=2)
def _load_quotes_file(path_s: str, mtime: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path_s, encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    rows.append(json.loads(ln))
    except Exception:
        return []
    return rows


def _load_quotes(db: SimingDB) -> list[dict[str, Any]]:
    p = ref_dir(db) / "tianji_quotes.jsonl"
    if not p.is_file():
        return []
    return _load_quotes_file(str(p), p.stat().st_mtime)



def natal_hint(natal: Optional[dict[str, Any]], palace: str) -> Optional[dict[str, str]]:
    """给定本命宫位，返回 {dim_name, headline, advice_short}；无参考包时返回 None。"""
    if not natal:
        return None
    dim = PALACE_TO_DIM.get(palace)
    info = (natal.get("dims") or {}).get(dim or "")
    if not info:
        return None
    return {
        "dim_name": DIMENSIONS[dim][0],
        "headline": str(info.get("headline") or "").strip().rstrip("。"),
        "advice_short": _first_sentence(str(info.get("advice") or ""), 48),
    }


_QUOTE_AVOID = ("死", "坟", "冢", "尸", "夭", "自杀", "病", "癌", "横祸", "开刀", "诸位看", "这里", "这边", "看到没有")


def pick_tianji_quote(db: SimingDB, star: str, day_seed: str) -> Optional[str]:
    """按星曜挑一条倪师原话（只取该星「定性/性情」小节的完整短句，避开凶死病灾与依赖板书指代的口语；同一天同一星恒定）。"""
    if not star:
        return None
    pool = [
        r for r in _load_quotes(db)
        if str(r.get("book") or "") == "01-stars"
        and str(r.get("h2") or "").startswith(star)
        and str(r.get("h3") or "") in ("定性", "性情")
        and 12 <= len(str(r.get("quote") or "")) <= 60
        and not any(w in str(r.get("quote") or "") for w in _QUOTE_AVOID)
    ]
    if not pool:
        return None
    idx = int(hashlib.sha1(f"{day_seed}:{star}".encode()).hexdigest(), 16) % len(pool)
    r = pool[idx]
    return f"倪师论{star}：「{r['quote']}」（《天纪》P{r.get('part', '?')} {r.get('t_hms', '')}）"


def resolve_dimension(query: str) -> Optional[str]:
    q = (query or "").strip().removesuffix("宫").lower()
    return _DIM_ALIASES.get(q) or _DIM_ALIASES.get(query.strip())


def natal_reply(db: SimingDB, query: str = "", full: bool = False) -> str:
    """查询本命解读：无参数给 13 维定调目录；指定维度给该维总览（full=True 给全文）。"""
    natal = load_natal(db)
    if not natal:
        return "还没有本命参考包，先在电脑上跑一次 `siming ziwei sync`。"
    dims = natal.get("dims") or {}
    tail = "\n（来源：紫微斗数开源解读数据集 v4.0 · CC BY 4.0；本命盘视图，未做真太阳时校正）"
    if query.strip() == "全文":
        query = ""
    if not query.strip():
        lines = [f"🔮 本命 13 维定调（{natal.get('key')} · 命宫主星 {natal.get('ming_star') or '无'}）"]
        for dim, (name, pal) in DIMENSIONS.items():
            hl = str((dims.get(dim) or {}).get("headline") or "")
            if hl:
                lines.append(f"• {name}（{pal}）：{hl}")
        lines.append("回「本命 财帛 / 本命 事业 …」看该维总览，加「全文」看完整推演。")
        return "\n".join(lines) + tail
    dim = resolve_dimension(query)
    if not dim or dim not in dims:
        return f"没认出维度「{query}」。可用：{'、'.join(n for n, _ in DIMENSIONS.values())}，或十二宫名。"
    name, pal = DIMENSIONS[dim]
    if full:
        body = str((natal.get("raw") or {}).get(dim) or "")
    else:
        info = dims[dim]
        body = f"{info.get('summary', '')}\n\n一句话定调：{info.get('headline', '')}"
    return f"🔮 本命·{name}（{pal}宫）\n{body.strip()}{tail}"
