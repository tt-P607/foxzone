"""楼中楼回复协议与回执解析测试。"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.foxzone.core.http import QZoneAPIClient
from plugins.foxzone.core.http.comments import (
    QZoneRateLimitError,
    QZoneUnknownResultError,
)
from plugins.foxzone.core.http.response import check_response, parse_response


def test_parse_jsonp_and_nested_frame_callback_without_changing_string_undefined() -> None:
    """JSONP/frame 的嵌套对象可解析，字符串中的 undefined 保持原样。"""
    assert parse_response('_cb({"code":0,"data":{"id":0}});')["data"]["id"] == 0
    parsed = parse_response(
        '<html><script>frameElement.callback({code:0,subcode:0,'
        'data:{id:"A/B",message:"undefined"},extra:undefined});'
        '</script></body></html>'
    )
    assert parsed == {
        "code": 0,
        "subcode": 0,
        "data": {"id": "A/B", "message": "undefined"},
        "extra": None,
    }


@pytest.mark.parametrize(
    "response",
    [
        {"code": True},
        {"code": "0"},
        {"ret": 0},
        {"code": 0, "subcode": True},
        {"code": 0, "subcode": "0"},
        {"code": 0, "subcode": -7},
        {"code": -3000, "subcode": 1},
    ],
)
def test_response_validation_rejects_invalid_or_conflicting_codes(
    response: dict[str, Any],
) -> None:
    """code/subcode 必须是整数且必须共同表示成功。"""
    with pytest.raises(RuntimeError):
        check_response(response, "回复")


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_id", [0, "0"])
async def test_reply_uses_separate_endpoint_and_parent_author(
    monkeypatch: pytest.MonkeyPatch,
    parent_id: str | int,
) -> None:
    """回复使用专用协议并返回服务端新 ID，不混淆操作者和父作者。"""
    client = QZoneAPIClient({}, "token", "10000")
    captured: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        captured.append((method, url, kwargs["data"]))
        return 'frameElement.callback({code:0,subcode:0,data:{id:0}});'

    monkeypatch.setattr(client, "_request", request)
    result = await client.reply(
        "POST/HEX-A",
        "20002",
        "  Name%,} 😀  ",
        "body",
        parent_id,
        "40004",
        parent_comment_uin="30003",
    )

    method, url, data = captured[0]
    assert method == "POST"
    assert url.endswith("emotion_cgi_addreply_ugc")
    assert data == {
        "uin": "10000",
        "hostUin": "20002",
        "hostuin": "10000",
        "topicId": "20002_POST/HEX-A",
        "commentId": "0",
        "commentUin": "30003",
        "content": "@{uin:40004,nick:Name%25%2C%7D 😀,auto:1} body",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "ref": "",
        "code_version": "1",
        "format": "fs",
        "qzreferrer": "https://user.qzone.qq.com/20002",
    }
    assert result == {
        "comment_id": "0",
        "parent_comment_id": "0",
        "parent_comment_uin": 30003,
    }


@pytest.mark.asyncio
async def test_reply_without_nested_recipient_does_not_add_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回复一级评论时正文保持原样。"""
    client = QZoneAPIClient({}, "token", "10000")
    captured: list[dict[str, Any]] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        captured.append(kwargs["data"])
        return '{"code":0,"data":{"id":"reply-non-hex"}}'

    monkeypatch.setattr(client, "_request", request)
    await client.reply(
        "feed",
        "20002",
        "",
        "plain reply",
        "parent/nonhex",
        parent_comment_uin=30003,
    )
    assert captured[0]["content"] == "plain reply"


@pytest.mark.asyncio
async def test_reply_rejects_images_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """不支持的回复图片在任何网络请求前拒绝。"""
    client = QZoneAPIClient({}, "token", "10000")
    requests: list[str] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        requests.append(url)
        return '{"code":0,"data":{"id":1}}'

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(ValueError, match="不支持图片"):
        await client.reply(
            "feed", "20002", "name", "reply", "parent", parent_comment_uin=30003,
            images=[b"image"],
        )
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "<html>login required</html>",
        '{"code":true,"data":{"id":1}}',
        '{"code":0,"subcode":"0","data":{"id":1}}',
        '{"code":0,"data":{}}',
        '{"code":0,"data":{"id":true}}',
        '{"code":0,"data":{"id":-1}}',
    ],
)
async def test_unknown_or_incomplete_reply_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    """无法确认写入结果或缺少有效 ID 时抛未知结果异常，且仅请求一次。"""
    client = QZoneAPIClient({}, "token", "10000")
    requests: list[str] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        requests.append(url)
        return response

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(QZoneUnknownResultError):
        await client.reply("feed", "20002", "", "reply", "0", parent_comment_uin=30003)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_type", "message"),
    [
        ('{"code":-3000,"message":"login required"}', RuntimeError, "错误码: -3000"),
        ('{"code":-10049,"subcode":0}', QZoneRateLimitError, "rate limit"),
        ('{"code":-7,"subcode":0}', RuntimeError, "code=-7"),
        ('{"code":0,"subcode":1}', RuntimeError, "subcode=1"),
    ],
)
async def test_explicit_reply_rejections_propagate_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    error_type: type[RuntimeError],
    message: str,
) -> None:
    """明确拒绝分类传播，特别的限流异常保留给批量引擎识别。"""
    client = QZoneAPIClient({}, "token", "10000")
    requests: list[str] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        requests.append(url)
        return response

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(error_type, match=message):
        await client.reply("feed", "20002", "", "reply", "0", parent_comment_uin=30003)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_reply_transport_failure_is_unknown_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写后断线作为未知投递结果传播，不触发第二次请求。"""
    client = QZoneAPIClient({}, "token", "10000")
    requests: list[str] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        requests.append(url)
        raise RuntimeError("connection lost")

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(QZoneUnknownResultError):
        await client.reply("feed", "20002", "", "reply", "0", parent_comment_uin=30003)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "uin"), [("name", ""), ("", "40004")])
async def test_reply_recipient_requires_both_fields(
    monkeypatch: pytest.MonkeyPatch, name: str, uin: str
) -> None:
    """被提及者的昵称和账号缺一时不发送任何请求。"""
    client = QZoneAPIClient({}, "token", "10000")

    async def request(*args: Any, **kwargs: Any) -> str:
        pytest.fail("invalid recipient must not issue a write")

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(ValueError, match="成对"):
        await client.reply("feed", "20002", name, "reply", "0", uin, parent_comment_uin=30003)