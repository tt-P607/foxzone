"""QZone 说说读取接口族（msglist_v6 / msgdetail_v6 / feeds3_html_more）。"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import bs4
from bs4.element import Tag

from .client import LIST_URL, MSG_DETAIL_URL, ZONE_LIST_URL, QZoneClientBase, logger
from .feed_content import parse_feed_content
from .feed_cursor import (
    feed_position,
    initial_feed_state,
    next_feed_state,
)
from .response import check_response, parse_response


def _as_count(*values: Any) -> int | None:
    """返回首个有效的服务端非负整数统计。"""
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 <= value <= 9_007_199_254_740_991:
            return value
        if isinstance(value, str) and value.isdecimal() and len(value) <= 16:
            number = int(value)
            if number <= 9_007_199_254_740_991:
                return number
    return None


def _image_urls(raw: Any) -> list[str] | None:
    """解析说说图片列表；字段缺失与确认无图分别保留。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise TypeError("说说图片字段结构异常")
    images: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("说说图片条目结构异常")
        for field in ("url3", "url2", "url1", "smallurl"):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                continue
            url = value.strip()
            if url.startswith("//"):
                url = f"https:{url}"
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            if url not in seen:
                images.append(url)
                seen.add(url)
            break
    return images


def _comment_identity(comment: dict[str, Any]) -> tuple[str, str, str]:
    """使用父评论、作者与评论 ID 组成楼中楼安全去重键。"""
    return (
        str(comment.get("parent_tid") if comment.get("parent_tid") is not None else ""),
        str(comment.get("comment_tid") if comment.get("comment_tid") is not None else ""),
        str(comment.get("qq_account") if comment.get("qq_account") is not None else ""),
    )


def _map_detail_comments(raw_comments: Iterable[Any], client: QZoneClientBase) -> list[dict[str, Any]]:
    """将详情页一级评论及内联回复映射为 FoxZone 消费者字段。"""
    result: list[dict[str, Any]] = []
    for raw in raw_comments:
        if not isinstance(raw, dict):
            raise TypeError("详情评论条目结构异常")
        parent_id = raw.get("tid")
        if parent_id is None or isinstance(parent_id, (dict, list)):
            raise ValueError("详情一级评论缺少有效 tid")
        parent_uin = raw.get("uin")
        mapped_parent = {
            "qq_account": parent_uin,
            "nickname": raw.get("name"),
            "content": raw.get("content"),
            "comment_tid": parent_id,
            "parent_tid": None,
            "parent_comment_id": None,
            "parent_comment_uin": None,
            "comment_id": parent_id,
            "uin": parent_uin,
            "name": raw.get("name"),
            "create_time": client._parse_comment_time(raw),
        }
        result.append(mapped_parent)
        replies = raw.get("list_3")
        if replies is None:
            continue
        if not isinstance(replies, list):
            raise TypeError("详情楼中楼字段结构异常")
        for reply in replies:
            if not isinstance(reply, dict):
                raise TypeError("详情楼中楼条目结构异常")
            reply_id = reply.get("tid")
            if reply_id is None or isinstance(reply_id, (dict, list)):
                raise ValueError("详情楼中楼缺少有效 tid")
            reply_uin = reply.get("uin")
            result.append(
                {
                    "qq_account": reply_uin,
                    "nickname": reply.get("name"),
                    "content": reply.get("content"),
                    "comment_tid": reply_id,
                    "parent_tid": parent_id,
                    "parent_comment_id": parent_id,
                    "parent_comment_uin": parent_uin,
                    "comment_id": reply_id,
                    "uin": reply_uin,
                    "name": reply.get("name"),
                    "create_time": client._parse_comment_time(reply),
                }
            )
    return result


