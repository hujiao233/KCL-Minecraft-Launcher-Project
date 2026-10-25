# -*- coding: utf-8 -*-
"""
脆冰启动器 - 网站服务端（更新 + 社区）

一个纯标准库（http.server + sqlite3）的网站服务端，Python 3.8+ 直接跑：
  · 版本管理：发布版本 / 更新日志 / 下发文件（启动器检查更新）
  · 管理员：首次启动随机生成口令，只在服务端窗口显示一次并写入 update_data/token.txt；
    登录后同一设备 1 天内免重输（源码里不再保留任何明文口令）
  · 社区：MC 讨论（可传 mod / 整合包）、资源（渠道「KCL玩家开发」）
  · 合作：组队做 mod，消息 / 文件仅加入者可见
  · 建议：用户上传建议，管理员能收到
  · MC 服务器宣传：类型 / 版本 / 宣传图 / 宣传语 / 名称 / 人数上限 / mod / 整合包，
    搜索（类型、KCL 已下载版本、指定版本、mod 白/黑名单过滤 + 关键词），
    结果按最后修改时间排序；加入流程（同意 / 拒绝 / 地址告知）、服务器论坛公告、
    QQ 群 / kukechat 群同意后可见、支持 markon
  · 开发空间：启动器「开发」页连接这里，可看动态、点赞、评论、发帖

启动器那边的契约（server.py fetch_update）：
    GET <manifest 地址>  ->  {"version": "1.0.3", "notes": "...", "url": "...",
                              "files": {"main.py": "http://.../d/12", ...}}
    files 里的每个地址要能直接下载到文件内容，启动器会覆盖到自己的目录。
    拿不到（服务器没开 / 地址没填）时启动器静默当作没更新，照常使用。

用法：
    python update_server.py                    # 默认 127.0.0.1:8756
    python update_server.py --host 0.0.0.0 --port 8756
    python update_server.py --open             # 启动后自动打开网站
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

APP_NAME = "脆冰网站服务端"
APP_VERSION = "1.1"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CBUPDATE_DATA") or os.path.join(ROOT, "update_data")
DB_PATH = os.path.join(DATA_DIR, "server.db")
FILE_DIR = os.path.join(DATA_DIR, "files")
COOKIE_NAME = "cbupd_sid"
TOKEN_FILE = os.path.join(DATA_DIR, "token.txt")
SESSION_TTL = 24 * 3600               # 登录后 1 天内同设备免重输口令

_db_lock = threading.Lock()


# ==================================================================
# 初始化 / 数据库
# ==================================================================

def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILE_DIR, exist_ok=True)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _db_lock:
        conn = connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS releases (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    version    TEXT NOT NULL,
                    title      TEXT DEFAULT '',
                    notes      TEXT DEFAULT '',
                    url        TEXT DEFAULT '',
                    published  INTEGER NOT NULL DEFAULT 0,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS items (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    release_id INTEGER NOT NULL,
                    sort       INTEGER NOT NULL DEFAULT 0,
                    text       TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    release_id INTEGER NOT NULL,
                    rel        TEXT NOT NULL,
                    stored     TEXT NOT NULL,
                    size       INTEGER NOT NULL DEFAULT 0,
                    sha1       TEXT DEFAULT ''
                );
                -- ============ 社区网站 ============
                CREATE TABLE IF NOT EXISTS sessions (
                    sid  TEXT PRIMARY KEY,
                    exp  REAL NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin',
                    uid  TEXT DEFAULT '',
                    name TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS site_users (
                    uid        TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    secret     TEXT NOT NULL,
                    created_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    stored     TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    size       INTEGER NOT NULL DEFAULT 0,
                    owner_uid  TEXT DEFAULT '',
                    created_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS posts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind        TEXT NOT NULL,             -- dev|community|resource|suggestion
                    title       TEXT DEFAULT '',
                    content     TEXT DEFAULT '',
                    author_uid  TEXT DEFAULT '',
                    author_name TEXT DEFAULT '',
                    file_id     INTEGER,
                    likes       INTEGER NOT NULL DEFAULT 0,
                    deleted     INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT '',
                    updated_at  TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS post_likes (
                    post_id INTEGER NOT NULL,
                    uid     TEXT NOT NULL,
                    PRIMARY KEY (post_id, uid)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id     INTEGER NOT NULL,
                    author_uid  TEXT DEFAULT '',
                    author_name TEXT DEFAULT '',
                    text        TEXT DEFAULT '',
                    deleted     INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS coops (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT DEFAULT '',
                    descr       TEXT DEFAULT '',
                    owner_uid   TEXT DEFAULT '',
                    owner_name  TEXT DEFAULT '',
                    deleted     INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT '',
                    updated_at  TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS coop_members (
                    coop_id   INTEGER NOT NULL,
                    uid       TEXT NOT NULL,
                    name      TEXT DEFAULT '',
                    role      TEXT DEFAULT 'member',
                    joined_at TEXT DEFAULT '',
                    PRIMARY KEY (coop_id, uid)
                );
                CREATE TABLE IF NOT EXISTS coop_msgs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    coop_id     INTEGER NOT NULL,
                    author_uid  TEXT DEFAULT '',
                    author_name TEXT DEFAULT '',
                    text        TEXT DEFAULT '',
                    created_at  TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS coop_files (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    coop_id     INTEGER NOT NULL,
                    upload_id   INTEGER NOT NULL,
                    author_uid  TEXT DEFAULT '',
                    author_name TEXT DEFAULT '',
                    created_at  TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS servers (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_uid    TEXT DEFAULT '',
                    owner_name   TEXT DEFAULT '',
                    name         TEXT DEFAULT '',
                    stype        TEXT DEFAULT '',        -- 类型：生存/创造/空岛/模组...
                    mcver        TEXT DEFAULT '',        -- MC 版本
                    image        TEXT DEFAULT '',        -- 宣传图 URL
                    slogan       TEXT DEFAULT '',        -- 宣传语
                    max_players  INTEGER DEFAULT 0,      -- 人数上限
                    mods         TEXT DEFAULT '',        -- 必装 mod，一行一个
                    modpack      TEXT DEFAULT '',        -- 整合包
                    need_approve INTEGER NOT NULL DEFAULT 1,  -- 加入是否需要服主同意
                    address      TEXT DEFAULT '',        -- 服务器地址
                    qq_group     TEXT DEFAULT '',        -- QQ 群（同意后可见）
                    kuke_group   TEXT DEFAULT '',        -- kukechat 群（同意后可见）
                    markon       INTEGER NOT NULL DEFAULT 0,  -- 支持 markon
                    deleted      INTEGER NOT NULL DEFAULT 0,
                    created_at   TEXT DEFAULT '',
                    updated_at   TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS join_reqs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id  INTEGER NOT NULL,
                    uid        TEXT NOT NULL,
                    name       TEXT DEFAULT '',
                    status     TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS announces (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id   INTEGER NOT NULL,
                    author_uid  TEXT DEFAULT '',
                    author_name TEXT DEFAULT '',
                    text        TEXT DEFAULT '',
                    created_at  TEXT DEFAULT ''
                );
                """
            )
            conn.commit()
        finally:
            conn.close()


def meta_get(key: str, default: str = "") -> str:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default
    finally:
        conn.close()


def meta_set(key: str, value: str) -> None:
    with _db_lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()


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


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
# 数据读写
# ==================================================================

def strval(v, limit: int = 20000) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    return s.strip()[:limit]


def release_summary(conn, row) -> dict:
    rid = row["id"]
    items = [
        r["text"]
        for r in conn.execute(
            "SELECT text FROM items WHERE release_id = ? ORDER BY sort, id", (rid,)
        )
    ]
    files = [
        {"id": r["id"], "rel": r["rel"], "size": r["size"]}
        for r in conn.execute(
            "SELECT id, rel, size FROM files WHERE release_id = ? ORDER BY rel", (rid,)
        )
    ]
    total = sum(f["size"] or 0 for f in files)
    return {
        "id": rid,
        "version": row["version"],
        "title": row["title"] or "",
        "notes": row["notes"] or "",
        "url": row["url"] or "",
        "items": items,
        "files": files,
        "fileCount": len(files),
        "totalSize": total,
        "published": bool(row["published"]),
        "isCurrent": bool(row["is_current"]),
        "createdAt": row["created_at"] or "",
    }


def list_releases() -> list:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM releases ORDER BY is_current DESC, published DESC, id DESC"
        ).fetchall()
        return [release_summary(conn, r) for r in rows]
    finally:
        conn.close()


def current_release() -> sqlite3.Row:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM releases WHERE published = 1 AND is_current = 1 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def replace_items(conn, rid: int, items) -> None:
    conn.execute("DELETE FROM items WHERE release_id = ?", (rid,))
    n = 0
    for it in items or []:
        text = strval(it, 4000)
        if not text:
            continue
        conn.execute(
            "INSERT INTO items(release_id, sort, text) VALUES(?, ?, ?)", (rid, n, text)
        )
        n += 1


