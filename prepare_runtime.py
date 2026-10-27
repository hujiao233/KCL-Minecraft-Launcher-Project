# -*- coding: utf-8 -*-
"""
准备便携运行时：把 embeddable Python + 依赖库装到 CL/runtime，
然后由 build_exe.py 打进 EXE，实现"双击即用、无需联网下载"。
用法：python prepare_runtime.py
"""
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(BASE, "runtime")
PY_VER = os.environ.get("CB_PY_VER", "3.12.10")
MIRRORS = [
    "https://mirrors.huaweicloud.com/python/{v}/python-{v}-embed-amd64.zip",
    "https://mirrors.aliyun.com/python/{v}/python-{v}-embed-amd64.zip",
    "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip",
]
GET_PIP = [
    "https://mirrors.aliyun.com/pypi/get-pip.py",
    "https://bootstrap.pypa.io/get-pip.py",
]
# 便携版 Python 没有 distutils，get-pip.py 会直接崩在
# "No module named 'distutils'"，所以优先下载 pip 的 wheel 解压。
# pip 24.x 不依赖 distutils，实测可用。
PIP_VER = "24.2"
PIP_INDEXES = [
    "https://mirrors.aliyun.com/pypi/simple/pip/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/pip/",
    "https://pypi.org/simple/pip/",
]
INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
# 先装好构建后端，遇到只有源码包（没有 wheel）的依赖时才不会卡住
BUILD_PKGS = ["setuptools", "wheel"]
DEPS = ["flask", "requests", "pywebview", "mcauth3", "pillow", "comtypes"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(m):
    print("[runtime] " + m, flush=True)


def download(url, dest, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "CuibingLauncher/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
    return os.path.getsize(dest)


def pip_ok(py) -> bool:
    r = subprocess.run([py, "-m", "pip", "--version"], capture_output=True,
                       creationflags=NO_WINDOW)
    return r.returncode == 0


def pip_wheel_url() -> str:
    """从镜像的 simple 索引里挑出 pip 的 wheel 地址。"""
    import re
    for idx in PIP_INDEXES:
        try:
            req = urllib.request.Request(idx, headers={"User-Agent": "CuibingLauncher/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                html = r.read().decode("utf-8", "ignore")
            links = [u for u in re.findall(r'href="([^"]+)"', html)
                     if "/pip-" in u and "-py3-none-any.whl" in u]
            hit = [u for u in links if "/pip-%s-py3-none-any.whl" % PIP_VER in u]
            if not hit:
                continue
            u = hit[-1].split("#")[0]
            if u.startswith("http"):
                return u
            return idx.split("/simple/")[0] + "/" + u.split("../../")[-1].lstrip("/")
        except Exception as e:  # noqa: BLE001
            log("pip 索引失败（%s）：%s" % (idx.split("/")[2], e))
    return ""


def install_pip(py, sp) -> bool:
    """
    便携版 Python 既没有 pip 也没有 distutils：
    get-pip.py 会在 import distutils 时崩掉，所以直接下 wheel 解压。
    """
    if pip_ok(py):
        return True

    url = pip_wheel_url()
    if url:
        wp = os.path.join(RUNTIME, "_pip.whl")
        try:
            log("下载 " + url.split("/")[-1])
            download(url, wp)
            os.makedirs(sp, exist_ok=True)
            with zipfile.ZipFile(wp) as z:
                z.extractall(sp)
            log("pip 就绪（wheel）")
        except Exception as e:  # noqa: BLE001
            log("pip wheel 失败：" + str(e))
        finally:
            try:
                os.remove(wp)
            except OSError:
                pass
        if pip_ok(py):
            return True

    # 兜底：get-pip.py（自带 distutils 的环境上才走得通）
    gp = os.path.join(RUNTIME, "get-pip.py")
    for url in GET_PIP:
        try:
            log("下载 " + url)
            download(url, gp)
            # 新版 get-pip.py 已移除 --no-setuptools / --no-wheel，带上会直接报错
            subprocess.run([py, gp, "--no-warn-script-location"],
                           cwd=RUNTIME, creationflags=NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            log("失败：" + str(e))
        finally:
            try:
                os.remove(gp)
            except OSError:
                pass
        if pip_ok(py):
            log("pip 就绪（get-pip）")
            return True
    return False


def main():
    if os.path.isdir(RUNTIME) and os.path.isfile(os.path.join(RUNTIME, "python.exe")):
        log("已存在运行时，先清空重建")
    if os.path.isdir(RUNTIME):
        shutil.rmtree(RUNTIME, ignore_errors=True)
    os.makedirs(RUNTIME, exist_ok=True)

    zip_path = os.path.join(BASE, "_py_embed.zip")
    ok = False
    for tpl in MIRRORS:
        url = tpl.format(v=PY_VER)
        try:
            log("下载 " + url)
            download(url, zip_path)
            if os.path.getsize(zip_path) < 1024 * 1024:
                raise IOError("文件过小")
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(RUNTIME)
            ok = True
            break
        except Exception as e:  # noqa: BLE001
            log("失败：" + str(e))
    if not ok:
        return 1
    try:
        os.remove(zip_path)
    except OSError:
        pass

    py = os.path.join(RUNTIME, "python.exe")
    if not os.path.isfile(py):
        log("python.exe 缺失")
        return 1

    # 允许 import site / 使用 Lib\site-packages
    for f in os.listdir(RUNTIME):
        if f.endswith("._pth"):
            p = os.path.join(RUNTIME, f)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("\nLib\\site-packages\nimport site\n")
            log("修补 " + f)
    sp = os.path.join(RUNTIME, "Lib", "site-packages")
    os.makedirs(sp, exist_ok=True)

    if not install_pip(py, sp):
        log("pip 不可用，无法安装依赖库")
        return 1

    for group in (BUILD_PKGS, DEPS):
        log("安装依赖：" + " ".join(group))
        subprocess.run([py, "-m", "pip", "install", "-i", INDEX, "--no-warn-script-location",
                        "--disable-pip-version-check", "--no-cache-dir", "--prefer-binary"]
                       + group, cwd=RUNTIME, creationflags=NO_WINDOW)

    # 精简
    removed = 0
    for root, dirs, files in os.walk(RUNTIME):
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        for d in list(dirs):
            if d in ("__pycache__", "tests", "test", "idlelib", "turtledemo"):
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                removed += 1
                dirs.remove(d)
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass
    log("清理目录 %d 个" % removed)

    total = 0
    for root, _, files in os.walk(RUNTIME):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("运行时体积 %.1f MB" % (total / 1024 / 1024))
    r = subprocess.run([py, "-c", "import flask,requests,webview,mcauth3,PIL;print('deps ok')"],
                       capture_output=True, creationflags=NO_WINDOW)
    log("依赖自检：" + (r.stdout + r.stderr).decode("utf-8", "ignore").strip()[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
