"""SQLite WAL 数据库访问、同事务 effects 队列、审计写入与在线备份恢复演练。"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from siming.config import bootstrap_config, load_policy, load_sources_config
from siming.crypto import read_secret, redact_sensitive
from siming.paths import SimingPaths, get_paths

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def now_local() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def logical_today(dt: Optional[datetime] = None, cutoff_hour: int = 5) -> date:
    """夜猫子逻辑日（默认 05:00 切日）：00:00~04:59 归属前一自然日，避免跨零点把昨晚当成明天。"""
    cur = (dt.astimezone(SHANGHAI_TZ) if dt else now_local())
    if cur.hour < max(0, min(12, int(cutoff_hour))):
        return (cur - timedelta(days=1)).date()
    return cur.date()


def logical_day_str(dt: Optional[datetime] = None, cutoff_hour: int = 5) -> str:
    return logical_today(dt=dt, cutoff_hour=cutoff_hour).isoformat()


def iso_now(dt: Optional[datetime] = None) -> str:
    target = dt.astimezone(SHANGHAI_TZ) if dt else now_local()
    return target.isoformat(timespec="seconds")


def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


def _schema_sql_path(paths: SimingPaths) -> Path:
    candidate = paths.repo_root / "sql" / "schema.sql"
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


def current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pid_is_siming_process(pid: Optional[int], expected_boot_id: str = "") -> bool:
    """核验 PID 仍存活、未跨机器重启，且确实属于本机 siming/python 进程（严防 PID 复用误杀无关任务）。"""
    if not pid or int(pid) <= 1:
        return False
    if expected_boot_id:
        cur_boot = current_boot_id()
        if cur_boot and cur_boot != expected_boot_id:
            return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        cmdline_raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        cmd_s = cmdline_raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").lower()
        if cmd_s and not any(k in cmd_s for k in ("siming", "python", "pytest", "bash", "sh")):
            return False
    except OSError:
        pass
    return True


class SimingDB:
    def __init__(self, paths: Optional[SimingPaths] = None) -> None:
        self.paths = bootstrap_config(paths or get_paths())
        self.db_path = self.paths.db_path

    def connect(self) -> sqlite3.Connection:
        self.paths.ensure_dirs()
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 15000;")
        return conn

    @contextlib.contextmanager
    def transaction(self, conn: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
        owns_conn = conn is None
        c = conn or self.connect()
        try:
            c.execute("BEGIN IMMEDIATE;")
            yield c
            c.execute("COMMIT;")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ROLLBACK;")
            raise
        finally:
            if owns_conn:
                c.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, col: str, col_ddl: str) -> None:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col in cols:
            return
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    def init_db(self) -> None:
        schema = _schema_sql_path(self.paths).read_text(encoding="utf-8")
        self.paths.ensure_dirs()
        lock_path = self.paths.data_dir / ".db_init.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                conn = self.connect()
                try:
                    conn.executescript(schema)
                    # 幂等迁移已有库新增列（文件锁 + duplicate column 双重防并发竞态）
                    self._ensure_column(conn, "effects", "unit_name", "TEXT NOT NULL DEFAULT ''")
                    self._ensure_column(conn, "effects", "attempt_gen", "INTEGER NOT NULL DEFAULT 0")

                    self._ensure_column(conn, "approvals", "bundle_id", "TEXT NOT NULL DEFAULT ''")
                    self._ensure_column(conn, "approvals", "claimed_at", "TEXT")
                    self._ensure_column(conn, "approvals", "claimer_pid", "INTEGER")
                    self._ensure_column(conn, "approvals", "claimer_boot_id", "TEXT NOT NULL DEFAULT ''")

                    self._ensure_column(conn, "task_runs", "wrapper_pid", "INTEGER")
                    self._ensure_column(conn, "task_runs", "boot_id", "TEXT NOT NULL DEFAULT ''")
                    self._ensure_column(conn, "task_runs", "blocked_notified_seq", "INTEGER NOT NULL DEFAULT -1")

                    self._ensure_column(conn, "subscription_audit", "is_ongoing", "INTEGER NOT NULL DEFAULT 1")
                    self._ensure_column(conn, "subscription_audit", "ongoing_source", "TEXT NOT NULL DEFAULT 'AUTO'")

                    if self.db_path.exists():
                        os.chmod(self.db_path, 0o600)
                    self._seed_defaults(conn)
                finally:
                    conn.close()
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _seed_defaults(self, conn: sqlite3.Connection) -> None:
        ts = iso_now()
        sources_cfg = load_sources_config(self.paths)
        with self.transaction(conn):
            for src_id, cfg in sources_cfg.items():
                kind = str(cfg.get("kind", "local"))
                max_silence = int(cfg.get("max_silence_sec", 3600))
                secret_name = cfg.get("secret_name")
                initial_auth = "OK"
                initial_fresh = "UNKNOWN"
                if secret_name and not read_secret(str(secret_name), self.paths):
                    initial_auth = "UNCONFIGURED"
                    initial_fresh = "UNCONFIGURED"
                elif src_id in ("ios_hb", "douyin_orders", "ios_tesla"):
                    initial_auth = "UNCONFIGURED"
                    initial_fresh = "UNCONFIGURED"
                conn.execute(
                    """
                    INSERT INTO sources (id, kind, max_silence_sec, auth_state, freshness, detail)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        kind = excluded.kind,
                        max_silence_sec = excluded.max_silence_sec
                    """,
                    (
                        src_id,
                        kind,
                        max_silence,
                        initial_auth,
                        initial_fresh,
                        str(cfg.get("description", "")),
                    ),
                )
                # 若凭据后来已补齐，自动从 UNCONFIGURED 解锁为 OK
                if secret_name and read_secret(str(secret_name), self.paths):
                    conn.execute(
                        "UPDATE sources SET auth_state='OK', freshness=CASE WHEN freshness='UNCONFIGURED' THEN 'UNKNOWN' ELSE freshness END WHERE id=? AND auth_state='UNCONFIGURED'",
                        (src_id,),
                    )

            # 预置 ROADMAP 约定的 Gmail Omamail -> intent:报销 挂起项（仅首次种子化）
            seeded = conn.execute(
                "SELECT value FROM meta WHERE key = 'seeded_omamail_wait'"
            ).fetchone()
            if not seeded:
                policy = load_policy(self.paths)
                exp = iso_now(now_local() + timedelta(days=30))
                mandate = {
                    "policy_version": int(policy.get("policy_version", 1)),
                    "strength": "explicit",
                    "action_mode": "remind",
                    "cwd": str(self.paths.repo_root),
                    "task": "把 Google Cloud Console 里的 Omamail OAuth 应用从 Testing 切为 Production 并刷新本机 Gmail 授权，随后归集当期发票 PDF。",
                }
                wait_id = self.next_seq_id(conn, "W")
                conn.execute(
                    """
                    INSERT INTO waits (
                        wait_id, status, strength, trigger_kind, trigger_expr,
                        probe_interval_sec, expires_at, action_mode, session_ref,
                        mandate_json, quote, created_at, updated_at
                    ) VALUES (?, 'WAITING', 'explicit', 'intent', ?, 1800, ?, 'remind', 'seed:roadmap', ?, ?, ?, ?)
                    """,
                    (
                        wait_id,
                        "报销|整理发票|Omamail|reimburse",
                        exp,
                        json.dumps(mandate, ensure_ascii=False),
                        "Gmail OAuth 先不管，等需要报销的时候再把 Omamail 切 Production",
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES ('seeded_omamail_wait', ?, ?)",
                    (wait_id, ts),
                )

    def next_seq_id(self, conn: sqlite3.Connection, prefix: str) -> str:
        key = f"seq_{prefix}"
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        nxt = (int(row["value"]) + 1) if row else 1
        ts = iso_now()
        conn.execute(
            """
            INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(nxt), ts),
        )
        return f"{prefix}{nxt:02d}"

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default
        finally:
            conn.close()

    def set_meta(self, key: str, value: str, conn: Optional[sqlite3.Connection] = None) -> None:
        ts = iso_now()
        if conn is not None:
            conn.execute(
                """
                INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, ts),
            )
            return
        with self.transaction() as c:
            self.set_meta(key, value, conn=c)

    def enqueue_effect(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: dict[str, Any],
        run_at: Optional[str] = None,
    ) -> str:
        ts = iso_now()
        effect_id = f"EF-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        conn.execute(
            """
            INSERT INTO effects (effect_id, kind, payload_json, status, retry_count, next_run_at, created_at)
            VALUES (?, ?, ?, 'PENDING', 0, ?, ?)
            """,
            (effect_id, kind, json.dumps(payload, ensure_ascii=False), run_at or ts, ts),
        )
        return effect_id

    def append_event(
        self,
        conn: sqlite3.Connection,
        source: str,
        thread_key: str,
        summary: str,
        payload: Optional[dict[str, Any]] = None,
        raw_ref: Optional[str] = None,
        occurred_at: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> Optional[str]:
        """只追加写入 events；若 fingerprint 已存在则幂等跳过返回 None。"""
        ts = iso_now()
        occ = occurred_at or ts
        if fingerprint:
            existing = conn.execute(
                "SELECT event_id FROM events WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing:
                return None
        event_id = f"EV-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        conn.execute(
            """
            INSERT INTO events (
                event_id, source, occurred_at, ingested_at, thread_key,
                summary, payload_json, raw_ref, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                source,
                occ,
                ts,
                thread_key,
                redact_sensitive(summary, self.paths),
                json.dumps(payload or {}, ensure_ascii=False),
                raw_ref,
                fingerprint,
            ),
        )
        conn.execute(
            """
            UPDATE sources
            SET last_ok_at = ?, last_run_at = ?, freshness = 'OK', auth_state = 'OK'
            WHERE id = ?
            """,
            (ts, ts, source),
        )
        return event_id

    def mark_source_ok(
        self,
        source_id: str,
        cursor: Optional[str] = None,
        detail: str = "",
        used: bool = False,
        ok_at: Optional[str] = None,
    ) -> None:
        ts = iso_now()
        effective_ok_at = ok_at or ts
        with self.transaction() as conn:
            row = conn.execute("SELECT cursor, last_used_at FROM sources WHERE id = ?", (source_id,)).fetchone()
            new_cursor = cursor if cursor is not None else (row["cursor"] if row else "")
            last_used = ts if used else (row["last_used_at"] if row else None)
            conn.execute(
                """
                INSERT INTO sources (id, cursor, last_ok_at, last_run_at, last_used_at, auth_state, freshness, detail)
                VALUES (?, ?, ?, ?, ?, 'OK', 'OK', ?)
                ON CONFLICT(id) DO UPDATE SET
                    cursor = excluded.cursor,
                    last_ok_at = excluded.last_ok_at,
                    last_run_at = excluded.last_run_at,
                    last_used_at = COALESCE(excluded.last_used_at, sources.last_used_at),
                    auth_state = 'OK',
                    freshness = 'OK',
                    detail = excluded.detail
                """,
                (source_id, new_cursor, effective_ok_at, ts, last_used, redact_sensitive(detail, self.paths)),
            )

    def record_human_use(
        self,
        feature_id: str,
        action: str,
        ref_id: str = "",
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """记录本人真实交互行为（用于 30 天无真实使用自动冻结统计，严禁由后台采集器调用）。"""
        ts = iso_now()

        def _write(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO feature_usage (feature_id, actor, action, ref_id, ts) VALUES (?, 'human', ?, ?, ?)",
                (feature_id, action, ref_id, ts),
            )
            c.execute("UPDATE sources SET last_used_at = ? WHERE id = ?", (ts, feature_id))

        if conn is not None:
            _write(conn)
        else:
            with self.transaction() as c:
                _write(c)

    def record_usage(
        self,
        model: str,
        runner: str = "coding_plan",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_cny: float = 0.0,
        ref_id: str = "",
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """统一记录司命后台 LLM / 机密通道调用量与成本，驱动周日健康自省报告。"""
        ts = iso_now()

        def _insert(c: sqlite3.Connection) -> None:
            c.execute(
                """
                INSERT INTO usage (ts, model, runner, tokens_in, tokens_out, cost_cny, ref_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, str(model or "unknown")[:120], str(runner or "coding_plan")[:64], max(0, int(tokens_in)), max(0, int(tokens_out)), float(cost_cny or 0.0), str(ref_id or "")[:80]),
            )

        if conn is not None:
            _insert(conn)
        else:
            with self.transaction() as c:
                _insert(c)

    def mark_source_error(self, source_id: str, auth_state: str, detail: str) -> None:
        ts = iso_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sources (id, last_run_at, auth_state, freshness, detail)
                VALUES (?, ?, ?, 'ERROR', ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    auth_state = excluded.auth_state,
                    freshness = CASE WHEN excluded.auth_state = 'UNCONFIGURED' THEN 'UNCONFIGURED' ELSE 'ERROR' END,
                    detail = excluded.detail
                """,
                (source_id, ts, auth_state, redact_sensitive(detail, self.paths)),
            )

    def write_audit(
        self,
        tier: str,
        action: str,
        params: Any,
        target_id: str = "",
        approval_ref: str = "",
        exit_code: int = 0,
        detail: str = "",
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        ts = iso_now()
        serialized = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        clean_detail = redact_sensitive(detail, self.paths)

        def _insert(c: sqlite3.Connection) -> None:
            c.execute(
                """
                INSERT INTO audit (ts, tier, action, target_id, params_digest, approval_ref, exit_code, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, tier, action, target_id, digest, approval_ref, exit_code, clean_detail),
            )

        if conn is not None:
            _insert(conn)
        else:
            with self.transaction() as c:
                _insert(c)

        with contextlib.suppress(OSError):
            month_file = self.paths.data_dir / f"audit-{now_local().strftime('%Y-%m')}.jsonl"
            record = {
                "ts": ts,
                "tier": tier,
                "action": action,
                "target_id": target_id,
                "params_digest": digest,
                "approval_ref": approval_ref,
                "exit_code": exit_code,
                "detail": clean_detail,
            }
            with month_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.chmod(month_file, 0o600)
        return digest

    def online_backup_and_verify(self, keep_days: int = 14) -> dict[str, Any]:
        """SQLite 在线 .backup 并在 $TMPDIR/siming/ 做恢复完整性演练。"""
        self.init_db()
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        backup_file = self.paths.backup_dir / f"siming-{stamp}.db"

        src = self.connect()
        dst = sqlite3.connect(str(backup_file))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(backup_file, 0o600)

        # 恢复演练：拷贝到 $TMPDIR/siming/ 独立打开跑 integrity_check 与核心表计数
        drill_path = self.paths.tmp_root / f"restore-drill-{stamp}.db"
        try:
            drill_path.write_bytes(backup_file.read_bytes())
            chk_conn = sqlite3.connect(str(drill_path))
            try:
                integrity = chk_conn.execute("PRAGMA integrity_check;").fetchone()[0]
                waits_cnt = chk_conn.execute("SELECT COUNT(*) FROM waits").fetchone()[0]
                cases_cnt = chk_conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            finally:
                chk_conn.close()
        finally:
            drill_path.unlink(missing_ok=True)

        if integrity != "ok":
            raise RuntimeError(f"备份恢复演练失败: integrity_check={integrity}")

        # 清理超期备份
        cutoff = now_local().timestamp() - keep_days * 86400
        pruned = 0
        for old in sorted(self.paths.backup_dir.glob("siming-*.db")):
            if old != backup_file and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
                pruned += 1

        self.write_audit(
            tier="A1",
            action="sqlite_backup_and_drill",
            params={"backup": str(backup_file), "waits": waits_cnt, "cases": cases_cnt},
            detail=f"integrity={integrity}, pruned={pruned}",
        )
        return {
            "ok": True,
            "backup_path": str(backup_file),
            "integrity": integrity,
            "waits_count": waits_cnt,
            "cases_count": cases_cnt,
            "pruned": pruned,
        }