def _monitor_comments(soup: bs4.BeautifulSoup) -> list[dict[str, Any]]:
    """映射动态 HTML 中的既有一级评论数据。"""
    comments: list[dict[str, Any]] = []
    for comment_div in soup.find_all("div", class_="single-reply"):
        if not isinstance(comment_div, Tag):
            continue
        qq_account = ""
        avatar = comment_div.find("div", class_="ui-avatar")
        if isinstance(avatar, Tag):
            avatar_link = avatar.find("a", href=True)
            if isinstance(avatar_link, Tag):
                match = re.search(r"qzone\.qq\.com/(\d+)", str(avatar_link["href"]))
                if match:
                    qq_account = match.group(1)
        nickname = ""
        nickname_link = comment_div.find("a", class_="nickname")
        if isinstance(nickname_link, Tag):
            nickname = nickname_link.get_text(strip=True)
        content = ""
        content_div = comment_div.find("div", class_="comments-content")
        if isinstance(content_div, Tag):
            content_soup = bs4.BeautifulSoup(str(content_div), "html.parser")
            for selector in (".comments-op", "a.nickname"):
                for node in content_soup.select(selector):
                    node.decompose()
            content = re.sub(r"\s+", " ", content_soup.get_text(" ", strip=True))
            content = re.sub(r"^:\s*", "", content).strip()
        comment_id = ""
        parent_comment_id: str | None = None
        parent_comment_uin: str | None = None
        reply_link = comment_div.find("a", class_="act-reply")
        if isinstance(reply_link, Tag):
            values = parse_qs(str(reply_link.get("data-param", "")))
            comment_id = values.get("t2_tid", [""])[0]
            if not qq_account:
                qq_account = values.get("t2_uin", [""])[0]
            parent_comment_id = values.get("t1_tid", [None])[0]
            parent_comment_uin = values.get("t1_uin", [None])[0]
        comments.append(
            {
                "qq_account": qq_account,
                "nickname": nickname,
                "content": content,
                "comment_tid": comment_id,
                "parent_tid": parent_comment_id,
                "parent_comment_id": parent_comment_id,
                "parent_comment_uin": parent_comment_uin,
                "comment_id": comment_id,
                "uin": qq_account,
                "name": nickname,
            }
        )
    return comments


def _has_more_feeds(data: dict[str, Any], main: dict[str, Any] | None) -> bool:
    """按 feeds CGI 规则读取结束标志，仅在 main 缺失时读 hasmore。"""
    value = main.get("hasMoreFeeds") if main is not None else data.get("hasmore")
    if value is True or (type(value) is int and value == 1) or value == "1":
        return True
    if value is False or (type(value) is int and value == 0) or value == "0":
        return False
    raise ValueError("好友动态响应缺少有效 hasMoreFeeds 标志")


def _feed_state_params(state: dict[str, str]) -> dict[str, str]:
    """将游标状态映射为 feeds3_html_more 的查询参数。"""
    params = dict(state)
    params["getAisort"] = state["aisortNextTime"]
    params.pop("aisortNextTime")
    return params


