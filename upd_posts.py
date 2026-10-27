# -*- coding: utf-8 -*-
"""脆冰更新服务端 - 社区帖子、评论与点赞。"""

from __future__ import annotations

from upd_config import _db_lock, now_str, strval
from upd_db import connect




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
