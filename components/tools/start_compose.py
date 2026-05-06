"""QZoneStartComposeFeedTool：发说说前的入口指引 Tool。

外部 chatter 决定要发说说时先调用这个 Tool，本 Tool 不做实际发布，
只返回一段「发说说指引」纯文本，包含：
- 发说说基调
- 你最近发过的说说快照（避免重复选题）
- 是否启用配图、配图触发条件
- image_info JSON schema
- NovelAI prompt 9 段式规则
- 当前生效的 style_anchor 与 base_negative

LLM 看完指引后，再调用 ``qzone_submit_feed`` 提交正文（与可选 image_info）。
"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from .. import SERVICE_SIG

logger = get_logger("foxzone.tool.start_compose", color=COLOR.CYAN)

_SERVICE_SIG = SERVICE_SIG


class QZoneStartComposeFeedTool(BaseTool):
    """发说说前的入口指引 Tool（无参）。"""

    tool_name = "qzone_start_compose_feed"
    tool_description = (
        "想发一条 QQ 空间说说时**先调用本工具**获取发说说指引（基调、最近说说快照、"
        "配图规则、NovelAI prompt 写法等）。本工具不会发布说说，只返回指引文本。"
        "读完指引后请调用 qzone_submit_feed 提交正文。"
    )

    async def execute(self) -> tuple[bool, str]:
        """返回发说说指引文本。

        Returns:
            (是否成功, 指引文本)
        """
        from ..service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            return False, "FoxZone 服务未注册"

        guidance = await service.compose_feed_guidance()
        if not guidance:
            return False, "生成发说说指引失败"
        return True, guidance
