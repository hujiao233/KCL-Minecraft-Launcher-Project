# -*- coding: utf-8 -*-
"""
脆冰启动器 (Cuibing Launcher) - 入口

用法：
  python main.py                 启动（后台自动体检补齐）
  python main.py --bootstrap     引导器调用：补齐后启动
  python main.py --no-check      跳过体检
  python main.py --check         只体检不启动（输出到控制台）
  python main.py --build         把本体打包成上级目录的单文件引导器 .bat
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import webbrowser

# pythonw.exe 下 stdout 为 None，先兜底，避免 print 报错
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import core  # noqa: E402


def wait_ready(url: str, timeout: float = 20.0) -> bool:
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "/api/hello", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def start_selfcheck_bg(delay: float = 1.0):
    """服务起来之后后台体检，缺失的游戏 / mod 文件自动补齐。"""
    def _run():
        try:
            time.sleep(delay)
            import server
            server.start_selfcheck_async(auto_repair=True)
        except Exception:
            traceback.print_exc()

    import threading
    threading.Thread(target=_run, daemon=True).start()


def grow_exe_if_requested(delay: float = 8.0):
    """
    精简版 exe（压缩启动器.exe）的自我补全：
    首次运行联网补齐的运行环境，会被打包回 exe 本体里（exe 变大），
    之后再启动、或者把 exe 拷到别的电脑，都不用重新下载。
    """
    def _run():
        try:
            time.sleep(delay)          # 等引导器进程退出，exe 才能写
            import base64
            import io
            import json
            import zipfile

            req = os.path.join(BASE_DIR, "_grow.json")
            if not os.path.isfile(req):
                return
            try:
                with open(req, "r", encoding="utf-8") as f:
                    info = json.load(f)
                os.remove(req)
            except Exception:  # noqa: BLE001
                return

            exe = info.get("exe") or ""
            if not exe or not os.path.isfile(exe):
                return
            with open(exe, "rb") as f:
                data = f.read()
            ma, mb = b"::CBPB__", b"::CBPE__"
            i, j = data.find(ma), data.rfind(mb)
            if i < 0 or j <= i:
                return
            head, tail = data[:i], data[j + len(mb):]
            old_zip = base64.b64decode(b"".join(data[i + len(ma):j].split()))

            buf = io.BytesIO()
            skip_dir = {"__pycache__"}
            with zipfile.ZipFile(io.BytesIO(old_zip)) as zin, \
                    zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
                for n in zin.namelist():
                    try:
                        zout.writestr(n, zin.read(n))
                    except Exception:  # noqa: BLE001
                        pass
                for d in (info.get("dirs") or []):
                    root = os.path.join(BASE_DIR, d)
                    if not os.path.isdir(root):
                        continue
                    for dp, dns, fns in os.walk(root):
                        dns[:] = [x for x in dns if x not in skip_dir]
                        for fn in fns:
                            if fn.lower().endswith((".pyc", ".pyo")):
                                continue
                            p = os.path.join(dp, fn)
                            rel = os.path.relpath(p, BASE_DIR).replace("\\", "/")
                            try:
                                if os.path.getsize(p) == 0:
                                    continue
                                zout.write(p, rel)
                            except OSError:
                                pass
            raw = buf.getvalue()
            b64 = base64.b64encode(raw)
            payload = b"\n" + b"\n".join(
                b64[k:k + 100] for k in range(0, len(b64), 100)) + b"\n"
            new = head + ma + payload + mb + tail

            tmp = exe + ".grow.tmp"
            with open(tmp, "wb") as f:
                f.write(new)
            # 先校验，确认新本体能正常解开再替换，绝不留下坏 exe
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(
                    b"".join(new[new.find(ma) + len(ma):new.rfind(mb)].split())))) as z:
                z.testzip()

            bak = exe + ".bak"
            if os.path.isfile(bak):
                os.remove(bak)
            os.replace(exe, bak)
            try:
                os.replace(tmp, exe)
            except Exception:  # noqa: BLE001
                os.replace(bak, exe)     # 写不进去就还原，下次再试
                raise
            try:
                os.remove(bak)
            except OSError:
                pass
            print("[脆冰] 本体已自我补全：%.1f MB（以后启动不用再下载运行环境）"
                  % (os.path.getsize(exe) / 1048576))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    threading.Thread(target=_run, daemon=True).start()


# ==================================================================
# 引导器打包：把本体压成单文件 .bat
# ==================================================================
# 结构说明：
#   1. 开头一段纯 ASCII 的 cmd 脚本（cmd 按 GBK 读取，所以正文不能有中文）
#   2. 文件末尾用 ::CBPB__ / ::CBPE__ 两个标记夹一段 base64，内容是全部本体文件的 zip
#   3. 运行时：PowerShell 正则取出这段 base64 还原成 zip → 交给 Python 解压到 .\CL
#   4. 之后定位 Python（排除 WindowsApps 商店 stub）、补依赖、启动 main.py

BAT_TEMPLATE = r"""@echo off
setlocal
rem ===== Cuibing Launcher - single file bootstrap =====
set "CB_SELF=%~f0"
set "CB_DIR=%~dp0"
set "CB_ZIP=%TEMP%\cb_payload.zip"
set "CB_PYF=%TEMP%\cb_python.txt"
set "CB_LOG=%TEMP%\CuibingLauncher-boot.log"
if "%~1"=="__cbh" goto :MAIN
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath cmd.exe -ArgumentList ('/c ' + [char]34 + $env:CB_SELF + [char]34 + ' __cbh') -WindowStyle Hidden" >nul 2>&1
if errorlevel 1 goto :MAIN
exit /b

