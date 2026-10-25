"""FoxZone Service 与 Tool 消费者集成测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.foxzone.components.service import QZoneService
from plugins.foxzone.components.tools import comment as comment_module
from plugins.foxzone.components.tools import read_feed as read_feed_module
from plugins.foxzone.components.tools.comment import QZoneCommentTool
from plugins.foxzone.components.tools.read_feed import ReadFeedTool
from plugins.foxzone.core.http.comments import QZoneUnknownResultError
from plugins.foxzone.core.reply_tracker import ReplyTrackerService


class _InteractionLog:
    """记录 Service 成功互动调用。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str, str]] = []

    def mark(self, host: str, feed_id: str, action: str, source: str) -> None:
        """保存互动参数。"""
        self.entries.append((host, feed_id, action, source))

    async def save(self) -> None:
        """提供异步持久化边界替身。"""


class _Client:
    """记录评论和回复请求。"""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str, str]] = []
        self.replies: list[dict[str, Any]] = []
        self.comment_handler: Any = None
        self.reply_handler: Any = None
        self.reply_result: dict[str, Any] = {
            "comment_id": "new-reply-id",
            "parent_comment_id": "",
            "parent_comment_uin": "",
        }

    async def comment(self, host: str, feed_id: str, text: str) -> bool:
        """保存普通评论请求并返回明确成功。"""
        self.comments.append((host, feed_id, text))
        if self.comment_handler:
            return await self.comment_handler()
        return True

    async def reply(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """保存楼中楼回复请求并返回新评论 ID。"""
        self.replies.append({"args": args, "kwargs": kwargs})
        if self.reply_handler:
            return await self.reply_handler()
        return {
            **self.reply_result,
            "parent_comment_id": args[4],
            "parent_comment_uin": kwargs["parent_comment_uin"],
        }


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    comments: list[dict[str, Any]] | None = None,
) -> tuple[QZoneService, _Client, ReplyTrackerService, _InteractionLog]:
    """构造不访问网络或持久存储的 QZoneService。"""
    tracker = ReplyTrackerService()
    tracker._data = {"data": {}, "scoped": {}, "pending_comments": {}}

    async def persist() -> None:
        """替代插件存储的落盘操作。"""

    monkeypatch.setattr(tracker, "_persist", persist)
    client = _Client()
    interaction_log = _InteractionLog()

    async def with_client(callback: Any) -> Any:
        """在本地客户端替身上运行 Service 回调。"""
        return await callback(client)

    runtime = SimpleNamespace(
        bot_qq=lambda: _async_value("100"),
        reply_tracker=tracker,
        reply_send_lock=asyncio.Lock(),
        interaction_log=interaction_log,
        with_client=with_client,
    )
    service = object.__new__(QZoneService)
    service._plugin = SimpleNamespace(runtime=runtime)

    async def detail(_host: str, _feed: str) -> dict[str, Any]:
        """返回精确评论列表替身。"""
        return {"comments": comments or []}

    service.get_feed_detail = detail
    return service, client, tracker, interaction_log


async def _async_value(value: Any) -> Any:
    """返回异步测试值。"""
    return value


@pytest.mark.asyncio
async def test_reply_resolves_zero_id_and_passes_complete_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """零 ID 可用作精确目标，父作者与完整身份作用域进入写入和状态。"""
    comments = [
        {
            "comment_tid": "0",
            "parent_tid": None,
            "qq_account": "200",
            "nickname": "一级作者",
        },
        {
            "comment_tid": "0",
            "parent_tid": "0",
            "parent_comment_id": "0",
            "parent_comment_uin": "200",
            "qq_account": "300",
            "nickname": "二级作者",
        },
    ]
    service, client, tracker, _ = _service(monkeypatch, comments=comments)

    result = await service.reply_comment(
        feed_id="feed",
        host_qq="400",
        reply_text="reply",
        comment_tid="0",
        comment_uin="300",
        parent_comment_id="0",
        parent_comment_uin="200",
    )

    assert result is True
    assert client.replies[0]["args"][4] == "0"
    assert client.replies[0]["args"][5] == "300"
    assert client.replies[0]["kwargs"] == {"parent_comment_uin": "200"}
    assert tracker.get_status(
        "feed",
        "0",
        account_qq="100",
        host_qq="400",
        parent_comment_id="0",
        parent_comment_uin="200",
        comment_uin="300",
    ) == "replied"


