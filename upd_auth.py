# -*- coding: utf-8 -*-
"""脆冰更新服务端 - 管理员口令、会话与用户认证。"""

from __future__ import annotations

from urllib.parse import parse_qs, quote, unquote, urlparse
import secrets
import hashlib
import sqlite3
import hmac
import time
import os

from upd_config import COOKIE_NAME, DATA_DIR, SESSION_TTL, TOKEN_FILE, _db_lock, now_str, strval
from upd_db import connect, meta_get, meta_set




def ensure_admin_pwd() -> None:
    """管理员口令：环境变量 CBUPDATE_TOKEN 优先；否则用 meta 里的哈希，
    没有就随机生成一个（只在窗口里显示一次，并写入 update_data/token.txt）。
    数据库里只存 盐+哈希，不存明文。"""
    global ADMIN_SALT, ADMIN_HASH
    env = (os.environ.get("CBUPDATE_TOKEN") or "").strip()
    if env:
        ADMIN_SALT = ""
        ADMIN_HASH = hashlib.sha256(env.encode("utf-8")).hexdigest()
        return
    stored = meta_get("admin_pwd", "")
    if not stored:
        pwd = secrets.token_urlsafe(10)
        ADMIN_SALT = secrets.token_hex(8)
        ADMIN_HASH = _pwd_hash(pwd, ADMIN_SALT)
        meta_set("admin_pwd", ADMIN_SALT + "$" + ADMIN_HASH)
        meta_set("admin_pwd_default", "1")
        global ADMIN_PWD_ONCE
        ADMIN_PWD_ONCE = pwd
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
                fh.write(pwd + "\n")
        except Exception:      # noqa: BLE001
            pass
        return
    ADMIN_SALT, _, ADMIN_HASH = stored.partition("$")




def _pwd_hash(pwd: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + pwd).encode("utf-8")).hexdigest()




def _store_pwd(pwd: str) -> None:
    """把新口令以 盐+哈希 写进 meta（明文不落盘）。"""
    salt = secrets.token_hex(8)
    meta_set("admin_pwd", salt + "$" + _pwd_hash(pwd, salt))
    meta_set("admin_pwd_default", "0")
    global ADMIN_SALT, ADMIN_HASH
    ADMIN_SALT, ADMIN_HASH = salt, _pwd_hash(pwd, salt)




ADMIN_SALT = ""

ADMIN_HASH = ""

ADMIN_PWD_ONCE = ""      # 首次启动随机生成的口令，打印一次后不再保存明文




def check_admin_pwd(pwd: str) -> bool:
    if not pwd:
        return False
    if not ADMIN_SALT:
        return hmac.compare_digest(
            hashlib.sha256(pwd.encode("utf-8")).hexdigest(), ADMIN_HASH)
    return hmac.compare_digest(_pwd_hash(pwd, ADMIN_SALT), ADMIN_HASH)




def new_session(role: str = "admin", uid: str = "", name: str = "") -> str:
    """会话写进数据库：服务端重启也不丢，1 天内同设备免重输密码。"""
    sid = secrets.token_urlsafe(24)
    with _db_lock:
        conn = connect()
        try:
            conn.execute("INSERT INTO sessions(sid, exp, role, uid, name) VALUES(?,?,?,?,?)",
                         (sid, time.time() + SESSION_TTL, role, uid, name))
            conn.execute("DELETE FROM sessions WHERE exp < ?", (time.time(),))
            conn.commit()
        finally:
            conn.close()
    return sid




def session_get(sid: str):
    if not sid:
        return None
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
    finally:
        conn.close()
    if not row or row["exp"] < time.time():
        if row:
            with _db_lock:
                conn = connect()
                try:
                    conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
                    conn.commit()
                finally:
                    conn.close()
        return None
    return row




def drop_session(sid: str) -> None:
    with _db_lock:
        conn = connect()
        try:
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
            conn.commit()
        finally:
            conn.close()




def cookie_of(headers) -> str:
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(COOKIE_NAME + "="):
            return unquote(part[len(COOKIE_NAME) + 1:])
    return ""



# ==================================================================
# 社区网站：用户身份
# ==================================================================


def user_by_uid(uid: str):
    if not uid:
        return None
    conn = connect()
    try:
        return conn.execute("SELECT * FROM site_users WHERE uid = ?", (uid,)).fetchone()
    finally:
        conn.close()




def user_login(name: str) -> dict:
    """按昵称登录 / 注册：同一个昵称+口令凭据再次登录返回同一身份。"""
    name = strval(name, 40)
    if not name:
        return {"ok": False, "msg": "请先给自己起个名字"}
    uid = "u-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM site_users WHERE uid = ?", (uid,)).fetchone()
        if row:
            return {"ok": True, "uid": row["uid"], "name": row["name"],
                    "secret": row["secret"]}
        secret = secrets.token_urlsafe(18)
        conn.execute("INSERT INTO site_users(uid, name, secret, created_at) VALUES(?,?,?,?)",
                     (uid, name, secret, now_str()))
        conn.commit()
        return {"ok": True, "uid": uid, "name": name, "secret": secret}
    finally:
        conn.close()




def user_auth(headers) -> sqlite3.Row:
    """从请求头取 X-Uid / X-Secret 校验用户身份。"""
    uid = headers.get("X-Uid") or ""
    secret = headers.get("X-Secret") or ""
    row = user_by_uid(uid)
    if not row or not secret or not hmac.compare_digest(row["secret"], secret):
        return None
    return row




def user_or_admin(headers):
    """管理员可代替用户操作（后台管理用）。"""
    u = user_auth(headers)
    if u is not None:
        return u, "user"
    if self_authed(headers):
        return None, "admin"
    return None, None




def self_authed(headers) -> bool:
    row = session_get(cookie_of(headers))
    return bool(row and row["role"] == "admin")
