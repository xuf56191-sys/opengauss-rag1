"""pytest 全局配置：模块路径 + 环境隔离。"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_api_auth(monkeypatch: pytest.MonkeyPatch):
    """测试不依赖 .env / 进程环境里的 API_ACCESS_KEY。

    背景：share.ps1 会把 API_ACCESS_KEY 写进本地 .env；若测试跟着走鉴权，
    「不带头调 /ask」的用例会突然 401。这里统一默认关闭，鉴权相关用例自己再打开。
    """
    import api7

    monkeypatch.setattr(api7, "API_ACCESS_KEY", "")
