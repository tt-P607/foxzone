"""回复跟踪状态兼容性与持久化测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from plugins.foxzone.autopilot.engine import BatchPolicy, BatchSendEngine
from plugins.foxzone.autopilot import friend_feeds
from plugins.foxzone.core.comment_tree import comment_identity
from plugins.foxzone.core.interaction_log import InteractionLog
from plugins.foxzone.core import reply_tracker
from plugins.foxzone.core.http.comments import QZoneUnknownResultError
from plugins.foxzone.core.reply_tracker import ReplyTrackerService
from plugins.foxzone.core.http import QZoneAPIClient
from plugins.foxzone.runtime import QZoneRuntime


SCOPE = {
    "account_qq": "100",
    "host_qq": "200",
    "parent_comment_id": "0",
    "parent_comment_uin": "300",
    "comment_uin": "400",
}


@pytest.mark.asyncio
async def test_batch_unknown_result_is_not_retried_or_marked_successful() -> None:
    """未知投递只发送一次，交由专用回调阻止后续自动重发。"""
    events: list[str] = []

    async def sender(item: dict[str, Any]) -> bool:
        events.append("send")
        raise QZoneUnknownResultError("accepted without an id")

    async def on_success(item: dict[str, Any]) -> None:
        events.append("success")

    async def on_finally(item: dict[str, Any]) -> None:
        events.append("finally")

    async def on_unknown(item: dict[str, Any]) -> None:
        events.append("pending")

    result = await BatchSendEngine(
        asyncio.Lock(), BatchPolicy(jitter_range=(0, 0), backoff_base=0)
    ).run(
        [{"comment_tid": "0"}],
        sender=sender,
        should_send=lambda item: True,
        label=lambda item: "comment",
        on_success=on_success,
        on_finally=on_finally,
        on_unknown=on_unknown,
    )
    assert events == ["send", "pending"]
    assert result.unknown == 1
    assert result.succeeded == 0


@pytest.mark.asyncio
async def test_tracker_loads_legacy_and_persists_full_scoped_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧记录保留为 legacy，新状态在完整作用域下独立存取。"""
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        reply_tracker.storage_api,
        "load_json",
        lambda *_: _resolved({"data": {"feed": {"old": 1.0}}}),
    )

    async def save_json(namespace: str, key: str, value: dict[str, Any]) -> None:
        saved.append(value.copy())

    monkeypatch.setattr(reply_tracker.storage_api, "save_json", save_json)
    tracker = ReplyTrackerService()
    await tracker.initialize()
    assert tracker.get_status("feed", "old") == "legacy"
    assert tracker.has_replied("feed", "old")
    assert tracker.is_blocked("feed", "old")

    await tracker.mark_pending("feed", "child", **SCOPE)
    assert tracker.get_status("feed", "child", **SCOPE) == "pending"
    assert tracker.is_blocked("feed", "child", **SCOPE)
    assert not tracker.has_replied("feed", "child")

    await tracker.mark_skipped("feed", "skipped", **SCOPE)
    assert tracker.get_status("feed", "skipped", **SCOPE) == "skipped"
    await tracker.mark_as_replied("feed", "done", **SCOPE)
    assert tracker.get_status("feed", "done", **SCOPE) == "replied"
    assert len(saved) == 3
    assert saved[-1]["data"] == {"feed": {"old": 1.0}}
    assert "scoped" in saved[-1]


@pytest.mark.asyncio
async def test_tracker_persistence_failure_propagates_but_blocks_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存储失败向上传播，但进程内仍阻止重复写入。"""
    monkeypatch.setattr(reply_tracker.storage_api, "load_json", lambda *_: _resolved(None))

    async def fail_save(*_: Any) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(reply_tracker.storage_api, "save_json", fail_save)
    tracker = ReplyTrackerService()
    await tracker.initialize()
    with pytest.raises(OSError, match="disk unavailable"):
        await tracker.mark_pending("feed", "child", **SCOPE)
    assert tracker.get_status("feed", "child", **SCOPE) == "pending"


@pytest.mark.asyncio
async def test_pending_ordinary_comments_are_account_and_feed_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通评论待核实状态不冒充成功，并按账号主人说说隔离。"""
    saved: list[dict[str, Any]] = []

    async def save_json(namespace: str, key: str, value: dict[str, Any]) -> None:
        saved.append(value)

    monkeypatch.setattr(reply_tracker.storage_api, "save_json", save_json)
    tracker = ReplyTrackerService()
    await tracker.mark_comment_pending("100", "200", "feed")
    assert tracker.is_comment_pending("100", "200", "feed")
    assert not tracker.is_comment_pending("101", "200", "feed")
    assert not tracker.is_comment_pending("100", "201", "feed")
    assert not tracker.has_replied("feed", "0")
    assert saved[0]["data"] == {}


async def _resolved(value: Any) -> Any:
    """提供异步存储加载替身结果。"""
    return value


