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
PIP_URL = "https://mirrors.aliyun.com/pypi/get-pip.py"
DEPS_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
PY_VER = "3.12.10"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def deps_ready(py) -> bool:
    try:
        r = subprocess.run(
            [py, "-c", "import flask,requests,webview;print('ok')"],
            capture_output=True, timeout=90, creationflags=NO_WINDOW)
        return r.returncode == 0 and b"ok" in ((r.stdout or b"") + (r.stderr or b""))
    except Exception:  # noqa: BLE001
        return False


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


def find_python(prefer_runtime=True):
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
            for f in os.listdir(RUNTIME_DIR):
                if f.endswith("._pth"):
                    with open(os.path.join(RUNTIME_DIR, f), "a", encoding="utf-8") as fh:
                        fh.write("\nLib\\site-packages\nimport site\n")
            py = os.path.join(RUNTIME_DIR, "python.exe")
            if os.path.isfile(py):
                log("便携 Python 就绪")
                return py, True
        except Exception as e:  # noqa: BLE001
            last = e
            log("镜像失败：" + str(e))
            continue
    raise RuntimeError("自动下载 Python 失败：" + str(last))


def ensure_deps(py):
    """检查 flask/requests/pywebview，缺了才用国内镜像装。"""
    code = "import flask,requests,webview;print('ok')"
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, timeout=90,
                           creationflags=NO_WINDOW)
        if r.returncode == 0 and b"ok" in ((r.stdout or b"") + (r.stderr or b"")):
            log("依赖库已就绪")
            return
    except Exception:
        pass
    log("安装依赖库（flask / requests / pywebview …）")
    chk = subprocess.run([py, "-m", "pip", "--version"], capture_output=True, timeout=90,
                         creationflags=NO_WINDOW)
    if chk.returncode != 0:
        gp = os.path.join(RUNTIME_DIR, "get-pip.py")
        try:
            download(PIP_URL, gp)
            subprocess.run([py, gp, "--no-warn-script-location"], timeout=300,
                           creationflags=NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            log("get-pip 失败：" + str(e))
        finally:
            try:
                os.remove(gp)
            except Exception:
                pass
    subprocess.run([py, "-m", "pip", "install", "-i", DEPS_MIRROR,
                    "--no-warn-script-location", "--disable-pip-version-check",
                    "flask", "requests", "pywebview", "mcauth3", "pillow"],
                   timeout=900, creationflags=NO_WINDOW)


# ---------------------------------------------------------------- 启动
def launch(py):
    main_py = os.path.join(KCL_DIR, "main.py")
    log("启动主程序…")
    f = open(LOG_PATH, "a", encoding="utf-8")
    p = subprocess.Popen([py, main_py, "--bootstrap"], cwd=KCL_DIR,
                         stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
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
        py = find_python(prefer_runtime=True)
        if not py or not deps_ready(py):
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
