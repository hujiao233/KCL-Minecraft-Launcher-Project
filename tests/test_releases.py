# -*- coding: utf-8 -*-
"""发布版本管理：新建、去重、发布、清单生成、文件增删。"""
from __future__ import annotations

import os

from upd_releases import (
    create_release,
    current_release,
    delete_release,
    file_by_id,
    list_releases,
    manifest_of,
    publish_release,
    remove_file,
    save_file,
    update_release,
)


def body(version, **kw):
    return {
        "version": version,
        "title": kw.get("title", "标题"),
        "notes": kw.get("notes", ""),
        "url": kw.get("url", ""),
        "items": kw.get("items", []),
    }


def first(rid):
    for row in list_releases():
        if row["id"] == rid:
            return row
    raise AssertionError("release %s not listed" % rid)


def test_create_release_rejects_empty_version(env):
    result = create_release(body("   "))
    assert result["ok"] is False
    assert "版本号" in result["msg"]


def test_create_release_rejects_duplicate(env):
    assert create_release(body("1.0.0"))["ok"] is True
    result = create_release(body("1.0.0"))
    assert result["ok"] is False
    assert "已存在" in result["msg"]


def test_blank_items_are_dropped(env):
    rid = create_release(body("1.0.1", items=["", "   ", "修复崩溃", "  新增暗色主题  "]))["id"]
    assert first(rid)["items"] == ["修复崩溃", "新增暗色主题"]


def test_update_release_reports_missing_and_taken_version(env):
    a = create_release(body("1.0.0"))["id"]
    b = create_release(body("2.0.0"))["id"]

    assert update_release(99999, body("3.0.0"))["ok"] is False

    clash = update_release(b, body("1.0.0"))
    assert clash["ok"] is False
    assert "占用" in clash["msg"]

    ok = update_release(a, body("1.0.0", title="改过的标题", items=["新要点"]))
    assert ok["ok"] is True
    assert first(a)["title"] == "改过的标题"


def test_publish_keeps_exactly_one_current(env):
    a = create_release(body("1.0.0"))["id"]
    b = create_release(body("1.1.0"))["id"]

    assert current_release() is None

    assert publish_release(a, True)["ok"] is True
    assert current_release()["id"] == a

    publish_release(b, True)
    assert current_release()["id"] == b
    assert not first(a)["isCurrent"]
    assert first(b)["isCurrent"]


def test_unpublish_clears_current(env):
    a = create_release(body("1.0.0"))["id"]
    publish_release(a, True)
    assert publish_release(a, False)["ok"] is True
    assert current_release() is None


def test_manifest_merges_notes_and_files(env, tmp_path):
    rid = create_release(
        body("1.2.0", notes="这次改了不少东西", items=["新增：暗色主题", "修复：下载中断"]))["id"]

    src = tmp_path / "main.py"
    src.write_bytes(b"print('hi')")
    saved = save_file(rid, "main.py", str(src), 11)
    assert saved["ok"] is True
    assert saved["rel"] == "main.py"

    publish_release(rid, True)
    fid = first(rid)["files"][0]["id"]
    mf = manifest_of(current_release(), "http://example.com:8756/")

    assert mf["version"] == "1.2.0"
    assert mf["notes"].startswith("这次改了不少东西")
    assert "- 新增：暗色主题" in mf["notes"]
    assert mf["files"] == {"main.py": "http://example.com:8756/d/%d" % fid}


def test_save_file_rejects_blank_rel_and_bad_release(env, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    rid = create_release(body("1.3.0"))["id"]

    blank = save_file(rid, "   ", str(src), 2)
    assert blank["ok"] is False

    assert save_file(99999, "a.txt", str(src), 2)["ok"] is False


def test_remove_file_deletes_from_disk(env, tmp_path):
    rid = create_release(body("1.4.0"))["id"]
    src = tmp_path / "a.txt"
    src.write_text("hello")
    save_file(rid, "a.txt", str(src), 5)

    fid = first(rid)["files"][0]["id"]
    assert file_by_id(fid) is not None
    assert os.listdir(env.files)

    assert remove_file(fid)["ok"] is True
    assert file_by_id(fid) is None
    assert os.listdir(env.files) == []


def test_delete_release_removes_files_and_is_idempotent(env, tmp_path):
    rid = create_release(body("1.5.0"))["id"]
    src = tmp_path / "b.bin"
    src.write_bytes(b"payload")
    save_file(rid, "b.bin", str(src), 7)
    assert os.listdir(env.files)

    assert delete_release(rid)["ok"] is True
    assert list_releases() == []
    assert os.listdir(env.files) == []

    # 删不存在的版本不应报错
    assert delete_release(99999)["ok"] is True
