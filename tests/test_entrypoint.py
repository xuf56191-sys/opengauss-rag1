"""部署入口脚本的单测（全离线）。

覆盖：
- 端口解析：PORT → APP_PORT → 默认 8000
- 向量库目录：可写就用；不可写回退到 /tmp/chroma
- 索引判定：空目录 / 有文件 / 不存在
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("docker_entrypoint", ROOT / "docker-entrypoint.py")
assert SPEC and SPEC.loader
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)


# ---------------------------------------------------------------- 端口

def test_port_from_env() -> None:
    assert entry.resolve_port({"PORT": "9000"}) == "9000"


def test_port_falls_back_to_app_port() -> None:
    assert entry.resolve_port({"APP_PORT": "7861"}) == "7861"


def test_port_default_is_8000() -> None:
    assert entry.resolve_port({}) == "8000"


def test_port_env_wins_over_app_port() -> None:
    assert entry.resolve_port({"PORT": "8000", "APP_PORT": "7861"}) == "8000"


# ---------------------------------------------------------------- 目录

def test_chroma_dir_uses_writable_env(tmp_path: Path) -> None:
    target = tmp_path / "chroma"
    assert entry.resolve_chroma_dir({"CHROMA_DIR": str(target)}) == target
    assert target.is_dir()


def test_chroma_dir_falls_back_when_env_not_writable(tmp_path: Path) -> None:
    """把「文件」当目录传进去 → mkdir 失败 → 必须回退，不能崩。"""
    bogus = tmp_path / "not_a_dir"
    bogus.write_text("x", encoding="utf-8")

    resolved = entry.resolve_chroma_dir({"CHROMA_DIR": str(bogus)})

    assert resolved != bogus
    assert entry._is_writable(resolved)


def test_chroma_dir_creates_missing_path(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "chroma"
    assert entry.resolve_chroma_dir({"CHROMA_DIR": str(target)}) == target
    assert target.is_dir()


# ---------------------------------------------------------------- 索引判定

def test_has_index_false_for_missing_dir(tmp_path: Path) -> None:
    assert entry.has_index(tmp_path / "nope") is False


def test_has_index_false_for_empty_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert entry.has_index(empty) is False


def test_has_index_true_when_files_exist(tmp_path: Path) -> None:
    d = tmp_path / "chroma"
    d.mkdir()
    (d / "chroma.sqlite3").write_text("x", encoding="utf-8")
    assert entry.has_index(d) is True


def test_write_probe_is_cleaned_up(tmp_path: Path) -> None:
    """探测可写性不能在目录里留下文件。"""
    target = tmp_path / "chroma"
    entry._is_writable(target)
    assert list(target.iterdir()) == []
