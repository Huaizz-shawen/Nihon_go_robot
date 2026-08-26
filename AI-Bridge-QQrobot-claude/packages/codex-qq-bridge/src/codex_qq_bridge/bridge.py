#!/usr/bin/env python3
"""Bridge private QQ messages to a local Codex App Server."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import re
import secrets
import signal
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .app_server import AppServerClient, AppServerError
from .qq_api import QQApi, build_approval_keyboard, extract_media_markers


PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[3]


def load_env() -> Path | None:
    """Load the first bridge .env found and return its path."""
    candidates = [
        Path.cwd() / ".env",
        REPO_ROOT / ".env",
        PACKAGE_DIR / ".env",
        Path.home() / "AI-Bridge-QQrobot-claude" / "packages" / "codex-qq-bridge" / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
            return path
        except OSError:
            continue
    return None


ENV_PATH = load_env()
APP_ID = os.environ.get("APP_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
MASTER_OPENID = os.environ.get("MASTER_OPENID", "")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEFAULT_CWD = str(Path(os.environ.get("CODEX_CWD", str(Path.cwd()))).expanduser().resolve())
DEFAULT_SANDBOX = os.environ.get("CODEX_SANDBOX", "workspace-write")
DEFAULT_APPROVAL_POLICY = os.environ.get("CODEX_APPROVAL_POLICY", "on-request")
STATE_FILE = Path(
    os.environ.get(
        "CODEX_QQ_STATE_FILE",
        str(Path.home() / ".config" / "codex-qq-bridge" / "state.json"),
    )
).expanduser()
LOG_DIR = Path(os.environ.get("BRIDGE_LOG_DIR", str(REPO_ROOT / "logs"))).expanduser()

API_INTENTS = (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26)
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
APPROVAL_TIMEOUT = 600.0
STREAM_FLUSH_INTERVAL = 2.0
STREAM_MIN_CHARS = 120
STREAM_CHUNK_CHARS = 1200

logger = logging.getLogger("codex_qq_bridge")


def configure_logging() -> None:
    if logger.handlers:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "codex-bridge.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    log_path.chmod(0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    if os.environ.get("BRIDGE_QUIET_STDOUT") != "1":
        logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)
    logger.propagate = False


@dataclass
class BridgeState:
    thread_id: str | None = None
    cwd: str = DEFAULT_CWD
    sandbox: str = DEFAULT_SANDBOX
    approval_policy: str = DEFAULT_APPROVAL_POLICY

    @classmethod
    def load(cls, path: Path) -> "BridgeState":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            state = cls(
                thread_id=data.get("thread_id"),
                cwd=str(data.get("cwd") or DEFAULT_CWD),
                sandbox=str(data.get("sandbox") or DEFAULT_SANDBOX),
                approval_policy=str(data.get("approval_policy") or DEFAULT_APPROVAL_POLICY),
            )
        except (OSError, ValueError, TypeError):
            state = cls()
        if not Path(state.cwd).is_dir():
            state.cwd = DEFAULT_CWD
        if state.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            state.sandbox = "workspace-write"
        if state.approval_policy not in {"untrusted", "on-request", "never"}:
            state.approval_policy = "on-request"
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)


@dataclass
class PendingApproval:
    method: str
    params: dict[str, Any]
    future: asyncio.Future[str]


@dataclass(frozen=True)
class ReplyTarget:
    """QQ destination associated with the currently active Codex turn."""

    chat_type: str
    chat_id: str
    msg_id: str = ""
    member_openid: str = ""
    member_name: str = ""


@dataclass
class StreamReply:
    """Incremental App Server text that has not yet been delivered to QQ."""

    text: str = ""
    sent_chars: int = 0
    last_flush: float = field(default_factory=time.monotonic)
    scheduled: bool = False
    delta_events: int = 0
    chunks_sent: int = 0
    target: ReplyTarget | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def attachment_inputs(content: str, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate QQ attachments into supported Codex turn input items."""
    inputs: list[dict[str, Any]] = []
    trailing: list[str] = []
    if content:
        inputs.append({"type": "text", "text": content})
    for attachment in attachments:
        url = str(attachment.get("url", "")).strip()
        content_type = str(attachment.get("content_type", "")).lower()
        name = str(attachment.get("filename", "file"))
        if not url:
            continue
        if "image" in content_type:
            inputs.append({"type": "image", "url": url})
        elif "audio" in content_type or "voice" in content_type:
            inputs.append({"type": "audio", "url": url})
        else:
            trailing.append(f"附件 {name}: {url}")
    if trailing:
        inputs.append({"type": "text", "text": "\n".join(trailing)})
    if not inputs:
        inputs.append({"type": "text", "text": "用户发送了一个空消息。"})
    return inputs


async def prepare_attachment_inputs(
    content: str, attachments: list[dict[str, Any]], qq: QQApi
) -> list[dict[str, Any]]:
    """Inline remote QQ image/audio attachments for the local App Server."""
    prepared: list[dict[str, Any]] = []
    errors: list[str] = []
    for attachment in attachments:
        copy = dict(attachment)
        content_type = str(copy.get("content_type", "")).lower()
        url = str(copy.get("url", "")).strip()
        voice_url = str(copy.get("voice_wav_url", "")).strip()
        if voice_url:
            url = voice_url
            content_type = "audio/wav"
            copy["content_type"] = content_type
        elif "voice" in content_type and not content_type.startswith("audio/"):
            content_type = "audio/wav"
            copy["content_type"] = content_type
        if url.startswith("//"):
            url = "https:" + url
        if url and ("image" in content_type or "audio" in content_type or "voice" in content_type):
            try:
                copy["url"] = await qq.fetch_attachment_data_url(url, content_type)
            except Exception as exc:
                logger.warning("Unable to prepare QQ media attachment: %s", exc)
                name = str(copy.get("filename") or "媒体附件")
                errors.append(f"附件 {name} 无法安全读取：{exc}")
                copy["url"] = ""
        elif url:
            copy["url"] = url
        prepared.append(copy)
    inputs = attachment_inputs(content, prepared)
    if errors:
        inputs.append({"type": "text", "text": "\n".join(errors)})
    return inputs


