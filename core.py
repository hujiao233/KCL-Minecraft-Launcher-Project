# -*- coding: utf-8 -*-
"""
脆冰启动器 - 核心模块
合并了配置、任务状态、下载器、Java 探测、离线账户五个部分。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests


# ==================================================================
# 来自 config.py
# ==================================================================

APP_NAME = "CuibingLauncher"
APP_VERSION = "1.2.2"

# 启动器本体数据目录：%APPDATA%/CuibingLauncher
ROOT_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(ROOT_DIR, "launcher.json")
LOGS_DIR = os.path.join(ROOT_DIR, "logs")

_lock = threading.Lock()

# 启动器程序所在目录（KCL.exe / main.py 所在的那个文件夹）
# 打包成 exe 时 sys.executable 就是 exe 本体，源码运行时用本文件位置
LAUNCH_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))


def _dir_usable(d: str) -> bool:
    """目录能不能建、能不能写（装在 Program Files 时启动器同级目录可能是只读的）。"""
    try:
        parent = d if os.path.isdir(d) else (os.path.dirname(d) or ".")
        return os.access(parent, os.W_OK)
    except Exception:      # noqa: BLE001
        return False


# 默认游戏目录：启动器同级的 .minecraft（便携——整个文件夹拷到 U 盘也能带走）
# 启动器同级目录不可写时（例如装在 Program Files）才退回官方位置
PORTABLE_GAME_DIR = os.path.join(LAUNCH_DIR, ".minecraft")
FALLBACK_GAME_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), ".minecraft"
)
DEFAULT_GAME_DIR = (PORTABLE_GAME_DIR if _dir_usable(PORTABLE_GAME_DIR)
                    else FALLBACK_GAME_DIR)

DEFAULT_CONFIG: Dict[str, Any] = {
    "gameDir": DEFAULT_GAME_DIR,
    "gameDirSelected": False,     # 是否已手动指定过游戏目录（首次自动检测）
    "javaPath": "",               # 留空 = 自动选择
    "maxMemory": 4096,            # MB
    "windowWidth": 1280,
    "windowHeight": 800,
    "fullscreen": False,
    "downloadSource": "bmclapi",  # bmclapi | official
    "maxDownloadThreads": 16,
    "accounts": [],               # [{name, uuid, type, ...}]
    "activeAccount": "",          # 当前选中的账户名
    "lastVersion": "",            # 上次选择的版本
    "closeAfterLaunch": False,
    "jvmArgs": "",
    # ---------- 版本隔离与版本级设置 ----------
    "isolateVersion": False,      # 全局：版本隔离（每个版本独立 mods/saves）
    "versionSettings": {},        # { vid: { key: value } } 未设置的项跟随主设置
    # ---------- 登录 ----------
    "authSource": "offline",      # offline | yggdrasil
    "authServer": "",             # 自定义登录源，如 https://littleskin.cn/api/yggdrasil
    "authServerName": "",
    "authRegister": "https://littleskin.cn/auth/register",   # 注册地址（选填）
    "authLabel": "",              # 登录方式名称，如 "littleskin登录"（选填）
    "authInjector": True,         # 外置登录时注入 authlib-injector
    # ---------- 资源平台 ----------
    "resourceSource": "auto",     # auto | modrinth | curseforge | mczwlt
    "resourceMirror": True,       # 资源平台走国内镜像
    "curseforgeKey": "",
    # ---------- 界面 ----------
    "language": "auto",           # auto | zh | en
    "themePreset": "ice",         # ice | midnight | forest | sunset | sakura | custom
    "themeAccent": "#8FD8FF",     # 主题色
    "themeDark": False,           # 自定义主题色时是深色界面还是浅色（跟着选过的预设走）
    "titleAlign": "left",         # 顶栏标题位置 left | center | right
    "uiScale": "auto",            # auto = 按 1020×680 等比缩放；off = 自由拉伸
    # ---------- 启动行为 ----------
    "autoMemory": True,           # 按机器内存自动分配
    "autoServer": False,          # 启动游戏时同时启动本地服务端
    "updateCheck": True,          # 启动时检查更新（非强制）
    "updateUrl": "",
    "lastUpdate": {},
    "changelogSeenVersion": "",   # 用户已看过的更新说明版本号，等于当前版本时不再弹窗
    # ---------- 网站连接 ----------
    "offlineMode": False,         # 关闭与外界连接：不连 KCL 服务器（更新/开发空间），其它下载正常
    "siteUrl": "",                # 脆冰网站服务端地址（开发空间等）
    # ---------- 界面布局 ----------
    # 自由布局：{ 页面: { 面板key: {x,y,w,h,z} } }，空表示用默认流式排版
    "uiLayout": {},
}

# 主题预设：名称 -> (主题色, 界面底色, 卡片底色)
THEME_PRESETS: Dict[str, Dict[str, str]] = {
    "ice":      {"accent": "#8FD8FF", "bg": "#EAF3FA", "panel": "#FFFFFF", "deep": "#1E4E6B"},
    "midnight": {"accent": "#7C8CFF", "bg": "#141A24", "panel": "#1D2530", "deep": "#B9C6FF"},
    "forest":   {"accent": "#6FBF8F", "bg": "#EDF5EF", "panel": "#FFFFFF", "deep": "#24563A"},
    "sunset":   {"accent": "#FF9E6B", "bg": "#FDEEE6", "panel": "#FFFFFF", "deep": "#7A3E20"},
    "sakura":  {"accent": "#FF9FC4", "bg": "#FCEFF4", "panel": "#FFFFFF", "deep": "#7A3350"},
}


def total_system_memory_mb() -> int:
    """读取物理内存总量（MB），失败返回 8192。"""
    try:
        import ctypes

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
    except Exception:  # noqa: BLE001
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 / 1024)
        except Exception:  # noqa: BLE001
            return 8192


def system_memory_info() -> Dict[str, Any]:
    """
    实时内存状态：总量 / 当前可用 / 已占用 / 占用率（MB，占用率为百分数）。
    Windows 走 GlobalMemoryStatusEx（和任务管理器一致），其它平台退化到只读总量。
    """
    total = avail = 0
    load = 0
    try:
        import ctypes

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
        total = int(stat.ullTotalPhys / 1024 / 1024)
        avail = int(stat.ullAvailPhys / 1024 / 1024)
        load = int(stat.dwMemoryLoad)
    except Exception:  # noqa: BLE001
        total = avail = 0
    if not total:
        total = total_system_memory_mb()
        avail = max(1024, int(total * 0.45))
        load = 55
    used = max(0, total - avail)
    if not load and total:
        load = min(100, int(used * 100 / total))
    return {"total": total, "available": avail, "used": used, "load": load}


def _mem_round(mb: int) -> int:
    """把内存值收拢到好记的档位（1024 以下按 256，8G 以下按 512，以上按 1024）。"""
    mb = int(mb)
    step = 256 if mb <= 2048 else (512 if mb <= 8192 else 1024)
    return max(1024, (mb // step) * step)


def _mem_base_by_total(total: int) -> int:
    """按物理内存总量给出的基准推荐值（不考虑当前占用）。"""
    for threshold, value in ((4 * 1024, 1536), (6 * 1024, 2048), (8 * 1024, 3072),
                             (12 * 1024, 3584), (16 * 1024, 4096), (24 * 1024, 5120),
                             (32 * 1024, 6144), (48 * 1024, 7168)):
        if total <= threshold:
            return value
    return 8192


def _mem_reserve(total: int) -> int:
    """必须留给系统和其他程序的内存，游戏不能碰。"""
    if total <= 4 * 1024:
        return 1024
    if total <= 6 * 1024:
        return 1280
    if total <= 8 * 1024:
        return 1536
    if total <= 16 * 1024:
        return 2048
    if total <= 32 * 1024:
        return 3072
    return 4096


def memory_advice(cfg: Dict[str, Any] | None = None, vid: str = "") -> Dict[str, Any]:
    """
    实时内存体检：结合「物理内存总量」和「此刻真正可用的内存」给出建议值。
    返回给界面展示用的完整报告（总量、可用、占用率、系统预留、基准值、上限、建议值、原因）。
    """
    cfg = cfg or load()
    info = system_memory_info()
    total, avail, load_pct = info["total"], info["available"], info["load"]

    base = _mem_base_by_total(total)
    reserve = _mem_reserve(total)
    cap = _mem_round(avail - reserve)          # 此刻最多能给游戏多少
    recommended = _mem_round(min(base, cap))
    recommended = max(1024, min(recommended, 16384))

    if vid:
        st = version_settings(vid, cfg)
        custom = (st.get("_custom") or {}).get("maxMemory")
        current = int(custom if custom is not None else st.get("maxMemory") or 4096)
    else:
        current = int(cfg.get("maxMemory") or 4096)

    reasons: List[str] = []
    level = "ok"
    if cap < base:
        reasons.append("此刻可用内存 %d MB，扣掉系统预留 %d MB 后，最多能给游戏 %d MB"
                       % (avail, reserve, cap))
        level = "tight" if cap < base * 0.6 else "warn"
    else:
        reasons.append("可用内存充足（%d MB），按物理内存 %d GB 的推荐值分配"
                       % (avail, max(1, round(total / 1024))))
    if load_pct >= 85:
        reasons.append("内存占用已达 %d%%，建议先关掉占内存的程序再启动" % load_pct)
        level = "tight"
    elif load_pct >= 70:
        reasons.append("内存占用 %d%%，已经偏高，游戏里别再开太多后台" % load_pct)
        if level == "ok":
            level = "warn"

    if recommended > current:
        reasons.append("比当前设置（%d MB）多 %d MB，更少卡顿" % (current, recommended - current))
    elif recommended < current:
        reasons.append("比当前设置（%d MB）少 %d MB，留给系统更从容，避免被系统杀进程"
                       % (current, current - recommended))
    else:
        reasons.append("当前设置（%d MB）已经是此刻的最优值" % current)

    # 附加 JVM 参数里写死的 -Xmx 会盖掉上面所有计算，必须让用户知道
    override = None
    if vid:
        extra = str(version_settings(vid, cfg).get("jvmArgs") or cfg.get("jvmArgs") or "")
    else:
        extra = str(cfg.get("jvmArgs") or "")
    m = re.search(r"-Xmx\s*(\d+)\s*([kmg])?", extra, re.I)
    if m:
        val = int(m.group(1))
        override = {"xmx": val, "raw": m.group(0).strip()}
        if val != recommended:   # 已经同步过就不再打扰
            reasons.append("注意：附加 JVM 参数里写死了 %s，它会盖掉这里的设置"
                           "（想让自动分配生效，把它删掉或点「同步」）" % m.group(0).strip())
            if level == "ok":
                level = "warn"

    return {
        "total": total, "available": avail, "used": info["used"], "load": load_pct,
        "reserve": reserve, "base": base, "cap": cap,
        "recommended": recommended, "current": current,
        "auto": cfg.get("autoMemory", True) is not False,
        "level": level, "reasons": reasons, "version": vid or "",
        "override": override,
    }


def optimize_memory(cfg: Dict[str, Any] | None = None, vid: str = "",
                    fix_args: bool = False) -> Dict[str, Any]:
    """
    一键内存优化：按此刻的真实可用内存算出最优值并写进配置。
    vid 为空写主设置，否则只写该版本的版本设置。
    fix_args=True 时，顺手把附加 JVM 参数里写死的 -Xmx 同步成新值。
    """
    cfg = cfg or load()
    adv = memory_advice(cfg, vid)
    rec = int(adv["recommended"])
    changed = False

    if vid:
        vs = cfg.setdefault("versionSettings", {}).setdefault(vid, {})
        if vs.get("maxMemory") != rec:
            vs["maxMemory"] = rec
            changed = True
    else:
        if int(cfg.get("maxMemory") or 0) != rec:
            cfg["maxMemory"] = rec
            changed = True
    if cfg.get("autoMemory") is not True:
        cfg["autoMemory"] = True
        changed = True

    # 附加参数里写死的 -Xmx 会盖掉一切，一并同步
    args_fixed = False
    if fix_args and adv.get("override"):
        if vid:
            holder = cfg.setdefault("versionSettings", {}).setdefault(vid, {})
            src = str(holder.get("jvmArgs") or cfg.get("jvmArgs") or "")
        else:
            holder = cfg
            src = str(cfg.get("jvmArgs") or "")
        new_args = re.sub(r"-Xmx\s*\d+\s*[kmgKMG]?", "-Xmx%dm" % rec, src, count=1)
        if new_args != src:
            holder["jvmArgs"] = new_args
            changed = True
            args_fixed = True

    if changed:
        save(cfg)

    adv["previous"] = int(adv.get("current") or 0)
    adv["current"] = rec
    adv["applied"] = changed
    adv["argsFixed"] = args_fixed
    if args_fixed:
        adv["override"] = None
        adv["reasons"] = [r for r in adv["reasons"] if "写死" not in r] + \
                         ["已把附加参数里的 -Xmx 同步成 %d MB" % rec]
    return adv


def live_memory_mb(cfg: Dict[str, Any] | None = None) -> int:
    """启动那一刻的实时分配值：按当前可用内存算，失败则退回按总量的推荐。"""
    try:
        return int(memory_advice(cfg)["recommended"])
    except Exception:  # noqa: BLE001
        return auto_memory_mb(cfg)


def auto_memory_mb(cfg: Dict[str, Any] | None = None) -> int:
    """
    按物理内存自动给出推荐上限（MB）：
      <=4G  -> 1024~2048
      8G    -> 3072
      16G   -> 4096
      32G   -> 6144
      >=64G -> 8192
    只按总量估算，不看当前占用；需要「实时」请改用 live_memory_mb()。
    """
    total = total_system_memory_mb()
    return _mem_base_by_total(total)

# 版本级可覆盖的设置项（值 None 表示「跟随主设置」）
VERSION_SETTING_KEYS = ("javaPath", "maxMemory", "jvmArgs", "windowWidth",
                        "windowHeight", "fullscreen", "isolate", "gameDir")

# 首次运行时自动挑选游戏目录
def auto_detect_game_dir() -> str:
    """首次启动自动探测：优先启动器同级的 .minecraft，不行再退回官方位置。"""
    for cand in (PORTABLE_GAME_DIR, FALLBACK_GAME_DIR, os.path.join(ROOT_DIR, "game")):
        if _dir_usable(cand):
            return cand
    return FALLBACK_GAME_DIR


def ensure_dirs() -> None:
    os.makedirs(ROOT_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)


def load() -> Dict[str, Any]:
    ensure_dirs()
    cfg = dict(DEFAULT_CONFIG)
    first_run = not os.path.isfile(CONFIG_PATH)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            # 配置损坏就用默认值兜底，不阻塞启动
            pass
    # 首次运行：自动挑选游戏目录（之后用户可手动改）
    if first_run and not cfg.get("gameDirSelected"):
        cfg["gameDir"] = auto_detect_game_dir()
        cfg["gameDirSelected"] = False   # 标记为「自动」，用户手动选择后置 True
        save(cfg)
    # 老用户迁移：游戏目录此前是自动分配的（没手动选过）就改到启动器同级的 .minecraft，
    # 手动指定过的永远听用户的，不动
    elif (not cfg.get("gameDirSelected")
          and os.path.normcase(os.path.abspath(cfg.get("gameDir") or ""))
          != os.path.normcase(os.path.abspath(PORTABLE_GAME_DIR))
          and _dir_usable(PORTABLE_GAME_DIR)):
        cfg["gameDir"] = PORTABLE_GAME_DIR
        save(cfg)
    if not cfg.get("gameDir"):
        cfg["gameDir"] = DEFAULT_GAME_DIR
    return cfg


def save(cfg: Dict[str, Any]) -> None:
    ensure_dirs()
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并式更新配置，返回最新完整配置。"""
    cfg = load()
    for k, v in patch.items():
        if k in DEFAULT_CONFIG or k not in cfg:
            cfg[k] = v
    save(cfg)
    return cfg


