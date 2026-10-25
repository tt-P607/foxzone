"""好友动态与说说详情读取的离线集成测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

import pytest

from plugins.foxzone.core.http import QZoneAPIClient
from plugins.foxzone.core.http.client import LIST_URL, MSG_DETAIL_URL, ZONE_LIST_URL


def _feed_page(
    key: str,
    *,
    has_more: bool,
    state: dict[str, Any] | None = None,
) -> str:
    """构造 feeds3_html_more 返回结构。"""
    feed = {
        "uin": "20002",
        "nickname": "Reader",
        "abstime": "1700000001",
        "appid": "311",
        "key": key,
        "likecnt": "4",
        "commentcnt": "9",
        "html": (
            f'<li class="f-single"><div class="f-item" data-key="{key}">'
            '<i name="feed_data" data-uin="20002" '
            f'data-fkey="{key}" data-tid="verified-{key}"></i>'
            '<div class="f-info qz_info_cut">摘要<a data-cmd="qz_toggle">展开</a></div>'
            '<div class="f-info qz_info_complete">完整<img alt="[笑]" src="face.gif"></div>'
            '<a class="img-item" data-originurl="https://example.com/post.jpg">'
            '<img src="https://example.com/thumb.jpg"></a>'
            '<a class="img-item" data-v_itemid="video">'
            '<img src="https://example.com/video.jpg"></a>'
            '<div class="mod-comments"><div class="f-info">评论污染</div>'
            '<img src="https://example.com/comment.jpg"></div>'
            '</div><div class="f-single-foot">'
            '<a class="qz_like_btn_v3" data-islike="1" data-likecnt="5">赞</a>'
            '<div class="single-reply"><a class="nickname">Commenter</a>'
            '<div class="comments-content"><a class="nickname">Commenter</a>: 保留评论'
            '<span class="comments-op">回复</span></div>'
            '<a class="act-reply" data-param="t2_tid=0&amp;t2_uin=30003"></a>'
            '</div></div></li>'
        ),
    }
    main = {"hasMoreFeeds": has_more, "error": "", **(state or {})}
    payload = {"code": 0, "data": {"main": main, "data": [feed]}}
    return f"_preloadCallback({json.dumps(payload)});"


def _detail(
    tid: str = "POST",
    comments: list[dict[str, Any]] | None = None,
    *,
    comment_total: int = 2,
) -> str:
    """构造详情 CGI 的正文和一级评论分页。"""
    payload = {
        "code": 0,
        "tid": tid,
        "content": "完整详情正文",
        "created_time": 1700000000,
        "commentnum": comment_total,
        "pic": [{"url3": "https://example.com/full.jpg", "url1": "https://example.com/s.jpg"}],
        "commentlist": comments if comments is not None else [],
    }
    return f"_Callback({json.dumps(payload)});"


def _comment(comment_id: int, uin: int = 20002, replies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """构造 SL msgdetail_v6 评论字段。"""
    return {
        "tid": comment_id,
        "uin": uin,
        "name": "Reader",
        "create_time": 1700000001,
        "content": "评论正文",
        "list_3": replies or [],
    }


def test_monitor_reads_pages_from_home_and_maps_only_own_post_content() -> None:
    """监控每轮从首页开始、跨页去重并保留兼容评论/点赞字段。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        calls: list[dict[str, Any]] = []

        async def request(method: str, url: str, **kwargs: Any) -> str:
            calls.append(kwargs["params"])
            if len(calls) == 1:
                return _feed_page(
                    "one", has_more=True, state={"begintime": "1700000000", "pagenum": "2"}
                )
            return _feed_page("two", has_more=False)

        client._request = request  # type: ignore[method-assign]
        feeds = await client.monitor_list_feeds(2)
        assert [feed["tid"] for feed in feeds] == ["verified-one", "verified-two"]
        assert feeds[0]["content"] == "完整[笑]"
        assert feeds[0]["content_complete"] is True
        assert feeds[0]["images"] == ["https://example.com/post.jpg"]
        assert feeds[0]["like_count"] == 4
        assert feeds[0]["comment_count"] == 9
        assert feeds[0]["liked"] is True
        assert feeds[0]["comments"][0]["comment_tid"] == "0"
        assert feeds[0]["comments"][0]["parent_tid"] is None
        assert feeds[0]["comments_complete"] is False
        assert calls[0]["pagenum"] == "1"
        assert calls[0]["dayspac"] == "0"
        assert calls[0]["aisortEndTime"] == "0"
        assert calls[0]["aisortOffset"] == "0"
        assert calls[0]["aisortBeginTime"] == "0"
        assert calls[0]["getAisort"] == "0"
        assert calls[0]["icServerTime"] == "0"
        assert calls[0]["externparam"] == ""
        assert calls[0]["daylist"] == ""
        assert calls[0]["uinlist"] == ""
        assert calls[0]["format"] == "jsonp"
        assert calls[0]["callback"] == "_preloadCallback"
        assert calls[1]["pagenum"] == "2"
        assert calls[1]["begintime"] == "1700000000"
        next_round = QZoneAPIClient({}, "token", "10001")
        next_calls: list[dict[str, Any]] = []

        async def next_request(method: str, url: str, **kwargs: Any) -> str:
            next_calls.append(kwargs["params"])
            return _feed_page("next-round", has_more=False)

        next_round._request = next_request  # type: ignore[method-assign]
        await next_round.monitor_list_feeds(1)
        assert next_calls[0]["pagenum"] == "1"
        assert next_calls[0]["begintime"] == "0"

    asyncio.run(run())


