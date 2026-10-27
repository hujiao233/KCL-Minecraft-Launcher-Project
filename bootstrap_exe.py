# -*- coding: utf-8 -*-
"""
脆冰启动器 - EXE 引导器（单文件、纯标准库）
双击后自动补齐：启动器本体文件 → Python 运行时 → 依赖库 → 启动主程序。
本体文件与便携运行时以 base64 zip 追加在 exe 末尾（::CBPB__ ... ::CBPE__）。
任何一步失败都会弹窗提示，并把过程写入 KCL/启动器日志.txt。
"""
import base64
import io
import os
import subprocess
import sys
import time
import zipfile

MARK_A = b"::CBPB__"
MARK_B = b"::CBPE__"
GROW_REQ = "runtime"          # 精简版：首次运行补齐后要回填进 exe 的目录
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                       else os.path.abspath(__file__)))
KCL_DIR = os.path.join(HERE, "KCL")
RUNTIME_DIR = os.path.join(KCL_DIR, "runtime")
LOG_PATH = os.path.join(KCL_DIR, "启动器日志.txt")
MIRRORS = [
    "https://mirrors.huaweicloud.com/python/{v}/python-{v}-embed-amd64.zip",
    "https://mirrors.aliyun.com/python/{v}/python-{v}-embed-amd64.zip",
    "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip",
]
PY_VER = "3.12.10"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 依赖库：核心（缺了起不来）/ 桌面窗口 / 可选（装不上也能跑）
DEPS_CORE = ["flask", "requests"]
DEPS_UI = ["pywebview"]
DEPS_OPT = ["mcauth3", "pillow"]
# 装包镜像，按顺序轮换重试
PYPI_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.huaweicloud.com/repository/pypi/simple",
    "https://pypi.org/simple",
]
GET_PIP_URLS = [
    "https://mirrors.aliyun.com/pypi/get-pip.py",
    "https://bootstrap.pypa.io/get-pip.py",
]
DEPS_DIR = os.path.join(KCL_DIR, "deps")   # 装不进 runtime 时的兜底目录


# ---------------------------------------------------------------- 日志 / 提示
def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _close_msgbox(title):
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        h = user32.FindWindowW("#32770", title)
        if h:
            user32.SendMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
    except Exception:
        pass


def show_wait(title, text):
    """在子线程弹一个提示框，主线程继续干活，结束调用 hide_wait(title)。"""
    import ctypes

    def run():
        try:
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x00000000 | 0x00001000)
        except Exception:
            pass

    import threading
    threading.Thread(target=run, daemon=True).start()


def hide_wait(title):
    _close_msgbox(title)


def alert(text, title="CL启动器"):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x00000010 | 0x00001000)
    except Exception:
        pass


# ---------------------------------------------------------------- 本体文件
def payload_bytes():
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    with open(exe, "rb") as f:
        data = f.read()
    i = data.find(MARK_A)
    j = data.rfind(MARK_B)
    if i < 0 or j < 0 or j <= i:
        raise RuntimeError("引导数据缺失：这不是完整的脆冰启动器 exe")
    raw = b"".join(data[i + len(MARK_A):j].split())
    return base64.b64decode(raw)


def extract_launcher():
    """本体文件缺哪个补哪个；便携运行时只在缺失时整体释放。"""
    zf = zipfile.ZipFile(io.BytesIO(payload_bytes()))
    names = zf.namelist()
    app = [n for n in names if not n.startswith("runtime/")
           and n != "_payload_meta.json"]
    rtn = [n for n in names if n.startswith("runtime/")]

    missing = [n for n in app if not os.path.isfile(os.path.join(KCL_DIR, n))]
    if missing:
        log("补齐启动器文件：%d 个" % len(missing))
        os.makedirs(KCL_DIR, exist_ok=True)
        zf.extractall(KCL_DIR, members=missing)
    else:
        log("启动器文件已就绪（%d 个）" % len(app))

    if rtn and not os.path.isfile(os.path.join(RUNTIME_DIR, "python.exe")):
        log("释放便携运行环境（%d 个文件）" % len(rtn))
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        zf.extractall(KCL_DIR, members=rtn)


def exe_path() -> str:
    return sys.executable if getattr(sys, "frozen", False) \
        else os.path.abspath(__file__)