def create_release(body: dict) -> dict:
    version = strval(body.get("version"), 64)
    if not version:
        return {"ok": False, "msg": "版本号不能为空"}
    with _db_lock:
        conn = connect()
        try:
            exists = conn.execute(
                "SELECT id FROM releases WHERE version = ?", (version,)
            ).fetchone()
            if exists:
                return {"ok": False, "msg": "版本号 %s 已存在" % version}
            cur = conn.execute(
                "INSERT INTO releases(version, title, notes, url, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    version,
                    strval(body.get("title"), 200),
                    strval(body.get("notes"), 20000),
                    strval(body.get("url"), 1000),
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            rid = int(cur.lastrowid)
            replace_items(conn, rid, body.get("items") or [])
            conn.commit()
            return {"ok": True, "id": rid}
        finally:
            conn.close()


def update_release(rid: int, body: dict) -> dict:
    version = strval(body.get("version"), 64)
    if not version:
        return {"ok": False, "msg": "版本号不能为空"}
    with _db_lock:
        conn = connect()
        try:
            row = conn.execute("SELECT id FROM releases WHERE id = ?", (rid,)).fetchone()
            if not row:
                return {"ok": False, "msg": "版本不存在"}
            dup = conn.execute(
                "SELECT id FROM releases WHERE version = ? AND id != ?", (version, rid)
            ).fetchone()
            if dup:
                return {"ok": False, "msg": "版本号 %s 已被其它记录占用" % version}
            conn.execute(
                "UPDATE releases SET version = ?, title = ?, notes = ?, url = ? "
                "WHERE id = ?",
                (
                    version,
                    strval(body.get("title"), 200),
                    strval(body.get("notes"), 20000),
                    strval(body.get("url"), 1000),
                    rid,
                ),
            )
            replace_items(conn, rid, body.get("items") or [])
            conn.commit()
            return {"ok": True, "id": rid}
        finally:
            conn.close()


def publish_release(rid: int, on: bool) -> dict:
    with _db_lock:
        conn = connect()
        try:
            row = conn.execute("SELECT id FROM releases WHERE id = ?", (rid,)).fetchone()
            if not row:
                return {"ok": False, "msg": "版本不存在"}
            if on:
                conn.execute("UPDATE releases SET is_current = 0")
                conn.execute(
                    "UPDATE releases SET published = 1, is_current = 1 WHERE id = ?",
                    (rid,),
                )
            else:
                conn.execute(
                    "UPDATE releases SET published = 0, is_current = 0 WHERE id = ?",
                    (rid,),
                )
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()


def delete_release(rid: int) -> dict:
    with _db_lock:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT stored FROM files WHERE release_id = ?", (rid,)
            ).fetchall()
            conn.execute("DELETE FROM items WHERE release_id = ?", (rid,))
            conn.execute("DELETE FROM files WHERE release_id = ?", (rid,))
            conn.execute("DELETE FROM releases WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
    for r in rows:
        try:
            os.remove(os.path.join(FILE_DIR, r["stored"]))
        except OSError:
            pass
    return {"ok": True}


def clean_rel(rel: str) -> str:
    """规范化相对路径，挡掉 ../ 之类越界写法。"""
    rel = (rel or "").strip().replace("\\", "/")
    rel = rel.lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def save_file(rid: int, rel: str, src_path: str, size: int) -> dict:
    rel = clean_rel(rel)
    if not rel:
        return {"ok": False, "msg": "请填写文件在启动器目录下的路径（例如 main.py）"}
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM releases WHERE id = ?", (rid,)).fetchone()
        if not row:
            return {"ok": False, "msg": "版本不存在"}
        old = conn.execute(
            "SELECT stored FROM files WHERE release_id = ? AND rel = ?", (rid, rel)
        ).fetchone()
    finally:
        conn.close()

    stored = old["stored"] if old else (uuid.uuid4().hex + ".bin")
    dst = os.path.join(FILE_DIR, stored)
    sha1 = hashlib.sha1()
    with open(src_path, "rb") as f:
        with open(dst, "wb") as out:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                sha1.update(chunk)
                out.write(chunk)
    digest = sha1.hexdigest()

    with _db_lock:
        conn = connect()
        try:
            if old:
                conn.execute(
                    "UPDATE files SET stored = ?, size = ?, sha1 = ? "
                    "WHERE release_id = ? AND rel = ?",
                    (stored, size, digest, rid, rel),
                )
            else:
                conn.execute(
                    "INSERT INTO files(release_id, rel, stored, size, sha1) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (rid, rel, stored, size, digest),
                )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "rel": rel, "size": size, "sha1": digest}


def remove_file(file_id: int) -> dict:
    with _db_lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT stored FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.commit()
        finally:
            conn.close()
    if row:
        try:
            os.remove(os.path.join(FILE_DIR, row["stored"]))
        except OSError:
            pass
    return {"ok": True}


def file_by_id(file_id: int) -> sqlite3.Row:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM files WHERE id = ?", (file_id,)
        ).fetchone()
    finally:
        conn.close()


# ==================================================================
# 清单与 URL
# ==================================================================

def manifest_of(row: sqlite3.Row, base_url: str) -> dict:
    conn = connect()
    try:
        items = [
            r["text"]
            for r in conn.execute(
                "SELECT text FROM items WHERE release_id = ? ORDER BY sort, id",
                (row["id"],),
            )
        ]
        files = {}
        for r in conn.execute(
            "SELECT id, rel FROM files WHERE release_id = ? ORDER BY rel", (row["id"],)
        ):
            files[r["rel"]] = base_url.rstrip("/") + "/d/%d" % r["id"]
    finally:
        conn.close()
    notes = (row["notes"] or "").strip()
    if items:
        notes = (notes + ("\n\n" if notes else "") + "\n".join("- " + i for i in items)).strip()
    return {
        "version": row["version"],
        "title": row["title"] or "",
        "notes": notes,
        "url": row["url"] or "",
        "publishedAt": row["created_at"] or "",
        "files": files,
    }


def guess_base_url(host_header: str) -> str:
    cfg = meta_get("baseUrl", "").strip()
    if cfg:
        return cfg.rstrip("/")
    host = (host_header or "").strip() or "127.0.0.1:8756"
    scheme = "https" if host.endswith(":443") else "http"
    return scheme + "://" + host


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


# ==================================================================
# 社区网站：帖子（社区 / 资源 / 建议 / 开发空间）
# ==================================================================

POST_KINDS = ("dev", "community", "resource", "suggestion")


def post_row(conn, pid: int):
    return conn.execute(
        "SELECT * FROM posts WHERE id = ? AND deleted = 0", (pid,)).fetchone()


def post_summary(conn, r, uid: str = "") -> dict:
    like_cnt = r["likes"] or 0
    cm = conn.execute(
        "SELECT COUNT(*) AS c FROM comments WHERE post_id = ? AND deleted = 0",
        (r["id"],)).fetchone()["c"]
    liked = False
    if uid:
        liked = conn.execute("SELECT 1 FROM post_likes WHERE post_id = ? AND uid = ?",
                             (r["id"], uid)).fetchone() is not None
    f = None
    if r["file_id"]:
        fr = conn.execute("SELECT id, name, size FROM uploads WHERE id = ?",
                          (r["file_id"],)).fetchone()
        if fr:
            f = {"id": fr["id"], "name": fr["name"], "size": fr["size"]}
    return {
        "id": r["id"], "kind": r["kind"], "title": r["title"] or "",
        "content": r["content"] or "", "author": r["author_name"] or "",
        "authorUid": r["author_uid"] or "", "file": f,
        "likes": like_cnt, "liked": liked, "comments": cm,
        "createdAt": r["created_at"] or "", "updatedAt": r["updated_at"] or "",
    }


def list_posts(kind: str, uid: str = "", for_admin: bool = False, author_uid: str = "") -> list:
    conn = connect()
    try:
        sql = "SELECT * FROM posts WHERE deleted = 0 AND kind = ?"
        args = [kind]
        # 建议：作者只看得到自己的，管理员全看得到
        if kind == "suggestion" and not for_admin:
            sql += " AND author_uid = ?"
            args.append(author_uid or "-")
        if author_uid and kind != "suggestion":
            pass
        sql += " ORDER BY id DESC LIMIT 200"
        rows = conn.execute(sql, args).fetchall()
        return [post_summary(conn, r, uid) for r in rows]
    finally:
        conn.close()


def create_post(kind: str, body: dict, user) -> dict:
    title = strval(body.get("title"), 200)
    content = strval(body.get("content"), 20000)
    fid = body.get("fileId")
    if kind == "resource" and not title:
        title = "未命名资源"
    if kind in ("community", "suggestion") and not title:
        title = (content[:30] or "无标题")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO posts(kind, title, content, author_uid, author_name, file_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (kind, title, content, user["uid"], user["name"],
             int(fid) if fid else None, now_str(), now_str()))
        conn.commit()
        return {"ok": True, "id": int(cur.lastrowid)}
    finally:
        conn.close()


def add_comment(pid: int, text: str, user) -> dict:
    text = strval(text, 4000)
    if not text:
        return {"ok": False, "msg": "评论不能为空"}
    with _db_lock:
        conn = connect()
        try:
            if not post_row(conn, pid):
                return {"ok": False, "msg": "帖子不存在"}
            conn.execute(
                "INSERT INTO comments(post_id, author_uid, author_name, text, created_at) "
                "VALUES(?,?,?,?,?)", (pid, user["uid"], user["name"], text, now_str()))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


def list_comments(pid: int) -> list:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM comments WHERE post_id = ? AND deleted = 0 ORDER BY id",
            (pid,)).fetchall()
        return [{"id": r["id"], "author": r["author_name"] or "",
                 "authorUid": r["author_uid"] or "", "text": r["text"] or "",
                 "createdAt": r["created_at"] or ""} for r in rows]
    finally:
        conn.close()


def toggle_like(pid: int, uid: str) -> dict:
    with _db_lock:
        conn = connect()
        try:
            if not post_row(conn, pid):
                return {"ok": False, "msg": "帖子不存在"}
            hit = conn.execute("SELECT 1 FROM post_likes WHERE post_id = ? AND uid = ?",
                               (pid, uid)).fetchone()
            if hit:
                conn.execute("DELETE FROM post_likes WHERE post_id = ? AND uid = ?", (pid, uid))
                conn.execute("UPDATE posts SET likes = likes - 1 WHERE id = ? AND likes > 0", (pid,))
                liked = False
            else:
                conn.execute("INSERT INTO post_likes(post_id, uid) VALUES(?,?)", (pid, uid))
                conn.execute("UPDATE posts SET likes = likes + 1 WHERE id = ?", (pid,))
                liked = True
            n = conn.execute("SELECT likes FROM posts WHERE id = ?", (pid,)).fetchone()["likes"]
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "liked": liked, "likes": n}


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