:MAIN
if exist "%CB_ZIP%" del /f /q "%CB_ZIP%" >nul 2>&1
if exist "%CB_PYF%" del /f /q "%CB_PYF%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:CB_SELF);$m=[regex]::Match($t,'::CBPB__([A-Za-z0-9+/=\s]+?)::CBPE__',16);if($m.Success){[IO.File]::WriteAllBytes($env:CB_ZIP,[Convert]::FromBase64String($m.Groups[1].Value.Trim()))}" >>"%CB_LOG%" 2>&1
if not exist "%CB_ZIP%" goto :FAIL

powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=Split-Path -Parent $env:CB_SELF;$cs=@('__PYPATH__');$cs+=(Get-Command pythonw -ErrorAction 0).Source;$cs+=(Get-Command python -ErrorAction 0).Source;$cs+=(Get-Command py -ErrorAction 0).Source;$p='';foreach($x in $cs){if($x -and (Test-Path $x) -and ($x -notlike '*WindowsApps*')){$p=$x;break}};Set-Content -LiteralPath $env:CB_PYF -Value $p -Encoding ASCII" >>"%CB_LOG%" 2>&1
set "CB_PY="
if exist "%CB_PYF%" set /p CB_PY=<"%CB_PYF%"
if not defined CB_PY goto :NOPY

"%CB_PY%" -c "import zipfile,sys,os;os.makedirs(sys.argv[2],exist_ok=True);zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "%CB_ZIP%" "%CB_DIR%CL" >>"%CB_LOG%" 2>&1
del /f /q "%CB_ZIP%" >nul 2>&1
if not exist "%CB_DIR%CL\main.py" goto :FAIL

"%CB_PY%" -c "import requests,flask" >nul 2>&1
if errorlevel 1 start "" /b "%CB_PY%" -m pip install -q --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple requests flask
"%CB_PY%" -c "import webview" >nul 2>&1
if errorlevel 1 start "" /b "%CB_PY%" -m pip install -q --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple pywebview

cd /d "%CB_DIR%CL"
start "" /b "%CB_PY%" main.py --bootstrap
exit /b

:FAIL
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell;$w.Popup('Failed to extract launcher files. Re-download this file.',0,'Cuibing Launcher',16)|Out-Null"
exit /b

:NOPY
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell;$w.Popup('Python 3.10+ not found. Install Python and enable Add to PATH.',0,'Cuibing Launcher',16)|Out-Null"
exit /b

