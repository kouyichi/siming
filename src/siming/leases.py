"""P2 资源租约互斥管家（leases.py）：
- 短事务内获取/续期/释放 resource_lease(resource_key, mode, holder_run_id, generation, expires_at)；
- 多资源按字典序固定顺序加锁，彻底防死锁；
- 浏览器 9222 分级锁（只读标签共享/独立 tab，涉及购物车/地址/支付升格为账户级独占 browser:9222:account:<platform>）；
- 代码仓库按 worktree:<repo>:<run_id> 隔离，仅合入主干时短暂持 repo:<repo>:<ref> 写锁。
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from siming.db import SimingDB, iso_now, now_local


def browser_tab_resource(tab_id: str, port: int = 9222) -> str:
    return f"browser:{port}:tab:{tab_id}"


def browser_account_resource(platform: str, port: int = 9222) -> str:
    return f"browser:{port}:account:{platform.strip().lower()}"


def repo_ref_resource(repo_name: str, ref: str = "HEAD") -> str:
    return f"repo:{repo_name.strip()}:{ref.strip()}"


def worktree_resource(repo_name: str, run_id: str) -> str:
    return f"worktree:{repo_name.strip()}:{run_id.strip()}"


def acquire_lease(
    db: SimingDB,
    *,
    resource_key: str,
    holder_run_id: str,
    generation: int = 1,
    mode: str = "exclusive",
    ttl_sec: int = 120,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """在单事务中申请或续期资源租约（支持过期自动回收与同持有人可重入续期）。"""
    db.init_db()
    cur = now or now_local()
    cur_iso = iso_now(cur)
    exp_iso = iso_now(cur + timedelta(seconds=max(5, int(ttl_sec))))
    clean_mode = "shared" if str(mode).lower() == "shared" else "exclusive"

    with db.transaction() as conn:
        # 清理已过期租约
        conn.execute("DELETE FROM resource_leases WHERE expires_at <= ?", (cur_iso,))
        row = conn.execute("SELECT * FROM resource_leases WHERE resource_key = ?", (resource_key,)).fetchone()
        if row:
            holder = str(row["holder_run_id"])
            h_gen = int(row["generation"] or 1)
            if holder == holder_run_id and h_gen <= int(generation):
                conn.execute(
                    "UPDATE resource_leases SET mode=?, generation=?, expires_at=? WHERE resource_key=?",
                    (clean_mode, int(generation), exp_iso, resource_key),
                )
                return {
                    "ok": True,
                    "acquired": True,
                    "renewed": True,
                    "resource_key": resource_key,
                    "holder_run_id": holder_run_id,
                    "generation": int(generation),
                    "expires_at": exp_iso,
                }
            if clean_mode == "shared" and str(row["mode"]) == "shared":
                # 共享读锁允许并存，为本次持有人建子键
                shared_key = f"{resource_key}#shared:{holder_run_id}"
                conn.execute(
                    """
                    INSERT INTO resource_leases (resource_key, mode, holder_run_id, generation, acquired_at, expires_at)
                    VALUES (?, 'shared', ?, ?, ?, ?)
                    ON CONFLICT(resource_key) DO UPDATE SET generation=excluded.generation, expires_at=excluded.expires_at
                    """,
                    (shared_key, holder_run_id, int(generation), cur_iso, exp_iso),
                )
                return {
                    "ok": True,
                    "acquired": True,
                    "shared": True,
                    "resource_key": shared_key,
                    "holder_run_id": holder_run_id,
                    "expires_at": exp_iso,
                }
            return {
                "ok": False,
                "acquired": False,
                "resource_key": resource_key,
                "held_by": holder,
                "held_generation": h_gen,
                "held_until": str(row["expires_at"]),
            }

        # 独占锁还需检查是否存在同前缀未过期的 shared 子租约
        if clean_mode == "exclusive":
            shared_conflict = conn.execute(
                "SELECT holder_run_id, expires_at FROM resource_leases WHERE resource_key LIKE ? AND holder_run_id != ? LIMIT 1",
                (f"{resource_key}#shared:%", holder_run_id),
            ).fetchone()
            if shared_conflict:
                return {
                    "ok": False,
                    "acquired": False,
                    "resource_key": resource_key,
                    "held_by": str(shared_conflict["holder_run_id"]),
                    "held_until": str(shared_conflict["expires_at"]),
                    "reason": "shared_readers_active",
                }

        conn.execute(
            """
            INSERT INTO resource_leases (resource_key, mode, holder_run_id, generation, acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (resource_key, clean_mode, holder_run_id, int(generation), cur_iso, exp_iso),
        )
    return {
        "ok": True,
        "acquired": True,
        "resource_key": resource_key,
        "holder_run_id": holder_run_id,
        "generation": int(generation),
        "mode": clean_mode,
        "expires_at": exp_iso,
    }


