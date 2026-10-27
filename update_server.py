# -*- coding: utf-8 -*-
"""
脆冰启动器 - 网站服务端（更新 + 社区）

一个纯标准库（http.server + sqlite3）的网站服务端，Python 3.8+ 直接跑：
  · 版本管理：发布版本 / 更新日志 / 下发文件（启动器检查更新）
  · 管理员：首次启动随机生成口令，只在服务端窗口显示一次并写入 update_data/token.txt；
    登录后同一设备 1 天内免重输（源码里不再保留任何明文口令）
  · 社区：MC 讨论（可传 mod / 整合包）、资源（渠道「KCL玩家开发」）
  · 合作：组队做 mod，消息 / 文件仅加入者可见
  · 建议：用户上传建议，管理员能收到
  · MC 服务器宣传：类型 / 版本 / 宣传图 / 宣传语 / 名称 / 人数上限 / mod / 整合包，
    搜索（类型、KCL 已下载版本、指定版本、mod 白/黑名单过滤 + 关键词），
    结果按最后修改时间排序；加入流程（同意 / 拒绝 / 地址告知）、服务器论坛公告、
    QQ 群 / kukechat 群同意后可见、支持 markon
  · 开发空间：启动器「开发」页连接这里，可看动态、点赞、评论、发帖

启动器那边的契约（server.py fetch_update）：
    GET <manifest 地址>  ->  {"version": "1.0.3", "notes": "...", "url": "...",
                              "files": {"main.py": "http://.../d/12", ...}}
    files 里的每个地址要能直接下载到文件内容，启动器会覆盖到自己的目录。
    拿不到（服务器没开 / 地址没填）时启动器静默当作没更新，照常使用。

用法：
    python update_server.py                    # 默认 127.0.0.1:8756
    python update_server.py --host 0.0.0.0 --port 8756
    python update_server.py --open             # 启动后自动打开网站

代码原先全挤在这一个文件里，现已按职责拆开（保持原有行为不变）：
    upd_config.py     全局配置与通用小工具
    upd_db.py         数据库连接与表结构
    upd_auth.py       管理员口令、会话、用户认证
    upd_releases.py   发布版本与下发文件
    upd_posts.py      社区帖子、评论、点赞
    upd_servers.py    联机服务器、加入审核、联机大厅
    upd_templates.py  内置页面模板
    upd_handler.py    HTTP 请求处理
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer

import upd_auth
import upd_templates
from upd_config import APP_NAME, APP_VERSION, DATA_DIR, TOKEN_FILE
from upd_db import ensure_dirs, init_db
from upd_handler import Handler

__all__ = ["make_server", "parse_args", "main", "Handler"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="脆冰启动器 网站服务端（更新 + 社区）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8756)
    p.add_argument("--open", action="store_true", help="启动后自动打开后台")
    return p.parse_args(argv)


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> int:
    args = parse_args(argv)
    ensure_dirs()
    init_db()

    # 同目录下的 update_ui.html / site_ui.html 存在时覆盖内置模板
    upd_templates.reload_templates()
    upd_auth.ensure_admin_pwd()

    try:
        srv = make_server(args.host, args.port)
    except OSError as exc:
        print("启动失败：%s" % exc)
        print("换个端口试试，例如：python update_server.py --port 8757")
        return 1

    host_show = args.host if args.host not in ("0.0.0.0", "") else "127.0.0.1"
    print("=" * 46)
    print("  %s %s" % (APP_NAME, APP_VERSION))
    print("=" * 46)
    print("网站地址   : http://%s:%d" % (host_show, srv.server_address[1]))
    print("管理后台   : http://%s:%d/admin" % (host_show, srv.server_address[1]))
    print("清单地址   : http://%s:%d/api/manifest" % (host_show, srv.server_address[1]))
    if os.environ.get("CBUPDATE_TOKEN"):
        print("管理口令   : 来自环境变量 CBUPDATE_TOKEN")
    elif upd_auth.ADMIN_PWD_ONCE:
        print("管理口令   : %s（随机生成，只显示这一次，已写入 %s，可在网站里修改）"
              % (upd_auth.ADMIN_PWD_ONCE, TOKEN_FILE))
    else:
        print("管理口令   : 已保存在数据目录（哈希），忘了就删掉 update_data 里的数据库重新生成")
    print("数据目录   : %s" % DATA_DIR)
    print("-" * 46)
    print("把上面的「清单地址」填进启动器：设置 → 更新 → 更新地址")
    print("把「网站地址」填进启动器：设置 → 网站 → 网站服务端地址")
    print("服务端没开也没关系，启动器会当作没有更新。")
    print("按 Ctrl+C 停止。")
    print("=" * 46)
    sys.stdout.flush()

    url = "http://%s:%d/" % (host_show, srv.server_address[1])
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
