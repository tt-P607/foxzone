"""QZoneCommentTool：在 QQ 空间指定说说下发表评论。

供外部 Chatter 通过 LLM Tool Calling 直接调用，跳过 Agent 中间层，
适合外部 chatter 自己已经看过说说、决定要怎么评论的精细控制场景。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from ...core.http.comments import QZoneUnknownResultError
from .. import SERVICE_SIG

logger = get_logger("foxzone.tool.comment", color=COLOR.CYAN)

#: ``QZoneCommentTool.description`` 中拼接给 LLM 的评论硬性约束。
#:
#: 这是 Tool 的硬编码"调用合同"（schema 描述），与 ``prompts.py`` 的
#: ``COMMENT_GUIDELINES`` 独立维护，不随其替换。
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

    name = "qzone_post_comment"
    description = (
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
        comment_id: Annotated[
            str | int | None,
            "可选：要回复的评论 ID；与 comment_uin 成对提供，不填表示普通说说评论。",
        ] = None,
        comment_uin: Annotated[
            str | None,
            "可选：目标评论作者 QQ；与 comment_id 成对提供。",
        ] = None,
        parent_comment_id: Annotated[
            str | int | None,
            "二级回复必填：所属一级评论 ID；与 parent_comment_uin 成对提供。",
        ] = None,
        parent_comment_uin: Annotated[
            str | None,
            "二级回复必填：所属一级评论作者 QQ。",
        ] = None,
    ) -> tuple[bool, str]:
        """发表评论。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid
            comment_text: 评论正文
            comment_id: 可选的精确目标评论 ID
            comment_uin: 可选的目标评论作者 QQ
            parent_comment_id: 二级回复的一级评论 ID
            parent_comment_uin: 二级回复的一级评论作者 QQ

        Returns:
            (是否成功, 结果摘要)
        """
        from ..service import QZoneService

        service: QZoneService | None = get_service(SERVICE_SIG)  # type: ignore[assignment]
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

        target_id = ("" if comment_id is None else str(comment_id)).strip()
        target_uin = (comment_uin or "").strip()
        parent_id = ("" if parent_comment_id is None else str(parent_comment_id)).strip()
        parent_uin = (parent_comment_uin or "").strip()
        if (
            comment_id is not None or comment_uin is not None
            or parent_comment_id is not None or parent_comment_uin is not None
        ) and not (target_id and target_uin):
            return False, "精确回复身份不能为空，未发送评论。"
        if bool(target_id) != bool(target_uin):
            return False, "精确回复必须同时提供 comment_id 和 comment_uin，未发送评论。"
        if bool(parent_id) != bool(parent_uin):
            return False, "父评论 ID 和作者必须成对提供，未发送评论。"
        if parent_id and not target_id:
            return False, "父评论字段只能用于精确二级回复，未发送评论。"

        try:
            if target_id:
                ok = await service.reply_comment(
                    feed_id=fid,
                    host_qq=tq,
                    reply_text=text,
                    comment_tid=target_id,
                    comment_uin=target_uin,
                    parent_comment_id=parent_id,
                    parent_comment_uin=parent_uin,
                )
                if ok:
                    return True, f"已成功回复评论（tid={fid}, comment_id={target_id}）。"
                return False, (
                    f"回复未发送：目标评论无法唯一核实，或二级回复缺少父评论身份"
                    f"（tid={fid}）。"
                )

            ok = await service.comment(target_qq=tq, feed_id=fid, text=text)
            if ok:
                return True, f"已成功评论（tid={fid}）：{text}"
            return False, f"评论失败（tid={fid}）"
        except QZoneUnknownResultError as exc:
            return False, f"评论结果待核实，已阻止自动重发：{exc}"
