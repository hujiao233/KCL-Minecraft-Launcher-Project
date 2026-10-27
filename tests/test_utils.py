# -*- coding: utf-8 -*-
"""通用小工具：字段清洗、路径规范化。"""
from __future__ import annotations

import re

import pytest

from upd_config import now_str, strval
from upd_releases import clean_rel


def test_strval_turns_none_and_blank_into_empty():
    assert strval(None) == ""
    assert strval("   ") == ""


def test_strval_strips_and_casts():
    assert strval("  abc  ") == "abc"
    assert strval(123) == "123"


def test_strval_truncates_to_limit():
    assert len(strval("x" * 500, 10)) == 10
    assert strval("abcdef", 3) == "abc"


def test_now_str_format():
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", now_str())


@pytest.mark.parametrize("raw, want", [
    ("main.py", "main.py"),
    ("/main.py", "main.py"),
    ("ui/index.html", "ui/index.html"),
    ("ui\\index.html", "ui/index.html"),
    ("///a//b/", "a/b"),
    ("a/./b", "a/b"),
    # 安全相关：向上穿越必须被剔除，否则服务端会把文件写到数据目录之外。
    # 注意实现是「丢弃 .. 片段」而不是「回退一层」，所以 a/../../b 得到 a/b 而非 b ——
    # 两种做法都越不出去，这里按实际行为锁定，防止有人改成真正解析后放行了 ..
    ("../../etc/passwd", "etc/passwd"),
    ("a/../../b", "a/b"),
    ("....//x", "..../x"),
    (".", ""),
    ("", ""),
    (None, ""),
])
def test_clean_rel(raw, want):
    assert clean_rel(raw) == want


def test_clean_rel_never_escapes_root():
    for raw in ["..", "../..", "a/b/../../../c", "....//x", "/../x", "x/../y/../../z"]:
        result = clean_rel(raw)
        parts = [p for p in result.split("/") if p]
        assert ".." not in parts          # 没有留下任何向上跳转
        assert not result.startswith("/")  # 也不会变成绝对路径
