# -*- coding: utf-8 -*-
"""管理员口令与会话、用户身份。"""
from __future__ import annotations

import hashlib
import time

import upd_auth
import upd_db


def test_admin_pwd_rejects_empty(admin):
    assert upd_auth.check_admin_pwd("") is False


def test_admin_pwd_matches_salted_hash(admin):
    assert upd_auth.check_admin_pwd(admin) is True
    assert upd_auth.check_admin_pwd(admin + "x") is False


def test_admin_pwd_supports_unsalted_branch(env, monkeypatch):
    """CBUPDATE_TOKEN 场景下 salt 为空，此时哈希公式是 sha256(pwd) 本身，
    不能再用带分隔符的 _pwd_hash（它会拼出 ":"），两者结果不同。
    """
    pwd = "plain-token-pwd"
    unsalted = hashlib.sha256(pwd.encode("utf-8")).hexdigest()
    monkeypatch.setattr(upd_auth, "ADMIN_SALT", "")
    monkeypatch.setattr(upd_auth, "ADMIN_HASH", unsalted)
    assert upd_auth.check_admin_pwd(pwd) is True
    assert upd_auth.check_admin_pwd("other") is False


def test_session_lifecycle(env):
    sid = upd_auth.new_session(role="admin", uid="", name="admin")
    assert isinstance(sid, str) and sid

    row = upd_auth.session_get(sid)
    assert row is not None
    assert row["role"] == "admin"

    upd_auth.drop_session(sid)
    assert upd_auth.session_get(sid) is None


def test_drop_unknown_session_is_safe(env):
    upd_auth.drop_session("no-such-session")


def test_expired_session_is_purged(env):
    sid = upd_auth.new_session()
    conn = upd_db.connect()
    conn.execute("UPDATE sessions SET exp=? WHERE sid=?", (time.time() - 10, sid))
    conn.commit()
    conn.close()

    assert upd_auth.session_get(sid) is None

    conn = upd_db.connect()
    left = conn.execute(
        "SELECT COUNT(*) c FROM sessions WHERE sid=?", (sid,)).fetchone()["c"]
    conn.close()
    assert left == 0


def test_missing_session_returns_none(env):
    assert upd_auth.session_get("") is None
    assert upd_auth.session_get("missing") is None


def test_user_login_stable_uid(env):
    """昵称即凭证：同一个人反复登录必须落回同一个 uid。"""
    first = upd_auth.user_login("小明")
    assert first["ok"] is True

    again = upd_auth.user_login("小明")
    assert again["uid"] == first["uid"]
    assert again["secret"] == first["secret"]
    assert again["name"] == "小明"


def test_user_login_rejects_blank_name(env):
    result = upd_auth.user_login("   ")
    assert result["ok"] is False
    assert "名字" in result["msg"]


def test_user_by_uid(env):
    info = upd_auth.user_login("阿华")
    row = upd_auth.user_by_uid(info["uid"])
    assert row is not None
    assert row["name"] == "阿华"
    assert upd_auth.user_by_uid("") is None
    assert upd_auth.user_by_uid("nobody") is None


def test_user_auth_checks_secret(env):
    info = upd_auth.user_login("阿华")
    good = {"X-Uid": info["uid"], "X-Secret": info["secret"]}
    assert upd_auth.user_auth(good) is not None

    bad = {"X-Uid": info["uid"], "X-Secret": "wrong"}
    assert upd_auth.user_auth(bad) is None
    assert upd_auth.user_auth({"X-Uid": "", "X-Secret": ""}) is None
