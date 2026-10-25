"""评论身份、父目标与展示格式测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.foxzone.core.comment_tree import (
    build_threaded_view,
    comment_identity,
    opaque_id,
    reply_item_key,
    reply_target,
    resolve_comment,
)
from plugins.foxzone.core.llm import generators
from plugins.foxzone.core.llm.formatters import (
    format_batch_comment_items,
    format_feed_items_block,
    format_self_feed,
)
from plugins.foxzone.core.llm.generators import ContentService
from plugins.foxzone.core.llm.parsers import (
    parse_batch_reply_decisions,
    parse_feed_decisions,
)


def test_opaque_ids_keep_zero_and_identity_scopes_duplicate_child_ids() -> None:
    """零 ID 有效，重复子 ID 通过父评论和作者区分。"""
    assert opaque_id(0) == "0"
    first = {"comment_tid": 0, "parent_tid": 3, "qq_account": 10}
    second = {"comment_tid": "0", "parent_tid": "4", "qq_account": "10"}
    assert comment_identity(first) != comment_identity(second)


def test_reply_target_uses_only_top_level_parent_index() -> None:
    """跨层重复 tid 时子回复目标解析到父评论而非另一条子回复。"""
    top = {"comment_tid": "1", "qq_account": "100"}
    child_collision = {"comment_tid": "1", "parent_tid": "9", "qq_account": "200"}
    target = {"comment_tid": "9", "parent_tid": "1", "qq_account": "300"}
    assert reply_target(target, [top, child_collision, target]) == ("1", "100")
    assert reply_target(top, [top]) == ("1", "100")


def test_resolve_comment_requires_unique_exact_identity() -> None:
    """同 ID 评论不能按列表首条或 QQ 猜测匹配。"""
    comments = [
        {"comment_tid": "0", "parent_tid": "1", "qq_account": "10"},
        {"comment_tid": "0", "parent_tid": "2", "qq_account": "10"},
    ]
    assert resolve_comment(comments, "0") is None
    assert resolve_comment(
        comments, "0", parent_comment_id="2", comment_uin="10"
    ) is comments[1]


def test_threaded_view_highlights_identity_and_exposes_reference_fields() -> None:
    """展示父 ID/作者及身份引用，高亮只命中精确评论身份。"""
    comments = [
        {"comment_tid": "0", "qq_account": "10", "nickname": "A", "content": "top"},
        {"comment_tid": "0", "parent_tid": "0", "qq_account": "20", "nickname": "B", "content": "child"},
    ]
    view = build_threaded_view(
        comments, "999", highlight_identity=comment_identity(comments[1])
    )
    assert view.count(">>>") == 1
    assert "id=0" in view and "parent=0" in view and "author=20" in view
    assert reply_item_key({"host_qq": "3", "feed_id": "4", **comments[1]})


class _Template:
    """提供不依赖 PromptManager 的异步模板替身。"""

    def set(self, *_: Any) -> _Template:
        """接受模板变量并返回自身。"""
        return self

    async def build(self) -> str:
        """返回固定提示词。"""
        return "fake prompt"


def _content_service(response: str) -> ContentService:
    """创建使用模拟运行时与模型响应的内容服务。"""
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(
                multimodal_mode=False,
                comment_model_task="test",
                vision_model_task="test",
            )
        ),
        vision_cache=None,
        bot_qq=lambda: _resolved("999"),
    )
    service = ContentService(runtime)
    service._get_prompt_template = lambda _: _resolved(_Template())
    service._send_prompt = lambda *_args, **_kwargs: _resolved(response)
    service._append_recent_self_block = lambda prompt, _: _resolved(prompt)
    return service


async def _resolved(value: Any) -> Any:
    """返回异步测试替身结果。"""
    return value


@pytest.mark.asyncio
async def test_batch_model_index_binds_duplicate_comment_ids_to_input_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型按本批序号选择时，输出键绑定到确切 feed 和评论作者。"""
    monkeypatch.setattr(generators, "get_personality_desc", lambda: "persona")
    monkeypatch.setattr(generators, "get_now_info", lambda: ("now", "day"))
    items = [
        {"host_qq": "9", "feed_id": "feed-a", "comment_tid": "0", "qq_account": "7"},
        {"host_qq": "9", "feed_id": "feed-b", "comment_tid": "0", "qq_account": "7"},
    ]
    service = _content_service(json.dumps([{"comment_index": 2, "reply": "reply"}]))

    result = await service.generate_batch_replies(items)

    assert result == [
        {
            "target_key": reply_item_key(items[1]),
            "comment_tid": "0",
            "feed_id": "feed-b",
            "reply": "reply",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        '[{"comment_index":true,"reply":"x"}]',
        '[{"comment_index":0,"reply":"x"}]',
        '[{"comment_index":1,"reply":4}]',
        '[{"comment_index":1,"reply":"  "}]',
        '[{"comment_index":1,"reply":null},{"comment_index":1,"reply":"x"}]',
        '[{"comment_index":2,"reply":"x"}]',
        "[]",
    ],
)
async def test_batch_decision_rejects_invalid_duplicate_and_out_of_range_indices(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    """非法、重复、越界或空决策不映射到任何输入评论。"""
    monkeypatch.setattr(generators, "get_personality_desc", lambda: "persona")
    monkeypatch.setattr(generators, "get_now_info", lambda: ("now", "day"))
    parsed = parse_batch_reply_decisions(response)
    if '"comment_index":2' in response:
        assert len(parsed) == 1
    else:
        assert parsed == []
    service = _content_service(response)
    assert await service.generate_batch_replies(
        [{"host_qq": "9", "feed_id": "feed", "comment_tid": "0"}]
    ) == []


@pytest.mark.asyncio
async def test_feed_reply_index_returns_selected_comment_identity_without_qq_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """好友回复决策的序号精确映射到评论身份，不以作者 QQ 反查。"""
    monkeypatch.setattr(generators, "get_personality_desc", lambda: "persona")
    monkeypatch.setattr(generators, "get_now_info", lambda: ("now", "day"))
    comments = [
        {"comment_tid": "0", "qq_account": "7", "content": "first"},
        {"comment_tid": "0", "qq_account": "7", "content": "second"},
    ]
    service = _content_service(
        '[{"tid":"feed","target_qq":"7","like":false,"comment":null,'
        '"reply_comment_index":2,"reply_to":"answer"}]'
    )

    result = await service.generate_feed_decisions(
        [{"tid": "feed", "target_qq": "7", "comments": comments}]
    )

    assert result == [
        {
            "tid": "feed",
            "target_qq": "7",
            "like": False,
            "comment": None,
            "reply_to": "answer",
            "reply_comment_key": comment_identity(comments[1]),
        }
    ]
    assert "reply_to_qq" not in parse_feed_decisions(
        '[{"tid":"feed","target_qq":"7","reply_to_qq":"7",'
        '"reply_to":"answer"}]'
    )[0]


def test_formatters_keep_long_text_none_and_comment_visibility_counts() -> None:
    """说说与评论保留完整正文，None 安全，并区分可见数和服务端数。"""
    long_text = "正文" * 160
    feed_block = format_feed_items_block(
        [
            {
                "tid": "feed",
                "target_qq": "7",
                "content": long_text,
                "content_complete": False,
                "images": None,
                "comments": [
                    {
                        "comment_tid": "0",
                        "parent_tid": "1",
                        "qq_account": "8",
                        "parent_comment_uin": "9",
                        "content": long_text,
                    }
                ],
                "comment_count": 0,
            }
        ]
    )
    self_feed = format_self_feed(
        1,
        {
            "content": None,
            "rt_con": None,
            "content_complete": None,
            "images": None,
            "comments": None,
            "comment_count": 0,
        },
    )
    batch_block = format_batch_comment_items(
        [
            {
                "feed_content": None,
                "comment_content": None,
                "parent_content": None,
                "feed_images": None,
                "all_comments": None,
            }
        ],
        "999",
    )

    assert long_text in feed_block
    assert "parent_author=9" in feed_block
    assert "正文完整性：可能为折叠片段" in feed_block
    assert "服务端评论数=0" in feed_block and "当前可见扁平项=1" in feed_block
    assert "一级评论可能部分可见" in feed_block
    assert "（无正文）" in self_feed
    assert "正文完整性：未知（保留当前返回正文，不截断）" in self_feed
    assert "服务端评论数=0" in self_feed and "当前可见扁平项=0" in self_feed
    assert "None" not in batch_block
    assert "（无内容）" in batch_block
