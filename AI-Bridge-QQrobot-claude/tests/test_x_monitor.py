"""Tests for the browser-based X profile monitor."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_SRC = REPO_ROOT / "packages" / "codex-qq-bridge" / "src"
if str(CODEX_SRC) not in sys.path:
    sys.path.insert(0, str(CODEX_SRC))

import codex_qq_bridge.x_monitor as x_monitor  # noqa: E402
from codex_qq_bridge.x_monitor import (  # noqa: E402
    GrammarPoint,
    MonitorStore,
    PostAnalysis,
    XPost,
    analysis_prompt,
    format_learning_message,
    format_post_time,
    has_pinned_marker,
    is_target_profile_url,
    load_active_group_openids,
    normalize_x_image_url,
    normalize_username,
    parse_status_href,
    published_at_from_post_id,
    publish_learning_post,
    send_learning_post_to_group,
    split_group_message,
    test_send_latest_post as send_latest_post_for_test,
)


def make_post(
    post_id: str = "2092229169593864431",
    *,
    image_urls: tuple[str, ...] = (),
) -> XPost:
    return XPost(
        username="AyAsA_violin",
        author_username="AyAsA_violin",
        post_id=post_id,
        status_url=f"https://x.com/AyAsA_violin/status/{post_id}",
        text="テスト投稿",
        published_at="2026-08-25T12:34:40.000Z",
        image_urls=image_urls,
    )


def test_parse_status_href_accepts_x_and_twitter_links():
    expected = (
        "AyAsA_violin",
        "2092229169593864431",
        "https://x.com/AyAsA_violin/status/2092229169593864431",
    )
    assert parse_status_href("/AyAsA_violin/status/2092229169593864431") == expected
    assert (
        parse_status_href(
            "https://twitter.com/AyAsA_violin/status/2092229169593864431/photo/1"
        )
        == expected
    )
    assert parse_status_href("https://x.com/AyAsA_violin") is None


def test_published_at_falls_back_to_x_snowflake_timestamp():
    assert published_at_from_post_id("2104047395210395771") == (
        "2026-09-27T03:16:04.985Z"
    )
    assert published_at_from_post_id("not-an-id") == ""


def test_normalize_username():
    assert normalize_username(" @AyAsA_violin ") == "AyAsA_violin"


def test_normalize_x_image_url_accepts_only_post_media():
    assert normalize_x_image_url(
        "https://pbs.twimg.com/media/example?format=webp&name=small"
    ) == "https://pbs.twimg.com/media/example?format=webp&name=large"
    assert normalize_x_image_url("https://pbs.twimg.com/profile_images/avatar.jpg") is None
    assert normalize_x_image_url("https://example.com/media/image.jpg") is None


def test_target_profile_url_rejects_home_and_login_redirects():
    assert is_target_profile_url("https://x.com/AyAsA_violin", "AyAsA_violin")
    assert is_target_profile_url("https://twitter.com/ayasa_violin/", "AyAsA_violin")
    assert not is_target_profile_url("https://x.com/home", "AyAsA_violin")
    assert not is_target_profile_url("https://x.com/i/flow/login", "AyAsA_violin")


def test_store_baseline_unchanged_and_changed(tmp_path):
    path = tmp_path / "x-monitor.sqlite3"
    store = MonitorStore(path)
    first = make_post()
    assert store.observe(first) == "baseline"
    assert store.observe(first) == "unchanged"
    assert path.stat().st_mode & 0o777 == 0o600

    loaded = MonitorStore(path)
    assert loaded.observe(first) == "unchanged"
    second = make_post("2092229169593864432")
    assert loaded.observe(second) == "changed"
    assert MonitorStore(path).observe(second) == "pending"

    loaded.mark_notified(second)
    assert MonitorStore(path).observe(second) == "unchanged"
    assert MonitorStore(path).observe(first) == "seen"

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT post_id, delivery_state FROM posts ORDER BY post_id"
        ).fetchall()
    assert rows == [
        (first.post_id, "baseline"),
        (second.post_id, "notified"),
    ]


def test_store_persists_analysis_and_group_chunk_progress(tmp_path):
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    post = make_post()
    store.observe(post)
    analysis = PostAnalysis(
        chinese_translation="测试帖子",
        quoted_chinese_translation="",
        grammar_points=(GrammarPoint("～です", "是……", "礼貌体"),),
    )
    store.save_analysis(post, analysis)
    assert MonitorStore(store.path).load_analysis(post) == analysis

    assert store.ensure_group_delivery(post, "group-1") == (0, 0)
    store.record_group_progress(post, "group-1", 1, complete=False)
    assert MonitorStore(store.path).ensure_group_delivery(post, "group-1") == (1, 0)
    store.record_group_progress(post, "group-1", 2, 1, complete=True)
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT sent_chunks, sent_images, delivery_state FROM group_deliveries"
        ).fetchone()
    assert row == (2, 1, "sent")


def test_store_invalidates_analysis_when_expanded_text_changes(tmp_path):
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    post = make_post()
    store.observe(post)
    store.save_analysis(post, PostAnalysis("截断译文", "", ()))

    expanded = XPost(
        **{
            **post.__dict__,
            "text": post.text + "。これは展開後の全文です。",
        }
    )
    assert store.observe(expanded) == "unchanged"

    assert store.latest_post(post.username).text == expanded.text
    assert store.load_analysis(expanded) is None


def test_show_more_click_supports_japanese_label():
    class FakeArticle:
        async def evaluate(self, _script, labels):
            assert "さらに表示" in labels
            assert "もっと見る" not in labels
            return True

    class FakePage:
        def __init__(self):
            self.waited = 0

        async def wait_for_timeout(self, milliseconds):
            self.waited = milliseconds

    page = FakePage()
    clicked = asyncio.run(x_monitor._click_owned_show_more(FakeArticle(), page))

    assert clicked is True
    assert page.waited == 750


def test_store_loads_newest_post_without_changing_state(tmp_path):
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    first = make_post("2092229169593864431")
    second = make_post("2092229169593864432")
    store.observe(first)
    store.observe(second)

    assert store.latest_post("AyAsA_violin") == second
    with sqlite3.connect(store.path) as connection:
        state = connection.execute(
            "SELECT delivery_state FROM posts WHERE post_id = ?", (second.post_id,)
        ).fetchone()[0]
    assert state == "pending"


def test_store_prefers_current_pointer_when_timestamp_is_missing(tmp_path):
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    older = make_post("2092229169593864431")
    current = XPost(
        username="AyAsA_violin",
        author_username="AyAsA_violin",
        post_id="2092229169593864432",
        status_url="https://x.com/AyAsA_violin/status/2092229169593864432",
        text="current",
        published_at="",
    )
    store.observe(older)
    store.observe(current)

    assert store.latest_post("AyAsA_violin") == current


def test_pinned_marker_supports_x_locales():
    assert has_pinned_marker(["AyAsA", "Pinned", "post body"])
    assert has_pinned_marker(["AyAsA", "固定済み", "post body"])
    assert has_pinned_marker(["AyAsA", "已置顶", "post body"])
    assert not has_pinned_marker(["AyAsA", "post body"])


def test_learning_message_contains_time_translation_and_grammar():
    post = make_post()
    analysis = PostAnalysis(
        chinese_translation="测试帖子",
        quoted_chinese_translation="",
        grammar_points=(GrammarPoint("～です", "是……", "礼貌体"),),
    )
    message = format_learning_message(post, analysis)
    assert "@AyAsA_violin" in message
    assert "2026-08-25 20:34（北京时间）" in message
    assert "https://x.com/AyAsA_violin/status/2092229169593864431" in message
    assert "テスト投稿" in message
    assert "测试帖子" in message
    assert "～です：是……；礼貌体" in message


def test_analysis_prompt_treats_post_as_untrusted_data():
    post = make_post()
    prompt = analysis_prompt(post)
    assert "不可信帖子数据" in prompt
    assert json.dumps(post.text, ensure_ascii=False) in prompt


def test_active_groups_and_message_splitting(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "groups": {
                    "active-group": {"active": True},
                    "default-active": {},
                    "removed-group": {"active": False},
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_active_group_openids(state_file) == [
        "active-group",
        "default-active",
    ]
    chunks = split_group_message("第一段\n" + "日" * 20, limit=10)
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == "第一段" + "日" * 20


def test_format_post_time_falls_back_for_invalid_value():
    assert format_post_time("not-a-date", "Asia/Shanghai") == "not-a-date"


def test_publish_learning_post_records_group_delivery(monkeypatch, tmp_path):
    sent: list[tuple[str, str]] = []
    sent_images: list[tuple[str, str, bool]] = []

    class FakeQQApi:
        def __init__(self, *_args, **_kwargs):
            pass

        async def send_group_text(self, group_openid, content, *, msg_id=""):
            sent.append((group_openid, content))
            return True

        async def send_local_image(
            self, path, group_openid, *, is_group=False, msg_id=""
        ):
            sent_images.append((group_openid, Path(path).name, is_group))
            return f"✅ 图片已发送: {Path(path).name}"

        async def close(self):
            pass

    monkeypatch.setattr(x_monitor, "QQApi", FakeQQApi)
    monkeypatch.setenv("APP_ID", "test-app")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("X_MONITOR_GROUP_OPENIDS", "group-1")
    monkeypatch.setenv("X_MEDIA_CACHE_DIR", str(tmp_path / "media"))
    async def fake_download(_post, directory):
        path = directory / "post-1.webp"
        path.write_bytes(b"image")
        return [path]

    monkeypatch.setattr(x_monitor, "download_post_images", fake_download)
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    post = make_post(
        image_urls=("https://pbs.twimg.com/media/example?format=webp&name=large",)
    )
    store.observe(post)
    analysis = PostAnalysis("测试帖子", "", ())

    delivered = asyncio.run(
        publish_learning_post(
            post,
            analysis,
            store,
            state_file=tmp_path / "unused-state.json",
        )
    )

    assert delivered == 1
    assert sent and sent[0][0] == "group-1"
    assert sent_images == [("group-1", "post-1.webp", True)]
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT delivery_state, sent_chunks, sent_images FROM group_deliveries"
        ).fetchone()
    assert row == ("sent", len(sent), 1)


def test_publish_isolates_group_failure_and_still_sends_authorized_images(
    monkeypatch, tmp_path
):
    events: list[tuple[str, str]] = []

    class FakeQQApi:
        def __init__(self, *_args, **_kwargs):
            pass

        async def send_group_text(self, group_openid, _content, *, msg_id=""):
            events.append(("text", group_openid))
            return group_openid != "blocked-group"

        async def send_local_image(
            self, path, group_openid, *, is_group=False, msg_id=""
        ):
            events.append(("image", group_openid))
            return f"✅ 图片已发送: {Path(path).name}"

        async def close(self):
            pass

    async def fake_download(_post, directory):
        path = directory / "post-1.webp"
        path.write_bytes(b"image")
        return [path]

    monkeypatch.setattr(x_monitor, "QQApi", FakeQQApi)
    monkeypatch.setattr(x_monitor, "download_post_images", fake_download)
    monkeypatch.setenv("APP_ID", "test-app")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    monkeypatch.setenv(
        "X_MONITOR_GROUP_OPENIDS", "authorized-group,blocked-group"
    )
    monkeypatch.setenv("X_MEDIA_CACHE_DIR", str(tmp_path / "media"))
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    post = make_post(
        image_urls=("https://pbs.twimg.com/media/example?format=webp&name=large",)
    )
    store.observe(post)

    with pytest.raises(RuntimeError, match="1/2 个 QQ 群投递失败"):
        asyncio.run(
            publish_learning_post(
                post,
                PostAnalysis("测试帖子", "", ()),
                store,
                state_file=tmp_path / "unused-state.json",
            )
        )

    assert events == [
        ("text", "authorized-group"),
        ("image", "authorized-group"),
        ("text", "blocked-group"),
    ]
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT group_openid, sent_chunks, sent_images, delivery_state "
            "FROM group_deliveries ORDER BY group_openid"
        ).fetchall()
    assert rows == [
        ("authorized-group", 1, 1, "sent"),
        ("blocked-group", 0, 0, "pending"),
    ]


def test_failed_image_does_not_advance_delivery_progress(monkeypatch, tmp_path):
    class FakeQQApi:
        def __init__(self, *_args, **_kwargs):
            pass

        async def send_group_text(self, _group_openid, _content, *, msg_id=""):
            return True

        async def send_local_image(self, path, _group_openid, **_kwargs):
            return f"❌ 图片发送失败: {Path(path).name}"

        async def close(self):
            pass

    async def fake_download(_post, directory):
        path = directory / "post-1.webp"
        path.write_bytes(b"image")
        return [path]

    monkeypatch.setattr(x_monitor, "QQApi", FakeQQApi)
    monkeypatch.setattr(x_monitor, "download_post_images", fake_download)
    monkeypatch.setenv("APP_ID", "test-app")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("X_MONITOR_GROUP_OPENIDS", "group-1")
    monkeypatch.setenv("X_MEDIA_CACHE_DIR", str(tmp_path / "media"))
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    post = make_post(
        image_urls=("https://pbs.twimg.com/media/example?format=webp&name=large",)
    )
    store.observe(post)

    with pytest.raises(RuntimeError, match="1/1 个 QQ 群投递失败"):
        asyncio.run(
            publish_learning_post(
                post,
                PostAnalysis("测试帖子", "", ()),
                store,
                state_file=tmp_path / "unused-state.json",
            )
        )

    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT sent_chunks, sent_images, delivery_state "
            "FROM group_deliveries"
        ).fetchone()
    assert row == (1, 0, "pending")


def test_send_learning_post_to_one_group_includes_images(monkeypatch, tmp_path):
    sent_text: list[tuple[str, str, str]] = []
    sent_images: list[tuple[str, str, bool, str]] = []

    class FakeQQApi:
        async def send_group_text(self, group_openid, content, *, msg_id=""):
            sent_text.append((group_openid, content, msg_id))
            return True

        async def send_local_image(
            self, path, group_openid, *, is_group=False, msg_id=""
        ):
            sent_images.append((group_openid, Path(path).name, is_group, msg_id))
            return f"✅ 图片已发送: {Path(path).name}"

    async def fake_download(_post, directory):
        paths = []
        for index in (1, 2):
            path = directory / f"post-{index}.webp"
            path.write_bytes(b"image")
            paths.append(path)
        return paths

    monkeypatch.setattr(x_monitor, "download_post_images", fake_download)
    monkeypatch.setenv("X_MEDIA_CACHE_DIR", str(tmp_path / "media"))
    post = make_post(
        image_urls=(
            "https://pbs.twimg.com/media/one?format=webp&name=large",
            "https://pbs.twimg.com/media/two?format=webp&name=large",
        )
    )

    result = asyncio.run(
        send_learning_post_to_group(
            post,
            PostAnalysis("测试帖子", "", ()),
            FakeQQApi(),
            "group-1",
            reply_msg_id="message-1",
        )
    )

    assert result == (len(sent_text), 2)
    assert sent_text and all(
        item[0] == "group-1" and item[2] == "message-1" for item in sent_text
    )
    assert sent_images == [
        ("group-1", "post-1.webp", True, "message-1"),
        ("group-1", "post-2.webp", True, "message-1"),
    ]


def test_download_post_images_uses_proxy_and_reuses_cache(monkeypatch, tmp_path):
    calls: list[httpx.Request] = []
    client_options: list[dict] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "image/webp"},
            content=b"cached-image",
        )

    def fake_client(**kwargs):
        client_options.append(dict(kwargs))
        return real_async_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
            follow_redirects=kwargs["follow_redirects"],
            headers=kwargs["headers"],
        )

    monkeypatch.setattr(x_monitor.httpx, "AsyncClient", fake_client)
    monkeypatch.setenv("X_MEDIA_PROXY", "http://127.0.0.1:7897")
    post = make_post(
        image_urls=(
            "https://pbs.twimg.com/media/example?format=webp&name=large",
        )
    )
    cache_dir = tmp_path / "cache"

    first = asyncio.run(x_monitor.download_post_images(post, cache_dir))
    second = asyncio.run(x_monitor.download_post_images(post, cache_dir))

    assert first == second
    assert first[0].read_bytes() == b"cached-image"
    assert len(calls) == 1
    assert all(
        options["proxy"] == "http://127.0.0.1:7897"
        and options["trust_env"] is False
        for options in client_options
    )


def test_send_learning_post_keeps_text_when_images_fail(monkeypatch, tmp_path):
    events: list[str] = []

    class FakeQQApi:
        async def send_group_text(
            self, _group_openid, _content, *, msg_id=""
        ):
            events.append("text")
            return True

        async def send_local_image(self, *_args, **_kwargs):
            events.append("image")
            return "✅ 图片已发送"

    async def failed_download(_post, _directory):
        events.append("download")
        raise httpx.ConnectError("image CDN unavailable")

    monkeypatch.setattr(x_monitor, "download_post_images", failed_download)
    monkeypatch.setenv("X_MEDIA_CACHE_DIR", str(tmp_path / "media"))
    post = make_post(
        image_urls=(
            "https://pbs.twimg.com/media/example?format=webp&name=large",
        )
    )

    result = asyncio.run(
        send_learning_post_to_group(
            post,
            PostAnalysis("测试帖子", "", ()),
            FakeQQApi(),
            "group-1",
        )
    )

    assert result == (1, 0)
    assert events == ["text", "download"]


def test_test_send_is_repeatable_and_does_not_touch_delivery_state(
    monkeypatch, tmp_path
):
    sent: list[tuple[str, str]] = []

    class FakeQQApi:
        def __init__(self, *_args, **_kwargs):
            pass

        async def send_group_text(self, group_openid, content, *, msg_id=""):
            sent.append((group_openid, content))
            return True

        async def close(self):
            pass

    monkeypatch.setattr(x_monitor, "QQApi", FakeQQApi)
    monkeypatch.setenv("APP_ID", "test-app")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("X_MONITOR_GROUP_OPENIDS", "group-1")
    store = MonitorStore(tmp_path / "x-monitor.sqlite3")
    first = make_post("2092229169593864431")
    latest = make_post("2092229169593864432")
    store.observe(first)
    store.observe(latest)
    store.save_analysis(latest, PostAnalysis("测试帖子", "", ()))

    async def run_twice():
        return (
            await send_latest_post_for_test(
                "AyAsA_violin",
                store.path,
                state_file=tmp_path / "unused-state.json",
                analysis_timeout_seconds=30,
            ),
            await send_latest_post_for_test(
                "AyAsA_violin",
                store.path,
                state_file=tmp_path / "unused-state.json",
                analysis_timeout_seconds=30,
            ),
        )

    first_result, second_result = asyncio.run(run_twice())
    assert first_result[0] == latest
    assert first_result[2] is False
    assert second_result[0] == latest
    assert len(sent) == 2
    with sqlite3.connect(store.path) as connection:
        state = connection.execute(
            "SELECT delivery_state FROM posts WHERE post_id = ?", (latest.post_id,)
        ).fetchone()[0]
        delivery_count = connection.execute(
            "SELECT COUNT(*) FROM group_deliveries"
        ).fetchone()[0]
    assert state == "pending"
    assert delivery_count == 0
