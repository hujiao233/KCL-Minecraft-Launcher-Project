# -*- coding: utf-8 -*-
"""脆冰更新服务端 - 全局配置与通用小工具。"""

from __future__ import annotations

import time
import threading
import os

APP_NAME = "脆冰网站服务端"

APP_VERSION = "1.1"

ROOT = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.environ.get("CBUPDATE_DATA") or os.path.join(ROOT, "update_data")

DB_PATH = os.path.join(DATA_DIR, "server.db")

FILE_DIR = os.path.join(DATA_DIR, "files")

COOKIE_NAME = "cbupd_sid"

TOKEN_FILE = os.path.join(DATA_DIR, "token.txt")

SESSION_TTL = 24 * 3600               # 登录后 1 天内同设备免重输口令



_db_lock = threading.Lock()




def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")



# ==================================================================
# 数据读写
# ==================================================================


def strval(v, limit: int = 20000) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    return s.strip()[:limit]



# ==================================================================
# 社区网站：帖子（社区 / 资源 / 建议 / 开发空间）
# ==================================================================


POST_KINDS = ("dev", "community", "resource", "suggestion")