::CBPB__
__PAYLOAD__
::CBPE__
"""


def build_bootstrap(out_dir: str = "") -> str:
    """把启动器本体打包成单文件引导器（上级目录的 脆冰启动器.bat）。"""
    import base64
    import io
    import zipfile

    import server

    out_dir = out_dir or os.path.dirname(BASE_DIR)
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in server.LAUNCHER_FILES:
            p = os.path.join(BASE_DIR, rel)
            if not os.path.isfile(p):
                print("[脆冰] 缺少文件，跳过：", rel)
                continue
            z.write(p, rel.replace("\\", "/"))
            n += 1
    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode("ascii")
    lines = [b64[i:i + 100] for i in range(0, len(b64), 100)]

    # 优先 pythonw.exe（无黑框），找不到就用当前解释器
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pyw):
        pyw = sys.executable

    bat = BAT_TEMPLATE.replace("__PAYLOAD__", "\n".join(lines)).replace("__PYPATH__", pyw)
    # cmd 按 ANSI(GBK) 读取 bat，这里用 GBK 保存，保证中文提示不乱码
    out = os.path.join(out_dir, "脆冰启动器.bat")
    with open(out, "w", encoding="gbk", errors="ignore", newline="\r\n") as f:
        f.write(bat)
    print(f"[脆冰] 已生成引导器：{out}")
    print(f"[脆冰] 内嵌 {n} 个文件，压缩包 {len(raw) / 1024:.0f} KB，"
          f"引导器 {os.path.getsize(out) / 1024:.0f} KB")
    return out


# ==================================================================
# 无边框窗口：标题栏拖动 + 四边四角原生拉伸（纯 ctypes，零依赖）
# ==================================================================

_WIN_HOOKS: dict = {}


def _install_frameless_controller(window, title_height: int = 50, border: int = 7):
    """
    给无边框窗口装上原生拖动（**只允许拖动，不允许改变窗口大小**）。

    原理：子类化窗口过程，接管 WM_NCHITTEST——
      · 标题栏区域返回 HTCAPTION  → 系统级拖动
      · 其余区域一律 HTCLIENT    → 没有拉伸热区，鼠标不会变成双向箭头
    另外把双击标题栏最大化、WM_SYSCOMMAND 的 SC_MAXIMIZE / SC_SIZE、
    Win+↑ 贴边分屏，以及窗口样式里的 WS_THICKFRAME / WS_MAXIMIZEBOX 全部挡掉。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    # 等 pywebview 把原生窗口建出来（最多 15 秒）
    hwnd = None
    for _ in range(300):
        native = getattr(window, "native", None)
        if native is not None:
            h = getattr(native, "Handle", None)
            if h is not None:
                try:
                    hwnd = int(h.ToInt64()) if hasattr(h, "ToInt64") else int(h)
                    break
                except Exception:
                    pass
            if isinstance(native, int):
                hwnd = native
                break
        time.sleep(0.05)
    if not hwnd:
        print("[脆冰] 未拿到窗口句柄，拖动/拉伸退化为 JS 模式")
        return

    user32 = ctypes.windll.user32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    ENUMCB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GWL_WNDPROC = -4
    WM_NCHITTEST = 0x0084
    WM_NCLBUTTONDOWN = 0x00A1
    WM_NCLBUTTONDBLCLK = 0x00A3
    WM_SYSCOMMAND = 0x0112
    SC_MOVE, SC_SIZE = 0xF010, 0xF000
    SC_MAXIMIZE = 0xF030
    GWL_STYLE = -16
    WS_THICKFRAME = 0x00040000      # 可拉伸边框
    WS_MAXIMIZEBOX = 0x00010000     # 最大化按钮 / Win+↑
    HTCLIENT, HTCAPTION = 1, 2
    HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
    HTTOPLEFT, HTTOPRIGHT = 13, 14
    HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
    RESIZE_HT = {HTLEFT, HTRIGHT, HTTOP, HTBOTTOM,
                 HTTOPLEFT, HTTOPRIGHT, HTBOTTOMLEFT, HTBOTTOMRIGHT}

    if hasattr(user32, "SetWindowLongPtrW"):
        SetWindowLongPtr = user32.SetWindowLongPtrW
        GetWindowLongPtr = user32.GetWindowLongPtrW
    else:  # 32 位 Python 回退
        SetWindowLongPtr = user32.SetWindowLongW
        GetWindowLongPtr = user32.GetWindowLongW
    CallWindowProc = user32.CallWindowProcW
    SendMessage = user32.SendMessageW

    SetWindowLongPtr.restype = WNDPROC
    SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int, WNDPROC]
    GetWindowLongPtr.restype = LRESULT
    GetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int]
    CallWindowProc.restype = LRESULT
    CallWindowProc.argtypes = [WNDPROC, wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM]
    SendMessage.restype = LRESULT
    SendMessage.argtypes = [wintypes.HWND, wintypes.UINT,
                            wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ENUMCB, wintypes.LPARAM]
    _WIN_HOOKS["hwnd"] = hwnd

    # WM_NCHITTEST 用的是物理像素，顶栏高度按 DPI 换算
    scale = 1.0
    try:
        if hasattr(user32, "GetDpiForWindow"):
            user32.GetDpiForWindow.argtypes = [wintypes.HWND]
            user32.GetDpiForWindow.restype = wintypes.UINT
            dpi = user32.GetDpiForWindow(wintypes.HWND(hwnd))
            if dpi:
                scale = dpi / 96.0
    except Exception:
        pass
    th = max(24, int(title_height * scale))
    bd = max(4, int(border * scale))
    btn_w = int(150 * scale)

    def hit_test(x, y):
        """只有顶栏可拖动；四边四角一律 HTCLIENT —— 窗口不能拉伸。"""
        try:
            rc = wintypes.RECT()
            if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
                return HTCLIENT
            # 顶栏：右侧留给窗口按钮（最小化 / 关闭）
            if y <= rc.top + th and x < rc.right - btn_w:
                return HTCAPTION
        except Exception:
            pass
        return HTCLIENT

    def make_proc(target, old):
        """target=顶层时直接判定；target=子窗口（WebView2）时只改写 HTCLIENT。"""
        def _proc(h, msg, wp, lp):
            if msg == WM_NCHITTEST:
                try:
                    x = ctypes.c_int16(lp & 0xFFFF).value
                    y = ctypes.c_int16((lp >> 16) & 0xFFFF).value
                    if h == target:
                        ht = hit_test(x, y)
                        return ht if ht != HTCLIENT else HTCLIENT
                    res = CallWindowProc(old, wintypes.HWND(h), msg, wp, lp)
                    if res == HTCLIENT:
                        ht = hit_test(x, y)
                        if ht != HTCLIENT:
                            return ht
                    return res
                except Exception:
                    pass
                return HTCLIENT if h == target else CallWindowProc(
                    old, wintypes.HWND(h), msg, wp, lp)
            if msg == WM_NCLBUTTONDBLCLK and wp == HTCAPTION:
                # 双击标题栏 = 系统默认的最大化，这里直接吃掉
                return 0
            if msg == WM_NCLBUTTONDOWN and wp == HTCAPTION:
                # 消息落在子窗口上时，转发给顶层窗口让系统接管「移动」
                try:
                    SendMessage(wintypes.HWND(hwnd), WM_SYSCOMMAND,
                                SC_MOVE | HTCAPTION, lp)
                    return 0
                except Exception:
                    pass
            if msg == WM_SYSCOMMAND:
                # 最大化 / 全屏 / 拉伸（含 Win+↑ 贴边分屏）一律拒绝
                # SC_SIZE 家族是 0xF000~0xF00F，SC_MOVE(0xF010) 要放行，否则拖不动
                if wp == SC_MAXIMIZE or (SC_SIZE <= wp < SC_MOVE):
                    return 0
            return CallWindowProc(old, wintypes.HWND(h), msg, wp, lp)
        return WNDPROC(_proc)

    # 0) 窗口样式：清掉可拉伸边框与最大化能力（不改 WNDPROC 那套 argtypes）
    try:
        LONG_FN = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_ssize_t)
        GET_FN = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_int)
        set_long = LONG_FN(ctypes.cast(SetWindowLongPtr, ctypes.c_void_p).value)
        get_long = GET_FN(ctypes.cast(GetWindowLongPtr, ctypes.c_void_p).value)
        style = get_long(wintypes.HWND(hwnd), GWL_STYLE)
        new_style = style & ~(WS_THICKFRAME | WS_MAXIMIZEBOX)
        if style and new_style != style:
            set_long(wintypes.HWND(hwnd), GWL_STYLE, new_style)
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT]
            SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
            SWP_NOZORDER, SWP_FRAMECHANGED = 0x0004, 0x0020
            user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER |
                                SWP_FRAMECHANGED)
            print("[脆冰] 窗口尺寸已锁定：不可拉伸 / 不可最大化（仍可拖动）")
    except Exception as exc:
        print("[脆冰] 窗口样式锁定失败（不影响拖动）：", exc)

    # 1) 顶层窗口
    old_ptr = GetWindowLongPtr(wintypes.HWND(hwnd), GWL_WNDPROC)
    old_proc = ctypes.cast(old_ptr, WNDPROC)   # 保留原过程地址
    new_proc = make_proc(hwnd, old_proc)
    _WIN_HOOKS["proc"] = new_proc              # 防 GC 回收
    _WIN_HOOKS["old"] = old_proc
    SetWindowLongPtr(wintypes.HWND(hwnd), GWL_WNDPROC, new_proc)

    # 2) 同进程的子窗口（若有的话，一并接管）
    #    注意：WebView2 的窗口属于 Edge 进程，SetWindowLongPtr 跨进程一定失败，
    #    所以拖动 / 拉伸不能指望子窗口转发，必须走下面的 JS + 轮询方案。
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]

    def collect(parent, depth=0):
        found = []
        def _cb(h, _l):
            found.append(h)
            return True
        user32.EnumChildWindows(wintypes.HWND(parent), ENUMCB(_cb), 0)
        for h in found:
            if h == hwnd:
                continue
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(h), ctypes.byref(pid))
            if pid.value != os.getpid():       # 跨进程：跳过，避免无意义的异常
                continue
            kids.append(h)
            if depth < 5:
                collect(h, depth + 1)

    kids: list = []
    try:
        collect(hwnd)
    except Exception:
        pass

    child_procs = []
    installed = 0
    for h in kids:
        try:
            p = GetWindowLongPtr(wintypes.HWND(h), GWL_WNDPROC)
            if not p:
                continue
            old = ctypes.cast(p, WNDPROC)
            new = make_proc(h, old)
            SetWindowLongPtr(wintypes.HWND(h), GWL_WNDPROC, new)
            child_procs.append(new)            # 防 GC 回收
            installed += 1
        except Exception:
            continue
    _WIN_HOOKS["child_procs"] = child_procs
    print(f"[脆冰] 窗口控制已安装：顶层 + {installed} 个同进程子窗口（DPI {scale:.2f}）")