def test_monitor_rejects_missing_end_flag_and_stalled_cursor() -> None:
    """缺少结束证据或状态未推进时抛错，不伪装为正常结束。"""
    async def missing_flag() -> None:
        client = QZoneAPIClient({}, "token", "10001")

        async def request(method: str, url: str, **kwargs: Any) -> str:
            payload = {"code": 0, "data": {"main": {}, "data": []}}
            return json.dumps(payload)

        client._request = request  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="hasMoreFeeds"):
            await client.monitor_list_feeds(1)

    async def stalled() -> None:
        client = QZoneAPIClient({}, "token", "10001")

        async def request(method: str, url: str, **kwargs: Any) -> str:
            return _feed_page("same", has_more=True, state={"pagenum": "2"})

        client._request = request  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="未推进"):
            await client.monitor_list_feeds(2)

    asyncio.run(missing_flag())
    asyncio.run(stalled())


def test_monitor_uses_server_page_number_without_computing_next_page() -> None:
    """分页原样使用服务端状态，不要求 pagenum 恰好加一。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        calls: list[dict[str, Any]] = []

        async def request(method: str, url: str, **kwargs: Any) -> str:
            calls.append(kwargs["params"])
            if len(calls) == 1:
                return _feed_page("one", has_more=True, state={"pagenum": "4"})
            return _feed_page("two", has_more=False)

        client._request = request  # type: ignore[method-assign]
        assert len(await client.monitor_list_feeds(2)) == 2
        assert calls[1]["pagenum"] == "4"

    asyncio.run(run())


def test_monitor_preserves_omitted_state_fields_when_other_fields_advance() -> None:
    """后页省略 pagenum 时沿用原值，由完整状态判断推进。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        calls: list[dict[str, Any]] = []

        async def request(method: str, url: str, **kwargs: Any) -> str:
            calls.append(kwargs["params"])
            if len(calls) == 1:
                return _feed_page("one", has_more=True, state={"externparam": "changed"})
            return _feed_page("two", has_more=False)

        client._request = request  # type: ignore[method-assign]
        assert len(await client.monitor_list_feeds(2)) == 2
        assert calls[1]["pagenum"] == "1"
        assert calls[1]["externparam"] == "changed"

    asyncio.run(run())


