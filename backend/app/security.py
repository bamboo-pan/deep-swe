import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

@dataclass(frozen=True)
class Credential:
    url: str
    token: str
    fingerprint: str

# job_name / trial 目录名进入文件系统删除路径与 Docker 名称匹配，必须白名单校验，
# 防止恢复备份注入 ".." 或绝对路径造成任意目录删除。
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,158}$")

def is_safe_job_name(name: object) -> bool:
    return isinstance(name, str) and bool(_JOB_NAME_RE.fullmatch(name)) and ".." not in name

def read_credential(path: Path) -> Credential:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("凭据文件必须包含 URL 和 Token 两个非空行")
    parsed = urlparse(lines[0])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("凭据 URL 必须是有效的 HTTP 或 HTTPS 地址")
    token = lines[1]
    return Credential(lines[0].rstrip("/"), token, hashlib.sha256(token.encode()).hexdigest()[:12])

def mask_token(token: str) -> str:
    return "••••••••" + token[-4:] if len(token) >= 4 else "••••••••"

def redact(value: str, secrets: list[str] | None = None) -> str:
    result = value
    for secret in secrets or []:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", result)
    result = re.sub(r'(?i)(api[_-]?key["\'\s:=]+)[^\s,"\']+', r'\1[REDACTED]', result)
    return result

