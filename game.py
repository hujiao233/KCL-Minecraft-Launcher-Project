# -*- coding: utf-8 -*-
"""
脆冰启动器 - 游戏启动
负责文件完整性校验、natives 解压、classpath 组装、参数模板渲染、进程管理。
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
import time
import ctypes
from typing import Any, Dict, List, Optional, Tuple

import core
import version as vmod

TIME = "%Y-%m-%d %H:%M:%S"


class GameProcess:
    """一次游戏运行的记录（一个实例 = 一个标签页）。"""

    def __init__(self, pid: int, version_id: str, account: str, proc: subprocess.Popen,
                 logfile: str, iid: str = "", name: str = "", mem_mb: int = 0,
                 mem_auto: bool = True, mem_source: str = "", profile: str = ""):
        self.id = iid or ("gp" + os.urandom(4).hex())
        self.name = name or ""
        self.custom_name = ""          # 用户手动改过的名字（优先级最高）
        self.pid = pid
        self.version_id = version_id
        self.account = account
        self.proc = proc
        self.started_at = time.time()
        self.exited = False
        self.exit_code: Optional[int] = None
        self.logfile = logfile
        self.tail: List[str] = []
        self._tail_lock = threading.Lock()
        self.command: List[str] = []
        self.window_ready = False      # 游戏窗口是否已经出现
        self.crashed = False           # 是否已判定为崩溃退出
        self.java_path = ""
        self.profile = profile         # 来自哪个启动预设
        # ---- 内存（每个实例独立）----
        self.mem_mb = int(mem_mb or 0)         # 启动时给 JVM 的 -Xmx
        self.mem_auto = bool(mem_auto)         # 这个实例是否自动分配
        self.mem_source = mem_source or "手动设置"
        self.rss_mb = 0                # 当前实际占用（工作集）
        self.peak_mb = 0               # 峰值占用
        self.target_mb = int(mem_mb or 0)      # 自动模式下持续收敛的目标
        self.cpu = 0.0                 # 最近一次采样到的 CPU 占用（%）
        self._last_cpu_t = 0.0
        self._last_cpu_s = 0.0
        self._last_gc = 0.0
        self.auto_note = ""

    @property
    def alive(self) -> bool:
        if self.exited:
            return False
        return self.proc.poll() is None

    @property
    def display_name(self) -> str:
        if self.custom_name:
            return self.custom_name
        if self.name:
            return self.name
        return "%s · %s" % (self.version_id, self.account or "离线")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "pid": self.pid,
            "name": self.display_name,
            "version": self.version_id,
            "account": self.account,
            "profile": self.profile,
            "alive": self.alive,
            "exitCode": self.exit_code,
            "windowReady": self.window_ready,
            "crashed": self.crashed,
            "startedAt": time.strftime(TIME, time.localtime(self.started_at)),
            "uptime": int(max(0, time.time() - self.started_at)),
            "logfile": self.logfile,
            "log": list(self.tail),
            "memory": {
                "limit": self.mem_mb,
                "auto": self.mem_auto,
                "source": self.mem_source,
                "used": self.rss_mb,
                "peak": self.peak_mb,
                "target": self.target_mb,
                "note": self.auto_note,
                "cpu": round(self.cpu, 1),
            },
        }


# ---------------- 实例表（支持多开） ----------------

_instances: Dict[str, GameProcess] = {}
_inst_order: List[str] = []
_inst_lock = threading.Lock()


def current_process() -> Optional[GameProcess]:
    """兼容老接口：返回最近启动的那个实例。"""
    with _inst_lock:
        for iid in reversed(_inst_order):
            gp = _instances.get(iid)
            if gp:
                return gp
    return None


def list_instances() -> List[GameProcess]:
    with _inst_lock:
        return [g for g in (_instances.get(i) for i in _inst_order) if g]


def get_instance(iid: str) -> Optional[GameProcess]:
    with _inst_lock:
        return _instances.get(iid)


def _register(gp: GameProcess) -> None:
    with _inst_lock:
        _instances[gp.id] = gp
        _inst_order.append(gp.id)


def _forget(iid: str) -> None:
    with _inst_lock:
        _instances.pop(iid, None)
        if iid in _inst_order:
            _inst_order.remove(iid)


def rename_instance(iid: str, name: str) -> Dict[str, Any]:
    gp = get_instance(iid)
    if not gp:
        return {"ok": False, "msg": "没有这个实例"}
    gp.custom_name = (name or "").strip()[:40]
    return {"ok": True, "name": gp.display_name}


# ---------------- 进程内存 / CPU 采样 ----------------

class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_PROC_QUERY_LIMITED = 0x1000
_PROC_VM_READ = 0x0010


def _win_proc_mem_mb(pid: int) -> Tuple[int, int]:
    """返回 (当前工作集 MB, 峰值工作集 MB)；非 Windows 或失败返回 (0, 0)。"""
    if os.name != "nt":
        return 0, 0
    h = None
    try:
        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        h = k32.OpenProcess(_PROC_QUERY_LIMITED | _PROC_VM_READ, False, int(pid))
        if not h:
            return 0, 0
        c = _PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return 0, 0
        return (int(c.WorkingSetSize / 1048576),
                int(c.PeakWorkingSetSize / 1048576))
    except Exception:
        return 0, 0
    finally:
        if h:
            try:
                ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass


def _win_proc_cpu_seconds(pid: int) -> float:
    """进程累计 CPU 时间（秒），用来算实时 CPU 占用。"""
    if os.name != "nt":
        return 0.0
    h = None
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROC_QUERY_LIMITED, False, int(pid))

        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        if not h:
            return 0.0
        c, e, k = FILETIME(), FILETIME(), FILETIME()
        u, s = FILETIME(), FILETIME()
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            return 0.0
        to_int = lambda f: (f.high << 32) | f.low     # noqa: E731
        return (to_int(k) + to_int(u)) / 1e7
    except Exception:
        return 0.0
    finally:
        if h:
            try:
                ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass


def _jcmd_path(gp: GameProcess) -> str:
    base = os.path.dirname(gp.java_path or "")
    exe = os.path.join(base, "jcmd.exe")
    return exe if os.path.isfile(exe) else ""


def _auto_tune(gp: GameProcess, cfg: Dict[str, Any]) -> None:
    """
    自动内存的「运行中」部分。

    JVM 的 -Xmx 在启动后不能改，所以这里做两件真正有用的事：
      1. 按当前可用内存、同时运行的实例数，持续算出每个进程应占的目标值；
      2. 目标明显低于实际占用 + 进程空闲时，让它把空闲堆还给系统（jcmd GC.run）。
    这样开着的游戏越多、系统越紧张，每个进程占的物理内存会自己收敛。
    """
    if not gp.mem_auto:
        gp.target_mb = gp.mem_mb
        return
    try:
        info = core.system_memory_info()
        alive = [g for g in list_instances() if g.alive]
        n = max(1, len(alive))
        # 可给游戏的总量 = 此刻空闲 + 各实例已经占着的（重新均分）
        pool = info["available"] + sum(g.rss_mb for g in alive)
        pool = max(1024, pool - core._mem_reserve(info["total"]))   # noqa: SLF001
        target = int(min(gp.mem_mb, max(1024, pool // n)))
        target = int(target // 256 * 256)
        gp.target_mb = target
        gp.auto_note = "%d 个实例共享 %s，建议每个 %s" % (
            n, _mb_text(info["available"]), _mb_text(target))
        # 只在真的「虚胖」且闲的时候归还内存，避免影响游戏
        if (cfg.get("autoMemGc", True) and gp.rss_mb > max(1536, target * 1.35)
                and gp.cpu < 12 and time.time() - gp._last_gc > 120 and gp.window_ready):
            jc = _jcmd_path(gp)
            if jc:
                gp._last_gc = time.time()
                try:
                    subprocess.run([jc, str(gp.pid), "GC.run"], timeout=8,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    pass
    except Exception:
        pass


def _mb_text(mb: int) -> str:
    mb = int(mb or 0)
    if mb >= 1024:
        g = mb / 1024.0
        return ("%.1f GB" % g) if g < 10 else ("%d GB" % round(g))
    return "%d MB" % mb


def _monitor() -> None:
    """每秒采样所有实例的占用；每 15 秒做一次自动内存收敛。"""
    last_tune = 0.0
    while True:
        try:
            cfg = core.load()
            now = time.time()
            for gp in list_instances():
                if not gp.alive:
                    continue
                used, peak = _win_proc_mem_mb(gp.pid)
                if used:
                    gp.rss_mb = used
                    gp.peak_mb = max(gp.peak_mb, peak or used)
                cs = _win_proc_cpu_seconds(gp.pid)
                if gp._last_cpu_t:
                    dt = max(0.2, now - gp._last_cpu_t)
                    gp.cpu = max(0.0, min(999.0, (cs - gp._last_cpu_s) / dt * 100.0))
                gp._last_cpu_t, gp._last_cpu_s = now, cs
                if now - last_tune > 15:
                    _auto_tune(gp, cfg)
            if now - last_tune > 15:
                last_tune = now
        except Exception:
            pass
        time.sleep(1.0)


_monitor_started = False


def _ensure_monitor() -> None:
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True
    threading.Thread(target=_monitor, daemon=True, name="kcl-mem-monitor").start()


# ---------------- 完整性检查 ----------------

def missing_files(vid: str, cfg: Dict[str, Any] | None = None,
                  check_assets: bool = True) -> Tuple[List[str], List[str]]:
    """返回 (缺失文件列表, 警告列表)。"""
    cfg = cfg or core.load()
    vj = vmod.resolve_version(vid)

    missing: List[str] = []
    warns: List[str] = []

    jar = core.version_jar_path(vmod.get_jar_version(vj, vid), cfg)
    if not os.path.isfile(jar):
        missing.append(os.path.basename(jar))

    for item in vmod.collect_libraries(vj, cfg):
        if not os.path.isfile(item.path):
            missing.append(item.name)

    for nj in vmod.native_jars(vj, cfg):
        if not os.path.isfile(nj):
            missing.append(os.path.basename(nj))

    if check_assets:
        idx_id = (vj.get("assetIndex") or {}).get("id") or vj.get("assets")
        if idx_id:
            idx_path = os.path.join(core.assets_dir(cfg), "indexes", f"{idx_id}.json")
            if not os.path.isfile(idx_path):
                # 资源缺失不影响进程启动（可进入游戏后再补），只作为警告
                warns.append(f"资源索引 {idx_id} 缺失")
            else:
                try:
                    with open(idx_path, "r", encoding="utf-8") as f:
                        index = json.load(f)
                    obj_root = os.path.join(core.assets_dir(cfg), "objects")
                    miss = 0
                    for _n, obj in (index.get("objects") or {}).items():
                        h = obj.get("hash", "")
                        if h and not os.path.isfile(os.path.join(obj_root, h[:2], h)):
                            miss += 1
                    if miss:
                        warns.append(f"资源文件缺失 {miss} 个")
                except Exception:
                    warns.append("资源索引损坏")

    return missing, warns


def check_ready(vid: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    try:
        missing, warns = missing_files(vid, cfg)
    except Exception as e:  # noqa: BLE001
        # 版本 json/jar 丢失、或联网补 json 也失败（如 NeoForge/Forge 这类
        # 非 Mojang 官方版本号）时，给一句人话，而不是让 404 堆栈炸出去
        return {"ready": False, "missing": [], "warnings": [], "broken": True,
                "msg": f"版本 {vid} 的信息文件读取失败（{type(e).__name__}）。"
                       f"可能是版本已损坏、被移动，或版本目录里缺少对应的 .json 文件；"
                       f"请到「版本」页重装该版本。"}
    return {"ready": len(missing) == 0, "missing": missing[:50], "warnings": warns}


def _repair_files(vid: str, with_assets: bool = True) -> bool:
    """补下载缺失的游戏文件（PCL 启动链里的「补全文件」）。"""
    t = core.Task("repair", "补全文件", vid)
    t.state = core.TaskState.RUNNING
    try:
        vmod.install_version(vid, t, with_assets=with_assets)
    except Exception:  # noqa: BLE001
        return False
    return t.state == core.TaskState.SUCCESS


def count_mods(game_dir: str) -> int:
    """数一下游戏目录里的模组文件数量（.jar / .zip / .litemod）。"""
    d = os.path.join(game_dir or "", "mods")
    try:
        if not os.path.isdir(d):
            return 0
        return len([f for f in os.listdir(d)
                    if f.lower().endswith((".jar", ".zip", ".litemod"))])
    except Exception:  # noqa: BLE001
        return 0


def detect_loader(vj: Dict[str, Any]) -> str:
    """从版本 JSON 判断模组加载器：neoforge / forge / fabric / quilt / liteloader。"""
    if not vj:
        return ""
    blob = ((vj.get("mainClass") or "") + " " +
            " ".join(str((l or {}).get("name", "")) for l in (vj.get("libraries") or []))
            ).lower()
    for key, name in (("neoforge", "neoforge"), ("neoforged", "neoforge"),
                      ("minecraftforge", "forge"), ("bootstraplauncher", "forge"),
                      ("cpw.mods", "forge"), ("fabricmc", "fabric"), ("knot", "fabric"),
                      ("quiltmc", "quilt"), ("liteloader", "liteloader"),
                      ("rift", "rift"), ("optifine", "optifine")):
        if key in blob:
            return name
    return ""


def precheck(vid: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    启动前预检查（PCL 的 McLaunchPrecheck）。
    路径里的 ! 与 ; 会直接破坏 classpath / JVM 参数，必须在启动前拦下来；
    另外顺带检查「装了 mod 但没有加载器」这类会导致 mod 全部不生效的情况。
    """
    cfg = cfg or core.load()
    try:
        paths = [core.game_dir(cfg), core.effective_game_dir(vid, cfg)]
    except Exception:
        paths = []
    for p in paths:
        if p and ("!" in p or ";" in p):
            return {"ok": False,
                    "msg": f"游戏路径里不能包含 ! 或 ;（{p}），"
                           f"请在设置里换一个游戏目录，否则 Minecraft 无法启动"}
    try:
        vj = vmod.resolve_version(vid)
    except Exception as e:  # noqa: BLE001
        # 版本 JSON 丢了，或者不是 Mojang 官方版本号（NeoForge/Forge 等）
        # 导致联网补也补不到：这时候给用户一条能操作的建议，别甩 404 上去
        why = "联网也拿不到这个版本的 JSON"
        if "404" in str(e):
            why = "这个版本不是 Mojang 官方版本号，联网补不到"
        return {"ok": False, "needInstall": True,
                "msg": f"版本「{vid}」的信息文件缺失（{why}）。"
                       f"通常是 versions/{vid}/ 下的 .json 被删掉或移动了，"
                       f"请到「版本」页重新安装这个版本。"}

    warns: List[str] = []
    try:
        gd = core.effective_game_dir(vid, cfg)
        n = count_mods(gd)
        loader = detect_loader(vj)
        if n and not loader:
            warns.append(
                f"mods 目录里有 {n} 个模组，但「{vid}」是原版（没有 Forge / Fabric / NeoForge "
                f"加载器），这些模组不会生效。请到版本管理里给该版本安装对应加载器。")
        if n:
            iso = core.resolve_isolate(vid, cfg)
            if iso.get("auto"):
                warns.append(
                    f"已自动识别为独立版本目录（{iso.get('reason', '')}），"
                    f"本次启动会读取 {gd} 下的 {n} 个模组。")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "warns": warns}


