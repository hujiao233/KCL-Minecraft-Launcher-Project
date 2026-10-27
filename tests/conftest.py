# -*- coding: utf-8 -*-
"""pytest 共用基础设施：把数据目录重定向到临时目录，避免测试污染仓库。"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import upd_auth  # noqa: E402
import upd_config  # noqa: E402
import upd_db  # noqa: E402
import upd_releases  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """初始化一个干净的临时数据目录，返回其中的关键路径。

    各模块里的路径常量是「按值导入」的，所以每个模块都要单独重定向，
    否则测试会真的写到仓库根目录下的 update_data/。
    """
    data = tmp_path / "update_data"
    files = data / "files"
    files.mkdir(parents=True)
    db = str(data / "server.db")
    token = str(data / "token.txt")

    monkeypatch.setenv("CBUPDATE_DATA", str(data))
    monkeypatch.setattr(upd_config, "DATA_DIR", str(data))
    monkeypatch.setattr(upd_config, "DB_PATH", db)
    monkeypatch.setattr(upd_config, "FILE_DIR", str(files))
    monkeypatch.setattr(upd_config, "TOKEN_FILE", token)
    monkeypatch.setattr(upd_db, "DATA_DIR", str(data))
    monkeypatch.setattr(upd_db, "FILE_DIR", str(files))
    monkeypatch.setattr(upd_db, "DB_PATH", db)
    monkeypatch.setattr(upd_releases, "FILE_DIR", str(files))
    monkeypatch.setattr(upd_auth, "DATA_DIR", str(data))
    monkeypatch.setattr(upd_auth, "TOKEN_FILE", token)

    upd_db.init_db()
    return SimpleNamespace(data=str(data), files=str(files), db=db, token=token)


@pytest.fixture
def admin(env, monkeypatch):
    """设置一个临时管理员口令（不会写口令文件）。"""
    pwd = "test-admin-pwd"
    monkeypatch.setattr(upd_auth, "ADMIN_SALT", "s")
    monkeypatch.setattr(upd_auth, "ADMIN_HASH", upd_auth._pwd_hash(pwd, "s"))
    return pwd
