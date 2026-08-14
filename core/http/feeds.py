"""QZone 说说读取接口族（msglist_v6 / msgdetail_v6 / feeds3_html_more）。"""

from __future__ import annotations

import re
import time
from typing import Any

import bs4
import json5
import orjson

from .client import LIST_URL, MSG_DETAIL_URL, ZONE_LIST_URL, QZoneClientBase, logger


class FeedsMixin(QZoneClientBase):
    """说说列表 / 详情 / 好友动态流读取能力。"""

    async def list_feeds(
        self,
        target_qq: str | int,
        num: int,
        skip_commented: bool = True,
        paginate_comments: bool = True,
    ) -> list[dict[str, Any]]:
        """获取指定 QQ 用户的说说列表（含全部评论，自动分页补全）。

        Args:
            target_qq: 目标 QQ 号
            num: 获取数量
            skip_commented: 为 True 时跳过 Bot 已评论的说说（用于监控互动场景）；
                为 False 时返回全部说说（用于纯读取展示场景）
            paginate_comments: 是否对每条说说调用 msgdetail_v6 补全评论区。
                关闭时仅使用 msglist_v6 自带的 commentlist（含 list_3 楼中楼），
                请求量从 1+N 降为 1。

        Returns:
            说说数据字典列表

        Raises:
            RuntimeError: Cookie 失效（code=-3000）或 API 返回错误
        """
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
            json_data = orjson.loads(res_text)

            if json_data.get("code") != 0:
                code = json_data.get("code")
                msg = json_data.get("message", "未知错误")
                raise RuntimeError(f"QQ 空间 API 错误: {msg} (错误码: {code})")

            my_name = (json_data.get("logininfo") or {}).get("name", "")
            feeds_list: list[dict[str, Any]] = []

            for msg_data in (json_data.get("msglist") or []):
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

                # 提取图片 URL
                images_data: list[str] = []
                for key in ("pic", "pictotal"):
                    if isinstance(msg_data.get(key), list):
                        images_data = [
                            p.get("url1", "")
                            for p in msg_data[key]
                            if p.get("url1")
                        ]
                        if images_data:
                            break

                # 解析评论列表
                comments: list[dict[str, Any]] = []
                for c in msg_data.get("commentlist") or []:
                    if not isinstance(c, dict):
                        continue
                    create_time = self._parse_comment_time(c)
                    comments.append(
                        {
                            "qq_account": c.get("uin"),
                            "nickname": c.get("name"),
                            "content": c.get("content"),
                            "comment_tid": c.get("tid"),
                            "parent_tid": None,
                            "create_time": create_time,
                        }
                    )
                    # 二级评论
                    for reply in c.get("list_3") or []:
                        if not isinstance(reply, dict):
                            continue
                        reply_time = self._parse_comment_time(reply)
                        comments.append(
                            {
                                "qq_account": reply.get("uin"),
                                "nickname": reply.get("name"),
                                "content": reply.get("content"),
                                "comment_tid": reply.get("tid"),
                                "parent_tid": c.get("tid"),
                                "create_time": reply_time,
                            }
                        )

                rt_raw = msg_data.get("rt_con", {})
                rt_content = (
                    rt_raw.get("content", "") if isinstance(rt_raw, dict) else ""
                )

                # 通过 msgdetail_v6 接口拉取完整评论列表，覆盖 msglist_v6 内嵌的评论。
                # msglist_v6 返回的 commentlist 中，主评论 tid 在某些场景下是局部序号
                # （"1"/"2"/...，而非 24 位 hex 全局 tid），会导致后续 reply 触发 -10049；
                # msgdetail_v6 始终返回 hex 全局 tid。
                if paginate_comments:
                    fresh = await self._fetch_all_comments(
                        host_qq=str(target_qq),
                        tid=str(msg_tid),
                    )
                    if fresh:
                        comments = fresh

                feeds_list.append(
                    {
                        "tid": msg_tid,
                        "content": msg_data.get("content", ""),
                        "created_time": time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.localtime(msg_data.get("created_time", 0)),
                        ),
                        "rt_con": rt_content,
                        "images": images_data,
                        "comments": comments,
                        "comment_total": int(msg_data.get("commentnum", len(comments))),
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
            return []

    async def _fetch_all_comments(
        self,
        host_qq: str,
        tid: str,
    ) -> list[dict[str, Any]]:
        """通过 ``msgdetail_v6`` 接口获取说说的完整评论列表。

        ``msgdetail_v6`` 一次性返回完整评论列表（含 list_3 楼中楼），
        且其中的评论 tid 为 hex 全局 tid，可作为 reply 接口的 commentId。
        若接口失败（如 -10004 资源限制），返回空列表，由调用方降级使用
        msglist_v6 内嵌评论数据。

        Args:
            host_qq: 说说主人 QQ 号
            tid: 说说 tid

        Returns:
            完整评论列表；接口不可用时返回空列表
        """
        try:
            detail = await self.fetch_feed_detail(str(host_qq), str(tid))
        except Exception as exc:
            logger.debug(f"msgdetail 调用异常（host={host_qq}, tid={tid}）: {exc}")
            return []

        if not detail:
            return []
        all_comments = detail.get("comments") or []
        if not isinstance(all_comments, list):
            return []
        return list(all_comments)

    async def fetch_feed_detail(
        self, host_qq: str, tid: str
    ) -> dict[str, Any] | None:
        """按 tid 精准查询单条说说详情（含评论区 list_3 楼中楼）。

        使用 ``emotion_cgi_msgdetail_v6`` 接口，规避 ``emotion_cgi_comment_list``
        在部分账号上 500 的故障。一次请求即可拿到这条 feed 的正文、图片
        与完整评论区，是「按 InteractionLog 标记精准回查」的最优路径。

        Args:
            host_qq: 说说主人 QQ 号
            tid: 说说 tid

        Returns:
            形如 ``{"tid", "content", "created_time", "images", "comments",
            "comment_total"}`` 的 dict；接口失败或未找到时返回 None。
        """
        # QZone 只接受该参数集；追加其他字段会返回 -10004 参数错误。
        params: dict[str, Any] = {
            "uin": str(host_qq),
            "tid": str(tid),
            "format": "jsonp",
            "g_tk": self._gtk,
        }
        # h5 子域，需要覆盖 Host/Referer
        headers = {
            "Host": "h5.qzone.qq.com",
            "Referer": f"https://h5.qzone.qq.com/mqzone/index?_proxy=1&hostuin={host_qq}",
        }
        try:
            res_text = await self._request(
                "GET", MSG_DETAIL_URL, params=params, headers=headers
            )
            # format=jsonp 返回 `_Callback({...});` 或 `_Callback({...})`，需剥外壳
            stripped = res_text.strip()
            if stripped.startswith("_Callback(") and stripped.endswith(");"):
                json_str = stripped[len("_Callback("):-2]
            elif stripped.startswith("_Callback(") and stripped.endswith(")"):
                json_str = stripped[len("_Callback("):-1]
            else:
                json_str = stripped
            json_data = orjson.loads(json_str)
        except Exception as exc:
            logger.warning(
                f"按 tid 拉取说说详情失败（host={host_qq}, tid={tid}）: {exc}"
            )
            return None

        code = json_data.get("code")
        if code != 0:
            logger.debug(
                f"msgdetail 返回非 0（host={host_qq}, tid={tid}）: "
                f"code={code}, message={json_data.get('message')}"
            )
            return None

        # msgdetail 返回结构有两种已知形态：根直接是 msg；或 {"msglist": [...]}.
        msg_data: dict[str, Any] | None = None
        if isinstance(json_data.get("msglist"), list) and json_data["msglist"]:
            msg_data = json_data["msglist"][0]
        elif json_data.get("tid") or json_data.get("content") is not None:
            msg_data = json_data
        if not msg_data:
            return None

        # 图片
        images_data: list[str] = []
        for key in ("pic", "pictotal"):
            raw = msg_data.get(key)
            if isinstance(raw, list):
                images_data = [p.get("url1", "") for p in raw if p.get("url1")]
                if images_data:
                    break

        # 评论区
        comments: list[dict[str, Any]] = []
        for c in msg_data.get("commentlist") or []:
            if not isinstance(c, dict):
                continue
            create_time = self._parse_comment_time(c)
            comments.append(
                {
                    "qq_account": c.get("uin"),
                    "nickname": c.get("name"),
                    "content": c.get("content"),
                    "comment_tid": c.get("tid"),
                    "parent_tid": None,
                    "create_time": create_time,
                }
            )
            for reply in c.get("list_3") or []:
                if not isinstance(reply, dict):
                    continue
                reply_time = self._parse_comment_time(reply)
                comments.append(
                    {
                        "qq_account": reply.get("uin"),
                        "nickname": reply.get("name"),
                        "content": reply.get("content"),
                        "comment_tid": reply.get("tid"),
                        "parent_tid": c.get("tid"),
                        "create_time": reply_time,
                    }
                )

        return {
            "tid": str(msg_data.get("tid", tid)),
            "content": msg_data.get("content", ""),
            "created_time": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(msg_data.get("created_time", 0)),
            ),
            "images": images_data,
            "comments": comments,
            "comment_total": int(msg_data.get("commentnum", len(comments))),
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
        params: dict[str, Any] = {
            "uin": self._uin,
            "scope": 0,
            "view": 1,
            "filter": "all",
            "flag": 1,
            "applist": "all",
            "pagenum": 1,
            "count": num,
            "format": "json",
            "g_tk": self._gtk,
            "useutf8": 1,
            "outputhtmlfeed": 1,
        }
        try:
            res_text = await self._request("GET", ZONE_LIST_URL, params=params)

            # 处理 JSONP 响应格式
            stripped = res_text.strip()
            if stripped.startswith("_Callback(") and stripped.endswith(");"):
                json_str = stripped[len("_Callback("):-2]
            elif stripped.startswith("{"):
                json_str = stripped
            else:
                logger.warning(f"意外的监控响应格式: {res_text[:100]}")
                return []

            json_str = json_str.replace("undefined", "null").strip()

            try:
                json_data = json5.loads(json_str)
            except Exception as e:
                logger.error(f"监控响应 JSON 解析失败: {e}")
                return []

            if not isinstance(json_data, dict):
                return []

            if json_data.get("code") != 0:
                code = json_data.get("code")
                msg = json_data.get("message", "未知错误")
                raise RuntimeError(f"QQ 空间 API 错误: {msg} (错误码: {code})")

            feeds_raw = (
                json_data.get("data", {}).get("data", [])
                if isinstance(json_data.get("data"), dict)
                else []
            )

            feeds_list: list[dict[str, Any]] = []
            for feed in feeds_raw:
                if not isinstance(feed, dict):
                    continue
                if str(feed.get("appid", "")) != "311":
                    continue

                target_qq_str = str(feed.get("uin", ""))
                tid = feed.get("key", "")
                html_content = feed.get("html", "")

                if not target_qq_str or not tid or not html_content:
                    continue
                if target_qq_str == str(self._uin):
                    continue

                soup = bs4.BeautifulSoup(html_content, "html.parser")

                # 提取点赞状态（已点赞的说说也返回，交由上层/LLM 决策是否再互动）
                like_btn = soup.find("a", class_="qz_like_btn_v3")
                liked = (
                    isinstance(like_btn, bs4.Tag)
                    and like_btn.get("data-islike") == "1"
                )

                text_div = soup.find("div", class_="f-info")
                text = (
                    text_div.get_text(strip=True)
                    if isinstance(text_div, bs4.Tag)
                    else ""
                )

                # 提取图片
                image_urls: list[str] = []
                img_box = soup.find("div", class_="img-box")
                if isinstance(img_box, bs4.Tag):
                    for img in img_box.find_all("img"):
                        if isinstance(img, bs4.Tag):
                            src = img.get("src")
                            if src and "qzonestyle.gtimg.cn" not in str(src):
                                image_urls.append(str(src))
                video_thumb = soup.select_one("div.video-img img")
                if isinstance(video_thumb, bs4.Tag) and "src" in video_thumb.attrs:
                    image_urls.append(str(video_thumb["src"]))
                image_urls = list(set(image_urls))

                # 提取评论：feeds3_html_more 的评论区使用 single-reply 结构，
                # 每条含头像 href（QQ）、.nickname（昵称）、.comments-content（内容），
                # 以及 act-reply 的 data-param 中的 t2_tid（评论 tid）/ t2_uin（评论者）。
                monitor_comments: list[dict[str, Any]] = []
                for comment_div in soup.find_all("div", class_="single-reply"):
                    if not isinstance(comment_div, bs4.Tag):
                        continue
                    # QQ 从头像 href 提取（user.qzone.qq.com/<qq>）
                    qq_account = ""
                    avatar = comment_div.find("div", class_="ui-avatar")
                    if isinstance(avatar, bs4.Tag):
                        avatar_a = avatar.find("a", href=True)
                        if avatar_a is not None:
                            href = str(avatar_a["href"])
                            m = re.search(r"qzone\.qq\.com/(\d+)", href)
                            if m:
                                qq_account = m.group(1)
                    # 昵称
                    nickname = ""
                    nick_a = comment_div.find("a", class_="nickname")
                    if isinstance(nick_a, bs4.Tag):
                        nickname = nick_a.get_text(strip=True)
                    # 内容：comments-content 移除昵称锚点与操作区（comments-op）后的正文
                    content = ""
                    content_div = comment_div.find("div", class_="comments-content")
                    if isinstance(content_div, bs4.Tag):
                        # 用字符串重建副本，避免 extract 影响原 DOM
                        content_soup = bs4.BeautifulSoup(
                            str(content_div), "html.parser"
                        )
                        op_div = content_soup.find("div", class_="comments-op")
                        if op_div is not None:
                            op_div.extract()
                        nick_node = content_soup.find("a", class_="nickname")
                        if nick_node is not None:
                            nick_node.extract()
                        content = content_soup.get_text(" ", strip=True)
                        # 归一化空白（HTML 含大量制表符/换行）
                        content = re.sub(r"\s+", " ", content).strip()
                        # 去掉可能残留的 ":" 前缀
                        content = re.sub(r"^:\s*", "", content).strip()
                    # 评论 tid 与评论者：act-reply 的 data-param 中 t2_tid / t2_uin
                    comment_tid = ""
                    reply_a = comment_div.find("a", class_="act-reply")
                    if isinstance(reply_a, bs4.Tag):
                        data_param = str(reply_a.get("data-param", ""))
                        m_tid = re.search(r"t2_tid=([^&\s]+)", data_param)
                        if m_tid:
                            comment_tid = m_tid.group(1)
                        if not qq_account:
                            m_uin = re.search(r"t2_uin=([^&\s]+)", data_param)
                            if m_uin:
                                qq_account = m_uin.group(1)
                    monitor_comments.append(
                        {
                            "qq_account": qq_account,
                            "nickname": nickname,
                            "content": content,
                            "comment_tid": comment_tid,
                            "parent_tid": None,
                        }
                    )

                # 发布时间：JSON 顶层 abstime（Unix 秒），转成与 list_feeds 一致的
                # "YYYY-MM-DD HH:MM:SS"，供 LLM 提示词 format_story_time 解析。
                abstime_raw = feed.get("abstime", 0)
                try:
                    abstime = int(abstime_raw) if abstime_raw else 0
                except (TypeError, ValueError):
                    abstime = 0
                created_time = (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(abstime))
                    if abstime > 0
                    else ""
                )

                feeds_list.append(
                    {
                        "target_qq": target_qq_str,
                        "tid": tid,
                        "content": text,
                        "created_time": created_time,
                        "images": image_urls,
                        "comments": monitor_comments,
                        "liked": liked,
                    }
                )

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
            return []