def payload_has_runtime() -> bool:
    """本体里有没有自带运行环境（精简版没有，要联网补齐）。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload_bytes()))
        return any(n.startswith("runtime/") for n in zf.namelist())
    except Exception:  # noqa: BLE001
        return False


def _run(py, args, timeout=600):
    """跑一条 python 命令，输出全部收回（exe 无控制台，不收就永远看不到）。"""
    return subprocess.run([py] + list(args), capture_output=True,
                          timeout=timeout, creationflags=NO_WINDOW)


def _tail(data, n=600):
    s = (data or b"").decode("utf-8", "ignore").strip().replace("\r", "")
    return s[-n:] if s else ""


def _pth_file(py):
    """便携版 Python 的 sys.path 配置文件（python3xx._pth）。"""
    d = os.path.dirname(py)
    try:
        for n in os.listdir(d):
            if n.endswith("._pth"):
                return os.path.join(d, n)
    except OSError:
        pass
    return None


def pth_add(py, entry) -> bool:
    """往 ._pth 里补一行；普通 Python 没有 ._pth，返回 False。"""
    p = _pth_file(py)
    if not p:
        return False
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.read().splitlines()]
        if any(l.lower() == entry.lower() for l in lines):
            return True
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")
        log("修补 %s：+%s" % (os.path.basename(p), entry))
        return True
    except OSError as e:  # noqa: BLE001
        log("修补 %s 失败：%s" % (p, e))
        return False


def site_packages(py) -> str:
    return os.path.join(os.path.dirname(py), "Lib", "site-packages")


def ensure_site(py):
    """便携版默认禁用 site，装好的库会 import 不到——这里补上再装包。"""
    pth_add(py, "import site")
    pth_add(py, "Lib\\site-packages")
    try:
        os.makedirs(site_packages(py), exist_ok=True)
    except OSError:
        pass


def _can_import(py, code, extra_path=None) -> bool:
    env = os.environ.copy()
    if extra_path:
        cur = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = extra_path + (os.pathsep + cur if cur else "")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, timeout=90,
                           creationflags=NO_WINDOW, env=env)
        return r.returncode == 0 and b"ok" in ((r.stdout or b"") + (r.stderr or b""))
    except Exception:  # noqa: BLE001
        return False


def deps_ready(py, extra_path=None) -> bool:
    """核心依赖 flask / requests 是否可用（pywebview 由主程序自己补装）。"""
    if extra_path is None and os.path.isdir(DEPS_DIR):
        extra_path = DEPS_DIR
    return _can_import(py, "import flask,requests;print('ok')", extra_path)


def write_grow_request():
    """精简版补齐完成后：让主程序把下载好的运行环境回填进 exe（本体变大）。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        import json
        if not os.path.isdir(RUNTIME_DIR):
            return
        req = os.path.join(KCL_DIR, "_grow.json")
        with open(req, "w", encoding="utf-8") as f:
            json.dump({"exe": exe_path(), "dirs": [GROW_REQ]}, f)
        log("已安排本体自我补全（下次启动时 exe 会把运行环境打包进去）")
    except Exception as e:  # noqa: BLE001
        log("写补全请求失败：" + str(e))


# ---------------------------------------------------------------- Python
def _is_good_py(exe):
    try:
        r = subprocess.run([exe, "-c", "import sys;print(sys.version_info[:2])"],
                           capture_output=True, timeout=25,
                           creationflags=NO_WINDOW)
        out = (r.stdout or b"") + (r.stderr or b"")
        return r.returncode == 0 and b"(3," in out
    except Exception:
        return False


def find_python(prefer_runtime=True, quiet=False):
    """依序找：自带运行时 → 环境变量 → PATH → 常见安装位置。"""
    cands = []
    if prefer_runtime:
        cands += [os.path.join(RUNTIME_DIR, "python.exe"),
                  os.path.join(RUNTIME_DIR, "pythonw.exe")]
    env = os.environ
    for k in ("CB_PYTHON", "PYTHON_HOME"):
        if env.get(k):
            cands.append(os.path.join(env[k], "python.exe"))
    for base in (env.get("LOCALAPPDATA", ""), env.get("APPDATA", ""),
                 env.get("ProgramFiles", r"C:\Program Files")):
        if base:
            p = os.path.join(base, "Python")
            if os.path.isdir(p):
                for n in sorted(os.listdir(p), reverse=True):
                    if n.lower().startswith("python"):
                        cands.append(os.path.join(p, n, "python.exe"))
    for d in env.get("PATH", "").split(os.pathsep):
        if d and os.path.isdir(d):
            cands.append(os.path.join(d, "python.exe"))
    for c in cands:
        if c and os.path.isfile(c) and _is_good_py(c):
            if not quiet:
                log("使用 Python：" + c)
            return c
    return None


