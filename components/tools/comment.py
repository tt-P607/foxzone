"""QZoneCommentTool：在 QQ 空间指定说说下发表评论。

供外部 Chatter 通过 LLM Tool Calling 直接调用，跳过 Agent 中间层，
适合外部 chatter 自己已经看过说说、决定要怎么评论的精细控制场景。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from ...core.interaction_log import ACTION_COMMENT, SOURCE_AGENT
from .. import SERVICE_SIG

logger = get_logger("foxzone.tool.comment", color=COLOR.CYAN)

_SERVICE_SIG = SERVICE_SIG

#: ``QZoneCommentTool.tool_description`` 中拼接给 LLM 的评论硬性约束。
#:
#: 这是 Tool 的硬编码"调用合同"（schema 描述），不属于用户可调提示词，
#: 所以不外置到 ``config.toml``。它**不会**被替换为 ``config.prompts.comment_guidelines``。
_TOOL_COMMENT_GUIDELINES: str = (
    "【QZone 评论统一规范（必须严格遵守）】\n"
    "1. 字数严格控制在 30 字以内。\n"
    "2. 自然口语化，符合人格特征，禁止任何 Emoji。\n"
    "3. 禁止在开头添加 @某人，系统会自动处理。\n"
    "4. 不要写「期待你下次分享」「等你更新」之类诱导对方回复的话。\n"
    "5. 多条评论之间避免重复的句式 / 开场词 / 句尾点缀。\n"
    "6. 人设里反复出现的标签词是底色，不要让它们在评论里几乎每条都跳出来。"
)


class QZoneCommentTool(BaseTool):
    """在指定说说下发表评论的对外 Tool。"""

    tool_name = "qzone_post_comment"
    tool_description = (
        "在指定 QQ 用户的某条说说下发表一条评论。"
        "需要先通过 qzone_read_feed 读取说说获得 tid，再用此工具评论。"
        "适合外部 chatter 自己看完说说后精细控制评论内容的场景。\n\n"
        + _TOOL_COMMENT_GUIDELINES
    )

    async def execute(
        self,
        target_qq: Annotated[str, "说说主人的 QQ 号（纯数字）"],
        feed_id: Annotated[str, "说说的 tid（帖子 ID，由 qzone_read_feed 返回）"],
        comment_text: Annotated[
            str,
            "评论正文：必须 ≤ 30 字、自然口语化、禁 emoji、勿在开头 @ 任何人。"
            "详见 tool_description 末尾的 QZone 评论统一规范。",
        ],
    ) -> tuple[bool, str]:
        """发表评论。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid
            comment_text: 评论正文

        Returns:
            (是否成功, 结果摘要)
        """
        from ..service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            return False, "FoxZone 服务未注册"

        text = comment_text.strip()
        if not text:
            return False, "评论内容不能为空"

        tq = target_qq.strip()
        fid = feed_id.strip()
        if not tq.isdigit():
            return False, f"'{target_qq}' 不是有效的 QQ 号"
        if not fid:
            return False, "feed_id 不能为空"

        ok = await service.comment(target_qq=tq, feed_id=fid, text=text)
        if ok:
            await service.mark_interaction(tq, fid, ACTION_COMMENT, SOURCE_AGENT)
            return True, f"已成功评论（tid={fid}）：{text}"
        return False, f"评论失败（tid={fid}）"
