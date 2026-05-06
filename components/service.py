"""QZoneService 组件：作为 BaseService 暴露 QQ 空间 API 能力。"""

from __future__ import annotations

import asyncio
import random
import time
import typing
from pathlib import Path
from typing import Any

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.base import BaseService

from ..config import FoxZoneConfig
from ..core.api_client import QZoneAPIClient
from ..core.content import ContentService, log_llm_prompt
from ..core.cookie import CookieService
from ..core.image.dispatcher import ImageDispatcher
from ..core.reply_tracker import ReplyTrackerService
from ..core.vision_cache import ImageVisionCache
from ..core.interaction_log import ACTION_COMMENT, ACTION_VISITED, SOURCE_AGENT, InteractionLog
from ..prompts import FEED_GUIDANCE_TEMPLATE

if typing.TYPE_CHECKING:
    from ..plugin import FoxZonePlugin

logger = get_logger("foxzone.service", color=COLOR.ORANGE)


def resolve_root_comment_tid(
    all_comments: list[dict[str, Any]], current_tid: str
) -> str:
    """沿 parent_tid 链向上找到顶层一级评论的 tid（QZone 楼中楼回复需要根 tid）。

    带循环保护（最多 10 跳）；任一环节查不到时降级返回当前已知 tid。

    Args:
        all_comments: 该说说下的全部评论（包含 list_3 二级回复，平铺为字典列表）
        current_tid: 要回复的目标评论 tid

    Returns:
        顶层一级评论 tid（字符串）
    """
    root_tid, _ = resolve_root_comment(all_comments, current_tid)
    return root_tid


def resolve_root_comment(
    all_comments: list[dict[str, Any]], current_tid: str
) -> tuple[str, str]:
    """沿 parent_tid 链向上找到顶层一级评论的 (tid, qq_account)。

    QZone 楼中楼回复 API 同时需要 commentId（顶层一级评论 tid）和
    commentUin（顶层一级评论作者 uin）。两者都必须指向同一根节点，
    否则 QZone 服务端会以 -10049 拒绝。

    Args:
        all_comments: 该说说下的全部评论（含 list_3 二级回复，平铺）
        current_tid: 要回复的目标评论 tid

    Returns:
        (root_tid, root_uin)
    """
    cur_tid = str(current_tid).strip()
    if not cur_tid:
        return cur_tid, ""
    by_tid: dict[str, dict[str, Any]] = {}
    for c in all_comments:
        ctid = str(c.get("comment_tid") or "").strip()
        if ctid:
            by_tid[ctid] = c
    seen: set[str] = set()
    cursor: dict[str, Any] | None = by_tid.get(cur_tid)
    last_known_tid = cur_tid
    last_known_uin = str((cursor or {}).get("qq_account") or "").strip() if cursor else ""
    for _ in range(10):
        if cursor is None or last_known_tid in seen:
            return last_known_tid, last_known_uin
        seen.add(last_known_tid)
        parent = str(cursor.get("parent_tid") or "").strip()
        if not parent:
            return last_known_tid, last_known_uin
        next_node = by_tid.get(parent)
        if next_node is None:
            # 找不到 parent 节点：返回 parent tid 但 uin 未知（保留当前已知 uin）
            return parent, last_known_uin
        last_known_tid = parent
        last_known_uin = str(next_node.get("qq_account") or "").strip()
        cursor = next_node
    return last_known_tid, last_known_uin


def is_local_seq_tid(tid: str) -> bool:
    """判断 tid 是否为 QZone list_3 二级评论的局部序号（如 ``"1"`` / ``"9"``）。

    QZone 一级评论 tid 是 24 位 hex 字符串（如 ``"8a38cf8285a4f9699eab0900"``），
    list_3 二级评论 tid 则是局部序号（纯数字、长度通常 < 10）。
    若 ``resolve_root_comment_tid`` 返回的根 tid 仍然是这种短数字形态，
    说明 ``all_comments`` 中缺该二级评论对应的一级父节点（msglist_v6 仅返
    回最近 5 条一级评论；翻页接口在部分账号上 500），此时把短数字当 commentId
    传给 reply 接口必然触发 -10049（订正使用人数过多/风控）。

    Args:
        tid: 待判定的评论 tid 字符串

    Returns:
        True 表示形似局部序号、不可作为 reply 接口的 commentId
    """
    s = str(tid).strip()
    return s.isdigit() and len(s) < 16


# 跨实例共享：串行化所有 reply 发送，详见 QZONE_API.md「外部回查实现要点」。
_REPLY_SEND_LOCK: asyncio.Lock = asyncio.Lock()


