# -*- coding: utf-8 -*-
"""社区帖子：发帖、标题兜底、评论、点赞。"""
from __future__ import annotations

import upd_db
from upd_posts import add_comment, create_post, list_comments, list_posts, toggle_like

AUTHOR = {"uid": "u1", "name": "服主"}


def test_resource_falls_back_to_fixed_title(env):
    pid = create_post("resource", {"content": "整合包说明"}, AUTHOR)["id"]
    assert list_posts("resource")[0]["title"] == "未命名资源"
    assert pid > 0


def test_community_title_taken_from_content(env):
    create_post("community", {"content": "今天更新了启动器"}, AUTHOR)
    assert list_posts("community")[0]["title"] == "今天更新了启动器"


def test_dev_post_keeps_empty_title(env):
    create_post("dev", {"content": "开发日志"}, AUTHOR)
    assert list_posts("dev")[0]["title"] == ""


def test_comment_needs_content(env):
    assert add_comment(1, "   ", AUTHOR)["ok"] is False


def test_comment_needs_existing_post(env):
    assert add_comment(99999, "内容", AUTHOR)["ok"] is False


def test_comment_round_trip(env):
    pid = create_post("community", {"title": "话题", "content": "正文"}, AUTHOR)["id"]
    assert add_comment(pid, "我来评论", AUTHOR)["ok"] is True

    comments = list_comments(pid)
    assert [c["text"] for c in comments] == ["我来评论"]
    assert comments[0]["author"] == "服主"


def test_like_toggles_back_and_forth(env):
    pid = create_post("community", {"title": "话题", "content": "正文"}, AUTHOR)["id"]

    first = toggle_like(pid, "u1")
    assert first["liked"] is True and first["likes"] == 1

    second = toggle_like(pid, "u1")
    assert second["liked"] is False and second["likes"] == 0

    assert toggle_like(pid, "u1")["likes"] == 1


def test_like_count_never_goes_negative(env):
    pid = create_post("community", {"title": "话题", "content": "正文"}, AUTHOR)["id"]
    toggle_like(pid, "u1")

    conn = upd_db.connect()
    conn.execute("UPDATE posts SET likes=0")
    conn.commit()
    conn.close()

    assert toggle_like(pid, "u1")["likes"] == 0


def test_like_rejects_missing_post(env):
    assert toggle_like(99999, "u1")["ok"] is False


def test_suggestion_is_private_to_its_author(env):
    pid = create_post("suggestion", {"content": "希望能加个暗色主题"}, AUTHOR)["id"]

    assert [p["id"] for p in list_posts("suggestion", author_uid="u1")] == [pid]
    # 别人看不到别人的建议
    assert list_posts("suggestion", author_uid="u2") == []
    # 管理员能看到全部
    assert [p["id"] for p in list_posts("suggestion", for_admin=True)] == [pid]
