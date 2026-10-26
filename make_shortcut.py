# -*- coding: utf-8 -*-
"""创建/刷新桌面快捷方式（中文名：脆冰启动器）。"""
import os
import sys

EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "[kc studio]脆冰启动器.exe")
ICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "assets", "cuibing.ico")
LNK_NAME = "KCL启动器.lnk"


def desktop():
    p = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(p):
        return p
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(0, 0, 0, 0, buf)
        if buf.value:
            return buf.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")


def main():
    if not os.path.isfile(EXE):
        print("找不到 EXE：", EXE)
        return 1
    dt = desktop()
    lnk = os.path.join(dt, LNK_NAME)
    ok = False
    # 方式 1：COM（comtypes / win32com）
    try:
        try:
            import comtypes.client as cc  # noqa: WPS433
            sh = cc.CreateObject("WScript.Shell", dynamic=True)
        except Exception:
            import win32com.client  # type: ignore
            sh = win32com.client.Dispatch("WScript.Shell")
        s = sh.CreateShortcut(lnk)
        s.TargetPath = EXE
        s.WorkingDirectory = os.path.dirname(EXE)
        s.IconLocation = ICO if os.path.isfile(ICO) else EXE
        s.Description = "KCL启动器 · KC studio制作"
        s.Save()
        ok = True
        print("快捷方式（COM）已更新：", lnk)
    except Exception as e:  # noqa: BLE001
        print("COM 方式失败：", e)

    if not ok:
        # 方式 2：PowerShell WScript.Shell
        import subprocess
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
            "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.IconLocation='%s';"
            "$s.Description='CL启动器';$s.Save()"
            % (lnk.replace("'", "''"), EXE.replace("'", "''"),
               os.path.dirname(EXE).replace("'", "''"),
               (ICO if os.path.isfile(ICO) else EXE).replace("'", "''"))
        )
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True)
        ok = os.path.isfile(lnk)
        print("PowerShell：", r.returncode, r.stderr.strip()[:200] if r.stderr else "")

    # 方式 3：手工写 .lnk（MS-SHLLINK 最小结构）
    if not ok:
        try:
            data = build_lnk(EXE, ICO, os.path.dirname(EXE))
            with open(lnk, "wb") as f:
                f.write(data)
            ok = os.path.isfile(lnk)
            print("快捷方式（手工生成）已写入：", lnk)
        except Exception as e:  # noqa: BLE001
            print("手工生成失败：", e)
    return 0 if ok else 2


def build_lnk(target, icon, workdir):
    """按 MS-SHLLINK 规范手写 .lnk（COM 不可用时的兜底）。"""
    import struct
    clsid = bytes([0x01, 0x14, 0x02, 0x00, 0, 0, 0, 0,
                   0xC0, 0, 0, 0, 0, 0, 0, 0x46])
    flags = 0x00000001 | 0x00000010 | 0x00000040 | 0x00000080  # IDList/工作目录/图标/Unicode
    header = struct.pack("<I", 0x0000004C) + clsid
    header += struct.pack("<I", flags)
    header += struct.pack("<I", 0x00000020)          # FILE_ATTRIBUTE_ARCHIVE
    header += struct.pack("<QQQ", 0, 0, 0)           # 创建/访问/修改时间
    header += struct.pack("<I", 0)                   # 文件大小
    header += struct.pack("<i", 0)                   # 图标索引
    header += struct.pack("<I", 1)                   # SW_SHOWNORMAL
    header += struct.pack("<HH", 0, 0)               # 快捷键 + 保留
    header += struct.pack("<II", 0, 0)               # 保留

    def ustr(s):
        return struct.pack("<H", len(s)) + s.encode("utf-16-le")

    body = b"\x00\x00"                               # 空 IDList（仅终止符）
    body += ustr(workdir)                            # WORKING_DIR
    body += ustr(icon if icon else target)           # ICON_LOCATION
    body += struct.pack("<I", 0)                     # ExtraData 终止块
    return header + body


if __name__ == "__main__":
    sys.exit(main())