def test_monitor_uses_hasmore_only_when_main_is_absent() -> None:
    """只有 main 字段缺失时才回退读取 hasmore。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        payload = {"code": 0, "data": {"hasmore": 0, "data": []}}

        async def request(method: str, url: str, **kwargs: Any) -> str:
            return json.dumps(payload)

        client._request = request  # type: ignore[method-assign]
        assert await client.monitor_list_feeds(1) == []

        payload["data"]["hasmore"] = 1
        with pytest.raises(ValueError, match="缺少 main 状态"):
            await client.monitor_list_feeds(1)

        payload["data"]["main"] = None
        with pytest.raises(TypeError, match="状态结构异常"):
            await client.monitor_list_feeds(1)

    asyncio.run(run())


def test_list_feeds_uses_detail_once_per_feed_and_preserves_zero_ids() -> None:
    """列表使用同一详情读取正文/图片和分页评论，父评论 ID 零值有效。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        requests: list[tuple[str, dict[str, Any]]] = []
        first_comment = _comment(0, replies=[_comment(1, 30003)])

        async def request(method: str, url: str, **kwargs: Any) -> str:
            requests.append((url, kwargs.get("params", {})))
            if url == LIST_URL:
                return json.dumps(
                    {
                        "code": 0,
                        "logininfo": {"name": "Bot"},
                        "msglist": [
                            {
                                "tid": "POST",
                                "content": "摘要",
                                "created_time": 1700000000,
                                "cmtnum": 3,
                                "commentnum": 2,
                                "pic": [],
                                "commentlist": [first_comment],
                            }
                        ],
                    }
                )
            position = kwargs["params"]["pos"]
            if position == 0:
                return _detail(comments=[first_comment])
            if position == 1:
                return _detail(comments=[_comment(2, 30004)])
            return _detail(comments=[])

        client._request = request  # type: ignore[method-assign]
        feeds = await client.list_feeds("10001", 1, skip_commented=False)
        assert len(feeds) == 1
        feed = feeds[0]
        assert feed["content"] == "完整详情正文"
        assert feed["content_complete"] is True
        assert feed["images"] == ["https://example.com/full.jpg"]
        assert feed["comment_total"] == 2
        assert feed["comments_complete"] is True
        assert feed["inline_replies_complete"] is False
        assert [comment["comment_tid"] for comment in feed["comments"]] == [0, 1, 2]
        assert feed["comments"][1]["parent_tid"] == 0
        assert feed["comments"][0]["parent_comment_id"] is None
        assert [params["pos"] for url, params in requests if url != LIST_URL] == [0, 1]
        detail_url = urlsplit(requests[1][0])
        assert detail_url.netloc == "h5.qzone.qq.com"
        assert detail_url.path.endswith("/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6")
        assert requests[1][1] == {
            "uin": "10001",
            "tid": "POST",
            "t1_source": "1",
            "not_trunc_con": "1",
            "need_right": "1",
            "not_adapt_outpic": "1",
            "g_tk": "token",
            "pos": 0,
            "num": 20,
        }

    asyncio.run(run())


def test_numeric_picture_count_is_not_treated_as_picture_array() -> None:
    """pictotal 是统计而不是 URL 数组，不能阻断有效详情读取。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")

        async def request(method: str, url: str, **kwargs: Any) -> str:
            return json.dumps({
                "code": 0, "tid": "POST", "content": "body",
                "pictotal": 12, "cmtnum": 0, "commentlist": [],
            })

        client._request = request  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST")
        assert detail is not None
        assert detail["images"] is None

    asyncio.run(run())


def test_detail_images_supplement_html_images_and_comment_total_controls_completion() -> None:
    """详情图片补充列表原图，短页继续读取且完整性以 cmtnum 为准。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        positions: list[int] = []

        async def request(method: str, url: str, **kwargs: Any) -> str:
            if url == LIST_URL:
                return json.dumps(
                    {
                        "code": 0,
                        "logininfo": {"name": "Bot"},
                        "msglist": [
                            {
                                "tid": "POST",
                                "content": "列表正文",
                                "created_time": 1700000000,
                                "pic": [{"url3": "https://example.com/list.jpg"}],
                                "commentlist": [],
                            }
                        ],
                    }
                )
            pos = kwargs["params"]["pos"]
            positions.append(pos)
            if pos == 0:
                return _detail(comments=[_comment(0)], comment_total=3)
            if pos == 1:
                return _detail(comments=[_comment(1)], comment_total=3)
            if pos == 2:
                return _detail(comments=[_comment(2)], comment_total=3)
            return _detail(comments=[], comment_total=3)

        client._request = request  # type: ignore[method-assign]
        feeds = await client.list_feeds("10001", 1, skip_commented=False)
        assert feeds[0]["images"] == [
            "https://example.com/list.jpg",
            "https://example.com/full.jpg",
        ]
        assert feeds[0]["comments_complete"] is True
        assert [comment["comment_tid"] for comment in feeds[0]["comments"]] == [0, 1, 2]
        assert positions == [0, 1, 2]

    asyncio.run(run())


