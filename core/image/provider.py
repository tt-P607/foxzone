"""图片生成 provider 抽象层。

定义 ``ImageProvider`` Protocol 与 ``ImageGenResult`` 类型别名，
作为各生图厂商接入的统一契约。所有具体 provider（NovelAI / SiliconFlow /
OpenAI 兼容协议）都通过适配后满足该 Protocol，由 ``ImageDispatcher``
按 ``cfg.ai_image.provider`` 选择运行时实例。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: 统一的生图返回三元组：``(是否成功, 落地图片路径, 消息)``。
ImageGenResult = tuple[bool, Path | None, str]


@runtime_checkable
class ImageProvider(Protocol):
    """图片生成 provider 协议。

    Attributes:
        provider_id: provider 标识符，与 ``config.ai_image.provider`` 取值一致
            （目前为 ``"novelai" | "siliconflow" | "openai"`` 之一）。
    """

    provider_id: str

    def is_available(self) -> bool:
        """运行时是否可用（API key / model_set 等已配置）。"""
        ...

    def format_guidance(self) -> str:
        """返回本 provider 已填充完成的发说说图像指引文本。

        每个 provider 负责读取自身配置段并填充专属占位符（如 NovelAI
        的 style_block / base_neg_block），从而让 dispatcher / service 完全不需感知
        各 provider 的接入细节。
        """
        ...

    async def generate(
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        aspect_ratio: str = "方图",
        extras: dict[str, Any] | None = None,
    ) -> ImageGenResult:
        """生成一张图片。

        Args:
            prompt: 主提示词（具体语义由各 provider 决定，参见 prompts.py 中
                按 provider 分发的 image guidance 段）。
            negative_prompt: 追加负面词，仅 NovelAI 真正支持，其他 provider 会忽略。
            aspect_ratio: 画幅类型，``"方图" | "竖图" | "横图"``。
            extras: 预留扩展参数（暂未使用）。

        Returns:
            ``(success, image_path, message)`` 三元组。
        """
        ...