def interaction_operator_and_button(data: dict[str, Any]) -> tuple[str, str]:
    """Parse current QQ OpenAPI v2 interaction fields with legacy fallback."""
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    data_block = data.get("data") if isinstance(data.get("data"), dict) else {}
    resolved = (
        data_block.get("resolved")
        if isinstance(data_block.get("resolved"), dict)
        else {}
    )
    openid = str(
        data.get("user_openid")
        or data.get("group_member_openid")
        or author.get("user_openid")
        or author.get("member_openid")
        or resolved.get("user_id")
        or ""
    )
    button_data = str(
        resolved.get("button_data") or data_block.get("button_data") or ""
    )
    return openid, button_data


def format_token_usage(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "📊 当前会话尚无 token 使用数据。"
    total = usage.get("total") or {}
    last = usage.get("last") or {}
    window = usage.get("modelContextWindow")
    current = int(last.get("totalTokens", 0))
    lines = [
        "📊 **Codex Context**",
        f"本轮: {current:,} tokens" + (f" ({current / window:.1%})" if window else ""),
        f"累计输入: {int(total.get('inputTokens', 0)):,}",
        f"累计输出: {int(total.get('outputTokens', 0)):,}",
        f"缓存输入: {int(total.get('cachedInputTokens', 0)):,}",
    ]
    if window:
        lines.append(f"上下文窗口: {int(window):,}")
    return "\n".join(lines)


class CodexQQBridge:
    def __init__(
        self,
        *,
        qq: QQApi | None = None,
        app_server: AppServerClient | None = None,
        state_file: Path = STATE_FILE,
        env_path: Path | None = ENV_PATH,
        master_openid: str = MASTER_OPENID,
    ) -> None:
        self.qq = qq or QQApi(APP_ID, CLIENT_SECRET, logger=logger)
        self.state_file = state_file
        self.env_path = env_path
        self.master_openid = master_openid
        self.state = BridgeState.load(state_file)
        self.app = app_server or AppServerClient(
            codex_bin=CODEX_BIN,
            logger=logger,
            notification_handler=self.handle_codex_notification,
            server_request_handler=self.handle_codex_server_request,
        )
        if app_server:
            self.app.notification_handler = self.handle_codex_notification
            self.app.server_request_handler = self.handle_codex_server_request
        self.active_turn_id: str | None = None
        self.last_msg_id: str | None = None
        self.last_token_usage: dict[str, Any] | None = None
        self.resume_mapping: dict[int, dict[str, Any]] = {}
        self.pending_approvals: dict[str, PendingApproval] = {}
        self.seen_message_ids: dict[str, float] = {}
        self.sent_item_ids: set[str] = set()
        self.side_threads: dict[str, asyncio.Future[str]] = {}
        self.side_replies: dict[str, list[str]] = {}
        self.completed_turn_ids: set[str] = set()
        self.stream_replies: dict[str, StreamReply] = {}
        self.active_reply_target: ReplyTarget | None = None
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._qq_tasks: set[asyncio.Task[None]] = set()
        self._runtime_lock = asyncio.Lock()
        self._master_lock = asyncio.Lock()
        self._typing_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        await self.ensure_runtime()
        self._monitor_task = asyncio.create_task(self._monitor_runtime())

    async def close(self) -> None:
        self._running = False
        if self._typing_task:
            self._typing_task.cancel()
        if self._monitor_task:
            self._monitor_task.cancel()
        for task in tuple(self._stream_tasks):
            task.cancel()
        self._stream_tasks.clear()
        for task in tuple(self._qq_tasks):
            task.cancel()
        if self._qq_tasks:
            await asyncio.gather(*self._qq_tasks, return_exceptions=True)
        self._qq_tasks.clear()
        self.stream_replies.clear()
        for pending in self.pending_approvals.values():
            if not pending.future.done():
                pending.future.set_result("decline")
        await self.app.stop()
        await self.qq.close()

    async def ensure_runtime(self) -> None:
        async with self._runtime_lock:
            if self.app.is_running:
                return
            await self.app.start()
            if self.state.thread_id:
                try:
                    await self._resume_thread(self.state.thread_id)
                    return
                except AppServerError as exc:
                    logger.warning("Unable to resume stored Codex thread: %s", exc)
            await self._new_thread()

    async def _monitor_runtime(self) -> None:
        while self._running:
            await asyncio.sleep(5)
            if self.app.is_running:
                continue
            try:
                logger.warning("Codex App Server stopped; recovering")
                await self.ensure_runtime()
            except Exception as exc:
                logger.error("Codex App Server recovery failed: %s", exc)

    def _thread_options(self) -> dict[str, Any]:
        return {
            "cwd": self.state.cwd,
            "sandbox": self.state.sandbox,
            "approvalPolicy": self.state.approval_policy,
            "approvalsReviewer": "user",
            "serviceName": "codex_qq_bridge",
        }

    async def _new_thread(self, cwd: str | None = None) -> dict[str, Any]:
        if cwd:
            self.state.cwd = cwd
        result = await self.app.request("thread/start", self._thread_options())
        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise AppServerError("thread/start response did not contain a thread id")
        self.state.thread_id = str(thread_id)
        self.active_turn_id = None
        self.last_token_usage = None
        self.sent_item_ids.clear()
        self.stream_replies.clear()
        self.state.save(self.state_file)
        return thread

    async def _resume_thread(self, thread_id: str) -> dict[str, Any]:
        params = {"threadId": thread_id, **self._thread_options()}
        result = await self.app.request("thread/resume", params)
        thread = result.get("thread") or {}
        if not thread.get("id"):
            raise AppServerError("thread/resume response did not contain a thread")
        self.state.thread_id = str(thread["id"])
        if thread.get("cwd") and Path(str(thread["cwd"])).is_dir():
            self.state.cwd = str(thread["cwd"])
        self.active_turn_id = None
        self.sent_item_ids.clear()
        self.stream_replies.clear()
        self.state.save(self.state_file)
        return thread

    def _private_target(self) -> ReplyTarget | None:
        if not self.master_openid:
            return None
        return ReplyTarget("c2c", self.master_openid, msg_id=self.last_msg_id or "")

    @staticmethod
    def _normalize_target(target: ReplyTarget | str) -> ReplyTarget:
        if isinstance(target, ReplyTarget):
            return target
        return ReplyTarget("c2c", target)

    async def _send_reply(
        self,
        content: str,
        *,
        target: ReplyTarget | str | None = None,
        keyboard: dict[str, Any] | None = None,
    ) -> bool:
        destination = (
            self._normalize_target(target)
            if target is not None
            else self.active_reply_target or self._private_target()
        )
        if not destination:
            return False
        if destination.chat_type == "group":
            label = re.sub(r"[\r\n\x00]+", " ", destination.member_name).strip()
            label = label[:80] or (
                f"用户{destination.member_openid[-6:]}"
                if destination.member_openid
                else "触发者"
            )
            prefix = f"@{label} "
            limit = max(1, 1400 - len(prefix))
            chunks = [content[index : index + limit] for index in range(0, len(content), limit)]
            for chunk in chunks or [""]:
                if not await self.qq.send_group_text(
                    destination.chat_id,
                    prefix + chunk,
                    msg_id=destination.msg_id,
                ):
                    return False
            return True
        return await self.qq.send_reply(
            destination.chat_id, content, keyboard=keyboard
        )

    async def start_turn(
        self,
        inputs: list[dict[str, Any]],
        msg_id: str,
        target: ReplyTarget | None = None,
    ) -> str:
        await self.ensure_runtime()
        if not self.state.thread_id:
            await self._new_thread()
        self.last_msg_id = msg_id
        self.active_reply_target = target or self._private_target()
        if self.active_turn_id:
            result = await self.app.request(
                "turn/steer",
                {
                    "threadId": self.state.thread_id,
                    "expectedTurnId": self.active_turn_id,
                    "input": inputs,
                    "clientUserMessageId": msg_id,
                },
            )
            self._start_typing()
            return str(result.get("turnId") or self.active_turn_id)
        result = await self.app.request(
            "turn/start",
            {
                "threadId": self.state.thread_id,
                "input": inputs,
                "clientUserMessageId": msg_id,
                "cwd": self.state.cwd,
                "approvalPolicy": self.state.approval_policy,
                "approvalsReviewer": "user",
            },
        )
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id:
            raise AppServerError("turn/start response did not contain a turn id")
        turn_id = str(turn_id)
        if turn_id not in self.completed_turn_ids:
            self.active_turn_id = turn_id
            self._start_typing()
        return turn_id

    def _start_typing(self) -> None:
        if self._typing_task and not self._typing_task.done():
            return
        self._typing_task = asyncio.create_task(self._typing_loop())

    async def _typing_loop(self) -> None:
        try:
            while self.active_turn_id and self.active_reply_target:
                target = self.active_reply_target
                if target.chat_type != "c2c" or not target.msg_id:
                    return
                await self.qq.send_typing(target.chat_id, target.msg_id)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            return

    def _stop_typing(self) -> None:
        if self._typing_task:
            self._typing_task.cancel()
            self._typing_task = None

    def _schedule_stream_flush(self, item_id: str) -> None:
        state = self.stream_replies.get(item_id)
        if not state or state.scheduled:
            return
        state.scheduled = True
        task = asyncio.create_task(
            self._flush_stream_later(item_id), name=f"codex-qq-stream-{item_id[:12]}"
        )
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    async def _flush_stream_later(self, item_id: str) -> None:
        state = self.stream_replies.get(item_id)
        if not state:
            return
        delay = max(0.0, STREAM_FLUSH_INTERVAL - (time.monotonic() - state.last_flush))
        try:
            await asyncio.sleep(delay)
            state = self.stream_replies.get(item_id)
            if not state:
                return
            state.scheduled = False
            await self._flush_stream_item(item_id)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _stream_chunk_end(text: str, start: int, *, force: bool) -> int | None:
        available = text[start:]
        marker_at = available.find("[[SEND_")
        if marker_at >= 0:
            available = available[:marker_at]
        if not available:
            return None
        limit = min(len(available), STREAM_CHUNK_CHARS)
        if force:
            return start + limit
        candidate = available[:limit]
        if len(candidate) < STREAM_MIN_CHARS:
            return None
        if len(available) >= STREAM_CHUNK_CHARS:
            return start + limit
        boundaries = [
            candidate.rfind("\n\n"),
            candidate.rfind("\n"),
            candidate.rfind("。"),
            candidate.rfind("！"),
            candidate.rfind("？"),
            candidate.rfind(". "),
            candidate.rfind("! "),
            candidate.rfind("? "),
        ]
        boundary = max(boundaries)
        if boundary + 1 < STREAM_MIN_CHARS:
            return None
        return start + boundary + (2 if candidate[boundary : boundary + 2] in {"\n\n", ". ", "! ", "? "} else 1)

    async def _flush_stream_item(
        self, item_id: str, *, force: bool = False, final_text: str | None = None
    ) -> None:
        state = self.stream_replies.get(item_id)
        if not state:
            return
        async with state.lock:
            if final_text is not None:
                state.text = final_text
            while state.sent_chars < len(state.text):
                end = self._stream_chunk_end(state.text, state.sent_chars, force=force)
                if end is None:
                    break
                chunk = state.text[state.sent_chars:end]
                if not chunk.strip():
                    state.sent_chars = end
                    continue
                if not await self._send_reply(chunk.strip(), target=state.target):
                    break
                state.sent_chars = end
                state.last_flush = time.monotonic()
                state.chunks_sent += 1
                if not force:
                    break
            if state.sent_chars < len(state.text) and not force:
                self._schedule_stream_flush(item_id)

    async def _finish_stream_item(self, item_id: str, text: str) -> None:
        state = self.stream_replies.setdefault(
            item_id, StreamReply(target=self.active_reply_target)
        )
        await self._flush_stream_item(item_id, force=True, final_text=text)
        remaining = state.text[state.sent_chars:]
        clean, media = extract_media_markers(remaining)
        if clean:
            if await self._send_reply(clean, target=state.target):
                state.sent_chars = len(state.text)
        elif not clean:
            state.sent_chars = len(state.text)
        if media:
            await self._send_marked_media_safely(media, state.target)
        logger.info(
            "Codex agent item delivered to QQ (item=%s, chars=%s, deltas=%s, chunks=%s)",
            item_id[:12],
            len(text),
            state.delta_events,
            state.chunks_sent,
        )
        self.stream_replies.pop(item_id, None)

    async def _send_marked_media_safely(
        self, media: list[dict[str, str]], target: ReplyTarget | None = None
    ) -> None:
        """Only honor model-generated file markers inside the active workspace."""
        destination = target or self.active_reply_target or self._private_target()
        if not destination:
            return
        workspace = Path(self.state.cwd).resolve()
        allowed: list[dict[str, str]] = []
        for item in media:
            try:
                path = Path(item["path"]).expanduser().resolve()
                path.relative_to(workspace)
            except (KeyError, OSError, ValueError):
                logger.warning("Rejected Codex media marker outside active workspace")
                await self._send_reply(
                    "⚠️ 已拒绝发送工作目录之外的本地文件。请使用 `/sendfile <路径>` 明确发送。",
                    target=destination,
                )
                continue
            allowed.append({**item, "path": str(path)})
        if allowed:
            await self.qq.send_marked_media(
                allowed,
                destination.chat_id,
                is_group=destination.chat_type == "group",
            )

    async def handle_codex_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if method == "thread/tokenUsage/updated" and thread_id == self.state.thread_id:
            self.last_token_usage = params.get("tokenUsage") or {}
            return
        if method == "item/agentMessage/delta":
            if thread_id != self.state.thread_id:
                return
            item_id = str(params.get("itemId", ""))
            delta = str(params.get("delta", ""))
            if not item_id or not delta:
                return
            state = self.stream_replies.setdefault(
                item_id, StreamReply(target=self.active_reply_target)
            )
            state.text += delta
            state.delta_events += 1
            self._schedule_stream_flush(item_id)
            return
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") != "agentMessage":
                return
            item_id = str(item.get("id", ""))
            if item_id and item_id in self.sent_item_ids:
                return
            if item_id:
                self.sent_item_ids.add(item_id)
            text = str(item.get("text", "")).strip()
            if thread_id in self.side_threads:
                if text:
                    self.side_replies.setdefault(str(thread_id), []).append(text)
                return
            if thread_id == self.state.thread_id and text:
                await self._finish_stream_item(item_id, text)
            return
        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = str(turn.get("id", ""))
            if thread_id in self.side_threads:
                side_thread_id = str(thread_id)
                future = self.side_threads.pop(side_thread_id)
                reply = "\n\n".join(self.side_replies.pop(side_thread_id, []))
                if not future.done():
                    future.set_result(reply)
                return
            if thread_id == self.state.thread_id:
                if turn_id:
                    self.completed_turn_ids.add(turn_id)
                    if len(self.completed_turn_ids) > 1000:
                        self.completed_turn_ids.clear()
                self.active_turn_id = None
                self._stop_typing()
                status = turn.get("status")
                error = turn.get("error") or {}
                if status == "failed":
                    await self._send_reply(
                        f"❌ Codex 任务失败：{error.get('message', '未知错误')}",
                    )
            return
        if method == "error" and thread_id == self.state.thread_id:
            error = params.get("error") or {}
            await self._send_reply(
                f"❌ Codex 错误：{error.get('message', '未知错误')}"
            )

    async def handle_codex_server_request(
        self, method: str, params: dict[str, Any], request_id: int | str
    ) -> dict[str, Any]:
        if params.get("threadId") != self.state.thread_id or not self.master_openid:
            return self.app._safe_default_server_response(method)
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            return self.app._safe_default_server_response(method)
        token = secrets.token_urlsafe(6)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending_approvals[token] = PendingApproval(method, params, future)
        await self._send_reply(
            self._format_approval(method, params),
            target=self._private_target(),
            keyboard=build_approval_keyboard(token),
        )
        try:
            decision = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            decision = "decline"
            await self._send_reply(
                "⌛ Codex 审批已超时，自动拒绝。", target=self._private_target()
            )
        finally:
            self.pending_approvals.pop(token, None)
        if method == "item/permissions/requestApproval":
            permissions = params.get("permissions") or {}
            return {
                "permissions": permissions if decision.startswith("accept") else {},
                "scope": "session" if decision == "acceptForSession" else "turn",
            }
        return {"decision": decision}

    @staticmethod
    def _format_approval(method: str, params: dict[str, Any]) -> str:
        reason = params.get("reason") or "Codex 请求额外权限"
        cwd = params.get("cwd") or ""
        if method == "item/commandExecution/requestApproval":
            detail = params.get("command") or "(命令内容不可用)"
            kind = "命令执行"
        elif method == "item/fileChange/requestApproval":
            detail = params.get("grantRoot") or "文件修改"
            kind = "文件修改"
        else:
            detail = json.dumps(params.get("permissions") or {}, ensure_ascii=False)
            kind = "额外权限"
        text = f"🔐 **Codex 请求{kind}审批**\n原因: {reason}\n"
        if cwd:
            text += f"目录: `{cwd}`\n"
        return text + f"内容: `{str(detail)[:700]}`"

    async def resolve_approval(self, token: str, decision: str) -> bool:
        pending = self.pending_approvals.get(token)
        if not pending or pending.future.done():
            return False
        if decision not in {"accept", "acceptForSession", "decline", "cancel"}:
            return False
        pending.future.set_result(decision)
        return True

    async def bind_or_authorize(self, openid: str) -> bool:
        """Bind only once; a different sender can never replace the master."""
        if not openid or len(openid) > 512 or any(char in openid for char in "\r\n\0"):
            return False
        async with self._master_lock:
            if self.master_openid:
                return secrets.compare_digest(
                    openid.encode("utf-8"), self.master_openid.encode("utf-8")
                )
            if not self._persist_master_openid(openid):
                return False
            self.master_openid = openid
            logger.info("MASTER_OPENID bound to first QQ user")
            return True

    def _persist_master_openid(self, openid: str) -> bool:
        path = self.env_path or REPO_ROOT / ".env"
        temporary = path.with_name(f".{path.name}.codex-qq.tmp")
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            replaced = False
            for index, line in enumerate(lines):
                if line.startswith("MASTER_OPENID="):
                    lines[index] = f"MASTER_OPENID={openid}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"MASTER_OPENID={openid}")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
            self.env_path = path
            return True
        except OSError as exc:
            logger.error("Unable to persist MASTER_OPENID: %s", exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        previous = self.seen_message_ids.get(msg_id)
        self.seen_message_ids[msg_id] = now
        if len(self.seen_message_ids) > 1000:
            self.seen_message_ids = {
                key: stamp for key, stamp in self.seen_message_ids.items() if now - stamp < 600
            }
        return previous is not None and now - previous < 300

    async def handle_c2c_message(self, data: dict[str, Any]) -> None:
        msg_id = str(data.get("id", ""))
        if not msg_id or self.is_duplicate(msg_id):
            return
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        openid = str(author.get("user_openid", ""))
        if not await self.bind_or_authorize(openid):
            logger.warning("Ignored message from unauthorized QQ user")
            return
        content = str(data.get("content", "")).strip()
        attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
        if not content and not attachments:
            return
        try:
            if content.startswith("codex-approve:"):
                parts = content.split(":", 2)
                if len(parts) == 3:
                    await self.resolve_approval(parts[1], parts[2])
                return
            target = ReplyTarget("c2c", openid, msg_id=msg_id)
            if await self._handle_command(content, target):
                return
            inputs = await prepare_attachment_inputs(content, attachments, self.qq)
            logger.info(
                "QQ message prepared for Codex (attachments=%s, input_types=%s)",
                len(attachments),
                [item.get("type") for item in inputs],
            )
            await self.start_turn(inputs, msg_id, target)
        except Exception as exc:
            logger.exception("QQ message handling failed: %s", exc)
            await self._send_reply(f"❌ 请求处理失败：{exc}", target=target)

    async def handle_group_message(self, data: dict[str, Any]) -> None:
        """Allow any mentioned group member to trigger Codex and reply in-place."""
        msg_id = str(data.get("id", ""))
        if not msg_id or self.is_duplicate(msg_id):
            return
        group_openid = str(data.get("group_openid", ""))
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        member_openid = str(author.get("member_openid", ""))
        member_name = str(
            author.get("nickname")
            or author.get("username")
            or (f"用户{member_openid[-6:]}" if member_openid else "触发者")
        )
        content = str(data.get("content", "")).strip()
        attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
        if not group_openid or (not content and not attachments):
            return
        target = ReplyTarget(
            "group",
            group_openid,
            msg_id=msg_id,
            member_openid=member_openid,
            member_name=member_name,
        )
        try:
            if await self._handle_command(content, target):
                return
            inputs = await prepare_attachment_inputs(content, attachments, self.qq)
            inputs.insert(
                0,
                {
                    "type": "text",
                    "text": f"以下消息来自 QQ 群聊用户 {member_name}。请直接回答该用户。",
                },
            )
            logger.info(
                "QQ group message prepared for Codex (attachments=%s, input_types=%s)",
                len(attachments),
                [item.get("type") for item in inputs],
            )
            await self.start_turn(inputs, msg_id, target)
        except Exception as exc:
            logger.exception("QQ group message handling failed: %s", exc)
            await self._send_reply(f"❌ 请求处理失败：{exc}", target=target)

    async def _handle_command(
        self, content: str, target: ReplyTarget | str
    ) -> bool:
        target = self._normalize_target(target)
        command = content.strip()
        lower = command.lower()
        if target.chat_type == "group" and lower.startswith("/") and lower != "/help":
            await self._send_reply(
                "⚠️ 群聊仅开放普通问答；会话、权限和本机文件命令请由主人私聊机器人执行。",
                target=target,
            )
            return True
        if lower in {"/stop", "/tingzhi", "/kill"}:
            if self.active_turn_id and self.state.thread_id:
                await self.app.request(
                    "turn/interrupt",
                    {"threadId": self.state.thread_id, "turnId": self.active_turn_id},
                )
                await self._send_reply("⛔ 已请求中断当前 Codex 任务。", target=target)
            else:
                await self._send_reply("ℹ️ 当前没有正在运行的任务。", target=target)
            return True
        if lower in {"/new", "/clear", "/reset", "/qingkong", "/xin duihua"}:
            if self.active_turn_id:
                await self._handle_command("/stop", target)
            thread = await self._new_thread()
            await self._send_reply(
                f"✅ 新会话已开始\nID: `{str(thread['id'])[:8]}…`", target=target
            )
            return True
        if lower.startswith(("/resume", "/history", "/huifu")):
            parts = command.split(None, 1)
            if len(parts) == 2 and parts[1].isdigit():
                entry = self.resume_mapping.get(int(parts[1]))
                if not entry:
                    await self._send_reply("⚠️ 请先发送 /resume 获取会话列表。", target=target)
                    return True
                thread = await self._resume_thread(str(entry["id"]))
                await self._send_reply(
                    f"✅ 已恢复会话 {parts[1]}\n📁 {thread.get('cwd', self.state.cwd)}",
                    target=target,
                )
                self.resume_mapping.clear()
                return True
            result = await self.app.request(
                "thread/list",
                {
                    "limit": 10,
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                    "cwd": self.state.cwd,
                    "sourceKinds": ["appServer", "cli", "exec", "vscode"],
                },
            )
            threads = result.get("data") or []
            self.resume_mapping = {index: item for index, item in enumerate(threads, 1)}
            if not threads:
                await self._send_reply(f"📭 `{self.state.cwd}` 暂无历史会话。", target=target)
                return True
            lines = ["📋 **Codex 历史会话**"]
            for index, thread in self.resume_mapping.items():
                marker = " 🟢当前" if thread.get("id") == self.state.thread_id else ""
                preview = thread.get("name") or thread.get("preview") or "(空会话)"
                lines.append(f"[{index}] {str(preview)[:80]}{marker}")
            lines.append("\n发送 `/resume N` 恢复。")
            await self._send_reply("\n".join(lines), target=target)
            return True
        if lower.startswith("/cd"):
            value = command[3:].strip()
            if not value:
                await self._send_reply("⚠️ 用法: /cd <目录>", target=target)
                return True
            if target.chat_type == "group":
                await self._send_reply("⚠️ 群聊中不允许切换本机工作目录。", target=target)
                return True
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = Path(self.state.cwd) / path
            path = path.resolve()
            if not path.is_dir():
                await self._send_reply(f"❌ 目录不存在: {path}", target=target)
                return True
            if self.active_turn_id:
                await self._handle_command("/stop", target)
            await self._new_thread(str(path))
            await self._send_reply(f"✅ 已切换目录并创建新会话\n📁 {path}", target=target)
            return True
        if lower == "/pwd":
            await self._send_reply(f"📁 当前目录：\n{self.state.cwd}", target=target)
            return True
        if lower == "/ls" or lower.startswith("/ls "):
            value = command[3:].strip()
            path = Path(value).expanduser() if value else Path(self.state.cwd)
            if not path.is_absolute():
                path = Path(self.state.cwd) / path
            path = path.resolve()
            if not path.is_dir():
                await self._send_reply(f"❌ 目录不存在: {path}", target=target)
                return True
            try:
                entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            except PermissionError:
                await self._send_reply(f"❌ 无权限读取: {path}", target=target)
                return True
            visible = [item for item in entries if not item.name.startswith(".")]
            lines = [f"📁 {path}"] + [
                item.name + ("/" if item.is_dir() else "") for item in visible[:50]
            ]
            if len(visible) > 50:
                lines.append(f"…还有 {len(visible) - 50} 项")
            await self._send_reply("\n".join(lines), target=target)
            return True
        if lower == "/context":
            await self._send_reply(format_token_usage(self.last_token_usage), target=target)
            return True
        if lower.startswith("/compact"):
            if not self.state.thread_id:
                await self._send_reply("⚠️ 当前没有会话。", target=target)
            else:
                await self.app.request("thread/compact/start", {"threadId": self.state.thread_id})
                await self._send_reply("🗜️ 已开始压缩当前 Codex 会话。", target=target)
            return True
        if lower.startswith(("/btw", "/by-the-way")):
            question = command.split(None, 1)[1].strip() if " " in command else ""
            if not question:
                await self._send_reply("⚠️ 用法: /btw <问题>", target=target)
                return True
            await self._send_reply("💬 正在进行独立的 BTW 查询…", target=target)
            answer = await self._ask_side_question(question)
            await self._send_reply(
                "💬 **BTW**\n" + (answer or "未得到回答。"), target=target
            )
            return True
        if lower.startswith("/mode"):
            await self._handle_mode(command, target)
            return True
        if lower.startswith("/sendimg"):
            if target.chat_type == "group":
                await self._send_reply("⚠️ 群聊中不允许直接读取本机图片路径。", target=target)
                return True
            path = command[8:].strip()
            result = await self.qq.send_local_image(path, target.chat_id) if path else "⚠️ 用法: /sendimg <路径>"
            await self._send_reply(result, target=target)
            return True
        if lower.startswith("/sendfile"):
            if target.chat_type == "group":
                await self._send_reply("⚠️ 群聊中不允许直接读取本机文件路径。", target=target)
                return True
            path = command[9:].strip()
            result = await self.qq.send_local_file(path, target.chat_id) if path else "⚠️ 用法: /sendfile <路径>"
            await self._send_reply(result, target=target)
            return True
        if lower == "/help":
            await self._send_reply(
                "**Codex QQ Bridge 命令**\n"
                "/new /resume /stop /cd /pwd /ls\n"
                "/context /compact /btw /mode\n"
                "/sendimg /sendfile",
                target=target,
            )
            return True
        return False

    async def _ask_side_question(self, question: str) -> str:
        if not self.state.thread_id:
            await self._new_thread()
        fork = await self.app.request(
            "thread/fork", {"threadId": self.state.thread_id, "ephemeral": True}
        )
        side_thread = (fork.get("thread") or {}).get("id")
        if not side_thread:
            raise AppServerError("Unable to create BTW side thread")
        side_thread = str(side_thread)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.side_threads[side_thread] = future
        turn_id = ""
        try:
            result = await self.app.request(
                "turn/start",
                {
                    "threadId": side_thread,
                    "input": [{"type": "text", "text": question}],
                    "cwd": self.state.cwd,
                    "approvalPolicy": "never",
                },
            )
            turn_id = str((result.get("turn") or {}).get("id", ""))
            if not turn_id:
                raise AppServerError("Unable to start BTW turn")
            return await asyncio.wait_for(future, timeout=240)
        except asyncio.TimeoutError:
            if turn_id:
                await self.app.request(
                    "turn/interrupt", {"threadId": side_thread, "turnId": turn_id}
                )
            return "BTW 查询超时。"
        finally:
            self.side_threads.pop(side_thread, None)
            self.side_replies.pop(side_thread, None)

    async def _handle_mode(
        self, command: str, target: ReplyTarget | str
    ) -> None:
        target = self._normalize_target(target)
        parts = command.lower().split()
        if len(parts) == 1 or parts[1] == "status":
            await self._send_reply(
                f"🛡️ sandbox: `{self.state.sandbox}`\n审批策略: `{self.state.approval_policy}`",
                target=target,
            )
            return
        mode = parts[1]
        if mode in {"safe", "auto"}:
            sandbox, approval = "workspace-write", "on-request"
        elif mode in {"readonly", "read-only", "plan"}:
            sandbox, approval = "read-only", "on-request"
        elif mode == "full" and len(parts) >= 3 and parts[2] == "confirm":
            if target.chat_type == "group":
                await self._send_reply("⚠️ 群聊中不能启用全权限模式。", target=target)
                return
            sandbox, approval = "danger-full-access", "on-request"
        elif mode == "full":
            await self._send_reply(
                "⚠️ 全权限模式可访问整个系统。确认请发送 `/mode full confirm`。",
                target=target,
            )
            return
        else:
            await self._send_reply(
                "用法: /mode {status|safe|readonly|full confirm}", target=target
            )
            return
        self.state.sandbox = sandbox
        self.state.approval_policy = approval
        self.state.save(self.state_file)
        if self.state.thread_id:
            try:
                await self._resume_thread(self.state.thread_id)
            except AppServerError:
                # An empty thread has no rollout until its first turn, so it cannot
                # be resumed yet. Recreate that still-empty thread with the mode.
                await self._new_thread()
        await self._send_reply(
            f"✅ 模式已更新: `{sandbox}` + `{approval}`", target=target
        )

    async def handle_interaction(self, data: dict[str, Any]) -> None:
        interaction_id = data.get("id")
        if not interaction_id:
            return
        try:
            await self.qq.acknowledge_interaction(str(interaction_id))
        except Exception as exc:
            logger.warning("QQ interaction ACK failed: %s", exc)
        openid, button_data = interaction_operator_and_button(data)
        if not await self.bind_or_authorize(openid):
            logger.warning("Ignored interaction from unauthorized QQ user")
            return
        if not button_data.startswith("codex-approve:"):
            return
        parts = button_data.split(":", 2)
        if len(parts) != 3 or not await self.resolve_approval(parts[1], parts[2]):
            await self._send_reply(
                "⚠️ 该审批已失效或已经处理。", target=ReplyTarget("c2c", openid)
            )
            return
        await self._send_reply(
            "✅ 审批决定已提交给 Codex。", target=ReplyTarget("c2c", openid)
        )


async def send_identify(ws: Any, qq: QQApi) -> None:
    token = await qq.token()
    await ws.send_json(
        {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": API_INTENTS,
                "shard": [0, 1],
                "properties": {
                    "$os": sys.platform,
                    "$browser": "codex-qq-bridge",
                    "$device": "codex-qq-bridge",
                },
            },
        }
    )


async def heartbeat_sender(ws: Any, interval: float, state: dict[str, Any]) -> None:
    try:
        while not ws.closed:
            await asyncio.sleep(interval)
            await ws.send_json({"op": 1, "d": state.get("seq")})
    except asyncio.CancelledError:
        return


async def event_loop(ws: Any, bridge: CodexQQBridge) -> None:
    from aiohttp import WSMsgType

    state: dict[str, Any] = {"seq": None}
    heartbeat: asyncio.Task[None] | None = None
    def track_handler(coro: Any, label: str) -> None:
        task = asyncio.create_task(coro, name=label)
        bridge._qq_tasks.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            bridge._qq_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error:
                logger.error("QQ event handler %s failed: %s", label, error)

        task.add_done_callback(done)
    try:
        while not ws.closed:
            message = await ws.receive()
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload.get("s"), int):
                    state["seq"] = payload["s"]
                if payload.get("op") == 10:
                    if heartbeat:
                        heartbeat.cancel()
                    interval_ms = (payload.get("d") or {}).get("heartbeat_interval", 30000)
                    heartbeat = asyncio.create_task(
                        heartbeat_sender(ws, float(interval_ms) / 1000 * 0.8, state)
                    )
                    await send_identify(ws, bridge.qq)
                    continue
                if payload.get("op") in {7, 9}:
                    logger.warning("QQ gateway requested reconnect (op=%s)", payload.get("op"))
                    return
                if payload.get("op") == 0:
                    event_type = payload.get("t")
                    data = payload.get("d") or {}
                    if event_type == "C2C_MESSAGE_CREATE":
                        track_handler(bridge.handle_c2c_message(data), "qq-c2c-message")
                    elif event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
                        track_handler(bridge.handle_group_message(data), "qq-group-message")
                    elif event_type == "INTERACTION_CREATE":
                        track_handler(bridge.handle_interaction(data), "qq-interaction")
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                return
            elif message.type == WSMsgType.ERROR:
                raise RuntimeError(f"QQ WebSocket error: {ws.exception()}")
    finally:
        if heartbeat:
            heartbeat.cancel()


