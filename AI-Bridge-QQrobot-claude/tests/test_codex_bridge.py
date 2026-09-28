"""Regression tests for the Codex App Server based QQ bridge."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_SRC = REPO_ROOT / "packages" / "codex-qq-bridge" / "src"
if str(CODEX_SRC) not in sys.path:
    sys.path.insert(0, str(CODEX_SRC))

from codex_qq_bridge.app_server import (  # noqa: E402
    APP_SERVER_STREAM_LIMIT,
    AppServerClient,
    AppServerError,
)
from codex_qq_bridge.bridge import (  # noqa: E402
    CodexQQBridge,
    GROUP_TOOLSET_VERSION,
    GroupRuntime,
    PendingApproval,
    ReplyTarget,
    attachment_inputs,
    format_token_usage,
    interaction_operator_and_button,
    group_learner_id_for_openid,
    heartbeat_sender,
    learner_id_for_openid,
    split_daily_lesson_sections,
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

    async def send_local_image(
        self,
        path: str,
        openid: str,
        *,
        is_group: bool = False,
        msg_id: str = "",
    ) -> str:
        return f"✅ 图片已发送: {Path(path).name}"

    async def send_local_file(self, path: str, openid: str) -> str:
        return f"✅ 文件已发送: {Path(path).name}"

    async def close(self) -> None:
        return None


class FakeHeartbeatWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self.close_count = 0

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


class FakeApp:
    def __init__(self) -> None:
        self.is_running = True
        self.notification_handler = None
        self.server_request_handler = None
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.thread_list_data: list[dict[str, Any]] = []
        self.thread_start_ids: list[str] = []
        self.fail_next_resume_connection = False
        self.config: dict[str, Any] = {}

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False

    async def request(self, method: str, params: dict[str, Any], **kwargs) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/start":
            thread_id = self.thread_start_ids.pop(0) if self.thread_start_ids else "thread-new"
            return {"thread": {"id": thread_id, "cwd": params.get("cwd")}}
        if method == "thread/resume":
            if self.fail_next_resume_connection:
                self.fail_next_resume_connection = False
                self.is_running = False
                raise AppServerError("Codex App Server connection closed")
            return {"thread": {"id": params["threadId"], "cwd": params.get("cwd")}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "config/read":
            return {"config": self.config, "origins": {}}
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
        self.temp.cleanup()

    async def test_master_binds_once_and_cannot_be_taken_over(self) -> None:
        self.assertTrue(await self.bridge.bind_or_authorize("owner"))
        self.assertFalse(await self.bridge.bind_or_authorize("attacker"))
        self.assertEqual(self.bridge.master_openid, "owner")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("MASTER_OPENID=owner", text)
        self.assertNotIn("attacker", text)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    async def test_missing_gateway_heartbeat_ack_forces_reconnect(self) -> None:
        ws = FakeHeartbeatWebSocket()
        state = {"seq": 42, "heartbeat_acked": False}

        await heartbeat_sender(ws, 0, state)

        self.assertEqual(ws.close_count, 1)
        self.assertEqual(ws.sent, [])

    async def test_gateway_heartbeat_marks_ack_pending_after_send(self) -> None:
        ws = FakeHeartbeatWebSocket()
        state = {"seq": 42, "heartbeat_acked": True}
        task = asyncio.create_task(heartbeat_sender(ws, 0.01, state))

        while not ws.sent:
            await asyncio.sleep(0)
        task.cancel()
        await task

        self.assertEqual(ws.sent, [{"op": 1, "d": 42}])
        self.assertIs(state["heartbeat_acked"], False)

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

    async def test_effective_config_is_applied_to_existing_threads(self) -> None:
        self.app.config = {
            "model": "gpt-6-sol",
            "model_reasoning_effort": "medium",
        }
        self.app.is_running = False
        self.bridge.state.thread_id = "stored-thread"

        await self.bridge.start_turn([{"type": "text", "text": "hello"}], "m-config")

        resume = next(
            params for method, params in self.app.requests if method == "thread/resume"
        )
        turn = next(
            params for method, params in self.app.requests if method == "turn/start"
        )
        self.assertEqual(resume["model"], "gpt-6-sol")
        self.assertEqual(turn["model"], "gpt-6-sol")
        self.assertEqual(turn["effort"], "medium")

    async def test_runtime_recovers_stored_thread(self) -> None:
        self.bridge.state.thread_id = "stored-thread"
        self.app.is_running = False
        await self.bridge.ensure_runtime()
        self.assertTrue(self.app.is_running)
        self.assertEqual(self.bridge.state.thread_id, "stored-thread")
        self.assertEqual(self.app.requests[-1][0], "thread/resume")
        self.assertIs(self.app.requests[-1][1]["excludeTurns"], True)

    async def test_app_server_reader_accepts_json_frames_over_default_limit(self) -> None:
        client = AppServerClient()
        reader = asyncio.StreamReader(limit=APP_SERVER_STREAM_LIMIT)
        client.process = SimpleNamespace(returncode=None, stdout=reader)
        client._connection_closed = False
        future = asyncio.get_running_loop().create_future()
        client._pending[1] = future
        payload = {"id": 1, "result": {"text": "x" * (128 * 1024)}}
        reader.feed_data((json.dumps(payload) + "\n").encode())
        reader.feed_eof()

        await client._read_stdout()

        self.assertEqual(len((await future)["text"]), 128 * 1024)
        self.assertFalse(client.is_running)

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

    async def test_agent_deltas_are_buffered_into_one_final_message(self) -> None:
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
        self.assertEqual(self.qq.messages, [])
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
        self.assertEqual(
            [message[1] for message in self.qq.messages],
            ["第一段已经生成。\n\n第二段也已经生成。"],
        )
        self.assertNotIn("stream-item", self.bridge.stream_replies)

    async def test_group_reply_under_safe_limit_stays_in_one_bubble(self) -> None:
        self.bridge.state.thread_id = "thread-1"
        self.bridge.active_reply_target = ReplyTarget(
            "group", "group-1", msg_id="group-message", member_name="小明"
        )
        text = "日" * 1300
        await self.bridge.handle_codex_notification(
            "item/agentMessage/delta",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "group-stream-item",
                "delta": text,
            },
        )
        self.assertEqual(self.qq.group_messages, [])
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "group-stream-item",
                    "type": "agentMessage",
                    "text": text,
                },
            },
        )
        self.assertEqual(len(self.qq.group_messages), 1)
        self.assertEqual(self.qq.group_messages[0][1], "@小明 " + text)

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
        self.assertEqual(self.qq.messages, [])
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "media-item", "type": "agentMessage", "text": delta},
            },
        )
        self.assertEqual([message[1] for message in self.qq.messages], ["已生成图片。"])
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

    def test_qq_http_client_ignores_environment_proxy_by_default(self) -> None:
        qq = QQApi("app", "secret")
        client = object()
        with patch(
            "codex_qq_bridge.qq_api.httpx.AsyncClient", return_value=client
        ) as constructor:
            self.assertIs(qq._http(), client)

        constructor.assert_called_once_with(
            timeout=30.0,
            follow_redirects=True,
            trust_env=False,
        )

    def test_qq_http_proxy_inheritance_requires_explicit_opt_in(self) -> None:
        qq = QQApi("app", "secret", trust_env_proxy=True)
        with patch("codex_qq_bridge.qq_api.httpx.AsyncClient") as constructor:
            qq._http()

        self.assertIs(constructor.call_args.kwargs["trust_env"], True)

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
        group_thread_id = params["threadId"]
        self.assertNotEqual(group_thread_id, "thread-1")
        group_start = next(
            params
            for method, params in self.app.requests
            if method == "thread/start" and params.get("dynamicTools")
        )
        self.assertEqual(
            {tool["name"] for tool in group_start["dynamicTools"]},
            {"publish_daily_lesson", "publish_latest_x_post"},
        )
        self.assertIn("QQ 群中的通用 Agent", group_start["developerInstructions"])
        self.assertIn(
            "不要仅因为问题与日语学习无关而拒绝",
            group_start["developerInstructions"],
        )
        self.assertIn("政治、暴力、色情", group_start["developerInstructions"])
        self.assertIn("因此我不能回答", group_start["developerInstructions"])
        self.assertIn("AyAsA 最新 X 动态", group_start["developerInstructions"])
        self.assertNotIn("developerInstructions", self.bridge._thread_options())
        self.assertIn("群聊用户 小明", params["input"][0]["text"])
        self.assertRegex(params["input"][0]["text"], r"learner_id: qq_[0-9a-f]{12}")
        self.assertRegex(params["input"][0]["text"], r"群课程轨迹 id：group_[0-9a-f]{12}")
        self.assertNotIn("member-1", params["input"][0]["text"])
        await self.bridge.handle_codex_notification(
            "item/completed",
            {
                "threadId": group_thread_id,
                "turnId": "turn-1",
                "item": {"id": "group-item", "type": "agentMessage", "text": "答案"},
            },
        )
        self.assertEqual(
            self.qq.group_messages,
            [("group-1", "@小明 答案", "group-message-1")],
        )
        self.assertEqual(self.bridge.master_openid, "owner")

    async def test_different_groups_use_different_persistent_threads(self) -> None:
        self.app.thread_start_ids = ["group-thread-1", "group-thread-2"]
        for index in (1, 2):
            await self.bridge.handle_group_message(
                {
                    "id": f"message-{index}",
                    "group_openid": f"group-{index}",
                    "content": "こんにちは",
                    "author": {"member_openid": f"member-{index}", "nickname": "成员"},
                }
            )
        starts = [params for method, params in self.app.requests if method == "turn/start"]
        self.assertEqual(
            [params["threadId"] for params in starts],
            ["group-thread-1", "group-thread-2"],
        )
        self.assertEqual(
            self.bridge.state.groups["group-1"]["thread_id"], "group-thread-1"
        )
        self.assertEqual(
            self.bridge.state.groups["group-2"]["thread_id"], "group-thread-2"
        )
        self.assertEqual(
            self.bridge.state.groups["group-1"]["toolset_version"],
            GROUP_TOOLSET_VERSION,
        )

    async def test_group_thread_rotates_when_dynamic_toolset_changes(self) -> None:
        self.bridge.state.groups = {
            "group-1": {
                "thread_id": "legacy-group-thread",
                "learner_id": "group_test",
                "active": True,
                "toolset_version": 2,
            }
        }
        self.app.thread_start_ids = ["upgraded-group-thread"]

        await self.bridge.handle_group_message(
            {
                "id": "group-message-toolset-upgrade",
                "group_openid": "group-1",
                "content": "こんにちは",
                "author": {"member_openid": "member-1", "nickname": "成员"},
            }
        )

        entry = self.bridge.state.groups["group-1"]
        self.assertEqual(entry["thread_id"], "upgraded-group-thread")
        self.assertEqual(entry["toolset_version"], GROUP_TOOLSET_VERSION)
        self.assertEqual(entry["previous_thread_ids"], ["legacy-group-thread"])
        self.assertFalse(
            any(
                method == "thread/resume"
                and params.get("threadId") == "legacy-group-thread"
                for method, params in self.app.requests
            )
        )

    async def test_resumed_group_thread_receives_tutor_scope_instructions(self) -> None:
        self.bridge.state.groups = {
            "group-1": {
                "thread_id": "stored-group-thread",
                "learner_id": "group_test",
                "active": True,
                "toolset_version": GROUP_TOOLSET_VERSION,
            }
        }

        await self.bridge.handle_group_message(
            {
                "id": "group-message-resume",
                "group_openid": "group-1",
                "content": "こんにちは",
                "author": {"member_openid": "member-1", "nickname": "成员"},
            }
        )

        resume = next(
            params for method, params in self.app.requests if method == "thread/resume"
        )
        self.assertEqual(resume["threadId"], "stored-group-thread")
        self.assertIs(resume["excludeTurns"], True)
        self.assertIn("QQ 群中的通用 Agent", resume["developerInstructions"])
        self.assertNotIn("只处理日语学习", resume["developerInstructions"])
        self.assertIn("因此我不能回答", resume["developerInstructions"])

    async def test_group_resume_reader_failure_recovers_before_new_thread(self) -> None:
        self.bridge.state.groups = {
            "group-1": {
                "thread_id": "stored-group-thread",
                "learner_id": "group_test",
                "active": True,
                "toolset_version": GROUP_TOOLSET_VERSION,
            }
        }
        self.app.fail_next_resume_connection = True
        self.app.thread_start_ids = ["private-recovery", "replacement-group-thread"]

        await self.bridge.handle_group_message(
            {
                "id": "group-message-recovery",
                "group_openid": "group-1",
                "content": "重新发布今天的课程",
                "author": {"member_openid": "member-1", "nickname": "成员"},
            }
        )

        self.assertTrue(self.app.is_running)
        self.assertEqual(
            self.bridge.state.groups["group-1"]["thread_id"],
            "replacement-group-thread",
        )
        turn = next(params for method, params in reversed(self.app.requests) if method == "turn/start")
        self.assertEqual(turn["threadId"], "replacement-group-thread")

    async def test_group_lifecycle_event_registers_and_disables_delivery(self) -> None:
        self.app.thread_start_ids = ["joined-thread"]
        await self.bridge.handle_group_added({"group_openid": "joined-group"})
        entry = self.bridge.state.groups["joined-group"]
        self.assertEqual(entry["thread_id"], "joined-thread")
        self.assertTrue(entry["active"])

        await self.bridge.handle_group_removed({"group_openid": "joined-group"})
        self.assertFalse(entry["active"])
        self.bridge._generate_group_lesson = AsyncMock()
        result = await self.bridge.publish_due_daily_lessons(
            datetime(2026, 8, 26, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.assertEqual(result, {})
        self.bridge._generate_group_lesson.assert_not_awaited()

    async def test_daily_lesson_is_one_proactive_bubble_per_section(self) -> None:
        lesson = self.root / "lesson.md"
        lesson.write_text(
            "# Lesson\n\n## 今日复习\n复习\n\n## 今日表达\n表达\n\n"
            "## 今日语法\n语法\n\n## 今日单词\n单词\n\n## 小练习\n练习\n\n"
            "## Source\n来源\n",
            encoding="utf-8",
        )
        self.bridge.state.groups = {
            "group-1": {"learner_id": "group_abc123", "last_lesson_date": None}
        }
        self.bridge.daily_message_interval = 0
        self.bridge._generate_group_lesson = AsyncMock(return_value=lesson)
        self.bridge._mark_group_lesson_published = AsyncMock()

        result = await self.bridge.publish_due_daily_lessons(
            datetime(2026, 8, 26, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

        self.assertEqual(result, {"group-1": True})
        self.assertEqual(len(self.qq.group_messages), 6)
        self.assertTrue(self.qq.group_messages[0][1].startswith("# Daily Japanese Lesson"))
        self.assertEqual(
            [message[1].split("## ", 1)[1].splitlines()[0] for message in self.qq.group_messages],
            ["今日复习", "今日表达", "今日语法", "今日单词", "小练习", "Source"],
        )
        self.assertTrue(all(message[2] == "" for message in self.qq.group_messages))
        self.assertEqual(
            self.bridge.state.groups["group-1"]["last_lesson_date"], "2026-08-26"
        )

        self.qq.group_messages.clear()
        partial = await self.bridge.publish_group_daily_lesson(
            "group-1",
            "2026-08-26",
            requested_sections=["vocabulary"],
        )
        self.assertTrue(partial)
        self.assertEqual(len(self.qq.group_messages), 1)
        self.assertIn("## 今日单词", self.qq.group_messages[0][1])
        self.assertEqual(self.bridge._mark_group_lesson_published.await_count, 1)

    async def test_codex_dynamic_tool_publishes_selected_snapshot_sections(self) -> None:
        self.bridge.state.groups = {
            "group-1": {
                "active": True,
                "learner_id": "group_abc123",
                "not_before_date": "2026-08-27",
            }
        }
        self.bridge.thread_to_group["group-thread"] = "group-1"
        self.bridge.publish_group_daily_lesson = AsyncMock(return_value=True)

        result = await self.bridge.handle_codex_server_request(
            "item/tool/call",
            {
                "threadId": "group-thread",
                "turnId": "turn-1",
                "callId": "call-1",
                "tool": "publish_daily_lesson",
                "arguments": {"sections": ["vocabulary", "exercises"]},
            },
            42,
        )

        self.assertTrue(result["success"])
        args = self.bridge.publish_group_daily_lesson.await_args
        self.assertEqual(args.args[0], "group-1")
        self.assertEqual(
            args.kwargs["requested_sections"], ["vocabulary", "exercises"]
        )

    async def test_codex_dynamic_tool_publishes_latest_ayasa_post(self) -> None:
        self.bridge.thread_to_group["group-thread"] = "group-1"
        runtime = GroupRuntime(
            active_reply_target=ReplyTarget(
                "group", "group-1", msg_id="message-x", member_name="小明"
            )
        )
        self.bridge.group_runtimes["group-1"] = runtime
        self.bridge.publish_latest_x_post = AsyncMock(
            return_value={
                "post_id": "2103850875332755953",
                "text_chunks": 1,
                "images": 4,
                "analysis_generated": False,
            }
        )

        result = await self.bridge.handle_codex_server_request(
            "item/tool/call",
            {
                "threadId": "group-thread",
                "turnId": "turn-1",
                "callId": "call-x",
                "tool": "publish_latest_x_post",
                "arguments": {},
            },
            43,
        )

        self.assertTrue(result["success"])
        self.bridge.publish_latest_x_post.assert_awaited_once_with(
            "group-1", reply_msg_id="message-x"
        )
        self.assertIn("4 image(s)", result["contentItems"][0]["text"])

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
    def test_learner_id_is_stable_and_does_not_expose_openid(self) -> None:
        learner_id = learner_id_for_openid("member-secret-openid")
        self.assertEqual(learner_id, learner_id_for_openid("member-secret-openid"))
        self.assertRegex(learner_id, r"^qq_[0-9a-f]{12}$")
        self.assertNotIn("member-secret-openid", learner_id)

    def test_group_curriculum_id_is_stable_and_pseudonymous(self) -> None:
        learner_id = group_learner_id_for_openid("raw-group-openid")
        self.assertEqual(learner_id, group_learner_id_for_openid("raw-group-openid"))
        self.assertRegex(learner_id, r"^group_[0-9a-f]{12}$")
        self.assertNotIn("raw-group-openid", learner_id)

    def test_daily_lesson_sections_keep_requested_order(self) -> None:
        markdown = "# title\n## 今日复习\na\n## 今日表达\nb\n## ignored\nx\n## 小练习\nc"
        self.assertEqual(
            [section.splitlines()[0] for section in split_daily_lesson_sections(markdown)],
            ["## 今日复习", "## 今日表达", "## 小练习"],
        )


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
        self.json_bodies: list[dict[str, Any] | None] = []

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.urls.append(url)
        self.json_bodies.append(kwargs.get("json"))
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

    async def test_group_image_uses_trigger_message_as_reply_reference(self) -> None:
        qq = QQApi("app", "secret")
        rest = FakeRestClient()
        qq._client = rest  # type: ignore[assignment]
        qq._access_token = "token"
        qq._token_expires_at = time.time() + 3600

        self.assertTrue(
            await qq._send_media(
                "file-info",
                "group-1",
                is_group=True,
                msg_id="message-1",
            )
        )
        message_body = next(
            body
            for url, body in zip(rest.urls, rest.json_bodies)
            if url.endswith("/messages")
        )
        self.assertEqual(message_body["msg_id"], "message-1")
        self.assertEqual(
            message_body["message_reference"], {"message_id": "message-1"}
        )

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
