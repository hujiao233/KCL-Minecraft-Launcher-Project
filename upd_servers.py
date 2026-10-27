# -*- coding: utf-8 -*-
"""脆冰更新服务端 - 联机服务器、加入审核与联机大厅。"""

from __future__ import annotations

import hmac

from upd_config import _db_lock, now_str, strval
from upd_db import connect
from upd_auth import user_by_uid



# ==================================================================
# 社区网站：MC 服务器宣传
# ==================================================================


def server_summary(conn, r, uid: str = "", admin: bool = False) -> dict:
    """组装服务器卡片。地址 / QQ 群 / kukechat 群 只有「已同意加入」的人能看到。"""
    sid = r["id"]
    status = ""
    if uid:
        jr = conn.execute(
            "SELECT status FROM join_reqs WHERE server_id = ? AND uid = ? "
            "ORDER BY id DESC LIMIT 1", (sid, uid)).fetchone()
        status = jr["status"] if jr else ""
    joined = status == "approved" or (uid and uid == r["owner_uid"])
    d = {
        "id": sid, "name": r["name"] or "", "type": r["stype"] or "",
        "version": r["mcver"] or "", "image": r["image"] or "",
        "slogan": r["slogan"] or "", "maxPlayers": r["max_players"] or 0,
        "mods": [m for m in (r["mods"] or "").splitlines() if m.strip()],
        "modpack": r["modpack"] or "",
        "needApprove": bool(r["need_approve"]), "markon": bool(r["markon"]),
        "owner": r["owner_name"] or "", "ownerUid": r["owner_uid"] or "",
        "createdAt": r["created_at"] or "", "updatedAt": r["updated_at"] or "",
        "joinStatus": status,
        "announces": int(conn.execute(
            "SELECT COUNT(*) AS c FROM announces WHERE server_id = ?", (sid,)).fetchone()["c"]),
    }
    if joined or admin:
        d["address"] = r["address"] or ""
        d["qqGroup"] = r["qq_group"] or ""
        d["kukeGroup"] = r["kuke_group"] or ""
    return d




