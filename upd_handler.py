# -*- coding: utf-8 -*-
"""脆冰更新服务端 - HTTP 请求处理。"""

from __future__ import annotations

from urllib.parse import parse_qs, quote, unquote, urlparse
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from upd_config import APP_NAME, APP_VERSION, COOKIE_NAME, DATA_DIR, FILE_DIR, POST_KINDS, SESSION_TTL, _db_lock, now_str, strval
from upd_db import connect, meta_get, meta_set
from upd_auth import _store_pwd, check_admin_pwd, cookie_of, drop_session, new_session, self_authed, session_get, user_auth, user_login
from upd_releases import clean_rel, create_release, current_release, delete_release, file_by_id, guess_base_url, list_releases, manifest_of, publish_release, register_upload, remove_file, save_file, update_release
from upd_posts import add_comment, create_post, list_comments, list_posts, post_row, post_summary, toggle_like
from upd_servers import coop_member, coop_summary, decide_join, launcher_joinlist, request_join, save_server, search_servers, server_announces, server_summary
import upd_templates




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
            self._html(upd_templates.SITE_HTML)
            return
        if path == "/admin":
            self._html(upd_templates.ADMIN_HTML)
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