@pytest.mark.asyncio
async def test_reply_accepts_zero_as_new_reply_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整数零是合法的新回复 ID，不会被误作缺少回执。"""
    service, client, _, _ = _service(
        monkeypatch,
        comments=[{"comment_tid": "0", "parent_tid": None, "qq_account": "200"}],
    )
    client.reply_result["comment_id"] = 0

    assert await service.reply_comment(
        feed_id="feed",
        host_qq="400",
        reply_text="reply",
        comment_tid="0",
        comment_uin="200",
    ) is True


@pytest.mark.asyncio
async def test_service_keeps_positional_reply_contract_with_verified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既有六个位置参数仍可调用，实际昵称与作者从详情核验。"""
    service, client, _, interactions = _service(
        monkeypatch,
        comments=[{"comment_tid": "0", "qq_account": "200", "nickname": "verified"}],
    )
    assert await service.reply_comment("feed", "400", "old-label", "body", "0", "200")
    assert client.replies[0]["args"][2] == "verified"
    assert client.replies[0]["kwargs"] == {"parent_comment_uin": "200"}
    assert len(interactions.entries) == 1


@pytest.mark.asyncio
async def test_reply_rejects_ambiguous_same_author_child_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨父评论重复的子 ID 不会按首条评论猜测发送。"""
    comments = [
        {"comment_tid": "0", "parent_tid": "1", "qq_account": "300"},
        {"comment_tid": "0", "parent_tid": "2", "qq_account": "300"},
    ]
    service, client, _, _ = _service(monkeypatch, comments=comments)

    result = await service.reply_comment(
        feed_id="feed",
        host_qq="400",
        reply_text="reply",
        comment_tid="0",
        comment_uin="300",
    )

    assert result is False
    assert client.replies == []


@pytest.mark.asyncio
async def test_reply_requires_explicit_parent_scope_for_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使子评论当前唯一，二级回复也必须显式给出父评论身份。"""
    comments = [
        {"comment_tid": "0", "parent_tid": None, "qq_account": "200"},
        {
            "comment_tid": "2",
            "parent_tid": "0",
            "parent_comment_id": "0",
            "parent_comment_uin": "200",
            "qq_account": "300",
        },
    ]
    service, client, _, _ = _service(monkeypatch, comments=comments)

    result = await service.reply_comment(
        feed_id="feed",
        host_qq="400",
        reply_text="reply",
        comment_tid="2",
        comment_uin="300",
    )

    assert result is False
    assert client.replies == []