def _game_window_appeared(pid: int, timeout: float = 150.0) -> bool:
    """
    轮询等待 Minecraft 窗口出现（PCL Watcher 的思路）。
    窗口一直不出现 = 多半已经崩溃，启动器可以立刻给出日志而不是干等。
    """
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        EnumWindows = u32.EnumWindows
        EnumWindows.restype = wintypes.BOOL
        EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        GetWindowThreadProcessId = u32.GetWindowThreadProcessId
        GetWindowThreadProcessId.restype = wintypes.DWORD
        GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        IsWindowVisible = u32.IsWindowVisible
        IsWindowVisible.argtypes = [wintypes.HWND]
        GetWindowTextLengthW = u32.GetWindowTextLengthW
        GetWindowTextLengthW.argtypes = [wintypes.HWND]
        GetWindowTextW = u32.GetWindowTextW
        GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_void_p)
        found = {"ok": False}

        def _cb(hwnd, _lparam):
            try:
                if not IsWindowVisible(hwnd):
                    return True
                pid_out = wintypes.DWORD()
                GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
                if pid_out.value != pid:
                    return True
                n = GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip():
                    found["ok"] = True
                    return False
            except Exception:
                pass
            return True

        deadline = time.time() + timeout
        while time.time() < deadline:
            found["ok"] = False
            EnumWindows(WNDENUMPROC(_cb), 0)
            if found["ok"]:
                return True
            time.sleep(1.0)
    except Exception:
        return False
    return False


