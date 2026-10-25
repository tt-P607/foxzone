"""评论写入回执与不确定结果的离线测试。"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.foxzone.core.http import QZoneAPIClient
from plugins.foxzone.core.http.comments import QZoneUnknownResultError


@pytest.mark.asyncio
async def test_unparseable_comment_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无法判定投递状态的写回执必须阻止自动重发。"""
    client = QZoneAPIClient({}, "token", "10000")
    requests: list[str] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        """只替换网络传输，保留真实评论结果处理。"""
        requests.append(method)
        return "<html>connection interrupted</html>"

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(QZoneUnknownResultError):
        await client.comment("20000", "post", "example")
    assert requests == ["POST"]


@pytest.mark.asyncio
async def test_regular_comment_keeps_endpoint_and_accepts_code_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通评论继续使用旧评论端点并校验明确成功回执。"""
    client = QZoneAPIClient({}, "token", "10000")
    captured: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        captured.append((method, url, kwargs["data"]))
        return '{"code":0,"subcode":0}'

    monkeypatch.setattr(client, "_request", request)
    assert await client.comment("20000", "feed", "text") is True
    assert captured[0][0] == "POST"
    assert captured[0][1].endswith("emotion_cgi_re_feeds")
    assert captured[0][2]["topicId"] == "20000_feed__1"


@pytest.mark.asyncio
async def test_regular_comment_preserves_existing_form_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通评论保持原有 emotion_cgi_re_feeds 表单语义。"""
    client = QZoneAPIClient({}, "token", "10000")
    captured: list[dict[str, Any]] = []

    async def request(method: str, url: str, **kwargs: Any) -> str:
        captured.append(kwargs["data"])
        return '{"code":0}'

    monkeypatch.setattr(client, "_request", request)
    await client.comment("20000", "feed", "text")
    assert captured[0] == {
        "topicId": "20000_feed__1",
        "uin": "10000",
        "hostUin": "20000",
        "feedsType": 100,
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "plat": "qzone",
        "source": "ic",
        "platformid": 52,
        "format": "fs",
        "ref": "feeds",
        "content": "text",
    }