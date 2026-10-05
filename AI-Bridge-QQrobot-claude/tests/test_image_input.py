"""QQ image download, quoted-message parsing, and native Codex input regressions."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/codex-qq-bridge/src"))
from codex_qq_bridge import qq_api
from codex_qq_bridge.bridge import (
    CodexQQBridge, message_attachments, prepare_attachment_inputs, quoted_message_context,
)
from codex_qq_bridge.qq_api import QQApi
from test_codex_bridge import FakeQQ, FakeApp

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP1QAAAAASUVORK5CYII=")


def test_quoted_attachments_are_collected_once_and_never_accept_local_paths():
    image = {"url": "//cdn.example/photo.png", "content_type": "image/png", "_local_image_path": "/etc/passwd"}
    event = {"attachments": [image, None], "msg_elements": [
        {"attachments": [dict(image, url="https://cdn.example/photo.png")], "content": "/role_switch_yuno"},
        {"attachments": {"url": "https://cdn.example/second.jpg", "content_type": "image/jpeg"}}, None,
    ]}
    attachments = message_attachments(event)
    assert len(attachments) == 2
    assert all("_local_image_path" not in a for a in attachments)
    quote = quoted_message_context(event)
    assert quote[0]["type"] == "text" and "仅作为上下文" in quote[0]["text"]


@pytest.mark.parametrize("scope", ["group", "c2c"])
def test_qq_quoted_image_reaches_codex_as_native_local_image(scope, tmp_path, monkeypatch, caplog):
    async def run():
        downloaded = []
        def serve(request):
            downloaded.append(request)
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        real_qq = QQApi("test", "secret", temp_dir=tmp_path)
        real_qq._client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        monkeypatch.setattr(real_qq, "_validate_attachment_url", AsyncMock())
        qq = FakeQQ()
        qq.fetch_attachment_image = real_qq.fetch_attachment_image
        app = FakeApp()
        bridge = CodexQQBridge(qq=qq, app_server=app, state_file=tmp_path / "state.json", master_openid="owner")
        event = {"id": "event-image", "author": {"member_openid": "member", "user_openid": "owner"},
                 "content": "看一下引用的图片", "message_type": 103,
                 "msg_elements": [{"content": "/role_switch_yuno", "attachments": [
                     {"url": "//cdn.example/picture.png?secret=hidden", "content_type": "image/png"}]}]}
        if scope == "group":
            event["group_openid"] = "group"
        try:
            with caplog.at_level("INFO", logger="codex_qq_bridge"):
                await getattr(bridge, f"handle_{scope}_message")(event)
            requests = [params for method, params in app.requests if method == "turn/start"]
            assert len(requests) == 1
            images = [i for i in requests[0]["input"] if i["type"] == "localImage"]
            assert len(images) == 1
            path = Path(images[0]["path"])
            assert path.read_bytes() == PNG
            assert path.parent == tmp_path / "incoming-images"
            assert path.stat().st_mode & 0o777 == 0o600
            assert downloaded[0].url.scheme == "https"
            assert bridge.state.active_role == "default"
            assert "hidden" not in caplog.text and "cdn.example" not in caplog.text
            assert "nested=1, collected=1" in caplog.text
        finally:
            if bridge._typing_task:
                bridge._typing_task.cancel()
            await real_qq.close()
    asyncio.run(run())


@pytest.mark.parametrize("attachment,mime", [
    ({"url": "https://cdn.example/image", "content_type": "image"}, "application/octet-stream"),
    ({"url": "https://cdn.example/image", "filename": "photo.png"}, "image/png"),
    ({"url": "https://cdn.example/image", "width": 1, "height": 1}, "image/png"),
])
def test_generic_image_types_are_detected_and_saved_locally(attachment, mime, tmp_path, monkeypatch):
    async def run():
        qq = QQApi("test", "secret", temp_dir=tmp_path)
        qq._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=PNG, headers={"content-type": mime})))
        monkeypatch.setattr(qq, "_validate_attachment_url", AsyncMock())
        try:
            inputs = await prepare_attachment_inputs("", [attachment], qq, image_cache_dir=tmp_path / "images")
            assert [i["type"] for i in inputs] == ["localImage"]
            path = Path(inputs[0]["path"])
            assert path.read_bytes() == PNG and path.suffix == ".png"
        finally:
            await qq.close()
    asyncio.run(run())


def test_download_failure_is_reported_without_signed_url_or_fake_image(tmp_path, monkeypatch):
    async def run():
        qq = QQApi("test", "secret", temp_dir=tmp_path)
        qq._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(403)))
        monkeypatch.setattr(qq, "_validate_attachment_url", AsyncMock())
        try:
            inputs = await prepare_attachment_inputs("看图", [{"url": "https://cdn.example/image?private=secret", "content_type": "image/png"}], qq)
            assert all(i["type"] == "text" for i in inputs)
            assert "HTTP 403" in inputs[-1]["text"]
            assert "secret" not in json.dumps(inputs)
            assert not (tmp_path / "incoming-images").exists()
        finally:
            await qq.close()
    asyncio.run(run())


@pytest.mark.parametrize("mime,payload", [("text/html", b"<html>error</html>"), ("image/png", b"not an image")])
def test_non_images_are_rejected_before_local_image_input(mime, payload, tmp_path, monkeypatch):
    async def run():
        qq = QQApi("test", "secret", temp_dir=tmp_path)
        qq._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=payload, headers={"content-type": mime})))
        monkeypatch.setattr(qq, "_validate_attachment_url", AsyncMock())
        try:
            with pytest.raises(ValueError):
                await qq.fetch_attachment_image("https://cdn.example/photo.png", "image/png")
            assert not (tmp_path / "incoming-images").exists()
        finally:
            await qq.close()
    asyncio.run(run())


def test_redirect_to_private_network_is_rejected_before_request(tmp_path):
    async def run():
        requests = []
        def serve(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://127.0.0.1/photo.png"})
        qq = QQApi("test", "secret", temp_dir=tmp_path)
        qq._client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        try:
            with pytest.raises(ValueError, match="私有网络"):
                await qq.fetch_attachment_image("https://1.1.1.1/photo.png", "image/png")
            assert len(requests) == 1
        finally:
            await qq.close()
    asyncio.run(run())


def test_auth_header_only_goes_to_qq_multimedia_host(tmp_path, monkeypatch):
    async def run():
        requests = []
        def serve(request):
            requests.append(request)
            if request.url.host == "multimedia.nt.qq.com.cn":
                return httpx.Response(302, headers={"location": "https://cdn.example/photo.png"})
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        qq = QQApi("test", "secret", temp_dir=tmp_path)
        qq._client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        qq._access_token, qq._token_expires_at = "test-token", time.time() + 600
        monkeypatch.setattr(qq, "_validate_attachment_url", AsyncMock())
        try:
            await qq.fetch_attachment_image("https://multimedia.nt.qq.com.cn/download", "image/png")
            assert requests[0].headers["Authorization"] == "QQBot test-token"
            assert "Authorization" not in requests[1].headers
        finally:
            await qq.close()
    asyncio.run(run())


@pytest.mark.parametrize("content_length", [True, False])
def test_oversized_images_are_rejected_with_and_without_content_length(content_length, tmp_path, monkeypatch):
    async def run():
        def serve(_request):
            response=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
            if not content_length:
                del response.headers["content-length"]
            return response
        qq = QQApi("test", "secret", temp_dir=tmp_path)
        qq._client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        monkeypatch.setattr(qq, "_validate_attachment_url", AsyncMock())
        monkeypatch.setattr(qq_api, "_ATTACHMENT_IMAGE_MAX", 10)
        try:
            with pytest.raises(ValueError, match="大小"):
                await qq.fetch_attachment_image("https://cdn.example/photo.png", "image/png")
            assert not (tmp_path / "incoming-images").exists()
        finally:
            await qq.close()
    asyncio.run(run())
