# -*- coding: utf-8 -*-
"""
脆冰启动器 - 本地 API 服务
前端通过 HTTP 调用这些接口，后端负责文件操作、下载、启动进程。
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
from typing import Any, Dict, List

import requests
from flask import Flask, jsonify, make_response, request, send_from_directory
from werkzeug.exceptions import HTTPException

import game
import core
import mods
import version as vmod
import world

ROOT = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(ROOT, "ui")

app = Flask(__name__, static_folder=None)


@app.after_request
def _allow_web_end(resp):
    """允许「启动器网页端.html」这种直接用浏览器打开的页面跨域调用本机接口。"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Uid, X-Secret"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


def ok(data: Any = None, **extra):
    payload = {"ok": True, "data": data}
    payload.update(extra)
    return jsonify(payload)


def err(msg: str, code: int = 400, **extra):
    payload = {"ok": False, "msg": msg}
    payload.update(extra)
    return jsonify(payload), code


# ------------------------------------------------------------------
# /api/* 一律返回 JSON：否则前端拿到 HTML / 500 页面时会显示"响应解析失败"
# ------------------------------------------------------------------

@app.errorhandler(HTTPException)
def _on_http_error(e):
    path = request.path or "/"
    if path.startswith("/api/"):
        return err("接口 %s 不可用（HTTP %s %s）" % (path, e.code, e.name),
                   code=e.code or 500)
    return e


@app.errorhandler(Exception)
def _on_error(e):
    traceback.print_exc()
    try:
        sys.stderr.flush()
    except Exception:
        pass
    path = request.path or "/"
    if path.startswith("/api/"):
        return err("接口 %s 报错：%s: %s" % (path, type(e).__name__, e), code=500)
    return err("内部错误：%s" % e, code=500)


# ==================================================================
# 自检与自动补齐
# ==================================================================

# 启动器本体必须具备的文件（相对 CL 目录）
LAUNCHER_FILES = [
    "main.py", "core.py", "version.py", "game.py", "server.py", "mods.py",
    "world.py", "auth.py",
    "ui/index.html", "ui/assets/cuibing.png", "ui/assets/cuibing.ico",
]

# 需要逐个校验包完整性的内容目录
CONTENT_DIRS = ["mods", "resourcepacks", "shaderpacks"]
QUARANTINE = "_损坏文件"

_sc_lock = threading.Lock()
_selfcheck: Dict[str, Any] = {
    "running": False,
    "done": False,
    "progress": "",
    "startedAt": 0.0,
    "finishedAt": 0.0,
    "report": None,
}


def selfcheck_state() -> Dict[str, Any]:
    with _sc_lock:
        return dict(_selfcheck)


def _sc_progress(text: str):
    with _sc_lock:
        _selfcheck["progress"] = text


def _bad_archives(d: str) -> List[str]:
    """扫描目录，返回损坏 / 空 的 zip、jar 文件名列表。"""
    import zipfile
    out: List[str] = []
    if not os.path.isdir(d):
        return out
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith("_")]
        for fn in files:
            if not fn.lower().endswith((".jar", ".zip")):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, d)
            try:
                if os.path.getsize(p) == 0:
                    out.append(rel)
                    continue
                with zipfile.ZipFile(p) as z:
                    z.namelist()
            except Exception:  # noqa: BLE001
                out.append(rel)
    return out


def run_selfcheck(auto_repair: bool = True, check_content: bool = True) -> Dict[str, Any]:
    """
    全面体检：
      1. 启动器本体文件
      2. 配置文件
      3. Java 运行时
      4. 每个已安装版本的 jar / 运行库 / natives / 资源（缺失自动补下载）
      5. 每个版本的 mods、资源包、光影包（逐个校验包完整性，损坏的隔离）
      6. authlib-injector
    """
    with _sc_lock:
        if _selfcheck["running"]:
            return dict(_selfcheck)
        _selfcheck.update(running=True, done=False, progress="准备中…",
                          startedAt=time.time(), finishedAt=0.0)

    t0 = time.time()
    rep: Dict[str, Any] = {
        "ok": True, "messages": [], "launcher": {}, "config": {}, "java": {},
        "versions": [], "content": {}, "repaired": 0, "quarantined": [],
        "elapsed": 0.0, "autoRepair": auto_repair,
    }
    try:
        # ---------- 1. 启动器本体 ----------
        _sc_progress("检查启动器文件…")
        miss_files = []
        for f in LAUNCHER_FILES:
            p = os.path.join(ROOT, f)
            if not os.path.isfile(p) or os.path.getsize(p) == 0:
                miss_files.append(f)
        rep["launcher"] = {"ok": not miss_files, "missing": miss_files,
                           "total": len(LAUNCHER_FILES)}
        if miss_files:
            rep["ok"] = False
            rep["messages"].append(f"启动器缺少 {len(miss_files)} 个文件，请用引导器重新补齐")
        _log("自检：启动器文件 %d/%d" % (len(LAUNCHER_FILES) - len(miss_files), len(LAUNCHER_FILES)))

        # ---------- 2. 配置 ----------
        _sc_progress("检查配置…")
        try:
            cfg = core.load()
            rep["config"] = {"ok": True, "path": core.CONFIG_PATH}
        except Exception as e:  # noqa: BLE001
            try:
                bak = core.CONFIG_PATH + ".bak"
                if os.path.isfile(core.CONFIG_PATH):
                    shutil.copy2(core.CONFIG_PATH, bak)
                os.remove(core.CONFIG_PATH)
            except Exception:
                pass
            cfg = core.load()
            rep["config"] = {"ok": False, "path": core.CONFIG_PATH, "msg": f"配置损坏已重建（{e}）"}
            rep["messages"].append("配置文件损坏，已重建为默认设置")

        # ---------- 3. Java ----------
        _sc_progress("检查 Java…")
        try:
            jl = core.list_java()
            ji = core.resolve_java(cfg.get("javaPath") or "")
            rep["java"] = {"ok": bool(ji), "count": len(jl),
                           "path": (ji or {}).get("path", ""),
                           "version": (ji or {}).get("version", "")}
            if not ji:
                rep["ok"] = False
                rep["messages"].append("未检测到 Java，无法启动游戏")
        except Exception as e:  # noqa: BLE001
            rep["java"] = {"ok": False, "msg": str(e)}

        # ---------- 4. 已安装版本 ----------
        try:
            installed = vmod.list_installed()
        except Exception as e:  # noqa: BLE001
            installed = []
            rep["messages"].append(f"版本扫描失败：{e}")

        for i, v in enumerate(installed):
            vid = v["id"]
            _sc_progress(f"检查版本 {vid}（{i + 1}/{len(installed)}）…")
            entry = {"id": vid, "missing": [], "warnings": [], "repaired": 0, "ok": True}
            try:
                missing, warns = game.missing_files(vid, cfg, check_assets=True)
                entry["missing"] = missing[:20]
                entry["warnings"] = warns
                # 只有真正缺运行时文件 / 资源对象才需要联网补齐；
                # 「资源索引缺失」多见于远古版本，镜像无此文件，强补只会空等超时。
                need_assets = any(w.startswith("资源文件缺失") for w in warns)
                if missing:
                    entry["ok"] = False
                if missing or need_assets:
                    if auto_repair:
                        _sc_progress(f"补齐 {vid}（{len(missing)} 个文件）…")
                        task = core.tasks.create("repair", f"自动补齐 {vid}", vid)
                        try:
                            vmod.install_version(vid, task, with_assets=need_assets)
                        except Exception as e:  # noqa: BLE001
                            task.error = str(e)
                            task.state = core.TaskState.FAILED
                        m2, w2 = game.missing_files(vid, cfg, check_assets=True)
                        entry["repaired"] = max(0, len(missing) - len(m2))
                        entry["missing"] = m2[:20]
                        entry["warnings"] = w2
                        entry["ok"] = not m2
                        rep["repaired"] += entry["repaired"]
                        if entry["ok"]:
                            rep["messages"].append(f"{vid} 已补齐 {entry['repaired']} 个文件")
                        else:
                            rep["ok"] = False
                            rep["messages"].append(f"{vid} 仍有 {len(m2)} 个文件缺失")
                    else:
                        rep["ok"] = False
            except Exception as e:  # noqa: BLE001
                entry["ok"] = False
                entry["warnings"].append(f"检查失败：{e}")
                rep["ok"] = False
            rep["versions"].append(entry)

        # ---------- 5. 内容文件（mod / 资源包 / 光影）----------
        if check_content:
            _sc_progress("检查模组与资源包…")
            dirs_seen = set()
            bad_all: Dict[str, List[str]] = {}
            for v in installed:
                gd = core.effective_game_dir(v["id"], cfg)
                for sub in CONTENT_DIRS:
                    d = os.path.join(gd, sub)
                    key = os.path.normcase(d)
                    if key in dirs_seen or not os.path.isdir(d):
                        continue
                    dirs_seen.add(key)
                    bad = _bad_archives(d)
                    if bad:
                        bad_all[d] = bad
            # 全局游戏目录（未隔离时 mods 在这里）
            for sub in CONTENT_DIRS:
                d = os.path.join(core.game_dir(cfg), sub)
                key = os.path.normcase(d)
                if key in dirs_seen or not os.path.isdir(d):
                    continue
                dirs_seen.add(key)
                bad = _bad_archives(d)
                if bad:
                    bad_all[d] = bad

            for d, bad in bad_all.items():
                moved = []
                for rel in bad:
                    src = os.path.join(d, rel)
                    dst = os.path.join(d, QUARANTINE, rel.replace("\\", "_").replace("/", "_"))
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if os.path.isfile(dst):
                            os.remove(dst)
                        shutil.move(src, dst)
                        moved.append(rel)
                    except Exception:  # noqa: BLE001
                        pass
                rep["quarantined"].extend([os.path.join(d, m) for m in moved])
                if moved:
                    rep["messages"].append(f"{os.path.basename(d)}：{len(moved)} 个损坏文件已移入 {QUARANTINE}")

            total_bad = sum(len(v) for v in bad_all.values())
            rep["content"] = {"checked": len(dirs_seen), "broken": total_bad,
                              "quarantined": len(rep["quarantined"]),
                              "detail": {k: v[:20] for k, v in bad_all.items()}}

        # ---------- 6. authlib-injector ----------
        try:
            p = core.authlib_injector_path()
            if os.path.isfile(p):
                import zipfile
                try:
                    with zipfile.ZipFile(p) as z:
                        z.namelist()
                    rep["authlib"] = {"ok": True, "path": p}
                except Exception:  # noqa: BLE001
                    os.remove(p)
                    rep["authlib"] = {"ok": False, "path": p, "msg": "已删除损坏文件"}
                    rep["messages"].append("authlib-injector 损坏已清除，登录时会自动重新下载")
            else:
                rep["authlib"] = {"ok": True, "path": "", "msg": "未安装（用到外置登录时才需要）"}
        except Exception as e:  # noqa: BLE001
            rep["authlib"] = {"ok": False, "msg": str(e)}

    except Exception as e:  # noqa: BLE001
        rep["ok"] = False
        rep["messages"].append(f"自检异常：{e}")
    finally:
        rep["elapsed"] = round(time.time() - t0, 1)
        with _sc_lock:
            _selfcheck.update(running=False, done=True, progress="完成",
                              finishedAt=time.time(), report=rep)
        try:
            _save_report(rep)
        except Exception:
            pass
    return rep