# ==================================================================
# 拖动 / 拉伸：JS 只负责“按下”，真正的位置计算在下面这个轮询循环里
# ==================================================================
# 为什么不用 WM_SYSCOMMAND(SC_MOVE/SC_SIZE)：
#   页面区域被 WebView2 子窗口盖住，鼠标消息根本不会落到顶层窗口，
#   系统的模态拖动循环拿不到鼠标，于是就“拖不动 / 拉不动”。
# 直接轮询光标 + SetWindowPos 不依赖任何消息路由，最稳。

_DRAG: dict = {"gen": 0}

# 界面的设计画布比例（与 ui/index.html 里的 DESIGN_W / DESIGN_H 一致）
UI_RATIO = 1020.0 / 680.0


def _ui_scale_on():
    """旧版「等比缩放」已弃用：界面改回按窗口自适应，拉伸不再锁定比例。"""
    return False


def _fit_ratio(edge, l, t, r, b, l0, t0, r0, b0):
    """把拉伸结果修正回设计比例：锚住不动的那条边/那个角，另一边按比例跟着变。"""
    MIN_W, MIN_H = 880, 587
    if edge in ("left", "right"):
        w = max(MIN_W, r - l)
        h = int(round(w / UI_RATIO))
        if h < MIN_H:
            h = MIN_H
            w = int(round(h * UI_RATIO))
        cy = (t0 + b0) // 2
        if edge == "left":
            return r0 - w, cy - h // 2, r0, cy - h // 2 + h
        return l0, cy - h // 2, l0 + w, cy - h // 2 + h
    if edge in ("top", "bottom"):
        h = max(MIN_H, b - t)
        w = int(round(h * UI_RATIO))
        if w < MIN_W:
            w = MIN_W
            h = int(round(w / UI_RATIO))
        cx = (l0 + r0) // 2
        if edge == "top":
            return cx - w // 2, b0 - h, cx - w // 2 + w, b0
        return cx - w // 2, t0, cx - w // 2 + w, t0 + h

    # 四角：锚住对角
    w = max(MIN_W, (r - l0) if "right" in edge else (r0 - l))
    h = int(round(w / UI_RATIO))
    if h < MIN_H:
        h = MIN_H
        w = int(round(h * UI_RATIO))
    if edge == "bottomright":
        return l0, t0, l0 + w, t0 + h
    if edge == "bottomleft":
        return r0 - w, t0, r0, t0 + h
    if edge == "topright":
        return l0, b0 - h, l0 + w, b0
    return r0 - w, b0 - h, r0, b0          # topleft


