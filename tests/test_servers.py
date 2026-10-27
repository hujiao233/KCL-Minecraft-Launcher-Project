# -*- coding: utf-8 -*-
"""联机服务器：发布、过滤搜索、加入审核、成员可见性。"""
from __future__ import annotations

import upd_auth
import upd_db
from upd_servers import (
    decide_join,
    launcher_joinlist,
    request_join,
    save_server,
    search_servers,
    server_announces,
)

OWNER = {"uid": "owner1", "name": "服主"}
OTHER = {"uid": "u2", "name": "路人"}


def _join_id(sid, uid):
    conn = upd_db.connect()
    row = conn.execute(
        "SELECT id FROM join_reqs WHERE server_id=? AND uid=? ORDER BY id DESC LIMIT 1",
        (sid, uid)).fetchone()
    conn.close()
    return row["id"] if row else None


def test_save_server_requires_name(env):
    assert save_server({"name": "   "}, OWNER)["ok"] is False


def test_negative_max_players_becomes_zero(env):
    sid = save_server({"name": "我的服", "maxPlayers": -5}, OWNER)["id"]
    row = [r for r in search_servers({}, uid="owner1") if r["id"] == sid][0]
    assert row["maxPlayers"] == 0


def test_only_owner_can_edit(env):
    sid = save_server({"name": "A服"}, OWNER)["id"]
    hijack = save_server({"name": "被改名了"}, OTHER, sid=sid)
    assert hijack["ok"] is False
    assert "自己" in hijack["msg"]


def test_edit_unknown_server_is_rejected(env):
    assert save_server({"name": "x"}, OWNER, sid=99999)["ok"] is False


def test_join_needs_approval_when_required(env):
    sid = save_server({"name": "B服", "needApprove": True}, OWNER)["id"]

    first = request_join(sid, OTHER)
    assert first["ok"] is True and first["status"] == "pending"

    # 重复申请应幂等，不会重复插入记录
    assert request_join(sid, OTHER)["status"] == "pending"
    conn = upd_db.connect()
    count = conn.execute(
        "SELECT COUNT(*) c FROM join_reqs WHERE server_id=? AND uid=?",
        (sid, "u2")).fetchone()["c"]
    conn.close()
    assert count == 1

    assert decide_join(_join_id(sid, "u2"), "owner1", True)["ok"] is True
    row = [r for r in search_servers({}, uid="u2") if r["id"] == sid][0]
    assert row["joinStatus"] == "approved"


def test_auto_approve_when_not_required(env):
    sid = save_server({"name": "D服", "needApprove": False}, OWNER)["id"]
    assert request_join(sid, OTHER)["status"] == "approved"


def test_owner_cannot_join_own_server(env):
    sid = save_server({"name": "E服"}, OWNER)["id"]
    assert request_join(sid, OWNER)["ok"] is False


def test_decide_join_permission(env):
    sid = save_server({"name": "F服", "needApprove": True}, OWNER)["id"]
    request_join(sid, OTHER)
    jid = _join_id(sid, "u2")

    assert decide_join(jid, "someone-else", True)["ok"] is False
    assert decide_join(99999, "owner1", True)["ok"] is False

    assert decide_join(jid, "owner1", False)["ok"] is True
    conn = upd_db.connect()
    status = conn.execute(
        "SELECT status FROM join_reqs WHERE id=?", (jid,)).fetchone()["status"]
    conn.close()
    assert status == "rejected"


def test_announces_returns_none_for_unknown_server(env):
    assert server_announces(99999, "owner1") is None


def test_announces_visible_after_approval(env):
    sid = save_server({"name": "G服", "needApprove": True}, OWNER)["id"]
    # 未加入时看不到公告
    assert server_announces(sid, "u2") == []

    request_join(sid, OTHER)
    decide_join(_join_id(sid, "u2"), "owner1", True)
    # 通过后仍是没有公告的空列表
    assert server_announces(sid, "u2") == []


def test_search_filters_by_keyword_and_mod(env):
    pure = save_server({"name": "纯净生存", "mods": ["jei"]}, OWNER)["id"]
    tech = save_server({"name": "科技服", "mods": ["ae2"]}, OWNER)["id"]

    assert [r["id"] for r in search_servers({"q": ["生存"]})] == [pure]
    assert [r["id"] for r in search_servers({"mod": ["ae2"]})] == [tech]
    assert [r["id"] for r in search_servers(
        {"mod": ["ae2"], "modmode": ["blacklist"]})] == [pure]


def test_contact_hidden_until_joined(env):
    sid = save_server({"name": "H服", "needApprove": True, "address": "mc.example.com"},
                      OWNER)["id"]
    stranger = [r for r in search_servers({}, uid="u2") if r["id"] == sid][0]
    assert "address" not in stranger

    request_join(sid, OTHER)
    decide_join(_join_id(sid, "u2"), "owner1", True)
    member = [r for r in search_servers({}, uid="u2") if r["id"] == sid][0]
    assert member["address"] == "mc.example.com"


def test_launcher_joinlist_needs_valid_secret(env):
    user = upd_auth.user_login("新来的")
    sid = save_server({"name": "I服", "needApprove": False}, OWNER)["id"]
    member = {"uid": user["uid"], "name": user["name"]}
    assert request_join(sid, member)["status"] == "approved"

    bad = launcher_joinlist(user["uid"], "wrong-secret")
    assert bad["ok"] is False

    good = launcher_joinlist(user["uid"], user["secret"])
    assert good["ok"] is True
    assert [s["name"] for s in good["list"]] == ["I服"]