def _log(msg: str):
    try:
        os.makedirs(core.LOGS_DIR, exist_ok=True)
        with open(os.path.join(core.LOGS_DIR, "selfcheck.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


def _save_report(rep: Dict[str, Any]):
    os.makedirs(core.ROOT_DIR, exist_ok=True)
    with open(os.path.join(core.ROOT_DIR, "selfcheck.json"), "w", encoding="utf-8") as f:
        import json
        json.dump(rep, f, ensure_ascii=False, indent=1)


def start_selfcheck_async(auto_repair: bool = True) -> bool:
    """后台线程跑体检，立即返回。"""
    with _sc_lock:
        if _selfcheck["running"]:
            return False

    def _run():
        try:
            run_selfcheck(auto_repair=auto_repair)
        except Exception as e:  # noqa: BLE001
            _log(f"自检线程异常：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return True


# ---------------- 系统与配置 ----------------

def total_memory_mb() -> int:
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return int(stat.ullTotalPhys / 1024 / 1024)
    except Exception:
        return 8192


@app.get("/api/hello")
def api_hello():
    cfg = core.load()
    mi = core.system_memory_info()
    return ok({
        "app": core.APP_NAME,
        "version": core.APP_VERSION,
        "gameDir": cfg.get("gameDir"),
        "totalMemory": mi["total"],
        "availableMemory": mi["available"],
        "memoryLoad": mi["load"],
        "platform": sys.platform,
        "source": vmod.get_source(cfg).get("name"),
    })


@app.get("/api/config")
def api_get_config():
    cfg = core.load()
    _mi = core.system_memory_info()
    cfg["_totalMemory"] = _mi["total"]
    cfg["_memory"] = _mi
    cfg["appVersion"] = core.APP_VERSION
    cfg["rootDir"] = core.ROOT_DIR
    cfg["_sources"] = [{"id": k, "name": v["name"]} for k, v in vmod.SOURCES.items()]
    cfg["_resourceSources"] = [{"id": "auto", "name": "自动选择（推荐）"}] + [
        {"id": k, "name": v["name"]} for k, v in mods.SOURCES.items()
    ]
    cfg["_cats"] = [{"id": k, "name": v} for k, v in vmod.CATS.items()]
    cfg["_kinds"] = [{"id": k, "name": v["name"]} for k, v in mods.KINDS.items()]
    return ok(cfg)


@app.post("/api/config")
def api_set_config():
    patch = request.get_json(force=True, silent=True) or {}
    allowed = {"gameDir", "gameDirSelected", "javaPath", "maxMemory", "windowWidth", "windowHeight",
               "fullscreen", "downloadSource", "maxDownloadThreads", "closeAfterLaunch",
               "jvmArgs", "lastVersion", "activeAccount",
               "isolateVersion", "authSource", "authServer", "authServerName",
               "authRegister", "authLabel", "uiScale",
               "authInjector", "resourceSource", "resourceMirror", "curseforgeKey",
               "language", "syncGameLang", "themePreset", "themeAccent",
               "titleAlign", "themePresets", "themeDark",
               "autoMemory", "autoServer", "updateCheck", "updateUrl",
               "offlineMode", "siteUrl", "uiLayout"}
    clean = {k: v for k, v in patch.items() if k in allowed}
    if "maxMemory" in clean:
        try:
            clean["maxMemory"] = max(512, int(clean["maxMemory"]))
        except Exception:
            clean.pop("maxMemory")
    if "gameDir" in clean and clean["gameDir"]:
        clean["gameDirSelected"] = True
    cfg = core.update(clean)
    if clean.get("gameDir"):
        try:
            os.makedirs(clean["gameDir"], exist_ok=True)
        except Exception:
            pass
    return ok(cfg)


# ---------------- 账户 ----------------

def _skin_ver(path: str) -> int:
    """皮肤文件的修改时间，前端用它做缓存失效：换皮肤后头像立刻跟着变。"""
    try:
        return int(os.path.getmtime(path))
    except Exception:
        return 0


@app.get("/api/accounts")
def api_accounts():
    cfg = core.load()
    accounts = cfg.get("accounts") or []
    for a in accounts:
        a["skinVer"] = _skin_ver(a.get("skinFile") or "")
    return ok({
        "accounts": accounts,
        "active": core.get_active(cfg),
        "activeName": (core.get_active(cfg) or {}).get("name", ""),
    })


@app.post("/api/accounts")
def api_add_account():
    body = request.get_json(force=True, silent=True) or {}
    r = core.add_account((body.get("name") or "").strip())
    if not r["ok"]:
        return err(r["msg"])
    return ok(r["account"])


@app.put("/api/accounts/<path:name>")
def api_rename_account(name):
    body = request.get_json(force=True, silent=True) or {}
    r = core.rename_account(name, (body.get("name") or "").strip())
    if not r["ok"]:
        return err(r["msg"])
    return ok(r["account"])


@app.delete("/api/accounts/<path:name>")
def api_del_account(name):
    r = core.remove_account(name)
    if not r["ok"]:
        return err(r["msg"])
    return ok(True)


@app.post("/api/accounts/active")
def api_active_account():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not core.find_account(name):
        return err(f"用户 {name} 不存在")
    core.update({"activeAccount": name})
    return ok({"activeName": name})


# ---------------- 登录（微软正版 / LittleSkin 第三方） ----------------

@app.post("/api/auth/ms/device")
def api_auth_ms_device():
    """申请微软设备码：用户打开验证网址输入代码。"""
    import auth
    r = auth.ms_device_code()
    if not r.get("ok"):
        return err(r.get("msg", "获取设备码失败"))
    return ok({k: r[k] for k in ("deviceCode", "userCode", "verifyUrl", "interval", "expireIn")})


@app.post("/api/auth/ms/poll")
def api_auth_ms_poll():
    """轮询微软登录结果，成功即写入账户。"""
    import auth
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("deviceCode") or "").strip()
    if not code:
        return err("缺少 deviceCode")
    r = auth.ms_device_poll(code)
    if r.get("pending"):
        return jsonify({"ok": False, "pending": True, "msg": r.get("msg", "等待确认")})
    if not r.get("ok"):
        return err(r.get("msg", "登录失败"))
    acc = r["account"]
    if not acc.get("name"):
        return err("未取到玩家名")
    auth.add_logged_in(acc)
    return ok(acc)


@app.post("/api/auth/ygg/login")
def api_auth_ygg_login():
    """第三方（Yggdrasil / authlib-injector）皮肤站账号密码登录。

    认证服务器必填，例如 https://littleskin.cn/api/yggdrasil
    可选：注册链接（登录框里给个去注册的入口）、登录方式名称（按钮与标题上显示）。
    """
    import auth
    body = request.get_json(force=True, silent=True) or {}
    user = (body.get("username") or "").strip()
    pwd = (body.get("password") or "")
    server = (body.get("server") or "").strip()
    label = (body.get("label") or "").strip()
    register = (body.get("register") or "").strip()

    if not server:
        return err("请填写认证服务器地址（例如 https://littleskin.cn/api/yggdrasil）")
    if not re.match(r"^https?://[^\s]+$", server):
        return err("认证服务器地址要写成完整网址，以 http:// 或 https:// 开头")
    if not user or not pwd:
        return err("请填写账号（邮箱或用户名）和密码")

    server = server.rstrip("/")
    r = auth.ygg_authenticate(server, user, pwd)
    if not r.get("ok"):
        return err(r.get("msg", "登录失败"))

    acc = r["account"]
    if label:
        acc["serverName"] = label
    if register:
        acc["registerUrl"] = register
    auth.add_logged_in(acc)

    # 记住这次填的内容，下次打开登录框直接带上
    try:
        cfg = core.load()
        cfg["authServer"] = server
        cfg["authServerName"] = label or cfg.get("authServerName") or ""
        cfg["authRegister"] = register or cfg.get("authRegister") or ""
        cfg["authLabel"] = label or cfg.get("authLabel") or ""
        core.save(cfg)
    except Exception:
        pass
    return ok(acc)


# ---------------- Java ----------------

@app.get("/api/java")
def api_java_list():
    lst = core.list_java()
    cfg = core.load()
    bundled = core.scan_bundled_java()
    for b in bundled:
        if all(b.get("path") != x.get("path") for x in lst):
            lst.append(b)
    mc = request.args.get("mc", "")
    req, pick = None, None
    if mc:
        # 只挑选、不触发下载，界面上展示"当前会自动用哪个 Java"
        pick, req = core.select_java(mc, None, cfg, allow_download=False)
    return ok({"java": lst, "current": cfg.get("javaPath", ""),
               "bundled": len(bundled),
               "pick": pick or (core.auto_pick_java(mc) if mc else None),
               "need": core.recommended_major(mc) if mc else 0,
               "req": req})


@app.post("/api/java/scan")
def api_java_scan():
    lst = core.list_java(force=True)
    return ok({"java": lst})


@app.get("/api/java/check")
def api_java_check():
    vid = request.args.get("version", "")
    p = request.args.get("path", "")
    ji = core.resolve_java(p)
    if not ji:
        return err("未检测到 Java")
    chk = core.check_java_for_version(ji, vid)
    req = core.java_requirement(vid) if vid else None
    if req:
        fit = core.java_in_range(ji, req)
        chk["inRange"] = fit
        if not fit:
            chk["level"] = "warn" if chk.get("level") == "ok" else chk.get("level")
            chk["msg"] = (chk.get("msg") or "") + (
                "（该版本要求 Java %s，当前 Java %s）"
                % (game._req_text(req), ji.get("major")))
    return ok({"java": ji, "check": chk, "req": req})


# ---------------- 版本 ----------------

@app.get("/api/versions/installed")
def api_installed():
    return ok(vmod.list_installed())


@app.get("/api/versions/online")
def api_online():
    refresh = request.args.get("refresh") in ("1", "true")
    cat = request.args.get("cat", "all")
    q = request.args.get("q", "").strip()
    try:
        lst = vmod.list_online_versions(force=refresh, cat=cat, query=q)
        counts = vmod.online_counts()
    except Exception as e:  # noqa: BLE001
        return err(f"版本列表获取失败：{e}")
    installed = {v["id"] for v in vmod.list_installed()}
    for v in lst:
        v["installed"] = v["id"] in installed
    return ok({"list": lst, "counts": counts})


@app.get("/api/versions/counts")
def api_version_counts():
    try:
        return ok(vmod.online_counts())
    except Exception as e:  # noqa: BLE001
        return err(str(e))


@app.post("/api/versions/install")
def api_install():
    body = request.get_json(force=True, silent=True) or {}
    vid = (body.get("id") or "").strip()
    if not vid:
        return err("缺少版本 ID")
    with_assets = bool(body.get("assets", True))
    loader = (body.get("loader") or "").strip()
    loader_version = (body.get("loaderVersion") or "").strip()
    isolate = body.get("isolate", None)      # True/False/None(跟随主设置)

    # 版本隔离：下载时就记进版本设置
    if isolate is not None:
        core.set_version_settings(vid, {"isolate": bool(isolate)})
    elif loader:
        # 带加载器时新版本 id 还未知，装完再补
        pass

    def _worker(task):
        try:
            vmod.install_version(vid, task, with_assets)
            final_id = vid
            if loader and loader_version and task.state != core.TaskState.FAILED:
                task.message = f"安装 {loader} {loader_version} …"
                final_id = vmod.install_loader(loader, vid, loader_version, task)
                if isolate is not None:
                    core.set_version_settings(final_id, {"isolate": bool(isolate)})
            task.target = final_id
        except Exception as e:  # noqa: BLE001
            task.state = core.TaskState.FAILED
            task.error = str(e)

    title = f"安装 {vid}" + (f" + {loader} {loader_version}" if loader else "")
    task = core.tasks.create("install", title, vid)
    core.tasks.start(task, _worker)
    return ok(task.to_dict())


# ---------------- 模组加载器 ----------------

@app.get("/api/loaders")
def api_loaders():
    mc = request.args.get("mc", "").strip()
    if not mc:
        return err("缺少 MC 版本")
    only = request.args.get("only", "").strip()
    try:
        return ok(vmod.list_loaders(mc, only))
    except Exception as e:  # noqa: BLE001
        return err(f"加载器列表获取失败：{e}")


@app.post("/api/versions/<path:vid>/loader")
def api_install_loader(vid):
    body = request.get_json(force=True, silent=True) or {}
    loader = (body.get("loader") or "").strip()
    loader_version = (body.get("loaderVersion") or "").strip()
    if not loader or not loader_version:
        return err("缺少加载器信息")

    def _worker(task):
        try:
            new_id = vmod.install_loader(loader, vid, loader_version, task)
            task.target = new_id
            task.title = f"安装 {loader} {loader_version}"
        except Exception as e:  # noqa: BLE001
            task.state = core.TaskState.FAILED
            task.error = str(e)

    task = core.tasks.create("loader", f"安装 {loader} {loader_version}", vid)
    core.tasks.start(task, _worker)
    return ok(task.to_dict())


# ---------------- 服务端下载 ----------------

@app.post("/api/versions/<path:vid>/server")
def api_install_server(vid):
    def _worker(task):
        try:
            d = vmod.install_server(vid, task)
            task.message = "服务端已就绪：" + d
        except Exception as e:  # noqa: BLE001
            task.state = core.TaskState.FAILED
            task.error = str(e)

    task = core.tasks.create("server", f"下载 {vid} 服务端", vid)
    core.tasks.start(task, _worker)
    return ok(task.to_dict())


# ---------------- 版本独立设置 ----------------

@app.get("/api/versions/<path:vid>/settings")
def api_get_version_settings(vid):
    st = core.version_settings(vid)
    st["isolateInfo"] = core.resolve_isolate(vid)
    st["gameDirEff"] = core.effective_game_dir(vid)
    st["modCount"] = count_dir_mods(st["gameDirEff"])
    return ok(st)


def count_dir_mods(game_dir: str) -> int:
    """数一下游戏目录里的模组文件数量（用于界面提示）。"""
    d = os.path.join(game_dir or "", "mods")
    try:
        if not os.path.isdir(d):
            return 0
        return len([f for f in os.listdir(d)
                    if f.lower().endswith((".jar", ".zip", ".litemod"))])
    except Exception:  # noqa: BLE001
        return 0


@app.put("/api/versions/<path:vid>/settings")
def api_set_version_settings(vid):
    body = request.get_json(force=True, silent=True) or {}
    patch = body.get("settings") or body
    try:
        return ok(core.set_version_settings(vid, patch))
    except Exception as e:  # noqa: BLE001
        return err(str(e))


@app.delete("/api/versions/<path:vid>/settings")
def api_clear_version_settings(vid):
    return ok(core.clear_version_settings(vid))


@app.delete("/api/versions/<path:vid>")
def api_delete_version(vid):
    if _busy_version(vid):
        return err("该版本正在安装中，无法删除")
    vmod.delete_version(vid)
    return ok(True)


def _busy_version(vid: str) -> bool:
    for t in core.tasks.all():
        if t["target"] == vid and t["state"] in ("pending", "running"):
            return True
    return False


@app.get("/api/versions/<path:vid>/status")
def api_version_status(vid):
    st = game.check_ready(vid)
    return ok(st)


@app.post("/api/versions/refresh")
def api_refresh_manifest():
    try:
        vmod.fetch_manifest(force=True)
        return ok(True)
    except Exception as e:  # noqa: BLE001
        return err(str(e))


# ---------------- 资源平台（Mod / 资源包 / 整合包） ----------------

@app.get("/api/resources/search")
def api_resource_search():
    q = request.args.get("q", "").strip()   # 允许为空：空关键词 = 浏览热门资源
    kind = request.args.get("kind", "mod")
    src = request.args.get("source", "")
    mc = request.args.get("mc", "")
    loader = request.args.get("loader", "")
    sort = request.args.get("sort", "downloads")
    order = request.args.get("order", "desc")
    try:
        # 没有关键词 = 浏览全部资源，多给一些条数
        lim = 60 if not q else 24
        r = mods.search(q, kind, src, mc, loader, sort=sort, order=order, limit=lim)
    except Exception as e:  # noqa: BLE001
        return err(f"搜索失败：{e}")
    before = len(r.get("results") or [])
    r["results"] = mods.filter_cheats(r.get("results") or [])
    r["blocked"] = before - len(r["results"])
    return ok(r)


@app.get("/api/resources/versions")
def api_resource_versions():
    pid = request.args.get("id", "").strip()
    src = request.args.get("source", "modrinth")
    mc = request.args.get("mc", "")
    loader = request.args.get("loader", "")
    if not pid:
        return err("缺少项目 ID")
    try:
        return ok(mods.project_versions(src, pid, mc, loader))
    except Exception as e:  # noqa: BLE001
        return err(f"版本列表获取失败：{e}")


@app.get("/api/resources/targets")
def api_resource_targets():
    """可安装目标：已安装的版本，附 MC 版本、加载器、是否隔离、落地目录。"""
    cfg = core.load()
    out = []
    for v in vmod.list_installed():
        try:
            info = mods.target_info(v["id"], cfg)
        except Exception:  # noqa: BLE001
            info = {"id": v["id"], "mc": v["id"], "loader": "", "isolate": False, "dir": ""}
        info["type"] = v.get("type", "")
        info["hasJar"] = bool(v.get("hasJar"))
        out.append(info)
    return ok({
        "targets": out,
        "current": (cfg.get("lastVersion") or (out[0]["id"] if out else "")),
        "isolateGlobal": bool(cfg.get("isolateVersion", False)),
    })


@app.post("/api/resources/compat")
def api_resource_compat():
    """
    安装前的兼容性检查。
    入参：{version, kind, mc:[...], loaders:[...], name, fileName}
    """
    body = request.get_json(force=True, silent=True) or {}
    vid = (body.get("version") or "").strip()
    kind = (body.get("kind") or "mod").strip()
    if not vid:
        return err("请先选择要安装到的游戏版本")
    meta = {
        "mc": body.get("mc") or [],
        "loaders": body.get("loaders") or [],
        "name": body.get("name") or "",
        "fileName": body.get("fileName") or "",
    }
    try:
        r = mods.check_compat(kind, vid, meta)
    except Exception as e:  # noqa: BLE001
        return err(f"兼容性检查失败：{e}")
    # 目标目录（给用户确认装到哪）
    try:
        r["installDir"] = mods.install_dir(kind, vid)
    except Exception:  # noqa: BLE001
        r["installDir"] = ""
    return ok(r)


@app.post("/api/resources/install")
def api_resource_install():
    body = request.get_json(force=True, silent=True) or {}
    vid = (body.get("version") or "").strip()      # 目标版本（含加载器）
    kind = (body.get("kind") or "mod").strip()
    url = (body.get("url") or "").strip()
    name = (body.get("fileName") or body.get("name") or "").strip()
    force = bool(body.get("force"))                # 用户已确认不兼容仍要下载
    if not vid:
        return err("请先选择要安装到的游戏版本")
    if not url:
        return err("缺少下载地址")

    # 外挂 / 作弊内容直接拒绝
    cheat = mods.check_cheat(body.get("name") or "", "", name)
    if cheat.get("blocked"):
        return err(cheat.get("reason") or "拒绝下载外挂类内容", code=403)

    # 兜底：未确认过的不兼容资源拒绝静默安装，交由前端弹窗二次确认
    meta = {
        "mc": body.get("mc") or [],
        "loaders": body.get("loaders") or [],
        "name": body.get("name") or "",
        "fileName": name,
    }
    if not force and (meta["mc"] or meta["loaders"]):
        try:
            c = mods.check_compat(kind, vid, meta)
            if c.get("level") == "bad":
                return err("内容不兼容，需要确认后才会下载", code=409, compat=c)
        except Exception:  # noqa: BLE001
            pass

    if kind == "modpack":
        def _worker(task):
            try:
                r = mods.install_modpack(url, vid, task)
                task.message = f"整合包安装完成，共 {r['count']} 个文件"
            except Exception as e:  # noqa: BLE001
                task.state = core.TaskState.FAILED
                task.error = str(e)

        task = core.tasks.create("modpack", f"安装整合包到 {vid}", vid)
        core.tasks.start(task, _worker)
        return ok(task.to_dict())

    try:
        r = mods.install_file(kind, vid, url, name)
        r["forced"] = force
        return ok(r)
    except Exception as e:  # noqa: BLE001
        return err(f"安装失败：{e}")


# ---------------- 登录（离线 / 自定义登录源） ----------------

@app.post("/api/auth/login")
def api_auth_login():
    body = request.get_json(force=True, silent=True) or {}
    server = (body.get("server") or "").strip()
    user = (body.get("username") or "").strip()
    pwd = body.get("password") or ""
    if not server:
        return err("请先填写登录源地址，例如 https://littleskin.cn/api/yggdrasil")
    if not user or not pwd:
        return err("请填写邮箱（或用户名）与密码")
    r = core.ygg_authenticate(server, user, pwd)
    if not r.get("ok"):
        return err(r.get("msg", "登录失败"))
    core.add_ygg_account(r["name"], r["uuid"], r["accessToken"],
                         r.get("clientToken", ""), server,
                         (body.get("serverName") or server))
    return ok({"name": r["name"], "uuid": r["uuid"], "profiles": r.get("profiles", [])})


@app.post("/api/auth/refresh")
def api_auth_refresh():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    cfg = core.load()
    acc = core.find_account(name, cfg)
    if not acc or acc.get("type") != "yggdrasil":
        return err("该账户不是外置登录账户")
    r = core.ygg_refresh(acc.get("server", ""), acc.get("accessToken", ""),
                         acc.get("clientToken", ""))
    if not r.get("ok"):
        return err(r.get("msg", "刷新失败"))
    core.add_ygg_account(r.get("name") or acc["name"], r.get("uuid") or acc["uuid"],
                         r["accessToken"], r.get("clientToken", ""),
                         acc.get("server", ""), acc.get("serverName", ""))
    return ok({"name": r.get("name") or acc["name"]})


@app.post("/api/auth/logout")
def api_auth_logout():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    cfg = core.load()
    acc = core.find_account(name, cfg)
    if acc and acc.get("type") == "yggdrasil":
        try:
            requests.post(core.ygg_url(acc.get("server", ""), "authserver/invalidate"),
                          json={"accessToken": acc.get("accessToken"),
                                "clientToken": acc.get("clientToken")},
                          timeout=(6, 12))
        except Exception:
            pass
    return ok(core.remove_account(name))


# ---------------- 自检与自动补齐 ----------------

@app.get("/api/selfcheck")
def api_selfcheck():
    st = selfcheck_state()
    return ok(st)


@app.post("/api/selfcheck/run")
def api_selfcheck_run():
    body = request.get_json(force=True, silent=True) or {}
    auto = body.get("repair", True) is not False
    if not start_selfcheck_async(auto_repair=bool(auto)):
        return err("体检正在运行中，请稍候")
    return ok({"started": True})


@app.post("/api/crash/analyze")
def api_crash_analyze():
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text") or ""
    return ok(game.analyze_crash(text))


@app.post("/api/cleanup")
def api_cleanup():
    """清理日志 / 下载残留 / 崩溃报告，释放磁盘空间。"""
    body = request.get_json(force=True, silent=True) or {}
    targets = body.get("targets") or ["logs"]
    cfg = core.load()
    removed = 0
    freed = 0
    details: List[str] = []

    def _clear(d: str, label: str):
        nonlocal removed, freed
        if not os.path.isdir(d):
            return
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p):
                    sz = os.path.getsize(p)
                    os.remove(p)
                    removed += 1
                    freed += sz
                elif os.path.isdir(p):
                    sz = _dir_size(p)
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
                    freed += sz
            except Exception:  # noqa: BLE001
                pass
        details.append(f"{label}：{d}")

    def _dir_size(d: str) -> int:
        total = 0
        for dp, _dn, fn in os.walk(d):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except Exception:  # noqa: BLE001
                    pass
        return total

    if "logs" in targets:
        _clear(core.LOGS_DIR, "启动器日志")
    if "crash" in targets:
        _clear(os.path.join(core.game_dir(cfg), "crash-reports"), "崩溃报告")
    if "temp" in targets:
        for root in (core.libraries_dir(cfg), core.versions_dir(cfg),
                     core.assets_dir(cfg), core.game_dir(cfg)):
            if not os.path.isdir(root):
                continue
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    if f.endswith(".cbtmp") or f.endswith(".part"):
                        try:
                            sz = os.path.getsize(os.path.join(dp, f))
                            os.remove(os.path.join(dp, f))
                            removed += 1
                            freed += sz
                        except Exception:  # noqa: BLE001
                            pass
    if "cache" in targets:
        # 删除 0 字节的库文件（不完整下载），之后会自动重新补齐
        for root in (core.libraries_dir(cfg), core.assets_dir(cfg)):
            if not os.path.isdir(root):
                continue
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    p = os.path.join(dp, f)
                    try:
                        if os.path.isfile(p) and os.path.getsize(p) == 0:
                            os.remove(p)
                            removed += 1
                    except Exception:  # noqa: BLE001
                        pass

    _log_cleanup("清理：" + ",".join(targets) + f" 释放 {freed / 1024 / 1024:.1f} MB")
    return ok({"removed": removed, "freed": freed,
               "freedText": f"{freed / 1024 / 1024:.1f} MB", "details": details})


def _log_cleanup(msg: str):
    try:
        os.makedirs(core.LOGS_DIR, exist_ok=True)
        with open(os.path.join(core.LOGS_DIR, "cleanup.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


# ==================================================================
# 更新检查（完全非强制，拿不到就当没有）
# ==================================================================

_update_lock = threading.Lock()
_update_cache: Dict[str, Any] = {
    "checkedAt": 0.0,
    "hasUpdate": False,
    "remote": None,
    "error": None,
}


def _vkey(v: str) -> tuple:
    """把 '0.2.1' 之类转成可比较的元组，非数字部分忽略。"""
    out = []
    for part in str(v or "0").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def fetch_update(force: bool = False) -> Dict[str, Any]:
    """
    尝试从远端拿更新信息。任何异常都吞掉——
    启动器不依赖服务器运行，拿不到就是没更新。
    """
    with _update_lock:
        now = time.time()
        if not force and _update_cache.get("checkedAt") and now - _update_cache["checkedAt"] < 600:
            return dict(_update_cache)

        cfg = core.load()
        url = (cfg.get("updateUrl") or "").strip()
        result: Dict[str, Any] = {
            "checked": False,
            "hasUpdate": False,
            "current": core.APP_VERSION,
            "latest": core.APP_VERSION,
            "notes": "",
            "url": "",
            "fileCount": 0,
            "error": None,
            "checkedAt": now,
        }

        if not cfg.get("updateCheck", True):
            result["error"] = "已关闭更新检查"
            _update_cache.update(result)
            return dict(result)
        if cfg.get("offlineMode"):
            result["error"] = "已关闭与外界连接"
            _update_cache.update(result)
            return dict(result)
        if not url:
            result["error"] = "未配置更新地址"
            _update_cache.update(result)
            return dict(result)

        try:
            s = requests.Session()
            s.headers.update({"User-Agent": "CuibingLauncher/" + core.APP_VERSION,
                              "Connection": "close"})
            r = s.get(url, timeout=8)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("bad payload")

            latest = str(data.get("version") or core.APP_VERSION)
            files = data.get("files") or {}
            if not isinstance(files, dict):
                files = {}

            result.update({
                "checked": True,
                "hasUpdate": _vkey(latest) > _vkey(core.APP_VERSION) or bool(files),
                "latest": latest,
                "notes": str(data.get("notes") or ""),
                "url": str(data.get("url") or ""),
                "fileCount": len(files),
                "error": None,
            })
            _update_cache.update(result)
            _update_cache["remote"] = data
            try:
                core.update({"lastUpdate": {"latest": latest, "at": now}})
            except Exception:
                pass
            return dict(result)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)[:120]
            _update_cache.update(result)
            return dict(result)


@app.get("/api/update/check")
def api_update_check():
    force = request.args.get("force") in ("1", "true", "yes")
    return ok(fetch_update(force))


# 更新日志（新版本发版时往列表头部追加一条）
CHANGELOG: List[Dict[str, Any]] = [
    {
        "version": "1.2.0",
        "date": "2026-10-25",
        "title": "实时内存优化",
        "items": [
            "内存改成「实时自动」：每次点启动的那一刻，按机器此刻真正可用的内存重新计算，"
            "并给系统留出 1~4 GB 不可占用的余量，后台程序多的时候会自动少给，不再把系统挤爆",
            "新增「一键内存优化」：主页启动条和设置页各有一个按钮，按当前可用内存算出最优值直接写入，"
            "并弹出说明（总量 / 此刻可用 / 占用率 / 系统预留 / 为什么是这个值）",
            "设置页新增「实时状态」：显示物理内存、此刻可用、占用率与建议值，每 15 秒自动刷新，也能手动刷新",
            "启动日志新增内存状态行：启动前可用多少、占用百分之多少、这次实际分配多少，排障更直观",
            "内存值按档位收拢（2G 以下 256 MB 一档，8G 以下 512 MB 一档，再多 1G 一档），不再出现奇怪的数字",
            "使用指南「Java 与内存」补充实时分配、一键优化、以及「为什么我设的值没生效」三条说明",
        ],
    },
    {
        "version": "1.1.1",
        "date": "2026-10-25",
        "title": "启动链路重构（借鉴 PCL）",
        "items": [
            "重写 Java 选择：不再给每个版本定死一个推荐版本，而是算出「版本区间」，"
            "按区间挑选、挑不到就自动下载（架构参考 PCL 的 ModJava / ModLaunch）",
            "Java 需求会综合 MC 版本、版本 JSON 里 Mojang 声明的要求，"
            "以及 Forge / NeoForge / Fabric / OptiFine / LiteLoader 的上下限约束",
            "新增 Mojang 官方 Java 运行时下载源（与 PCL 同源，带国内镜像），"
            "并移除两个实测返回 404 的失效镜像地址",
            "启动改用 java.exe 而不是 javaw.exe，日志与崩溃分析恢复可用",
            "JVM 参数去重：重复的 -Xmx / -cp / -Dfile.encoding 等会被正确覆盖而不是让 JVM 报错",
            "按 Java 版本自动选择 GC（Java 21+ 分代 ZGC，15~20 ZGC，其余 G1GC）",
            "编码参数按 Java 版本切换（Java 18+ 用 -Dfile.encoding=COMPAT，19 前后 stdout 属性名不同）",
            "启动前预检查：游戏路径含 ! 或 ; 直接拦下（会破坏 classpath）",
            "natives 路径非 ASCII 或过长时自动换到纯 ASCII 目录，中文/超长路径不再卡启动",
            "启动时把 Java 的 bin 加入 PATH，并正确设置 appdata，老版本与 Mod 更容易找到东西",
            "缺失少量文件时先自动补齐再启动（PCL 的「补全文件」步骤），而不是直接拒绝",
            "启动日志新增基础参数 / 登录参数区块，并等待游戏窗口出现来判定是否崩溃",
            "侧栏新增「找服务器」：连上脆冰网站就能按关键词 / 类型 / MC 版本 / mod 筛服务器，一键申请加入",
            "开发空间可以选择发到你加入的私人空间，还能勾选「同时发到开发空间」",
            "Java 补齐升级为「补齐全部」：一次装好 MC 1.13 ~ 26.1+ 需要的 Java 8 / 17 / 21 / 25",
            "使用指南新增「找服务器 / 开发空间 / Java 组件与自定义参数」三组，每组都带分步演示",
            "新增「压缩启动器.exe」精简版：本体很小，首次运行自动补全运行环境并自我增长",
            "清理代码中注释里残留的默认管理员口令",
        ],
    },
    {
        "version": "1.1.0",
        "date": "2026-10-25",
        "title": "正式发布启动器",
        "items": [
            "KCL 启动器正式发布（v1.1.0）",
            "移除界面不透明度功能（用处不大且存在显示问题）",
            "新增「开发」页：连接脆冰网站的开发空间，可以看动态、点赞、评论、发帖",
            "开发者头像更新：shiyixianjunawa 真实头像上墙",
            "设置新增「关闭与外界连接」：关闭后启动器照常使用，但不访问网站端数据（KCL 服务器不连接，其余资源下载不受影响）",
            "脆冰网站服务端：管理员后台 + 社区 / 合作 / 建议 / 资源（KCL玩家开发）/ MC 服务器宣传 / 开发空间",
            "管理员口令哈希存储，登录后同一设备 1 天内免重输",
        ],
    },
    {
        "version": "1.0.2",
        "date": "2026-10-24",
        "title": "指南「我要试试」分段演示",
        "items": [
            "每个指南分类后面都有自己的「我要试试」，点它只演示这一部分",
            "顶部按钮是「我要试试（全部）」，按指南顺序从头到尾走一遍",
            "引导气泡上会显示当前属于哪一个部分，以及第几步 / 共几步",
            "第一次打开指南页会弹出提示，告诉你点「我要试试」可以跟着走",
            "演示完会自动回到指南页，中途退出也会回去",
            "修复：头像不跟随皮肤变化（现在读真实皮肤画脸，换皮肤立刻更新）",
            "修复：版本清单某个镜像挂掉时整个列表拿不到（改为多源自动回退）",
            "修复：任务栏图标显示成 Python 图标（现在统一为脆冰图标）",
            "修复：确认/输入弹窗改为内置主题化弹窗，不再弹系统原生框",
            "修复：点「启动游戏」报错 TypeError（后端参数冲突）",
            "修复：外置登录账户启动时后端变量未定义导致启动失败",
        ],
    },
    {
        "version": "1.0",
        "date": "2026-10-24",
        "title": "KCL 首个正式版",
        "items": [
            "品牌升级为 KCL 启动器（KC studio 制作），换上脆冰图标与开场动画",
            "EXE 内置 Python 运行环境：双击即用，不依赖系统 Python，也不需要联网",
            "修复无边框窗口不能拖动、不能拉伸的问题（顶栏拖动 + 四边四角拉伸）",
            "资源中心：不输关键词也能浏览全部资源，可按下载量 / 更新时间 / 支持版本排序",
            "新增正版登录（微软设备码）与 LittleSkin 第三方登录",
            "存档地图：SeedMap、结构查询、史莱姆区块、实时地图与标点规划",
            "Java 自动补齐与自动选择，可自定义启动参数",
            "外挂 / 作弊类模组与数据包自动拦截",
            "皮肤：LittleSkin 公开皮肤库 + 正版玩家皮肤（手动刷新，不轮询）",
            "检查更新与更新日志：只有你点按钮才联网，绝不强制更新",
        ],
    },
]


@app.get("/api/changelog")
def api_changelog():
    return ok({"current": core.APP_VERSION, "logs": CHANGELOG})


@app.get("/api/update/info")
def api_update_info():
    cfg = core.load()
    return ok({"current": core.APP_VERSION,
               "updateUrl": cfg.get("updateUrl", ""),
               "updateCheck": cfg.get("updateCheck", True),
               "lastUpdate": cfg.get("lastUpdate", {}),
               "cache": {k: v for k, v in _update_cache.items() if k != "remote"}})


@app.post("/api/update/apply")
def api_update_apply():
    """
    把远端 files 映射里的文件逐个下载覆盖到 CL 目录。
    覆盖前先备份到 _备份/<时间戳>/，失败自动回滚。
    """
    with _update_lock:
        remote = _update_cache.get("remote")
    if not remote:
        r = fetch_update(True)
        with _update_lock:
            remote = _update_cache.get("remote")
        if not remote:
            return err("暂时拿不到更新信息")

    files = remote.get("files") or {}
    if not isinstance(files, dict) or not files:
        return err("更新包里没有可更新的文件")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = os.path.join(ROOT, "_备份", stamp)
    written: List[str] = []
    try:
        for rel, src in files.items():
            rel = str(rel).replace("\\", "/").lstrip("/")
            if rel.startswith("..") or "/../" in rel:
                continue
            dst = os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isfile(dst):
                b = os.path.join(backup_root, rel)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(dst, b)
            s = requests.Session()
            s.headers.update({"Connection": "close"})
            with s.get(str(src), stream=True, timeout=30) as resp:
                resp.raise_for_status()
                tmp = dst + ".cbtmp"
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, dst)
            written.append(rel)
    except Exception as exc:  # noqa: BLE001
        for rel in reversed(written):
            try:
                b = os.path.join(backup_root, rel)
                if os.path.isfile(b):
                    shutil.copy2(b, os.path.join(ROOT, rel))
            except Exception:
                pass
        return err("更新失败，已回滚：" + str(exc)[:120])

    return ok({"updated": written, "backup": backup_root,
               "count": len(written), "latest": remote.get("version", "")})


# ---------------- 任务 ----------------

@app.get("/api/tasks")
def api_tasks():
    return ok(core.tasks.all())


@app.get("/api/tasks/<task_id>")
def api_task(task_id):
    t = core.tasks.get(task_id)
    if not t:
        return err("任务不存在", 404)
    return ok(t.to_dict())


@app.post("/api/tasks/<task_id>/cancel")
def api_task_cancel(task_id):
    t = core.tasks.get(task_id)
    if not t:
        return err("任务不存在", 404)
    t.cancel()
    return ok(True)


@app.delete("/api/tasks/<task_id>")
def api_task_remove(task_id):
    t = core.tasks.get(task_id)
    if not t:
        return err("任务不存在", 404)
    if t.state in (core.TaskState.PENDING, core.TaskState.RUNNING):
        t.cancel()
    t.state = core.TaskState.CANCELLED
    if task_id in core.tasks._tasks:  # noqa: SLF001
        core.tasks._tasks.pop(task_id, None)  # noqa: SLF001
    return ok(True)


# ---------------- 启动 ----------------

@app.post("/api/launch")
def api_launch():
    body = request.get_json(force=True, silent=True) or {}
    vid = (body.get("version") or body.get("id") or "").strip()
    if not vid:
        return err("未指定版本")
    opts = {
        "width": body.get("width"),
        "height": body.get("height"),
        "fullscreen": body.get("fullscreen"),
        "maxMemory": body.get("maxMemory"),
        "javaPath": body.get("javaPath"),
        "jvmArgs": body.get("jvmArgs"),
    }
    try:
        r = game.launch(vid, (body.get("account") or "").strip(), None, opts)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return err("启动过程出错：%s: %s" % (type(e).__name__, e), code=500)
    if not r.get("ok"):
        extra = {k: v for k, v in r.items() if k not in ("ok", "msg")}
        return err(str(r.get("msg") or "启动失败"), **extra)
    return ok(r)


@app.get("/api/launch/status")
def api_launch_status():
    gp = game.current_process()
    if not gp:
        return ok({"running": False})
    d = gp.to_dict()
    d["running"] = gp.alive
    return ok(d)


@app.post("/api/launch/stop")
def api_launch_stop():
    r = game.stop_game()
    if not r["ok"]:
        return err(r["msg"])
    return ok(True)


@app.get("/api/pick/dir")
def api_pick_dir():
    """弹出系统目录选择框（Windows 原生 filedialog）。"""
    try:
        import tkinter  # noqa: F401
        from tkinter import filedialog
        cfg = core.load()
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(initialdir=core.game_dir(cfg), title="选择游戏目录")
        try:
            root.destroy()
        except Exception:
            pass
        if path:
            return ok(path)
        return err("已取消")
    except Exception as e:  # noqa: BLE001
        return err(f"无法打开选择框，请手动填写路径（{e}）")


@app.post("/api/open/folder")
def api_open_folder():
    body = request.get_json(force=True, silent=True) or {}
    kind = body.get("kind", "game")
    cfg = core.load()
    mapping = {
        "game": core.game_dir(cfg),
        "versions": core.versions_dir(cfg),
        "mods": os.path.join(core.game_dir(cfg), "mods"),
        "saves": os.path.join(core.game_dir(cfg), "saves"),
        "logs": core.LOGS_DIR,
        "app": core.ROOT_DIR,
    }
    path = mapping.get(kind)
    if not path:
        return err("未知目录")
    os.makedirs(path, exist_ok=True)
    try:
        os.startfile(path)  # noqa: S606 - Windows 专有
    except Exception as e:  # noqa: BLE001
        return err(str(e))
    return ok(path)


# ---------------- 静态资源 ----------------

@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/<path:p>")
def static_files(p):
    # 写错的 /api 请求不能回 HTML 页面（前端会显示"响应解析失败"）
    if p == "api" or p.startswith("api/"):
        return err("接口不存在：/%s" % p, code=404)
    full = os.path.join(UI_DIR, p)
    if os.path.isfile(full):
        return send_from_directory(UI_DIR, p)
    return send_from_directory(UI_DIR, "index.html")


def find_free_port(start: int = 18920, tries: int = 40) -> int:
    for i in range(tries):
        port = start + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def start_server(port: int = 0) -> Dict[str, Any]:
    """启动 Flask（后台线程），返回 {"port": ...}"""
    port = port or find_free_port()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False,
                               threaded=True, use_reloader=False),
        daemon=True,
    ).start()
    return {"port": port, "url": f"http://127.0.0.1:{port}"}