@pytest.mark.asyncio
async def test_friend_reply_preserves_selected_thread_and_blocks_unknown_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同作者跨线程重复子 ID 时按模型选定身份写入，未知结果下轮不重发。"""
    comments = [
        {"comment_tid": "1", "qq_account": "200", "nickname": "A"},
        {"comment_tid": "2", "qq_account": "201", "nickname": "B"},
        {"comment_tid": "0", "parent_tid": "1", "parent_comment_uin": "200", "qq_account": "300", "nickname": "C"},
        {"comment_tid": "0", "parent_tid": "2", "parent_comment_uin": "201", "qq_account": "300", "nickname": "C"},
    ]
    client = SimpleNamespace(
        fetch_feed_detail=AsyncMock(return_value={"comments": comments}),
        reply=AsyncMock(side_effect=QZoneUnknownResultError("lost acknowledgement")),
    )

    async def with_client(callback: Any) -> Any:
        return await callback(client)

    tracker = ReplyTrackerService()
    monkeypatch.setattr(tracker, "_persist", AsyncMock())
    interaction_log = InteractionLog()
    monkeypatch.setattr(interaction_log, "save", AsyncMock())
    runtime = SimpleNamespace(
        bot_qq=AsyncMock(return_value="100"),
        with_client=with_client,
        reply_send_lock=asyncio.Lock(),
        reply_tracker=tracker,
        interaction_log=interaction_log,
        config=SimpleNamespace(
            monitor=SimpleNamespace(always_like=True),
            llm=SimpleNamespace(multimodal_mode=True),
        ),
        content=SimpleNamespace(generate_feed_decisions=AsyncMock(return_value=[{
            "tid": "post", "target_qq": "400", "reply_to": "reply",
            "reply_comment_key": comment_identity(comments[3]),
        }])),
    )
    item = {"tid": "post", "target_qq": "400", "comments": comments}
    result = await friend_feeds.process_feed_monitor_batch(runtime, [item])
    assert result["unknown"] == 1
    assert result["replied"] == 0
    client.reply.assert_awaited_once_with(
        "post", "400", "C", "reply", "2", "300", parent_comment_uin="201"
    )
    assert tracker.get_status(
        "post", "0", account_qq="100", host_qq="400", parent_comment_id="2",
        parent_comment_uin="201", comment_uin="300",
    ) == "pending"
    assert not interaction_log.has_commented("400", "post")
    assert not interaction_log.has_seen("400", "post")
    await friend_feeds.process_feed_monitor_batch(runtime, [item])
    assert client.reply.await_count == 1


@pytest.mark.asyncio
async def test_cookie_refresh_only_for_explicit_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """明确的登录拒绝刷新一次 Cookie，网络不确定性不能触发重写。"""
    client = QZoneAPIClient({}, "token", "100")
    request = AsyncMock(side_effect=['{"code":0,"subcode":-3000}', '{"code":0}'])
    monkeypatch.setattr(client, "_request", request)
    runtime = object.__new__(QZoneRuntime)
    monkeypatch.setattr(runtime, "build_client", AsyncMock(return_value=client))
    monkeypatch.setattr(runtime, "bot_qq", AsyncMock(return_value="100"))
    runtime.cookie = SimpleNamespace(clear_cache=lambda account: cleared.append(account))
    cleared: list[str] = []
    assert await runtime.with_client(lambda active: active.comment("200", "post", "body"))
    assert request.await_count == 2
    assert cleared == ["100"]

    request.reset_mock(side_effect=True)
    request.side_effect = OSError("network lost")
    with pytest.raises(QZoneUnknownResultError):
        await runtime.with_client(lambda active: active.comment("200", "post", "body"))
    assert request.await_count == 1
    assert cleared == ["100"]


@pytest.mark.asyncio
async def test_friend_monitor_enriches_folded_content_after_seen_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只为未处理折叠动态补详情，完整正文与服务端统计送入模型。"""
    interaction_log = InteractionLog()
    interaction_log.mark("200", "seen", "read", "poll")
    monkeypatch.setattr(interaction_log, "save", AsyncMock())
    feeds = [
        {"target_qq": "200", "tid": "seen", "content_complete": False},
        {"target_qq": "200", "tid": "new", "content": "short", "content_complete": False, "images": None},
    ]
    client = SimpleNamespace(
        monitor_list_feeds=AsyncMock(return_value=feeds),
        fetch_feed_detail=AsyncMock(return_value={
            "tid": "new", "content": "complete\n" + "body" * 100,
            "content_complete": True, "images": None,
            "comments": [], "comment_count": 0,
        }),
    )

    async def with_client(callback: Any) -> Any:
        return await callback(client)

    runtime = SimpleNamespace(
        with_client=with_client, bot_qq=AsyncMock(return_value="100"),
        interaction_log=interaction_log, reply_tracker=ReplyTrackerService(),
        config=SimpleNamespace(monitor=SimpleNamespace(always_like=False)),
    )
    process = AsyncMock()
    monkeypatch.setattr(friend_feeds, "process_feed_monitor_batch", process)
    await friend_feeds.friend_monitor_once(runtime, 2)
    client.fetch_feed_detail.assert_awaited_once_with("200", "new", paginate_comments=False)
    item = process.await_args.args[1][0]
    assert item["content"] == "complete\n" + "body" * 100
    assert item["content_complete"] is True
    assert item["images"] is None
    assert item["comment_count"] == 0