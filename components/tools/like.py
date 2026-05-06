"""QZoneLikeTool：为 QQ 空间指定说说点赞。

供外部 Chatter 通过 LLM Tool Calling 直接调用，跳过 Agent 中间层，
适合外部 chatter 自己已经看过说说、决定要点赞的精细控制场景。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from ...core.interaction_log import ACTION_LIKE, SOURCE_AGENT
from .. import SERVICE_SIG

logger = get_logger("foxzone.tool.like", color=COLOR.CYAN)

_SERVICE_SIG = SERVICE_SIG


class QZoneLikeTool(BaseTool):
    """为指定说说点赞的对外 Tool。"""

    tool_name = "qzone_like_feed"
    tool_description = (
        "为指定 QQ 用户的某条说说点赞。"
        "需要先通过 qzone_read_feed 读取说说获得 tid，再用此工具点赞。"
        "适合外部 chatter 自己看完说说后决定点赞的场景。"
    )

    async def execute(
        self,
        target_qq: Annotated[str, "说说主人的 QQ 号（纯数字）"],
        feed_id: Annotated[str, "说说的 tid（帖子 ID，由 qzone_read_feed 返回）"],
    ) -> tuple[bool, str]:
        """点赞。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid

        Returns:
            (是否成功, 结果摘要)
        """
        from ..service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            return False, "FoxZone 服务未注册"

        tq = target_qq.strip()
        fid = feed_id.strip()
        if not tq.isdigit():
            return False, f"'{target_qq}' 不是有效的 QQ 号"
        if not fid:
            return False, "feed_id 不能为空"

        ok = await service.like(target_qq=tq, feed_id=fid)
        if ok:
            await service.mark_interaction(tq, fid, ACTION_LIKE, SOURCE_AGENT)
            return True, f"已为说说 {fid} 点赞"
        return False, f"点赞失败（tid={fid}）"