def _window_drag_loop(hwnd, gen, mode, edge, start_rc, sx, sy):
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                       ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
        VK_LBUTTON = 0x01
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        MIN_W, MIN_H = 760, 520
        lock_ratio = (mode == "resize") and _ui_scale_on()

        l0, t0, r0, b0 = start_rc
        p = wintypes.POINT()
        while _DRAG.get("gen") == gen:
            if not (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000):
                break                       # 左键松开了
            if not user32.GetCursorPos(ctypes.byref(p)):
                break
            dx, dy = p.x - sx, p.y - sy
            if mode == "move":
                x, y = l0 + dx, t0 + dy
                w, h = r0 - l0, b0 - t0
            else:
                l, t, r, b = l0, t0, r0, b0
                if "left" in edge:
                    l = min(l0 + dx, r0 - MIN_W)
                if "right" in edge:
                    r = max(r0 + dx, l0 + MIN_W)
                if "top" in edge:
                    t = min(t0 + dy, b0 - MIN_H)
                if "bottom" in edge:
                    b = max(b0 + dy, t0 + MIN_H)
                if lock_ratio:                  # 等比：窗口与界面一起按比例变
                    l, t, r, b = _fit_ratio(edge, l, t, r, b,
                                            l0, t0, r0, b0)
                x, y, w, h = l, t, r - l, b - t
            if w < MIN_W:
                w = MIN_W
            if h < MIN_H:
                h = MIN_H
            user32.SetWindowPos(wintypes.HWND(hwnd), None, int(x), int(y),
                                int(w), int(h), SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.01)
    except Exception:
        pass
    finally:
        if _DRAG.get("gen") == gen:
            _DRAG["gen"] = gen + 1