def acquire_leases(
    db: SimingDB,
    *,
    resource_keys: list[str],
    holder_run_id: str,
    generation: int = 1,
    mode: str = "exclusive",
    ttl_sec: int = 120,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """按固定排序依次申请多个资源租约；任一失败则回滚释放已获租约，防死锁。"""
    ordered = sorted({k.strip() for k in resource_keys if k and k.strip()})
    acquired_keys: list[str] = []
    for k in ordered:
        res = acquire_lease(
            db,
            resource_key=k,
            holder_run_id=holder_run_id,
            generation=generation,
            mode=mode,
            ttl_sec=ttl_sec,
            now=now,
        )
        if not res.get("acquired"):
            for ak in acquired_keys:
                release_lease(db, resource_key=ak, holder_run_id=holder_run_id)
            return {"ok": False, "acquired": False, "failed_key": k, "conflict": res}
        acquired_keys.append(str(res["resource_key"]))
    return {"ok": True, "acquired": True, "resource_keys": acquired_keys}


def release_lease(
    db: SimingDB,
    *,
    resource_key: str,
    holder_run_id: str,
    generation: Optional[int] = None,
) -> int:
    """按持有人（及可选 generation）安全释放租约，绝不误删其他 Run 的锁。"""
    db.init_db()
    with db.transaction() as conn:
        if generation is not None:
            cur = conn.execute(
                "DELETE FROM resource_leases WHERE (resource_key=? OR resource_key=?) AND holder_run_id=? AND generation=?",
                (resource_key, f"{resource_key}#shared:{holder_run_id}", holder_run_id, int(generation)),
            )
        else:
            cur = conn.execute(
                "DELETE FROM resource_leases WHERE (resource_key=? OR resource_key=?) AND holder_run_id=?",
                (resource_key, f"{resource_key}#shared:{holder_run_id}", holder_run_id),
            )
        return int(cur.rowcount or 0)


def list_active_leases(db: SimingDB, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    cur_iso = iso_now(now or now_local())
    conn = db.connect_readonly()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM resource_leases WHERE expires_at > ? ORDER BY resource_key ASC",
                (cur_iso,),
            ).fetchall()
        ]
    finally:
        conn.close()


@contextlib.contextmanager
def hold_resource_lease(
    db: SimingDB,
    *,
    resource_key: str,
    holder_run_id: str,
    generation: int = 1,
    mode: str = "exclusive",
    ttl_sec: int = 120,
) -> Iterator[dict[str, Any]]:
    res = acquire_lease(
        db,
        resource_key=resource_key,
        holder_run_id=holder_run_id,
        generation=generation,
        mode=mode,
        ttl_sec=ttl_sec,
    )
    if not res.get("acquired"):
        raise RuntimeError(f"资源租约冲突：{resource_key} 当前由 {res.get('held_by')} 占用至 {res.get('held_until')}")
    try:
        yield res
    finally:
        release_lease(db, resource_key=str(res["resource_key"]), holder_run_id=holder_run_id, generation=generation)
