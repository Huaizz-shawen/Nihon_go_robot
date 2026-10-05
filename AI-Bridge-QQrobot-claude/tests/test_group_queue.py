"""Group requests must wait for whole turns without steering other members."""
import asyncio

import pytest

from test_codex_bridge import FakeApp, FakeQQ
from codex_qq_bridge.app_server import AppServerError
from codex_qq_bridge.bridge import CodexQQBridge


class QueueApp(FakeApp):
    def __init__(self):
        super().__init__()
        self.thread_start_ids = ["group-thread-a", "group-thread-b"]
        self.turn_count = 0

    async def request(self, method, params, **kwargs):
        if method == "turn/start":
            self.requests.append((method, params))
            self.turn_count += 1
            return {"turn": {"id": f"turn-{self.turn_count}"}}
        return await super().request(method, params, **kwargs)


@pytest.fixture
def bridge(tmp_path):
    qq, app = FakeQQ(), QueueApp()
    bridge = CodexQQBridge(qq=qq, app_server=app, state_file=tmp_path / "state.json",
                           env_path=None, master_openid="owner")
    bridge.state.thread_id = "private-thread"
    return bridge


def message(number, group="group-a"):
    return {"id": f"msg-{number}", "group_openid": group,
            "content": f"处理任务 {number}",
            "author": {"member_openid": f"member-{number}", "nickname": f"成员{number}"}}


def starts(bridge):
    return [params for method, params in bridge.app.requests if method == "turn/start"]


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), timeout=2)


async def complete(bridge, turn_id, *, group="group-a", status="completed"):
    await bridge.handle_codex_notification("turn/completed", {
        "threadId": bridge.group_runtimes[group].thread_id,
        "turn": {"id": turn_id, "status": status, "error": {"message": "测试失败"}},
    })


async def output(bridge, item_id, text):
    await bridge.handle_codex_notification("item/completed", {
        "threadId": bridge.group_runtimes["group-a"].thread_id,
        "item": {"id": item_id, "type": "agentMessage", "text": text},
    })


def test_three_members_wait_fifo_and_keep_original_reply_recipient(bridge):
    async def run():
        await bridge.handle_group_message(message(1))
        second = asyncio.create_task(bridge.handle_group_message(message(2)))
        await until(lambda: any("排队" in text for _, text, _ in bridge.qq.group_messages))
        third = asyncio.create_task(bridge.handle_group_message(message(3)))
        await output(bridge, "working", "还在工作")
        assert len(starts(bridge)) == 1
        assert bridge.group_runtimes["group-a"].active_reply_target.member_openid == "member-1"
        assert any(text == "@成员1 还在工作" for _, text, _ in bridge.qq.group_messages)
        assert not second.done() and not third.done()

        await output(bridge, "answer-1", "任务一完成")
        await complete(bridge, "turn-1")
        await asyncio.wait_for(second, 2)
        await until(lambda: sum("排队" in text for _, text, _ in bridge.qq.group_messages) == 2)
        assert len(starts(bridge)) == 2 and not third.done()
        await output(bridge, "answer-2", "任务二完成")
        # A late duplicate of turn 1 cannot release turn 2.
        await complete(bridge, "turn-1")
        assert bridge.group_runtimes["group-a"].active_turn_id == "turn-2"
        assert not third.done()
        await complete(bridge, "turn-2")
        await asyncio.wait_for(third, 2)
        await output(bridge, "answer-3", "任务三完成")
        await complete(bridge, "turn-3")
        assert [p["clientUserMessageId"] for p in starts(bridge)] == ["msg-1", "msg-2", "msg-3"]
        assert all(method not in {"turn/steer", "turn/interrupt"} for method, _ in bridge.app.requests)
        for n in range(1, 4):
            answers = [text for _, text, msg_id in bridge.qq.group_messages
                       if msg_id == f"msg-{n}" and "排队" not in text and "还在工作" not in text]
            assert len(answers) == 1 and answers[0].startswith(f"@成员{n} ")
    asyncio.run(run())


def test_waiting_group_does_not_block_another_group(bridge):
    async def run():
        await bridge.handle_group_message(message(1))
        waiting = asyncio.create_task(bridge.handle_group_message(message(2)))
        await until(lambda: len(bridge.qq.group_messages) == 1)
        await asyncio.wait_for(bridge.handle_group_message(message(3, "group-b")), 2)
        assert [p["clientUserMessageId"] for p in starts(bridge)] == ["msg-1", "msg-3"]
        assert not waiting.done()
        await complete(bridge, "turn-1")
        await asyncio.wait_for(waiting, 2)
        assert starts(bridge)[-1]["clientUserMessageId"] == "msg-2"
    asyncio.run(run())


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_failed_or_cancelled_turn_releases_next_request(bridge, status):
    async def run():
        await bridge.handle_group_message(message(1))
        waiting = asyncio.create_task(bridge.handle_group_message(message(2)))
        await until(lambda: bool(bridge.qq.group_messages))
        await complete(bridge, "turn-1", status=status)
        await asyncio.wait_for(waiting, 2)
        assert starts(bridge)[-1]["clientUserMessageId"] == "msg-2"
        if status == "failed":
            assert any(text.startswith("@成员1 ❌ Codex 任务失败")
                       for _, text, _ in bridge.qq.group_messages)
    asyncio.run(run())