def download(url, dest, timeout=180):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "CuibingLauncher/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(dest)


def ensure_python():
    """优先用 exe 内嵌的便携运行时，其次系统 Python，最后才联网下载。"""
    py = find_python(prefer_runtime=True)
    if py:
        return py, False

    # 本体 payload 里带了便携运行时？尝试从 payload 恢复
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload_bytes()))
        rtn = [n for n in zf.namelist() if n.startswith("runtime/")]
        if rtn:
            log("释放便携运行时（%d 个文件）" % len(rtn))
            os.makedirs(RUNTIME_DIR, exist_ok=True)
            zf.extractall(KCL_DIR)
            py = find_python(prefer_runtime=True)
            if py:
                return py, False
    except Exception as e:  # noqa: BLE001
        log("释放运行时失败：" + str(e))

    # 本机装了 Python 但没在候选列表里（例如装在其它盘），也接受
    py = find_python(prefer_runtime=False)
    if py:
        return py, False

    log("没有找到 Python，需要联网下载便携版（约 11 MB，仅此一次）")
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    tmp = os.path.join(RUNTIME_DIR, "py.zip")
    last = None
    for tpl in MIRRORS:
        url = tpl.format(v=PY_VER)
        try:
            log("下载 " + url.split("/")[2])
            download(url, tmp)
            if os.path.getsize(tmp) < 1024 * 1024:
                raise IOError("文件过小")
            with zipfile.ZipFile(tmp) as z:
                z.extractall(RUNTIME_DIR)
            os.remove(tmp)
            py = os.path.join(RUNTIME_DIR, "python.exe")
            if os.path.isfile(py):
                ensure_site(py)      # 让 Lib\site-packages 生效
                log("便携 Python 就绪")
                return py, True
        except Exception as e:  # noqa: BLE001
            last = e
            log("镜像失败：" + str(e))
            continue
    raise RuntimeError("自动下载 Python 失败：" + str(last))


def pip_ok(py) -> bool:
    try:
        return _run(py, ["-m", "pip", "--version"], timeout=90).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def ensure_pip(py) -> bool:
    """便携版 Python 没有 pip，先补上；补不上就没法装依赖。"""
    if pip_ok(py):
        return True
    log("pip 不可用，尝试安装 pip…")
    try:
        r = _run(py, ["-m", "ensurepip", "--default-pip"], timeout=300)
        if r.returncode == 0 and pip_ok(py):
            log("pip 就绪（ensurepip）")
            return True
    except Exception as e:  # noqa: BLE001
        log("ensurepip 失败：" + str(e))
    for url in GET_PIP_URLS:
        gp = os.path.join(os.path.dirname(py), "get-pip.py")
        try:
            download(url, gp)
            if os.path.getsize(gp) < 10000:
                raise IOError("get-pip.py 不完整")
            _run(py, [gp, "--no-warn-script-location"], timeout=300)
        except Exception as e:  # noqa: BLE001
            log("get-pip 失败（%s）：%s" % (url.split("/")[2], e))
        finally:
            try:
                os.remove(gp)
            except OSError:
                pass
        if pip_ok(py):
            log("pip 就绪")
            return True
    log("pip 仍然不可用，无法安装依赖库")
    return False


