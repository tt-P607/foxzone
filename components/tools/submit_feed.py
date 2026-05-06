"""QZoneSubmitFeedTool：提交并发布说说（执行 Tool）。

外部 chatter 在调用过 ``qzone_start_compose_feed`` 阅读指引之后，
通过本 Tool 把写好的正文与（可选的）image_info 提交、立即发布。
本 Tool 不再做任何 LLM 调用，参数结构由调用方按指引自行构造。
"""

from __future__ import annotations

from typing import Annotated, Any

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from .. import SERVICE_SIG

logger = get_logger("foxzone.tool.submit_feed", color=COLOR.CYAN)

_SERVICE_SIG = SERVICE_SIG


class QZoneSubmitFeedTool(BaseTool):
    """提交并发布说说的执行 Tool。"""

    tool_name = "qzone_submit_feed"
    tool_description = (
        "提交并发布一条 QQ 空间说说。调用前请先调用 qzone_start_compose_feed 阅读发说说指引。"
        "content 是你写好的说说正文；如果想配图，把按指引写好的 image_info 字典一起传，"
        "不需要配图就省略 image_info。"
    )

    async def execute(
        self,
        content: Annotated[str, "已写好的说说正文（必填）"],
        image_info: Annotated[
            dict[str, Any] | None,
            "可选的配图描述字典，结构详见 qzone_start_compose_feed 返回的指引："
            "{prompt, negative_prompt, aspect_ratio}。"
            "不需要配图请省略此参数。",
        ] = None,
    ) -> tuple[bool, str]:
        """提交并发布说说。

        Args:
            content: 说说正文
            image_info: 可选 image_info 字典

        Returns:
            (是否成功, 结果摘要)
        """
        from ..service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            return False, "FoxZone 服务未注册"

        text = content.strip()
        if not text:
            return False, "content 不能为空"

        result = await service.publish_feed_with_image_info(text, image_info)
        ok = bool(result.get("success", False))
        message = str(result.get("message", ""))
        if ok:
            return True, f"已成功发布说说：\n\n{message}"
        return False, message or "说说发布失败"