def detect_proxy_config() -> tuple[bool, str]:
    proxy = next(
        (
            value
            for value in (
                os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
                os.environ.get("HTTP_PROXY"), os.environ.get("http_proxy"),
                os.environ.get("ALL_PROXY"), os.environ.get("all_proxy"),
            )
            if value
        ),
        None,
    )
    if not proxy:
        return True, "QQ gateway 使用直连"
    scheme = proxy.split("://", 1)[0].lower()
    masked = proxy.rsplit("@", 1)[-1]
    display = masked if "://" in masked else f"{scheme}://{masked}"
    if scheme.startswith("socks"):
        return False, f"SOCKS 代理 {display} 不受 aiohttp 内置支持，将直连"
    return True, f"QQ gateway 使用代理 {display}"


async def main() -> None:
    configure_logging()
    bridge = CodexQQBridge()
    await bridge.start()
    import aiohttp

    trust_env, proxy_note = detect_proxy_config()
    logger.info(proxy_note)
    retry = 0
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    installed_signals: list[signal.Signals] = []
    if main_task:
        for handled_signal in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(handled_signal, main_task.cancel)
                installed_signals.append(handled_signal)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        while True:
            try:
                gateway_url = await bridge.qq.gateway_url()
                async with aiohttp.ClientSession(trust_env=trust_env) as session:
                    async with session.ws_connect(
                        gateway_url,
                        timeout=aiohttp.ClientTimeout(sock_connect=20),
                    ) as ws:
                        retry = 0
                        logger.info("QQ gateway connected")
                        await event_loop(ws, bridge)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = RECONNECT_BACKOFF[min(retry, len(RECONNECT_BACKOFF) - 1)]
                retry += 1
                logger.error("QQ gateway disconnected: %s; retrying in %ss", exc, delay)
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        logger.info("Bridge shutdown requested")
    finally:
        for handled_signal in installed_signals:
            loop.remove_signal_handler(handled_signal)
        await bridge.close()


