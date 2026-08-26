"""Async JSON-RPC client for the local Codex App Server.

The bridge owns one ``codex app-server`` subprocess and communicates over its
newline-delimited stdio transport.  This keeps QQ conversations attached to an
explicit Codex thread instead of guessing which rollout JSONL file is newest.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any


NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[
    [str, dict[str, Any], int | str], Awaitable[dict[str, Any]] | dict[str, Any]
]


class AppServerError(RuntimeError):
    """Raised when the Codex App Server returns or encounters an error."""


class AppServerClient:
    """Small, dependency-free client for ``codex app-server --stdio``."""

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        logger: logging.Logger | None = None,
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.logger = logger or logging.getLogger(__name__)
        self.notification_handler = notification_handler
        self.server_request_handler = server_request_handler
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._notification_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = (
            asyncio.Queue()
        )
        self._server_request_tasks: set[asyncio.Task[None]] = set()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        """Start App Server and complete its required initialize handshake."""
        if self.is_running:
            return
        if self.process is not None:
            await self.stop()
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        self.process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(
            self._read_stdout(), name="codex-app-server-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name="codex-app-server-stderr"
        )
        self._notification_task = asyncio.create_task(
            self._consume_notifications(), name="codex-app-server-events"
        )
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_qq_bridge",
                        "title": "Codex QQ Bridge",
                        "version": "0.1.0",
                    }
                },
            )
            await self.notify("initialized", {})
        except Exception:
            await self.stop()
            raise
        self.logger.info("Codex App Server initialized (pid=%s)", self.process.pid)

    async def stop(self) -> None:
        """Stop the subprocess and fail outstanding requests."""
        process = self.process
        self.process = None
        tasks = [
            task
            for task in (
                *tuple(self._server_request_tasks),
                self._reader_task,
                self._stderr_task,
                self._notification_task,
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        self._server_request_tasks.clear()
        self._reader_task = None
        self._stderr_task = None
        self._notification_task = None
        error = AppServerError("Codex App Server stopped")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._notification_queue = asyncio.Queue()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30
    ) -> dict[str, Any]:
        """Send a client request and wait for its JSON-RPC result."""
        if not self.is_running or not self.process:
            raise AppServerError("Codex App Server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AppServerError(f"Timed out waiting for {method}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        await self._send({"id": request_id, "result": result})

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if not process or process.returncode is not None or not process.stdin:
            raise AppServerError("Codex App Server stdin is unavailable")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    self.logger.warning("Invalid App Server JSON: %r", raw[:200])
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.logger.exception("Codex App Server stdout reader failed: %s", exc)
        finally:
            if self.process is process and process.returncode is not None:
                self.logger.error("Codex App Server exited with code %s", process.returncode)
            error = AppServerError("Codex App Server connection closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self.logger.info("[app-server] %s", line)
        except asyncio.CancelledError:
            return

    async def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if method and request_id is not None:
            task = asyncio.create_task(
                self._handle_server_request(str(method), message.get("params") or {}, request_id)
            )
            self._server_request_tasks.add(task)
            task.add_done_callback(self._server_request_tasks.discard)
            return
        if request_id is not None:
            future = self._pending.get(request_id)
            if not future or future.done():
                return
            if "error" in message:
                error = message.get("error") or {}
                future.set_exception(
                    AppServerError(
                        f"App Server request failed: {error.get('message', error)}"
                    )
                )
            else:
                future.set_result(message.get("result") or {})
            return
        if method:
            await self._notification_queue.put((str(method), message.get("params") or {}))

    async def _handle_server_request(
        self, method: str, params: dict[str, Any], request_id: int | str
    ) -> None:
        try:
            if self.server_request_handler is None:
                result = self._safe_default_server_response(method)
            else:
                result = self.server_request_handler(method, params, request_id)
                if inspect.isawaitable(result):
                    result = await result
            await self.respond(request_id, result)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.logger.exception("Server request handler failed for %s: %s", method, exc)
            try:
                await self.respond(request_id, self._safe_default_server_response(method))
            except Exception:
                pass

    @staticmethod
    def _safe_default_server_response(method: str) -> dict[str, Any]:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }:
            return {"decision": "decline"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline", "content": None}
        return {}

    async def _consume_notifications(self) -> None:
        try:
            while True:
                method, params = await self._notification_queue.get()
                try:
                    if self.notification_handler:
                        result = self.notification_handler(method, params)
                        if inspect.isawaitable(result):
                            await result
                except Exception as exc:
                    self.logger.exception(
                        "Notification handler failed for %s: %s", method, exc
                    )
                finally:
                    self._notification_queue.task_done()
        except asyncio.CancelledError:
            return
