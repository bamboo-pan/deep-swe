import json, shutil
from pathlib import Path
import pytest
from app.runner import _anthropic_url, _codex_config, _docker_url, _write_secret_auth
from app.security import mask_token, read_credential, redact

def test_docker_url_mapping():
    assert _docker_url("http://127.0.0.1:9887/v1") == "http://host.docker.internal:9887/v1"
    assert _docker_url("http://localhost:9887/v1") == "http://host.docker.internal:9887/v1"
    assert _docker_url("http://192.168.0.108:9887/v1") == "http://192.168.0.108:9887/v1"
    assert _anthropic_url("http://127.0.0.1:9887/v1") == "http://host.docker.internal:9887"
    assert _anthropic_url("http://192.168.0.108:9887/v1/") == "http://192.168.0.108:9887"

def test_invalid_credentials_are_rejected(tmp_path: Path):
    path=tmp_path/"bad.txt"; path.write_text("ftp://bad\ntoken\n",encoding="utf-8")
    with pytest.raises(ValueError): read_credential(path)

def test_secret_auth_is_valid_utf8_without_bom_and_config_has_no_token(tmp_path: Path):
    folder,auth=_write_secret_auth("paid-secret-token")
    try:
        raw=auth.read_bytes(); assert not raw.startswith(b"\xef\xbb\xbf")
        assert json.loads(raw)["OPENAI_API_KEY"] == "paid-secret-token"
        config=_codex_config("http://127.0.0.1:9887/v1",tmp_path,"gpt-5.6-sol","high").read_text(encoding="utf-8")
        assert "host.docker.internal" in config and "paid-secret-token" not in config
        assert 'model_reasoning_effort = "high"' in config
    finally: shutil.rmtree(folder,ignore_errors=True)

def test_redaction_and_masking():
    secret="super-secret-1234"; text=redact(f"Bearer {secret} api_key={secret}",[secret])
    assert secret not in text and "[REDACTED]" in text
    assert mask_token(secret).endswith("1234") and secret not in mask_token(secret)