# ---------------- 参数组装 ----------------

def build_classpath(vid: str, vj: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    parts: List[str] = []
    jar_ver = vmod.get_jar_version(vj, vid)
    parts.append(core.version_jar_path(jar_ver, cfg))

    seen = set()
    lib_root = core.libraries_dir(cfg)
    for lib in vj.get("libraries", []):
        if not vmod.library_allowed(lib):
            continue
        # 只把真正存在的 artifact 加入 classpath（natives-only 的库没有主 jar）
        art = (lib.get("downloads") or {}).get("artifact") or {}
        rel = art.get("path")
        if not rel:
            # Fabric / Quilt 的 profile 是 Maven 坐标格式（name + url），
            # 没有 downloads.artifact，需要从坐标推导相对路径
            name = lib.get("name", "")
            if not name:
                continue
            try:
                rel = vmod.library_path({"name": name})
            except Exception:
                continue
        p = os.path.join(lib_root, rel.replace("/", os.sep))
        low = p.lower()
        if low in seen:
            continue
        seen.add(low)
        parts.append(p)
    return os.pathsep.join(parts)


def _render(args: List[Any], ph: Dict[str, str], features: Dict[str, bool]) -> List[str]:
    out: List[str] = []
    for a in args:
        if not vmod.argument_allowed(a, features):
            continue
        if isinstance(a, list):
            out.extend(_render(a, ph, features))
            continue
        if isinstance(a, dict):
            val = a.get("value")
            if isinstance(val, list):
                out.extend(_render(val, ph, features))
            elif val is not None:
                out.append(_fmt(str(val), ph))
            continue
        out.append(_fmt(str(a), ph))
    return out


def _fmt(s: str, ph: Dict[str, str]) -> str:
    for k, v in ph.items():
        if "${" + k + "}" in s:
            s = s.replace("${" + k + "}", v)
    return s


def _dedup_args(args: List[str]) -> List[str]:
    """
    去掉重复的单值 JVM 参数（借鉴 PCL 的 DeduplicateJavaArguments）。
    版本 JSON、用户自定义参数、我们默认加的模板可能重复给 -Xmx / -cp 等，
    后出现的会覆盖前面，JVM 也会因为重复参数直接报错。
    """
    # 这些参数只能出现一次，出现多次时「后写的赢」
    single = {"-xmx", "-xms", "-xmn", "-xss", "-djava.library.path",
              "-dfile.encoding", "-dsun.stdout.encoding", "-dsun.stderr.encoding",
              "-dstdout.encoding", "-dstderr.encoding", "-dlog4j2.formatmsgnolookups",
              "-xx:maxpermsize", "-xx:heapdumppath", "-xx:+useg1gc", "-xx:+usezgc",
              "-jar", "-version"}
    pair = {"-cp", "-classpath", "--add-exports", "--add-opens"}
    out: List[str] = []
    seen: Dict[str, int] = {}
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if low in pair and i + 1 < len(args):
            if low in seen:
                out[seen[low]] = a
                out[seen[low] + 1] = args[i + 1]
            else:
                seen[low] = len(out)
                out.extend([a, args[i + 1]])
            i += 2
            continue
        m = re.match(r"^(-x(?:mx|ms|mn|ss))", low)
        if m:
            key = m.group(1)
        elif low.startswith("-d") or low.startswith("-xx:"):
            key = low.split("=", 1)[0]
        else:
            key = low
        if key in single:
            if key in seen:
                out[seen[key]] = a
            else:
                seen[key] = len(out)
                out.append(a)
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _gc_args(java_major: int, win_ok: bool) -> List[str]:
    """
    按 Java 版本挑垃圾回收器（PCL 的策略）：
      Java 21+ 分代 ZGC；15~20 非分代 ZGC；14 及以下 / 老系统 G1GC。
    """
    if java_major >= 15 and win_ok:
        out = ["-XX:+UnlockExperimentalVMOptions", "-XX:+UseZGC"]
        if java_major in (21, 22):
            out.append("-XX:+ZGenerational")
        return out
    out = ["-XX:+UseG1GC", "-XX:G1NewSizePercent=20", "-XX:G1ReservePercent=20",
           "-XX:G1HeapRegionSize=32M", "-XX:MaxGCPauseMillis=50"]
    if java_major <= 7:
        out.append("-XX:MaxPermSize=512m")
    return out


def _encoding_args(java_major: int) -> List[str]:
    """
    编码参数。Java 18 起 file.encoding 会被 COMPAT 覆盖，硬写 UTF-8 反而会让
    老版本 MC / 部分 Mod 乱码；stdout/stderr 的属性名在 Java 19 前后也不一样。
    """
    out = []
    enc = "GBK" if _system_gbk() else "UTF-8"
    so = "sun.stdout.encoding" if java_major < 19 else "stdout.encoding"
    se = "sun.stderr.encoding" if java_major < 19 else "stderr.encoding"
    out.append("-D%s=%s" % (so, enc))
    out.append("-D%s=%s" % (se, enc))
    if java_major >= 18:
        out.append("-Dfile.encoding=COMPAT")
    else:
        out.append("-Dfile.encoding=%s" % enc)
    return out


def _system_gbk() -> bool:
    """系统 ANSI 代码页是中文（936）时按 GBK 输出，否则 UTF-8。"""
    try:
        import ctypes
        return ctypes.windll.kernel32.GetACP() == 936  # type: ignore[attr-defined]
    except Exception:
        return False


def _win_ok_for_zgc() -> bool:
    """ZGC 需要 Windows 10 1809（build 17763）及以上。"""
    try:
        import sys as _sys
        if hasattr(_sys, "getwindowsversion"):
            v = _sys.getwindowsversion()
            return v.major > 10 or (v.major == 10 and v.build >= 17763)
    except Exception:
        pass
    return False


def build_command(vid: str, account: Dict[str, str], cfg: Dict[str, Any],
                  opts: Dict[str, Any] | None = None) -> Tuple[List[str], str, Dict[str, Any]]:
    """
    组装完整启动命令。
    返回 (argv, cwd, meta)
    """
    opts = opts or {}
    vj = vmod.resolve_version(vid)

    # 版本设置优先，其次主设置，最后是本次启动的临时覆盖
    st = core.version_settings(vid, cfg)
    java_path = opts.get("javaPath") or st.get("javaPath") or ""
    # PCL 风格：先算需求区间，再挑 Java，实在没有就自动下载一份
    ji, req = core.select_java(
        vid, vj, cfg, java_path=java_path,
        allow_download=bool(opts.get("allowJavaDownload", True)),
        task=opts.get("task"),
    )
    if not ji:
        raise RuntimeError("未检测到可用的 Java 运行环境，请在设置中手动指定 java.exe，"
                           "或使用「设置 → Java 补齐」自动下载")
    # javaw.exe 拿不到游戏输出，换成同目录的 java.exe
    ji = core.prefer_console_java(ji)
    java_major = int(ji.get("major") or 0)

    # 运行库/资源始终共用主目录；game_directory 在版本隔离时指向独立子目录
    main_dir = core.game_dir(cfg)
    game_dir = core.effective_game_dir(vid, cfg)
    # 版本隔离判定（手动 > 自动识别 > 主设置）与模组数量
    iso = core.resolve_isolate(vid, cfg)
    mod_count = count_mods(game_dir)
    assets_dir = core.assets_dir(cfg)
    natives = core.natives_dir(vid, cfg)

    width = int(opts.get("width") or st.get("windowWidth") or 1280)
    height = int(opts.get("height") or st.get("windowHeight") or 800)
    fullscreen = bool(opts.get("fullscreen") if opts.get("fullscreen") is not None
                      else st.get("fullscreen", False))
    # ---------- 内存 ----------
    # 自动模式（默认）：按「启动这一刻真正可用的内存」算，系统吃紧就自动少给一点。
    # 手动模式：用设置里 / 版本设置里定死的值，版本里单独改过的优先。
    custom_mem = (st.get("_custom") or {}).get("maxMemory")
    if custom_mem is not None:
        custom_mem = int(custom_mem)
    mem_info = core.memory_advice(cfg, vid)
    auto_flag = opts.get("autoMem")
    if auto_flag is None:
        # 没显式指定就跟随全局设置（版本单独改过内存的，视为手动）
        auto_flag = (cfg.get("autoMemory", True) is not False) and custom_mem is None
    auto_flag = bool(auto_flag)
    if auto_flag:
        rec = int(mem_info.get("recommended") or core.auto_memory_mb(cfg))
        cap = int(opts.get("maxMemory") or 0)
        mem = min(rec, cap) if cap else rec
        mem_src = "实时自动"
    else:
        mem = int(opts.get("maxMemory") or custom_mem or st.get("maxMemory")
                  or cfg.get("maxMemory") or 4096)
        mem_src = "本次指定" if opts.get("maxMemory") else (
            "版本设置" if custom_mem else "手动设置")
    try:
        mem = max(1024, min(int(mem), core.slider_max_mb()))
    except Exception:
        mem = 4096

    asset_index_id = (vj.get("assetIndex") or {}).get("id") or vj.get("assets") or ""

    features = {
        "is_demo_user": False,
        "has_custom_resolution": not fullscreen,
        "has_quick_plays_support": False,
        "is_quick_play_singleplayer": False,
        "is_quick_play_multiplayer": False,
        "is_quick_play_realms": False,
    }

    # 外置登录（Yggdrasil / 微软正版）使用真实令牌
    is_authed = (account.get("type") in ("yggdrasil", "microsoft")) and bool(account.get("accessToken"))
    token = account.get("accessToken") if is_authed else "0"

    ph = {
        "auth_player_name": account.get("name", "Player"),
        "version_name": vid,
        "game_directory": game_dir,
        "assets_root": assets_dir,
        "assets_index_name": asset_index_id,
        "asset_index_name": asset_index_id,
        "auth_uuid": account.get("uuid", ""),
        "auth_access_token": token,
        "auth_session": token,
        "user_type": "mojang" if is_authed else "legacy",
        "version_type": vj.get("type", "release"),
        "user_properties": "{}",
        "resolution_width": str(width),
        "resolution_height": str(height),
        "natives_directory": natives,
        "launcher_name": "CuibingLauncher",
        "launcher_version": core.APP_VERSION,
        "classpath_separator": os.pathsep,
        "game_assets": assets_dir,
        "auth_xuid": "",
        "clientid": "",
        "quickPlayPath": "",
        "quickPlaySingleplayer": "",
        "quickPlayMultiplayer": "",
        "quickPlayRealms": "",
    }

    # 解压 natives（惰性，缺文件时执行）
    try:
        vmod.extract_natives(vid, cfg)
    except Exception:
        pass

    classpath = build_classpath(vid, vj, cfg)
    ph["classpath"] = classpath

    # ---------- JVM 参数 ----------
    jvm: List[str] = [ji["path"]]

    # 老版本模板里常见的 Intel 驱动 workaround
    jvm.append("-XX:HeapDumpPath=MojangTricksIntelDriversForPerformance_javaw.exe_minecraft.exe.heapdump")
    jvm.extend(_gc_args(java_major, _win_ok_for_zgc()))
    jvm.append(f"-Xmx{mem}M")
    jvm.extend(_encoding_args(java_major))
    jvm.append("-Dfml.ignoreInvalidMinecraftCertificates=true")
    jvm.append("-Dfml.ignorePatchDiscrepancies=true")
    jvm.append("-Dlog4j2.formatMsgNoLookups=true")
    jvm.append("-Dminecraft.launcher.brand=CuibingLauncher")
    jvm.append("-Dminecraft.launcher.version=" + core.APP_VERSION)

    # 外置登录：注入 authlib-injector，皮肤与正版验证才能走自定义登录源
    injector = ""
    if account.get("type") == "yggdrasil" and cfg.get("authInjector", True):
        server = account.get("server") or cfg.get("authServer") or ""
        if server:
            injector = core.ensure_authlib_injector() or ""
        if injector:
            jvm.append(f"-javaagent:{injector}={server}")
            jvm.append("-Dauthlibinjector.side=client")

    # 日志配置（Log4j2 漏洞防护）
    log_item, log_arg = vmod.collect_logging(vj, cfg)
    if log_item and os.path.isfile(log_item.path):
        ph["log_path"] = log_item.path
        if "${log_path}" in log_arg:
            jvm.append(_fmt(log_arg, ph))

    raw_jvm = (vj.get("arguments") or {}).get("jvm") or []
    rendered_jvm = _render(raw_jvm, ph, features)
    jvm.extend(rendered_jvm)

    joined = " ".join(jvm).lower()
    if "java.library.path" not in joined:
        jvm.append(f"-Djava.library.path={natives}")
    if classpath not in " ".join(jvm):
        jvm.append("-cp")
        jvm.append(classpath)

    # 用户自定义 JVM 参数（版本设置 > 主设置）
    extra = (opts.get("jvmArgs") or st.get("jvmArgs") or "").strip()
    if extra:
        parts = [x for x in extra.split() if x]
        # 实时自动模式下，参数里写死的 -Xmx 会让自动分配失效，直接剔除
        # （想要固定值请关掉「自动分配」，或在版本设置里单独指定内存）
        if mem_src == "实时自动":
            parts = [p for p in parts if not re.match(r"-Xmx", p, re.I)]
        jvm.extend(parts)

    # ---------- 游戏参数 ----------
    # 去重：版本 JSON / 默认模板 / 用户自定义都可能重复给同一个参数
    jvm = _dedup_args(jvm)
    main_class = vj.get("mainClass", "net.minecraft.client.main.Main")
    jvm.append(main_class)

    game_args: List[str] = []
    arguments = vj.get("arguments")
    if arguments and arguments.get("game"):
        game_args = _render(arguments.get("game") or [], ph, features)
    else:
        legacy = vj.get("minecraftArguments", "")
        if legacy:
            game_args = _fmt(legacy, ph).split()

    # 兜底：老版本模板里可能没有这些必要参数
    must = [("--username", ph["auth_player_name"]),
            ("--uuid", ph["auth_uuid"]),
            ("--accessToken", token),
            ("--userType", "mojang" if is_authed else "legacy"),
            ("--version", vid),
            ("--gameDir", game_dir),
            ("--assetsDir", assets_dir),
            ("--assetIndex", asset_index_id)]
    for flag, val in must:
        if flag not in game_args:
            game_args.extend([flag, val])

    if not fullscreen:
        if "--width" not in game_args:
            game_args.extend(["--width", str(width)])
        if "--height" not in game_args:
            game_args.extend(["--height", str(height)])
    if "--versionType" not in game_args:
        game_args.extend(["--versionType", ph["version_type"]])

    argv = jvm + game_args

    meta = {
        "version": vid,
        "java": ji["path"],
        "javaVersion": ji.get("version", ""),
        "javaSource": ji.get("source", ""),
        "javaNeed": _req_text(req),
        "javaNotes": req.get("notes") or [],
        "memory": mem,
        "memSource": mem_src,
        "memAuto": bool(auto_flag),
        "memInfo": {
            "total": mem_info.get("total"), "available": mem_info.get("available"),
            "load": mem_info.get("load"), "recommended": mem_info.get("recommended"),
        },
        "mainClass": main_class,
        "account": account.get("name", ""),
        "authType": account.get("type") or "offline",
        "width": width,
        "height": height,
        "gameDir": game_dir,
        "gameDirBase": main_dir,
        "isolated": bool(iso.get("isolated")),
        "isolateAuto": bool(iso.get("auto")),
        "isolateReason": iso.get("reason") or "",
        "modCount": mod_count,
        "injector": bool(injector),
    }
    return argv, game_dir, meta


def _req_text(rng: Dict[str, Any]) -> str:
    lo, hi = rng.get("min"), rng.get("max")
    if lo is None and hi is None:
        return "任意"
    parts = []
    if lo is not None:
        parts.append("≥ %d" % lo[0])
    if hi is not None:
        parts.append("<%d" % hi[0] if not rng.get("maxInc") else "≤ %d" % hi[0])
    return " ".join(parts)


# ---------------- 实际启动 ----------------

def _reader(proc: subprocess.Popen, gp: GameProcess, f):
    try:
        for line in iter(proc.stdout.readline, b""):
            if not line:
                break
            try:
                s = line.decode("utf-8", "ignore").rstrip()
            except Exception:
                continue
            with gp._tail_lock:
                gp.tail.append(s)
                if len(gp.tail) > 500:
                    gp.tail.pop(0)
            try:
                f.write(s + "\n")
                f.flush()
            except Exception:
                pass
    except Exception:
        pass


def launch(vid: str, account_name: str = "", cfg: Dict[str, Any] | None = None,
           opts: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = cfg or core.load()

    # 预检查（借鉴 PCL 的 McLaunchPrecheck）：路径里的 ! 和 ; 会直接破坏 classpath
    pre = precheck(vid, cfg)
    if not pre.get("ok"):
        return {"ok": False, "msg": pre.get("msg", "启动前检查未通过")}
    warns = list(pre.get("warns") or [])

    account = None
    for a in (cfg.get("accounts") or []):
        if a.get("name") == account_name:
            account = a
    if account is None:
        account = core.get_active(cfg)
    if account is None:
        return {"ok": False, "msg": "还没有账户，请先在账户页面创建或登录"}

    # 正版 / 第三方登录：启动前校验并自动续期令牌
    if account.get("type") in ("yggdrasil", "microsoft"):
        try:
            import auth
            pf = auth.preflight_token(dict(account))
            if not pf.get("ok"):
                return {"ok": False, "msg": pf.get("msg", "登录已失效，请重新登录")}
            account = pf["account"]
        except Exception:
            pass  # 网络不通时用现有令牌尽力尝试

    ready = check_ready(vid, cfg)
    if ready.get("broken"):
        return {"ok": False, "msg": ready.get("msg", "版本文件损坏"), "needInstall": True}
    if not ready["ready"]:
        # PCL 的「补全文件」步骤：少量文件缺失时先补齐再启动，而不是直接拒绝
        if opts.get("autoFix", True) and len(ready["missing"]) <= 40:
            try:
                _repair_files(vid, with_assets=False)
                ready = check_ready(vid, cfg)
                if not ready["ready"]:
                    _repair_files(vid, with_assets=True)
                    ready = check_ready(vid, cfg)
            except Exception:  # noqa: BLE001
                pass
        if not ready["ready"]:
            return {"ok": False, "msg": "游戏文件不完整：" + "、".join(ready["missing"][:6]),
                    "missing": ready["missing"], "needInstall": True}

    try:
        argv, cwd, meta = build_command(vid, account, cfg, opts)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": str(e), "needJava": "Java" in str(e)}

    # 环境变量（PCL 的 McLaunchRun）：
    #  - Java 的 bin 塞进 PATH，部分 Mod / 启动脚本会直接调 java、keytool
    #  - appdata 指到游戏目录的上一层，让认 %appdata%\.minecraft 的老版本/Mod 找对地方
    env = os.environ.copy()
    jbin = os.path.dirname(meta.get("java") or "")
    if jbin and os.path.isdir(jbin):
        env["PATH"] = core.short_path(jbin) + os.pathsep + env.get("PATH", "")
    gd = cwd
    if os.path.basename(gd.rstrip("\\/")).lower() == ".minecraft":
        parent = os.path.dirname(gd.rstrip("\\/"))
        if parent and os.path.isdir(parent):
            env["appdata"] = core.short_path(parent)

    # 语言同步：让游戏语言跟随启动器界面语言
    if cfg.get("syncGameLang", True):
        try:
            code = sync_game_language(cwd, resolve_language(cfg))
            if code:
                meta["gameLanguage"] = code
        except Exception:  # noqa: BLE001
            pass

    # 日志文件
    core.ensure_dirs()
    logfile = os.path.join(core.LOGS_DIR,
                           "game-" + time.strftime("%Y%m%d-%H%M%S") + ".log")
    try:
        logf = open(logfile, "w", encoding="utf-8", errors="ignore")
    except Exception:
        logf = None

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as e:  # noqa: BLE001
        if logf:
            logf.close()
        return {"ok": False, "msg": f"启动失败：{e}", "cmd": argv[:2]}

    gp = GameProcess(proc.pid, vid, account.get("name", ""), proc, logfile,
                     name=str(opts.get("name") or "").strip(),
                     mem_mb=int(meta.get("memory") or 0),
                     mem_auto=bool(meta.get("memAuto", True)),
                     mem_source=str(meta.get("memSource") or ""),
                     profile=str(opts.get("profile") or ""))
    gp.java_path = meta.get("java") or ""
    gp.command = argv
    _register(gp)
    _ensure_monitor()

    if logf and proc.stdout:
        t = threading.Thread(target=_reader, args=(proc, gp, logf), daemon=True)
        t.start()

    with gp._tail_lock:
        # 像 PCL 那样把关键启动信息打进日志，出问题一眼能看出是 Java 还是版本的事
        gp.tail.append("[脆冰] ~ 基础参数 ~")
        gp.tail.append(f"[脆冰] 启动器版本: {core.APP_VERSION}")
        gp.tail.append(f"[脆冰] 游戏版本: {vid}")
        gp.tail.append(f"[脆冰] Java: {meta.get('java')}")
        gp.tail.append(f"[脆冰] Java 版本: {meta.get('javaVersion')}"
                       f"（{meta.get('javaSource') or '本机'}，本版本要求 {meta.get('javaNeed')}）")
        gp.tail.append(f"[脆冰] 分配内存: {meta.get('memory')} MB（{meta.get('memSource')}）")
        mi = meta.get("memInfo") or {}
        if mi.get("total"):
            gp.tail.append(f"[脆冰] 内存状态: 共 {mi.get('total')} MB，"
                           f"启动前可用 {mi.get('available')} MB，占用 {mi.get('load')}%")
        gp.tail.append(f"[脆冰] 主类: {meta.get('mainClass')}")
        gp.tail.append(f"[脆冰] MC 文件夹: {cwd}")
        gp.tail.append(f"[脆冰] 版本隔离: {meta.get('isolated')}"
                       f"（{meta.get('isolateReason') or '—'}）")
        gp.tail.append(f"[脆冰] 模组数量: {meta.get('modCount')}（{cwd} 下 mods 目录）")
        gp.tail.append("[脆冰] ~ 登录参数 ~")
        gp.tail.append(f"[脆冰] 玩家: {meta.get('account')} | 方式: {meta.get('authType')}")
        for n in (meta.get("javaNotes") or []):
            gp.tail.append(f"[脆冰] 提示: {n}")
        for n in warns:
            gp.tail.append(f"[脆冰] 提示: {n}")

    # 记录上次启动的版本
    cfg2 = core.load()
    cfg2["lastVersion"] = vid
    core.save(cfg2)

    # 后台监视：等窗口出现 -> 判定启动成功；进程提前退出 -> 判定崩溃
    def _watch():
        try:
            gp.window_ready = _game_window_appeared(proc.pid, timeout=180.0)
            proc.wait()
            gp.exit_code = proc.returncode
            gp.exited = True
            gp.crashed = (not gp.window_ready) or bool(proc.returncode)
        except Exception:
            pass

    gp.window_ready = False
    gp.crashed = False
    threading.Thread(target=_watch, daemon=True).start()

    return {"ok": True, "id": gp.id, "name": gp.display_name, "pid": proc.pid,
            "meta": meta, "logfile": logfile, "warns": warns}


# ---------------- 游戏语言同步 ----------------

# 启动器语言 -> 游戏语言代码
_GAME_LANG = {"zh": "zh_cn", "en": "en_us"}


def resolve_language(cfg: Dict[str, Any] | None = None) -> str:
    """把 language 配置解析成 zh / en（auto 时跟随系统）。"""
    cfg = cfg or core.load()
    v = (cfg.get("language") or "auto").lower()
    if v in ("zh", "zh-cn", "zh_cn", "chinese"):
        return "zh"
    if v in ("en", "en-us", "en_us", "english"):
        return "en"
    # auto：按系统区域判断
    try:
        import locale

        loc = (locale.getdefaultlocale()[0] or "").lower()
        if loc.startswith("zh"):
            return "zh"
        if loc:
            return "en"
    except Exception:  # noqa: BLE001
        pass
    return "zh"


def sync_game_language(game_dir: str, lang: str) -> Optional[str]:
    """
    把 options.txt 里的 lang 项改成与界面一致的语言。
    返回写入后的语言代码；目录不可写则返回 None。
    """
    code = _GAME_LANG.get(lang, "zh_cn")
    try:
        os.makedirs(game_dir, exist_ok=True)
    except Exception:  # noqa: BLE001
        return None
    path = os.path.join(game_dir, "options.txt")
    lines: List[str] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:  # noqa: BLE001
            lines = []
    hit = False
    for i, ln in enumerate(lines):
        if ln.startswith("lang:"):
            lines[i] = "lang:" + code
            hit = True
            break
    if not hit:
        lines.append("lang:" + code)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        return None
    return code


# ---------------- 崩溃分析 ----------------
# 每条规则：(任一关键词命中, 标题, 可能原因, 解决方式列表)
_CRASH_RULES: List[Tuple[Tuple[str, ...], str, str, List[str]]] = [
    (("java.lang.OutOfMemoryError", "Java heap space", "There is insufficient memory"),
     "内存不足",
     "分配给游戏的内存不够，或同时加载了太多模组、光影、高清材质。",
     ["把内存上限调大（建议不超过物理内存的一半）",
      "先移除光影或高分辨率材质包试试",
      "关闭其它占用内存的程序后重试"]),

    (("Pixel format not accelerated", "Could not create context", "GLXBadContext",
      "WGL_NV_render_texture_rectangle", "no OpenGL context found"),
     "显卡驱动 / OpenGL 问题",
     "显卡驱动过旧或缺失，游戏无法创建 OpenGL 渲染环境。",
     ["更新显卡驱动（去 NVIDIA / AMD / Intel 官网）",
      "在游戏视频设置里关闭垂直同步、切换全屏 / 窗口模式",
      "双显卡机器请指定用独立显卡运行"]),

    (("UnsupportedClassVersionError", "class file version",
      "Unsupported major.minor version"),
     "Java 版本不匹配",
     "当前 Java 版本与该游戏版本要求的 Java 不一致。",
     ["1.16 及更早 → 用 Java 8",
      "1.17 ~ 1.20.4 → 用 Java 17",
      "1.20.5 及更新 → 用 Java 21",
      "在设置页手动指定 javaw.exe"]),

    (("ClassNotFoundException", "NoClassDefFoundError",
      "Could not find or load main class", "Failed to find Minecraft main class"),
     "游戏文件缺失或损坏",
     "运行库或主 jar 缺失，通常是下载中断、杀毒软件误删导致。",
     ["在版本页点「重新安装」修复该版本",
      "把游戏目录加入杀毒软件白名单后重新下载"]),

    (("Mod resolution failed", "mod resolution", "Failed to load mod",
      "Mixin apply for mod", "mixin apply failed", "Errors were found"),
     "模组冲突或版本不符",
     "有模组与当前 MC 版本 / 加载器版本不匹配，或多个模组互相冲突。",
     ["核对模组支持的 MC 版本和加载器版本",
      "把 mods 里的模组分批移出，用二分法定位问题模组",
      "重命名 config 目录让模组重新生成配置"]),

    (("javax.net.ssl", "SSLHandshakeException", "Connection timed out",
      "Unable to connect", "java.net.SocketException"),
     "网络问题",
     "游戏在联网校验或加载在线资源时失败。",
     ["检查网络与代理设置", "关闭需要联网的功能", "稍后重试"]),

    (("Invalid or corrupt jarfile", "Invalid signature file", "ZipException"),
     "Jar 文件损坏",
     "下载的文件不完整或被修改过。",
     ["删除该文件后重新下载", "在版本页点「重新安装"]),

    (("Access is denied", "Permission denied"),
     "权限不足",
     "游戏目录没有写入权限，或文件被其它程序占用。",
     ["不要放在需要管理员权限的目录（如 C:\\ 根目录）",
      "关闭占用文件的程序 / 杀毒软件后重试"]),
]


def analyze_crash(log_text: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    根据游戏日志猜测崩溃原因并给出解决办法。
    返回 { "title", "reason", "fixes": [...], "raw": [...] }
    """
    cfg = cfg or core.load()
    text = log_text or ""
    low = text.lower()
    hits: List[Dict[str, Any]] = []

    for keys, title, reason, fixes in _CRASH_RULES:
        for k in keys:
            if k.lower() in low:
                hits.append({"title": title, "reason": reason, "fixes": list(fixes),
                             "keyword": k})
                break

    if not hits:
        # 兜底：按退出码给通用建议
        hits.append({
            "title": "未知原因退出",
            "reason": "没有匹配到已知崩溃特征，可能是文件缺失、模组冲突或 Java 环境问题。",
            "fixes": [
                "用「文件体检」检查并补齐游戏文件",
                "暂时移走 mods 目录，确认是否模组引起",
                "换一个 Java 版本再试",
                "查看日志最后几行的 Exception 信息",
            ],
            "keyword": "",
        })

    return {"ok": True, "items": hits, "count": len(hits)}


def stop_instance(iid: str = "") -> Dict[str, Any]:
    """结束某个实例的游戏进程；不传 id 就结束最近启动的那个。"""
    gp = get_instance(iid) if iid else current_process()
    if not gp:
        return {"ok": False, "msg": "没有这个实例"}
    if not gp.alive:
        return {"ok": True, "msg": "这个实例已经停止了", "id": gp.id}
    try:
        gp.proc.terminate()
        for _ in range(20):
            if gp.proc.poll() is not None:
                break
            time.sleep(0.15)
        if gp.proc.poll() is None:
            gp.proc.kill()
        return {"ok": True, "msg": "已结束游戏进程", "id": gp.id}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": str(e), "id": gp.id}


def stop_game() -> Dict[str, Any]:
    return stop_instance("")


def close_instance(iid: str) -> Dict[str, Any]:
    """关闭标签页：进程还活着就先结束，再从实例表里移除。"""
    gp = get_instance(iid)
    if not gp:
        return {"ok": True, "msg": "标签已消失"}
    if gp.alive:
        try:
            gp.proc.terminate()
            for _ in range(20):
                if gp.proc.poll() is not None:
                    break
                time.sleep(0.15)
            if gp.proc.poll() is None:
                gp.proc.kill()
        except Exception:
            pass
    _forget(iid)
    return {"ok": True, "msg": "标签已关闭"}


def set_instance_memory(iid: str, auto: bool | None = None,
                        mb: int | None = None) -> Dict[str, Any]:
    """
    调整某个实例的内存设置。

    注意：正在运行的 JVM 改不了 -Xmx，所以这里改的是「这个实例之后的自动分配策略」，
    立即生效的是目标值与空闲堆归还；新的上限在下次启动时使用。
    """
    gp = get_instance(iid)
    if not gp:
        return {"ok": False, "msg": "没有这个实例"}
    if auto is not None:
        gp.mem_auto = bool(auto)
        gp.mem_source = "实时自动" if gp.mem_auto else "手动设置"
    if mb:
        gp.mem_mb = max(1024, min(int(mb), core.slider_max_mb()))
        gp.target_mb = gp.mem_mb
        if not gp.mem_auto:
            gp.mem_source = "手动设置"
    return {"ok": True, "memory": {
        "limit": gp.mem_mb, "auto": gp.mem_auto, "source": gp.mem_source,
        "used": gp.rss_mb, "peak": gp.peak_mb, "target": gp.target_mb,
        "note": gp.auto_note, "cpu": round(gp.cpu, 1),
    }}


def instances_snapshot() -> Dict[str, Any]:
    """给界面用的完整快照：所有标签 + 本机内存状态。"""
    info = core.system_memory_info()
    insts = []
    for g in list_instances():
        d = g.to_dict()
        # 轮询频繁，日志只带最近一段，避免每 1.2 秒传几百行
        if len(d.get("log") or []) > 200:
            d["log"] = d["log"][-200:]
        insts.append(d)
    return {
        "instances": insts,
        "memory": {
            "total": info["total"], "available": info["available"],
            "load": info["load"], "sliderMax": core.slider_max_mb(info["total"]),
        },
    }