# ---------- 游戏目录相关路径 ----------

def game_dir(cfg: Dict[str, Any] | None = None) -> str:
    cfg = cfg or load()
    d = cfg.get("gameDir") or DEFAULT_GAME_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:      # noqa: BLE001 - 目录不可写（如装在 Program Files）时退回官方位置
        d = FALLBACK_GAME_DIR
        os.makedirs(d, exist_ok=True)
    return d


def versions_dir(cfg: Dict[str, Any] | None = None) -> str:
    d = os.path.join(game_dir(cfg), "versions")
    os.makedirs(d, exist_ok=True)
    return d


def libraries_dir(cfg: Dict[str, Any] | None = None) -> str:
    d = os.path.join(game_dir(cfg), "libraries")
    os.makedirs(d, exist_ok=True)
    return d


def assets_dir(cfg: Dict[str, Any] | None = None) -> str:
    d = os.path.join(game_dir(cfg), "assets")
    os.makedirs(d, exist_ok=True)
    return d


# ==================================================================
# 老游戏目录 → 新游戏目录 的文件转移
# ==================================================================

# 只搬这些顶层目录（游戏本体、运行库、资源、存档、mod 之类）
MIGRATE_ITEMS = (
    "versions", "libraries", "assets", "mods", "saves", "resourcepacks",
    "shaderpacks", "config", "crash-reports", "screenshots", "backups",
    "schematics", "datapacks", "logs",
)
# 顶层散着的配置文件
MIGRATE_FILES = (
    "options.txt", "servers.dat", "hotbar.nbt", "usercache.json",
    "usernamecache.json", "launcher_profiles.json", "launcher_accounts.json",
)


def _path_size(p: str) -> int:
    """文件或目录的总字节数。"""
    try:
        if os.path.isfile(p):
            return os.path.getsize(p)
    except OSError:
        return 0
    total = 0
    for dp, _dn, fn in os.walk(p):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def _drive_of(p: str) -> str:
    return os.path.splitdrive(os.path.abspath(p))[0].upper()


def _human_mb(n: int) -> str:
    return "%.1f MB" % (n / 1024 / 1024)


def migrate_plan(src: str = "", dst: str = "") -> Dict[str, Any]:
    """列出能从老目录搬到新目录的东西（只统计真实存在的），先给个数让用户确认。"""
    src = os.path.abspath(src or FALLBACK_GAME_DIR)
    dst = os.path.abspath(dst or (load().get("gameDir") or DEFAULT_GAME_DIR))
    if not os.path.isdir(src):
        return {"ok": False, "msg": "源目录不存在：" + src,
                "src": src, "dst": dst, "items": [], "total": 0}
    if os.path.normcase(src) == os.path.normcase(dst):
        return {"ok": False, "msg": "源目录和目标目录是同一个，不用搬",
                "src": src, "dst": dst, "items": [], "total": 0}
    items, total = [], 0
    for name in MIGRATE_ITEMS + MIGRATE_FILES:
        p = os.path.join(src, name)
        if not os.path.exists(p):
            continue
        size = _path_size(p)
        total += size
        items.append({"name": name, "size": size, "sizeText": _human_mb(size),
                      "inDst": os.path.exists(os.path.join(dst, name))})
    return {"ok": True, "src": src, "dst": dst, "items": items,
            "total": total, "totalText": _human_mb(total),
            "sameDrive": _drive_of(src) == _drive_of(dst)}


