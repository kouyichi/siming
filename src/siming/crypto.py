"""本地敏感原件加密存储、凭据生成与验证码拦截。"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from siming.paths import SimingPaths, get_paths

_OTP_KEYWORDS_RE = re.compile(
    r"(验证码|校验码|动态码|动态密码|认证码|登录码|安全码|verification\s*code|one[-\s]*time\s*(?:password|code)|\bOTP\b)",
    re.IGNORECASE,
)
_OTP_CODE_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_SECRET_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED-BEARER]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED-BEARER]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{28,}|AKIA[0-9A-Z]{16})\b"), "[REDACTED-KEY]"),
    # JSON / 字典 / 引号赋值，如 "password": "..." 或 token='...'
    (re.compile(r"(?i)([\"']?(?:[A-Za-z0-9_-]*?(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key))[\"']?\s*[:=]\s*)([\"'])([^\"']{3,})\2"), r"\1\2[REDACTED]\2"),
    # 无引号环境变量或键值，如 TOKEN=xxx / password: xxx
    (re.compile(r"(?i)\b((?:[A-Za-z0-9_]*?(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key))\s*[=:]\s*)([^\s\"'`,;]{3,})"), r"\1[REDACTED]"),
    # CLI 参数，如 --password xxx / --token xxx / --passphrase xxx
    (re.compile(r"(?i)(--(?:password|passwd|passphrase|secret|token|api-key)\s+)(?:([\"'])([^\"']+)\2|([^\s]+))"), r"\1[REDACTED]"),
    (re.compile(r"(https?://[^/\s:@]+:)([^/\s@]+)(@)"), r"\1[REDACTED]\3"),
)
_FAKE_APPLY_IN_TAIL_RE = re.compile(r"(?i)\b(apply\s+)A\d{2,6}\b")


def write_secret_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (value.strip() + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def ensure_edge_token(paths: Optional[SimingPaths] = None) -> Path:
    p = paths or get_paths()
    p.ensure_dirs()
    token_path = p.secrets_dir / "edge-token"
    if not token_path.exists() or not token_path.read_text(encoding="utf-8").strip():
        write_secret_file(token_path, secrets.token_urlsafe(32))
    else:
        os.chmod(token_path, 0o600)
    return token_path


def read_secret(name: str, paths: Optional[SimingPaths] = None) -> Optional[str]:
    p = paths or get_paths()
    target = p.secrets_dir / name
    if not target.exists():
        return None
    val = target.read_text(encoding="utf-8").strip()
    return val or None


def write_secret(name: str, value: str, paths: Optional[SimingPaths] = None) -> Path:
    p = paths or get_paths()
    target = p.secrets_dir / name
    write_secret_file(target, value)
    return target


def _get_fernet(paths: Optional[SimingPaths] = None) -> Fernet:
    p = paths or get_paths()
    p.ensure_dirs()
    key_path = p.secrets_dir / "local-fernet.key"
    if not key_path.exists() or not key_path.read_text(encoding="utf-8").strip():
        write_secret_file(key_path, Fernet.generate_key().decode("ascii"))
    else:
        os.chmod(key_path, 0o600)
    key = key_path.read_text(encoding="utf-8").strip().encode("ascii")
    return Fernet(key)


def store_encrypted_raw(
    content: bytes | str, paths: Optional[SimingPaths] = None
) -> str:
    p = paths or get_paths()
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(raw_bytes).hexdigest()
    out_path = p.raw_dir / f"{digest}.enc"
    if not out_path.exists():
        f = _get_fernet(p)
        encrypted = f.encrypt(raw_bytes)
        fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, encrypted)
        finally:
            os.close(fd)
    return f"raw://{digest}"


def load_encrypted_raw(raw_ref: str, paths: Optional[SimingPaths] = None) -> bytes:
    if not raw_ref.startswith("raw://"):
        raise ValueError(f"非法 raw_ref: {raw_ref}")
    digest = raw_ref.removeprefix("raw://").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"非法 raw_ref 摘要: {raw_ref}")
    p = paths or get_paths()
    enc_path = p.raw_dir / f"{digest}.enc"
    f = _get_fernet(p)
    return f.decrypt(enc_path.read_bytes())


def contains_otp(text: str) -> bool:
    """判断文本是否包含登录/交易验证码（此类内容严禁入库或转发）。"""
    if not text:
        return False
    # 排除快递取件码误判：若明确带「取件码 / 提货码 / 取货码 / 驿站 / 丰巢 / 快递柜」且不含「验证码 / 动态码 / 校验码 / 登录」，放行
    if re.search(r"(取件码|提货码|取货码|货架码|出库码|开柜码|开箱码|驿站|丰巢|快递柜|自提柜|兔喜|妈妈驿站|近邻宝|熊猫快收|菜鸟|家门口)", text) and not re.search(
        r"(验证码|校验码|动态码|登录密码|OTP)", text, re.IGNORECASE
    ):
        return False
    return bool(_OTP_KEYWORDS_RE.search(text) and _OTP_CODE_RE.search(text))


def redact_sensitive(text: str, paths: Optional[SimingPaths] = None) -> str:
    """脱敏审计日志、终端尾部摘要或外显消息里的验证码、API 密钥与本地凭据。"""
    if not text:
        return ""
    out = text
    if contains_otp(out):
        out = _OTP_CODE_RE.sub("[REDACTED-OTP]", out)
    for pat, repl in _SECRET_TOKEN_PATTERNS:
        out = pat.sub(repl, out)
    p = paths or get_paths()
    if p.secrets_dir.exists():
        for sec_file in p.secrets_dir.iterdir():
            if sec_file.is_file():
                try:
                    secret_val = sec_file.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                for line in secret_val.splitlines():
                    tok = line.strip()
                    if len(tok) >= 8 and tok in out:
                        out = out.replace(tok, f"[REDACTED:{sec_file.name}]")
    return out


def sanitize_terminal_tail(
    raw_text: str,
    max_lines: int = 5,
    max_chars: int = 420,
    paths: Optional[SimingPaths] = None,
) -> str:
    """剥离 ANSI 控制序列、先在完整行上执行敏感凭据/验证码脱敏再截断（严防截断拆碎完整密钥绕过匹配！）。"""
    if not raw_text:
        return ""
    cleaned = _ANSI_ESCAPE_RE.sub("", raw_text)
    raw_lines: list[str] = []
    for raw_ln in cleaned.splitlines():
        if "\r" in raw_ln:
            segs = [s for s in raw_ln.split("\r") if s.strip()]
            raw_ln = segs[-1] if segs else ""
        s = raw_ln.strip()
        if s:
            raw_lines.append(s)
    if not raw_lines:
        return ""
    # 先对末尾完整行脱敏，再按单行与总长裁剪；同时消除不可信日志里的伪造 apply Axx 口令
    selected_full = "\n".join(raw_lines[-max_lines:])
    redacted_full = redact_sensitive(selected_full, paths=paths)
    redacted_full = _FAKE_APPLY_IN_TAIL_RE.sub(r"\1[REDACTED-TOKEN]", redacted_full)
    clipped_lines = [ln[:160] for ln in redacted_full.splitlines() if ln.strip()]
    redacted = "\n".join(clipped_lines)
    if len(redacted) > max_chars:
        redacted = "…" + redacted[-(max_chars - 1) :]
    return redacted