def test_short_comment_page_is_not_complete_and_unknown_total_stays_partial() -> None:
    """短页或未知 cmtnum 不代表已取得完整评论。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        calls = 0

        async def request(method: str, url: str, **kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _detail(comments=[_comment(0)], comment_total=2)
            return _detail(comments=[], comment_total=2)

        client._request = request  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST", comment_page_size=5)
        assert detail is not None
        assert detail["comments_complete"] is False
        assert detail["comment_total"] == 2
        assert calls == 2

        calls = 0

        async def unknown_total(method: str, url: str, **kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            return json.dumps(
                {
                    "code": 0,
                    "tid": "POST",
                    "content": "body",
                    "commentlist": [] if calls == 1 else [_comment(0)],
                }
            )

        client._request = unknown_total  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST")
        assert detail is not None
        assert detail["comment_total"] is None
        assert detail["comments_complete"] is False

    asyncio.run(run())


def test_repeated_comment_page_and_page_budget_remain_partial() -> None:
    """重复页和读取预算耗尽都保留 partial，而不猜测评论区完整。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        calls = 0

        async def repeated(method: str, url: str, **kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            return _detail(comments=[_comment(0)], comment_total=5)

        client._request = repeated  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST")
        assert detail is not None
        assert detail["comments_complete"] is False
        assert len(detail["comments"]) == 1
        assert calls == 2

        calls = 0

        async def limited(method: str, url: str, **kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            return _detail(comments=[_comment(0)], comment_total=5)

        client._request = limited  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST", max_comment_pages=1)
        assert detail is not None
        assert detail["comments_complete"] is False
        assert calls == 1

    asyncio.run(run())


def test_empty_detail_body_does_not_replace_nonempty_list_content() -> None:
    """详情正文为空时保留列表正文，不把失败内容误作有效完整正文。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")

        async def request(method: str, url: str, **kwargs: Any) -> str:
            if url == LIST_URL:
                return json.dumps(
                    {
                        "code": 0,
                        "logininfo": {"name": "Bot"},
                        "msglist": [
                            {"tid": "POST", "content": "有效列表正文", "commentlist": []}
                        ],
                    }
                )
            return json.dumps(
                {
                    "code": 0,
                    "tid": "POST",
                    "content": "",
                    "commentnum": 0,
                    "commentlist": [],
                }
            )

        client._request = request  # type: ignore[method-assign]
        feed = (await client.list_feeds("10001", 1, skip_commented=False))[0]
        assert feed["content"] == "有效列表正文"
        assert feed["content_complete"] is None

    asyncio.run(run())


def test_detail_rejects_mismatched_tid_and_propagates_cookie_expiry() -> None:
    """错误 tid 不会被请求值兜底，-3000 保持可识别 RuntimeError。"""
    async def mismatched() -> None:
        client = QZoneAPIClient({}, "token", "10001")

        async def request(method: str, url: str, **kwargs: Any) -> str:
            return _detail(tid="OTHER")

        client._request = request  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="tid"):
            await client.fetch_feed_detail("20002", "POST")

    async def expired() -> None:
        client = QZoneAPIClient({}, "token", "10001")

        async def request(method: str, url: str, **kwargs: Any) -> str:
            return json.dumps({"code": -3000, "message": "expired"})

        client._request = request  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="错误码: -3000"):
            await client.fetch_feed_detail("20002", "POST")

    asyncio.run(mismatched())
    asyncio.run(expired())


@pytest.mark.parametrize("total", [0, 12])
def test_detail_accepts_null_comment_page_and_keeps_server_total(total: int) -> None:
    """null 评论页是空页，只有服务端总数为零才确认一级评论完整。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")

        async def request(method: str, url: str, **kwargs: Any) -> str:
            return json.dumps({
                "code": 0, "tid": "POST", "content": "", "cmtnum": total,
                "commentlist": None,
            })

        client._request = request  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST")
        assert detail is not None
        assert detail["comments"] == []
        assert detail["comment_count"] == total
        assert detail["comments_complete"] is (total == 0)

    asyncio.run(run())


def test_detail_maps_url_variants_and_deduplicates_valid_http_images() -> None:
    """详情图片按 url3/url2/url1/smallurl 顺序选择并去重。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        pics = [
            {"url3": "https://example.com/three.jpg", "url2": "https://example.com/two.jpg"},
            {"url3": "javascript:bad()", "url2": "https://example.com/two.jpg"},
            {"url2": "data:image/png;base64,bad", "url1": "//example.com/one.jpg"},
            {"url1": "file:///private.jpg", "smallurl": "https://example.com/small.jpg"},
        ]

        async def request(method: str, url: str, **kwargs: Any) -> str:
            return json.dumps(
                {
                    "code": 0,
                    "tid": "POST",
                    "content": "正文",
                    "cmtnum": 0,
                    "commentlist": [],
                    "pic": pics,
                }
            )

        client._request = request  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST", paginate_comments=False)
        assert detail is not None
        assert detail["images"] == [
            "https://example.com/three.jpg",
            "https://example.com/two.jpg",
            "https://example.com/one.jpg",
            "https://example.com/small.jpg",
        ]

    asyncio.run(run())


def test_detail_keeps_partial_comments_after_page_error_but_not_cookie_expiry() -> None:
    """普通后续页故障保留可见评论并标 partial，-3000 仍必须上抛。"""
    async def partial() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        requests = 0

        async def request(method: str, url: str, **kwargs: Any) -> str:
            nonlocal requests
            requests += 1
            if requests == 1:
                return _detail(comments=[_comment(0)])
            raise RuntimeError("network interrupted")

        client._request = request  # type: ignore[method-assign]
        detail = await client.fetch_feed_detail("20002", "POST")
        assert detail is not None
        assert [comment["comment_tid"] for comment in detail["comments"]] == [0]
        assert detail["comment_total"] == 2
        assert detail["comments_complete"] is False

    async def expired_on_later_page() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        requests = 0

        async def request(method: str, url: str, **kwargs: Any) -> str:
            nonlocal requests
            requests += 1
            if requests == 1:
                return _detail(comments=[_comment(0)])
            return json.dumps({"code": -3000, "message": "expired"})

        client._request = request  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="错误码: -3000"):
            await client.fetch_feed_detail("20002", "POST")

    asyncio.run(partial())
    asyncio.run(expired_on_later_page())


def test_list_feeds_can_disable_detail_and_reports_partial_comment_state() -> None:
    """关闭评论详情请求时保留列表内嵌评论并明确其并非完整结果。"""
    async def run() -> None:
        client = QZoneAPIClient({}, "token", "10001")
        requests: list[str] = []

        async def request(method: str, url: str, **kwargs: Any) -> str:
            requests.append(url)
            return json.dumps(
                {
                    "code": 0,
                    "logininfo": {"name": "Bot"},
                    "msglist": [
                        {
                            "tid": "POST",
                            "content": "摘要",
                            "created_time": 1700000000,
                            "commentnum": 1,
                            "commentlist": [_comment(0)],
                        }
                    ],
                }
            )

        client._request = request  # type: ignore[method-assign]
        feed = (await client.list_feeds("10001", 1, skip_commented=False, paginate_comments=False))[0]
        assert feed["comments"][0]["comment_tid"] == 0
        assert feed["comments_complete"] is False
        assert feed["content_complete"] is None
        assert requests == [LIST_URL]

    asyncio.run(run())


def test_feed_page_endpoint_is_the_direct_h5_proxy() -> None:
    """验证好友动态继续使用既有 feeds3_html_more 直连端点。"""
    assert urlsplit(ZONE_LIST_URL).path.endswith("/feeds/feeds3_html_more")
    assert urlsplit(MSG_DETAIL_URL).netloc == "h5.qzone.qq.com"