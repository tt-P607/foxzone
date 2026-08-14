"""FoxZone 文本内容生成服务（ContentService）。

封装所有与大语言模型交互以生成文本的逻辑：

- 生成 QQ 空间说说正文
- 批量决策自己说说下的评论回复
- 批量决策好友说说的评论互动

所有长提示词统一从 PromptManager 读取。

本类持有 :class:`~plugins.foxzone.runtime.QZoneRuntime` 引用获取
配置、识图缓存与 API 客户端，不通过 ``get_service()`` 反查。
"""

from __future__ import annotations

import re
import typing
from typing import Any

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.prompt_api import get_template
from src.app.plugin_system.types import Image, LLMPayload, PromptTemplate, ROLE, Text

from .formatters import format_batch_comment_items, format_feed_items_block, format_self_feed
from .parsers import (
    parse_batch_reply_decisions,
    parse_feed_decisions,
    parse_simple_json_text,
)
from .personality import get_now_info, get_personality_desc
from .vision import describe_images, download_images_map

if typing.TYPE_CHECKING:
    from ...config import FoxZoneConfig
    from ...runtime import QZoneRuntime
    from src.kernel.llm import ModelSet

logger = get_logger("foxzone.content_service", color=COLOR.ORANGE)


def log_llm_prompt(label: str, **sections: str) -> None:
    """以面板格式在日志中打印 LLM 提示词（仅输出到控制台）。

    借助 rich 的 Panel 组件自动适配终端宽度，正确处理 CJK 字符对齐。

    Args:
        label: 面板标题
        **sections: 各节内容，键为节名，值为内容文本
    """
    parts: list[str] = []
    for section_name, content in sections.items():
        parts.append(f"[bold]▸ {section_name}[/bold]")
        parts.append(content)
        parts.append("")
    logger.print_panel("\n".join(parts).strip(), title=label, border_style="cyan")