def _begin_window_drag(mode="move", edge=""):
    """JS 在顶栏按下时调用。窗口尺寸固定，只接受 move，拉伸一律拒绝。"""
    if mode != "move":
        return False
    hwnd = _WIN_HOOKS.get("hwnd")
    if not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        hw = wintypes.HWND(hwnd)
        if user32.IsZoomed(hw):              # 最大化 / 最小化状态下不折腾
            return False
        rc = wintypes.RECT()
        if not user32.GetWindowRect(hw, ctypes.byref(rc)):
            return False
        p = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(p)):
            return False
        _DRAG["gen"] = _DRAG.get("gen", 0) + 1
        gen = _DRAG["gen"]
        threading.Thread(
            target=_window_drag_loop, daemon=True,
            args=(hwnd, gen, mode, str(edge),
                  (rc.left, rc.top, rc.right, rc.bottom), p.x, p.y),
        ).start()
        return True
    except Exception:
        return False


def _end_window_drag():
    _DRAG["gen"] = _DRAG.get("gen", 0) + 1
    return True


def _after_window_start(window):
    """GUI 循环起来后安装无边框窗口的拖拽/拉伸控制。"""
    try:
        _install_frameless_controller(window)
    except Exception:
        traceback.print_exc()
    try:
        _force_window_icon()
    except Exception:
        pass


def _force_window_icon():
    """兜底：用 Win32 给顶层窗口再设一次图标（WM_SETICON）。

    pywebview 的 Form.Icon 万一加载失败会回退成 python.exe 的图标，
    这里直接 LoadImage 读 cuibing.ico 强刷，保证任务栏一定是脆冰。
    """
    import ctypes
    from ctypes import wintypes

    ico = os.path.join(BASE_DIR, "ui", "assets", "cuibing.ico")
    if not os.path.isfile(ico):
        return
    user32 = ctypes.windll.user32
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
    big = user32.LoadImageW(None, ico, IMAGE_ICON, 32, 32,
                            LR_LOADFROMFILE)
    small = user32.LoadImageW(None, ico, IMAGE_ICON, 16, 16,
                              LR_LOADFROMFILE)
    if not (big or small):
        return
    time.sleep(0.5)  # 等窗口句柄就绪

    def apply(h):
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        if big:
            user32.SendMessageW(wintypes.HWND(h), WM_SETICON,
                                ICON_BIG, wintypes.HICON(big))
        if small:
            user32.SendMessageW(wintypes.HWND(h), WM_SETICON,
                                ICON_SMALL, wintypes.HICON(small))

    # pywebview 有原生句柄就直接用
    try:
        h = window.native.Handle if hasattr(window, "native") else None
    except Exception:
        h = None
    if h:
        apply(int(h))
        return
    # 找不到就按标题扫顶层窗口
    EnumWindows = user32.EnumWindows
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def _cb(h, _l):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(wintypes.HWND(h), buf, 64)
        if "WindowsForms10" in buf.value:  # WinForms 窗口类
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(h),
                                            ctypes.byref(pid))
            if pid.value == os.getpid():
                found.append(h)
        return True

    EnumWindows(WNDENUMPROC(_cb), 0)
    for h in found[:2]:
        apply(h)


