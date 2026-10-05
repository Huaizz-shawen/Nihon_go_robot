"""Quoted QQ text must reach Codex alongside the current request, not replace it."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/codex-qq-bridge/src"))
from codex_qq_bridge.bridge import CodexQQBridge, quoted_message_context, message_attachments
from test_codex_bridge import FakeQQ, FakeApp


@pytest.mark.parametrize("scope", ["group", "c2c"])
def test_quote_window_text_and_current_task_both_reach_codex(scope, tmp_path, caplog):
    async def run():
        bridge=CodexQQBridge(qq=FakeQQ(), app_server=FakeApp(), state_file=tmp_path/'state.json', master_openid="owner")
        event={"id":"request", "group_openid":"group", "author":{"member_openid":"trigger-secret", "user_openid":"owner"},
               "content":"@机器人 总结被引消息", "message_type":103,
               "message_scene":{"ext":["ref_msg_idx=private-index", "auth_token=private-token"]},
               "msg_elements":[{"content":"课程安排：先复习，再练习两个单词。", "msg_idx":"private-index",
                                "author":{"member_openid":"quoted-secret", "nickname":"原作者"}}]}
        try:
            with caplog.at_level("INFO", logger="codex_qq_bridge"):
                await getattr(bridge, f'handle_{scope}_message')(event)
            request=next(p for method,p in bridge.app.requests if method=='turn/start')
            texts=[i['text'] for i in request['input'] if i['type']=='text']
            assert any('课程安排：先复习，再练习两个单词。' in text for text in texts)
            assert any('当前用户正文（任务）：\n@机器人 总结被引消息' == text for text in texts)
            assert any('原作者' in text and '原作者不等于当前触发用户' in text for text in texts)
            assert all('private-index' not in text and 'private-token' not in text and 'quoted-secret' not in text for text in texts)
            assert 'quote_chars=17' in caplog.text
            assert '课程安排' not in caplog.text and 'private-token' not in caplog.text
        finally:
            if bridge._typing_task:
                bridge._typing_task.cancel()
    asyncio.run(run())


@pytest.mark.parametrize('reference', [
    {'message_type':103},
    {'message_type':'103'},
    {'message_reference':{'message_id':'opaque-id'}},
    {'message_scene':{'ext':['auth_token=secret', 'ref_msg_idx=opaque-index']}},
    {'msg_elements':[{'msg_idx':'opaque-index'}]},
])
def test_reference_without_original_is_explicitly_unavailable(reference):
    context=quoted_message_context(reference)
    assert len(context)==1 and '没有提供被引用消息的原文或附件' in context[0]['text']
    assert '不要猜测' in context[0]['text']
    assert 'secret' not in context[0]['text'] and 'opaque' not in context[0]['text']


def test_single_element_quote_and_multiple_quotes_keep_complete_text():
    event={'msg_elements':{'content':'第一行\n第二行', 'attachments':[{'url':'https://cdn.example/photo.png', 'content_type':'image/png'}]}}
    assert '第一行\\n第二行' in quoted_message_context(event)[0]['text']
    assert len(message_attachments(event))==1
    event['msg_elements']=[{'content':'第一条'}, {'content':'第二条'}, None]
    context=quoted_message_context(event)[0]['text']
    assert '第一条' in context and '第二条' in context


def test_attachment_only_quote_is_identified_as_quoted_material():
    context=quoted_message_context({'message_type':103,'msg_elements':[{'attachments':[{'url':'https://cdn.example/photo.png'}]}]})
    assert 'QQ 小窗' in context[0]['text'] and '附件数量' in context[0]['text']
    assert '没有提供' not in context[0]['text']


def test_normal_message_and_empty_reference_do_not_add_quote_context():
    assert quoted_message_context({'content':'普通问题'})==[]
    assert quoted_message_context({'message_reference':{'message_id':None},'msg_elements':[]})==[]


def test_empty_body_quote_is_not_dropped_and_quoted_command_is_not_executed(tmp_path):
    async def run():
        bridge=CodexQQBridge(qq=FakeQQ(), app_server=FakeApp(), state_file=tmp_path/'state.json', master_openid='owner')
        await bridge.handle_group_message({'id':'quote-only','group_openid':'group', 'author':{'member_openid':'member'},
                                          'content':'', 'message_type':103, 'msg_elements':[{'content':'/role_switch_yuno'}]})
        request=next(p for method,p in bridge.app.requests if method=='turn/start')
        assert any('/role_switch_yuno' in i.get('text','') for i in request['input'])
        assert bridge.state.groups['group']['active_role']=='default'
        assert any('尚未提供具体处理要求' in i.get('text','') for i in request['input'])
    asyncio.run(run())