class ContentService:
    """FoxZone 文本内容生成服务。"""

    def __init__(self, runtime: "QZoneRuntime") -> None:
        """初始化内容服务。

        Args:
            runtime: 插件运行时状态容器
        """
        self._runtime = runtime

    @property
    def _cfg(self) -> "FoxZoneConfig":
        """当前插件配置（FoxZoneConfig）。"""
        return self._runtime.config

    # ------------------------------------------------------------------
    # LLM 发送
    # ------------------------------------------------------------------

    async def _get_prompt_template(self, name: str) -> PromptTemplate | None:
        """读取指定名称的提示词模板。"""
        try:
            return get_template(name)
        except Exception as exc:
            logger.error(f"读取提示词模板 '{name}' 失败: {exc}")
            return None

    def _get_model_set(self, task_name: str) -> ModelSet | None:
        """读取模型任务对应的 ModelSet。"""
        try:
            return get_model_set_by_task(task_name)
        except Exception as exc:
            logger.error(f"model.toml 中未找到任务配置 '{task_name}': {exc}")
            return None

    async def _send_prompt(
        self,
        task_name: str,
        request_name: str,
        prompt_text: str,
        images: list[str] | None = None,
        content: list[Any] | None = None,
    ) -> str:
        """统一发送提示词并返回完整文本结果；可选附带多模态图片。

        Args:
            task_name: 模型任务名
            request_name: 请求名
            prompt_text: 提示词文本（用于日志展示）
            images: 可选的 ``base64|...`` 图片 data 列表，以多模态 payload 附加
            content: 可选；已内联好的 Text/Image 交替 content 列表，
                传入时优先于 ``prompt_text`` 与 ``images`` 的默认拼接
        """
        model_set = self._get_model_set(task_name)
        if model_set is None:
            logger.error(f"_send_prompt 无可用 ModelSet: task={task_name} request={request_name}")
            return ""

        n_text = sum(1 for c in (content or []) if getattr(c, "type", "") == "text")
        n_img = sum(1 for c in (content or []) if getattr(c, "type", "") == "image")
        logger.debug(
            f"发送 LLM 请求: task={task_name} request={request_name} "
            f"content_items={len(content or [])} (text={n_text}, image={n_img}) "
            f"prompt_len={len(prompt_text)}"
        )

        log_llm_prompt(request_name, 用户消息=prompt_text)

        if content is None:
            content = [Text(prompt_text)]
            if images:
                content.extend(Image(data) for data in images)

        request = create_llm_request(model_set, request_name=request_name)
        request.add_payload(LLMPayload(ROLE.USER, content))
        response = await request.send(stream=False)
        return await response

    @staticmethod
    def _inline_feed_images(
        text: str, image_map: dict[tuple[int, int], str]
    ) -> list[Any]:
        """将提示词中的 ``[[IMG:i:j]]`` 标记替换为 Text + Image 邻接内容。

        每条说说的图片标记按说说序号 i、图片序号 j 精确映射到图片数据，
        使模型能明确知道图片归属哪条说说，避免多说说批量决策时图片错位。

        Args:
            text: 含 ``[[IMG:i:j]]`` 标记的提示词文本
            image_map: ``{(i, j): base64_data}`` 映射

        Returns:
            Text/Image 交替排列的内容列表
        """
        pattern = re.compile(r"\[\[IMG:(\d+):(\d+)\]\]")
        content: list[Any] = []
        cursor = 0
        for match in pattern.finditer(text):
            if match.start() > cursor:
                content.append(Text(text[cursor:match.start()]))
            key = (int(match.group(1)), int(match.group(2)))
            data = image_map.get(key)
            if data:
                content.append(Image(data))
            else:
                content.append(Text(match.group(0)))
            cursor = match.end()
        if cursor < len(text):
            content.append(Text(text[cursor:]))
        if not content:
            content.append(Text(text))
        return content

    # ------------------------------------------------------------------
    # 自己说说快照（供说说生成 / 评论回复提供上下文）
    # ------------------------------------------------------------------

    async def get_recent_self_feeds_block(self, num: int = 3) -> str:
        """生成「自己最近 N 条说说」的完整上下文文本块。

        包括正文、发布时间、图片描述、评论区——形态与读取他人说说时一致。

        Args:
            num: 取多少条最近的说说，默认 3 条

        Returns:
            格式化好的多行文本块；若无说说或读取失败返回空字符串。
        """
        bot_qq = await self._runtime.bot_qq()
        try:
            feeds = await self._runtime.with_client(
                lambda client: client.list_feeds(
                    bot_qq, max(1, num), skip_commented=False
                )
            )
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
                image_descs = await describe_images(
                    all_image_urls,
                    self._cfg.llm.vision_model_task,
                    self._runtime.vision_cache,
                )
            except Exception as exc:
                logger.warning(f"识别自己说说配图失败: {exc}")

        lines: list[str] = []
        for idx, feed in enumerate(feeds, start=1):
            lines.append(format_self_feed(idx, feed, image_descs))
        return "\n".join(lines).strip()

    async def _append_recent_self_block(self, prompt_text: str, purpose: str) -> str:
        """向提示词末尾附加「自己最近说说快照」段（若有）。"""
        recent_self = await self.get_recent_self_feeds_block(num=3)
        if not recent_self:
            return prompt_text
        return (
            prompt_text
            + "\n\n<recent_self_feeds>\n"
            + f"以下是你最近发过的说说快照（包含原文、配图描述、评论区），{purpose}：\n"
            + f"{recent_self}\n"
            + "</recent_self_feeds>"
        )

    # ------------------------------------------------------------------
    # 生成入口
    # ------------------------------------------------------------------

    async def generate_story(self, topic: str, context: str | None = None) -> str:
        """生成纯文本 QQ 空间说说。"""
        template = await self._get_prompt_template("foxzone.story.generate")
        if template is None:
            return ""

        current_time, weekday = get_now_info()
        personality_desc = get_personality_desc()
        topic_desc = f"主题：{topic}" if topic else "主题不限"
        history = await self._get_send_history()

        prompt_text = await (
            template.set("personality_desc", personality_desc)
            .set("current_time", current_time)
            .set("weekday", weekday)
            .set("topic_desc", topic_desc)
            .set("output_format", '{"text": "说说正文内容"}')
            .set("history", history)
            .build()
        )
        prompt_text = await self._append_recent_self_block(
            prompt_text, "供你参考连贯上下文、避免重复选题"
        )
        if context:
            prompt_text += f"\n\n作为参考，以下是一些最近的聊天记录：\n---\n{context}\n---"

        response_text = await self._send_prompt(
            self._cfg.llm.story_model_task,
            "foxzone.story.generate",
            prompt_text,
        )
        story_text = parse_simple_json_text(response_text)
        if story_text:
            logger.info(f"成功生成说说：'{story_text}'")
        else:
            logger.error("生成说说内容失败或为空。")
        return story_text

    async def generate_batch_replies(
        self,
        comment_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量生成评论回复决策。

        LLM 一次性处理本轮所有新评论，自主决定哪些需要回复、如何回复。

        Args:
            comment_items: 评论项列表，每项需含 feed_id、feed_content、
                           comment_tid、comment_content、commenter_name、
                           story_time、comment_time、all_comments、feed_images

        Returns:
            决策列表，每项为 ``{"comment_tid": str, "feed_id": str, "reply": str | None}``。
            ``reply`` 为 None 或缺失时表示模型决定不回复该条评论。
        """
        if not comment_items:
            return []

        template = await self._get_prompt_template("foxzone.comment.reply.batch")
        if template is None:
            return []

        personality_desc = get_personality_desc()
        current_time, _ = get_now_info()
        bot_qq = await self._runtime.bot_qq()

        multimodal = bool(self._cfg.llm.multimodal_mode)
        image_descs: dict[str, str] = {}
        image_map: dict[tuple[int, int], str] = {}
        if multimodal:
            # 多模态模式：按 (评论序号, 图片序号) 下载图片并精确关联，
            # 跳过 VLM 识图，图片随文本邻接内联，避免错位。
            all_image_urls: list[str] = []
            url_keys: list[tuple[tuple[int, int], str]] = []
            for i, item in enumerate(comment_items, 1):
                urls = [str(u) for u in item.get("feed_images", []) if u]
                for j, url in enumerate(urls, 1):
                    all_image_urls.append(url)
                    url_keys.append(((i, j), url))
            if all_image_urls:
                url_to_data = await download_images_map(all_image_urls)
                image_map = {
                    key: url_to_data[url]
                    for key, url in url_keys
                    if url in url_to_data
                }
                logger.info(
                    f"多模态模式：下载 {len(image_map)}/{len(all_image_urls)} 张说说图片直接传模型。"
                )
        else:
            all_image_urls = [
                str(u) for item in comment_items for u in item.get("feed_images", []) if u
            ]
            if all_image_urls:
                image_descs = await describe_images(
                    all_image_urls,
                    self._cfg.llm.vision_model_task,
                    self._runtime.vision_cache,
                )

        comment_items_block = format_batch_comment_items(
            comment_items, bot_qq, image_descs=image_descs, multimodal=multimodal
        )

        prompt_text = await (
            template.set("personality_desc", personality_desc)
            .set("current_time", current_time)
            .set("comment_items_block", comment_items_block)
            .build()
        )
        prompt_text = await self._append_recent_self_block(
            prompt_text, "便于你在回复评论时联想到自己当时发说说的语境与心情"
        )

        content = (
            self._inline_feed_images(prompt_text, image_map) if multimodal else None
        )

        response_text = await self._send_prompt(
            self._cfg.llm.comment_model_task,
            "foxzone.comment.reply.batch",
            prompt_text,
            content=content,
        )

        decisions = parse_batch_reply_decisions(response_text)
        for d in decisions:
            logger.debug(
                f"评论回复决策明细: comment_tid={d.get('comment_tid')} "
                f"feed_id={d.get('feed_id')} reply={str(d.get('reply'))[:60]!r}"
            )
        logger.info(
            f"批量评论决策完成：{len(comment_items)} 条评论，"
            f"{sum(1 for d in decisions if d.get('reply'))} 条决定回复。"
        )
        return decisions

    async def generate_feed_decisions(
        self,
        feed_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量生成好友说说互动决策（点赞 + 评论）。

        Args:
            feed_items: 说说项列表，每项需含 tid, target_qq, content,
                        created_time, image_text, comment_count 字段

        Returns:
            决策列表，每项为 ``{"tid": str, "target_qq": str,
            "like": bool, "comment": str | None}``。
            ``like`` 表示是否点赞；``comment`` 为 None 表示不写评论。
        """
        if not feed_items:
            return []

        template = await self._get_prompt_template("foxzone.friend.feed.interact")
        if template is None:
            return []

        personality_desc = get_personality_desc()
        current_time, _ = get_now_info()

        multimodal = bool(self._cfg.llm.multimodal_mode)
        image_map: dict[tuple[int, int], str] = {}
        if multimodal:
            # 多模态模式：按 (说说序号, 图片序号) 下载图片并精确关联，
            # 跳过 VLM 识图，图片随文本邻接内联，避免批量决策时错位。
            all_image_urls: list[str] = []
            url_keys: list[tuple[tuple[int, int], str]] = []
            for i, item in enumerate(feed_items, 1):
                urls = [str(u) for u in item.get("images", []) if u]
                for j, url in enumerate(urls, 1):
                    all_image_urls.append(url)
                    url_keys.append(((i, j), url))
            if all_image_urls:
                url_to_data = await download_images_map(all_image_urls)
                image_map = {
                    key: url_to_data[url]
                    for key, url in url_keys
                    if url in url_to_data
                }
                logger.info(
                    f"多模态模式：下载 {len(image_map)}/{len(all_image_urls)} 张好友说说图片直接传模型。"
                )

        feed_items_block = format_feed_items_block(feed_items, multimodal=multimodal)

        prompt_text = await (
            template.set("personality_desc", personality_desc)
            .set("current_time", current_time)
            .set("feed_items_block", feed_items_block)
            .build()
        )

        content = (
            self._inline_feed_images(prompt_text, image_map) if multimodal else None
        )

        response_text = await self._send_prompt(
            self._cfg.llm.comment_model_task,
            "foxzone.friend.feed.interact",
            prompt_text,
            content=content,
        )

        decisions = parse_feed_decisions(response_text)
        for d in decisions:
            logger.debug(
                f"好友说说决策明细: tid={d.get('tid')} target_qq={d.get('target_qq')} "
                f"like={d.get('like')} comment={str(d.get('comment'))[:60]!r}"
            )
        logger.info(
            f"好友说说评论决策完成：{len(feed_items)} 条说说，"
            f"决定评论 {sum(1 for d in decisions if d.get('comment'))} 条。"
        )
        return decisions

    # ------------------------------------------------------------------
    # 发送历史
    # ------------------------------------------------------------------

    async def _get_send_history(self) -> str:
        """读取最近发送的说说历史，避免生成重复内容。"""
        try:
            from src.app.plugin_system.api import storage_api

            data = await storage_api.load_json("foxzone", "send_history")
            if data is None or not isinstance(data, dict):
                return ""

            records = data.get("records", [])
            if not isinstance(records, list) or not records:
                return ""

            lines: list[str] = []
            for record in records[-10:]:
                if not isinstance(record, dict):
                    continue
                time_str = str(record.get("time", ""))
                text = str(record.get("text", "")).strip()
                if not text:
                    continue
                lines.append(f"- [{time_str}] {text}" if time_str else f"- {text}")

            return "\n".join(lines)
        except Exception as exc:
            logger.warning(f"读取发送历史失败: {exc}")
            return ""
