"""Regression tests for the Codex App Server based QQ bridge."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_SRC = REPO_ROOT / "packages" / "codex-qq-bridge" / "src"
if str(CODEX_SRC) not in sys.path:
    sys.path.insert(0, str(CODEX_SRC))

from codex_qq_bridge.app_server import AppServerClient  # noqa: E402
from codex_qq_bridge.bridge import (  # noqa: E402
    CodexQQBridge,
    PendingApproval,
    ReplyTarget,
    attachment_inputs,
    format_token_usage,
    interaction_operator_and_button,
    prepare_attachment_inputs,
)
from codex_qq_bridge.qq_api import QQApi, build_approval_keyboard  # noqa: E402


class FakeQQ:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict[str, Any] | None]] = []
        self.media: list[dict[str, str]] = []
        self.group_messages: list[tuple[str, str, str]] = []
        self.acked: list[str] = []

    async def send_reply(self, openid: str, content: str, *, keyboard=None) -> bool:
        self.messages.append((openid, content, keyboard))
        return True

    async def send_typing(self, openid: str, msg_id: str) -> bool:
        return True

    async def send_group_text(
        self, group_openid: str, content: str, *, msg_id: str = "", keyboard=None
    ) -> bool:
        self.group_messages.append((group_openid, content, msg_id))
        return True

    async def fetch_attachment_data_url(self, url: str, declared_type: str) -> str:
        if "fail" in url:
            raise ValueError("download rejected")
        return f"data:{declared_type};base64,aW1hZ2U="

    async def acknowledge_interaction(self, interaction_id: str) -> None:
        self.acked.append(interaction_id)

    async def send_marked_media(self, media, openid: str, *, is_group: bool = False) -> None:
        self.media.extend(media)

    async def send_local_image(self, path: str, openid: str) -> str:
        return f"✅ 图片已发送: {Path(path).name}"

    async def send_local_file(self, path: str, openid: str) -> str:
        return f"✅ 文件已发送: {Path(path).name}"

    async def close(self) -> None:
        return None


class FakeApp:
    def __init__(self) -> None:
        self.is_running = True
        self.notification_handler = None
        self.server_request_handler = None
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.thread_list_data: list[dict[str, Any]] = []

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False

    async def request(self, method: str, params: dict[str, Any], **kwargs) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-new", "cwd": params.get("cwd")}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "cwd": params.get("cwd")}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "thread/list":
            return {"data": self.thread_list_data}
        return {}

    @staticmethod
    def _safe_default_server_response(method: str) -> dict[str, Any]:
        return AppServerClient._safe_default_server_response(method)


class CodexBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-bridge-tests-")
        self.root = Path(self.temp.name)
        self.env = self.root / ".env"
        self.env.write_text("APP_ID=test\nCLIENT_SECRET=test\nMASTER_OPENID=\n", encoding="utf-8")
        self.qq = FakeQQ()
        self.app = FakeApp()
        self.bridge = CodexQQBridge(
            qq=self.qq,
            app_server=self.app,
            state_file=self.root / "state.json",
            env_path=self.env,
            master_openid="",
        )

    async def asyncTearDown(self) -> None:
        if self.bridge._typing_task:
            self.bridge._typing_task.cancel()
        for task in tuple(self.bridge._stream_tasks):
            task.cancel()
        self.temp.cleanup()

    async def test_master_binds_once_and_cannot_be_taken_over(self) -> None:
        self.assertTrue(await self.bridge.bind_or_authorize("owner"))
        self.assertFalse(await self.bridge.bind_or_authorize("attacker"))
        self.assertEqual(self.bridge.master_openid, "owner")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("MASTER_OPENID=owner", text)
        self.assertNotIn("attacker", text)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    async def test_master_is_not_bound_when_persistence_fails(self) -> None:
        self.bridge.env_path = self.root
        self.assertFalse(await self.bridge.bind_or_authorize("owner"))
        self.assertEqual(self.bridge.master_openid, "")

    async def test_master_rejects_env_injection_characters(self) -> None:
        self.assertFalse(await self.bridge.bind_or_authorize("owner\nAPP_ID=stolen"))
        self.assertEqual(self.bridge.master_openid, "")
        self.assertNotIn("stolen", self.env.read_text(encoding="utf-8"))

    async def test_unauthorized_message_never_reaches_codex(self) -> None:
        self.bridge.master_openid = "owner"
        await self.bridge.handle_c2c_message(
            {"id": "m1", "content": "do something", "author": {"user_openid": "attacker"}}
        )
        self.assertEqual(self.app.requests, [])

    async def test_non_ascii_sender_is_safely_rejected(self) -> None:
        self.bridge.master_openid = "owner"
        self.assertFalse(await self.bridge.bind_or_authorize("攻击者"))

    async def test_start_turn_uses_explicit_thread(self) -> None:
        self.bridge.master_openid = "owner"
        await self.bridge._new_thread(str(self.root))
        turn_id = await self.bridge.start_turn([{"type": "text", "text": "hello"}], "m2")
        self.assertEqual(turn_id, "turn-1")
        method, params = self.app.requests[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["threadId"], "thread-new")
        self.assertEqual(params["approvalPolicy"], "on-request")

    async def test_runtime_recovers_stored_thread(self) -> None:
        self.bridge.state.thread_id = "stored-thread"
        self.app.is_running = False
        await self.bridge.ensure_runtime()
        self.assertTrue(self.app.is_running)
        self.assertEqual(self.bridge.state.thread_id, "stored-thread")
        self.assertEqual(self.app.requests[-1][0], "thread/resume")

    async def test_stop_interrupts_active_turn(self) -> None:
        self.bridge.state.thread_id = "thread-1"
        self.bridge.active_turn_id = "turn-1"
        self.assertTrue(await self.bridge._handle_command("/stop", "owner"))
        self.assertEqual(
            self.app.requests[-1],
            ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}),
        )

    async def test_compact_targets_current_thread(self) -> None:
        self.bridge.state.thread_id = "thread-1"
        self.assertTrue(await self.bridge._handle_command("/compact", "owner"))
        self.assertEqual(
            self.app.requests[-1],
            ("thread/compact/start", {"threadId": "thread-1"}),
        )

    async def test_cd_starts_thread_in_resolved_directory(self) -> None:
        child = self.root / "child"
        child.mkdir()
        self.assertTrue(await self.bridge._handle_command(f"/cd {child}", "owner"))
        method, params = self.app.requests[-1]
        self.assertEqual(method, "thread/start")
        self.assertEqual(params["cwd"], str(child.resolve()))

    async def test_resume_lists_and_selects_explicit_thread(self) -> None:
        self.bridge.state.cwd = str(self.root)
        self.app.thread_list_data = [
            {"id": "history-1", "cwd": str(self.root), "preview": "previous task"}
        ]
        self.assertTrue(await self.bridge._handle_command("/resume", "owner"))
        self.assertEqual(self.bridge.resume_mapping[1]["id"], "history-1")
        self.assertTrue(await self.bridge._handle_command("/resume 1", "owner"))
        self.assertEqual(self.bridge.state.thread_id, "history-1")

    async def test_full_mode_requires_confirmation(self) -> None:
        await self.bridge._handle_command("/mode full", "owner")
        self.assertNotEqual(self.bridge.state.sandbox, "danger-full-access")
        await self.bridge._handle_command("/mode full confirm", "owner")
        self.assertEqual(self.bridge.state.sandbox, "danger-full-access")

    async def test_fast_completed_turn_is_not_marked_active_again(self) -> None:
        self.bridge.master_openid = "owner"
        await self.bridge._new_thread(str(self.root))
        self.bridge.completed_turn_ids.add("turn-1")
        turn_id = await self.bridge.start_turn([{"type": "text", "text": "fast"}], "m-fast")
        self.assertEqual(turn_id, "turn-1")
        self.assertIsNone(self.bridge.active_turn_id)

    async def test_approval_round_trip(self) -> None:
        self.bridge.master_openid = "owner"
        self.bridge.state.thread_id = "thread-1"
        task = asyncio.create_task(
            self.bridge.handle_codex_server_request(
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "command": "git status",
                },
                9,
            )
        )
        for _ in range(20):
            if self.bridge.pending_approvals:
                break
            await asyncio.sleep(0)
        token = next(iter(self.bridge.pending_approvals))
        self.assertTrue(await self.bridge.resolve_approval(token, "acceptForSession"))
        self.assertEqual(await task, {"decision": "acceptForSession"})
        self.assertIsNotNone(self.qq.messages[-1][2])

    async def test_current_qq_button_event_resolves_approval(self) -> None:
        self.bridge.master_openid = "owner"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.bridge.pending_approvals["opaque"] = PendingApproval(
            "item/commandExecution/requestApproval", {}, future
        )
        await self.bridge.handle_interaction(
            {
                "id": "interaction-1",
                "user_openid": "owner",
                "data": {
                    "resolved": {
                        "button_data": "codex-approve:opaque:accept",
                        "button_id": "allow_opaque",
                    }
                },
            }
        )
        self.assertEqual(await future, "accept")
        self.assertEqual(self.qq.acked, ["interaction-1"])

    async def test_agent_message_is_sent_once(self) -> None:
        self.bridge.master_openid = "owner"
        self.bridge.state.thread_id = "thread-1"
        params = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {"id": "item-1", "type": "agentMessage", "text": "done"},
        }
        await self.bridge.handle_codex_notification("item/completed", params)
        await self.bridge.handle_codex_notification("item/completed", params)
        self.assertEqual([message[1] for message in self.qq.messages], ["done"])

    async def test_agent_deltas_stream_without_repeating_final_text(self) -> None:
        self.bridge.master_openid = "owner"
        self.bridge.state.thread_id = "thread-1"
        for delta in ("第一段已经生成。\n\n", "第二段也已经生成。"):
            await self.bridge.handle_codex_notification(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "stream-item",
                    "delta": delta,
                },
            )
        await self.bridge._flush_stream_item("stream-item", force=True)
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "stream-item",
                    "type": "agentMessage",
                    "text": "第一段已经生成。\n\n第二段也已经生成。",
                },
            },
        )
        combined = "".join(message[1] for message in self.qq.messages)
        self.assertEqual(combined, "第一段已经生成。\n\n第二段也已经生成。")
        self.assertNotIn("stream-item", self.bridge.stream_replies)

    async def test_media_marker_is_held_until_item_completion(self) -> None:
        self.bridge.master_openid = "owner"
        self.bridge.state.thread_id = "thread-1"
        self.bridge.state.cwd = str(self.root)
        media_path = self.root / "chart.png"
        media_path.write_bytes(b"not-a-real-png")
        delta = f"已生成图片。\n[[SEND_IMAGE:{media_path}]]"
        await self.bridge.handle_codex_notification(
            "item/agentMessage/delta",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "media-item",
                "delta": delta,
            },
        )
        await self.bridge._flush_stream_item("media-item", force=True)
        self.assertEqual([message[1] for message in self.qq.messages], ["已生成图片。"])
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "media-item", "type": "agentMessage", "text": delta},
            },
        )
        self.assertNotIn("[[SEND_IMAGE", str(self.qq.messages))
        self.assertEqual(
            self.qq.media, [{"type": "image", "path": str(media_path.resolve())}]
        )

    async def test_model_media_marker_cannot_exfiltrate_outside_workspace(self) -> None:
        self.bridge.master_openid = "owner"
        self.bridge.state.thread_id = "thread-1"
        self.bridge.state.cwd = str(self.root)
        text = "[[SEND_FILE:/etc/passwd]]"
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "unsafe-media", "type": "agentMessage", "text": text},
            },
        )
        self.assertEqual(self.qq.media, [])
        self.assertIn("工作目录之外", self.qq.messages[-1][1])

    async def test_side_thread_events_are_buffered_by_thread_id(self) -> None:
        future = asyncio.get_running_loop().create_future()
        self.bridge.side_threads["side-thread"] = future
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": "side-thread",
                "turnId": "side-turn",
                "item": {"id": "side-item", "type": "agentMessage", "text": "side answer"},
            },
        )
        await self.bridge.handle_codex_notification(
            "turn/completed",
            {"threadId": "side-thread", "turn": {"id": "side-turn", "status": "completed"}},
        )
        self.assertEqual(await future, "side answer")
        self.assertEqual(self.qq.messages, [])

    async def test_remote_image_is_inlined_before_turn_start(self) -> None:
        inputs = await prepare_attachment_inputs(
            "描述图片",
            [{"url": "https://cdn.example/image.png", "content_type": "image/png"}],
            self.qq,
        )
        self.assertEqual([item["type"] for item in inputs], ["text", "image"])
        self.assertTrue(inputs[1]["url"].startswith("data:image/png;base64,"))

    async def test_group_member_triggers_turn_and_reply_mentions_sender(self) -> None:
        self.bridge.master_openid = "owner"
        self.bridge.state.thread_id = "thread-1"
        await self.bridge.handle_group_message(
            {
                "id": "group-message-1",
                "group_openid": "group-1",
                "content": "请回答这个问题",
                "author": {"member_openid": "member-1", "nickname": "小明"},
            }
        )
        method, params = self.app.requests[-1]
        self.assertEqual(method, "turn/start")
        self.assertIn("群聊用户 小明", params["input"][0]["text"])
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "group-item", "type": "agentMessage", "text": "答案"},
            },
        )
        self.assertEqual(
            self.qq.group_messages,
            [("group-1", "@小明 答案", "group-message-1")],
        )
        self.assertEqual(self.bridge.master_openid, "owner")

    async def test_group_cannot_enable_full_mode_or_send_arbitrary_file(self) -> None:
        target = ReplyTarget(
            "group", "group-1", msg_id="m1", member_openid="member-1", member_name="成员"
        )
        await self.bridge._handle_command("/mode full confirm", target)
        self.assertNotEqual(self.bridge.state.sandbox, "danger-full-access")
        await self.bridge._handle_command("/sendfile /etc/passwd", target)
        self.assertIn("群聊仅开放普通问答", self.qq.group_messages[-1][1])

    async def test_rejected_attachment_becomes_explanatory_text(self) -> None:
        inputs = await prepare_attachment_inputs(
            "描述图片",
            [
                {
                    "url": "https://fail.example/image.png",
                    "content_type": "image/png",
                    "filename": "bad.png",
                }
            ],
            self.qq,
        )
        self.assertEqual([item["type"] for item in inputs], ["text", "text"])
        self.assertIn("bad.png", inputs[-1]["text"])

    async def test_protocol_relative_voice_wav_is_inlined_as_audio(self) -> None:
        inputs = await prepare_attachment_inputs(
            "听一下",
            [
                {
                    "url": "//cdn.example/raw.silk",
                    "voice_wav_url": "//cdn.example/voice.wav",
                    "content_type": "voice",
                }
            ],
            self.qq,
        )
        self.assertEqual([item["type"] for item in inputs], ["text", "audio"])
        self.assertTrue(inputs[1]["url"].startswith("data:audio/wav;base64,"))


class PureFunctionTests(unittest.TestCase):
    def test_attachment_inputs_use_native_image_and_audio_items(self) -> None:
        result = attachment_inputs(
            "inspect",
            [
                {"url": "https://example/image.png", "content_type": "image/png"},
                {"url": "https://example/audio.amr", "content_type": "audio/amr"},
                {"url": "https://example/file.zip", "content_type": "application/zip", "filename": "x.zip"},
            ],
        )
        self.assertEqual([item["type"] for item in result], ["text", "image", "audio", "text"])

    def test_approval_keyboard_does_not_expose_request_details(self) -> None:
        keyboard = build_approval_keyboard("opaque")
        serialized = str(keyboard)
        self.assertIn("codex-approve:opaque:accept", serialized)
        self.assertNotIn("git status", serialized)

    def test_context_usage_format(self) -> None:
        output = format_token_usage(
            {
                "last": {"totalTokens": 500},
                "total": {"inputTokens": 1000, "outputTokens": 200, "cachedInputTokens": 400},
                "modelContextWindow": 10000,
            }
        )
        self.assertIn("5.0%", output)
        self.assertIn("1,000", output)

    def test_current_qq_interaction_payload_shape(self) -> None:
        openid, button = interaction_operator_and_button(
            {
                "user_openid": "owner",
                "data": {
                    "resolved": {
                        "button_data": "codex-approve:opaque:accept",
                        "button_id": "allow_opaque",
                    }
                },
            }
        )
        self.assertEqual(openid, "owner")
        self.assertEqual(button, "codex-approve:opaque:accept")


class FakeResponse:
    def __init__(self, data: dict[str, Any] | None = None, status_code: int = 200) -> None:
        self._data = data or {}
        self.status_code = status_code
        self.text = str(self._data)

    def json(self) -> dict[str, Any]:
        return self._data


class FakeRestClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.urls.append(url)
        if url.endswith("/upload_prepare"):
            return FakeResponse(
                {
                    "upload_id": "upload-1",
                    "block_size": 1024,
                    "parts": [{"index": 0, "presigned_url": "https://upload.test/part"}],
                }
            )
        if url.endswith("/files"):
            return FakeResponse({"file_info": "file-info"})
        return FakeResponse()

    async def aclose(self) -> None:
        return None


class FakeUploadClient:
    posted_urls: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.uploaded = b""

    async def __aenter__(self) -> "FakeUploadClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def put(self, url: str, *, content: bytes, headers: dict[str, str]) -> FakeResponse:
        self.uploaded += content
        return FakeResponse()

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.posted_urls.append(url)
        return FakeResponse()


class FakeDownloadResponse:
    def __init__(self, payload: bytes, content_type: str = "image/png") -> None:
        self.payload = payload
        self.status_code = 200
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(payload)),
        }

    async def __aenter__(self) -> "FakeDownloadResponse":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def aiter_bytes(self):
        yield self.payload


class FakeDownloadClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def stream(self, method: str, url: str, **kwargs) -> FakeDownloadResponse:
        return FakeDownloadResponse(self.payload)


class QQApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_url_rejects_private_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "私有网络"):
            await QQApi._validate_attachment_url("https://127.0.0.1/image.png")

    async def test_qq_cdn_allows_only_proxy_fake_ip_range(self) -> None:
        fake_record = [
            (2, 1, 6, "", ("198.18.1.83", 443)),
            (30, 1, 6, "", ("fdfe:dcba:9876::216", 443, 0, 0)),
        ]
        with patch("codex_qq_bridge.qq_api.socket.getaddrinfo", return_value=fake_record):
            await QQApi._validate_attachment_url(
                "https://multimedia.nt.qq.com.cn/download?id=test"
            )
            with self.assertRaisesRegex(ValueError, "私有网络"):
                await QQApi._validate_attachment_url("https://attacker.example/image.png")

    async def test_qq_cdn_still_rejects_real_private_ip(self) -> None:
        private_record = [(2, 1, 6, "", ("192.168.1.2", 443))]
        with patch("codex_qq_bridge.qq_api.socket.getaddrinfo", return_value=private_record):
            with self.assertRaisesRegex(ValueError, "私有网络"):
                await QQApi._validate_attachment_url(
                    "https://multimedia.nt.qq.com.cn/download?id=test"
                )

    async def test_attachment_download_becomes_inline_data_url(self) -> None:
        qq = QQApi("app", "secret")
        qq._client = FakeDownloadClient(b"png-bytes")  # type: ignore[assignment]

        async def allow_url(*args) -> None:
            return None

        with patch.object(QQApi, "_validate_attachment_url", allow_url):
            result = await qq.fetch_attachment_data_url(
                "https://cdn.example/image.png", "image/png"
            )
        self.assertEqual(result, "data:image/png;base64,cG5nLWJ5dGVz")

    async def test_file_upload_protocol_and_media_send(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-qq-upload-") as directory:
            path = Path(directory) / "result.txt"
            path.write_text("codex result", encoding="utf-8")
            qq = QQApi("app", "secret")
            rest = FakeRestClient()
            qq._client = rest  # type: ignore[assignment]
            qq._access_token = "token"
            qq._token_expires_at = time.time() + 3600
            FakeUploadClient.posted_urls.clear()
            with patch("codex_qq_bridge.qq_api.httpx.AsyncClient", FakeUploadClient):
                result = await qq.send_local_file(str(path), "owner")
            self.assertEqual(result, "✅ 文件已发送: result.txt")
            self.assertTrue(any(url.endswith("/upload_prepare") for url in rest.urls))
            self.assertTrue(
                any(url.endswith("/upload_part_finish") for url in FakeUploadClient.posted_urls)
            )
            self.assertTrue(any(url.endswith("/files") for url in rest.urls))
            self.assertTrue(any(url.endswith("/messages") for url in rest.urls))

    async def test_group_text_uses_group_endpoint_and_reply_reference(self) -> None:
        qq = QQApi("app", "secret")
        rest = FakeRestClient()
        qq._client = rest  # type: ignore[assignment]
        qq._access_token = "token"
        qq._token_expires_at = time.time() + 3600
        self.assertTrue(
            await qq.send_group_text("group-1", "@小明 hello", msg_id="message-1")
        )
        self.assertTrue(any("/v2/groups/group-1/messages" in url for url in rest.urls))

    async def test_long_reply_temp_file_is_private_and_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-qq-reply-") as directory:
            qq = QQApi("app", "secret", temp_dir=Path(directory))
            observed: dict[str, Any] = {}

            async def send_text(openid: str, content: str, *, keyboard=None) -> bool:
                return True

            async def send_file(file_path: str, openid: str) -> str:
                path = Path(file_path)
                observed["mode"] = path.stat().st_mode & 0o777
                observed["path"] = path
                return "✅ 文件已发送"

            qq.send_text = send_text  # type: ignore[method-assign]
            qq.send_local_file = send_file  # type: ignore[method-assign]
            self.assertTrue(await qq.send_reply("owner", "x" * 2000))
            self.assertEqual(observed["mode"], 0o600)
            self.assertFalse(observed["path"].exists())


if __name__ == "__main__":
    unittest.main()
