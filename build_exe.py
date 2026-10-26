# -*- coding: utf-8 -*-
"""
构建 EXE 启动器：
  1. PyInstaller 打包 bootstrap_exe.py（无控制台、带脆冰图标）
  2. 把启动器本体文件 + 便携运行环境（runtime）以 base64 zip 追加到 exe 末尾

两种形态：
  python build_exe.py                 # 完整版（带运行环境，exe 较大，双击即用）
  python build_exe.py --slim          # 精简版「压缩启动器.exe」：只带本体，
                                      #   首次运行联网补齐 Python + 依赖，
                                      #   补齐后自动回填进 exe 本体（exe 变大），
                                      #   之后再运行/换电脑都不用重新下载

用法：python build_exe.py [--no-runtime] [--slim] [--out 名字.exe]
"""
import base64
import io
import json
import os
import subprocess
import sys
import time
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
SKIP_DIRS = {"__pycache__", "bin", "include", "libs", "Tools", "tests", "test"}
SKIP_EXT = {".pyc", ".pyo", ".pdb", ".lib", ".a", ".chm"}
EXE_NAME = "[kc studio]脆冰启动器.exe"
SLIM_EXE_NAME = "压缩启动器.exe"


def runtime_files():
    out = []
    if not os.path.isdir(RUNTIME_DIR):
        return out
    for root, dirs, files in os.walk(RUNTIME_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if os.path.splitext(f)[1].lower() in SKIP_EXT:
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, BASE_DIR).replace("\\", "/")
            try:
                if os.path.getsize(p) == 0:
                    continue
            except OSError:
                continue
            out.append(rel)
    return out


DEFAULT_FILES = [
    "main.py", "core.py", "version.py", "game.py", "server.py", "mods.py",
    "world.py", "auth.py",
    "ui/index.html", "ui/assets/cuibing.png", "ui/assets/cuibing.ico",
]


def launcher_files():
    try:
        import server
        return list(server.LAUNCHER_FILES)
    except Exception:  # noqa: BLE001
        return list(DEFAULT_FILES)


def build(with_runtime=True, out_name: str = "") -> str:
    files = launcher_files()
    rt = runtime_files() if with_runtime else []
    print("[build] 便携运行环境 %d 个文件" % len(rt))
    files += rt
    # 精简版：告诉引导器"本体里没有运行环境，第一次运行时联网补齐并回填"
    if not rt:
        meta = {"slim": True, "built": time.strftime("%Y-%m-%d %H:%M:%S")}
        p = os.path.join(BASE_DIR, "_payload_meta.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        files.append("_payload_meta.json")

    # 1) 本体 payload
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            p = os.path.join(BASE_DIR, rel)
            if not os.path.isfile(p):
                print("[build] 缺少文件，跳过：", rel)
                continue
            z.write(p, rel)
            n += 1
    raw = buf.getvalue()
    try:      # 打进包里就够了，源码目录里不用留
        os.remove(os.path.join(BASE_DIR, "_payload_meta.json"))
    except OSError:
        pass
    b64 = base64.b64encode(raw).decode("ascii")
    payload = "\n" + "\n".join(b64[i:i + 100] for i in range(0, len(b64), 100)) + "\n"
    print(f"[build] 打包 {n} 个文件，压缩包 {len(raw) / 1024 / 1024:.1f} MB")

    # 2) PyInstaller 打包引导器
    icon = os.path.join(BASE_DIR, "ui", "assets", "cuibing.ico")
    dist = os.path.join(BASE_DIR, "_build_exe")
    work = os.path.join(BASE_DIR, "_build_tmp")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconsole", "--clean",
        "--name", "CuibingLauncher",
        "--distpath", dist, "--workpath", work, "--specpath", work,
    ]
    if os.path.isfile(icon):
        cmd += ["--icon", icon]
    cmd.append(os.path.join(BASE_DIR, "bootstrap_exe.py"))
    print("[build] PyInstaller 打包中…")
    r = subprocess.run(cmd, cwd=BASE_DIR)
    if r.returncode != 0:
        raise RuntimeError("PyInstaller 失败")

    exe = os.path.join(dist, "CuibingLauncher.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("没有生成 exe")

    # 3) 追加 payload（::CBPB__ ... ::CBPE__）
    with open(exe, "rb") as f:
        data = f.read()
    data += b"\n::CBPB__" + payload.encode("ascii") + b"::CBPE__\n"
    name = out_name or (SLIM_EXE_NAME if not with_runtime else EXE_NAME)
    out = os.path.join(os.path.dirname(BASE_DIR), name)
    with open(out, "wb") as f:
        f.write(data)
    print(f"[build] 完成：{out}（{os.path.getsize(out) / 1024 / 1024:.1f} MB）")
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    slim = "--slim" in args
    name = ""
    for i, a in enumerate(args):
        if a == "--out" and i + 1 < len(args):
            name = args[i + 1]
    build(with_runtime=not slim, out_name=name)