class FeedsMixin(QZoneClientBase):
    """说说列表 / 详情 / 好友动态流读取能力。"""

    async def list_feeds(
        self,
        target_qq: str | int,
        num: int,
        skip_commented: bool = True,
        paginate_comments: bool = True,
    ) -> list[dict[str, Any]]:
        """获取指定账号的说说；可选读取带完整性状态的详情评论分页。"""
        params: dict[str, Any] = {
            "g_tk": self._gtk,
            "uin": target_qq,
            "ftype": 0,
            "sort": 0,
            "pos": 0,
            "num": num,
            "replynum": 999,
            "code_version": 1,
            "format": "json",
            "need_comment": 1,
        }
        try:
            res_text = await self._request("GET", LIST_URL, params=params)
            json_data = parse_response(res_text)
            check_response(json_data, "msglist")
            msglist = json_data.get("msglist")
            if not isinstance(msglist, list):
                raise TypeError("说说列表响应缺少 msglist")

            my_name = (json_data.get("logininfo") or {}).get("name", "")
            feeds_list: list[dict[str, Any]] = []

            for msg_data in msglist:
                if not isinstance(msg_data, dict):
                    continue
                msg_tid = msg_data.get("tid", "")
                # 如果是读取好友说说且启用了过滤，跳过已评论项
                is_friend_feed = str(target_qq) != str(self._uin)
                if skip_commented and is_friend_feed:
                    comment_list = msg_data.get("commentlist") or []
                    if any(
                        isinstance(c, dict) and c.get("name") == my_name
                        for c in comment_list
                    ):
                        continue

                images_data = _image_urls(msg_data.get("pic"))
                inline_comments = msg_data.get("commentlist")
                if inline_comments is None:
                    inline_comments = []
                if not isinstance(inline_comments, list):
                    raise TypeError("说说内嵌评论字段结构异常")
                comments = _map_detail_comments(inline_comments, self)

                rt_raw = msg_data.get("rt_con", {})
                rt_content = (
                    rt_raw.get("content", "") if isinstance(rt_raw, dict) else ""
                )

                detail: dict[str, Any] | None = None
                if paginate_comments:
                    try:
                        detail = await self.fetch_feed_detail(
                            host_qq=str(target_qq), tid=str(msg_tid), paginate_comments=True
                        )
                    except RuntimeError as exc:
                        if "错误码: -3000" in str(exc):
                            raise
                        logger.warning(f"说说详情读取失败，保留列表数据 (tid={msg_tid}): {exc}")
                    except (aiohttp.ClientError, OSError, TimeoutError, ValueError, TypeError) as exc:
                        logger.warning(f"说说详情读取失败，保留列表数据 (tid={msg_tid}): {exc}")
                    if detail is not None:
                        comments = detail["comments"]

                content = msg_data.get("content")
                if not isinstance(content, str):
                    raise TypeError("说说正文字段结构异常")
                content_complete: bool | None = None
                comment_total = _as_count(msg_data.get("cmtnum"), msg_data.get("commentnum"))
                if detail is not None:
                    if detail["content"]:
                        content = detail["content"]
                        content_complete = detail["content_complete"]
                    if detail["images"] is not None:
                        images_data = list(dict.fromkeys([*(images_data or []), *detail["images"]]))
                    if detail["comment_count"] is not None:
                        comment_total = detail["comment_count"]

                feeds_list.append(
                    {
                        "tid": msg_tid,
                        "content": content,
                        "content_complete": content_complete,
                        "created_time": time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(int(msg_data.get("created_time") or 0))
                        ),
                        "rt_con": rt_content,
                        "images": images_data,
                        "comments": comments,
                        "comment_total": comment_total,
                        "comment_count": comment_total,
                        "comments_complete": (
                            detail["comments_complete"] if detail is not None else False
                        ),
                        "inline_replies_complete": False,
                    }
                )

            logger.info(
                f"[#F38BA8]从 QQ [#CBA6F7]{target_qq}[/#CBA6F7] 的空间"
                f"获取到 [#CBA6F7]{len(feeds_list)}[/#CBA6F7] 条说说[/#F38BA8]"
            )
            for f in feeds_list:
                logger.debug(
                    f"list_feeds 条目: tid={f.get('tid')} "
                    f"正文={str(f.get('content'))[:60]!r} "
                    f"图片数={len(f.get('images', []) or [])} "
                    f"评论数={len(f.get('comments', []) or [])}"
                )
            return feeds_list

        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"获取说说列表失败: {e}")
            raise

    async def _fetch_all_comments(
        self,
        host_qq: str,
        tid: str,
    ) -> list[dict[str, Any]]:
        """兼容旧调用者，返回详情中的已读取扁平评论列表。"""
        detail = await self.fetch_feed_detail(host_qq, tid)
        return list(detail["comments"]) if detail is not None else []

    async def fetch_feed_detail(
        self,
        host_qq: str,
        tid: str,
        paginate_comments: bool = True,
        comment_page_size: int = 20,
        max_comment_pages: int = 20,
    ) -> dict[str, Any] | None:
        """读取说说详情与有界一级评论页，返回服务端完整性状态。"""
        if not tid:
            raise ValueError("tid 不能为空")
        if comment_page_size < 1 or max_comment_pages < 1:
            raise ValueError("评论页大小和页数预算必须为正数")
        params: dict[str, Any] = {
            "uin": str(host_qq),
            "tid": str(tid),
            "t1_source": "1",
            "not_trunc_con": "1",
            "need_right": "1",
            "not_adapt_outpic": "1",
            "g_tk": self._gtk,
        }
        if paginate_comments:
            params.update(pos=0, num=comment_page_size)
        url = MSG_DETAIL_URL.replace("taotao.qzone.qq.com", "taotao.qq.com")
        headers = {"Referer": f"https://user.qzone.qq.com/{host_qq}"}

        async def request_page(pos: int) -> dict[str, Any]:
            """读取并校验一个详情评论页。"""
            page_params = dict(params)
            if paginate_comments:
                page_params.update(pos=pos, num=comment_page_size)
            text = await self._request("GET", url, params=page_params, headers=headers)
            response = parse_response(text)
            check_response(response, "msgdetail")
            raw = response.get("msglist")
            if isinstance(raw, list):
                if not raw or not isinstance(raw[0], dict):
                    raise ValueError("详情响应缺少说说对象")
                message = raw[0]
            else:
                message = response
            if str(message.get("tid", "")) != str(tid):
                raise ValueError("详情响应 tid 与请求不匹配")
            if not isinstance(message.get("content"), str):
                raise TypeError("详情响应正文结构异常")
            if message.get("commentlist") is not None and not isinstance(message["commentlist"], list):
                raise TypeError("详情评论页结构异常")
            return message

        first = await request_page(0)
        raw_images = _image_urls(first.get("pic"))
        comment_total = _as_count(
            first.get("cmtnum"), first.get("commentnum"), first.get("comment_num")
        )
        comments: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        seen_parents: set[tuple[str, str, str]] = set()
        comments_complete = False
        inline_replies_complete = False
        if not paginate_comments:
            initial = first.get("commentlist") or []
            comments = _map_detail_comments(initial, self)
        else:
            page = first
            pos = 0
            for page_index in range(max_comment_pages):
                raw_comments = page.get("commentlist") or []
                if not isinstance(raw_comments, list):
                    raise TypeError("详情评论页结构异常")
                mapped = _map_detail_comments(raw_comments, self)
                new_count = 0
                for comment in mapped:
                    identity = _comment_identity(comment)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    comments.append(comment)
                    if comment["parent_tid"] is None:
                        seen_parents.add(identity)
                        new_count += 1
                if not raw_comments:
                    comments_complete = comment_total is not None and len(seen_parents) == comment_total
                    break
                if new_count == 0:
                    break
                if comment_total is not None and len(seen_parents) >= comment_total:
                    comments_complete = len(seen_parents) == comment_total
                    break
                pos += len(raw_comments)
                if page_index + 1 >= max_comment_pages:
                    break
                try:
                    page = await request_page(pos)
                except RuntimeError as exc:
                    if "错误码: -3000" in str(exc):
                        raise
                    logger.warning(f"评论后续页读取中断（host={host_qq}, tid={tid}）: {exc}")
                    break
                except (aiohttp.ClientError, OSError, TimeoutError, ValueError, TypeError) as exc:
                    logger.warning(f"评论后续页读取中断（host={host_qq}, tid={tid}）: {exc}")
                    break
            inline_replies_complete = False

        created_raw = first.get("created_time", first.get("create_time", 0))
        try:
            created_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(created_raw or 0)))
        except (TypeError, ValueError, OverflowError, OSError):
            created_time = ""
        return {
            "tid": str(tid),
            "content": first["content"],
            "content_complete": True,
            "created_time": created_time,
            "images": raw_images,
            "comments": comments,
            "comment_total": comment_total,
            "comment_count": comment_total,
            "comments_complete": comments_complete,
            "inline_replies_complete": inline_replies_complete,
        }

    async def monitor_list_feeds(self, num: int) -> list[dict[str, Any]]:
        """获取好友动态流（用于监控场景）。

        Args:
            num: 获取数量

        Returns:
            好友动态字典列表

        Raises:
            RuntimeError: Cookie 失效（code=-3000）或 API 返回错误
        """
        if num < 1:
            return []
        page_size = min(10, num)
        page_budget = min(20, max(1, ((num + page_size - 1) // page_size) * 4))
        state = initial_feed_state()
        seen_positions = {feed_position(state)}
        seen_feeds: set[tuple[str, str, str]] = set()
        feeds_list: list[dict[str, Any]] = []
        try:
            for page_number in range(1, page_budget + 1):
                params: dict[str, Any] = {
                    "uin": self._uin,
                    "scope": 0,
                    "view": 1,
                    "gid": "",
                    "flag": 1,
                    "filter": "all",
                    "applist": "all",
                    "refresh": 0,
                    "firstGetGroup": 0,
                    "mixnocache": 0,
                    "scene": 0,
                    "sidomain": "qzonestyle.gtimg.cn",
                    "count": page_size,
                    **_feed_state_params(state),
                    "g_tk": self._gtk,
                    "useutf8": 1,
                    "outputhtmlfeed": 1,
                    "callback": "_preloadCallback",
                    "format": "jsonp",
                }
                params["usertime"] = int(time.time() * 1000)
                params["rd"] = str(time.time_ns())
                params["windowId"] = f"feed-{page_number}-{time.time_ns()}"
                response = parse_response(
                    await self._request("GET", ZONE_LIST_URL, params=params)
                )
                check_response(response, "feeds3_html_more")
                data = response.get("data")
                if not isinstance(data, dict):
                    raise TypeError("好友动态响应缺少 data 对象")
                main_value = data.get("main")
                if "main" in data and not isinstance(main_value, dict):
                    raise TypeError("好友动态分页状态结构异常")
                main = main_value if isinstance(main_value, dict) else None
                if main is not None and "error" in main and main["error"] != "":
                    raise RuntimeError("好友动态服务端报告读取失败")
                raw_feeds = data.get("data")
                if not isinstance(raw_feeds, list):
                    raise TypeError("好友动态响应缺少动态列表")
                has_more = _has_more_feeds(data, main)
                if has_more and main is None:
                    raise ValueError("好友动态继续分页但缺少 main 状态")

                for feed in raw_feeds:
                    if feed is None:
                        continue
                    if not isinstance(feed, dict):
                        raise TypeError("好友动态条目结构异常")
                    if str(feed.get("appid", "")) != "311":
                        continue
                    target_qq_str = str(feed.get("uin", ""))
                    feed_key = feed.get("key", feed.get("feedskey", ""))
                    html_content = feed.get("html")
                    if (
                        not target_qq_str
                        or target_qq_str == str(self._uin)
                        or not isinstance(feed_key, str)
                        or not feed_key
                        or not isinstance(html_content, str)
                        or not html_content
                    ):
                        continue
                    structured = parse_feed_content(
                        html_content,
                        feed_key,
                        target_qq_str,
                        311,
                        feed.get("likecnt"),
                        feed.get("commentcnt"),
                    )
                    verified_tid = structured["tid"]
                    identity = (str(self._uin), target_qq_str, str(verified_tid or feed_key))
                    if identity in seen_feeds:
                        continue
                    seen_feeds.add(identity)
                    try:
                        abstime = int(feed.get("abstime") or 0)
                    except (TypeError, ValueError):
                        abstime = 0
                    soup = bs4.BeautifulSoup(html_content, "html.parser")
                    like_button = soup.find("a", class_="qz_like_btn_v3")
                    liked = isinstance(like_button, Tag) and like_button.get("data-islike") == "1"
                    feeds_list.append(
                        {
                            "target_qq": target_qq_str,
                            "tid": verified_tid,
                            "content": structured["content"],
                            "content_complete": structured["content_complete"],
                            "created_time": time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(abstime)
                            ) if abstime > 0 else "",
                            "images": structured["images"],
                            "like_count": structured["like_count"],
                            "comment_count": _as_count(
                                feed.get("commentcnt"), feed.get("commentnum")
                            ),
                            "comments": _monitor_comments(soup),
                            "comments_complete": False,
                            "inline_replies_complete": False,
                            "liked": liked,
                            "html": html_content,
                            "key": feed_key,
                        }
                    )
                    if len(feeds_list) >= num:
                        break
                if len(feeds_list) >= num or not has_more:
                    break
                next_state = next_feed_state(main, state)
                next_position = feed_position(next_state)
                if next_position in seen_positions:
                    raise RuntimeError("好友动态分页游标未推进")
                seen_positions.add(next_position)
                state = next_state
                if page_number == page_budget and len(feeds_list) < num:
                    raise RuntimeError("好友动态分页超出本次读取预算")

            logger.info(
                f"[#F38BA8]监控发现 [#CBA6F7]{len(feeds_list)}[/#CBA6F7]"
                f" 条未处理的新说说[/#F38BA8]"
            )
            for f in feeds_list:
                logger.debug(
                    f"monitor_list_feeds 条目: target_qq={f.get('target_qq')} "
                    f"tid={f.get('tid')} 正文={str(f.get('content'))[:60]!r} "
                    f"图片数={len(f.get('images', []) or [])}"
                )
            return feeds_list

        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"监控好友动态失败: {e}")
            raise