@pytest.mark.asyncio
async def test_unknown_reply_persists_full_scope_and_prevents_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回复未知回执保存精确 pending 作用域，之后不再发请求。"""
    comments = [
        {"comment_tid": "0", "parent_tid": None, "qq_account": "200"},
        {
            "comment_tid": "2",
            "parent_tid": "0",
            "parent_comment_id": "0",
            "parent_comment_uin": "200",
            "qq_account": "300",
        },
    ]
    service, client, tracker, _ = _service(monkeypatch, comments=comments)

    async def unknown() -> dict[str, Any]:
        """模拟回复请求已发出但回执未知。"""
        raise QZoneUnknownResultError("unknown")

    client.reply_handler = unknown
    args = {
        "feed_id": "feed",
        "host_qq": "400",
        "reply_text": "reply",
        "comment_tid": "2",
        "comment_uin": "300",
        "parent_comment_id": "0",
        "parent_comment_uin": "200",
    }
    with pytest.raises(QZoneUnknownResultError, match="unknown"):
        await service.reply_comment(**args)
    assert tracker.get_status(
        "feed",
        "2",
        account_qq="100",
        host_qq="400",
        parent_comment_id="0",
        parent_comment_uin="200",
        comment_uin="300",
    ) == "pending"
    with pytest.raises(QZoneUnknownResultError, match="禁止再次发送"):
        await service.reply_comment(**args)
    assert len(client.replies) == 1


@pytest.mark.asyncio
async def test_unknown_comment_persists_pending_and_prevents_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通评论未知回执保存 pending，重复调用不再次触达客户端。"""
    service, client, tracker, interaction_log = _service(monkeypatch)

    async def unknown() -> bool:
        """模拟请求已发出但回执未知。"""
        raise QZoneUnknownResultError("unknown")

    client.comment_handler = unknown
    with pytest.raises(QZoneUnknownResultError, match="unknown"):
        await service.comment("400", "feed", "text")
    assert tracker.is_comment_pending("100", "400", "feed")
    with pytest.raises(QZoneUnknownResultError, match="禁止再次发送"):
        await service.comment("400", "feed", "text")
    assert len(client.comments) == 1
    assert interaction_log.entries == []


@pytest.mark.asyncio
async def test_explicit_comment_error_is_not_recorded_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """明确服务端错误返回失败，不写入 pending 状态。"""
    service, client, tracker, interaction_log = _service(monkeypatch)

    async def rejected() -> bool:
        """模拟明确拒绝回执。"""
        raise RuntimeError("rejected")

    client.comment_handler = rejected
    assert await service.comment("400", "feed", "text") is False
    assert not tracker.is_comment_pending("100", "400", "feed")
    assert interaction_log.entries == []


@pytest.mark.asyncio
async def test_comment_send_is_serialized_and_records_only_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享 runtime 锁使普通评论发送串行，明确成功才记互动。"""
    service, client, _, interaction_log = _service(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0

    async def delayed_success() -> bool:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        entered.set()
        await release.wait()
        active -= 1
        return True

    client.comment_handler = delayed_success
    first = asyncio.create_task(service.comment("400", "feed-a", "one"))
    await entered.wait()
    second = asyncio.create_task(service.comment("400", "feed-b", "two"))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert maximum_active == 1
    assert len(interaction_log.entries) == 2


@pytest.mark.asyncio
async def test_comment_tool_requires_exact_target_fields_and_propagates_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标字段不完整时不回退普通评论，未知结果明确提示禁止重发。"""
    service, _, _, _ = _service(monkeypatch)
    monkeypatch.setattr(comment_module, "get_service", lambda _sig: service)
    tool = object.__new__(QZoneCommentTool)

    incomplete = await tool.execute("400", "feed", "text", comment_id="0")
    assert incomplete[0] is False
    assert "未发送" in incomplete[1]

    async def unknown(**_kwargs: Any) -> bool:
        """模拟 Service 的不确定结果。"""
        raise QZoneUnknownResultError("待核实")

    service.comment = unknown
    result = await tool.execute("400", "feed", "text")
    assert result[0] is False
    assert "阻止自动重发" in result[1]