def _merge_tree(src: str, dst: str, on_file=None) -> Tuple[int, int, int, int]:
    """把 src 合并进 dst：目标已存在的同名文件跳过（绝不覆盖），返回 (复制数, 跳过数, 失败数, 字节数)。"""
    copied = skipped = failed = nbytes = 0

    def _one(s: str, t: str):
        nonlocal copied, skipped, failed, nbytes
        if os.path.exists(t):
            skipped += 1
            return
        try:
            os.makedirs(os.path.dirname(t) or ".", exist_ok=True)
            shutil.copy2(s, t)
            copied += 1
            try:
                sz = os.path.getsize(t)
            except OSError:
                sz = 0
            nbytes += sz
            if on_file:
                on_file(t, sz)
        except Exception:      # noqa: BLE001
            failed += 1

    if os.path.isfile(src):
        _one(src, dst)
        return copied, skipped, failed, nbytes
    for dp, _dn, fn in os.walk(src):
        rel = os.path.relpath(dp, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception:      # noqa: BLE001
            pass
        for f in fn:
            _one(os.path.join(dp, f), os.path.join(target_dir, f))
    return copied, skipped, failed, nbytes


def migrate_game_dir(src: str = "", dst: str = "", move: bool = False,
                     on_progress=None) -> Dict[str, Any]:
    """
    把老游戏目录里的东西搬到新游戏目录。

    move=False（默认）：复制过去，老目录原样留着，确认没问题再自己删——最稳。
    move=True        ：每个顶层项复制成功且零失败后，才删掉老目录里那一项。
    同名文件一律跳过，不覆盖目标里已有的内容；跨盘复制前会先检查空间够不够。
    """
    plan = migrate_plan(src, dst)
    if not plan.get("ok"):
        return plan
    src, dst = plan["src"], plan["dst"]

    if _drive_of(src) != _drive_of(dst) and plan["total"] > 0:
        try:
            free = shutil.disk_usage(os.path.dirname(dst) or dst).free
            if plan["total"] > free:
                return {"ok": False, "msg": "目标盘空间不足：需要 %s，可用 %s"
                        % (_human_mb(plan["total"]), _human_mb(free)),
                        "src": src, "dst": dst}
        except Exception:      # noqa: BLE001
            pass

    os.makedirs(dst, exist_ok=True)
    done_bytes = 0
    result: Dict[str, Any] = {
        "ok": True, "src": src, "dst": dst, "moved": [], "copied": [],
        "failed": [], "files": 0, "skipped": 0, "bytes": 0, "total": plan["total"],
    }

    for it in plan["items"]:
        name = it["name"]
        s, t = os.path.join(src, name), os.path.join(dst, name)

        def _cb(_p, sz, _name=name):
            nonlocal done_bytes
            done_bytes += sz
            if on_progress:
                on_progress(_name, _p, done_bytes, plan["total"])

        c, sk, fa, nb = _merge_tree(s, t, _cb)
        result["files"] += c
        result["skipped"] += sk
        result["bytes"] += nb
        if fa:
            result["failed"].append(name)
            result["ok"] = False
            continue
        if move:
            try:
                if os.path.isfile(s):
                    os.remove(s)
                else:
                    shutil.rmtree(s, ignore_errors=True)
                result["moved"].append(name)
            except Exception:      # noqa: BLE001
                result["copied"].append(name)
        else:
            result["copied"].append(name)

    result["bytesText"] = _human_mb(result["bytes"])
    result["msg"] = ("搬完 %d 个文件（%s）" % (result["files"], result["bytesText"])
                     if result["ok"] else "有内容没搬成功：" + "、".join(result["failed"]))
    return result


def version_dir(version_id: str, cfg: Dict[str, Any] | None = None) -> str:
    return os.path.join(versions_dir(cfg), version_id)


def version_json_path(version_id: str, cfg: Dict[str, Any] | None = None) -> str:
    return os.path.join(version_dir(version_id, cfg), f"{version_id}.json")


def version_jar_path(version_id: str, cfg: Dict[str, Any] | None = None) -> str:
    return os.path.join(version_dir(version_id, cfg), f"{version_id}.jar")


def short_path(p: str) -> str:
    """
    转成 8.3 短路径（PCL 的 PathUtils.ToShortPath）。
    中文 / 超长路径会让部分 Mod 与老版本 MC 直接崩，短路径能绕开。
    """
    if not p or not os.path.exists(p):
        return p
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(4096)
        n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 4096)  # type: ignore[attr-defined]
        if n and n < 4096 and buf.value:
            return buf.value
    except Exception:
        pass
    return p


def _is_ascii(p: str) -> bool:
    try:
        p.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def natives_dir(version_id: str, cfg: Dict[str, Any] | None = None) -> str:
    """
    natives 解压目录。
    借鉴 PCL：路径里只要出现非 ASCII 或过长，就换到一个纯 ASCII 的位置，
    否则 -Djava.library.path 带中文/超长路径很容易让游戏起不来。
    """
    d = os.path.join(version_dir(version_id, cfg), "natives")
    if _is_ascii(d) and len(d) < 150:
        os.makedirs(d, exist_ok=True)
        return d
    safe = os.path.join(ROOT_DIR, "natives", re.sub(r"[^\w.\-]", "_", str(version_id))[:60])
    if not _is_ascii(safe):
        safe = os.path.join("C:\\ProgramData", APP_NAME, "natives")
    try:
        os.makedirs(safe, exist_ok=True)
        return safe
    except OSError:
        os.makedirs(d, exist_ok=True)
        return d


# ---------- 账户 ----------

def list_accounts(cfg: Dict[str, Any] | None = None) -> List[Dict[str, str]]:
    cfg = cfg or load()
    return list(cfg.get("accounts") or [])


def find_account(name: str, cfg: Dict[str, Any] | None = None):
    for a in list_accounts(cfg):
        if a.get("name") == name:
            return a
    return None


# ==================================================================
# 来自 state.py
# ==================================================================

class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task:
    """一个后台任务（下载安装 / 资源补全）。"""

    def __init__(self, kind: str, title: str, target: str = ""):
        self.id: str = _uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.target = target
        self.state: TaskState = TaskState.PENDING
        self.total: int = 0
        self.done: int = 0
        self.current_file: str = ""
        self.speed: int = 0          # bytes/s（瞬时估算）
        self.message: str = ""
        self.error: str = ""
        self.result: Any = None      # 任务自己产出的结果（例如迁移报告）
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.done / self.total)

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "target": self.target,
            "state": self.state.value,
            "total": self.total,
            "done": self.done,
            "progress": round(self.progress, 4),
            "currentFile": self.current_file,
            "speed": self.speed,
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0,
        }


class TaskManager:
    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, title: str, target: str = "") -> Task:
        t = Task(kind, title, target)
        with self._lock:
            self._tasks[t.id] = t
        return t

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self):
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def start(self, task: Task, worker, *args, **kwargs):
        """启动任务线程。worker(task, *args) 中应自行更新 task 状态。"""
        task.state = TaskState.RUNNING
        task.started_at = time.time()

        def _run():
            try:
                worker(task, *args, **kwargs)
                if task.state == TaskState.RUNNING:
                    task.state = TaskState.SUCCESS
            except Exception as e:  # noqa: BLE001
                task.state = TaskState.FAILED
                task.error = f"{type(e).__name__}: {e}"
            finally:
                task.finished_at = time.time()

        t = threading.Thread(target=_run, daemon=True)
        task._thread = t
        t.start()
        return task


tasks = TaskManager()


# ==================================================================
# 来自 downloader.py
# ==================================================================

UA = "CuibingLauncher/0.1 (Minecraft Launcher)"


class DownloadItem:
    """
    一个待下载文件。urls 是候选地址列表，按顺序尝试——镜像节点不稳定时
    会自动切换到下一个源，这是国内网络下下载成功率的关键。
    """
    __slots__ = ("urls", "path", "size", "sha1", "name")

    def __init__(self, url, path: str, size: int = 0, sha1: str = "", name: str = "",
                 urls: Optional[List[str]] = None):
        if urls:
            self.urls = [u for u in urls if u]
        elif isinstance(url, (list, tuple)):
            self.urls = [u for u in url if u]
        else:
            self.urls = [url] if url else []
        self.path = path
        self.size = size
        self.sha1 = sha1
        self.name = name or os.path.basename(path)

    @property
    def url(self) -> str:
        return self.urls[0] if self.urls else ""


def sha1_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def file_ok(item: DownloadItem) -> bool:
    """已存在且校验通过则跳过下载。"""
    if not os.path.isfile(item.path):
        return False
    if item.size and os.path.getsize(item.path) != item.size:
        return False
    if item.sha1:
        try:
            return sha1_file(item.path) == item.sha1
        except Exception:
            return False
    return True


def download_one(
    item: DownloadItem,
    session: requests.Session,
    progress_cb: Optional[Callable[[int], None]] = None,
    timeout=(6, 20),
    retries: int = 3,
) -> None:
    """
    下载单个文件。每一轮依次尝试所有候选地址，全部失败才抛出最后一个异常。
    """
    os.makedirs(os.path.dirname(item.path) or ".", exist_ok=True)
    last_err: Optional[Exception] = None
    if not item.urls:
        raise RuntimeError(f"没有可用下载地址: {item.name}")

    for attempt in range(max(1, retries)):
        for url in item.urls:
            try:
                tmp = item.path + ".cbtmp"
                # Connection: close —— 每次请求都重新握手，避开被复用的坏节点连接
                with session.get(url, stream=True, timeout=timeout,
                                 headers={"User-Agent": UA, "Connection": "close"}) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            if not chunk:
                                continue
                            f.write(chunk)
                            if progress_cb:
                                progress_cb(len(chunk))

                # 完整性校验
                if item.sha1:
                    got = sha1_file(tmp)
                    if got != item.sha1:
                        os.remove(tmp)
                        raise ValueError(f"SHA1 不匹配: {item.name}")
                elif item.size and os.path.getsize(tmp) != item.size:
                    os.remove(tmp)
                    raise ValueError(f"大小不匹配: {item.name}")

                if os.path.exists(item.path):
                    os.remove(item.path)
                os.replace(tmp, item.path)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.3)

    raise last_err or RuntimeError(f"下载失败: {item.name}")


_thread_local = threading.local()


def _thread_session(pool: int = 16) -> requests.Session:
    """
    每个工作线程持有独立 Session。
    镜像站的坏节点会被 DNS 轮询命中，共享连接池会让坏连接被反复复用，
    因此这里按线程隔离，并在失败后重建。
    """
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=pool,
                                                pool_maxsize=pool, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return s