def main() -> int:
    argv = sys.argv[1:]

    if "--build" in argv:
        build_bootstrap()
        return 0

    core.ensure_dirs()
    import server

    if "--check" in argv:
        import json
        rep = server.run_selfcheck(auto_repair="--no-repair" not in argv)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep.get("ok") else 2

    info = server.start_server()
    url = info["url"]
    print(f"[脆冰] 本地服务已启动：{url}")

    if not wait_ready(url):
        print("[脆冰] 服务启动超时")
        return 1

    if "--no-check" not in argv:
        start_selfcheck_bg()

    # 精简版 exe：首次运行补齐的运行环境回填进本体（exe 变大，之后再装不用联网）
    grow_exe_if_requested()

    # 优先使用桌面窗口（无边框，标题栏由页面自己画）
    try:
        import webview
    except ImportError:
        # 桌面窗口是主体验：缺 pywebview 时同步补装，绝不能悄悄退化成浏览器
        print("[脆冰] 缺少 pywebview，正在自动安装…")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check",
                        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
                        "pywebview"], timeout=300)
        try:
            import webview  # noqa: F401
        except ImportError:
            traceback.print_exc()
            print("[脆冰] pywebview 安装失败，改用默认浏览器打开")

    try:
        import webview

        class WindowApi:
            """暴露给前端的窗口控制接口。"""

            def __init__(self):
                self.maximized = False

            def minimize(self):
                webview.windows[0].minimize()

            def toggle_maximize(self):
                """窗口尺寸固定：不允许最大化 / 全屏。保留接口只为兼容旧页面。"""
                self.maximized = False
                return False

            def close(self):
                webview.windows[0].destroy()

            def start_drag(self):
                """页面顶栏按下：只移动窗口，不改变大小。"""
                return _begin_window_drag("move")

            def start_resize(self, edge):
                """窗口尺寸固定：拉伸接口保留但不做任何事。"""
                return False

            def stop_drag(self):
                """鼠标松开：结束拖动 / 拉伸。"""
                return _end_window_drag()

        api = WindowApi()
        window = webview.create_window(
            "KCL启动器",
            url,
            js_api=api,
            width=1020,
            height=680,
            min_size=(880, 600),
            background_color="#EAF3FA",
            resizable=False,     # 窗口尺寸固定：不能拉伸
            fullscreen=False,    # 不允许全屏
            zoomable=False,      # 不允许 WebView2 缩放
            frameless=True,      # 去掉系统标题栏，顶栏由页面自己画
            easy_drag=False,     # 拖动交给下面的原生 WM_NCHITTEST，更跟手
            shadow=False,        # 关键：shadow=True 时 DWM 会把玻璃框扩进来，无边框下显示为黑框
        )
        # 任务栏/标题栏图标：窗口进程是 runtime\python.exe，不设 icon 会继承 Python 图标
        app_icon = os.path.join(BASE_DIR, "ui", "assets", "cuibing.ico")
        if not os.path.isfile(app_icon):
            app_icon = None
        webview.start(_after_window_start, (window,), debug=False, icon=app_icon)
        return 0
    except Exception:
        traceback.print_exc()
        print("[脆冰] 桌面窗口不可用，改用默认浏览器打开")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "桌面窗口启动失败，已改用浏览器打开。\n"
                "常见原因是缺少 Edge WebView2 运行库，装上后重启启动器即可恢复窗口模式。",
                "CL启动器", 0x00000030 | 0x00001000)
        except Exception:
            pass

    # 兜底：系统浏览器
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        try:
            input("按回车键退出…")
        except Exception:
            time.sleep(10)
