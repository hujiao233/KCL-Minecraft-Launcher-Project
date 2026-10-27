# -*- coding: utf-8 -*-
"""脆冰更新服务端 - 发布版本与文件的增删改查。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import shutil
import uuid
import os

from upd_config import FILE_DIR, _db_lock, now_str, strval
from upd_db import connect, meta_get




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