def _close_thread_session() -> None:
    s = getattr(_thread_local, "session", None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    _thread_local.session = None


def download_all(
    items: List[DownloadItem],
    workers: int = 16,
    on_file_done: Optional[Callable[[DownloadItem, int, int], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    retry_rounds: int = 2,
) -> Tuple[int, List[Tuple[str, str]]]:
    """
    并发下载全部条目，失败项会自动进行若干轮「低并发重试」。
    国内镜像常出现间歇性节点故障，重试轮次能显著提高安装成功率。
    返回 (成功数, 最终失败列表[(name, 错误说明)])
    """
    total = len(items)
    counter = {"done": 0}
    counter_lock = threading.Lock()
    remains: List[DownloadItem] = list(items)
    final_errors: List[Tuple[str, str]] = []

    for round_no in range(retry_rounds + 1):
        if not remains:
            break
        if should_stop and should_stop():
            break
        # 首轮跑满并发，重试轮降到 1/4 并发，更利于打散到健康节点
        w = max(1, min(workers if round_no == 0 else max(2, workers // 4), len(remains)))
        round_fail: List[Tuple[DownloadItem, str]] = []
        lock = threading.Lock()

        def _worker(item: DownloadItem):
            if should_stop and should_stop():
                return
            session = _thread_session(workers)
            try:
                download_one(item, session, progress_cb=on_progress,
                             timeout=(6, 20),
                             retries=(3 if round_no == 0 else 4))
            except Exception as e:  # noqa: BLE001
                # 丢弃可能残留坏连接的 Session，下次请求会重新解析节点
                _close_thread_session()
                with lock:
                    round_fail.append((item, str(e)))
            finally:
                with counter_lock:
                    counter["done"] += 1
                    n = counter["done"]
                if on_file_done:
                    on_file_done(item, n, total)

        with ThreadPoolExecutor(max_workers=w) as ex:
            futs = [ex.submit(_worker, it) for it in remains]
            for _ in as_completed(futs):
                pass

        remains = [it for it, _ in round_fail]
        final_errors = [(it.name, err) for it, err in round_fail]
        if remains and round_no < retry_rounds:
            time.sleep(0.6)

    ok = total - len(remains)
    return ok, final_errors


# ==================================================================
# 来自 java.py
# ==================================================================

_cache: Optional[List[Dict[str, Any]]] = None


def _scan_roots() -> List[str]:
    roots = []
    env = os.environ

    for key in ("JAVA_HOME", "JDK_HOME", "JRE_HOME"):
        v = env.get(key)
        if v:
            roots.append(os.path.join(v, "bin"))

    # 常见安装根目录
    bases = [
        env.get("ProgramFiles", r"C:\Program Files"),
        env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.path.join(env.get("APPDATA", ""), ".minecraft", "runtime"),
    ]
    vendors = [
        "Java", "Microsoft", "Eclipse Adoptium", "AdoptOpenJDK", "BellSoft",
        "Zulu", "Azul Systems", "Semeru", "Temurin", "graalvm", "OpenJDK",
        "Amazon", "Tencent", "Dragonwell", "Huawei", "Bisheng", "jdk",
    ]
    for b in bases:
        if not b or not os.path.isdir(b):
            continue
        for v in vendors:
            p = os.path.join(b, v)
            if os.path.isdir(p):
                roots.append(p)

    # MSIX 版本-framepath
    extra = env.get("PATH", "").split(os.pathsep)
    roots.extend([p for p in extra if "java" in p.lower()])
    return roots


def _collect_java_exes() -> List[str]:
    found: List[str] = []
    seen = set()
    dirs_done = set()

    def _add(path: str):
        if not path:
            return
        path = os.path.normpath(path)
        low = path.lower()
        if low in seen:
            return
        d = os.path.dirname(low)
        # 同一个目录里优先要 java.exe：javaw.exe 没有控制台输出，
        # 启动器拿不到游戏日志，崩溃分析也就无从谈起（PCL 也有同样的坑）
        if low.endswith("javaw.exe") and d in dirs_done:
            return
        seen.add(low)
        found.append(path)
        if low.endswith("java.exe"):
            dirs_done.add(d)

    def _scan_dir(d: str):
        for name in ("java.exe", "javaw.exe"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                _add(p)

    for root in _scan_roots():
        if not os.path.isdir(root):
            continue
        _scan_dir(root)
        # 二级目录：…/jdk-17/bin/java.exe
        try:
            for entry in os.listdir(root)[:200]:
                sub = os.path.join(root, entry, "bin")
                if os.path.isdir(sub):
                    _scan_dir(sub)
        except Exception:
            pass

    # PATH 中的 java
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and os.path.isdir(p):
            _scan_dir(p)
    return found


def _probe(exe: str) -> Optional[Dict[str, Any]]:
    """执行 java -version 获取版本与厂商信息。"""
    try:
        out = subprocess.run(
            [exe, "-version"], capture_output=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (out.stderr or b"").decode("utf-8", "ignore") + \
               (out.stdout or b"").decode("utf-8", "ignore")
    except Exception:
        return None
    if not text:
        return None

    version = ""
    m = re.search(r'"(\d+(?:\.\d+)*)[^"]*"', text)
    if m:
        version = m.group(1)
    major = 0
    try:
        major = int(version.split(".")[0])
    except Exception:
        major = 0
    if major == 1:
        try:
            major = int(version.split(".")[1])
        except Exception:
            pass

    vendor = ""
    vm = re.search(r"\((?:build [^,]+,\s*)?([^,)]+?)(?:,|\))", text)
    if vm:
        vendor = vm.group(1).strip()

    arch = "x64" if "64-Bit" in text or "64-bit" in text else "x86"
    bits = 64 if arch == "x64" else 32
    if not major:
        return None
    return {
        "path": exe,
        "version": version,
        "major": major,
        "vtuple": _v_tuple(version),
        "vendor": vendor,
        "arch": arch,
        "bits": bits,
        # javaw.exe 不产生标准输出，日志会断流；把它换掉能拿到完整日志
        "console": os.path.basename(exe).lower() == "java.exe",
    }


def _v_tuple(v: Any) -> Tuple[int, int, int, int]:
    """把 Java 版本统一成可比较的 4 元组（1.8.0_402 -> 8.0.402.0）。"""
    if isinstance(v, (tuple, list)):
        out = [int(x) for x in v][:4]
        return tuple((out + [0, 0, 0, 0])[:4])
    nums = re.findall(r"\d+", str(v or ""))
    if not nums:
        return (0, 0, 0, 0)
    out = [int(x) for x in nums[:4]]
    if out and out[0] == 1 and len(out) > 1:
        out = out[1:] + [0]
    return tuple((out + [0, 0, 0, 0])[:4])


def _java_score(info: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """
    Java 推荐度排序（借鉴 PCL 的 SortAsync）：
    64 位优先 > 主版本贴合常用需求优先 > 修订号新优先 > 有控制台输出优先。
    """
    major = int(info.get("major") or 0)
    # 8 / 17 / 21 是 MC 生态里用得最多的三个主版本
    pref = {8: 100, 17: 99, 21: 98, 16: 88, 11: 80}.get(major, 0)
    if not pref:
        pref = max(10, 70 - abs(major - 17) * 5)
    return (
        1 if int(info.get("bits") or 0) >= 64 else 0,
        pref,
        int(info.get("vtuple", (0, 0, 0, 0))[2] or 0),
        1 if info.get("console") else 0,
    )


def list_java(force: bool = False) -> List[Dict[str, Any]]:
    """列出本机所有可用 Java，按推荐度排序（64 位 + 常用主版本优先）。"""
    global _cache
    if _cache and not force:
        return _cache
    out: List[Dict[str, Any]] = []
    seen_ver = set()
    for exe in _collect_java_exes():
        info = _probe(exe)
        if not info:
            continue
        out.append(info)
    # 去重/排序
    out.sort(key=_java_score, reverse=True)
    res: List[Dict[str, Any]] = []
    for i in out:
        key = (i["version"], i["vendor"], i["bits"])
        if key in seen_ver:
            continue
        seen_ver.add(key)
        res.append(i)
    _cache = res
    return res


def resolve_java(preferred: str = "") -> Optional[Dict[str, Any]]:
    """优先使用用户指定路径，否则选一个可用的。"""
    if preferred and os.path.isfile(preferred):
        info = _probe(preferred)
        if info:
            return info
    lst = list_java()
    return lst[0] if lst else None


# 不同 MC 版本所需的 Java 主版本
def recommended_major(mc_version: str) -> int:
    """
    返回建议的 Java 主版本：
    1.16 及以下 -> 8；1.17~1.20.4 -> 17；1.20.5+ -> 21
    """
    def _tuple(v: str):
        parts = re.findall(r"\d+", v)
        try:
            return tuple(int(p) for p in parts[:3])
        except Exception:
            return (0,)

    t = _tuple(mc_version)
    v10000 = (t[1] if len(t) > 1 else 0)
    v10 = (t[2] if len(t) > 2 else 0)
    if len(t) == 0:
        return 8
    # 快照名/远古版本（如 1.13、b1.7.3）单独判段
    if t[0] > 1:
        return 21
    if v10000 >= 21:
        return 21
    if v10000 >= 20 and v10 >= 5:
        return 21
    if v10000 >= 17:
        return 17
    return 8


def check_java_for_version(java: Dict[str, Any], mc_version: str) -> Dict[str, Any]:
    """给出 Java 与版本是否匹配的提示。"""
    need = recommended_major(mc_version)
    cur = java.get("major", 0)
    if cur == need:
        return {"ok": True, "need": need, "level": "ok", "msg": ""}
    if cur > need:
        return {"ok": True, "need": need, "level": "warn",
                "msg": f"建议使用 Java {need}，当前为 Java {cur}，部分旧版本可能异常"}
    return {"ok": False, "need": need, "level": "error",
            "msg": f"需要 Java {need} 或更高版本，当前为 Java {cur}"}


# ==================================================================
# Java 需求区间与自动选择（架构借鉴 PCL 的 ModJava.vb / ModLaunch.vb）
#
# PCL 的关键做法：不是给每个 MC 版本定死一个「推荐 Java 版本」，而是算出
# 一个 **版本区间**（下界 / 上界），再在排序好的 Java 列表里挑第一个落在
# 区间里的；列表里没有就重新扫一遍；还没有就按区间自动下载一份。
# 这样 1.16.5 Forge（上限 Java 8）和 1.20.5（下限 Java 21）都能各自拿到对的 Java。
# ==================================================================

def _ge(a: str, b: str) -> bool:
    """两个点分版本号比较：a >= b。"""
    def _n(s: str) -> List[int]:
        out = []
        for p in re.findall(r"\d+", str(s or "")):
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return out
    x, y = _n(a), _n(b)
    n = max(len(x), len(y))
    x += [0] * (n - len(x))
    y += [0] * (n - len(y))
    return x >= y


def _vanilla_of(vid: str) -> str:
    """从版本名里抠出原版号：1.16.5-forge-36.2.26 -> 1.16.5；24w14a -> 24w14a。"""
    s = str(vid or "")
    m = re.match(r"^(\d{2}w\d{2}[a-z])", s)
    if m:
        return m.group(1)
    for m in re.finditer(r"(?<![\d.])(\d+\.\d+(?:\.\d+)?)", s):
        v = m.group(1)
        if v == "1" or v.startswith("1."):
            return v
    return ""


def _mc_tuple(mc: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", str(mc or ""))
    out = []
    for n in nums[:3]:
        try:
            out.append(int(n))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _release_time(vj: Dict[str, Any]):
    import datetime
    for key in ("releaseTime", "time"):
        raw = (vj or {}).get(key)
        if not raw:
            continue
        s = str(raw).replace("Z", "+00:00")
        try:
            return datetime.datetime.fromisoformat(s).replace(tzinfo=None)
        except Exception:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
            if m:
                try:
                    return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except Exception:
                    pass
    return None


def detect_loaders(vid: str, vj: Dict[str, Any] | None = None) -> Dict[str, str]:
    """识别版本上装了哪些加载器及其版本（forge / neoforge / fabric / optifine / liteloader）。"""
    out: Dict[str, str] = {}
    low = str(vid or "").lower()
    libs = (vj or {}).get("libraries") or []

    def from_libs(prefix: str) -> str:
        for lib in libs:
            name = str(lib.get("name") or "")
            if name.lower().startswith(prefix):
                return name.split(":")[-1]
        return ""

    def from_id(key: str) -> str:
        m = re.search(re.escape(key) + r"[-_ ]?v?(\d[\w.\-]*)", low)
        return m.group(1) if m else ""

    if "neoforge" in low:
        out["neoforge"] = from_libs("net.neoforged:neoforge:") or from_libs("net.neoforged:forge:") or from_id("neoforge")
    elif "forge" in low:
        out["forge"] = from_libs("net.minecraftforge:forge:") or from_id("forge")
    if "fabric" in low:
        out["fabric"] = from_libs("net.fabricmc:fabric-loader:") or from_id("fabric")
    if "quilt" in low:
        out["fabric"] = out.get("fabric") or from_libs("org.quiltmc:quilt-loader:") or "0.17.0"
    if "optifine" in low:
        out["optifine"] = from_id("optifine") or "1"
    if "liteloader" in low:
        out["liteloader"] = "1"
    return {k: v for k, v in out.items() if v}


def java_requirement(vid: str, vj: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    算出这个版本启动所需的 Java 版本区间。

    返回 {"min": 元组|None, "minInc": bool, "max": 元组|None, "maxInc": bool,
          "component": str|None, "notes": [...]}
    """
    vj = vj or {}
    rng: Dict[str, Any] = {"min": None, "minInc": True, "max": None,
                           "maxInc": False, "component": None, "notes": []}

    def add_min(t, inc: bool = True, why: str = ""):
        t = _v_tuple(t)
        if rng["max"] is not None:
            if t > rng["max"] or (t == rng["max"] and not (inc and rng["maxInc"])):
                rng["notes"].append("Java 版本要求冲突，已放宽上限（%s）" % (why or "以更高要求为准"))
                rng["max"] = None
        if rng["min"] is None or t > rng["min"]:
            rng["min"], rng["minInc"] = t, inc
        if why:
            rng["notes"].append(why)

    def add_max(t, inc: bool = False, why: str = ""):
        t = _v_tuple(t)
        if rng["min"] is not None:
            if t < rng["min"] or (t == rng["min"] and not (inc and rng["minInc"])):
                rng["notes"].append("Java 版本要求冲突，已放宽下限（%s）" % (why or "以加载器要求为准"))
                rng["min"] = None
        if rng["max"] is None or t < rng["max"]:
            rng["max"], rng["maxInc"] = t, inc
        if why:
            rng["notes"].append(why)

    mc = _vanilla_of(vid)
    vt = _mc_tuple(mc)
    rt = _release_time(vj)

    # ---------- 原版本身的要求 ----------
    if vt and vt[0] == 1 and len(vt) >= 2:
        minor, patch = vt[1], vt[2]
        if (minor, patch) >= (20, 5):
            add_min(21, True, "Minecraft 1.20.5+ 需要 Java 21")
        elif minor >= 18:
            add_min(17, True, "Minecraft 1.18+ 需要 Java 17")
        elif minor >= 17:
            add_min(16, True, "Minecraft 1.17+ 需要 Java 16")
        elif minor >= 12:
            add_min(8, True, "Minecraft 1.12+ 需要 Java 8")
        elif minor <= 7:
            # 1.7.10 及更早的版本在 Java 9+ 上必崩（PCL 只挡到 1.5.2，这里放宽一点）
            add_max(9, False, "Minecraft 1.7.10 及更早版本不支持 Java 9 及以上")
    elif rt:
        # 快照/非标准版本只能按发布时间判断
        if rt >= _dt(2024, 4, 2):
            add_min(21, True, "该快照需要 Java 21")
        elif rt >= _dt(2021, 11, 16):
            add_min(17, True, "该快照需要 Java 17")
        elif rt >= _dt(2021, 5, 11):
            add_min(16, True, "该快照需要 Java 16")
        elif rt.year >= 2017:
            add_min(8, True, "该版本需要 Java 8")
        elif rt <= _dt(2013, 5, 1) and rt.year >= 2001:
            add_max(9, False, "该版本不支持 Java 9 及以上")

    # ---------- 版本 JSON 里 Mojang 自己声明的要求 ----------
    jver = (vj.get("javaVersion") or {})
    code = jver.get("majorVersion") or vj.get("java_version") or 0
    try:
        code = int(code)
    except Exception:
        code = 0
    if code >= 22:
        add_min(code, True, "版本 JSON 要求 Java %d 及以上" % code)
        comp = jver.get("component") or vj.get("java_component") or ""
        rng["component"] = comp or None

    # ---------- 加载器的额外约束 ----------
    loaders = detect_loaders(vid, vj)
    if "optifine" in loaders and vt and vt[0] == 1:
        if vt[1] < 7:
            add_max(9, False, "OptiFine 1.7 以下不支持 Java 9+")
        elif 8 <= vt[1] < 12:
            add_min(8, True, "OptiFine 1.8~1.11 需要 Java 8")
            add_max(9, False, "OptiFine 1.8~1.11 只能用 Java 8")
        elif vt[1] == 12:
            add_max(9, False, "OptiFine 1.12 最高只支持 Java 8")

    forge = loaders.get("forge")
    if forge:
        mcv = (vt[1], vt[2]) if (vt and vt[0] == 1) else (0, 0)
        if (6, 1) <= mcv <= (7, 2):
            add_min(7, True, "Forge 1.6.1~1.7.2 需要 Java 7")
            add_max(8, False, "Forge 1.6.1~1.7.2 只能用 Java 7")
        elif not vt or vt[1] <= 12:
            add_max(9, False, "Forge 1.12 及以下最高只支持 Java 8")
        elif vt[1] <= 14:
            add_max(11, False, "Forge 1.13~1.14 最高只支持 Java 10")
        elif vt[1] == 15:
            add_max(16, False, "Forge 1.15 最高只支持 Java 15")
        elif _ge(forge, "34.0.0") and not _ge(forge, "36.2.26"):
            add_max((8, 0, 320), True, "Forge 34~36.2.25 最高只支持 Java 8u320")
        elif _ge(forge, "36.2.26") and not _ge(forge, "37.0.0"):
            add_max(24, False, "Forge 36.2.26+ 最高只支持 Java 23")
        elif _ge(forge, "37.0.0") and not _ge(forge, "37.0.80"):
            add_max(17, False, "Forge 37.0.0~37.0.79 最高只支持 Java 16")
        elif vt[1] == 18 and "optifine" in loaders:
            add_max(19, False, "Forge 1.18 + OptiFine 最高只支持 Java 18")
        elif _ge(forge, "45.0.21") and not _ge(forge, "45.0.66"):
            add_max(20, False, "Forge 45.0.21~45.0.65 最高只支持 Java 19")
        elif _ge(forge, "45.0.66") and not _ge(forge, "47.4.9"):
            add_max(22, False, "Forge 45.0.66~47.4.8 最高只支持 Java 21")

    neo = loaders.get("neoforge")
    if neo and vt and vt[0] == 1 and vt[1] == 20:
        if vt[2] <= 1 or not _ge(neo, "20.2.62"):
            add_max(22, False, "NeoForge 1.20.1/1.20.2 最高只支持 Java 21")

    fabric = loaders.get("fabric")
    if fabric:
        if vt and vt[0] == 1:
            if 15 <= vt[1] <= 16:
                add_min(8, True, "Fabric 1.15~1.16 需要 Java 8+")
            elif vt[1] >= 18:
                add_min(17, True, "Fabric 1.18+ 需要 Java 17+")
        if not _ge(fabric, "0.17.0"):
            add_max(25, False, "Fabric Loader 0.16 及更早版本不兼容 Java 25")

    if "liteloader" in loaders:
        add_max(9, False, "LiteLoader 最高只支持 Java 8")

    rng["loaders"] = loaders
    return rng


def _dt(y: int, m: int, d: int):
    import datetime
    return datetime.datetime(y, m, d)


def java_in_range(info: Dict[str, Any], rng: Dict[str, Any]) -> bool:
    """判断某个 Java 是否落在需求区间内。"""
    t = info.get("vtuple") or _v_tuple(info.get("version") or info.get("major"))
    lo, hi = rng.get("min"), rng.get("max")
    if lo is not None:
        if t < lo:
            return False
        if t == lo and not rng.get("minInc", True):
            return False
    if hi is not None:
        if t > hi:
            return False
        if t == hi and not rng.get("maxInc", False):
            return False
    return True


def _all_java(force: bool = False) -> List[Dict[str, Any]]:
    """本机 Java（含启动器自带）+ 排序去重。"""
    lst = list(list_java(force)) + [x for x in scan_bundled_java() if x]
    out: List[Dict[str, Any]] = []
    seen = set()
    for i in sorted(lst, key=_java_score, reverse=True):
        key = (os.path.normcase(str(i.get("path") or "")).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    return out


def _target_major_for_download(rng: Dict[str, Any]) -> int:
    """按需求区间决定自动下载哪个主版本的 Java。"""
    # 注意：不要靠组件名猜主版本（java-runtime-epsilon 现在是 25，以后还会变），
    # 组件名只用来在 Mojang 清单里精确命中，主版本一律按需求区间算。
    lo = rng.get("min") or (8, 0, 0, 0)
    major = max(8, int(lo[0]))
    hi = rng.get("max")
    if hi is not None:
        # 上限是开区间时取前一个主版本，闭区间时就取它本身
        cap = int(hi[0]) if rng.get("maxInc") else int(hi[0]) - 1
        major = max(8, min(major, cap))
    return major


def select_java(vid: str, vj: Dict[str, Any] | None = None,
                cfg: Dict[str, Any] | None = None,
                java_path: str = "", allow_download: bool = True,
                task=None) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    PCL 风格的选 Java 流程：
      指定路径 -> 列表内挑区间内的 -> 重新扫描再挑 -> 自动下载后挑 -> 兜底最接近的。
    返回 (java_info, requirement)
    """
    rng = java_requirement(vid, vj)

    # 1. 用户强制指定
    if java_path and os.path.isfile(java_path):
        info = _probe(java_path)
        if info:
            info["source"] = "指定"
            return info, rng

    # 2. 现有列表里挑第一个落在区间内的
    cands = _all_java()
    for i in cands:
        if java_in_range(i, rng):
            i["source"] = "自动选择"
            return i, rng

    # 3. 重新扫一遍（可能刚装的 Java 还没被缓存到）
    cands = _all_java(True)
    for i in cands:
        if java_in_range(i, rng):
            i["source"] = "自动选择"
            return i, rng

    # 4. 自动下载一份符合区间的 Java
    if allow_download:
        major = _target_major_for_download(rng)
        try:
            got = ensure_java(major, task, component=rng.get("component") or "")
        except Exception:
            got = None
        if got:
            # 装到的必须是能用的（区间内的），否则继续往下走放宽逻辑
            if java_in_range(got, rng) or not rng.get("min"):
                got["source"] = "自动下载 Java %d" % major
                return got, rng
        # 下载失败，退而求其次：区间放宽后再试一次列表
        for i in _all_java(True):
            if java_in_range(i, {"min": rng.get("min"), "minInc": True,
                                 "max": None, "maxInc": False}):
                i["source"] = "自动选择（已忽略上限）"
                return i, rng

    # 5. 兜底：离区间最近的那个
    if cands:
        lo = rng.get("min") or (8, 0, 0, 0)
        best = sorted(cands, key=lambda i: abs((i.get("vtuple") or (0, 0, 0, 0))[0] - lo[0]))[0]
        best["source"] = "兜底选择"
        return best, rng
    return None, rng


def prefer_console_java(info: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    把 javaw.exe 换成同目录的 java.exe。
    javaw.exe 不挂控制台，启动器读不到游戏输出，日志和崩溃分析会全部失灵。
    """
    if not info:
        return info
    p = str(info.get("path") or "")
    if os.path.basename(p).lower() != "javaw.exe":
        return info
    alt = os.path.join(os.path.dirname(p), "java.exe")
    if not os.path.isfile(alt):
        return info
    got = _probe(alt)
    if not got:
        return info
    got.update({k: v for k, v in info.items() if k not in got})
    got["path"] = alt
    got["console"] = True
    return got


# ==================================================================
# 来自 auth.py
# ==================================================================

NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def offline_uuid(name: str) -> str:
    """按 Mojang 离线规范生成 UUID（与单机/离线服保持一致）。"""
    digest = hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest()
    b = bytearray(digest)
    b[6] = (b[6] & 0x0F) | 0x30     # version 3
    b[8] = (b[8] & 0x3F) | 0x80     # RFC 4122 variant
    return str(_uuid.UUID(bytes=bytes(b)))


def validate_name(name: str) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "msg": "用户名不能为空"}
    if len(name) < 3 or len(name) > 16:
        return {"ok": False, "msg": "用户名需为 3-16 个字符"}
    if not NAME_RE.match(name):
        return {"ok": False, "msg": "只能包含字母、数字和下划线"}
    return {"ok": True, "msg": ""}


def make_offline(name: str) -> Dict[str, str]:
    return {
        "name": name,
        "uuid": offline_uuid(name),
        "type": "offline",
    }


def add_account(name: str) -> Dict[str, Any]:
    """新增离线账户（重名则视为已存在）。"""
    chk = validate_name(name)
    if not chk["ok"]:
        return {"ok": False, "msg": chk["msg"]}

    cfg = load()
    accounts: List[Dict[str, str]] = cfg.get("accounts") or []
    if any(a.get("name") == name for a in accounts):
        return {"ok": False, "msg": f"用户 {name} 已存在"}

    acc = make_offline(name)
    accounts.append(acc)
    cfg["accounts"] = accounts
    if not cfg.get("activeAccount"):
        cfg["activeAccount"] = name
    save(cfg)
    return {"ok": True, "account": acc}


def rename_account(old: str, new: str) -> Dict[str, Any]:
    """修改离线用户名（同时更新 UUID）。"""
    chk = validate_name(new)
    if not chk["ok"]:
        return {"ok": False, "msg": chk["msg"]}

    cfg = load()
    accounts = cfg.get("accounts") or []
    target: Optional[Dict[str, str]] = None
    for a in accounts:
        if a.get("name") == old:
            target = a
        elif a.get("name") == new:
            return {"ok": False, "msg": f"用户 {new} 已存在"}
    if not target:
        return {"ok": False, "msg": f"找不到用户 {old}"}

    target["name"] = new
    target["uuid"] = offline_uuid(new)
    if cfg.get("activeAccount") == old:
        cfg["activeAccount"] = new
    save(cfg)
    return {"ok": True, "account": target}


def remove_account(name: str) -> Dict[str, Any]:
    cfg = load()
    accounts = [a for a in (cfg.get("accounts") or []) if a.get("name") != name]
    if len(accounts) == len(cfg.get("accounts") or []):
        return {"ok": False, "msg": f"找不到用户 {name}"}
    cfg["accounts"] = accounts
    if cfg.get("activeAccount") == name:
        cfg["activeAccount"] = accounts[0]["name"] if accounts else ""
    save(cfg)
    return {"ok": True}


def get_active(cfg: Dict[str, Any] | None = None) -> Optional[Dict[str, str]]:
    cfg = cfg or load()
    accounts = cfg.get("accounts") or []
    if not accounts:
        return None
    name = cfg.get("activeAccount", "")
    for a in accounts:
        if a.get("name") == name:
            return a
    return accounts[0]


# ==================================================================
# 版本级设置 & 版本隔离
# ==================================================================

def version_settings(vid: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    取某个版本生效的设置。
    规则：版本设置里没有动过的项，自动跟随主（全局）设置。
    """
    cfg = cfg or load()
    raw = (cfg.get("versionSettings") or {}).get(vid) or {}
    eff: Dict[str, Any] = {
        "javaPath": cfg.get("javaPath", ""),
        "maxMemory": cfg.get("maxMemory", 4096),
        "jvmArgs": cfg.get("jvmArgs", ""),
        "windowWidth": cfg.get("windowWidth", 1280),
        "windowHeight": cfg.get("windowHeight", 800),
        "fullscreen": bool(cfg.get("fullscreen", False)),
        "gameDir": cfg.get("gameDir") or DEFAULT_GAME_DIR,
        "isolate": bool(cfg.get("isolateVersion", False)),
    }
    for k in VERSION_SETTING_KEYS:
        if k in raw and raw[k] is not None:
            eff[k] = raw[k]
    eff["_custom"] = {k: raw.get(k) for k in VERSION_SETTING_KEYS
                      if k in raw and raw[k] is not None}
    eff["_isolateGlobal"] = bool(cfg.get("isolateVersion", False))
    # 是否隔离：手动设置 > 自动识别（版本目录已有 mod/存档）> 主设置
    try:
        iso = resolve_isolate(vid, cfg)
        eff["isolate"] = bool(iso["isolated"])
        eff["_isolateInfo"] = iso
    except Exception:  # noqa: BLE001
        pass
    return eff


def set_version_settings(vid: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """写入版本设置；值为 None 表示「恢复跟随主设置」。"""
    cfg = load()
    vs = cfg.setdefault("versionSettings", {}) or {}
    cur = dict(vs.get(vid) or {})
    for k, v in (patch or {}).items():
        if k not in VERSION_SETTING_KEYS:
            continue
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    if cur:
        vs[vid] = cur
    else:
        vs.pop(vid, None)
    cfg["versionSettings"] = vs
    save(cfg)
    return version_settings(vid, cfg)


def clear_version_settings(vid: str) -> Dict[str, Any]:
    cfg = load()
    (cfg.get("versionSettings") or {}).pop(vid, None)
    save(cfg)
    return version_settings(vid, cfg)


# 版本独立目录里出现这些数据目录（且非空），说明该版本的数据本来就独立存放
# —— 常见于从 PCL / HMCL 迁过来的版本，它们的 mod、存档都在 versions/<vid>/ 下
ISOLATE_DATA_DIRS = ("mods", "saves", "config", "resourcepacks", "shaderpacks",
                     "defaultconfigs", "kubejs", "scripts", "options.txt")

_ISO_CACHE: Dict[str, Any] = {}
_ISO_TTL = 15.0


def base_game_dir(vid: str = "", cfg: Dict[str, Any] | None = None) -> str:
    """不含版本隔离的游戏主目录（只读原始配置，避免与 version_settings 互相调用）。"""
    cfg = cfg or load()
    raw = ((cfg.get("versionSettings") or {}).get(vid) or {}) if vid else {}
    return raw.get("gameDir") or cfg.get("gameDir") or DEFAULT_GAME_DIR


def detect_version_data(vid: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    扫描 versions/<vid>/ 下是否已存在独立的游戏数据（mod / 存档 / 配置等）。
    结果缓存 15 秒，避免启动与刷新界面时反复扫盘。
    """
    cfg = cfg or load()
    now = time.time()
    hit = _ISO_CACHE.get(vid)
    if hit and now - hit[0] < _ISO_TTL:
        return hit[1]

    base = base_game_dir(vid, cfg)
    sub = os.path.join(base, "versions", vid)
    found: Dict[str, Any] = {}
    try:
        if os.path.isdir(sub):
            for name in ISOLATE_DATA_DIRS:
                p = os.path.join(sub, name)
                if os.path.isfile(p):
                    found[name] = 1
                    continue
                if os.path.isdir(p):
                    n = len([f for f in os.listdir(p) if not f.startswith(".")])
                    if n:
                        found[name] = n
    except Exception:  # noqa: BLE001
        pass
    # 主目录（未隔离时用的）里同期也有数据吗
    main = {}
    try:
        if os.path.isdir(base):
            for name in ISOLATE_DATA_DIRS:
                p = os.path.join(base, name)
                if os.path.isfile(p):
                    main[name] = 1
                elif os.path.isdir(p):
                    n = len([f for f in os.listdir(p) if not f.startswith(".")])
                    if n:
                        main[name] = n
    except Exception:  # noqa: BLE001
        pass

    out = {"dir": sub, "base": base, "data": found, "mainData": main,
           "has": bool(found)}
    _ISO_CACHE[vid] = (now, out)
    return out


def resolve_isolate(vid: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    决定某版本是否启用版本隔离：
      1) 版本设置里手动选过  -> 完全听用户的
      2) 没选过             -> 若 versions/<vid>/ 下已有独立数据，自动隔离
      3) 都没有             -> 跟随主设置
    返回 {isolated, reason, auto, data, mainData, dir, base}
    """
    cfg = cfg or load()
    raw = (cfg.get("versionSettings") or {}).get(vid) or {}
    det = detect_version_data(vid, cfg)

    if raw.get("isolate") is not None:
        iso = bool(raw["isolate"])
        if iso:
            reason = "手动设置：开启版本隔离"
        else:
            reason = "手动设置：关闭版本隔离"
            if det["has"]:
                reason += "（注意：版本目录里的 %s 读不到）" % _fmt_data(det["data"])
        return {"isolated": iso, "reason": reason, "auto": False,
                "data": det["data"], "mainData": det["mainData"],
                "dir": det["dir"], "base": det["base"]}

    if det["has"]:
        return {"isolated": True, "auto": True,
                "reason": "自动：该版本目录里已有独立数据（%s）" % _fmt_data(det["data"]),
                "data": det["data"], "mainData": det["mainData"],
                "dir": det["dir"], "base": det["base"]}

    g = bool(cfg.get("isolateVersion", False))
    return {"isolated": g, "auto": False,
            "reason": "跟随主设置（%s）" % ("开启隔离" if g else "关闭隔离"),
            "data": det["data"], "mainData": det["mainData"],
            "dir": det["dir"], "base": det["base"]}


def _fmt_data(d: Dict[str, Any]) -> str:
    zh = {"mods": "模组", "saves": "存档", "config": "配置",
          "resourcepacks": "资源包", "shaderpacks": "光影",
          "defaultconfigs": "默认配置", "kubejs": "KubeJS", "scripts": "脚本",
          "options.txt": "游戏设置"}
    return "、".join("%s %d" % (zh.get(k, k), v) for k, v in d.items()) or "数据"


def is_isolated(vid: str, cfg: Dict[str, Any] | None = None) -> bool:
    return bool(resolve_isolate(vid, cfg)["isolated"])


def effective_game_dir(vid: str, cfg: Dict[str, Any] | None = None) -> str:
    """
    版本实际使用的游戏目录。
    开启版本隔离后为 <gameDir>/versions/<vid>/，存档、mod、配置互相独立；
    运行库与资源仍共用主目录（它们都是绝对路径，不受影响）。
    未手动设置时会自动识别：版本目录里已经有 mod/存档 的，自动按隔离处理，
    避免从别的启动器迁过来的版本启动时读不到自己的 mod。
    """
    cfg = cfg or load()
    r = resolve_isolate(vid, cfg)
    d = r["dir"] if r["isolated"] else r["base"]
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


# ==================================================================
# 自定义登录源（Yggdrasil / 外置登录）
# ==================================================================

def ygg_url(server: str, path: str) -> str:
    return server.rstrip("/") + "/" + path.lstrip("/")


def ygg_authenticate(server: str, username: str, password: str,
                     client_token: str = "") -> Dict[str, Any]:
    """
    Yggdrasil 标准登录。返回 {ok, accessToken, clientToken, profiles, msg}。
    LittleSkin 等外置登录源都遵循这套协议。
    """
    if not server:
        return {"ok": False, "msg": "未设置登录源地址"}
    payload: Dict[str, Any] = {
        "agent": {"name": "Minecraft", "version": 1},
        "username": username,
        "password": password,
        "requestUser": True,
    }
    if client_token:
        payload["clientToken"] = client_token
    try:
        r = requests.post(ygg_url(server, "authserver/authenticate"), json=payload,
                          timeout=(8, 20),
                          headers={"User-Agent": UA, "Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"无法连接登录服务器：{e}"}
    if r.status_code == 403:
        return {"ok": False, "msg": "登录源拒绝了请求（403），请检查地址或是否需要登录验证"}
    if r.status_code != 200:
        try:
            msg = (r.json() or {}).get("errorMessage") or (r.json() or {}).get("message")
        except Exception:
            msg = None
        return {"ok": False, "msg": msg or f"登录失败（HTTP {r.status_code}）"}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "msg": "登录服务器返回了无法解析的内容"}

    profiles = data.get("availableProfiles") or []
    sel = data.get("selectedProfile")
    if not sel and profiles:
        sel = profiles[0]
    if not sel:
        return {"ok": False, "msg": "账号下没有可用角色（请先到登录源网页创建角色）"}
    return {
        "ok": True,
        "accessToken": data.get("accessToken", ""),
        "clientToken": data.get("clientToken", ""),
        "uuid": sel.get("id", ""),
        "name": sel.get("name", ""),
        "profiles": [{"id": p.get("id"), "name": p.get("name")} for p in profiles],
    }


def ygg_refresh(server: str, access_token: str, client_token: str = "") -> Dict[str, Any]:
    """刷新令牌，成功返回新的 accessToken / 角色。"""
    if not server or not access_token:
        return {"ok": False, "msg": "缺少令牌"}
    payload: Dict[str, Any] = {"accessToken": access_token}
    if client_token:
        payload["clientToken"] = client_token
    try:
        r = requests.post(ygg_url(server, "authserver/refresh"), json=payload,
                          timeout=(8, 20),
                          headers={"User-Agent": UA, "Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"无法连接登录服务器：{e}"}
    if r.status_code in (403, 401):
        return {"ok": False, "msg": "令牌已失效，需要重新登录"}
    if r.status_code != 200:
        return {"ok": False, "msg": f"刷新失败（HTTP {r.status_code}）"}
    d = r.json()
    sel = d.get("selectedProfile") or {}
    return {"ok": True, "accessToken": d.get("accessToken", ""),
            "clientToken": d.get("clientToken", client_token),
            "uuid": sel.get("id", ""), "name": sel.get("name", "")}


def ygg_validate(server: str, access_token: str, client_token: str = "") -> bool:
    if not server or not access_token:
        return False
    try:
        r = requests.post(ygg_url(server, "authserver/validate"),
                          json={"accessToken": access_token, "clientToken": client_token},
                          timeout=(6, 12),
                          headers={"User-Agent": UA, "Content-Type": "application/json"})
        return r.status_code == 204
    except Exception:
        return False


def add_ygg_account(name: str, uuid: str, token: str, client_token: str,
                    server: str, server_name: str = "") -> Dict[str, Any]:
    """保存外置登录账户（重名则覆盖令牌）。"""
    cfg = load()
    accounts: List[Dict[str, Any]] = list(cfg.get("accounts") or [])
    acc = {
        "name": name, "uuid": uuid, "type": "yggdrasil",
        "accessToken": token, "clientToken": client_token,
        "server": server, "serverName": server_name,
    }
    for i, a in enumerate(accounts):
        if a.get("name") == name and a.get("type") == "yggdrasil":
            accounts[i] = acc
            break
    else:
        accounts.append(acc)
    cfg["accounts"] = accounts
    cfg["activeAccount"] = name
    cfg["authSource"] = "yggdrasil"
    cfg["authServer"] = server
    cfg["authServerName"] = server_name
    save(cfg)
    return {"ok": True, "account": acc}


# ---------- authlib-injector ----------

def authlib_injector_path() -> str:
    return os.path.join(ROOT_DIR, "authlib-injector.jar")


def ensure_authlib_injector() -> Optional[str]:
    """
    保证本地存在 authlib-injector（外置登录皮肤与正版验证依赖它）。
    已存在则直接返回路径，否则从镜像下载最新版。
    """
    dest = authlib_injector_path()
    if os.path.isfile(dest) and os.path.getsize(dest) > 100000:
        return dest
    try:
        r = requests.get("https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/latest.json",
                         timeout=(6, 15), headers={"User-Agent": UA})
        info = r.json()
        ver = info.get("version")
        dl = (info.get("download_url") or
              f"https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/{ver}/authlib-injector-{ver}.jar")
    except Exception:
        return None
    try:
        tmp = dest + ".cbtmp"
        with requests.get(dl, stream=True, timeout=(6, 40),
                          headers={"User-Agent": UA}) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
        if os.path.getsize(tmp) < 100000:
            raise ValueError("文件过小")
        if os.path.exists(dest):
            os.remove(dest)
        os.replace(tmp, dest)
        return dest
    except Exception:
        return None


# ==================================================================
# Java 自动补齐
# ==================================================================

import zipfile as _zipfile

JAVA_DIR = os.path.join(ROOT_DIR, "java")


def java_install_dir() -> str:
    os.makedirs(JAVA_DIR, exist_ok=True)
    return JAVA_DIR


def _walk_java(root: str) -> List[str]:
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath[len(root):].count(os.sep) > 4:
            dirnames[:] = []
            continue
        if "javaw.exe" in filenames:
            found.append(os.path.join(dirpath, "javaw.exe"))
    return found


def scan_bundled_java() -> List[Dict[str, Any]]:
    """扫描启动器自带的 Java（自动下载来的）。"""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(JAVA_DIR):
        return out
    for name in os.listdir(JAVA_DIR):
        d = os.path.join(JAVA_DIR, name)
        if not os.path.isdir(d):
            continue
        for exe in _walk_java(d):
            info = _probe(exe)
            if info:
                info["bundled"] = True
                out.append(info)
    return out


def java_arch() -> str:
    pa = (os.environ.get("PROCESSOR_ARCHITECTURE", "") or "").upper()
    if "ARM" in pa or "AARCH" in pa:
        return "aarch64"
    if pa.endswith("64"):
        return "x64"
    # 32 位系统一般也是 x64 CPU，交给实际探测结果
    return "x64"


def java_sources(major: int) -> List[str]:
    """
    Adoptium 兜底源（Mojang 官方运行时优先，见 ensure_java）。
    以前填的两个镜像地址（清华目录页 / bmclapi openjdk）实测都是 404，
    会把"自动下载"拖成"点了半天没反应"，这里只保留真正能下的地址。
    """
    arch = java_arch()
    return [
        "https://api.adoptium.net/v3/binary/latest/%d/ga/windows/%s/jre/hotspot/normal/eclipse"
        % (major, arch),
        "https://api.adoptium.net/v3/binary/latest/%d/ga/windows/%s/jdk/hotspot/normal/eclipse"
        % (major, arch),
    ]


# ---------- Mojang 官方 Java 运行时（PCL 也是从这里下） ----------

MOJANG_JAVA_INDEX = [
    "https://bmclapi2.bangbang93.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json",
    "https://piston-meta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json",
]


def mojang_java_index() -> Dict[str, Any]:
    """拉取 Mojang 提供的 Java 运行时清单（带国内镜像回退）。"""
    for url in MOJANG_JAVA_INDEX:
        try:
            r = requests.get(url, timeout=(6, 20), headers={"User-Agent": UA})
            r.raise_for_status()
            data = r.json()
            plat = "windows-x64" if java_arch() == "x64" else "windows-x86"
            items = data.get(plat) or data.get("windows-x64") or {}
            out = []
            for name, arr in items.items():
                if not isinstance(arr, list) or not arr:
                    continue
                entry = arr[0]
                vname = str(((entry.get("version") or {}).get("name")) or "")
                out.append({"component": name, "entry": entry,
                            "vtuple": _v_tuple(vname), "name": vname})
            if out:
                return {"ok": True, "list": out}
        except Exception:
            continue
    return {"ok": False, "list": []}


# ---------------------------------------------------------------------------
# 补齐全部：覆盖 MC 1.13 → 26.1+ 需要的所有 Java 主版本
#   1.13 ~ 1.16.5  → Java 8
#   1.17 ~ 1.20.4  → Java 17（1.17 官方要 16，Java 17 同样能跑）
#   1.20.5 ~ 1.21  → Java 21
#   26.1 及以上     → Java 25（新版版本 JSON 里 Mojang 声明的运行时）
# ---------------------------------------------------------------------------
JAVA_FILL_PLAN: List[Dict[str, Any]] = [
    {"major": 8, "component": "jre-legacy", "for": "MC 1.13 ~ 1.16.5"},
    {"major": 17, "component": "java-runtime-beta", "for": "MC 1.17 ~ 1.20.4"},
    {"major": 21, "component": "java-runtime-gamma", "for": "MC 1.20.5 ~ 1.21.x"},
    {"major": 25, "component": "java-runtime-epsilon", "for": "MC 26.1 及以上"},
]


def java_have_majors() -> List[int]:
    """本机 + 启动器自带，已经有哪些主版本的 Java。"""
    out = set()
    for i in list_java() + scan_bundled_java():
        m = i.get("major")
        if m:
            out.add(int(m))
    return sorted(out)


def java_fill_plan() -> List[Dict[str, Any]]:
    """「补齐全部」的计划：每款 Java 的用途 + 是否已装。"""
    have = set(java_have_majors())
    out = []
    for it in JAVA_FILL_PLAN:
        row = dict(it)
        row["have"] = row["major"] in have
        out.append(row)
    return out


def fill_java_all(majors: Optional[List[int]] = None, task=None,
                  force: bool = False) -> Dict[str, Any]:
    """
    一次补齐多个 Java（默认补齐计划里全部缺失的版本）。
    返回 {"ok": [...], "failed": [...], "skipped": [...]}。
    """
    plan = java_fill_plan()
    if majors:
        want = []
        try:
            want = [int(m) for m in majors]
        except Exception:  # noqa: BLE001
            want = []
        plan = [p for p in plan if p["major"] in want] or plan
    todo = [p for p in plan if force or not p["have"]]
    skipped = [p["major"] for p in plan if not force and p["have"]]
    ok_list: List[int] = []
    fail_list: List[int] = []
    total = len(todo)
    for n, p in enumerate(todo, 1):
        if task and task.cancelled:
            break
        if task:
            task.total = total * 100
            task.done = (n - 1) * 100
            task.message = "补齐 Java %d（%d/%d）· %s" % (
                p["major"], n, total, p["for"])
        got = None
        try:
            got = ensure_java(p["major"], task,
                              component=p.get("component") or "", force=force)
        except Exception:  # noqa: BLE001
            got = None
        if got:
            ok_list.append(p["major"])
        else:
            fail_list.append(p["major"])
        if task:
            task.done = n * 100
    if task:
        task.done = task.total
        if ok_list:
            task.message = "已补齐 Java " + "、".join(str(m) for m in ok_list)
        if fail_list and not ok_list:
            task.state = TaskState.FAILED
            task.error = "补齐失败：Java " + "、".join(str(m) for m in fail_list)
        elif fail_list:
            task.message += "（失败：Java " + "、".join(
                str(m) for m in fail_list) + "）"
    return {"ok": ok_list, "failed": fail_list, "skipped": skipped}


def download_mojang_java(major: int = 0, component: str = "", task=None) -> Optional[Dict[str, Any]]:
    """按 Mojang 官方清单下载一份 JRE（逐文件校验 sha1，和 PCL 同一个源）。"""
    idx = mojang_java_index()
    if not idx.get("ok"):
        return None
    cands = [c for c in idx["list"] if c["component"] != "minecraft-java-exe"]
    target = None
    if component:
        target = next((c for c in cands if c["component"] == component), None)
    if target is None and major:
        target = next((c for c in cands if c["vtuple"][0] == major), None)
    if target is None and major:
        target = next((c for c in cands if c["vtuple"][0] >= major), None)
    # 指定了组件但它反而比要求还旧，就不用它了
    if target is not None and major and target["vtuple"][0] < major:
        target = next((c for c in cands if c["vtuple"][0] >= major), None)
    if target is None:
        return None

    manifest_url = ((target["entry"].get("manifest") or {}).get("url")) or ""
    if not manifest_url:
        return None
    try:
        r = requests.get(manifest_url, timeout=(6, 20), headers={"User-Agent": UA})
        r.raise_for_status()
        files = (r.json().get("files") or {})
    except Exception:
        return None

    base = os.path.join(java_install_dir(), "mojang-" + target["component"])
    os.makedirs(base, exist_ok=True)
    items: List[DownloadItem] = []
    for rel, meta in files.items():
        raw = (meta.get("downloads") or {}).get("raw") or {}
        url = raw.get("url")
        if not url:
            continue
        # 换国内镜像，官方源在国内常常超时
        mirror = url.replace("piston-data.mojang.com", "bmclapi2.bangbang93.com")
        items.append(DownloadItem(
            [url] if mirror == url else [mirror, url],
            os.path.join(base, rel.replace("/", os.sep)),
            int(raw.get("size") or 0), raw.get("sha1") or "", rel,
        ))
    if not items:
        return None

    if task:
        task.message = "下载 Mojang Java（%s）" % target["name"]
    need = [it for it in items if not file_ok(it)]
    if need:
        if task:
            task.total = len(need)
        ok, errs = download_all(need, workers=16, retry_rounds=2)
        if errs:
            if task:
                task.message = "Mojang Java 部分文件下载失败，改用其它源"
            return None

    for exe in _walk_java(base):
        info = _probe(exe)
        if info:
            info["bundled"] = True
            info["component"] = target["component"]
            return info
    return None


def download_java(major: int, task=None) -> Optional[Dict[str, Any]]:
    """下载一份 JRE 到启动器目录。"""
    if task:
        task.message = "准备下载 Java %d" % major
    root = java_install_dir()
    target = os.path.join(root, "jre-%d" % major)
    tmp = os.path.join(root, "jre-%d.zip" % major)
    last: Optional[Exception] = None
    for url in java_sources(major):
        try:
            if task:
                task.message = "下载 Java %d" % major
            with requests.get(url, stream=True, timeout=(10, 180),
                              headers={"User-Agent": UA, "Connection": "close"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                if task:
                    task.total = 100
                got = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if not chunk:
                            continue
                        f.write(chunk)
                        got += len(chunk)
                        if task and total:
                            task.done = min(99, int(got * 100 / total))
            if os.path.getsize(tmp) < 512 * 1024:
                raise ValueError("下载到的文件过小，不是有效的 Java 包")
            if task:
                task.message = "解压 Java %d" % major
            os.makedirs(target, exist_ok=True)
            with _zipfile.ZipFile(tmp) as z:
                z.extractall(target)
            try:
                os.remove(tmp)
            except Exception:
                pass
            for exe in _walk_java(target):
                info = _probe(exe)
                if info:
                    info["bundled"] = True
                    return info
            last = RuntimeError("解压后没有找到 javaw.exe")
        except Exception as e:  # noqa: BLE001
            last = e
            continue
    if task:
        task.state = TaskState.FAILED
        task.error = "Java %d 自动下载失败：%s" % (major, last)
    return None


def ensure_java(major: int, task=None, component: str = "",
                force: bool = False) -> Optional[Dict[str, Any]]:
    """
    保证有指定主版本的 Java，没有就自动下载。
    顺序：本机已有 -> Mojang 官方运行时（PCL 同款源）-> Adoptium 镜像。
    force=True 时跳过"本机已有"这一步，直接往启动器目录装一份（用于「补齐全部」）。
    """
    if not force:
        for info in list_java(True):
            if info.get("major") == major:
                return info
        for info in scan_bundled_java():
            if info.get("major") == major:
                return info

    if task:
        task.message = "准备 Java %d" % major
    try:
        got = download_mojang_java(major, component, task)
        if got:
            return got
    except Exception:
        pass
    return download_java(major, task)


def auto_pick_java(mc_version: str = "") -> Optional[Dict[str, Any]]:
    """按 MC 版本自动挑选最合适的 Java。"""
    need = recommended_major(mc_version) if mc_version else 0
    lst = list_java() + scan_bundled_java()
    if not lst:
        return None
    if need:
        exact = [j for j in lst if j.get("major") == need and j.get("bits", 0) >= 64]
        if exact:
            return sorted(exact, key=lambda x: x.get("bits", 0), reverse=True)[0]
        higher = [j for j in lst if (j.get("major") or 0) > need]
        if higher:
            return sorted(higher, key=lambda x: x.get("major") or 0)[0]
    return sorted(lst, key=lambda x: (x.get("bits", 0), x.get("major") or 0), reverse=True)[0]


# ==================================================================
# 皮肤（LittleSkin 公开皮肤 / 正版玩家皮肤）
# ==================================================================

import base64 as _b64
import hashlib as _hl

SKIN_DIR = os.path.join(ROOT_DIR, "skins")

SKIN_SOURCES = {
    "mojang": {
        "name": "正版（Mojang）",
        "uuid": "https://api.mojang.com/users/profiles/minecraft/",
        "profile": "https://sessionserver.mojang.com/session/minecraft/profile/",
    },
    "crafthead": {
        "name": "正版镜像 Crafthead",
        "uuid": "https://api.crafthead.net/player/",
        "profile": "https://api.crafthead.net/player/",
    },
    "littleskin": {
        "name": "LittleSkin 皮肤站",
        "uuid": "https://littleskin.cn/api/yggdrasil/api/profiles/minecraft/",
        "profile": "https://littleskin.cn/api/yggdrasil/sessionserver/session/minecraft/profile/",
    },
}


def skin_dir() -> str:
    os.makedirs(SKIN_DIR, exist_ok=True)
    return SKIN_DIR


def _http_json(url: str) -> Any:
    try:
        r = requests.get(url, timeout=(6, 15), headers={"User-Agent": UA, "Connection": "close"})
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _source_order(source: str) -> List[str]:
    if source in SKIN_SOURCES:
        return [source, "crafthead", "mojang"]
    return ["mojang", "crafthead", "littleskin"]


def fetch_uuid(player: str, source: str = "auto") -> Optional[str]:
    """玩家名 -> UUID。"""
    player = (player or "").strip()
    if not player:
        return None
    for s in _source_order(source):
        cfg = SKIN_SOURCES.get(s)
        if not cfg:
            continue
        d = _http_json(cfg["uuid"] + player)
        if isinstance(d, dict) and d.get("id"):
            return str(d["id"])
    return None


def _decode_props(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {"name": d.get("name", ""), "uuid": d.get("id", ""),
           "skin": "", "cape": "", "model": "classic"}
    for p in d.get("properties") or []:
        if p.get("name") != "textures":
            continue
        try:
            raw = _b64.b64decode(str(p.get("value", "")) + "==")
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        tex = data.get("textures") or {}
        sk = tex.get("SKIN") or {}
        cp = tex.get("CAPE") or {}
        out["skin"] = sk.get("url", "")
        out["cape"] = cp.get("url", "")
        if isinstance(sk.get("metadata"), dict):
            out["model"] = sk["metadata"].get("model", "classic")
    return out


def fetch_profile(uuid: str, source: str = "auto") -> Optional[Dict[str, Any]]:
    """UUID -> 皮肤/披风信息。"""
    uid = (uuid or "").replace("-", "")
    if not uid:
        return None
    for s in _source_order(source):
        cfg = SKIN_SOURCES.get(s)
        if not cfg:
            continue
        d = _http_json(cfg["profile"] + uid)
        if isinstance(d, dict) and d.get("properties"):
            r = _decode_props(d)
            r["source"] = s
            return r
    return None


def download_skin(url: str, name: str = "") -> Optional[str]:
    """把皮肤图片存到本地，返回路径。"""
    if not url:
        return None
    d = skin_dir()
    key = _hl.md5(url.encode("utf-8")).hexdigest()[:16]
    safe = "".join(c for c in (name or "skin") if c.isalnum() or c in "-_")[:24] or "skin"
    dest = os.path.join(d, safe + "-" + key + ".png")
    try:
        with requests.get(url, timeout=(6, 20),
                          headers={"User-Agent": UA, "Connection": "close"}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
        if os.path.getsize(dest) < 32:
            os.remove(dest)
            return None
        return dest
    except Exception:
        return None