def test_start_failure_does_not_leave_group_locked(bridge):
    original = bridge.app.request

    async def fail_once(method, params, **kwargs):
        if method == "turn/start":
            bridge.app.request = original
            raise AppServerError("start failed")
        return await original(method, params, **kwargs)

    bridge.app.request = fail_once

    async def run():
        await bridge.handle_group_message(message(1))
        await asyncio.wait_for(bridge.handle_group_message(message(2)), 2)
        assert starts(bridge)[-1]["clientUserMessageId"] == "msg-2"
        assert any(text.startswith("@成员1 ❌ 请求处理失败") for _, text, _ in bridge.qq.group_messages)
    asyncio.run(run())


def test_fast_completion_before_start_response_does_not_block_next(bridge):
    original = bridge.app.request

    async def finish_fast(method, params, **kwargs):
        result = await original(method, params, **kwargs)
        if method == "turn/start":
            await complete(bridge, result["turn"]["id"])
        return result

    bridge.app.request = finish_fast

    async def run():
        await bridge.handle_group_message(message(1))
        await asyncio.wait_for(bridge.handle_group_message(message(2)), 2)
        assert [p["clientUserMessageId"] for p in starts(bridge)] == ["msg-1", "msg-2"]
        assert bridge.group_runtimes["group-a"].active_turn_id is None
    asyncio.run(run())


def test_fast_failure_waits_for_original_error_delivery(bridge):
    async def run():
        error_started, allow_error = asyncio.Event(), asyncio.Event()
        original_send = bridge.qq.send_group_text
        original_request = bridge.app.request
        completion = None

        async def delayed_send(group, content, **kwargs):
            if "Codex 任务失败" in content:
                error_started.set()
                await allow_error.wait()
            return await original_send(group, content, **kwargs)

        async def fast_failure(method, params, **kwargs):
            nonlocal completion
            result = await original_request(method, params, **kwargs)
            if method == "turn/start" and result["turn"]["id"] == "turn-1":
                completion = asyncio.create_task(complete(bridge, "turn-1", status="failed"))
                await error_started.wait()
            return result

        bridge.qq.send_group_text = delayed_send
        bridge.app.request = fast_failure
        await bridge.handle_group_message(message(1))
        second = asyncio.create_task(bridge.handle_group_message(message(2)))
        # Queue notice waits behind the original error's output lock as well.
        await until(lambda: bridge._conversation_locks["group:group-a"].locked())
        assert len(starts(bridge)) == 1 and not second.done()
        assert bridge.group_runtimes["group-a"].active_reply_target.member_openid == "member-1"
        allow_error.set()
        await asyncio.wait_for(completion, 2)
        await asyncio.wait_for(second, 2)
        assert starts(bridge)[-1]["clientUserMessageId"] == "msg-2"
        assert any(text.startswith("@成员1 ❌ Codex 任务失败") for _, text, _ in bridge.qq.group_messages)
    asyncio.run(run())


def test_runtime_recovery_releases_waiters_and_resumes_group(bridge):
    async def run():
        await bridge.handle_group_message(message(1))
        waiting = asyncio.create_task(bridge.handle_group_message(message(2)))
        await until(lambda: bool(bridge.qq.group_messages))
        bridge.app.is_running = False
        await bridge.ensure_runtime()
        await asyncio.wait_for(waiting, 2)
        assert starts(bridge)[-1]["clientUserMessageId"] == "msg-2"
        assert any(method == "thread/resume" and params["threadId"] == "group-thread-a"
                   for method, params in bridge.app.requests)
    asyncio.run(run())


def test_cancelled_waiter_does_not_interrupt_original_turn(bridge):
    async def run():
        await bridge.handle_group_message(message(1))
        waiting = asyncio.create_task(bridge.handle_group_message(message(2)))
        await until(lambda: bool(bridge.qq.group_messages))
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert bridge.group_runtimes["group-a"].active_turn_id == "turn-1"
        third = asyncio.create_task(bridge.handle_group_message(message(3)))
        await complete(bridge, "turn-1")
        await asyncio.wait_for(third, 2)
        assert [p["clientUserMessageId"] for p in starts(bridge)] == ["msg-1", "msg-3"]
        assert not any(method == "turn/interrupt" for method, _ in bridge.app.requests)
    asyncio.run(run())