# ==================================================================
# HTTP 处理
# ==================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "CuibingUpdateServer/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):   # 静默默认日志
        pass

    def _cors(self) -> list:
        # 启动器界面跨域调用 API 需要 CORS（POST/PUT 带 X-Uid 等自定义头，需响应预检）
        return [("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type, X-Uid, X-Secret, X-Token"),
                ("Access-Control-Max-Age", "600")]

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []) + self._cors():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200, extra=None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra)

    def _html(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    def _fail(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "msg": msg}, code)

    def _path_qs(self):
        u = urlparse(self.path)
        return u.path.rstrip("/") or "/", parse_qs(u.query or "")

    def _json_body(self):
        # 统一从缓存解析（do_POST 入口已把 body 读完，避免 keep-alive 残留污染下一请求）
        raw = getattr(self, "_body_cache", None)
        if raw is None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            self._body_cache = raw
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _authed(self) -> bool:
        return self_authed(self.headers)

    def _user(self):
        return user_auth(self.headers)

    # ---------- 路由 ----------
    def do_HEAD(self):
        self.handle_get(head=True)

    def do_GET(self):
        self.handle_get(head=False)

    def handle_get(self, head: bool = False):
        path, qs = self._path_qs()

        if path in ("/", "/site"):
            self._html(SITE_HTML)
            return
        if path == "/admin":
            self._html(ADMIN_HTML)
            return
        if path == "/api/health":
            self._json({"ok": True, "app": "CuibingSiteServer", "version": APP_VERSION})
            return
        if path == "/api/state":
            cur = current_release()
            row = session_get(cookie_of(self.headers))
            self._json({
                "ok": True,
                "logged": bool(row and row["role"] == "admin"),
                "current": cur["version"] if cur else "",
                "baseUrl": meta_get("baseUrl", ""),
            })
            return
        if path == "/api/manifest":
            cur = current_release()
            if not cur:
                self._fail("还没有发布任何版本", 404)
                return
            self._json(manifest_of(cur, guess_base_url(self.headers.get("Host"))))
            return
        if path == "/api/changelog":
            conn = connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM releases WHERE published = 1 "
                    "ORDER BY id DESC LIMIT 50"
                ).fetchall()
                out = []
                for r in rows:
                    items = [
                        x["text"] for x in conn.execute(
                            "SELECT text FROM items WHERE release_id = ? "
                            "ORDER BY sort, id", (r["id"],)
                        )
                    ]
                    out.append({
                        "version": r["version"], "title": r["title"] or "",
                        "notes": r["notes"] or "", "url": r["url"] or "",
                        "items": items, "createdAt": r["created_at"] or "",
                    })
            finally:
                conn.close()
            self._json({"ok": True, "list": out})
            return

        # ============ 社区网站公开接口 ============
        if path == "/api/launcher/state":
            # 启动器「开发」页的连通性探测
            self._json({"ok": True, "app": APP_NAME, "siteVersion": APP_VERSION})
            return

        if path == "/api/user/login":
            # 用昵称换取身份（uid + secret），之后写操作带 X-Uid / X-Secret
            name = strval((qs.get("name") or [""])[0], 40)
            self._json(user_login(name))
            return

        if path == "/api/posts":
            kind = (qs.get("kind") or ["dev"])[0]
            if kind not in POST_KINDS:
                self._fail("未知栏目：" + kind)
                return
            u = self._user()
            admin = self._authed()
            author = (u["uid"] if u else "") or ((qs.get("author") or [""])[0])
            self._json({"ok": True, "list": list_posts(
                kind, u["uid"] if u else "", admin, author)})
            return

        if path.startswith("/api/posts/"):
            m = self._match_id(path, "/api/posts/")
            if m and m[1] == "comments":
                u = self._user()
                self._json({"ok": True, "list": list_comments(m[0])})
                return
            if m and m[1] == "":
                conn = connect()
                try:
                    r = post_row(conn, m[0])
                    u = self._user()
                    self._json({"ok": True, "post": post_summary(
                        conn, r, u["uid"] if u else "")} if r else {"ok": False})
                finally:
                    conn.close()
                return

        if path == "/api/servers":
            u = self._user()
            self._json({"ok": True, "list": search_servers(
                qs, u["uid"] if u else "", self._authed())})
            return

        if path == "/api/servers/mine":
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            conn = connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM servers WHERE deleted = 0 AND owner_uid = ? "
                    "ORDER BY updated_at DESC", (u["uid"],)).fetchall()
                self._json({"ok": True, "list": [
                    server_summary(conn, r, u["uid"], self._authed()) for r in rows]})
            finally:
                conn.close()
            return

        if path.startswith("/api/servers/"):
            m = self._match_id(path, "/api/servers/")
            if m and m[1] == "requests":
                # 服主看待处理申请；普通用户看自己的申请状态
                u = self._user()
                if not u:
                    self._fail("请先设置昵称", 401)
                    return
                conn = connect()
                try:
                    r = conn.execute("SELECT * FROM servers WHERE id = ?", (m[0],)).fetchone()
                    if not r or r["deleted"]:
                        self._fail("服务器不存在", 404)
                        return
                    if r["owner_uid"] == u["uid"] or self._authed():
                        rows = conn.execute(
                            "SELECT * FROM join_reqs WHERE server_id = ? ORDER BY id DESC",
                            (m[0],)).fetchall()
                        out = [{"id": x["id"], "uid": x["uid"], "name": x["name"],
                                "status": x["status"], "createdAt": x["created_at"]}
                               for x in rows]
                    else:
                        rows = conn.execute(
                            "SELECT * FROM join_reqs WHERE server_id = ? AND uid = ? "
                            "ORDER BY id DESC LIMIT 1", (m[0], u["uid"])).fetchall()
                        out = [{"id": x["id"], "status": x["status"],
                                "createdAt": x["created_at"]} for x in rows]
                    self._json({"ok": True, "list": out})
                finally:
                    conn.close()
                return
            if m and m[1] == "announces":
                u = self._user()
                lst = server_announces(m[0], u["uid"] if u else "", self._authed())
                if lst is None:
                    self._fail("服务器不存在", 404)
                    return
                self._json({"ok": True, "list": lst})
                return
            if m and m[1] == "":
                u = self._user()
                conn = connect()
                try:
                    r = conn.execute("SELECT * FROM servers WHERE id = ? AND deleted = 0",
                                     (m[0],)).fetchone()
                    self._json({"ok": bool(r), "server": server_summary(
                        conn, r, u["uid"] if u else "", self._authed())} if r
                        else {"ok": False, "msg": "服务器不存在"})
                finally:
                    conn.close()
                return

        if path == "/api/coops":
            u = self._user()
            admin = self._authed()
            conn = connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM coops WHERE deleted = 0 ORDER BY updated_at DESC "
                    "LIMIT 200").fetchall()
                self._json({"ok": True, "list": [
                    coop_summary(conn, r, u["uid"] if u else "", admin) for r in rows]})
            finally:
                conn.close()
            return

        if path.startswith("/api/coops/"):
            m = self._match_id(path, "/api/coops/")
            if m and m[1] == "":
                u = self._user()
                admin = self._authed()
                conn = connect()
                try:
                    r = conn.execute("SELECT * FROM coops WHERE id = ? AND deleted = 0",
                                     (m[0],)).fetchone()
                    if not r:
                        self._fail("合作不存在", 404)
                        return
                    info = coop_summary(conn, r, u["uid"] if u else "", admin)
                    if info["joined"]:
                        msgs = [{"id": x["id"], "author": x["author_name"] or "",
                                 "text": x["text"] or "", "createdAt": x["created_at"] or ""}
                                for x in conn.execute(
                                    "SELECT * FROM coop_msgs WHERE coop_id = ? ORDER BY id "
                                    "DESC LIMIT 100", (m[0],))]
                        files = []
                        for x in conn.execute(
                                "SELECT f.*, u.name AS fname, u.size AS fsize "
                                "FROM coop_files f JOIN uploads u ON u.id = f.upload_id "
                                "WHERE f.coop_id = ? ORDER BY f.id DESC", (m[0],)):
                            files.append({"id": x["upload_id"], "name": x["fname"],
                                          "size": x["fsize"], "by": x["author_name"] or "",
                                          "createdAt": x["created_at"] or ""})
                        info["messages"] = msgs
                        info["files"] = files
                    self._json({"ok": True, "coop": info})
                finally:
                    conn.close()
                return

        # 上传文件下载：/f/<id>
        if path.startswith("/f/"):
            try:
                fid = int(path[3:])
            except ValueError:
                self._fail("文件不存在", 404)
                return
            conn = connect()
            try:
                row = conn.execute("SELECT * FROM uploads WHERE id = ?", (fid,)).fetchone()
            finally:
                conn.close()
            if not row:
                self._fail("文件不存在", 404)
                return
            fp = os.path.join(FILE_DIR, row["stored"])
            if not os.path.isfile(fp):
                self._fail("文件内容已丢失", 404)
                return
            name = row["name"] or ("file-%d" % fid)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(fp)))
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''%s" % quote(name))
            self.end_headers()
            if head:
                return
            try:
                with open(fp, "rb") as f:
                    while True:
                        chunk = f.read(1 << 18)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # 文件下载：/d/<id>
        if path.startswith("/d/"):
            try:
                fid = int(path[3:])
            except ValueError:
                self._fail("文件不存在", 404)
                return
            row = file_by_id(fid)
            if not row:
                self._fail("文件不存在", 404)
                return
            fp = os.path.join(FILE_DIR, row["stored"])
            if not os.path.isfile(fp):
                self._fail("文件内容已丢失", 404)
                return
            name = row["rel"].rsplit("/", 1)[-1]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(fp)))
            self.send_header(
                "Content-Disposition",
                "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                % (name, quote(name)),
            )
            self.end_headers()
            if head:
                return
            try:
                with open(fp, "rb") as f:
                    while True:
                        chunk = f.read(1 << 18)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # 单个版本的清单预览
        m = self._match_id(path, "/api/releases/")
        if m and m[1] == "manifest":
            if not self._authed():
                self._fail("请先登录", 401)
                return
            conn = connect()
            try:
                row = conn.execute(
                    "SELECT * FROM releases WHERE id = ?", (m[0],)
                ).fetchone()
            finally:
                conn.close()
            if not row:
                self._fail("版本不存在", 404)
                return
            self._json({"ok": True,
                        "manifest": manifest_of(row,
                                                guess_base_url(self.headers.get("Host")))})
            return

        if path == "/api/releases":
            if not self._authed():
                self._fail("请先登录", 401)
                return
            self._json({"ok": True, "list": list_releases()})
            return

        self._fail("接口不存在：" + path, 404)

    def do_POST(self):
        path, qs = self._path_qs()
        # 入口先把请求体读完：任何分支提前返回都不会残留字节污染 keep-alive 连接
        length = int(self.headers.get("Content-Length") or 0)
        self._body_cache = self.rfile.read(length) if length > 0 else b""

        # ---- 登录 / 登出 ----
        if path == "/api/login":
            body = self._json_body()
            if not check_admin_pwd(strval(body.get("token") or body.get("password"), 200)):
                self._fail("口令不对", 401)
                return
            sid = new_session("admin")
            self._json(
                {"ok": True},
                extra=[("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                        % (COOKIE_NAME, sid, SESSION_TTL))],
            )
            return
        if path == "/api/logout":
            drop_session(cookie_of(self.headers))
            self._json({"ok": True})
            return

        # ============ 社区网站：需要用户身份 ============
        if path == "/api/user/login":
            body = self._json_body()
            r = user_login(strval(body.get("name"), 40))
            self._json(r, 200 if r.get("ok") else 400)
            return

        if path == "/api/posts":
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            body = self._json_body()
            kind = strval(body.get("kind"), 20)
            if kind not in POST_KINDS:
                self._fail("未知栏目")
                return
            self._json(create_post(kind, body, u))
            return

        m = self._match_id(path, "/api/posts/")
        if m:
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            if m[1] == "like":
                self._json(toggle_like(m[0], u["uid"]))
                return
            if m[1] == "comments":
                body = self._json_body()
                r = add_comment(m[0], strval(body.get("text"), 4000), u)
                self._json(r, 200 if r.get("ok") else 400)
                return
            if m[1] == "delete":
                conn = connect()
                try:
                    r = post_row(conn, m[0])
                    admin = self._authed()
                    if not r or (r["author_uid"] != u["uid"] and not admin):
                        self._fail("只能删除自己的帖子", 403)
                        return
                finally:
                    conn.close()
                with _db_lock:
                    conn = connect()
                    try:
                        conn.execute("UPDATE posts SET deleted = 1 WHERE id = ?", (m[0],))
                        conn.commit()
                    finally:
                        conn.close()
                self._json({"ok": True})
                return
            self._fail("接口不存在：" + path, 404)
            return

        # ---- MC 服务器宣传 ----
        if path == "/api/servers":
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            r = save_server(self._json_body(), u)
            self._json(r, 200 if r.get("ok") else 400)
            return

        m = self._match_id(path, "/api/servers/")
        if m:
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            if m[1] == "join":
                r = request_join(m[0], u)
                self._json(r, 200 if r.get("ok") else 400)
                return
            if m[1] == "save":
                r = save_server(self._json_body(), u, sid=m[0])
                self._json(r, 200 if r.get("ok") else 400)
                return
            if m[1] == "announce":
                conn = connect()
                try:
                    r0 = conn.execute("SELECT * FROM servers WHERE id = ?", (m[0],)).fetchone()
                finally:
                    conn.close()
                if not r0 or r0["deleted"]:
                    self._fail("服务器不存在", 404)
                    return
                if r0["owner_uid"] != u["uid"] and not self._authed():
                    self._fail("只有服主能发公告", 403)
                    return
                text = strval(self._json_body().get("text"), 4000)
                if not text:
                    self._fail("公告内容不能为空")
                    return
                with _db_lock:
                    conn = connect()
                    try:
                        conn.execute(
                            "INSERT INTO announces(server_id, author_uid, author_name, "
                            "text, created_at) VALUES(?,?,?,?,?)",
                            (m[0], u["uid"], u["name"], text, now_str()))
                        conn.commit()
                    finally:
                        conn.close()
                self._json({"ok": True})
                return
            if m[1] == "delete":
                conn = connect()
                try:
                    r0 = conn.execute("SELECT * FROM servers WHERE id = ?", (m[0],)).fetchone()
                    if not r0 or (r0["owner_uid"] != u["uid"] and not self._authed()):
                        self._fail("只能删除自己发布的服务器", 403)
                        return
                finally:
                    conn.close()
                with _db_lock:
                    conn = connect()
                    try:
                        conn.execute("UPDATE servers SET deleted = 1 WHERE id = ?", (m[0],))
                        conn.commit()
                    finally:
                        conn.close()
                self._json({"ok": True})
                return
            self._fail("接口不存在：" + path, 404)
            return

        # ---- 合作 ----
        if path == "/api/coops":
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            body = self._json_body()
            title = strval(body.get("title"), 120)
            if not title:
                self._fail("合作标题不能为空")
                return
            with _db_lock:
                conn = connect()
                try:
                    cur = conn.execute(
                        "INSERT INTO coops(title, descr, owner_uid, owner_name, "
                        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                        (title, strval(body.get("desc"), 4000), u["uid"], u["name"],
                         now_str(), now_str()))
                    conn.execute(
                        "INSERT INTO coop_members(coop_id, uid, name, role, joined_at) "
                        "VALUES(?,?,?,?,?)",
                        (int(cur.lastrowid), u["uid"], u["name"], "owner", now_str()))
                    conn.commit()
                    self._json({"ok": True, "id": int(cur.lastrowid)})
                finally:
                    conn.close()
            return

        m = self._match_id(path, "/api/coops/")
        if m:
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            cid = m[0]
            if m[1] == "join":
                conn = connect()
                try:
                    r0 = conn.execute("SELECT * FROM coops WHERE id = ? AND deleted = 0",
                                      (cid,)).fetchone()
                    if not r0:
                        self._fail("合作不存在", 404)
                        return
                    if not coop_member(conn, cid, u["uid"]):
                        conn.execute(
                            "INSERT OR IGNORE INTO coop_members(coop_id, uid, name, role, "
                            "joined_at) VALUES(?,?,?,?,?)",
                            (cid, u["uid"], u["name"], "member", now_str()))
                        conn.execute("UPDATE coops SET updated_at = ? WHERE id = ?",
                                     (now_str(), cid))
                        conn.commit()
                finally:
                    conn.close()
                self._json({"ok": True, "joined": True})
                return
            if m[1] == "message":
                conn = connect()
                try:
                    if not coop_member(conn, cid, u["uid"]) and not self._authed():
                        self._fail("加入合作后才能查看与发言", 403)
                        return
                finally:
                    conn.close()
                text = strval(self._json_body().get("text"), 4000)
                if not text:
                    self._fail("消息不能为空")
                    return
                with _db_lock:
                    conn = connect()
                    try:
                        conn.execute(
                            "INSERT INTO coop_msgs(coop_id, author_uid, author_name, "
                            "text, created_at) VALUES(?,?,?,?,?)",
                            (cid, u["uid"], u["name"], text, now_str()))
                        conn.execute("UPDATE coops SET updated_at = ? WHERE id = ?",
                                     (now_str(), cid))
                        conn.commit()
                    finally:
                        conn.close()
                self._json({"ok": True})
                return
            self._fail("接口不存在：" + path, 404)
            return

        # ---- 启动器：自动加入列表 ----
        if path == "/api/launcher/joinlist":
            body = self._json_body()
            self._json(launcher_joinlist(strval(body.get("uid"), 64),
                                         strval(body.get("secret"), 128)))
            return

        # ---- 服主处理加入申请 ----
        m = self._match_id(path, "/api/joinreqs/")
        if m and m[1] == "decide":
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            body = self._json_body()
            r = decide_join(m[0], u["uid"], bool(body.get("approve")))
            self._json(r, 200 if r.get("ok") else 400)
            return

        # ---- 以下都要管理员登录 ----
        if not self._authed():
            self._fail("请先登录", 401)
            return

        if path == "/api/config":
            body = self._json_body()
            meta_set("baseUrl", strval(body.get("baseUrl"), 500))
            self._json({"ok": True, "baseUrl": meta_get("baseUrl", "")})
            return

        if path == "/api/admin/changepwd":
            body = self._json_body()
            new = strval(body.get("password"), 100)
            if len(new) < 6:
                self._fail("新口令至少 6 位")
                return
            _store_pwd(new)
            self._json({"ok": True})
            return

        if path == "/api/releases":
            r = create_release(self._json_body())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if path == "/api/releases/preview":
            body = self._json_body()
            version = strval(body.get("version"), 64) or "预览"
            items = [strval(i, 4000) for i in (body.get("items") or []) if strval(i, 4000)]
            notes = strval(body.get("notes"), 20000)
            if items:
                notes = (notes + ("\n\n" if notes else "") +
                         "\n".join("- " + i for i in items)).strip()
            self._json({"ok": True, "manifest": {
                "version": version,
                "title": strval(body.get("title"), 200),
                "notes": notes,
                "url": strval(body.get("url"), 1000),
                "files": {},
            }})
            return

        m = self._match_id(path, "/api/releases/")
        if m:
            rid, rest = m
            if rest == "":
                r = update_release(rid, self._json_body())
                self._json(r, 200 if r.get("ok") else 400)
                return
            if rest == "publish":
                self._json(publish_release(rid, True))
                return
            if rest == "unpublish":
                self._json(publish_release(rid, False))
                return
            if rest == "delete":
                self._json(delete_release(rid))
                return
            if rest == "manifest":
                conn = connect()
                try:
                    row = conn.execute(
                        "SELECT * FROM releases WHERE id = ?", (rid,)
                    ).fetchone()
                finally:
                    conn.close()
                if not row:
                    self._fail("版本不存在", 404)
                    return
                self._json({"ok": True,
                            "manifest": manifest_of(row, guess_base_url(
                                self.headers.get("Host")))})
                return
            if rest == "files/remove":
                try:
                    fid = int(self._json_body().get("fileId"))
                except (TypeError, ValueError):
                    self._fail("缺少文件 ID")
                    return
                self._json(remove_file(fid))
                return

        self._fail("接口不存在：" + path, 404)

    def _match_id(self, path: str, prefix: str):
        if not path.startswith(prefix):
            return None
        tail = path[len(prefix):]
        if not tail:
            return None
        parts = tail.split("/")
        try:
            rid = int(parts[0])
        except ValueError:
            return None
        return rid, "/".join(parts[1:])

    # ---------- 上传：body 就是文件字节 ----------
    def do_PUT(self):
        self._receive_file()

    def do_OPTIONS(self):
        # CORS 预检
        self.send_response(204)
        for k, v in self._cors():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _receive_file(self):
        path, qs = self._path_qs()
        m_rel = self._match_id(path, "/api/releases/")
        m_post = self._match_id(path, "/api/posts/")
        m_coop = self._match_id(path, "/api/coops/")

        # ---- 版本下发文件（管理员） ----
        if m_rel and m_rel[1] == "files":
            if not self._authed():
                self._fail("请先登录", 401)
                return
            fp = self._save_upload_body(qs)
            if not isinstance(fp, str):
                return
            try:
                r = save_file(m_rel[0], clean_rel((qs.get("rel") or [""])[0]), fp,
                              int(self.headers.get("Content-Length") or 0))
            finally:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            self._json(r, 200 if r.get("ok") else 400)
            return

        # ---- 帖子附件（用户） ----
        if m_post and m_post[1] == "file":
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            conn = connect()
            try:
                r0 = post_row(conn, m_post[0])
                if not r0:
                    self._fail("帖子不存在", 404)
                    return
                if r0["author_uid"] != u["uid"] and not self._authed():
                    self._fail("只能给自己的帖子传附件", 403)
                    return
            finally:
                conn.close()
            fp = self._save_upload_body(qs)
            if not isinstance(fp, str):
                return
            try:
                up = register_upload(fp, (qs.get("name") or [""])[0], u["uid"])
            finally:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            if not up.get("ok"):
                self._json(up, 400)
                return
            with _db_lock:
                conn = connect()
                try:
                    conn.execute("UPDATE posts SET file_id = ?, updated_at = ? WHERE id = ?",
                                 (up["id"], now_str(), m_post[0]))
                    conn.commit()
                finally:
                    conn.close()
            self._json(up)
            return

        # ---- 合作文件（仅加入者） ----
        if m_coop and m_coop[1] == "file":
            u = self._user()
            if not u:
                self._fail("请先设置昵称", 401)
                return
            conn = connect()
            try:
                r0 = conn.execute("SELECT * FROM coops WHERE id = ? AND deleted = 0",
                                  (m_coop[0],)).fetchone()
                if not r0:
                    self._fail("合作不存在", 404)
                    return
                if not coop_member(conn, m_coop[0], u["uid"]) and not self._authed():
                    self._fail("加入合作后才能上传文件", 403)
                    return
            finally:
                conn.close()
            fp = self._save_upload_body(qs)
            if not isinstance(fp, str):
                return
            try:
                up = register_upload(fp, (qs.get("name") or [""])[0], u["uid"])
            finally:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            if not up.get("ok"):
                self._json(up, 400)
                return
            with _db_lock:
                conn = connect()
                try:
                    conn.execute(
                        "INSERT INTO coop_files(coop_id, upload_id, author_uid, "
                        "author_name, created_at) VALUES(?,?,?,?,?)",
                        (m_coop[0], up["id"], u["uid"], u["name"], now_str()))
                    conn.execute("UPDATE coops SET updated_at = ? WHERE id = ?",
                                 (now_str(), m_coop[0]))
                    conn.commit()
                finally:
                    conn.close()
            self._json(up)
            return

        self._fail("接口不存在：" + path, 404)

    def _save_upload_body(self, qs):
        """把请求体落成临时文件，返回临时路径；出错时自己已经响应了。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._fail("文件内容为空")
            return None
        tmpdir = os.path.join(DATA_DIR, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        tmp = os.path.join(tmpdir, uuid.uuid4().hex + ".part")
        remain = length
        try:
            with open(tmp, "wb") as f:
                while remain > 0:
                    chunk = self.rfile.read(min(1 << 20, remain))
                    if not chunk:
                        break
                    f.write(chunk)
                    remain -= len(chunk)
        except Exception as exc:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            self._fail("接收文件失败：%s" % exc, 500)
            return None
        if remain > 0:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self._fail("文件传输中断", 400)
            return None
        return tmp


def register_upload(tmp_path: str, name: str, owner_uid: str) -> dict:
    name = clean_rel(strval(name, 200)) or ("file-" + uuid.uuid4().hex[:8])
    stored = uuid.uuid4().hex + ".bin"
    dst = os.path.join(FILE_DIR, stored)
    try:
        shutil.move(tmp_path, dst)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": "保存文件失败：%s" % exc}
    size = os.path.getsize(dst)
    with _db_lock:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT INTO uploads(stored, name, size, owner_uid, created_at) "
                "VALUES(?,?,?,?,?)", (stored, name, size, owner_uid, now_str()))
            conn.commit()
            return {"ok": True, "id": int(cur.lastrowid), "name": name, "size": size}
        finally:
            conn.close()


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="脆冰启动器 网站服务端（更新 + 社区）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8756)
    p.add_argument("--open", action="store_true", help="启动后自动打开后台")
    return p.parse_args(argv)


def load_ui() -> str:
    """同目录下若有 update_ui.html 就以它为准（改界面不用重新打包）。"""
    ext = os.path.join(ROOT, "update_ui.html")
    try:
        if os.path.isfile(ext):
            return open(ext, "r", encoding="utf-8").read()
    except OSError:
        pass
    return ADMIN_HTML


def load_site() -> str:
    """同目录下若有 site_ui.html 就以它为准（社区网站界面）。"""
    ext = os.path.join(ROOT, "site_ui.html")
    try:
        if os.path.isfile(ext):
            return open(ext, "r", encoding="utf-8").read()
    except OSError:
        pass
    return SITE_HTML


def main(argv=None) -> int:
    args = parse_args(argv)
    ensure_dirs()
    init_db()

    global ADMIN_HTML, SITE_HTML
    ADMIN_HTML = load_ui()
    SITE_HTML = load_site()

    global ADMIN_SALT, ADMIN_HASH
    ensure_admin_pwd()

    try:
        srv = make_server(args.host, args.port)
    except OSError as exc:
        print("启动失败：%s" % exc)
        print("换个端口试试，例如：python update_server.py --port 8757")
        return 1

    host_show = args.host if args.host not in ("0.0.0.0", "") else "127.0.0.1"
    print("=" * 46)
    print("  %s %s" % (APP_NAME, APP_VERSION))
    print("=" * 46)
    print("网站地址   : http://%s:%d" % (host_show, srv.server_address[1]))
    print("管理后台   : http://%s:%d/admin" % (host_show, srv.server_address[1]))
    print("清单地址   : http://%s:%d/api/manifest" % (host_show, srv.server_address[1]))
    if os.environ.get("CBUPDATE_TOKEN"):
        print("管理口令   : 来自环境变量 CBUPDATE_TOKEN")
    elif ADMIN_PWD_ONCE:
        print("管理口令   : %s（随机生成，只显示这一次，已写入 %s，可在网站里修改）"
              % (ADMIN_PWD_ONCE, TOKEN_FILE))
    else:
        print("管理口令   : 已保存在数据目录（哈希），忘了就删掉 update_data 里的数据库重新生成")
    print("数据目录   : %s" % DATA_DIR)
    print("-" * 46)
    print("把上面的「清单地址」填进启动器：设置 → 更新 → 更新地址")
    print("把「网站地址」填进启动器：设置 → 网站 → 网站服务端地址")
    print("服务端没开也没关系，启动器会当作没有更新。")
    print("按 Ctrl+C 停止。")
    print("=" * 46)
    sys.stdout.flush()

    url = "http://%s:%d/" % (host_show, srv.server_address[1])
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
    return 0


SITE_HTML = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
             "<title>脆冰网站</title></head><body>"
             "<p>缺少 site_ui.html，请把它放到 update_server.py 同目录。</p></body></html>")

ADMIN_HTML = "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n<title>\u8106\u51b0\u66f4\u65b0\u670d\u52a1\u7aef</title>\n<link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%232f8fc4'/%3E%3C/svg%3E\">\n<style>\n  :root{\n    --accent:#2f8fc4; --accent-2:#8fd8ff; --ice-1:#dff1fb; --ice-2:#bfe4f7;\n    --bg:#f2f8fc; --panel:#ffffff; --deep:#14384f; --text:#173a4f;\n    --muted:#6b8798; --line:rgba(47,143,196,.18); --radius:12px;\n    --shadow:0 6px 20px rgba(20,60,90,.10);\n  }\n  *{box-sizing:border-box}\n  body{margin:0;background:var(--bg);color:var(--text);\n    font:14px/1.6 \"Microsoft YaHei\",\"PingFang SC\",system-ui,sans-serif}\n  .wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}\n  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);\n    box-shadow:var(--shadow)}\n  h1,h2,h3{margin:0}\n  .top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}\n  .logo{width:40px;height:40px;border-radius:11px;background:linear-gradient(135deg,var(--accent-2),var(--accent));\n    display:grid;place-items:center;color:#fff;font-weight:800;font-size:17px}\n  .top h1{font-size:19px}\n  .sub{color:var(--muted);font-size:12.5px}\n  .mv{margin-left:auto;text-align:right}\n  .mv code{background:var(--ice-1);padding:5px 9px;border-radius:8px;font-size:12.5px;\n    display:inline-block;color:var(--deep);word-break:break-all}\n  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}\n  .grow{flex:1;min-width:180px}\n  input,textarea,select{width:100%;padding:9px 11px;border:1px solid var(--line);\n    border-radius:9px;background:#fff;color:var(--text);font:inherit;outline:none}\n  input:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,143,196,.12)}\n  textarea{resize:vertical;min-height:100px;line-height:1.7}\n  label{display:block;font-size:12.5px;color:var(--muted);margin:10px 0 4px}\n  button{cursor:pointer;border:1px solid transparent;border-radius:9px;padding:8px 14px;\n    font:inherit;background:var(--accent);color:#fff;transition:.15s}\n  button:hover{filter:brightness(1.06)}\n  button.ghost{background:#fff;border-color:var(--line);color:var(--text)}\n  button.ghost:hover{border-color:var(--accent);color:var(--accent)}\n  button.danger{background:#e05a52}\n  button.sm{padding:5px 10px;font-size:12.5px;border-radius:8px}\n  button:disabled{opacity:.55;cursor:not-allowed}\n  .grid{display:grid;grid-template-columns:322px 1fr;gap:16px;align-items:start}\n  @media(max-width:900px){.grid{grid-template-columns:1fr}}\n  .pad{padding:16px}\n  .list{max-height:70vh;overflow:auto}\n  .item{padding:11px 13px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;\n    cursor:pointer;background:#fff;transition:.15s}\n  .item:hover{border-color:var(--accent-2);background:var(--ice-1)}\n  .item.on{border-color:var(--accent);background:var(--ice-1);box-shadow:inset 3px 0 0 var(--accent)}\n  .item .v{font-weight:700}\n  .item .t{color:var(--muted);font-size:12.5px}\n  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;margin-left:6px;\n    background:var(--ice-2);color:var(--deep)}\n  .tag.pub{background:var(--accent);color:#fff}\n  .files{border-top:1px dashed var(--line);margin-top:14px;padding-top:12px}\n  .drop{border:2px dashed var(--line);border-radius:11px;padding:16px;text-align:center;\n    color:var(--muted);background:#fbfdff}\n  .drop.over{border-color:var(--accent);background:var(--ice-1);color:var(--accent)}\n  .fl{margin-top:10px}\n  .fl div{display:flex;gap:8px;align-items:center;padding:6px 9px;border:1px solid var(--line);\n    border-radius:8px;margin-bottom:6px;font-size:12.5px;background:#fff}\n  .fl b{font-weight:600;word-break:break-all}\n  .fl span{margin-left:auto;color:var(--muted);white-space:nowrap}\n  pre{background:#0f2431;color:#cfe9fb;padding:13px;border-radius:10px;overflow:auto;\n    font-size:12.5px;line-height:1.65;max-height:300px;margin:10px 0 0}\n  .hint{color:var(--muted);font-size:12px;margin-top:6px}\n  .toast{position:fixed;left:50%;top:20px;transform:translateX(-50%);z-index:99;\n    background:var(--deep);color:#fff;padding:10px 18px;border-radius:10px;opacity:0;\n    transition:.25s;pointer-events:none;font-size:13px}\n  .toast.on{opacity:1;top:26px}\n  .toast.bad{background:#c0413a}\n  .login{max-width:390px;margin:9vh auto;padding:24px}\n  .login h2{font-size:17px;margin-bottom:4px}\n  .mt{margin-top:14px}\n  .kv{display:flex;gap:6px;align-items:center;margin-top:8px}\n  .kv input{flex:1}\n  .sec{font-size:13.5px;font-weight:700;margin:16px 0 2px;color:var(--deep)}\n</style>\n</head>\n<body>\n<div class=\"wrap\" id=\"app\" hidden>\n  <div class=\"top\">\n    <div class=\"logo\">\u51b0</div>\n    <div>\n      <h1>\u8106\u51b0\u66f4\u65b0\u670d\u52a1\u7aef <span class=\"sub\" id=\"ver\"></span></h1>\n      <div class=\"sub\">\u53d1\u5e03\u7248\u672c \u00b7 \u5199\u66f4\u65b0\u65e5\u5fd7 \u00b7 \u4e0a\u4f20\u6587\u4ef6 \u2014\u2014 \u542f\u52a8\u5668\u6bcf\u6b21\u542f\u52a8\u4f1a\u6765\u68c0\u67e5</div>\n    </div>\n    <div class=\"mv\">\n      <div class=\"sub\">\u6e05\u5355\u5730\u5740\uff08\u586b\u5230\u542f\u52a8\u5668\u300c\u8bbe\u7f6e \u2192 \u66f4\u65b0 \u2192 \u66f4\u65b0\u5730\u5740\u300d\uff09</div>\n      <code id=\"mfUrl\">-</code>\n      <div class=\"row\" style=\"justify-content:flex-end;margin-top:6px\">\n        <button class=\"ghost sm\" id=\"btnCopy\">\u590d\u5236\u5730\u5740</button>\n        <button class=\"ghost sm\" id=\"btnHealth\">\u8fde\u901a\u6d4b\u8bd5</button>\n        <button class=\"ghost sm\" id=\"btnLogout\">\u9000\u51fa</button>\n      </div>\n    </div>\n  </div>\n\n  <div class=\"grid\">\n    <div class=\"card pad\">\n      <div class=\"row\" style=\"margin-bottom:10px\">\n        <h3 style=\"font-size:14px\">\u7248\u672c\u5217\u8868</h3>\n        <button class=\"sm grow\" style=\"flex:0 0 auto;margin-left:auto\" id=\"btnNew\">+ \u65b0\u5efa\u7248\u672c</button>\n      </div>\n      <div class=\"list\" id=\"list\"></div>\n      <div class=\"hint\" id=\"curHint\"></div>\n    </div>\n\n    <div class=\"card pad\">\n      <h3 style=\"font-size:14px\" id=\"formTitle\">\u7f16\u8f91\u7248\u672c</h3>\n      <input type=\"hidden\" id=\"fId\">\n      <div class=\"row\">\n        <div class=\"grow\"><label>\u7248\u672c\u53f7\uff08\u8981\u6bd4\u542f\u52a8\u5668\u5f53\u524d\u7248\u672c\u5927\u624d\u4f1a\u63d0\u793a\u66f4\u65b0\uff09</label>\n          <input id=\"fVersion\" placeholder=\"\u4f8b\u5982 1.0.3\"></div>\n        <div class=\"grow\"><label>\u6807\u9898</label>\n          <input id=\"fTitle\" placeholder=\"\u4f8b\u5982 \u542f\u52a8\u903b\u8f91\u4fee\u590d\"></div>\n      </div>\n      <label>\u66f4\u65b0\u65e5\u5fd7\uff08\u4e00\u884c\u4e00\u6761\uff0c\u4f1a\u81ea\u52a8\u62fc\u8fdb\u8bf4\u660e\u91cc\uff09</label>\n      <textarea id=\"fItems\" placeholder=\"\u4fee\u590d\uff1a\u70b9\u542f\u52a8\u62a5\u9519&#10;\u65b0\u589e\uff1a\u67d0\u67d0\u529f\u80fd\"></textarea>\n      <label>\u8be6\u7ec6\u8bf4\u660e\uff08\u53ef\u9009\uff0c\u7eaf\u6587\u672c\uff09</label>\n      <textarea id=\"fNotes\" style=\"min-height:76px\" placeholder=\"\u8fd9\u6b21\u66f4\u65b0\u60f3\u5bf9\u7528\u6237\u8bf4\u7684\u8bdd\"></textarea>\n      <label>\u53d1\u5e03\u9875\u94fe\u63a5\uff08\u53ef\u9009\uff0c\u70b9\u4e86\u300c\u4e0b\u8f7d\u65b0\u7248\u672c\u300d\u4f1a\u6253\u5f00\u5b83\uff09</label>\n      <input id=\"fUrl\" placeholder=\"https://...\">\n\n      <div class=\"files\">\n        <div class=\"sec\">\u8981\u4e0b\u53d1\u7684\u6587\u4ef6</div>\n        <div class=\"hint\">\u8def\u5f84 = \u6587\u4ef6\u5728\u542f\u52a8\u5668\u76ee\u5f55\u4e0b\u7684\u4f4d\u7f6e\uff0c\u4f8b\u5982 <code>main.py</code>\u3001<code>ui/index.html</code>\u3002\n          \u670d\u52a1\u7aef\u5173\u95ed\u4e5f\u4e0d\u4f1a\u5f71\u54cd\u542f\u52a8\u5668\u8fd0\u884c\uff0c\u6240\u4ee5\u8fd9\u91cc\u53ea\u653e\u771f\u7684\u9700\u8981\u66f4\u65b0\u7684\u6587\u4ef6\u3002</div>\n        <div class=\"row mt\">\n          <input class=\"grow\" id=\"fRel\" placeholder=\"main.py\">\n          <input type=\"file\" id=\"fPick\" class=\"grow\" style=\"flex:0 0 210px;padding:6px\">\n          <button class=\"sm\" id=\"btnUpload\">\u4e0a\u4f20</button>\n        </div>\n        <div class=\"drop mt\" id=\"drop\">\u628a\u6587\u4ef6\u62d6\u5230\u8fd9\u91cc\u4e5f\u80fd\u4e0a\u4f20\uff08\u81ea\u52a8\u7528\u6587\u4ef6\u540d\u5f53\u8def\u5f84\uff09</div>\n        <div class=\"fl\" id=\"fl\"></div>\n      </div>\n\n      <div class=\"row mt\">\n        <button id=\"btnSave\">\u4fdd\u5b58</button>\n        <button class=\"ghost\" id=\"btnPublish\">\u8bbe\u4e3a\u6700\u65b0\u7248\uff08\u53d1\u5e03\uff09</button>\n        <button class=\"ghost\" id=\"btnUnpublish\">\u53d6\u6d88\u53d1\u5e03</button>\n        <button class=\"danger\" id=\"btnDelete\" style=\"margin-left:auto\">\u5220\u9664\u7248\u672c</button>\n      </div>\n      <div class=\"sec mt\">\u6e05\u5355\u9884\u89c8\uff08\u542f\u52a8\u5668\u770b\u5230\u7684\u5c31\u662f\u8fd9\u4e2a\uff09</div>\n      <pre id=\"mfPreview\">{}</pre>\n      <label style=\"margin-top:14px\">\u5bf9\u5916\u5730\u5740\u524d\u7f00\uff08\u90e8\u7f72\u5230\u670d\u52a1\u5668\u65f6\u586b\uff0c\u4f8b\u5982 http://1.2.3.4:8756\uff1b\u7559\u7a7a\u6309\u8bbf\u95ee\u5730\u5740\u81ea\u52a8\u751f\u6210\uff09</label>\n      <div class=\"kv\"><input id=\"fBase\" placeholder=\"\u7559\u7a7a = \u81ea\u52a8\"><button class=\"ghost sm\" id=\"btnBase\">\u4fdd\u5b58</button></div>\n    </div>\n  </div>\n</div>\n\n<div class=\"card pad login\" id=\"login\">\n  <h2>\u8106\u51b0\u66f4\u65b0\u670d\u52a1\u7aef</h2>\n  <div class=\"sub\">\u8bf7\u8f93\u5165\u7ba1\u7406\u53e3\u4ee4\uff08\u670d\u52a1\u7aef\u542f\u52a8\u65f6\u4f1a\u5728\u7a97\u53e3\u91cc\u663e\u793a\uff0c\u4e5f\u5b58\u5728 update_data/token.txt\uff09</div>\n  <label class=\"mt\">\u7ba1\u7406\u53e3\u4ee4</label>\n  <input id=\"inToken\" type=\"password\" placeholder=\"\u53e3\u4ee4\" autocomplete=\"off\">\n  <button class=\"mt\" style=\"width:100%\" id=\"btnLogin\">\u8fdb\u5165\u540e\u53f0</button>\n  <div class=\"hint\" id=\"lgHint\"></div>\n</div>\n\n<div class=\"toast\" id=\"toast\"></div>\n\n<script>\n(function () {\n  \"use strict\";\n  var TOKEN = localStorage.getItem(\"cbupd_token\") || \"\";\n  var DATA = { list: [], cur: \"\", base: \"\" };\n  var sel = null;\n  var $ = function (s) { return document.querySelector(s); };\n  var errs = [];\n\n  function toast(msg, bad) {\n    var t = $(\"#toast\");\n    t.textContent = msg;\n    t.className = \"toast on\" + (bad ? \" bad\" : \"\");\n    setTimeout(function () { t.className = \"toast\" + (bad ? \" bad\" : \"\"); }, 2400);\n  }\n  function api(path, opt) {\n    opt = opt || {};\n    var h = { \"Content-Type\": \"application/json\" };\n    if (TOKEN) h[\"X-Token\"] = TOKEN;\n    return fetch(path, {\n      method: opt.method || (opt.body ? \"POST\" : \"GET\"),\n      headers: opt.headers || h,\n      body: opt.body\n    }).then(function (r) {\n      return r.json().catch(function () { return { ok: false, msg: \"HTTP \" + r.status }; });\n    });\n  }\n  function esc(s) {\n    return String(s == null ? \"\" : s).replace(/[&<>\"]/g, function (c) {\n      return { \"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\" }[c];\n    });\n  }\n  function fsize(n) {\n    n = n || 0;\n    if (n < 1024) return n + \" B\";\n    if (n < 1048576) return (n / 1024).toFixed(1) + \" KB\";\n    return (n / 1048576).toFixed(1) + \" MB\";\n  }\n\n  // ---------- \u767b\u5f55 ----------\n  function tryEnter() {\n    return api(\"/api/state\").then(function (d) {\n      if (d.ok && d.logged) { enter(d); return true; }\n      return false;\n    }).catch(function () { return false; });\n  }\n  function enter(st) {\n    $(\"#login\").hidden = true;\n    $(\"#app\").hidden = false;\n    $(\"#mfUrl\").textContent = location.origin + \"/api/manifest\";\n    $(\"#fBase\").value = (st && st.base) || \"\";\n    DATA.base = (st && st.base) || \"\";\n    reload();\n  }\n  $(\"#btnLogin\").addEventListener(\"click\", function () {\n    var t = $(\"#inToken\").value.trim();\n    if (!t) { $(\"#lgHint\").textContent = \"\u8bf7\u8f93\u5165\u53e3\u4ee4\"; return; }\n    TOKEN = t;\n    api(\"/api/login\", { body: JSON.stringify({ token: t }) }).then(function (r) {\n      if (!r.ok) { $(\"#lgHint\").textContent = r.msg || \"\u53e3\u4ee4\u4e0d\u5bf9\"; TOKEN = \"\"; return; }\n      localStorage.setItem(\"cbupd_token\", t);\n      $(\"#lgHint\").textContent = \"\";\n      enter(null);\n    }).catch(function (e) { $(\"#lgHint\").textContent = \"\u8fde\u4e0d\u4e0a\u670d\u52a1\uff1a\" + e; });\n  });\n  $(\"#inToken\").addEventListener(\"keydown\", function (e) {\n    if (e.key === \"Enter\") $(\"#btnLogin\").click();\n  });\n  $(\"#btnLogout\").addEventListener(\"click\", function () {\n    localStorage.removeItem(\"cbupd_token\");\n    location.reload();\n  });\n\n  // ---------- \u5217\u8868 ----------\n  function reload(keepSel) {\n    return api(\"/api/releases\").then(function (d) {\n      if (!d.ok) { toast(d.msg || \"\u8bfb\u53d6\u5931\u8d25\", true); return; }\n      DATA.list = d.list || [];\n      renderList();\n      var id = keepSel ? sel : null;\n      if (id == null) {\n        if (DATA.list.length) id = DATA.list[0].id; else id = 0;\n      }\n      select(id);\n    }).catch(function (e) { toast(\"\u8bfb\u53d6\u5931\u8d25\uff1a\" + e, true); });\n  }\n  function renderList() {\n    var box = $(\"#list\");\n    box.innerHTML = \"\";\n    if (!DATA.list.length) {\n      box.innerHTML = '<div class=\"hint\">\u8fd8\u6ca1\u6709\u7248\u672c\uff0c\u70b9\u53f3\u4e0a\u89d2\u300c+ \u65b0\u5efa\u7248\u672c\u300d\u3002</div>';\n      $(\"#curHint\").textContent = \"\u5f53\u524d\u6ca1\u6709\u53d1\u5e03\u4efb\u4f55\u7248\u672c\uff0c\u542f\u52a8\u5668\u68c0\u67e5\u66f4\u65b0\u65f6\u4e0d\u4f1a\u63d0\u793a\u3002\";\n      return;\n    }\n    DATA.list.forEach(function (r) {\n      var d = document.createElement(\"div\");\n      d.className = \"item\" + (r.id === sel ? \" on\" : \"\");\n      d.setAttribute(\"data-id\", r.id);\n      d.innerHTML = '<div class=\"v\">' + esc(r.version) +\n        (r.isCurrent ? '<span class=\"tag pub\">\u6700\u65b0\u7248</span>' :\n          (r.published ? '<span class=\"tag\">\u5df2\u53d1\u5e03</span>' : '')) + '</div>' +\n        '<div class=\"t\">' + esc(r.title || \"\uff08\u65e0\u6807\u9898\uff09\") + '</div>' +\n        '<div class=\"t\">' + (r.fileCount ? (r.fileCount + \" \u4e2a\u6587\u4ef6 \u00b7 \" + fsize(r.totalSize)) : \"\u65e0\u6587\u4ef6\") +\n        ' \u00b7 ' + esc(r.createdAt) + '</div>';\n      d.addEventListener(\"click\", function () { select(r.id); });\n      box.appendChild(d);\n    });\n    var cur = DATA.list.filter(function (r) { return r.isCurrent; })[0];\n    $(\"#curHint\").textContent = cur\n      ? (\"\u542f\u52a8\u5668\u73b0\u5728\u4f1a\u68c0\u67e5\u5230\u7248\u672c \" + cur.version)\n      : \"\u8fd8\u6ca1\u6709\u8bbe\u4e3a\u6700\u65b0\u7248\uff0c\u542f\u52a8\u5668\u4e0d\u4f1a\u63d0\u793a\u66f4\u65b0\u3002\";\n  }\n  function select(id) {\n    sel = id;\n    renderList();\n    var r = DATA.list.filter(function (x) { return x.id === id; })[0];\n    if (!r) {\n      $(\"#fId\").value = \"\"; $(\"#fVersion\").value = \"\"; $(\"#fTitle\").value = \"\";\n      $(\"#fItems\").value = \"\"; $(\"#fNotes\").value = \"\"; $(\"#fUrl\").value = \"\";\n      $(\"#fl\").innerHTML = \"\"; $(\"#mfPreview\").textContent = \"{}\";\n      $(\"#formTitle\").textContent = \"\u65b0\u5efa\u7248\u672c\";\n      return;\n    }\n    $(\"#formTitle\").textContent = \"\u7f16\u8f91\u7248\u672c \u00b7 \" + r.version;\n    $(\"#fId\").value = r.id;\n    $(\"#fVersion\").value = r.version;\n    $(\"#fTitle\").value = r.title;\n    $(\"#fItems\").value = (r.items || []).join(\"\\n\");\n    $(\"#fNotes\").value = r.notes;\n    $(\"#fUrl\").value = r.url;\n    renderFiles(r);\n    preview();\n  }\n  function renderFiles(r) {\n    var box = $(\"#fl\");\n    box.innerHTML = \"\";\n    if (!r.files || !r.files.length) {\n      box.innerHTML = '<div class=\"hint\">\u8fd8\u6ca1\u6709\u4e0a\u4f20\u6587\u4ef6\u3002\u6ca1\u6709\u6587\u4ef6\u65f6\u53ea\u63d0\u793a\u6709\u66f4\u65b0\uff0c\u4e0d\u4f1a\u81ea\u52a8\u4e0b\u8f7d\u66ff\u6362\u3002</div>';\n      return;\n    }\n    r.files.forEach(function (f) {\n      var d = document.createElement(\"div\");\n      d.innerHTML = '<b>' + esc(f.rel) + '</b><span>' + fsize(f.size) + '</span>';\n      var del = document.createElement(\"button\");\n      del.className = \"ghost sm\";\n      del.textContent = \"\u5220\u9664\";\n      del.addEventListener(\"click\", function () {\n        api(\"/api/releases/\" + r.id + \"/files/remove\",\n          { body: JSON.stringify({ fileId: f.id }) }).then(function (res) {\n            if (!res.ok) { toast(res.msg || \"\u5220\u9664\u5931\u8d25\", true); return; }\n            toast(\"\u5df2\u5220\u9664\");\n            reload(true);\n          });\n      });\n      d.appendChild(del);\n      box.appendChild(d);\n    });\n  }\n\n  // ---------- \u7f16\u8f91\u52a8\u4f5c ----------\n  function body() {\n    return {\n      version: $(\"#fVersion\").value.trim(),\n      title: $(\"#fTitle\").value.trim(),\n      notes: $(\"#fNotes\").value,\n      url: $(\"#fUrl\").value.trim(),\n      items: $(\"#fItems\").value.split(\"\\n\").map(function (s) { return s.trim(); })\n        .filter(function (s) { return !!s; })\n    };\n  }\n  $(\"#btnNew\").addEventListener(\"click\", function () {\n    var v = prompt(\"\u65b0\u7248\u672c\u53f7\uff08\u8981\u6bd4\u542f\u52a8\u5668\u5f53\u524d\u7248\u672c\u5927\uff09\", \"\");\n    if (!v) return;\n    api(\"/api/releases\", { body: JSON.stringify({ version: v.trim() }) }).then(function (r) {\n      if (!r.ok) { toast(r.msg || \"\u65b0\u5efa\u5931\u8d25\", true); return; }\n      toast(\"\u5df2\u65b0\u5efa\");\n      reload().then(function () { select(r.id); });\n    });\n  });\n  $(\"#btnSave\").addEventListener(\"click\", function () {\n    var b = body();\n    if (!b.version) { toast(\"\u7248\u672c\u53f7\u4e0d\u80fd\u4e3a\u7a7a\", true); return; }\n    var id = $(\"#fId\").value;\n    api(id ? (\"/api/releases/\" + id) : \"/api/releases\",\n      { body: JSON.stringify(b) }).then(function (r) {\n        if (!r.ok) { toast(r.msg || \"\u4fdd\u5b58\u5931\u8d25\", true); return; }\n        toast(\"\u5df2\u4fdd\u5b58\");\n        reload().then(function () { select(r.id); });\n      });\n  });\n  $(\"#btnPublish\").addEventListener(\"click\", function () {\n    var id = $(\"#fId\").value;\n    if (!id) { toast(\"\u5148\u9009\u4e00\u4e2a\u7248\u672c\", true); return; }\n    api(\"/api/releases/\" + id + \"/publish\", { body: \"{}\" }).then(function (r) {\n      toast(r.ok ? \"\u5df2\u8bbe\u4e3a\u6700\u65b0\u7248\" : (r.msg || \"\u53d1\u5e03\u5931\u8d25\"), !r.ok);\n      if (r.ok) reload(true);\n    });\n  });\n  $(\"#btnUnpublish\").addEventListener(\"click\", function () {\n    var id = $(\"#fId\").value;\n    if (!id) return;\n    api(\"/api/releases/\" + id + \"/unpublish\", { body: \"{}\" }).then(function (r) {\n      toast(r.ok ? \"\u5df2\u53d6\u6d88\u53d1\u5e03\" : (r.msg || \"\u5931\u8d25\"), !r.ok);\n      if (r.ok) reload(true);\n    });\n  });\n  $(\"#btnDelete\").addEventListener(\"click\", function () {\n    var id = $(\"#fId\").value;\n    if (!id) return;\n    if (!confirm(\"\u5220\u9664\u8fd9\u4e2a\u7248\u672c\u548c\u5b83\u7684\u6587\u4ef6\uff1f\u6b64\u64cd\u4f5c\u4e0d\u53ef\u6062\u590d\u3002\")) return;\n    api(\"/api/releases/\" + id + \"/delete\", { body: \"{}\" }).then(function (r) {\n      toast(r.ok ? \"\u5df2\u5220\u9664\" : (r.msg || \"\u5220\u9664\u5931\u8d25\"), !r.ok);\n      if (r.ok) reload();\n    });\n  });\n  $(\"#btnBase\").addEventListener(\"click\", function () {\n    api(\"/api/config\", { body: JSON.stringify({ baseUrl: $(\"#fBase\").value.trim() }) })\n      .then(function (r) {\n        toast(r.ok ? \"\u5df2\u4fdd\u5b58\" : (r.msg || \"\u4fdd\u5b58\u5931\u8d25\"), !r.ok);\n        if (r.ok) preview();\n      });\n  });\n  $(\"#btnCopy\").addEventListener(\"click\", function () {\n    var url = location.origin + \"/api/manifest\";\n    try {\n      navigator.clipboard.writeText(url);\n      toast(\"\u5df2\u590d\u5236\u6e05\u5355\u5730\u5740\");\n    } catch (e) {\n      prompt(\"\u624b\u52a8\u590d\u5236\uff1a\", url);\n    }\n  });\n  $(\"#btnHealth\").addEventListener(\"click\", function () {\n    api(\"/api/health\").then(function (r) { toast(r.ok ? \"\u670d\u52a1\u6b63\u5e38\" : \"\u5f02\u5e38\", !r.ok); });\n  });\n\n  // ---------- \u4e0a\u4f20 ----------\n  function upload(file, rel) {\n    var id = $(\"#fId\").value;\n    if (!id) { toast(\"\u5148\u9009\u4e00\u4e2a\u7248\u672c\", true); return; }\n    rel = (rel || $(\"#fRel\").value.trim() || file.name).trim();\n    if (!rel) { toast(\"\u8bf7\u586b\u6587\u4ef6\u8def\u5f84\", true); return; }\n    if (rel.replace(/\\\\/g, \"/\").indexOf(\"..\") >= 0) { toast(\"\u8def\u5f84\u4e0d\u80fd\u542b ..\", true); return; }\n    $(\"#btnUpload\").disabled = true;\n    fetch(\"/api/releases/\" + id + \"/files?rel=\" + encodeURIComponent(rel), {\n      method: \"PUT\",\n      headers: { \"X-Token\": TOKEN, \"Content-Type\": \"application/octet-stream\" },\n      body: file\n    }).then(function (r) {\n      return r.json().catch(function () { return { ok: false, msg: \"HTTP \" + r.status }; });\n    }).then(function (r) {\n      $(\"#btnUpload\").disabled = false;\n      if (!r.ok) { toast(r.msg || \"\u4e0a\u4f20\u5931\u8d25\", true); return; }\n      toast(\"\u5df2\u4e0a\u4f20\uff1a\" + r.rel);\n      $(\"#fRel\").value = \"\";\n      reload(true);\n    }).catch(function (e) {\n      $(\"#btnUpload\").disabled = false;\n      toast(\"\u4e0a\u4f20\u5931\u8d25\uff1a\" + e, true);\n    });\n  }\n  $(\"#btnUpload\").addEventListener(\"click\", function () {\n    var f = $(\"#fPick\").files[0];\n    if (!f) { toast(\"\u5148\u9009\u4e00\u4e2a\u6587\u4ef6\", true); return; }\n    upload(f, $(\"#fRel\").value.trim() || f.name);\n    $(\"#fPick\").value = \"\";\n  });\n  var dz = $(\"#drop\");\n  [\"dragenter\", \"dragover\"].forEach(function (ev) {\n    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add(\"over\"); });\n  });\n  [\"dragleave\", \"drop\"].forEach(function (ev) {\n    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove(\"over\"); });\n  });\n  dz.addEventListener(\"drop\", function (e) {\n    if (!e.dataTransfer.files.length) return;\n    upload(e.dataTransfer.files[0], \"\");\n  });\n\n  // ---------- \u9884\u89c8 ----------\n  function preview() {\n    var id = $(\"#fId\").value;\n    if (!id) { $(\"#mfPreview\").textContent = \"{}\"; return; }\n    fetch(\"/api/releases/\" + id + \"/manifest\", { headers: { \"X-Token\": TOKEN } })\n      .then(function (r) { return r.json(); })\n      .then(function (d) {\n        $(\"#mfPreview\").textContent = d.ok\n          ? JSON.stringify(d.manifest, null, 2)\n          : (d.msg || \"\u9884\u89c8\u5931\u8d25\");\n      })\n      .catch(function (e) { $(\"#mfPreview\").textContent = \"\u9884\u89c8\u5931\u8d25\uff1a\" + e; });\n  }\n  [\"fVersion\", \"fTitle\", \"fItems\", \"fNotes\", \"fUrl\"].forEach(function (k) {\n    $(\"#\" + k).addEventListener(\"change\", preview);\n  });\n\n  window.addEventListener(\"error\", function (e) { errs.push(String(e.message)); });\n  tryEnter();\n})();\n</script>\n</body>\n</html>\n"


if __name__ == "__main__":
    sys.exit(main())
