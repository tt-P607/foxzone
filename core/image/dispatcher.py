"""图片生成 provider 调度器。

根据 ``cfg.ai_image.provider`` 选择具体 provider 并对外暴露统一接口。
按 provider 分发的「发说说指引」段（image guidance）由 provider 自身的
``format_guidance()`` 内聚处理，调度器仅做透明转发。

设计目标：上层（QZoneService / Tool）不再需要 ``if provider == ...`` 分支，
全部通过本调度器透明分派；新增/移除 provider 仅需调整本文件 + 增添
``ImageProvider`` 实现，service 侧无需任何感知。
"""

from __future__ import annotations

import typing
from pathlib import Path

from src.app.plugin_system.api.log_api import COLOR, get_logger

from ...prompts import IMAGE_GUIDANCE_NOVELAI, IMAGE_GUIDANCE_SILICONFLOW
from .novelai import NovelAIService
from .openai import OpenAIImageService
from .provider import ImageProvider
from .siliconflow import ImageService

if typing.TYPE_CHECKING:
    from ...plugin import FoxZonePlugin

logger = get_logger("foxzone.image_dispatcher", color=COLOR.ORANGE)


class _NovelAIAdapter:
    """将现有 ``NovelAIService`` 适配到 ``ImageProvider`` 协议。

    NovelAI 的 ``format_guidance`` 会注入 ``style_anchor`` / ``base_negative``
    两块运行时配置，让 LLM 知道画风锚点和默认负面词都会被自动注入到 prompt。
    """

    provider_id: str = "novelai"

    def __init__(self, plugin: "FoxZonePlugin", svc: NovelAIService) -> None:
        self._plugin = plugin
        self._svc = svc

    def is_available(self) -> bool:
        return self._svc.is_available()

    def format_guidance(self) -> str:
        cfg = self._plugin.config.novelai
        style_anchor = cfg.character_prompt.strip()
        base_negative = cfg.base_negative_prompt.strip()
        style_block = (
            f"<style_anchor>会自动加到 prompt 头部，不要重复写：\n{style_anchor}\n</style_anchor>"
            if style_anchor
            else "<style_anchor>（未配置画风锚点）</style_anchor>"
        )
        base_neg_block = (
            f"<base_negative>会自动并入 negative_prompt，不要重复写：\n{base_negative}\n</base_negative>"
            if base_negative
            else "<base_negative>（未配置基础负面词）</base_negative>"
        )
        return IMAGE_GUIDANCE_NOVELAI.format(
            style_block=style_block, base_neg_block=base_neg_block
        )

    async def generate(
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        aspect_ratio: str = "方图",
        extras: dict | None = None,
    ) -> tuple[bool, Path | None, str]:
        del extras
        return await self._svc.generate_image_from_prompt_data(
            prompt=prompt,
            negative_prompt=negative_prompt,
            aspect_ratio=aspect_ratio,
        )


class _SiliconFlowAdapter:
    """将现有 ``ImageService`` 适配到 ``ImageProvider`` 协议。"""

    provider_id: str = "siliconflow"

    def __init__(self, svc: ImageService) -> None:
        self._svc = svc

    def is_available(self) -> bool:
        return self._svc.is_available()

    def format_guidance(self) -> str:
        return IMAGE_GUIDANCE_SILICONFLOW

    async def generate(
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        aspect_ratio: str = "方图",
        extras: dict | None = None,
    ) -> tuple[bool, Path | None, str]:
        del negative_prompt, aspect_ratio, extras
        success, path = await self._svc.generate_image_from_prompt(prompt=prompt)
        message = "生成成功" if success else "SiliconFlow 生成失败"
        return success, path, message


class ImageDispatcher:
    """生图 provider 调度器。

    Attributes:
        _plugin: 宿主插件实例
        _providers: ``provider_id -> ImageProvider`` 注册表
    """

    def __init__(self, plugin: "FoxZonePlugin") -> None:
        """初始化调度器并预先实例化全部已知 provider。"""
        self._plugin = plugin
        self._providers: dict[str, ImageProvider] = {
            "novelai": _NovelAIAdapter(plugin, NovelAIService(plugin)),
            "siliconflow": _SiliconFlowAdapter(ImageService(plugin)),
            "openai": OpenAIImageService(plugin),
        }

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def current_provider_id(self) -> str:
        """返回当前配置中选定的 provider 标识。"""
        return str(self._plugin.config.ai_image.provider).strip()

    def get_provider(self, provider_id: str | None = None) -> ImageProvider | None:
        """获取指定（或当前配置）provider 实例。"""
        pid = provider_id or self.current_provider_id()
        return self._providers.get(pid)

    def get_guidance(self, provider_id: str | None = None) -> str:
        """返回指定 provider 已格式化好的图像指引段。

        实际工作内聚在各 provider 的 ``format_guidance()`` 中，调度器仅做转发。
        """
        provider = self.get_provider(provider_id)
        if provider is None:
            return ""
        return provider.format_guidance()

    async def generate(
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        aspect_ratio: str = "方图",
    ) -> tuple[bool, Path | None, str]:
        """按当前配置的 provider 生成图片。"""
        pid = self.current_provider_id()
        provider = self.get_provider(pid)
        if provider is None:
            msg = f"未知 provider: {pid!r}"
            logger.warning(msg)
            return False, None, msg

        if not provider.is_available():
            msg = f"provider {pid!r} 未配置/不可用"
            logger.warning(msg)
            return False, None, msg

        try:
            return await provider.generate(
                prompt=prompt,
                negative_prompt=negative_prompt,
                aspect_ratio=aspect_ratio,
            )
        except Exception as exc:
            logger.error(f"provider {pid!r} 生图异常: {exc}", exc_info=True)
            return False, None, f"生成异常: {exc!s}"

