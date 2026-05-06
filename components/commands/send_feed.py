"""发送说说命令模块。

提供运营者手动触发发送 QQ 空间说说的聊天命令。
命令格式：/foxzone send [主题]
"""

from __future__ import annotations

from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.types import PermissionLevel

from .. import SERVICE_SIG


class SendFeedCommand(BaseCommand):
    """手动发送 QQ 空间说说命令。

    仅限 OPERATOR（运营者）级别权限使用。
    命令格式：``/foxzone send [主题]``

    示例：
        ``/foxzone send`` — 随机主题发说说
        ``/foxzone send 周末心情`` — 以"周末心情"为主题发说说
    """

    command_name = "foxzone"
    command_description = "FoxZone 管理命令，支持手动发布 QQ 空间说说"
    permission_level = PermissionLevel.OPERATOR
    command_prefix = "/"

    @cmd_route()
    async def handle_help(self) -> tuple[bool, str]:
        """返回 foxzone 命令帮助。"""
        return True, "用法：/foxzone send [主题]"

    @cmd_route("send")
    async def handle_send(
        self,
        w0: str = "",
        w1: str = "",
        w2: str = "",
        w3: str = "",
        w4: str = "",
        w5: str = "",
    ) -> tuple[bool, str]:
        """处理发送说说命令。

        Args:
            w0: 主题片段 1
            w1: 主题片段 2
            w2: 主题片段 3
            w3: 主题片段 4
            w4: 主题片段 5
            w5: 主题片段 6

        Returns:
            (是否成功, 回复消息)
        """
        from ..service import QZoneService

        service: QZoneService | None = get_service(SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            return False, "FoxZone 服务未注册。"

        if not service.plugin.config.general.enabled:  # type: ignore[union-attr]
            return False, "FoxZone 插件当前已禁用。"

        topic = " ".join(part for part in (w0, w1, w2, w3, w4, w5) if part).strip()
        result = await service.publish_generated_feed(topic=topic)

        if result["success"]:
            return True, f"说说已发布！内容：{result['message']}"
        else:
            return False, f"发布说说失败：{result['message']}"
