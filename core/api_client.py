"""QQ 空间 HTTP API 客户端。

将旧的闭包字典重构为类型安全的正式类，内聚所有 QQ 空间 HTTP 请求逻辑。
"""

from __future__ import annotations

import base64
import time
from typing import Any

import aiohttp
import bs4
import json5
import orjson

from src.app.plugin_system.api.log_api import get_logger, COLOR

logger = get_logger("foxzone.api_client", color=COLOR.ORANGE)

# QQ 空间 API 端点定义
_ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
_EMOTION_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
_DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
_COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
_LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
# 回复使用 h5 子域名（与评论接口不同域名）
_REPLY_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
# 单条说说详情（按 tid 精确查询，含评论区 list_3 楼中楼） 当前评论拉取主接口
_MSG_DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"

# 通用 Chrome 请求头
_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"


class QZoneAPIClient:
    """QQ 空间 HTTP API 客户端（有状态，持有 Cookie/gtk/uin 上下文）。

    使用工厂方法 :meth:`create` 从 Cookie 字典构建实例。
    所有 API 方法在 Cookie 失效（code=-3000）时抛出 ``RuntimeError``，
    由上层的 ``QZoneService._with_client`` 统一处理重试。

    Attributes:
        _cookies: Cookie 字典
        _gtk: QQ 空间 gtk 参数（由 p_skey 计算）
        _uin: QQ 号（去掉 uin Cookie 中的 "o" 前缀）
    """

    def __init__(self, cookies: dict[str, str], gtk: str, uin: str) -> None:
        """初始化 API 客户端。

        Args:
            cookies: Cookie 字典
            gtk: 预计算的 gtk 参数
            uin: QQ 号字符串（不含 "o" 前缀）
        """
        self._cookies = cookies
        self._gtk = gtk
        self._uin = uin

    @classmethod
    def create(cls, cookies: dict[str, str]) -> "QZoneAPIClient":
        """从 Cookie 字典创建 API 客户端。

        自动从 Cookie 中提取 p_skey 计算 gtk，以及提取 uin。

        Args:
            cookies: 完整的 QQ 空间 Cookie 字典

        Returns:
            配置好的 QZoneAPIClient 实例

        Raises:
            ValueError: Cookie 缺少必要字段（p_skey 或 uin）
        """
        p_skey = cookies.get("p_skey") or cookies.get("P_SKEY", "")
        if not p_skey:
            raise ValueError("Cookie 中缺少关键字段 'p_skey'。")

        gtk = cls._generate_gtk(p_skey)
        uin = cookies.get("uin", "").lstrip("o")
        if not uin:
            raise ValueError("Cookie 中缺少关键字段 'uin'。")

        return cls(cookies, gtk, uin)

    @staticmethod
    def _generate_gtk(skey: str) -> str:
        """通过 p_skey 计算 QQ 空间 gtk 参数。

        Args:
            skey: Cookie 中的 p_skey 值

        Returns:
            gtk 参数字符串
        """
        hash_val = 5381
        for char in skey:
            hash_val += (hash_val << 5) + ord(char)
        return str(hash_val & 2_147_483_647)

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """发送 HTTP 请求并返回响应文本。

        Args:
            method: HTTP 方法（GET / POST）
            url: 请求 URL
            params: 查询参数
            data: 表单数据（POST 时使用）
            headers: 额外请求头（会覆盖默认头）

        Returns:
            响应文本

        Raises:
            aiohttp.ClientResponseError: HTTP 请求失败
        """
        final_headers: dict[str, str] = {
            "User-Agent": _CHROME_UA,
            "Referer": f"https://user.qzone.qq.com/{self._uin}",
            "Origin": "https://user.qzone.qq.com",
            "Connection": "keep-alive",
        }
        # 注意：不设置 Host 头，让 aiohttp 自动从实际 URL 推导。
        # 之前硬编码 Host=user.qzone.qq.com 会与 h5.qzone.qq.com / taotao.qq.com
        # 等接口 URL 不匹配，触发 QZone 服务端反爬识别，返回 -10049 限流降级。
        if headers:
            final_headers.update(headers)

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(cookies=self._cookies) as session:
            async with session.request(
                method,
                url,
                params=params,
                data=data,
                headers=final_headers,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
                # QQ 空间偶尔会在响应开头带 UTF-8 BOM，统一剥离避免 orjson/json 解析失败
                if text.startswith("\ufeff"):
                    text = text.lstrip("\ufeff")
                return text

    # ------------------------------------------------------------------
    # 图片相关私有方法
    # ------------------------------------------------------------------

    @staticmethod
    def _image_to_base64(image_bytes: bytes) -> str:
        """将图片字节转为 base64 字符串（QQ 空间上传格式）。

        Args:
            image_bytes: 图片二进制数据

        Returns:
            base64 编码字符串
        """
        return str(base64.b64encode(image_bytes))[2:-1]

    @staticmethod
    def _get_picbo_and_richval(upload_result: dict[str, Any]) -> tuple[str, str]:
        """从上传结果中提取 pic_bo 和 richval 参数。

        Args:
            upload_result: QQ 空间图片上传 API 的响应数据

        Returns:
            (picbo, richval) 元组

        Raises:
            ValueError: 上传结果格式不符合预期
        """
        if "ret" not in upload_result:
            raise ValueError("上传结果中缺少 'ret' 字段。")
        if upload_result["ret"] != 0:
            raise ValueError(f"图片上传失败：ret={upload_result['ret']}")

        url_str = upload_result["data"]["url"]
        picbo_spt = url_str.split("&bo=")
        if len(picbo_spt) < 2:
            raise ValueError("无法从上传 URL 中提取 picbo。")
        picbo = picbo_spt[1]

        d = upload_result["data"]
        richval = ",{},{},{},{},{},{},,{},{}".format(
            d["albumid"],
            d["lloc"],
            d["sloc"],
            d["type"],
            d["height"],
            d["width"],
            d["height"],
            d["width"],
        )
        return picbo, richval

    async def _upload_image(self, image_bytes: bytes, index: int) -> dict[str, str] | None:
        """上传单张图片到 QQ 空间。

        Args:
            image_bytes: 图片二进制数据
            index: 图片序号（仅用于日志）

        Returns:
            包含 ``pic_bo`` 和 ``richval`` 的字典；上传失败时返回 None
        """
        upload_url = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
        post_data = {
            "filename": "filename",
            "zzpanelkey": "",
            "uploadtype": "1",
            "albumtype": "7",
            "exttype": "0",
            "skey": self._cookies.get("skey", ""),
            "zzpaneluin": self._uin,
            "p_uin": self._uin,
            "uin": self._uin,
            "p_skey": self._cookies.get("p_skey", ""),
            "output_type": "json",
            "qzonetoken": "",
            "refer": "shuoshuo",
            "charset": "utf-8",
            "output_charset": "utf-8",
            "upload_hd": "1",
            "hd_width": "2048",
            "hd_height": "10000",
            "hd_quality": "96",
            "backUrls": (
                "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
            ),
            "url": f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={self._gtk}",
            "base64": "1",
            "picfile": self._image_to_base64(image_bytes),
        }
        hdrs = {
            "referer": f"https://user.qzone.qq.com/{self._uin}",
            "origin": "https://user.qzone.qq.com",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(cookies=self._cookies) as session:
                async with session.post(
                    upload_url, data=post_data, headers=hdrs, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"图片 {index + 1} 上传 HTTP 失败：{resp.status}")
                        return None

                    resp_text = await resp.text()
                    start = resp_text.find("{")
                    end = resp_text.rfind("}") + 1
                    if start == -1 or end == 0:
                        logger.error(f"图片 {index + 1} 上传响应无有效 JSON。")
                        return None

                    upload_result = orjson.loads(resp_text[start:end])
                    if upload_result.get("ret") != 0:
                        logger.error(f"图片 {index + 1} 上传失败：{upload_result}")
                        return None

                    picbo, richval = self._get_picbo_and_richval(upload_result)
                    logger.info(f"图片 {index + 1} 上传成功。")
                    return {"pic_bo": picbo, "richval": richval}

        except Exception as e:
            logger.error(f"上传图片 {index + 1} 时发生异常: {e}")
            return None

    # ------------------------------------------------------------------
    # 公开 API 方法
    # ------------------------------------------------------------------

    async def publish(self, content: str, images: list[bytes]) -> bool:
        """发布说说（支持带图）。

        Args:
            content: 说说正文
            images: 图片字节列表（可为空）

        Returns:
            True 表示发布成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）
        """
        post_data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": self._uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self._uin}",
        }

        if images:
            logger.info(f"开始上传 {len(images)} 张图片…")
            pic_bos: list[str] = []
            richvals: list[str] = []
            for i, img_bytes in enumerate(images):
                upload_result = await self._upload_image(img_bytes, i)
                if upload_result:
                    pic_bos.append(upload_result["pic_bo"])
                    richvals.append(upload_result["richval"])

            if pic_bos:
                post_data["pic_bo"] = ",".join(pic_bos)
                post_data["richtype"] = "1"
                post_data["richval"] = "\t".join(richvals)
                logger.info(f"将附带 {len(pic_bos)} 张图片发布说说。")
            else:
                logger.warning("所有图片上传失败，将改为发布纯文本。")

        try:
            res_text = await self._request(
                "POST", _EMOTION_PUBLISH_URL, params={"g_tk": self._gtk}, data=post_data
            )
            result = orjson.loads(res_text)
            if result.get("code") == -3000:
                raise RuntimeError(
                    f"发布说说失败: {result.get('message')} (错误码: -3000)"
                )
            tid = result.get("tid", "")
            if tid:
                logger.info(f"说说发布成功，tid: {tid}")
                return True
            else:
                logger.error(f"发布说说失败，API 返回: {result}")
                return False
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"发布说说异常: {e}")
            return False

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
            paginate_comments: 是否对每条说说调用评论分页接口补全长评论区。
                关闭时仅使用 msglist_v6 自带的 commentlist（含 list_3 楼中楼），
                请求量从 1+N 降为 1。已知评论分页接口 ``emotion_cgi_comment_list``
                在某些场景下会返回 500，关闭它可避免无效请求。

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
            res_text = await self._request("GET", _LIST_URL, params=params)
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
                # （"1"/"2"/...，而非 24 位 hex 全局 tid），导致后续 reply 命中 -10049。
                # msgdetail_v6 始终返回 hex 全局 tid，是楼中楼回复的可靠数据源。
                if paginate_comments:
                    fresh = await self._fetch_all_comments(
                        host_qq=str(target_qq),
                        tid=str(msg_tid),
                        initial_count=0,
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

            logger.info(f"从 QQ {target_qq} 的空间获取到 {len(feeds_list)} 条说说。")
            return feeds_list

        except RuntimeError:
            raise
        except Exception as e:
            import traceback
            logger.error(f"获取说说列表失败: {e}\n{traceback.format_exc()}")
            return []

    async def _fetch_all_comments(
        self,
        host_qq: str,
        tid: str,
        initial_count: int = 0,
        page_size: int = 20,  # 兼容旧签名，msgdetail 接口不分页（一次性拉所有评论）
    ) -> list[dict[str, Any]]:
        """通过 ``msgdetail_v6`` 接口获取说说的完整评论列表。

        历史方案演进：
        1. 旧 ``emotion_cgi_comment_list``：500 错误（接口已废弃）
        2. ``emotion_cgi_ic_getcomments``：仅返回 HTML 渲染片段，无结构化 tid
        3. **当前**：``emotion_cgi_msgdetail_v6`` 返回结构化 JSON，含 hex 全局 tid

        ``msgdetail_v6`` 一次性返回完整评论列表（含 list_3 楼中楼），无需分页。
        若接口因资源限制（-10004 等）失败，返回空列表，调用方降级使用 msglist_v6 数据。

        Args:
            host_qq: 说说主人 QQ 号
            tid: 说说 tid
            initial_count: 已有评论数，从此偏移起截取（兼容旧调用语义）
            page_size: 已废弃，保留参数兼容性

        Returns:
            从 initial_count 开始的评论列表（空列表表示接口不可用）
        """
        del page_size  # 不再使用
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
        if initial_count <= 0:
            return list(all_comments)
        return all_comments[initial_count:] if initial_count < len(all_comments) else []

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
        # astrbot_plugin_qzone 实测可用的极简参数集（不要追加其他字段，
        # 否则 QZone 返回 -10004 参数错误）。
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
                "GET", _MSG_DETAIL_URL, params=params, headers=headers
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

    async def comment(self, target_qq: str, feed_id: str, text: str) -> bool:
        """对指定说说发表评论。

        Args:
            target_qq: 目标 QQ 号
            feed_id: 说说 tid
            text: 评论内容

        Returns:
            True 表示评论成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）
        """
        data: dict[str, Any] = {
            "topicId": f"{target_qq}_{feed_id}__1",
            "uin": self._uin,
            "hostUin": target_qq,
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "format": "fs",
            "ref": "feeds",
            "content": text,
        }
        try:
            resp_text = await self._request(
                "POST", _COMMENT_URL, params={"g_tk": self._gtk}, data=data
            )
            try:
                resp_data = orjson.loads(resp_text)
                code = resp_data.get("code", -1)
                if code == 0:
                    return True
                if code == -3000:
                    raise RuntimeError(
                        f"评论失败: {resp_data.get('message')} (错误码: -3000)"
                    )
                logger.error(f"评论 API 返回失败: code={code}, message={resp_data.get('message')}")
                return False
            except orjson.JSONDecodeError:
                # 响应无法解析为 JSON，假定成功
                return True
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"评论说说异常: {e}")
            return False

    async def like(self, target_qq: str, feed_id: str) -> bool:
        """对指定说说点赞。

        Args:
            target_qq: 目标 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示点赞成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）
        """
        data: dict[str, Any] = {
            "opuin": self._uin,
            "unikey": f"http://user.qzone.qq.com/{target_qq}/mood/{feed_id}",
            "curkey": f"http://user.qzone.qq.com/{target_qq}/mood/{feed_id}",
            "from": 1,
            "appid": 311,
            "typeid": 0,
            "abstime": int(time.time()),
            "fid": feed_id,
            "active": 0,
            "format": "json",
            "fupdate": 1,
        }
        try:
            resp_text = await self._request(
                "POST", _DOLIKE_URL, params={"g_tk": self._gtk}, data=data
            )
            try:
                resp_data = orjson.loads(resp_text)
                code = resp_data.get("code", -1)
                if code == 0:
                    return True
                if code == -3000:
                    raise RuntimeError(
                        f"点赞失败: {resp_data.get('message')} (错误码: -3000)"
                    )
                logger.warning(
                    f"点赞 API 返回失败: code={code}, message={resp_data.get('message')}"
                )
                return False
            except orjson.JSONDecodeError:
                return True
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"点赞说说异常: {e}")
            return False

    async def reply(
        self,
        feed_id: str,
        host_qq: str,
        target_name: str,
        content: str,
        comment_tid: str,
        commenter_qq: str = "",
    ) -> bool:
        """回复指定评论（二级评论）。

        Args:
            feed_id: 说说 tid
            host_qq: 说说主人 QQ 号
            target_name: 被回复的评论者昵称
            content: 回复内容
            comment_tid: 被回复的评论 tid
            commenter_qq: 被回复的评论者 QQ 号（commentUin 字段）

        Returns:
            True 表示回复成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）
        """
        # content 必须包含 @ 提及格式（浏览器对二级评论强制如此），
        # 否则 QZone 反爬会以 -10049 拒绝。
        mentioned_content = (
            f"@{{uin:{commenter_qq},nick:{target_name},auto:1}} {content}"
            if commenter_qq
            else content
        )
        data: dict[str, Any] = {
            "topicId": f"{host_qq}_{feed_id}__1",
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "hostUin": host_qq,
            "isSignIn": "",
            "platformid": 52,
            "uin": self._uin,
            "format": "fs",
            "ref": "feeds",
            "content": mentioned_content,
            "commentId": comment_tid,
            # commentUin 是"操作者 uin"（即 bot 自己），不是评论作者 QQ
            "commentUin": self._uin,
            "richval": "",
            "richtype": "",
            "private": "0",
            "paramstr": "1",
            "qzreferrer": f"https://user.qzone.qq.com/{self._uin}",
        }
        reply_headers: dict[str, str] = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-CH-UA": '"Chromium";v="138", "Not(A:Brand";v="99", "Google Chrome";v="138"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Referer": f"https://user.qzone.qq.com/{self._uin}",
            "Origin": "https://user.qzone.qq.com",
        }
        try:
            resp_text = await self._request(
                "POST", _REPLY_URL, params={"g_tk": self._gtk}, data=data, headers=reply_headers
            )
            # 详细调试日志：输出请求关键字段和响应原文，便于诊断 reply 是否真发出
            logger.info(
                f"reply 调用: feed_id={feed_id}, host_qq={host_qq}, "
                f"commentId={comment_tid}, commentUin={commenter_qq}, "
                f"target_name={target_name!r}, content_len={len(content)}"
            )
            logger.debug(f"reply 响应原文: {resp_text[:500]}")
            try:
                resp_data = orjson.loads(resp_text)
                code = resp_data.get("code", -1)
                if code == 0:
                    # 成功也打印一下完整响应，便于核对
                    logger.info(
                        f"reply 接口返回 code=0: "
                        f"new_tid={resp_data.get('tid') or resp_data.get('commentid')}, "
                        f"raw_keys={list(resp_data.keys())}"
                    )
                    return True
                if code == -3000:
                    raise RuntimeError(
                        f"回复失败: {resp_data.get('message')} (错误码: -3000)"
                    )
                logger.error(
                    f"回复 API 返回失败: code={code}, "
                    f"message={resp_data.get('message')}, fid={feed_id}, "
                    f"raw={resp_text[:300]}"
                )
                return False
            except orjson.JSONDecodeError:
                # format=fs 模式下 QZone 返回的是 frame 桥接 HTML，里面嵌入
                # frameElement.callback({...}) 调用。需要从中提取 JSON 片段判断真实 code。
                import re as _re

                m = _re.search(
                    r"frameElement\.callback\s*\(\s*(\{[\s\S]*?\})\s*\)",
                    resp_text,
                )
                parsed_code: int | None = None
                parsed_msg: str = ""
                parsed_subcode: int | None = None
                if m:
                    candidate = m.group(1).replace("undefined", "null")
                    try:
                        parsed = orjson.loads(candidate)
                        if isinstance(parsed, dict):
                            parsed_code = parsed.get("code", parsed.get("ret"))
                            parsed_msg = str(
                                parsed.get("message") or parsed.get("msg") or ""
                            )
                            parsed_subcode = parsed.get("subcode")
                    except orjson.JSONDecodeError as je:
                        logger.debug(f"frame callback JSON 解析仍失败: {je}; 片段={candidate[:200]}")

                logger.warning(
                    f"reply 响应为 frame 桥接 HTML，提取结果: "
                    f"code={parsed_code}, subcode={parsed_subcode}, msg={parsed_msg!r}"
                )
                logger.debug(f"reply 完整响应原文: {resp_text}")

                if parsed_code == 0:
                    return True
                if parsed_code == -3000:
                    raise RuntimeError(
                        f"回复失败: {parsed_msg} (错误码: -3000)"
                    )
                if parsed_code == -10049:
                    # QZone 限流：retry 也徒劳，抛 RuntimeError 让上层判定为不可重试错误
                    raise RuntimeError(
                        f"QZone 限流（code=-10049, subcode={parsed_subcode}）：{parsed_msg}"
                    )
                if parsed_code is not None:
                    logger.error(
                        f"回复接口返回错误: code={parsed_code}, "
                        f"subcode={parsed_subcode}, msg={parsed_msg!r}"
                    )
                    return False
                logger.error(
                    f"reply 响应无法解析任何 code/msg 字段，视为失败。原文片段: {resp_text[:300]}"
                )
                return False
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"回复评论异常: {e}")
            return False

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
            res_text = await self._request("GET", _ZONE_LIST_URL, params=params)

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

                # 跳过已点赞的说说
                like_btn = soup.find("a", class_="qz_like_btn_v3")
                if (
                    isinstance(like_btn, bs4.Tag)
                    and like_btn.get("data-islike") == "1"
                ):
                    continue

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

                # 提取评论
                monitor_comments: list[dict[str, Any]] = []
                for comment_div in soup.find_all("div", class_="f-single-comment"):
                    if not isinstance(comment_div, bs4.Tag):
                        continue
                    author_a = comment_div.find("a", class_="f-nick")
                    content_span = comment_div.find("span", class_="f-re-con")
                    if isinstance(author_a, bs4.Tag) and isinstance(content_span, bs4.Tag):
                        monitor_comments.append(
                            {
                                "qq_account": str(comment_div.get("data-uin", "")),
                                "nickname": author_a.get_text(strip=True),
                                "content": content_span.get_text(strip=True),
                                "comment_tid": comment_div.get("data-tid", ""),
                                "parent_tid": None,
                            }
                        )

                feeds_list.append(
                    {
                        "target_qq": target_qq_str,
                        "tid": tid,
                        "content": text,
                        "images": image_urls,
                        "comments": monitor_comments,
                    }
                )

            logger.info(f"监控发现 {len(feeds_list)} 条未处理的新说说。")
            return feeds_list

        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"监控好友动态失败: {e}")
            return []

    @staticmethod
    def _parse_comment_time(comment_data: dict[str, Any]) -> str:
        """从评论数据中解析格式化的时间字符串。

        优先使用 createTime2（YYYY-MM-DD HH:MM:SS 格式），
        其次将 create_time 时间戳转换为可读格式。

        Args:
            comment_data: 单条评论的数据字典

        Returns:
            格式化的时间字符串；解析失败时返回空字符串
        """
        if comment_data.get("createTime2"):
            return str(comment_data["createTime2"])

        raw_time = comment_data.get("create_time")
        if raw_time:
            try:
                ts = int(raw_time)
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except (ValueError, TypeError):
                pass
        return ""
