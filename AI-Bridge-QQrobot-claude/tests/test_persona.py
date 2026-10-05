"""Role isolation and shared delivery progress regressions (no live QQ/model calls)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/codex-qq-bridge/src"))
from codex_qq_bridge import persona, x_monitor
from codex_qq_bridge.bridge import BridgeState, CodexQQBridge, ReplyTarget
from codex_qq_bridge.app_server import AppServerError
from test_codex_bridge import FakeQQ, FakeApp
from test_x_monitor import make_post


def make_bridge(tmp_path):
    return CodexQQBridge(qq=FakeQQ(), app_server=FakeApp(), state_file=tmp_path / "state.json", master_openid="owner")


@pytest.mark.parametrize("role,name,birthday", [("rui", "八潮瑠唯", "11-19"), ("yuno", "千石由乃", "11-04")])
def test_profiles_include_facts_and_shared_learner_policy(role, name, birthday):
    loaded = persona.load_persona(role)
    assert name in loaded.instructions and birthday in loaded.instructions
    assert "owner" in loaded.instructions and "角色后缀" in loaded.instructions
    assert '"provenance":' not in loaded.instructions
    assert persona.load_persona("default") is None


def test_private_switch_round_trip_and_restart(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        bridge.app.thread_start_ids = ["private-default", "private-rui", "private-yuno"]
        await bridge._new_thread()
        target = ReplyTarget("c2c", "owner")
        await bridge._handle_command("/role_switch_rui", target)
        rui_options = bridge.app.requests[-1][1]
        assert "八潮瑠唯" in rui_options["developerInstructions"]
        await bridge._handle_command("/role_switch_yuno", target)
        assert bridge.state.thread_id == "private-yuno"
        await bridge._handle_command("/role_switch_rui", target)
        assert bridge.state.thread_id == "private-rui"
        assert bridge.app.requests[-1][0] == "thread/resume"
        restored = make_bridge(tmp_path)
        assert restored.state.active_role == "rui"
        assert restored.state.thread_id == "private-rui"
        await restored._switch_role("default", target)
        assert restored.state.thread_id == "private-default"
        assert "developerInstructions" not in restored.app.requests[-1][1]
        # Owner history can never cross persona/session ownership.
        assert not restored._can_resume_thread("private-yuno")
        await restored._switch_role("yuno", target)
        assert restored._can_resume_thread("private-yuno")
        assert not restored._can_resume_thread("private-default")
    asyncio.run(run())


def test_group_scope_shared_progress_and_private_independence(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        bridge.app.thread_start_ids = ["group-a-default", "group-b-default", "group-a-rui", "group-a-yuno", "private-rui"]
        await bridge._ensure_group_session("a")
        await bridge._ensure_group_session("b")
        shared = {"last_lesson_date": "2026-10-01", "last_lesson_path": "unchanged.md", "lesson_delivery": {"date": "2026-10-02", "sent_sections": 2}}
        bridge.state.groups["a"].update(shared)
        learner = bridge.state.groups["a"]["learner_id"]
        target = ReplyTarget("group", "a", member_openid="member-1")
        await bridge._handle_command("/role_switch_rui", target)
        options = bridge.app.requests[-1][1]
        assert "八潮瑠唯" in options["developerInstructions"]
        assert "publish_daily_lesson" in str(options["dynamicTools"])
        # Another member switches the same group selection.
        await bridge._handle_command("/role_switch_yuno", ReplyTarget("group", "a", member_openid="member-2"))
        await bridge._handle_command("/role_switch_rui", target)
        entry = bridge.state.groups["a"]
        assert entry["thread_id"] == "group-a-rui" and entry["learner_id"] == learner
        assert all(entry[k] == value for k, value in shared.items())
        assert bridge.state.groups["b"]["thread_id"] == "group-b-default"
        assert bridge.state.groups["b"]["active_role"] == "default"
        assert bridge.state.active_role == "default"
        await bridge._switch_role("rui", ReplyTarget("c2c", "owner"))
        assert bridge.state.thread_id == "private-rui"
        assert entry["thread_id"] == "group-a-rui"
        assert "group-a-yuno" not in bridge.thread_to_group
        before = len(bridge.qq.group_messages)
        await bridge.handle_codex_notification("item/completed", {"threadId": "group-a-yuno", "item": {"id": "late-item", "type": "agentMessage", "text": "late response"}})
        assert len(bridge.qq.group_messages) == before
        restarted = make_bridge(tmp_path)
        await restarted._ensure_group_session("a")
        assert restarted.group_runtimes["a"].thread_id == "group-a-rui"
        assert "八潮瑠唯" in restarted.app.requests[-1][1]["developerInstructions"]
    asyncio.run(run())


def test_switch_failure_busy_and_unknown_commands_do_not_change_role(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        await bridge._new_thread()
        before = bridge.state.thread_id
        target = ReplyTarget("c2c", "owner")
        bridge.active_turn_id = "running"
        await bridge._handle_command("/role_switch_rui", target)
        assert bridge.state.active_role == "default" and bridge.state.thread_id == before
        bridge.active_turn_id = None
        with patch.object(bridge.app, "request", side_effect=AppServerError("unavailable")):
            with pytest.raises(AppServerError):
                await bridge._switch_role("rui", target)
        assert bridge.state.active_role == "default" and bridge.state.thread_id == before
        request_count = len(bridge.app.requests)
        assert await bridge._handle_command("/role_switch_missing", target)
        assert len(bridge.app.requests) == request_count
    asyncio.run(run())


def test_legacy_state_migrates_to_default_without_resetting_progress(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"thread_id": "owner-legacy", "groups": {"g": {"thread_id": "group-legacy", "last_lesson_date": "2026-09-30", "learner_id": "group_old", "toolset_version": 3}}}))
    state = BridgeState.load(path)
    state.save(path)
    assert state.role_sessions["default"]["thread_id"] == "owner-legacy"
    entry = state.groups["g"]
    assert entry["role_sessions"]["default"]["thread_id"] == "group-legacy"
    assert entry["last_lesson_date"] == "2026-09-30" and entry["learner_id"] == "group_old"


def test_narration_cache_invalidates_config_and_content_and_preserves_material(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_PERSONA_CACHE_DIR", str(tmp_path / "cache"))
    generated = AsyncMock(return_value=["先确认这个表达的用法。"])
    monkeypatch.setattr(persona, "_generate_leadins", generated)
    blocks = ["## 今日语法\n\n日本語。\n接续：名词 + です。\nSource: local"]
    async def run():
        first = await persona.narrate_blocks("rui", "lesson", blocks, state_file=tmp_path / "state")
        again = await persona.narrate_blocks("rui", "lesson", blocks, state_file=tmp_path / "state")
        assert first == again
        assert generated.await_count == 1
        assert first[0].startswith("## 今日语法\n")
        assert blocks[0].partition("\n")[2] in first[0]
        await persona.narrate_blocks("rui", "lesson", [blocks[0] + "\n新的知识"], state_file=tmp_path / "state")
        await persona.narrate_blocks("yuno", "lesson", blocks, state_file=tmp_path / "state")
        assert generated.await_count == 3
        root = tmp_path / "profiles"
        (root / "yashio-rui").mkdir(parents=True)
        original = persona.DEFAULT_ROOT / "yashio-rui/persona.yaml"
        (root / "yashio-rui/persona.yaml").write_text(original.read_text().replace('revision: "0.2.0"', 'revision: "0.2.1"'))
        monkeypatch.setenv("CODEX_PERSONA_ROOT", str(root))
        await persona.narrate_blocks("rui", "lesson", blocks, state_file=tmp_path / "state")
        assert generated.await_count == 4
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / "cache").iterdir())
    asyncio.run(run())


def test_narration_failure_uses_cached_authored_fallback(tmp_path, monkeypatch):
    generated = AsyncMock(side_effect=RuntimeError("model down"))
    monkeypatch.setattr(persona, "_generate_leadins", generated)
    async def run():
        material = ["【日语原文】\nテスト\n【中文译文】\n测试\n来源"]
        first = await persona.narrate_blocks("yuno", "x", material, state_file=tmp_path / "state")
        again = await persona.narrate_blocks("yuno", "x", material, state_file=tmp_path / "state")
        assert first == again and material[0] in first[0]
        assert generated.await_count == 1
    asyncio.run(run())


def test_daily_delivery_pins_original_role_and_resumes_without_duplicate(tmp_path, monkeypatch):
    async def render(role, _purpose, blocks, **_kwargs):
        return [block.split("\n", 1)[0] + f"\n{role}讲述\n" + block.split("\n", 1)[1] for block in blocks]
    monkeypatch.setattr("codex_qq_bridge.bridge.narrate_blocks", render)
    async def run():
        bridge = make_bridge(tmp_path)
        bridge.daily_message_interval = 0
        bridge.app.thread_start_ids = ["default-g", "rui-g", "yuno-g"]
        await bridge._ensure_group_session("g")
        await bridge._switch_role("rui", ReplyTarget("group", "g"))
        lesson = tmp_path / "lesson.md"
        headings = ["今日复习", "今日表达", "今日语法", "今日单词", "小练习", "Source"]
        lesson.write_text("\n\n".join(f"## {h}\n\n{h}完整内容" for h in headings))
        bridge._generate_group_lesson = AsyncMock(return_value=lesson)
        bridge._mark_group_lesson_published = AsyncMock()
        sent = []
        fail = True
        async def send(_group, content):
            if fail and len(sent) == 2:
                return False
            sent.append(content)
            return True
        bridge._send_group_bubble = send
        assert not await bridge.publish_group_daily_lesson("g", "2026-10-01", resume_delivery=True)
        assert bridge.state.groups["g"]["lesson_delivery"]["sent_sections"] == 2
        await bridge._switch_role("yuno", ReplyTarget("group", "g"))
        fail = False
        assert await bridge.publish_group_daily_lesson("g", "2026-10-01", resume_delivery=True)
        assert len(sent) == 6 and all("rui讲述" in item for item in sent)
        assert "lesson_delivery" not in bridge.state.groups["g"]
        assert bridge.state.groups["g"]["last_lesson_date"] == "2026-10-01"
        bridge._mark_group_lesson_published.assert_awaited_once()
    asyncio.run(run())


def test_x_retry_pins_chunks_across_role_change_and_complete_is_not_resent(tmp_path, monkeypatch):
    monkeypatch.delenv("X_MONITOR_GROUP_OPENIDS", raising=False)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"groups": {"g": {"active": True, "active_role": "rui"}}}))
    store = x_monitor.MonitorStore(tmp_path / "monitor.db")
    post = make_post()
    store.observe(post)
    analysis = x_monitor.PostAnalysis("测试帖子", "", ())
    sent = []
    fail = True
    class QQ:
        async def send_group_text(self, group, content):
            if fail and sent:
                return False
            sent.append(content)
            return True
        async def close(self):
            pass
    async def render(role, _purpose, blocks, **_kwargs):
        return [f"{role}讲述\n" + blocks[0]]
    monkeypatch.setattr(x_monitor, "build_qq_client", QQ)
    monkeypatch.setattr(x_monitor, "narrate_blocks", render)
    monkeypatch.setattr(x_monitor, "download_post_images", AsyncMock(return_value=[]))
    # Force multiple chunks without relying on the current QQ limit.
    monkeypatch.setattr(x_monitor, "split_group_message", lambda value: [value[:6], value[6:]])
    async def run():
        with pytest.raises(RuntimeError):
            await x_monitor.publish_learning_post(post, analysis, store, state_file=state)
        original = store.load_group_chunks(post, "g")
        assert sent == original[:1]
        state.write_text(json.dumps({"groups": {"g": {"active": True, "active_role": "yuno"}}}))
        nonlocal fail
        fail = False
        assert await x_monitor.publish_learning_post(post, analysis, store, state_file=state) == 1
        assert sent == original
        await x_monitor.publish_learning_post(post, analysis, store, state_file=state)
        assert sent == original
    asyncio.run(run())


def test_long_lesson_section_resumes_after_last_sent_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_qq_bridge.bridge.QQ_TEXT_SAFE_LIMIT", 64)
    async def run():
        bridge = make_bridge(tmp_path)
        bridge.daily_message_interval = 0
        await bridge._ensure_group_session("g")
        lesson = tmp_path / "lesson.md"
        headings = ["今日复习", "今日表达", "今日语法", "今日单词", "小练习", "Source"]
        lesson.write_text("\n\n".join(f"## {h}\n\n" + h * 60 for h in headings))
        bridge._generate_group_lesson = AsyncMock(return_value=lesson)
        bridge._mark_group_lesson_published = AsyncMock()
        original_send = bridge.qq.send_group_text
        calls = 0
        fail = True
        async def send(group, text, **kwargs):
            nonlocal calls
            calls += 1
            if fail and calls == 2:
                return False
            return await original_send(group, text, **kwargs)
        bridge.qq.send_group_text = send
        assert not await bridge.publish_group_daily_lesson("g", "2026-10-01", resume_delivery=True)
        entry = bridge.state.groups["g"]
        assert entry["lesson_delivery"]["sent_sections"] == 0
        assert entry["lesson_delivery"]["sent_section_chunks"] == 1
        sections = entry["lesson_delivery"]["sections"]
        expected = []
        for index, section in enumerate(sections):
            if index == 0:
                section = "# Daily Japanese Lesson · 2026-10-01\n\n" + section
            expected.extend(section[i:i+64] for i in range(0, len(section), 64))
        fail = False
        assert await bridge.publish_group_daily_lesson("g", "2026-10-01", resume_delivery=True)
        assert [text for _, text, _ in bridge.qq.group_messages] == expected
    asyncio.run(run())


def test_legacy_x_partial_delivery_keeps_neutral_layout(tmp_path, monkeypatch):
    monkeypatch.delenv("X_MONITOR_GROUP_OPENIDS", raising=False)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"groups": {"g": {"active": True, "active_role": "yuno"}}}))
    store = x_monitor.MonitorStore(tmp_path / "monitor.db")
    post = make_post()
    analysis = x_monitor.PostAnalysis("测试帖子", "", ())
    store.observe(post)
    store.ensure_group_delivery(post, "g")
    store.record_group_progress(post, "g", 1, complete=False)
    default = x_monitor.format_learning_message(post, analysis, timezone_name="Asia/Shanghai")
    chunks = [default[:10], default[10:]]
    monkeypatch.setattr(x_monitor, "split_group_message", lambda _value: chunks)
    generator = AsyncMock(side_effect=AssertionError("legacy partials must not be reformatted"))
    monkeypatch.setattr(x_monitor, "narrate_blocks", generator)
    monkeypatch.setattr(x_monitor, "download_post_images", AsyncMock(return_value=[]))
    sent = []
    class QQ:
        async def send_group_text(self, _group, content):
            sent.append(content)
            return True
        async def close(self):
            pass
    monkeypatch.setattr(x_monitor, "build_qq_client", QQ)
    asyncio.run(x_monitor.publish_learning_post(post, analysis, store, state_file=state))
    assert sent == chunks[1:]
    assert store.load_group_chunks(post, "g") == chunks
    generator.assert_not_awaited()


def test_btw_fork_receives_current_role_instructions(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        await bridge._switch_role("yuno", ReplyTarget("c2c", "owner"))
        original = bridge.app.request
        async def request(method, params, **kwargs):
            if method == "thread/fork":
                bridge.app.requests.append((method, params))
                return {"thread": {"id": "side-yuno"}}
            if method == "turn/start" and params["threadId"] == "side-yuno":
                # Simulate model completion during the RPC's wait.
                await bridge.handle_codex_notification("item/completed", {"threadId": "side-yuno", "item": {"id": "side-answer", "type": "agentMessage", "text": "回答"}})
                await bridge.handle_codex_notification("turn/completed", {"threadId": "side-yuno", "turn": {"id": "side-turn", "status": "completed"}})
                return {"turn": {"id": "side-turn"}}
            return await original(method, params, **kwargs)
        bridge.app.request = request
        assert await bridge._ask_side_question("问题") == "回答"
        options = next(params for method, params in bridge.app.requests if method == "thread/fork")
        assert "千石由乃" in options["developerInstructions"]
    asyncio.run(run())