# ==================================================================
# 地图：SeedMap / 实时地图 / 标点规划 / 服务器
# ==================================================================

@app.get("/api/worlds")
def api_worlds():
    try:
        return ok({"worlds": world.list_worlds()})
    except Exception as e:  # noqa: BLE001
        return err("读取存档失败：" + str(e))


@app.get("/api/world/map")
def api_world_map():
    """实时地图：渲染已生成的区块。"""
    d = request.args.get("dir", "").strip()
    if not d or not os.path.isdir(d):
        return err("存档目录不存在")
    dim = request.args.get("dim", "overworld")
    try:
        cx = float(request.args.get("cx", 0))
        cz = float(request.args.get("cz", 0))
        span = max(64, min(4096, int(request.args.get("span", 512))))
        tile = max(64, min(768, int(request.args.get("tile", 320))))
    except Exception:
        return err("参数不正确")
    if request.args.get("clear"):
        world.clear_cache()
    try:
        r = world.render_map(d, dim, (cx, cz), span, tile)
    except Exception as e:  # noqa: BLE001
        return err("渲染失败：" + str(e))
    return ok(r) if r.get("ok") else err(r.get("msg", "渲染失败"))


@app.get("/api/world/structures")
def api_world_structures():
    """结构位置：优先读存档真实数据，没有就按 seed 推测。"""
    d = request.args.get("dir", "").strip()
    if not d or not os.path.isdir(d):
        return err("存档目录不存在")
    dim = request.args.get("dim", "overworld")
    try:
        cx = float(request.args.get("cx", 0))
        cz = float(request.args.get("cz", 0))
        span = max(128, min(8192, int(request.args.get("span", 1024))))
    except Exception:
        return err("参数不正确")
    x0, x1 = cx - span / 2.0, cx + span / 2.0
    z0, z1 = cz - span / 2.0, cz + span / 2.0

    seed = None
    try:
        lvl = os.path.join(d, "level.dat")
        if os.path.isfile(lvl):
            seed = world._extract_seed(world.read_level_dat(lvl))
    except Exception:
        seed = None
    if request.args.get("seed"):
        try:
            seed = int(request.args["seed"])
        except Exception:
            pass

    real = world.scan_structures(d, dim)
    guessed = []
    if seed is not None:
        have = set((s["id"], s["x"] // 16, s["z"] // 16) for s in real)
        for g in world.guess_structures(seed, x0, z0, x1, z1):
            if (g["id"], g["x"] // 16, g["z"] // 16) in have:
                continue
            guessed.append(g)
    slimes = world.slime_chunks(seed, x0, z0, x1, z1) if seed is not None else []
    return ok({"seed": seed, "structures": real + guessed,
               "realCount": len(real), "guessCount": len(guessed),
               "slime": slimes, "bounds": {"x0": x0, "z0": z0, "x1": x1, "z1": z1}})


@app.get("/api/world/marks")
def api_world_marks_get():
    d = request.args.get("dir", "").strip()
    if not d:
        return err("缺少存档目录")
    return ok(world.load_marks(d))


@app.post("/api/world/marks")
def api_world_marks_save():
    body = request.get_json(force=True, silent=True) or {}
    d = (body.get("dir") or "").strip()
    if not d:
        return err("缺少存档目录")
    try:
        return ok(world.save_marks(d, body))
    except Exception as e:  # noqa: BLE001
        return err("保存失败：" + str(e))


@app.post("/api/server/ping")
def api_server_ping():
    body = request.get_json(force=True, silent=True) or {}
    addr = (body.get("address") or "").strip()
    if not addr:
        return err("请填写服务器地址")
    try:
        r = _ping_server(addr, int(body.get("port") or 25565))
    except Exception as e:  # noqa: BLE001
        return err("探测失败：" + str(e))
    r["note"] = ("服务器的区块数据只有在真正登录并接收区块包时才能拿到，"
                 "启动器不会自动登录他人服务器；这里提供的是服务器状态与"
                 "按 seed 推算的地图，需要实时地图请对本地存档使用。")
    return ok(r) if r.get("ok") else err(r.get("msg", "探测失败"))


def _ping_server(host: str, port: int = 25565, timeout: float = 4.0):
    """Server List Ping（1.7+ 协议）。"""
    import json as _json
    import struct as _st

    def _vi(v: int) -> bytes:
        out = b""
        while True:
            b = v & 0x7F
            v >>= 7
            out += bytes([b | (0x80 if v else 0)])
            if not v:
                return out

    def _uvi(s) -> int:
        r, n = 0, 0
        while True:
            b = s.recv(1)
            if not b:
                raise IOError("连接已关闭")
            b = b[0]
            r |= (b & 0x7F) << (7 * n)
            n += 1
            if n > 5:
                raise IOError("varint 过长")
            if not (b & 0x80):
                return r

    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = host
    payload = _vi(0) + _vi(765) + _vi(len(host)) + host.encode("utf-8")
    payload += _st.pack(">H", port) + _vi(1)
    s = socket.create_connection((ip, port), timeout=timeout)
    try:
        s.sendall(_vi(len(payload)) + payload)
        s.sendall(_vi(1) + b"\x00")
        _uvi(s)
        _uvi(s)
        n = _uvi(s)
        buf = b""
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                break
            buf += c
        data = _json.loads(buf.decode("utf-8", errors="replace"))
        desc = data.get("description")
        motd = desc.get("text", "") if isinstance(desc, dict) else str(desc or "")
        return {"ok": True,
                "version": (data.get("version") or {}).get("name", ""),
                "protocol": (data.get("version") or {}).get("protocol", 0),
                "players": (data.get("players") or {}).get("online", 0),
                "max": (data.get("players") or {}).get("max", 0),
                "motd": motd, "host": host, "port": port,
                "hasIcon": bool(data.get("favicon"))}
    finally:
        try:
            s.close()
        except Exception:
            pass


# ==================================================================
# 皮肤
# ==================================================================

@app.get("/api/skin/sources")
def api_skin_sources():
    return ok({"sources": [{"id": k, "name": v["name"]}
                           for k, v in core.SKIN_SOURCES.items()]})


@app.get("/api/skin/query")
def api_skin_query():
    """按玩家名查询皮肤（手动触发，不轮询）。"""
    player = request.args.get("player", "").strip()
    source = request.args.get("source", "auto")
    if not player:
        return err("请填写玩家名")
    uid = None
    if re.fullmatch(r"[0-9a-fA-F]{32}", player.replace("-", "")):
        uid = player.replace("-", "")
    if uid is None:
        uid = core.fetch_uuid(player, source)
    if not uid:
        return err("没有找到这个玩家，检查名字或换个皮肤源")
    prof = core.fetch_profile(uid, source)
    if not prof:
        return err("取不到「%s」的皮肤信息（网络不通或该源没有数据）" % player)
    local = core.download_skin(prof.get("skin", ""), player)
    cape = core.download_skin(prof.get("cape", ""), player + "-cape")
    return ok({"name": prof.get("name") or player, "uuid": prof.get("uuid") or uid,
               "skinUrl": prof.get("skin", ""), "capeUrl": prof.get("cape", ""),
               "model": prof.get("model", "classic"), "source": prof.get("source", ""),
               "skinFile": local, "capeFile": cape,
               "skinVer": _skin_ver(local or ""),
               "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S")})


@app.get("/api/skin/file")
def api_skin_file():
    p = request.args.get("path", "").strip()
    if not p or not os.path.isfile(p):
        return err("皮肤文件不存在")
    d, n = os.path.split(os.path.abspath(p))
    resp = make_response(send_from_directory(d, n))
    # 换皮肤后文件名不变、内容变了，必须禁缓存，否则头像还是旧的
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.post("/api/accounts/skin/sync")
def api_account_skin_sync():
    """用账户自己的 UUID 拉一次皮肤（手动点按钮才访问，不轮询），存进账户供头像使用。"""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    cfg = core.load()
    acc = core.find_account(name, cfg)
    if not acc:
        return err("账户不存在：" + name)
    uid = (acc.get("uuid") or "").replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", uid):
        return err("这个账户没有 UUID，取不到正版皮肤")
    src = "littleskin" if acc.get("type") == "yggdrasil" else "mojang"
    prof = core.fetch_profile(uid, src) or core.fetch_profile(uid, "auto")
    if not prof:
        return err("取不到皮肤信息（网络不通或该源没有数据）")
    who = acc.get("name") or "skin"
    local = core.download_skin(prof.get("skin", ""), who)
    if not local:
        return err("皮肤下载失败")
    acc["skinFile"] = local
    acc["capeFile"] = core.download_skin(prof.get("cape", ""), who + "-cape") or ""
    acc["skinModel"] = prof.get("model", "classic")
    core.save(cfg)
    return ok({"account": acc, "skinVer": _skin_ver(local)})


@app.get("/api/skin/list")
def api_skin_list():
    d = core.skin_dir()
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(".png"):
                out.append({"name": n, "path": os.path.join(d, n)})
    return ok({"skins": out})


@app.post("/api/skin/apply")
def api_skin_apply():
    """把皮肤挂到某个离线账户上。"""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("account") or "").strip()
    if not name:
        return err("缺少账户名")
    cfg = core.load()
    hit = None
    for a in cfg.get("accounts") or []:
        if a.get("name") == name:
            hit = a
            break
    if not hit:
        return err("账户不存在：" + name)
    hit["skinFile"] = (body.get("skinFile") or "").strip()
    hit["capeFile"] = (body.get("capeFile") or "").strip()
    hit["skinModel"] = (body.get("model") or "classic").strip()
    core.save(cfg)
    return ok({"account": hit, "skinVer": _skin_ver(hit.get("skinFile") or "")})


# ==================================================================
# Java 自动补齐
# ==================================================================

@app.get("/api/java/list")
def api_java_full():
    lst = core.list_java(bool(request.args.get("force")))
    bundled = core.scan_bundled_java()
    for b in bundled:
        if all(b.get("path") != x.get("path") for x in lst):
            lst.append(b)
    mc = request.args.get("mc", "")
    return ok({"java": lst, "bundled": len(bundled),
               "pick": core.auto_pick_java(mc) if mc else None,
               "need": core.recommended_major(mc) if mc else 0})


@app.post("/api/java/ensure")
def api_java_ensure():
    """确保有指定主版本的 Java，没有就自动下载。"""
    body = request.get_json(force=True, silent=True) or {}
    try:
        major = int(body.get("major") or 0)
    except Exception:
        return err("请指定 Java 版本")
    if not (8 <= major <= 30):
        return err("只支持自动补齐 Java 8 ~ 30（常用：8 / 17 / 21 / 25）")
    force = bool(body.get("force"))

    def _worker(task):
        info = core.ensure_java(major, task, force=force)
        if info:
            task.message = "Java %s 已就绪" % info.get("major")
        else:
            task.state = core.TaskState.FAILED
            task.error = "自动补齐 Java 失败"

    task = core.tasks.create("java", "自动补齐 Java %d" % major, "")
    core.tasks.start(task, _worker)
    return ok(task.to_dict())


@app.get("/api/java/plan")
def api_java_plan():
    """「补齐全部」的计划：MC 1.13 ~ 26.1+ 需要哪些 Java，各自装没装。"""
    return ok({"plan": core.java_fill_plan(), "have": core.java_have_majors(),
               "dir": core.java_install_dir()})


@app.post("/api/java/fill")
def api_java_fill():
    """一次补齐多个 Java（默认补齐全部缺失的：8 / 17 / 21 / 25）。"""
    body = request.get_json(force=True, silent=True) or {}
    majors = body.get("majors")
    if majors is not None and not isinstance(majors, list):
        return err("majors 需要是数组")
    force = bool(body.get("force"))

    def _worker(task):
        res = core.fill_java_all(majors, task, force=force)
        if not res["ok"] and res["failed"]:
            task.state = core.TaskState.FAILED
            task.error = "Java " + "、".join(str(m) for m in res["failed"]) + " 补齐失败"
        elif res["ok"]:
            task.message = "已补齐 Java " + "、".join(str(m) for m in res["ok"])
        else:
            task.message = "这些 Java 都已经装好了"

    task = core.tasks.create("java", "补齐全部 Java（8 / 17 / 21 / 25）", "")
    core.tasks.start(task, _worker)
    return ok(task.to_dict())


# ---------------------------------------------------------------- 内存
@app.get("/api/memory/info")
def api_memory_info():
    """实时内存体检：总量 / 此刻可用 / 占用率 / 建议值 / 原因说明。"""
    vid = (request.args.get("version") or "").strip()
    return ok(core.memory_advice(None, vid))


@app.post("/api/memory/optimize")
def api_memory_optimize():
    """一键内存优化：按此刻真实可用内存算出最优值，直接写进配置。"""
    body = request.get_json(force=True, silent=True) or {}
    vid = str(body.get("version") or "").strip()
    fix_args = bool(body.get("fixArgs"))
    before = core.memory_advice(None, vid)
    adv = core.optimize_memory(None, vid, fix_args=fix_args)
    adv["before"] = int(before.get("current") or 0)
    _cfg = core.load()
    _vs = (_cfg.get("versionSettings") or {}).get(vid) or {}
    adv["jvmArgs"] = str(_vs.get("jvmArgs") if _vs.get("jvmArgs") is not None
                         else _cfg.get("jvmArgs") or "")
    return ok(adv)