def _get_version() -> str:
    try:
        from importlib.metadata import version
        return version("codex-qq-bridge")
    except Exception:
        return "0.0.0"


def init_config() -> int:
    path = REPO_ROOT / ".env"
    if path.exists():
        print(f"配置文件已存在：{path}")
        return 1
    app_id = input("QQ Bot AppID: ").strip()
    client_secret = getpass.getpass("QQ Bot ClientSecret: ").strip()
    cwd = input(f"Codex 默认目录 [{DEFAULT_CWD}]: ").strip() or DEFAULT_CWD
    if not app_id or not client_secret or not Path(cwd).expanduser().is_dir():
        print("AppID、ClientSecret 或默认目录无效。")
        return 1
    path.write_text(
        "\n".join(
            [
                f"APP_ID={app_id}",
                f"CLIENT_SECRET={client_secret}",
                "MASTER_OPENID=",
                f"CODEX_CWD={Path(cwd).expanduser().resolve()}",
                "CODEX_SANDBOX=workspace-write",
                "CODEX_APPROVAL_POLICY=on-request",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    print(f"配置已写入 {path}；MASTER_OPENID 将绑定首位发消息的 QQ 用户。")
    return 0


def cli() -> int:
    if "--version" in sys.argv or "-V" in sys.argv:
        print(_get_version())
        return 0
    if "--init" in sys.argv:
        return init_config()
    if "--help" in sys.argv or "-h" in sys.argv:
        print("用法: codex-qq-bridge [--init|--version|--help]")
        return 0
    if not ENV_PATH:
        print("⚠️ 未找到 .env；请先运行 codex-qq-bridge --init")
        return 1
    if not APP_ID or not CLIENT_SECRET:
        print("⚠️ .env 缺少 APP_ID 或 CLIENT_SECRET")
        return 1
    if not shutil.which(CODEX_BIN):
        print(f"⚠️ 找不到 Codex CLI: {CODEX_BIN}")
        return 1
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    return 0