def pip_install(py, pkgs, index, target=None, timeout=900) -> bool:
    """装一批包；失败时把 pip 的输出写进日志，方便排查。"""
    cmd = ["-m", "pip", "install", "-i", index, "--no-warn-script-location",
           "--disable-pip-version-check", "--no-cache-dir", "--prefer-binary"]
    if target:
        cmd += ["--target", target, "--upgrade"]
    cmd += list(pkgs)
    try:
        r = _run(py, cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("安装超时：%s" % " ".join(pkgs))
        return False
    if r.returncode == 0:
        return True
    log("安装失败（%s，退出码 %s）：%s"
        % (index.split("/")[2], r.returncode,
           _tail((r.stdout or b"") + (r.stderr or b""))))
    return False


def install_optional(py):
    """可选依赖装不上不影响启动，只在缺失时试一次。"""
    if _can_import(py, "import mcauth3,PIL;print('ok')"):
        return
    pip_install(py, DEPS_OPT, PYPI_MIRRORS[0], timeout=600)


def ensure_deps(py):
    """
    检查 flask/requests/pywebview，缺了才装。
    装不上就抛错，绝不静默往下走（以前装失败照样启动，结果 import requests 崩）。
    """
    ensure_site(py)
    if deps_ready(py):
        log("依赖库已就绪")
        install_optional(py)
        return

    log("安装依赖库（flask / requests / pywebview …）")
    if not ensure_pip(py):
        raise RuntimeError("pip 不可用，无法安装依赖库（详见日志）")

    # 1) 常规装法：逐个镜像重试，装完还要真 import 一次才算数
    for idx in PYPI_MIRRORS:
        if not pip_install(py, DEPS_CORE + DEPS_UI, idx):
            continue
        if deps_ready(py):
            log("依赖库安装完成（镜像 %s）" % idx.split("/")[2])
            install_optional(py)
            return
        log("装完仍 import 不到，换下一个镜像")

    # 2) 兜底：装进独立目录，再把目录挂到 ._pth / PYTHONPATH
    log("常规安装失败，改用独立目录安装…")
    os.makedirs(DEPS_DIR, exist_ok=True)
    for idx in PYPI_MIRRORS:
        if not pip_install(py, DEPS_CORE + DEPS_UI, idx, target=DEPS_DIR):
            continue
        if deps_ready(py, DEPS_DIR):
            log("依赖库已装到 %s" % DEPS_DIR)
            install_optional(py)
            return
        if pth_add(py, DEPS_DIR) and deps_ready(py):
            log("依赖库已装到 %s（已写入 ._pth）" % DEPS_DIR)
            install_optional(py)
            return

    raise RuntimeError("依赖库安装失败，请检查网络后重试（详见日志）")


# ---------------------------------------------------------------- 启动
def launch_env():
    """兜底依赖目录要挂进去，主程序才能 import 到。"""
    env = os.environ.copy()
    if os.path.isdir(DEPS_DIR):
        cur = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = DEPS_DIR + (os.pathsep + cur if cur else "")
    return env


def launch(py):
    main_py = os.path.join(KCL_DIR, "main.py")
    log("启动主程序…")
    f = open(LOG_PATH, "a", encoding="utf-8")
    p = subprocess.Popen([py, main_py, "--bootstrap"], cwd=KCL_DIR,
                         stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         env=launch_env(), creationflags=NO_WINDOW)
    # 等主程序把本地服务跑起来（日志里会出现 127.0.0.1）
    deadline = time.time() + 40
    started = False
    while time.time() < deadline:
        time.sleep(0.6)
        if p.poll() is not None:
            break
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as fh:
                tail = fh.read()[-4000:]
            if "127.0.0.1:" in tail:
                started = True
                break
        except Exception:
            pass
    try:
        f.close()
    except Exception:
        pass
    return started, p


def main():
    try:
        os.makedirs(KCL_DIR, exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write("脆冰启动器 启动日志 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass
    log("exe 目录：" + HERE)

    wait_title = "CL启动器 - 正在准备"
    long_job = False
    try:
        extract_launcher()
        slim = not payload_has_runtime()
        if slim:
            log("精简版本体：运行环境在首次运行时联网补齐，之后会回填进 exe")
        py_probe = find_python(prefer_runtime=True, quiet=True)
        if not py_probe or not deps_ready(py_probe):
            long_job = True
            show_wait(wait_title, "首次启动需要准备运行环境（Python + 依赖库），\n"
                                 "完成后窗口会自动打开，并且会把它们打包进 exe，\n"
                                 "以后再启动就不用下载了，请稍候…")
        py, downloaded = ensure_python()
        ensure_deps(py)
        if long_job:
            hide_wait(wait_title)
        if slim:
            write_grow_request()   # 主程序起来之后把运行环境回填进 exe
        started, proc = launch(py)
        if started:
            log("启动完成")
            return 0
        rc = proc.poll()
        msg = ("主程序启动失败（退出码 %s）。\n\n"
               "常见原因是依赖库没装全：可以删掉 KCL\\runtime 文件夹后\n"
               "重新双击启动器，让它重新联网补齐运行环境。\n\n"
               "详细日志：%s" % (rc, LOG_PATH))
        log(msg.replace("\n", " "))
        alert(msg)
        return 1
    except Exception as e:  # noqa: BLE001
        hide_wait(wait_title)
        import traceback
        detail = traceback.format_exc()
        log("启动失败：" + str(e))
        log(detail)
        alert("启动失败：%s\n\n详细日志：%s" % (e, LOG_PATH))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            import traceback
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except Exception:
            pass
        sys.exit(1)