@pytest.mark.asyncio
async def test_tool_preserves_numeric_zero_and_rejects_explicit_empty_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数值零进入精确回复，显式空身份不能回退普通评论。"""
    service, client, _, _ = _service(monkeypatch, comments=[
        {"comment_tid": "0", "qq_account": "200", "nickname": "parent"},
        {"comment_tid": "0", "parent_tid": "0", "parent_comment_uin": "200", "qq_account": "300", "nickname": "child"},
    ])
    monkeypatch.setattr(comment_module, "get_service", lambda _sig: service)
    tool = object.__new__(QZoneCommentTool)
    result = await tool.execute(
        "400", "feed", "body", comment_id=0, comment_uin="300",
        parent_comment_id=0, parent_comment_uin="200",
    )
    assert result[0] is True
    assert client.replies[0]["args"][4] == "0"
    assert client.comments == []
    invalid = await tool.execute("400", "feed", "body", comment_id="")
    assert invalid[0] is False
    assert client.comments == []


@pytest.mark.asyncio
async def test_read_feed_keeps_server_counts_completeness_long_text_and_all_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取工具展示服务端统计、完整正文及全部图片，None 和未知状态安全。"""
    long_text = "长正文" * 120
    urls = [f"https://example.com/{index}.jpg" for index in range(12)]
    feeds = [
        {
            "tid": "partial",
            "content": long_text,
            "content_complete": False,
            "images": urls,
            "comments": [{"comment_tid": "0", "content": "comment"}],
            "comment_count": 0,
            "comments_complete": False,
            "inline_replies_complete": False,
        },
        {
            "tid": "unknown",
            "content": None,
            "rt_con": None,
            "content_complete": None,
            "images": None,
            "comments": None,
            "comment_count": None,
            "comments_complete": None,
            "inline_replies_complete": None,
        },
        {
            "tid": "complete",
            "content": "完整内容",
            "content_complete": True,
            "images": [],
            "comments": [],
            "comment_count": 0,
            "comments_complete": True,
            "inline_replies_complete": True,
        },
    ]

    class _ReadService:
        """提供模拟 feed 列表、详情和图片识别。"""

        async def list_feeds(self, **_kwargs: Any) -> list[dict[str, Any]]:
            """返回列表入口。"""
            return feeds

        async def get_feed_detail(self, _host: str, feed_id: str) -> dict[str, Any]:
            """返回带完整性状态的详情。"""
            return next(feed for feed in feeds if feed["tid"] == feed_id)

        async def describe_images(self, image_urls: list[str]) -> dict[str, str]:
            """记录全部图片并返回描述。"""
            self.image_urls = image_urls
            return {url: f"image-{index}" for index, url in enumerate(image_urls)}

    service = _ReadService()
    monkeypatch.setattr(read_feed_module, "get_service", lambda _sig: service)
    tool = object.__new__(ReadFeedTool)

    success, output = await tool.execute("400")

    assert success is True
    assert long_text in output
    assert "服务端 0 条，当前可见 1 条" in output
    assert "正文完整=False" in output
    assert "正文完整=True" in output
    assert "正文完整=未知" in output
    assert "一级评论完整=False" in output
    assert "完整内容" in output
    assert "服务端数量=未知" in output
    assert "（无正文）" in output
    assert all(url in service.image_urls for url in urls)
    assert "图片12：image-11" in output
    assert "None" not in output


def test_feed_formatter_reports_unknown_server_total() -> None:
    """服务端总数未知时只报告当前可见评论数，不当作全量结果。"""
    formatted = ReadFeedTool._format_feed(
        1,
        {
            "tid": "feed",
            "comments": [{"comment_tid": "1", "content": "visible"}],
            "comment_count": None,
            "comments_complete": None,
            "inline_replies_complete": None,
        },
    )

    assert "服务端数量未知，当前可见 1 条" in formatted
    assert "一级评论完整=未知" in formatted


def test_read_tool_exposes_exact_child_and_parent_identity() -> None:
    """工具向模型展示父作者及父 ID 零值，而不只展示重号子 ID。"""
    formatted = ReadFeedTool._format_feed(1, {
        "tid": "post", "comments": [{
            "comment_tid": 0, "qq_account": "300", "parent_tid": 0,
            "parent_comment_id": 0, "parent_comment_uin": "200", "content": "child",
        }],
    })
    assert "comment_id=0, comment_uin=300" in formatted
    assert "parent_comment_id=0, parent_comment_uin=200" in formatted