class QZoneService(BaseService):
    """QQ 空间统一服务出口。"""

    service_name = "qzone_service"
    service_description = "QQ 空间 API 服务（发布说说、读取动态、互动等）"

    def __init__(self, plugin: "FoxZonePlugin") -> None:  # type: ignore[override]
        """初始化 QZoneService。"""
        super().__init__(plugin)
        self._plugin: "FoxZonePlugin" = plugin  # type: ignore[assignment]
        self._cfg: FoxZoneConfig = plugin.config  # type: ignore[assignment]
        self._cookie = CookieService(self._cfg)
        self._content = ContentService(plugin)
        self._dispatcher = ImageDispatcher(plugin)
        self._reply_tracker = ReplyTrackerService()
        self._vision_cache = ImageVisionCache()
        self._interaction_log = InteractionLog()
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """初始化持久化状态。"""
        if self._initialized:
            return

        async with self._initialize_lock:
            if self._initialized:
                return
            await self._reply_tracker.initialize()
            await self._vision_cache.initialize()
            await self._interaction_log.initialize()
            self._initialized = True

    async def publish_feed(
        self,
        content: str,
        images: list[bytes] | None = None,
    ) -> bool:
        """发布一条说说。"""
        await self.initialize()
        clean_content = content.strip()
        if not clean_content:
            logger.warning("说说内容为空，已拒绝发布。")
            return False

        try:
            success = await self._with_client(
                lambda client: client.publish(clean_content, images or [])
            )
        except Exception as exc:
            logger.error(f"发布说说时发生异常: {exc}")
            return False

        if success:
            await self._save_send_history(clean_content)
        return success

    async def list_feeds(
        self,
        target_qq: str,
        num: int = 5,
        skip_commented: bool = True,
        paginate_comments: bool = True,
    ) -> list[dict[str, Any]]:
        """读取指定 QQ 的说说列表（含全部评论，自动分页补全）。

        Args:
            target_qq: 目标 QQ 号
            num: 读取条数
            skip_commented: 是否跳过 Bot 已评论的说说（监控互动场景传 True，纯读取展示传 False）
            paginate_comments: 是否对每条说说调用评论翻页接口补全长评论区。
                关闭时只用 msglist_v6 自带的 commentlist（含 list_3 楼中楼），
                请求量从 1+N 降为 1，规避 ``emotion_cgi_comment_list`` 500 故障。
        """
        await self.initialize()
        target = str(target_qq).strip()
        if not target:
            return []

        try:
            feeds = await self._with_client(
                lambda client: client.list_feeds(
                    target,
                    max(1, num),
                    skip_commented=skip_commented,
                    paginate_comments=paginate_comments,
                )
            )
        except Exception as exc:
            logger.error(f"读取 QQ {target} 的说说失败: {exc}")
            return []

        return feeds or []

    async def comment(self, target_qq: str, feed_id: str, text: str) -> bool:
        """评论指定说说。"""
        await self.initialize()
        if not text.strip():
            return False

        try:
            return await self._with_client(
                lambda client: client.comment(str(target_qq), str(feed_id), text.strip())
            )
        except Exception as exc:
            logger.error(f"评论说说失败: {exc}")
            return False

    async def like(self, target_qq: str, feed_id: str) -> bool:
        """点赞指定说说。"""
        await self.initialize()
        try:
            return await self._with_client(
                lambda client: client.like(str(target_qq), str(feed_id))
            )
        except Exception as exc:
            logger.error(f"点赞说说失败: {exc}")
            return False

    async def reply_comment(
        self,
        feed_id: str,
        host_qq: str,
        target_name: str,
        reply_text: str,
        comment_tid: str,
        commenter_qq: str = "",
    ) -> bool:
        """回复指定评论。"""
        await self.initialize()
        if not reply_text.strip():
            return False

        try:
            return await self._with_client(
                lambda client: client.reply(
                    str(feed_id),
                    str(host_qq),
                    target_name,
                    reply_text.strip(),
                    str(comment_tid),
                    str(commenter_qq),
                )
            )
        except RuntimeError:
            # 不可重试错误（cookie 失效 / -10049 限流）需向上传播，
            # 让批量处理器据此停止重试。
            raise
        except Exception as exc:
            logger.error(f"回复评论失败: {exc}")
            return False

    async def list_own_feeds_with_comments(self, num: int = 5) -> list[dict[str, Any]]:
        """读取自己的说说及全部评论。"""
        return await self.list_feeds(self._get_bot_qq(), num=num, skip_commented=False)

    async def get_recent_self_feeds_block(self, num: int = 3) -> str:
        """生成「自己最近 N 条说说」的完整上下文文本块。

        包括正文、发布时间、图片描述、评论区——形态与读取他人说说时一致，
        用于在「发说说」「回复自己说说下评论」时为 LLM 提供历史语境。

        Args:
            num: 取多少条最近的说说，默认 3 条

        Returns:
            格式化好的多行文本块；若无说说或读取失败返回空字符串。
        """
        try:
            feeds = await self.list_own_feeds_with_comments(num=max(1, num))
        except Exception as exc:
            logger.warning(f"读取自己最近说说失败: {exc}")
            return ""

        if not feeds:
            return ""

        # 收集图片 URL 批量识别（带缓存）
        all_image_urls: list[str] = []
        for feed in feeds:
            all_image_urls.extend(str(u) for u in feed.get("images", []) if u)
        image_descs: dict[str, str] = {}
        if all_image_urls:
            try:
                image_descs = await self.describe_images(all_image_urls)
            except Exception as exc:
                logger.warning(f"识别自己说说配图失败: {exc}")

        lines: list[str] = []
        for idx, feed in enumerate(feeds, start=1):
            lines.append(self._format_self_feed(idx, feed, image_descs))
        return "\n".join(lines).strip()

    @staticmethod
    def _format_self_feed(
        idx: int,
        feed: dict[str, Any],
        image_descs: dict[str, str] | None = None,
    ) -> str:
        """格式化单条「自己的说说」为带评论的完整文本块。"""
        tid = str(feed.get("tid", "")).strip()
        content = str(feed.get("content") or feed.get("rt_con") or "（无正文）").strip()
        created_time = str(feed.get("created_time", "")).strip()
        images: list[str] = feed.get("images", []) or []
        comments: list[dict[str, Any]] = feed.get("comments", []) or []

        parts: list[str] = []
        header = f"【我的说说 {idx}】"
        if created_time:
            header += f"  {created_time}"
        if tid:
            header += f"  (tid={tid})"
        parts.append(header)
        parts.append(f"正文：{content}")

        if images:
            descs = image_descs or {}
            for i, url in enumerate(images, start=1):
                desc = descs.get(str(url), "").strip()
                parts.append(f"图片{i}：{desc}" if desc else f"图片{i}：（内容未识别）")

        if comments:
            parts.append(f"评论（共 {len(comments)} 条）：")
            for c in comments:
                nickname = str(c.get("nickname", "")).strip() or "匿名"
                qq = str(c.get("qq_account", "")).strip()
                ctime = str(c.get("create_time", "")).strip()
                ctext = str(c.get("content", "")).strip()
                line = f"  · [{nickname}"
                if qq:
                    line += f"({qq})"
                line += "]"
                if ctime:
                    line += f" {ctime}"
                line += f"：{ctext}"
                parts.append(line)
        else:
            parts.append("评论：暂无")

        parts.append("")  # 段落分隔
        return "\n".join(parts)

    async def has_replied_comment(self, feed_id: str, comment_tid: str) -> bool:
        """检查是否已回复过指定评论。"""
        await self.initialize()
        return self._reply_tracker.has_replied(feed_id, comment_tid)

    async def mark_comment_replied(self, feed_id: str, comment_tid: str) -> None:
        """标记评论已回复。"""
        await self.initialize()
        await self._reply_tracker.mark_as_replied(feed_id, comment_tid)

    # ------------------------------------------------------------------
    # 互动记录（点赞/评论好友说说）
    # ------------------------------------------------------------------

    def has_interacted(self, target_qq: str, feed_id: str) -> bool:
        """判断是否已与该说说有过任何互动（点赞或评论）。

        Args:
            target_qq: 说说主人 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示已有互动记录
        """
        return self._interaction_log.has_interacted(target_qq, feed_id)

    def has_visited(self, target_qq: str, feed_id: str) -> bool:
        """判断 Agent 是否已处理过该说说（无论是否实际互动）。

        Args:
            target_qq: 说说主人 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示已处理，无需再次触发
        """
        return self._interaction_log.has_visited(target_qq, feed_id)

    async def mark_visited(self, target_qq: str, feed_id: str) -> None:
        """标记说说已由 Agent 处理（不代表有点赞/评论）。

        Args:
            target_qq: 说说主人 QQ 号
            feed_id: 说说 tid
        """
        await self.initialize()
        self._interaction_log.mark(target_qq, feed_id, ACTION_VISITED, SOURCE_AGENT)
        await self._interaction_log.save()

    def has_liked(self, target_qq: str, feed_id: str) -> bool:
        """判断是否已对该说说点赞过。

        Args:
            target_qq: 说说主人 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示已点赞
        """
        return self._interaction_log.has_liked(target_qq, feed_id)

    def has_commented(self, target_qq: str, feed_id: str) -> bool:
        """判断是否已对该说说评论过。

        Args:
            target_qq: 说说主人 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示已评论
        """
        return self._interaction_log.has_commented(target_qq, feed_id)

    async def iter_commented_targets(self, exclude_qq: str = "") -> list[tuple[str, str]]:
        """枚举所有 bot 评论过的 (target_qq, feed_id)。

        用于「我评论过的说说」回查轮询：定期重新拉取这些说说的评论区，
        发现别人在 bot 评论下的二级回复并触发接力对话。

        Args:
            exclude_qq: 排除某个 QQ（通常传入 bot_qq，跳过自己空间，避免与 _poll_loop 重复）

        Returns:
            (target_qq, feed_id) 元组列表
        """
        await self.initialize()
        return self._interaction_log.iter_commented(exclude_target_qq=exclude_qq)

    async def iter_followup_targets(
        self, exclude_qq: str = "", limit: int = 0
    ) -> list[tuple[str, str]]:
        """按「最久未回查」优先返回 bot 评论过的 (target_qq, feed_id)。

        用于外部空间评论回查的轮转调度，避免单轮请求过多触发 QZone 限流。

        Args:
            exclude_qq: 排除某个 QQ（通常传入 bot_qq）
            limit: 最多返回多少条；<= 0 表示不限制

        Returns:
            按 last_followup_check 升序排列的 (target_qq, feed_id) 列表
        """
        await self.initialize()
        return self._interaction_log.iter_commented_for_followup(
            exclude_target_qq=exclude_qq, limit=limit
        )

    async def iter_followup_qqs(
        self, exclude_qq: str = "", limit: int = 0,
        max_feed_age_hours: float = 0,
    ) -> list[tuple[str, list[str]]]:
        """按「最久未回查」聚合返回需回查的 (target_qq, [feed_ids…])。

        用于以 QQ 为粒度的轮转调度：每轮只挑 ``limit`` 个 QQ，
        每个 QQ 一次 ``list_feeds`` 即可同时检查其名下全部 bot 评论过的 feed。

        Args:
            exclude_qq: 排除该 QQ（通常是 bot 自己）
            limit: 本轮最多回查多少个 QQ；<= 0 表示不限
            max_feed_age_hours: 评论过的说说超过该时长（小时）则不再回查；
                <= 0 表示不限。
        """
        await self.initialize()
        return self._interaction_log.iter_followup_qqs(
            exclude_target_qq=exclude_qq, limit=limit,
            max_feed_age_hours=max_feed_age_hours,
        )

    async def mark_followup_checked(self, target_qq: str, feed_id: str) -> None:
        """更新某 (target_qq, feed_id) 的最近回查时间戳并落盘。"""
        await self.initialize()
        self._interaction_log.mark_followup_checked(target_qq, feed_id)
        await self._interaction_log.save()

    async def get_feed_comments(
        self, host_qq: str, feed_id: str, page_size: int = 50
    ) -> list[dict[str, Any]]:
        """精确拉取单条说说的评论（含 list_3 楼中楼回复）。

        使用 ``emotion_cgi_msgdetail_v6`` 接口（``api_client.fetch_feed_detail``）
        按 tid 精准查询，1 个请求即可获得完整评论区，规避
        ``emotion_cgi_comment_list`` 在部分账号上 500 的故障。

        Args:
            host_qq: 说说主人 QQ 号
            feed_id: 说说 tid
            page_size: 兼容参数（当前未使用，保留以兼容旧测试）

        Returns:
            评论字典列表；接口失败或未命中返回空。
        """
        try:
            detail = await self._with_client(
                lambda client: client.fetch_feed_detail(
                    host_qq=str(host_qq), tid=str(feed_id)
                )
            )
        except Exception as exc:
            logger.error(
                f"读取说说 {feed_id}（host={host_qq}）详情失败: {exc}"
            )
            return []
        if not detail:
            return []
        return list(detail.get("comments", []) or [])

    async def get_feed_detail(
        self, host_qq: str, feed_id: str
    ) -> dict[str, Any] | None:
        """精确拉取单条说说完整详情（正文+图片+评论）。

        基于 ``emotion_cgi_msgdetail_v6``，一次请求拿到 feed 全貌；
        用于「外部空间评论回查」按 InteractionLog 标记精准命中。
        """
        try:
            return await self._with_client(
                lambda client: client.fetch_feed_detail(
                    host_qq=str(host_qq), tid=str(feed_id)
                )
            )
        except Exception as exc:
            logger.error(
                f"读取说说 {feed_id}（host={host_qq}）详情失败: {exc}"
            )
            return None

    async def mark_interaction(
        self,
        target_qq: str,
        feed_id: str,
        action: str,
        source: str = SOURCE_AGENT,
    ) -> None:
        """记录一次对好友说说的互动。

        Args:
            target_qq: 说说主人 QQ 号
            feed_id: 说说 tid
            action: 互动类型，使用 ``ACTION_LIKE`` / ``ACTION_COMMENT``
            source: 来源标识，``SOURCE_AGENT`` 或 ``SOURCE_POLL``
        """
        await self.initialize()
        self._interaction_log.mark(target_qq, feed_id, action, source)
        await self._interaction_log.save()

    async def get_monitor_feeds(self, num: int = 10) -> list[dict[str, Any]]:
        """获取好友动态流（用于好友说说自动监控）。

        返回的每项包含 target_qq, tid, content, images, comments 等字段。
        只返回未点赞的说说（monitor_list_feeds 内部已过滤已点赞项）。

        Args:
            num: 最多获取的好友说说数量

        Returns:
            好友说说数据字典列表
        """
        await self.initialize()
        try:
            feeds = await self._with_client(
                lambda client: client.monitor_list_feeds(max(1, num))
            )
            return feeds or []
        except Exception as exc:
            logger.error(f"获取好友动态失败: {exc}")
            return []

    async def describe_images(self, urls: list[str]) -> dict[str, str]:
        """批量获取图片的视觉识别描述（有缓存则复用，否则调用 vision LLM）。

        若 config.llm.vision_model_task 为空，则跳过识图，返回空字典。

        Args:
            urls: 图片 URL 列表

        Returns:
            ``{url: description}`` 字典，识别失败或未识别的 URL 不在结果中
        """
        await self.initialize()

        task_name = self._cfg.llm.vision_model_task.strip()
        if not task_name:
            return {}

        result: dict[str, str] = {}
        to_recognize: list[str] = []

        for url in urls:
            if not url:
                continue
            cached = self._vision_cache.get(url)
            if cached:
                result[url] = cached
            else:
                to_recognize.append(url)

        if not to_recognize:
            return result

        from io import BytesIO

        import aiohttp

        from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
        from src.app.plugin_system.types import Image, LLMPayload, ROLE, Text

        try:
            model_set = get_model_set_by_task(task_name)
        except Exception as exc:
            logger.warning(f"视觉识别模型任务 '{task_name}' 不可用，跳过识图: {exc}")
            return result

        _sem = asyncio.Semaphore(5)  # 最多 5 并发识图

        async def _recognize_one(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
            """下载并识别单张图片，返回 (url, description)；失败/超时返回空字符串。"""
            async with _sem:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.warning(f"下载图片失败（HTTP {resp.status}）: {url}")
                            return url, ""
                        image_data = await resp.read()

                    request = create_llm_request(model_set, request_name="foxzone.vision")
                    request.add_payload(
                        LLMPayload(
                            ROLE.USER,
                            [
                                Image(BytesIO(image_data)),  # type: ignore[arg-type]
                                Text(
                                    "请描述这张图片，字数控制在100字以内。简要说明图片主题、核心元素及背景环境。"
                                    "如能识别图片来源（如动漫、游戏、影视等），仅在完全确认时才可简要注明，"
                                    "否则不得猜测或提及来源，直接客观描述即可。"
                                    "如果图片中包含任何文字或代码，请完整转述，这部分不计入字数限制，"
                                    "力求客观、生动地还原图片内容。"
                                ),
                            ],
                        )
                    )
                    response = await asyncio.wait_for(
                        request.send(stream=False), timeout=30
                    )
                    description = (await response or "").strip()
                    return url, description
                except asyncio.TimeoutError:
                    logger.warning(f"识别图片超时（>30s）: {url}")
                    return url, ""
                except Exception as exc:
                    logger.warning(f"识别图片失败 {url}: {exc}")
                    return url, ""

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            pairs = await asyncio.gather(*[_recognize_one(session, url) for url in to_recognize])

        for url, description in pairs:
            if description:
                self._vision_cache.set(url, description)
                result[url] = description

        await self._vision_cache.save()
        return result

    async def generate_batch_replies(
        self,
        comment_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量生成评论回复决策。

        LLM 一次性处理本轮所有新评论，自主决定哪些需要回复、如何回复。

        Args:
            comment_items: 评论项列表（与 QZoneAdapter 投递的 batch 格式一致）

        Returns:
            决策列表，每项为 ``{"comment_tid": str, "feed_id": str, "reply": str | None}``
        """
        await self.initialize()
        return await self._content.generate_batch_replies(comment_items)

    async def process_external_followup_batch(
        self,
        comment_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """直接处理「外部空间接力回复」批次：决策 + 发送 + 标记，不走 ON_MESSAGE_RECEIVED。

        外部回查路径不需要也不应触发 ChatStream 分发链路，直接由 service 闭环执行：
        1. 调 LLM 批量决策
        2. 对决策回复的项依次发送（含 5~10s 随机间隔与失败重试）
        3. 全部标记为已处理，避免重复回查

        Args:
            comment_items: 与原 batch envelope 相同结构的接力回复项列表，
                每项必含 ``feed_id, comment_tid, host_qq, commenter_qq, commenter_name`` 等字段

        Returns:
            ``{"replied": int, "skipped": int, "decisions": dict[str, str | None]}``
        """
        if not comment_items:
            return {"replied": 0, "skipped": 0, "decisions": {}}

        await self.initialize()

        # 防双 bot 死循环：在 LLM 决策前过滤掉同 feed 已达接力上限的 comment
        max_replies = int(
            getattr(
                self._cfg.monitor,
                "external_followup_max_replies_per_feed",
                0,
            )
        )
        if max_replies > 0:
            filtered: list[dict[str, Any]] = []
            blocked: list[tuple[str, str, int]] = []
            for item in comment_items:
                host_qq = str(item.get("host_qq", ""))
                feed_id = str(item.get("feed_id", ""))
                if not (host_qq and feed_id):
                    filtered.append(item)
                    continue
                count = self._interaction_log.get_external_reply_count(host_qq, feed_id)
                if count >= max_replies:
                    blocked.append((host_qq, feed_id, count))
                    continue
                filtered.append(item)
            if blocked:
                logger.warning(
                    f"外部接力触发同 feed 上限保护：{len(blocked)} 条 comment 已跳过 "
                    f"(上限={max_replies}，示例 host={blocked[0][0]} feed={blocked[0][1]} count={blocked[0][2]})"
                )
            comment_items = filtered
            if not comment_items:
                return {"replied": 0, "skipped": len(blocked), "decisions": {}}

        try:
            decisions = await self._content.generate_batch_replies(comment_items)
        except Exception as exc:
            logger.error(f"外部接力批量决策失败: {exc}")
            return {"replied": 0, "skipped": 0, "decisions": {}}

        decision_map: dict[str, str | None] = {
            d["comment_tid"]: d.get("reply")
            for d in decisions
            if d.get("comment_tid")
        }

        decision_lines: list[str] = []
        for item in comment_items:
            ctid = item.get("comment_tid", "")
            who = item.get("commenter_name", "?")
            reply = decision_map.get(ctid)
            decision_lines.append(f"{'✓' if reply else '✗'} [{who}] → {reply or '跳过'}")
        log_llm_prompt(
            "外部接力评论回复决策",
            决策结果="\n".join(decision_lines) if decision_lines else "（无）",
        )

        replied_count = 0
        skipped_count = 0
        replies_sent = 0

        # 仅锁发送循环，不锁前面的 LLM 决策。所有退出路径前手动 release。
        await _REPLY_SEND_LOCK.acquire()

        for item in comment_items:
            comment_tid = str(item.get("comment_tid", ""))
            feed_id = str(item.get("feed_id", ""))
            commenter_name = str(item.get("commenter_name", "未知用户"))
            commenter_qq = str(item.get("commenter_qq", ""))
            host_qq = str(item.get("host_qq", ""))

            if not (comment_tid and feed_id and host_qq):
                continue

            reply_text = decision_map.get(comment_tid)

            if reply_text:
                # commentId 取顶层一级评论 tid；@ 与 commentUin 的处理详见 api_client.reply。
                root_comment_tid = resolve_root_comment_tid(
                    item.get("all_comments") or [], comment_tid
                )
                _all_c = item.get("all_comments") or []
                logger.debug(
                    f"reply 前 all_comments 摘要: total={len(_all_c)} "
                    f"target={comment_tid!r} parent={item.get('parent_tid')!r} "
                    f"resolved_root={root_comment_tid!r} "
                    f"tids=[{', '.join(str(c.get('comment_tid', '')) for c in _all_c[:20])}]"
                )

                if replies_sent > 0:
                    delay = random.uniform(15, 30)
                    logger.debug(f"外部接力回复发送间隔：等待 {delay:.1f}s")
                    await asyncio.sleep(delay)

                success = False
                last_err: Exception | None = None
                rate_limited = False
                for attempt in range(3):
                    try:
                        success = await self.reply_comment(
                            feed_id=feed_id,
                            host_qq=host_qq,
                            target_name=commenter_name,
                            reply_text=reply_text,
                            comment_tid=root_comment_tid,
                            commenter_qq=commenter_qq,
                        )
                        if success:
                            break
                        last_err = None
                    except RuntimeError as exc:
                        # api_client.reply 抛 RuntimeError = 不可重试错误
                        # （-3000 cookie 失效 / -10049 QZone 限流）
                        last_err = exc
                        success = False
                        rate_limited = "-10049" in str(exc) or "限流" in str(exc)
                        logger.warning(
                            f"外部接力回复遇到不可重试错误，停止重试: {exc}"
                        )
                        break
                    except Exception as exc:
                        last_err = exc
                        success = False
                    if attempt < 2:
                        backoff = 3 * (attempt + 1)
                        logger.warning(
                            f"外部接力回复失败 (feed_id={feed_id}, comment_tid={comment_tid})，"
                            f"{backoff}s 后重试 ({attempt + 1}/2)"
                            + (f"：{last_err}" if last_err else "")
                        )
                        await asyncio.sleep(backoff)

                replies_sent += 1

                if success:
                    replied_count += 1
                    logger.info(
                        f"外部接力回复成功 '{commenter_name}'：'{reply_text}' "
                        f"(feed_id={feed_id}, comment_tid={comment_tid}, "
                        f"root_tid={root_comment_tid}, host_qq={host_qq})"
                    )
                    # 续期 last_ts，避免持续对话中的 feed 被 max_feed_age_hours 过滤掉
                    await self.mark_interaction(host_qq, feed_id, ACTION_COMMENT)
                    # 递增同 feed 接力计数（防双 bot 死循环）
                    new_count = self._interaction_log.increment_external_reply_count(
                        host_qq, feed_id
                    )
                    await self._interaction_log.save()
                    if max_replies > 0 and new_count >= max_replies:
                        logger.warning(
                            f"外部接力同 feed 计数已达上限 {new_count}/{max_replies}，"
                            f"该 feed 后续将停止接力 (host={host_qq}, feed={feed_id})"
                        )
                else:
                    logger.error(
                        f"外部接力回复失败: feed_id={feed_id}, comment_tid={comment_tid}, "
                        f"root_tid={root_comment_tid}, host_qq={host_qq}"
                    )
                    if rate_limited:
                        # 限流：标记已处理避免下轮重复触发，并跳过当前条继续处理后续
                        logger.warning(
                            f"QZone 限流，跳过该 comment 并标记已处理: feed_id={feed_id}, comment_tid={comment_tid}"
                        )
                        await self.mark_comment_replied(feed_id, comment_tid)
                        skipped_count += 1
                        continue
            else:
                skipped_count += 1

            await self.mark_comment_replied(feed_id, comment_tid)

        _REPLY_SEND_LOCK.release()

        logger.info(
            f"外部接力批量处理完成：共 {len(comment_items)} 条，"
            f"回复 {replied_count} 条，跳过 {skipped_count} 条。"
        )
        return {
            "replied": replied_count,
            "skipped": skipped_count,
            "decisions": decision_map,
        }

    async def generate_feed_decisions(
        self,
        feed_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量生成好友说说评论决策（点赞已由 Adapter 完成）。

        Args:
            feed_items: 说说项列表，每项含 tid, target_qq, content,
                        created_time, image_text, comment_count 字段

        Returns:
            决策列表，每项为 ``{"tid": str, "target_qq": str, "comment": str | None}``
        """
        await self.initialize()
        return await self._content.generate_feed_decisions(feed_items)

    async def process_feed_monitor_batch(
        self,
        feed_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """直接处理「好友说说监控」批次：图片识别 + 决策 + 发评论 + 标记。

        与 ``process_external_followup_batch`` 同样不走 ON_MESSAGE_RECEIVED，
        由 service 闭环执行，避免 EventBus 5s 超时。

        Args:
            feed_items: 已点赞但尚未评论的说说项列表（与原 batch envelope 中
                ``friend_feed_items`` 字段格式一致）

        Returns:
            ``{"commented": int, "decisions": dict[str, str | None]}``
        """
        if not feed_items:
            return {"commented": 0, "decisions": {}}

        await self.initialize()

        # 批量识别图片
        all_image_urls: list[str] = []
        for item in feed_items:
            all_image_urls.extend(item.get("images", []))
        if all_image_urls:
            logger.info(f"开始批量识别 {len(all_image_urls)} 张好友说说配图…")
            try:
                image_descs = await self.describe_images(all_image_urls)
                logger.info(
                    f"图片识别完成：{len(image_descs)}/{len(all_image_urls)} 张。"
                )
                for item in feed_items:
                    urls: list[str] = item.get("images", [])
                    if urls:
                        item["image_text"] = "\n".join(
                            f"图片{j}：{image_descs.get(u, '[图片]')}"
                            for j, u in enumerate(urls, 1)
                        )
            except Exception as exc:
                logger.warning(f"图片识别失败，使用占位符继续: {exc}")

        logger.info(f"批量决策 {len(feed_items)} 条已点赞好友说说是否需要评论…")
        try:
            feed_decisions = await self._content.generate_feed_decisions(feed_items)
        except Exception as exc:
            logger.error(f"批量生成好友说说评论决策时发生异常: {exc}")
            return {"commented": 0, "decisions": {}}

        decision_map: dict[str, str | None] = {
            d["tid"]: d.get("comment")
            for d in feed_decisions
            if d.get("tid")
        }

        commented_count = 0
        decision_lines: list[str] = []
        sent_so_far = 0

        for item in feed_items:
            tid = str(item.get("tid", ""))
            target_qq = str(item.get("target_qq", ""))
            if not (tid and target_qq):
                continue

            comment_text = decision_map.get(tid)
            decision_label = f"(qq={target_qq}, tid={tid})"

            if comment_text:
                if sent_so_far > 0:
                    delay = random.uniform(15, 30)
                    logger.debug(f"评论发送间隔：等待 {delay:.1f}s")
                    await asyncio.sleep(delay)

                ok = False
                last_err: Exception | None = None
                rate_limited = False
                for attempt in range(3):
                    try:
                        ok = await self.comment(
                            target_qq=target_qq, feed_id=tid, text=comment_text
                        )
                        if ok:
                            break
                        last_err = None
                    except RuntimeError as exc:
                        last_err = exc
                        ok = False
                        rate_limited = "-10049" in str(exc) or "限流" in str(exc)
                        logger.warning(
                            f"评论遇到不可重试错误，停止重试 {decision_label}: {exc}"
                        )
                        break
                    except Exception as exc:
                        last_err = exc
                        ok = False
                    if attempt < 2:
                        backoff = 3 * (attempt + 1)
                        logger.warning(
                            f"评论发送失败 {decision_label}，{backoff}s 后重试 "
                            f"({attempt + 1}/2)"
                            + (f"：{last_err}" if last_err else "")
                        )
                        await asyncio.sleep(backoff)

                sent_so_far += 1

                if ok:
                    commented_count += 1
                    await self.mark_interaction(target_qq, tid, ACTION_COMMENT)
                    logger.info(f"评论成功 {decision_label}：「{comment_text}」")
                    decision_lines.append(
                        f"✓ [qq={target_qq} tid={tid}] → 评论：{comment_text}"
                    )
                else:
                    logger.warning(f"评论失败 {decision_label}")
                    decision_lines.append(f"✗ [qq={target_qq} tid={tid}] → 评论失败")
                    if rate_limited:
                        logger.warning(
                            "QZone 限流，剩余好友说说不再评论，本批保持未标记以便下次再试"
                        )
                        log_llm_prompt(
                            "好友说说评论决策",
                            决策结果="\n".join(decision_lines) if decision_lines else "（无）",
                        )
                        return {
                            "commented": commented_count,
                            "decisions": decision_map,
                        }
            else:
                decision_lines.append(f"· [qq={target_qq} tid={tid}] → 仅点赞")

        log_llm_prompt(
            "好友说说评论决策",
            决策结果="\n".join(decision_lines) if decision_lines else "（无）",
        )
        logger.info(
            f"好友说说监控批量处理完成：共 {len(feed_items)} 条，"
            f"评论 {commented_count} 条。"
        )
        return {"commented": commented_count, "decisions": decision_map}

    async def publish_generated_feed(
        self,
        topic: str = "",
        with_image: bool | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        """按主题生成内容并发布说说。"""
        story, images = await self._build_generated_feed(
            topic=topic,
            with_image=with_image,
            context=context,
        )
        if not story:
            return {"success": False, "message": "生成说说内容失败"}

        success = await self.publish_feed(story, images)
        if success:
            return {"success": True, "message": story}
        return {"success": False, "message": "发布说说至 QQ 空间失败"}

    async def publish_feed_with_image_info(
        self,
        content: str,
        image_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发布外部已写好正文的说说，并可选附带由调用方提供的 image_info 直接生成图片。

        Args:
            content: 说说正文（必需）
            image_info: 由调用方按 NovelAI 规则编写的图片描述字典，结构示例：
                ``{"prompt": "1girl, ...", "negative_prompt": "...", "aspect_ratio": "方图"}``
                传 None / 空字典则发布纯文本。

        Returns:
            ``{"success": bool, "message": str}``
        """
        clean_content = content.strip()
        if not clean_content:
            return {"success": False, "message": "说说内容不能为空"}

        await self.initialize()

        images: list[bytes] = []
        info = image_info or {}
        if info.get("prompt"):
            if not self._cfg.ai_image.enable_ai_image:
                logger.warning("配置中未启用 AI 配图，将以纯文本发布。")
            else:
                image_path = await self._generate_image_from_info(info)
                images = self._load_image_bytes(image_path)

        success = await self.publish_feed(clean_content, images)
        if success:
            suffix = "（含 AI 配图）" if images else ""
            return {"success": True, "message": f"{clean_content}{suffix}"}
        return {"success": False, "message": "发布说说至 QQ 空间失败"}

    async def compose_feed_guidance(self) -> str:
        """生成「发说说指引」纯文本块，供 Tool A 返回给外部 chatter LLM。

        指引内容包括：发说说基调、最近自己发过的说说快照、配图触发条件、
        image_info JSON schema、NovelAI 9 段式规则、当前生效的 style_anchor 与 base_negative。
        不调用 LLM，纯文本拼装。

        Returns:
            完整指引文本（多行）。
        """
        await self.initialize()

        recent_self_feeds = await self.get_recent_self_feeds_block(num=3)
        recent_block = (
            f"<recent_self_feeds>\n这是你（Bot 自己）最近发过的说说快照，不是用户或好友的动态。参考它们来保持语气连贯、避免重复选题：\n"
            f"{recent_self_feeds}\n</recent_self_feeds>"
            if recent_self_feeds
            else "<recent_self_feeds>（暂无你自己最近发过的说说）</recent_self_feeds>"
        )

        ai_image_enabled = self._cfg.ai_image.enable_ai_image
        provider_id = self._cfg.ai_image.provider

        ai_image_section = (
            "<ai_image_status>AI 配图当前禁用，本次不要传 image_info。</ai_image_status>"
            if not ai_image_enabled
            else f"<ai_image_status>AI 配图已启用（provider={provider_id}）。</ai_image_status>"
        )

        provider_guidance_block = self._build_provider_guidance_block(
            ai_image_enabled, provider_id
        )

        return FEED_GUIDANCE_TEMPLATE.format(
            recent_block=recent_block,
            ai_image_section=ai_image_section,
            provider_guidance_block=provider_guidance_block,
        )

    def _build_provider_guidance_block(
        self, ai_image_enabled: bool, provider_id: str
    ) -> str:
        """按当前 provider 取出已格式化的图像指引段。

        指引文本的具体填充逻辑（如 NovelAI 的 ``style_anchor`` / ``base_negative``
        注入、OpenAI 的参考图提示等）全部内聚在各 provider 的
        ``format_guidance()`` 中，本方法仅做转发与边界处理。
        """
        if not ai_image_enabled:
            return ""

        guidance = self._dispatcher.get_guidance(provider_id)
        if not guidance:
            return f"（未识别的 provider: {provider_id!r}，无可用图像指引）"
        return guidance

    def _get_bot_qq(self) -> str:
        """获取 Bot QQ 号。"""
        return str(self._cfg.general.bot_qq)

    async def _build_client(self) -> QZoneAPIClient | None:
        """构建 QZoneAPIClient 实例。"""
        bot_qq = self._get_bot_qq()
        cookies = await self._cookie.get_cookies(bot_qq)
        if not cookies:
            logger.error(
                "构建 API 客户端失败：无法获取 Cookie。"
                "请确保 Napcat 连接正常，或存在有效的本地 Cookie 缓存。"
            )
            return None

        try:
            return QZoneAPIClient.create(cookies)
        except ValueError as exc:
            logger.error(f"构建 API 客户端失败：{exc}")
            return None

    async def _with_client(self, func: Any) -> Any:
        """统一处理 Cookie 失效重试（最多重试一次）。"""
        bot_qq = self._get_bot_qq()

        for retry_count in range(2):
            client = await self._build_client()
            if client is None:
                raise RuntimeError("获取 QZone API 客户端失败：无法获取 Cookie。")

            try:
                return await func(client)
            except RuntimeError as exc:
                if "错误码: -3000" in str(exc) and retry_count == 0:
                    logger.warning("Cookie 失效（-3000），清除缓存并重试…")
                    self._cookie.clear_cache(bot_qq)
                    continue
                raise

        raise RuntimeError("API 调用失败：超过最大重试次数。")

    async def _build_generated_feed(
        self,
        topic: str,
        with_image: bool | None,
        context: str | None,
    ) -> tuple[str, list[bytes]]:
        """生成说说正文及图片字节。"""
        use_ai_image = (
            self._cfg.ai_image.enable_ai_image
            if with_image is None
            else with_image
        )

        image_path: Path | None = None
        if use_ai_image:
            story, image_info = await self._content.generate_story_with_image_info(
                topic,
                context=context,
            )
            if not story:
                return "", []

            image_path = await self._generate_image_from_info(image_info)
        else:
            story = await self._content.generate_story(topic, context=context)
            if not story:
                return "", []

        return story, self._load_image_bytes(image_path)

    async def _generate_image_from_info(
        self,
        image_info: dict[str, Any],
    ) -> Path | None:
        """根据生成出的配图信息落地图片（统一走 ImageDispatcher）。"""
        prompt = str(image_info.get("prompt", "")).strip()
        if not prompt:
            logger.warning("生成了配图模式，但未得到有效图片提示词，将发布纯文本说说。")
            return None

        success, image_path, message = await self._dispatcher.generate(
            prompt=prompt,
            negative_prompt=image_info.get("negative_prompt"),
            aspect_ratio=str(image_info.get("aspect_ratio", "方图")),
        )
        if success:
            return image_path

        logger.warning(
            f"AI 配图生成失败 [provider={self._dispatcher.current_provider_id()}]: {message}"
        )
        return None

    def _load_image_bytes(self, image_path: Path | None) -> list[bytes]:
        """读取生成后的图片字节。"""
        if image_path is None or not image_path.exists():
            return []

        try:
            with open(image_path, "rb") as file_obj:
                logger.info("已将 AI 配图附加到说说。")
                return [file_obj.read()]
        except OSError as exc:
            logger.error(f"读取配图文件失败: {exc}")
            return []

    async def _save_send_history(self, story: str) -> None:
        """将刚发送的说说内容追加到发送历史（最多保留 20 条）。"""
        try:
            data = await storage_api.load_json("foxzone", "send_history")
            if data is None:
                data = {"records": []}

            records: list[dict[str, Any]] = data.get("records", [])
            records.append(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "text": story,
                }
            )
            data["records"] = records[-20:]
            await storage_api.save_json("foxzone", "send_history", data)
        except Exception as exc:
            logger.warning(f"保存发送历史失败: {exc}")
