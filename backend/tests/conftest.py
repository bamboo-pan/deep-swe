import os
import shutil
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="deepswe-ui-tests-"))
os.environ["DEEPSWE_UI_DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}"
os.environ["DEEPSWE_UI_JOBS_DIR"] = str(TEST_ROOT / "jobs")
# 隔离凭据路径：测试绝不能读取开发者真实凭据文件
os.environ["DEEPSWE_UI_CREDENTIAL_FILE"] = str(TEST_ROOT / "credentials" / "credential.txt")

def pytest_sessionfinish(session, exitstatus):
    from app.database import engine
    engine.dispose()  # 释放 SQLite 文件锁，否则 Windows 上 rmtree 清不掉 test.db
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
