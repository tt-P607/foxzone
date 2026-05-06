"""FoxZone 文本内容生成服务。

封装所有与大语言模型交互以生成文本的逻辑，包括：
- 生成 QQ 空间说说正文
- 生成带配图信息的说说
- 针对好友说说生成评论
- 针对自己说说下的评论生成回复

所有长提示词统一从 PromptManager 读取，避免在业务代码中散落硬编码模板。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import inspect
import json
import re
import typing
from typing import Any, ClassVar

import aiohttp

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.plugin_api import get_plugin
from src.app.plugin_system.api.prompt_api import get_template
from src.app.plugin_system.types import LLMPayload, PromptTemplate, ROLE, Text
from src.core.components.registry import get_global_registry
from src.core.config import get_core_config
from src.kernel.llm import ToolRegistry, ToolResult

if typing.TYPE_CHECKING:
    from ..plugin import FoxZonePlugin

from ..config import FoxZoneConfig

logger = get_logger("foxzone.content_service", color=COLOR.ORANGE)

_VLM_TIMEOUT = 30.0
_IMAGE_DOWNLOAD_TIMEOUT = 15.0


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

    def __init__(self, plugin: "FoxZonePlugin") -> None:
        """初始化内容服务。

        Args:
            plugin: 宿主插件实例
        """
        self._plugin: "FoxZonePlugin" = plugin
        self._cfg: FoxZoneConfig = plugin.config  # type: ignore[assignment]

    def _get_personality_desc(self) -> str:
        """构建结构化的 QZone 场景系统提示词。

        采用 XML 标签结构分四段：``<introduce>``（QZone 场景定位）、
        ``<personality>``（人设字段）、``<expression_principles>``（防 OOC/防模板化原则）、
        ``<background_story>``（可选）。

        Raises:
            读取核心人格配置失败时直接抛出，由调用方处理。
        """
        personality = get_core_config().personality

        # 1. introduce：QZone 场景头
        introduce_block = (
            "<introduce>\n"
            "此刻你正活跃在 QQ 空间——一个朋友间分享日常、互相点赞评论的半公开社交场所。\n"
            "QQ 空间的互动是**异步、慢节奏**的：你看到的每条说说都是朋友某个瞬间的情绪切片，\n"
            "不像聊天那样需要即时来回。互动的价值在于「看见」与「被看见」，而不是「必须说点什么」。\n"
            "请像真实的朋友一样自然参与——可以共鸣、可以打趣、也可以只是默默点赞。\n"
            "</introduce>"
        )

        # 2. personality：从 core.toml 拼装
        personality_lines: list[str] = ["<personality>"]
        if personality.nickname:
            personality_lines.append(f"你的名字是 **{personality.nickname}**。")
        if personality.personality_core:
            personality_lines.append(f"你的核心人格：{personality.personality_core}")
        if personality.personality_side:
            personality_lines.append(f"你的人格侧面：{personality.personality_side}")
        if personality.identity:
            personality_lines.append(f"你的身份：{personality.identity}")
        if personality.reply_style:
            personality_lines.append(f"你的表达方式：{personality.reply_style}")
        personality_lines.append("</personality>")
        personality_block = "\n".join(personality_lines)

        # 3. expression_principles：QZone 场景下的核心表达准则（防 OOC + 防模板化）
        principles_block = (
            "<expression_principles>\n"
            "# 真实感优先\n"
            "像真实朋友间的随手互动，**不要书面化、不要堆砌人设标签词**。\n"
            "日常才是基调，偶尔的个性化点缀才是惊喜——绝对不要每条评论都强行体现人设。\n"
            "情绪有惯性：评论的基调由说说本身的氛围决定，而不是从中性状态硬启动一个固定模板。\n"
            "\n"
            "# 防模板化\n"
            "避免连续多条评论使用相似的开场词、感叹词或句尾点缀。\n"
            "你的人设标签词（人设中反复出现的核心意象）是底色，**不应该成为口癖**。\n"
            "回复偶尔「不那么像你」反而更真实——真实的人不会时时刻刻都在表演自己。\n"
            "\n"
            "# 场景边界\n"
            "QQ 空间是社交分享场合，**不是聊天对话**。\n"
            "- 不要在评论里 @ 对方，不要把评论写得像「邀请对方继续对话」；\n"
            "- 评论是顺手的一句感想，不是问候、不是追问、不是发起话题；\n"
            "- 若说说本身没什么共鸣，宁可只点赞——评论的价值在「有话说才说」。\n"
            "\n"
            "# 情绪与边界\n"
            "对悲伤、严肃、负面情绪的说说，立刻收起玩笑，用真诚而克制的语气；\n"
            "对炫耀、晒图、日常吐槽，自然延续氛围即可，不需要强行升华或夸张。\n"
            "</expression_principles>"
        )

        blocks: list[str] = [introduce_block, personality_block, principles_block]

        # 4. 可选 background_story
        if personality.background_story and len(personality.background_story) >= 10:
            blocks.append(
                "<background_story>\n"
                "（作为行动依据，不要在评论或回复中直接复述背景故事）\n"
                f"{personality.background_story}\n"
                "</background_story>"
            )

        return "\n\n".join(blocks)

    def _get_now_info(self) -> tuple[str, str]:
        """获取当前时间和星期信息。"""
        now = datetime.datetime.now()
        current_time = now.strftime("%Y年%m月%d日 %H:%M")
        weekday_names = [
            "星期一",
            "星期二",
            "星期三",
            "星期四",
            "星期五",
            "星期六",
            "星期日",
        ]
        return current_time, weekday_names[now.weekday()]

    async def _recognize_images(self, image_urls: list[str]) -> str:
        """识别说说配图内容并返回描述块。"""
        if not image_urls:
            return ""

        url_to_desc = await self._batch_recognize_images(image_urls)
        descriptions: list[str] = []
        for index, url in enumerate(image_urls, start=1):
            desc = url_to_desc.get(url, "")
            if desc:
                descriptions.append(f"图片{index}：{desc}")

        if descriptions:
            return "\n\n[说说配图描述]\n" + "\n".join(descriptions)
        return f"\n\n[说说包含 {len(image_urls)} 张图片]"

    async def _batch_recognize_images(self, image_urls: list[str]) -> dict[str, str]:
        """批量识别图片，返回 ``{url: description}`` 字典（去重）。

        Args:
            image_urls: 图片 URL 列表（可含重复）

        Returns:
            URL → 描述 字典，识别失败的 URL 不在结果中。
        """
        if not image_urls:
            return {}

        unique_urls = list({url for url in image_urls if url})
        if not unique_urls:
            return {}

        try:
            from src.app.plugin_system.api.media_api import recognize_media
        except ImportError:
            logger.debug("无法导入 media_api，跳过批量识图。")
            return {}

        result: dict[str, str] = {}
        timeout = aiohttp.ClientTimeout(total=_IMAGE_DOWNLOAD_TIMEOUT)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in unique_urls:
                try:
                    async with session.get(url) as response:
                        if response.status != 200:
                            logger.warning(f"下载图片失败 HTTP {response.status}: {url}")
                            continue
                        image_bytes = await response.read()

                    image_base64 = base64.b64encode(image_bytes).decode("ascii")
                    description = await asyncio.wait_for(
                        recognize_media(image_base64, "image", use_cache=True),
                        timeout=_VLM_TIMEOUT,
                    )
                    if description:
                        result[url] = description
                except asyncio.TimeoutError:
                    logger.warning(f"图片识别超时: {url}")
                except Exception as exc:
                    logger.warning(f"识别图片失败 {url}: {exc}")

        return result

    @staticmethod
    def _format_comments_block(
        comments: list[dict[str, Any]],
        target_name: str,
        bot_qq: str | None = None,
    ) -> str:
        """格式化评论区文本。"""
        del target_name
        if not comments:
            return "暂无评论"

        lines: list[str] = []
        for comment in comments:
            nickname = str(comment.get("nickname", "未知"))
            content = str(comment.get("content", ""))
            time_str = str(comment.get("create_time", ""))

            if bot_qq and str(comment.get("qq_account", "")) == str(bot_qq):
                display_name = "你"
                content = re.sub(r"^@\S+\s*", "", content)
            else:
                display_name = nickname

            lines.append(f"- [{time_str}] {display_name}：{content}")

        return "\n".join(lines)

    def _clean_reply(self, text: str) -> str:
        """清理 LLM 返回文本中的格式噪音。"""
        cleaned = text.strip()
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        if cleaned.startswith("'") and cleaned.endswith("'"):
            cleaned = cleaned[1:-1]

        cleaned = re.sub(r"^回复\s*@[^:：]+[:：]?\s*", "", cleaned)
        cleaned = re.sub(r"^@[^:：\s]+[:：]?\s*", "", cleaned)
        return cleaned.strip()

    def _is_truncated_json(self, response: str) -> bool:
        """检测 JSON 文本是否明显被截断。"""
        stripped = response.strip()
        if stripped.startswith("{") and not stripped.endswith("}"):
            return True
        if stripped.startswith("[") and not stripped.endswith("]"):
            return True
        if stripped.startswith("{") and stripped.count('"') % 2 != 0:
            return True
        return False

    def _extract_text_from_broken_json(self, response: str) -> str:
        """从损坏 JSON 中尽量提取 text 字段。"""
        patterns = [
            r'"text"\s*:\s*"([^"\\]*)"',
            r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
            r'"text"\s*:\s*"([^\n}]*)',
        ]
        for pattern in patterns:
            match = re.search(pattern, response, re.DOTALL)
            if not match:
                continue

            extracted = match.group(1)
            extracted = (
                extracted.replace(r'\"', '"')
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
                .strip()
            )
            if len(extracted) >= 3 and not extracted.endswith(("\\", ",")):
                return extracted
        return ""

    def _format_story_time(self, story_time: str | None) -> str:
        """将数据库时间转为更适合提示词的中文时间。"""
        if not story_time:
            return ""
        try:
            dt = datetime.datetime.strptime(story_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return story_time
        return dt.strftime("%m月%d日 %H:%M")

    async def _get_prompt_template(self, name: str) -> PromptTemplate | None:
        """读取指定名称的提示词模板。"""
        try:
            return get_template(name)
        except Exception as exc:
            logger.error(f"读取提示词模板 '{name}' 失败: {exc}")
            return None

    def _get_model_set(self, task_name: str):
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
    ) -> str:
        """统一发送纯文本提示词并返回完整文本结果。"""
        model_set = self._get_model_set(task_name)
        if model_set is None:
            return ""

        log_llm_prompt(request_name, 用户消息=prompt_text)

        request = create_llm_request(model_set, request_name=request_name)
        request.add_payload(LLMPayload(ROLE.USER, [Text(prompt_text)]))
        response = await request.send(stream=False)
        return await response

    # ── 记忆 Tool 集成（booku_memory 暴露的所有 Tool / Agent） ──────────
    _MEMORY_PLUGIN_NAME: ClassVar[str] = "booku_memory"
    _MEMORY_MAX_TOOL_CALL_ROUNDS: ClassVar[int] = 3

    def _resolve_memory_tools(
        self,
    ) -> tuple[list[type], "object | None"]:
        """枚举 ``booku_memory`` 插件已注册的全部 Tool / Agent 组件。

        FoxZone 不对记忆插件采用 lite / agent 哪种模式做出约束——由
        ``booku_memory`` 自己决定暴露哪些组件，本方法只负责把这些组件
        汇集成可注入到 LLM 请求的列表。

        Returns:
            (component_classes, owner_plugin)：

            - ``component_classes``：``booku_memory`` 当前注册的所有 ``TOOL``
              与 ``AGENT`` 组件类列表；
            - ``owner_plugin``：``booku_memory`` 插件实例（用于实例化组件，
              使其内部能正确读取 ``BookuMemoryConfig``）。

            集成开关关闭、插件未加载或没有任何 Tool/Agent 暴露时返回 ``([], None)``。
        """
        memory_cfg = getattr(self._cfg, "memory", None)
        if memory_cfg is None or not bool(getattr(memory_cfg, "enable_memory_integration", False)):
            return [], None

        owner_plugin = get_plugin(self._MEMORY_PLUGIN_NAME)
        if owner_plugin is None:
            logger.debug(
                f"记忆 Tool 集成：插件 {self._MEMORY_PLUGIN_NAME} 未加载，跳过"
            )
            return [], None

        from src.core.components.types import ComponentType

        registry = get_global_registry()
        component_classes: list[type] = []
        for ctype in (ComponentType.TOOL, ComponentType.AGENT):
            try:
                mapping = registry.get_by_plugin_and_type(
                    self._MEMORY_PLUGIN_NAME, ctype
                )
            except Exception:
                mapping = {}
            component_classes.extend(mapping.values())

        if not component_classes:
            logger.debug(
                f"记忆 Tool 集成：{self._MEMORY_PLUGIN_NAME} 未注册任何 Tool/Agent，跳过"
            )
            return [], None

        return component_classes, owner_plugin

    @staticmethod
    def _stringify_tool_result(value: object) -> str:
        """将 Tool execute 返回的结果序列化为字符串供 LLM 阅读。"""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    @staticmethod
    def _instantiate_memory_component(
        component_cls: type,
        owner_plugin: object,
        request_name: str,
    ) -> object:
        """根据组件构造签名实例化 Tool 或 Agent。

        - ``BaseTool`` 子类：``cls(plugin)``；
        - ``BaseAgent`` 子类：``cls(stream_id, plugin)``，其中 ``stream_id``
          以 ``foxzone:<request_name>`` 形式构造，标识 FoxZone 离线场景；
        - 其他可调用：按 ``cls(plugin)`` 兜底。

        通过 ``inspect`` 读取 ``__init__`` 形参列表来判断，避免硬编码到
        框架的具体类层级。
        """
        try:
            sig = inspect.signature(component_cls.__init__)
            params = [
                name
                for name, p in sig.parameters.items()
                if name != "self"
                and p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
        except (TypeError, ValueError):
            params = []

        if "stream_id" in params:
            return component_cls(stream_id=f"foxzone:{request_name}", plugin=owner_plugin)  # type: ignore[call-arg]
        return component_cls(owner_plugin)  # type: ignore[call-arg]

    async def _send_prompt_with_memory_tools(
        self,
        task_name: str,
        request_name: str,
        prompt_text: str,
    ) -> str:
        """带 booku_memory 组件调用能力的 LLM 多轮循环执行入口。

        - 集成开关关闭、插件未加载或未注册任何 Tool/Agent 时，自动回退到
          ``_send_prompt``，保证既有调用方不被破坏；
        - LLM 每一轮可调用任意数量的组件，本方法按调用顺序串行执行；
        - 达到 ``_MEMORY_MAX_TOOL_CALL_ROUNDS`` 仍未给出最终文本时，按当前累计的
          ``message`` 字段截断返回。
        """
        component_classes, owner_plugin = self._resolve_memory_tools()
        if not component_classes or owner_plugin is None:
            return await self._send_prompt(task_name, request_name, prompt_text)

        model_set = self._get_model_set(task_name)
        if model_set is None:
            return ""

        log_llm_prompt(request_name, 用户消息=prompt_text)

        registry = ToolRegistry()
        for cls in component_classes:
            try:
                registry.register(cls)
            except Exception as exc:
                logger.warning(f"记忆组件注册失败，已跳过：{cls!r} err={exc}")

        if not registry.get_all():
            return await self._send_prompt(task_name, request_name, prompt_text)

        request = create_llm_request(model_set, request_name=request_name)
        request.add_payload(LLMPayload(ROLE.USER, [Text(prompt_text)]))
        request.add_payload(LLMPayload(ROLE.TOOL, registry.get_all()))  # type: ignore[arg-type]

        max_rounds = self._MEMORY_MAX_TOOL_CALL_ROUNDS

        response = await request.send(stream=False)
        await response  # 触发收集 message + call_list

        for round_idx in range(max_rounds):
            calls = response.call_list or []
            if not calls:
                break

            for call in calls:
                tool_cls = registry.get(call.name)
                if tool_cls is None:
                    result_text = f"未注册的工具: {call.name}"
                else:
                    args = call.args if isinstance(call.args, dict) else {}
                    try:
                        instance = self._instantiate_memory_component(
                            tool_cls, owner_plugin, request_name
                        )
                        # 过滤 LLM 可能多塞的未声明字段（如 reason），避免 execute() TypeError
                        exec_sig = inspect.signature(instance.execute)  # type: ignore[attr-defined]
                        if not any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in exec_sig.parameters.values()
                        ):
                            valid_keys = set(exec_sig.parameters.keys())
                            dropped = [k for k in args if k not in valid_keys]
                            if dropped:
                                logger.debug(
                                    f"记忆组件 {call.name} 忽略未声明参数: {dropped}"
                                )
                                args = {k: v for k, v in args.items() if k in valid_keys}
                        ok, raw = await instance.execute(**args)  # type: ignore[attr-defined]
                        result_text = self._stringify_tool_result(raw)
                        logger.debug(
                            f"记忆组件调用：{call.name} ok={ok}, "
                            f"result_preview={result_text[:120]!r}"
                        )
                    except Exception as exc:
                        result_text = f"工具执行异常: {exc}"
                        logger.warning(
                            f"记忆组件调用失败：{call.name} args={args} err={exc}"
                        )
                response.add_payload(
                    LLMPayload(
                        ROLE.TOOL_RESULT,
                        [ToolResult(value=result_text, call_id=call.id, name=call.name)],
                    )
                )

            if round_idx == max_rounds - 1:
                logger.warning(
                    f"记忆组件调用达到最大轮次 {max_rounds}，返回当前累计文本。"
                )
                break

            response = await response.send(stream=False)
            await response

        return response.message or ""

    async def generate_story(self, topic: str, context: str | None = None) -> str:
        """生成纯文本 QQ 空间说说。"""
        template = await self._get_prompt_template("foxzone.story.generate")
        if template is None:
            return ""

        current_time, weekday = self._get_now_info()
        personality_desc = self._get_personality_desc()
        topic_desc = f"主题：{topic}" if topic else "主题不限"
        history = await self._get_send_history()
        image_prompt_guide = "(本条说说不需要配图。)\n"

        prompt_text = await (
            template.set("personality_desc", personality_desc)
            .set("current_time", current_time)
            .set("weekday", weekday)
            .set("topic_desc", topic_desc)
            .set("image_prompt_guide", image_prompt_guide)
            .set("output_format", '{"text": "说说正文内容"}')
            .set("history", history)
            .build()
        )
        recent_self = await self._get_recent_self_feeds_block(num=3)
        if recent_self:
            prompt_text += (
                "\n\n<recent_self_feeds>\n"
                "以下是你最近发过的说说快照（包含原文、配图描述、评论区），"
                "供你参考连贯上下文、避免重复选题：\n"
                f"{recent_self}\n"
                "</recent_self_feeds>"
            )
        if context:
            prompt_text += f"\n\n作为参考，以下是一些最近的聊天记录：\n---\n{context}\n---"

        response_text = await self._send_prompt_with_memory_tools(
            self._cfg.llm.story_model_task,
            "foxzone.story.generate",
            prompt_text,
        )
        story_text = self._parse_simple_json_text(response_text)
        if story_text:
            logger.info(f"成功生成说说：'{story_text}'")
        else:
            logger.error("生成说说内容失败或为空。")
        return story_text

    async def generate_story_with_image_info(
        self,
        topic: str,
        context: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """生成说说内容及其配图提示信息。"""
        template = await self._get_prompt_template("foxzone.story.generate")
        if template is None:
            return "", {}

        current_time, weekday = self._get_now_info()
        personality_desc = self._get_personality_desc()
        topic_desc = f"主题：{topic}" if topic else "主题不限"
        history = await self._get_send_history()

        provider = self._cfg.ai_image.provider
        style_prompt = self._cfg.novelai.character_prompt.strip()
        base_negative = self._cfg.novelai.base_negative_prompt.strip()
        if provider == "novelai":
            output_format = (
                '{"text": "说说正文内容", '
                '"image": {'
                '"prompt": "详细英文提示词（不写画质头，从 1girl 等角色数性别开始）", '
                '"negative_prompt": "针对性负面词（基础负面词会自动并入）", '
                '"aspect_ratio": "方图"}}'
            )
            style_anchor = (
                f"\n画风锚点（自动注入到 prompt 头部，**不要重复写画质头**）：\n{style_prompt}\n"
                if style_prompt
                else ""
            )
            base_neg_block = (
                f"\n基础负面词（自动并入 negative_prompt，**不要重复写**）：\n{base_negative}\n"
                if base_negative
                else ""
            )
            image_prompt_guide = (
                "请同时生成适用于 NovelAI 的配图信息。\n"
                f"{style_anchor}"
                f"{base_neg_block}"
                "**仅当画面主体是人物（自己出镜的自拍 / 角色照 / 含人物场景）时配图**，"
                "纯静物/纯风景/纯抒情说说请将 image.prompt 留空字符串。\n"
                "image.prompt 为英文 tag 序列（不含画质头），"
                "aspect_ratio 只能是 方图/横图/竖图。\n"
                "角色外貌请从上方人设描述里自行推导成 NovelAI tag（发色/瞳色/服装/气质等）写入 prompt。\n"
            )
        else:
            output_format = (
                '{"text": "说说正文内容", '
                '"image": {"prompt": "详细英文场景描述"}}'
            )
            image_prompt_guide = (
                "请同时生成适用于 SiliconFlow 的英文配图描述。\n"
                "image.prompt 需包含主体、场景、氛围与光线。\n"
            )

        prompt_text = await (
            template.set("personality_desc", personality_desc)
            .set("current_time", current_time)
            .set("weekday", weekday)
            .set("topic_desc", topic_desc)
            .set("image_prompt_guide", image_prompt_guide)
            .set("output_format", output_format)
            .set("history", history)
            .build()
        )
        recent_self = await self._get_recent_self_feeds_block(num=3)
        if recent_self:
            prompt_text += (
                "\n\n<recent_self_feeds>\n"
                "以下是你最近发过的说说快照（包含原文、配图描述、评论区），"
                "供你参考连贯上下文、避免重复选题：\n"
                f"{recent_self}\n"
                "</recent_self_feeds>"
            )
        if context:
            prompt_text += f"\n\n作为参考，以下是一些最近的聊天记录：\n---\n{context}\n---"

        response_text = await self._send_prompt_with_memory_tools(
            self._cfg.llm.story_model_task,
            "foxzone.story.generate_with_image",
            prompt_text,
        )
        story_text, image_info = self._parse_story_with_image_json(response_text)
        if story_text:
            logger.info(f"成功生成带配图说说：'{story_text}'")
        else:
            logger.error("生成带配图说说失败或内容为空。")
        return story_text, image_info

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

        personality_desc = self._get_personality_desc()
        current_time, _ = self._get_now_info()
        bot_qq = self._cfg.general.bot_qq

        # 收集所有说说配图 URL，批量识别后注入提示词
        all_image_urls: list[str] = []
        for item in comment_items:
            all_image_urls.extend(str(u) for u in item.get("feed_images", []) if u)
        image_descs = await self._batch_recognize_images(all_image_urls) if all_image_urls else {}

        comment_items_block = self._format_batch_comment_items(
            comment_items, bot_qq, image_descs=image_descs
        )

        prompt_text = await (
            template.set("personality_desc", personality_desc)
            .set("current_time", current_time)
            .set("comment_items_block", comment_items_block)
            .build()
        )
        recent_self = await self._get_recent_self_feeds_block(num=3)
        if recent_self:
            prompt_text += (
                "\n\n<recent_self_feeds>\n"
                "以下是你最近发过的说说快照（包含原文、配图描述、评论区），"
                "便于你在回复评论时联想到自己当时发说说的语境与心情：\n"
                f"{recent_self}\n"
                "</recent_self_feeds>"
            )

        response_text = await self._send_prompt_with_memory_tools(
            self._cfg.llm.comment_model_task,
            "foxzone.comment.reply.batch",
            prompt_text,
        )

        decisions = self._parse_batch_decisions_json(response_text)
        logger.info(
            f"批量评论决策完成：{len(comment_items)} 条评论，"
            f"{sum(1 for d in decisions if d.get('reply'))} 条决定回复。"
        )
        return decisions

    async def generate_feed_decisions(
        self,
        feed_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量生成好友说说评论决策（点赞已由 Adapter 完成，仅决策评论）。

        Args:
            feed_items: 说说项列表，每项需含 tid, target_qq, content,
                        created_time, image_text, comment_count 字段

        Returns:
            决策列表，每项为 ``{"tid": str, "target_qq": str, "comment": str | None}``。
            ``comment`` 为 None 表示仅点赞、不写评论。
        """
        if not feed_items:
            return []

        template = await self._get_prompt_template("foxzone.friend.feed.interact")
        if template is None:
            return []

        personality_desc = self._get_personality_desc()
        current_time, _ = self._get_now_info()
        feed_items_block = self._format_feed_items_block(feed_items)

        prompt_text = await (
            template.set("personality_desc", personality_desc)
            .set("current_time", current_time)
            .set("feed_items_block", feed_items_block)
            .build()
        )

        response_text = await self._send_prompt_with_memory_tools(
            self._cfg.llm.comment_model_task,
            "foxzone.friend.feed.interact",
            prompt_text,
        )

        decisions = self._parse_feed_decisions_json(response_text)
        logger.info(
            f"好友说说评论决策完成：{len(feed_items)} 条说说，"
            f"决定评论 {sum(1 for d in decisions if d.get('comment'))} 条。"
        )
        return decisions

    def _format_feed_items_block(
        self,
        feed_items: list[dict[str, Any]],
    ) -> str:
        """将好友说说列表格式化为提示词文本块。

        Args:
            feed_items: 说说项列表

        Returns:
            格式化后的多说说描述文本
        """
        blocks: list[str] = []
        total = len(feed_items)
        for i, item in enumerate(feed_items, 1):
            tid = item.get("tid", "")
            target_qq = item.get("target_qq", "")
            content = str(item.get("content", "（无正文）")).strip()
            created_time = self._format_story_time(item.get("created_time")) or "未知时间"
            image_text = item.get("image_text", "")
            comment_count = int(item.get("comment_count", 0))

            block = (
                f"=== 说说 {i}/{total} ===\n"
                f"好友 QQ：{target_qq}  发布时间：{created_time}\n"
                f"正文：「{content[:200]}{'…' if len(content) > 200 else ''}」\n"
            )
            if image_text:
                block += f"{image_text}\n"
            if comment_count:
                block += f"当前评论数：{comment_count}\n"
            block += f"[meta] tid={tid}  target_qq={target_qq}"
            blocks.append(block)

        return "\n\n".join(blocks)

    def _parse_feed_decisions_json(
        self,
        response: str,
    ) -> list[dict[str, Any]]:
        """解析好友说说互动决策 JSON 数组。

        Args:
            response: LLM 原始响应文本

        Returns:
            决策列表，每项保证含 tid、target_qq、like、comment 字段。
        """
        raw = self._strip_markdown_fence(response)
        try:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                import json5
                data = json5.loads(raw)

            if not isinstance(data, list):
                logger.warning(f"好友说说决策响应不是 JSON 数组: {raw[:200]}")
                return []

            result: list[dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                tid = str(item.get("tid", "")).strip()
                target_qq = str(item.get("target_qq", "")).strip()
                if not (tid and target_qq):
                    continue
                comment_raw = item.get("comment")
                comment: str | None = None
                if comment_raw is not None:
                    comment = self._clean_reply(str(comment_raw))
                    if not comment:
                        comment = None
                result.append({"tid": tid, "target_qq": target_qq, "comment": comment})

            return result
        except Exception as exc:
            logger.error(f"解析好友说说决策 JSON 失败: {exc}，响应内容: {raw[:300]}")
            return []

    def _format_batch_comment_items(
        self,
        comment_items: list[dict[str, Any]],
        bot_qq: str,
        image_descs: dict[str, str] | None = None,
    ) -> str:
        """将批量评论项格式化为提示词文本块（楼中楼对话链视图）。

        Args:
            comment_items: 评论项列表
            bot_qq: Bot 的 QQ 号，用于在评论区中高亮 Bot 自己的发言
            image_descs: 说说配图 URL → 视觉描述映射；缺失则按"[图片]"占位

        Returns:
            格式化后的多评论描述文本
        """
        descs = image_descs or {}
        blocks: list[str] = []
        for index, item in enumerate(comment_items, start=1):
            feed_id = item.get("feed_id", "")
            feed_content = item.get("feed_content", "（无说说内容）").strip()
            story_time = self._format_story_time(item.get("story_time")) or "未知时间"
            comment_tid = str(item.get("comment_tid", ""))
            comment_content = item.get("comment_content", "").strip()
            commenter_name = item.get("commenter_name", "未知用户")
            comment_time = self._format_story_time(item.get("comment_time")) or "未知时间"
            all_comments: list[dict[str, Any]] = item.get("all_comments", [])
            feed_images: list[str] = [str(u) for u in item.get("feed_images", []) if u]
            is_reply_to_bot: bool = bool(item.get("is_reply_to_bot"))
            parent_content: str = str(item.get("parent_content", "")).strip()

            threaded = self._format_threaded_comments(
                all_comments, bot_qq=bot_qq, highlight_tid=comment_tid
            )

            image_lines: list[str] = []
            for j, url in enumerate(feed_images, 1):
                desc = descs.get(url, "")
                image_lines.append(f"图片{j}：{desc if desc else '[图片]'}")
            image_block_text = ("\n" + "\n".join(image_lines)) if image_lines else ""

            # 当前评论的语境标识
            if is_reply_to_bot:
                context_line = (
                    f"⚠ 这条评论是 **{commenter_name}** 在接你的话——"
                    f"你之前说过：「{parent_content}」，对方现在回应：「{comment_content}」。\n"
                    f"必须承接上下文，回应对方的话题/疑问，禁止重起新话题。"
                )
            else:
                context_line = (
                    f"评论者：{commenter_name}  评论时间：{comment_time}\n"
                    f"评论内容：「{comment_content}」"
                )

            block = (
                f"=== 评论 {index}/{len(comment_items)} ===\n"
                f"你的说说（{story_time}）：「{feed_content}」"
                f"{image_block_text}\n"
                f"{context_line}\n"
                f"该说说完整对话链（>>> 标记的是本次需要决策的那条）：\n{threaded}\n"
                f"[meta] comment_tid={comment_tid}  feed_id={feed_id}"
            )
            blocks.append(block)

        return "\n\n".join(blocks)

    @staticmethod
    def _format_threaded_comments(
        comments: list[dict[str, Any]],
        bot_qq: str | None,
        highlight_tid: str = "",
    ) -> str:
        """以楼中楼结构格式化评论区。

        顶层评论（``parent_tid`` 为空）按发布时间罗列；指向某条顶层评论的
        子回复按时间紧跟其下，缩进展示。Bot 自己的评论标记 ``（你）``，
        与 ``highlight_tid`` 匹配的评论加上 ``>>> `` 前缀。

        Args:
            comments: 扁平评论列表（已含 ``parent_tid`` 字段）
            bot_qq: Bot QQ 号，用于识别 Bot 自己发的评论
            highlight_tid: 当前需要决策的评论 tid

        Returns:
            多行字符串；评论列表为空时返回 "暂无评论"
        """
        if not comments:
            return "暂无评论"

        # 建索引：comment_tid → comment
        by_tid: dict[str, dict[str, Any]] = {
            str(c.get("comment_tid", "")): c for c in comments if c.get("comment_tid")
        }

        def _resolve_root(c: dict[str, Any]) -> str:
            """沿 parent_tid 链向上查找顶层评论 tid（QZone 子回复彼此 @ 时也会嵌套）。"""
            seen: set[str] = set()
            cur = c
            for _ in range(10):  # 防御循环引用
                pid = str(cur.get("parent_tid") or "").strip()
                if not pid or pid in seen:
                    return str(cur.get("comment_tid", ""))
                seen.add(pid)
                parent = by_tid.get(pid)
                if parent is None:
                    return str(cur.get("comment_tid", ""))
                cur = parent
            return str(cur.get("comment_tid", ""))

        # 分桶：顶层 / 子回复（按根 tid 分组）
        top_levels: list[dict[str, Any]] = []
        children_map: dict[str, list[dict[str, Any]]] = {}
        for c in comments:
            parent = str(c.get("parent_tid") or "").strip()
            if not parent:
                top_levels.append(c)
            else:
                root_tid = _resolve_root(c)
                if root_tid and root_tid != str(c.get("comment_tid", "")):
                    children_map.setdefault(root_tid, []).append(c)
                else:
                    # 父评论缺失，降级当顶层
                    top_levels.append(c)

        def _render(c: dict[str, Any], indent: str) -> str:
            tid = str(c.get("comment_tid", ""))
            nickname = str(c.get("nickname", "未知"))
            content = str(c.get("content", ""))
            time_str = str(c.get("create_time", ""))

            if bot_qq and str(c.get("qq_account", "")) == str(bot_qq):
                display_name = "你"
                content = re.sub(r"^@\S+\s*", "", content)
            else:
                display_name = nickname

            marker = ">>> " if highlight_tid and tid == highlight_tid else ""
            return f"{indent}{marker}[{time_str}] {display_name}：{content}"

        lines: list[str] = []
        for top in top_levels:
            lines.append(_render(top, indent="· "))
            top_tid = str(top.get("comment_tid", ""))
            for child in children_map.get(top_tid, []):
                lines.append(_render(child, indent="    └─ "))

        return "\n".join(lines)

    def _parse_batch_decisions_json(
        self,
        response: str,
    ) -> list[dict[str, Any]]:
        """解析批量回复决策 JSON 数组。

        Args:
            response: LLM 原始响应文本

        Returns:
            决策列表，每项保证含 comment_tid、feed_id 字段；
            reply 字段为 None 或非空字符串。
        """
        raw = self._strip_markdown_fence(response)
        try:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                import json5
                data = json5.loads(raw)

            if not isinstance(data, list):
                logger.warning(f"批量决策响应不是 JSON 数组: {raw[:200]}")
                return []

            result: list[dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                comment_tid = str(item.get("comment_tid", "")).strip()
                feed_id = str(item.get("feed_id", "")).strip()
                if not (comment_tid and feed_id):
                    continue
                reply_raw = item.get("reply")
                if reply_raw is None:
                    reply: str | None = None
                else:
                    reply = self._clean_reply(str(reply_raw))
                    if not reply:
                        reply = None
                result.append({"comment_tid": comment_tid, "feed_id": feed_id, "reply": reply})

            return result
        except Exception as exc:
            logger.error(f"解析批量决策 JSON 失败: {exc}，响应内容: {raw[:300]}")
            return []

    @staticmethod
    def _strip_markdown_fence(response: str) -> str:
        """移除响应中的 Markdown 代码块包裹。"""
        raw = response.strip()
        if raw.startswith("```json"):
            raw = raw[7:]
        elif raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        return raw.strip()

    def _parse_simple_json_text(self, response: str) -> str:
        """解析 {"text": "..."} 格式响应。"""
        raw = self._strip_markdown_fence(response)
        if self._is_truncated_json(raw):
            logger.warning("检测到响应 JSON 被截断，尝试提取 text 字段。")
            return self._extract_text_from_broken_json(raw)

        try:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                import json5

                data = json5.loads(raw)
            if not isinstance(data, dict):
                return self._extract_text_from_broken_json(raw)
            return str(data.get("text", "")).strip()
        except Exception as exc:
            logger.warning(f"解析 JSON 失败: {exc}，尝试直接提取 text 字段。")
            return self._extract_text_from_broken_json(raw)

    def _parse_story_with_image_json(
        self,
        response: str,
    ) -> tuple[str, dict[str, Any]]:
        """解析包含 image 字段的 JSON 响应。"""
        raw = self._strip_markdown_fence(response)
        if self._is_truncated_json(raw):
            logger.warning("检测到配图 JSON 被截断，尝试提取文本部分。")
            return self._extract_text_from_broken_json(raw), {}

        try:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                import json5

                data = json5.loads(raw)
            if not isinstance(data, dict):
                return self._extract_text_from_broken_json(raw), {}

            story_text = str(data.get("text", "")).strip()
            image_info = data.get("image", {})
            if not isinstance(image_info, dict):
                image_info = {}
            return story_text, image_info
        except Exception as exc:
            logger.error(f"解析配图 JSON 失败: {exc}，尝试提取纯文本部分。")
            return self._extract_text_from_broken_json(raw), {}

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

    async def _get_recent_self_feeds_block(self, num: int = 3) -> str:
        """读取自己最近 N 条说说的完整快照（正文/图片描述/评论区）。

        通过 service registry 反向调用 QZoneService.get_recent_self_feeds_block，
        用于在「发说说」「回复自己说说下评论」时为 LLM 提供上下文。

        Args:
            num: 取多少条最近说说，默认 3 条

        Returns:
            完整的多行文本块；失败或无说说时返回空字符串。
        """
        try:
            from src.app.plugin_system.api.service_api import get_service

            service = get_service("foxzone:service:qzone_service")
            if service is None:
                return ""
            return await service.get_recent_self_feeds_block(num=num)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning(f"读取最近说说快照失败: {exc}")
            return ""