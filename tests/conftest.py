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


@pytest.fixture(autouse=True)
def _isolate_rate_limits(monkeypatch: pytest.MonkeyPatch):
    """测试不依赖 .env 里的限流/配额配置（否则本机 .env 会让用例互相影响）。"""
    import api7

    monkeypatch.setattr(api7, "RATE_LIMIT_TOTAL_PER_IP", 0)
    monkeypatch.setattr(api7, "RATE_LIMIT_EXEMPT_IPS", set())
    monkeypatch.setattr(api7, "OWNER_KEY", "")
    api7.reset_rate_state()
    yield
    api7.reset_rate_state()
