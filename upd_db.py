# -*- coding: utf-8 -*-
"""脆冰更新服务端 - 数据库连接与表结构初始化。"""

from __future__ import annotations

import sqlite3
import os

from upd_config import DATA_DIR, DB_PATH, FILE_DIR, _db_lock



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