def search_servers(qs, uid: str = "", admin: bool = False) -> list:
    """过滤：type / ver（指定版本）/ kclver（KCL 已下载版本，逗号分隔）/ mod +
    modmode（whitelist|blacklist）/ q（关键词）/ markon。按最后修改时间倒序。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM servers WHERE deleted = 0 ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        ftype = strval((qs.get("type") or [""])[0], 60)
        ver = strval((qs.get("ver") or [""])[0], 40)
        kclver = strval((qs.get("kclver") or [""])[0], 400)
        mod = strval((qs.get("mod") or [""])[0], 80).lower()
        modmode = (qs.get("modmode") or [""])[0]
        q = strval((qs.get("q") or [""])[0], 80).lower()
        markon = (qs.get("markon") or [""])[0]
        kcl_vers = {v.strip().lower() for v in kclver.split(",") if v.strip()}
        out = []
        for r in rows:
            if ftype and (r["stype"] or "") != ftype:
                continue
            if ver and (r["mcver"] or "") != ver:
                continue
            if kcl_vers and (r["mcver"] or "").lower() not in kcl_vers:
                continue
            if markon in ("1", "true", "yes") and not r["markon"]:
                continue
            mods = [m.strip() for m in (r["mods"] or "").splitlines() if m.strip()]
            if mod:
                hit = any(mod in m.lower() for m in mods)
                if modmode == "blacklist" and hit:
                    continue
                if modmode != "blacklist" and not hit:
                    continue
            if q and q not in (r["name"] or "").lower() and q not in (r["slogan"] or "").lower():
                continue
            out.append(server_summary(conn, r, uid, admin))
        return out
    finally:
        conn.close()




def save_server(body: dict, user, sid: int = 0) -> dict:
    def g(k, lim):
        return strval(body.get(k), lim)
    name = g("name", 80)
    if not name:
        return {"ok": False, "msg": "服务器名称不能为空"}
    mods = "\n".join(strval(m, 120) for m in (body.get("mods") or []) if strval(m, 120))
    vals = (
        name, g("stype", 40), g("version", 40), g("image", 1000), g("slogan", 500),
        max(0, int(body.get("maxPlayers") or 0)), mods, g("modpack", 200),
        1 if body.get("needApprove", True) else 0,
        g("address", 200), g("qqGroup", 100), g("kukeGroup", 100),
        1 if body.get("markon") else 0,
    )
    with _db_lock:
        conn = connect()
        try:
            if sid:
                row = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
                if not row or row["deleted"]:
                    return {"ok": False, "msg": "服务器不存在"}
                if row["owner_uid"] != user["uid"]:
                    return {"ok": False, "msg": "只能修改自己发布的服务器"}
                conn.execute(
                    "UPDATE servers SET name=?, stype=?, mcver=?, image=?, slogan=?, "
                    "max_players=?, mods=?, modpack=?, need_approve=?, address=?, "
                    "qq_group=?, kuke_group=?, markon=?, updated_at=? WHERE id=?",
                    vals + (now_str(), sid))
            else:
                cur = conn.execute(
                    "INSERT INTO servers(owner_uid, owner_name, name, stype, mcver, image, "
                    "slogan, max_players, mods, modpack, need_approve, address, "
                    "qq_group, kuke_group, markon, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (user["uid"], user["name"]) + vals + (now_str(), now_str()))
                new_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "id": sid or new_id}




def request_join(sid: int, user) -> dict:
    with _db_lock:
        conn = connect()
        try:
            r = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
            if not r or r["deleted"]:
                return {"ok": False, "msg": "服务器不存在"}
            if r["owner_uid"] == user["uid"]:
                return {"ok": False, "msg": "这是你自己发布的服务器"}
            old = conn.execute(
                "SELECT * FROM join_reqs WHERE server_id = ? AND uid = ? "
                "ORDER BY id DESC LIMIT 1", (sid, user["uid"])).fetchone()
            if old and old["status"] == "approved":
                return {"ok": True, "status": "approved"}
            if old and old["status"] == "pending":
                return {"ok": True, "status": "pending"}
            status = "approved" if not r["need_approve"] else "pending"
            conn.execute(
                "INSERT INTO join_reqs(server_id, uid, name, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (sid, user["uid"], user["name"], status, now_str(), now_str()))
            conn.commit()
            return {"ok": True, "status": status}
        finally:
            conn.close()




def decide_join(jid: int, owner_uid: str, approve: bool) -> dict:
    with _db_lock:
        conn = connect()
        try:
            jr = conn.execute("SELECT * FROM join_reqs WHERE id = ?", (jid,)).fetchone()
            if not jr:
                return {"ok": False, "msg": "申请不存在"}
            r = conn.execute("SELECT * FROM servers WHERE id = ?", (jr["server_id"],)).fetchone()
            if not r or r["owner_uid"] != owner_uid:
                return {"ok": False, "msg": "只有服主能处理加入申请"}
            conn.execute("UPDATE join_reqs SET status = ?, updated_at = ? WHERE id = ?",
                         ("approved" if approve else "rejected", now_str(), jid))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()




def server_announces(sid: int, uid: str, admin: bool = False) -> list:
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
        if not r or r["deleted"]:
            return None
        joined = admin or (uid and uid == r["owner_uid"])
        if not joined and uid:
            jr = conn.execute(
                "SELECT status FROM join_reqs WHERE server_id = ? AND uid = ? "
                "ORDER BY id DESC LIMIT 1", (sid, uid)).fetchone()
            joined = jr and jr["status"] == "approved"
        if not joined:
            return []
        rows = conn.execute(
            "SELECT * FROM announces WHERE server_id = ? ORDER BY id DESC LIMIT 100",
            (sid,)).fetchall()
        return [{"id": x["id"], "author": x["author_name"] or "", "text": x["text"] or "",
                 "createdAt": x["created_at"] or ""} for x in rows]
    finally:
        conn.close()




def launcher_joinlist(uid: str, secret: str) -> dict:
    """启动器专用：返回「已同意加入」的服务器，用于下次启动自动加入。"""
    u = user_auth_uid_secret(uid, secret)
    if not u:
        return {"ok": False, "msg": "身份不对"}
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT j.server_id, s.* FROM join_reqs j "
            "JOIN servers s ON s.id = j.server_id "
            "WHERE j.uid = ? AND j.status = 'approved' AND s.deleted = 0 "
            "ORDER BY s.updated_at DESC", (uid,)).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["server_id"], "name": r["name"], "address": r["address"],
                "version": r["mcver"], "mods": [m for m in (r["mods"] or "").splitlines() if m.strip()],
                "modpack": r["modpack"], "markon": bool(r["markon"]),
                "updatedAt": r["updated_at"],
            })
        return {"ok": True, "list": out}
    finally:
        conn.close()




def user_auth_uid_secret(uid: str, secret: str):
    row = user_by_uid(uid)
    if not row or not secret or not hmac.compare_digest(row["secret"], secret):
        return None
    return row



# ==================================================================
# 社区网站：合作
# ==================================================================


def coop_summary(conn, r, uid: str = "", admin: bool = False) -> dict:
    members = [{"uid": m["uid"], "name": m["name"], "role": m["role"]}
               for m in conn.execute(
                   "SELECT * FROM coop_members WHERE coop_id = ? ORDER BY joined_at",
                   (r["id"],))]
    joined = admin or any(m["uid"] == uid for m in members) if uid else admin
    return {
        "id": r["id"], "title": r["title"] or "", "desc": r["descr"] or "",
        "owner": r["owner_name"] or "", "ownerUid": r["owner_uid"] or "",
        "members": members if joined else [{"uid": m["uid"], "name": m["name"],
                                            "role": m["role"]} for m in members],
        "joined": joined, "msgCount": int(conn.execute(
            "SELECT COUNT(*) AS c FROM coop_msgs WHERE coop_id = ?",
            (r["id"],)).fetchone()["c"]),
        "fileCount": int(conn.execute(
            "SELECT COUNT(*) AS c FROM coop_files WHERE coop_id = ?",
            (r["id"],)).fetchone()["c"]),
        "createdAt": r["created_at"] or "", "updatedAt": r["updated_at"] or "",
    }




def coop_member(conn, cid: int, uid: str):
    if not uid:
        return None
    return conn.execute("SELECT * FROM coop_members WHERE coop_id = ? AND uid = ?",
                        (cid, uid)).fetchone()
