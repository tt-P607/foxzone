"""QZoneChatter：QQ 空间评论智能回复 Chatter。

监听来自 QZoneAdapter 的批量评论消息，调用 LLM 批量决策是否回复、如何回复，
并通过 QZoneService 发送到 QQ 空间。
"""

from __future__ import annotations

import asyncio
import random
from typing import AsyncGenerator, Any

from src.app.plugin_system.base import BaseChatter, Wait, Success, Failure, Stop
from src.app.plugin_system.api.log_api import get_logger, COLOR
from src.app.plugin_system.api.service_api import get_service
from ..core.content import log_llm_prompt
from ..core.interaction_log import ACTION_COMMENT

from ..config import FoxZoneConfig
from . import SERVICE_SIG
from .service import (
    is_local_seq_tid as _is_local_seq_tid,
    resolve_root_comment_tid as _resolve_root_comment_tid,
)

logger = get_logger("foxzone.chatter", color=COLOR.MAGENTA)

_SERVICE_SIG = SERVICE_SIG


class QZoneChatter(BaseChatter):
    """QQ 空间评论智能回复 Chatter。

    处理由 QZoneAdapter 以批量格式投递的 QQ 空间评论消息。
    LLM 一次性接收本轮所有新评论，自主决定哪些值得回复、如何回复，
    最终将选定的回复通过 QZoneService 写回 QQ 空间。

    Class Attributes:
        chatter_name: Chatter 注册名称
        chatter_description: Chatter 描述
        associated_platforms: 仅处理 qzone 平台消息
    """

    chatter_name = "qzone_chatter"
    chatter_description = "QQ 空间评论批量智能回复 Chatter"
    associated_platforms = ["qzone"]

    async def execute(self) -> AsyncGenerator[Wait | Success | Failure | Stop, None]:
        """执行一轮批量评论回复。

        从聊天流中取出当前批量消息，将所有新评论交给 LLM 统一决策，
        对决定回复的评论发送回复，对所有已处理的评论标记为已完成。

        Yields:
            Failure: 消息缺失、服务不可用或 LLM 调用失败时返回
            Stop: 无消息时返回
            Success: 批量处理完成时返回
        """
        from src.app.plugin_system.api.stream_api import activate_stream

        logger.info(f"QZoneChatter.execute() 触发，stream_id={self.stream_id[:8]}")
        chat_stream = await activate_stream(self.stream_id)
        if chat_stream is None:
            logger.error(f"无法激活聊天流: {self.stream_id}")
            yield Failure("无法激活聊天流")
            return

        context = chat_stream.context
        if not context.unread_messages:
            yield Stop(time=0)
            return

        # 取最新一条未读消息（适配器每轮只投递一条批量 Envelope）
        message = context.unread_messages[-1]

        # ── 提取批量数据 ──────────────────────────────────────────────
        raw: dict[str, Any] = message.raw_data or {}
        if not raw.get("batch_mode"):
            logger.error(
                f"QZoneChatter 收到非批量格式消息，已跳过: message_id={message.message_id}"
            )
            yield Failure("消息格式不兼容：需要 batch_mode=True 的批量消息")
            return

        comment_items: list[dict[str, Any]] = raw.get("comment_items", [])
        friend_feed_items: list[dict[str, Any]] = raw.get("friend_feed_items", [])

        if not comment_items and not friend_feed_items:
            logger.debug("批量消息中无可处理项，跳过。")
            yield Stop(time=0)
            return

        # ── 获取服务实例 ──────────────────────────────────────────────
        from .service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            yield Failure("无法获取 QZoneService")
            return

        # ── 好友说说互动模式（仅决策评论；点赞已由 Adapter 完成）──────
        if friend_feed_items:
            logger.info(
                f"进入好友说说评论决策流程：共 {len(friend_feed_items)} 条已点赞说说。"
            )
            # 批量收集所有图片 URL，调用视觉模型统一识别后回填 image_text
            all_image_urls: list[str] = []
            for item in friend_feed_items:
                all_image_urls.extend(item.get("images", []))
            if all_image_urls:
                logger.info(
                    f"开始批量识别 {len(all_image_urls)} 张好友说说配图…"
                )
                try:
                    image_descs = await service.describe_images(all_image_urls)
                    logger.info(
                        f"图片识别完成：{len(image_descs)}/{len(all_image_urls)} 张。"
                    )
                    for item in friend_feed_items:
                        urls: list[str] = item.get("images", [])
                        if urls:
                            item["image_text"] = "\n".join(
                                f"图片{j}：{image_descs.get(u, '[图片]')}"
                                for j, u in enumerate(urls, 1)
                            )
                except Exception as exc:
                    logger.warning(f"图片识别失败，使用占位符继续: {exc}")

            logger.info(
                f"批量决策 {len(friend_feed_items)} 条已点赞好友说说是否需要评论…"
            )
            try:
                feed_decisions = await service.generate_feed_decisions(friend_feed_items)
            except Exception as e:
                logger.error(f"批量生成好友说说评论决策时发生异常: {e}")
                yield Failure(f"好友说说评论决策失败: {e}")
                return

            # 将决策按 tid 建索引，便于查找
            decision_map: dict[str, str | None] = {
                d["tid"]: d.get("comment")
                for d in feed_decisions
                if d.get("tid")
            }

            commented_count = 0
            decision_lines: list[str] = []
            # 在"将要发评论"的项之间加 15~30 秒随机间隔，防 QZone 风控
            sent_so_far = 0
            for item in friend_feed_items:
                tid = item.get("tid", "")
                target_qq = item.get("target_qq", "")
                if not (tid and target_qq):
                    continue

                comment_text = decision_map.get(tid)
                decision_label = f"(qq={target_qq}, tid={tid})"

                if comment_text:
                    # 非首条评论前等待 15~30s 随机间隔
                    if sent_so_far > 0:
                        delay = random.uniform(15, 30)
                        logger.debug(f"评论发送间隔：等待 {delay:.1f}s")
                        await asyncio.sleep(delay)

                    # 失败重试：最多尝试 3 次（首发 + 2 次重试），退避 3s/6s
                    ok = False
                    last_err: Exception | None = None
                    for attempt in range(3):
                        try:
                            ok = await service.comment(
                                target_qq=target_qq, feed_id=tid, text=comment_text
                            )
                            if ok:
                                break
                            last_err = None
                        except Exception as exc:
                            last_err = exc
                            ok = False
                        if attempt < 2:
                            backoff = 3 * (attempt + 1)
                            logger.warning(
                                f"评论发送失败 {decision_label}，{backoff}s 后重试 "
                                f"({attempt + 1}/2)" + (f"：{last_err}" if last_err else "")
                            )
                            await asyncio.sleep(backoff)

                    sent_so_far += 1

                    if ok:
                        commented_count += 1
                        await service.mark_interaction(target_qq, tid, ACTION_COMMENT)
                        logger.info(f"评论成功 {decision_label}：「{comment_text}」")
                        decision_lines.append(
                            f"✓ [qq={target_qq} tid={tid}] → 评论：{comment_text}"
                        )
                    else:
                        logger.warning(f"评论失败 {decision_label}")
                        decision_lines.append(f"✗ [qq={target_qq} tid={tid}] → 评论失败")
                else:
                    decision_lines.append(f"· [qq={target_qq} tid={tid}] → 仅点赞")

            log_llm_prompt(
                "好友说说评论决策",
                决策结果="\n".join(decision_lines) if decision_lines else "（无）",
            )

            await self.flush_unreads([message])
            yield Success(
                message=(
                    f"处理 {len(friend_feed_items)} 条已点赞好友说说："
                    f"评论 {commented_count} 条"
                ),
                data={"commented": commented_count},
            )
            return

        # ── 自己说说评论回复模式 ──────────────────────────────────────
        cfg: FoxZoneConfig | None = self.plugin.config  # type: ignore[assignment]
        bot_qq = cfg.general.bot_qq if cfg is not None else ""

        # ── 批量调用 LLM 生成决策 ─────────────────────────────────────
        logger.info(f"批量处理 {len(comment_items)} 条新评论，正在请求 LLM 决策…")
        try:
            decisions = await service.generate_batch_replies(comment_items)
        except Exception as e:
            logger.error(f"批量生成回复决策时发生异常: {e}")
            yield Failure(f"批量生成回复失败: {e}")
            return

        # 将决策结果建立索引，便于按 comment_tid 查找
        decision_map: dict[str, str | None] = {
            d["comment_tid"]: d.get("reply")
            for d in decisions
            if d.get("comment_tid")
        }

        # 打印 LLM 决策摘要
        decision_lines: list[str] = []
        for item in comment_items:
            ctid = item.get("comment_tid", "")
            who = item.get("commenter_name", "?")
            reply = decision_map.get(ctid)
            if reply:
                decision_lines.append(f"✓ [{who}] → {reply}")
            else:
                decision_lines.append(f"✗ [{who}] → 跳过")
        log_llm_prompt(
            "批量评论回复决策",
            决策结果="\n".join(decision_lines) if decision_lines else "（无）",
        )

        # ── 执行回复并标记所有评论为已处理 ──────────────────────────
        replied_count = 0
        skipped_count = 0
        # 在"将要发回复"的项之间加 5~10 秒随机间隔，防 QZone 风控
        replies_sent = 0

        for item in comment_items:
            comment_tid = item.get("comment_tid", "")
            feed_id = item.get("feed_id", "")
            commenter_name = item.get("commenter_name", "未知用户")

            if not (comment_tid and feed_id):
                continue

            reply_text = decision_map.get(comment_tid)

            if reply_text:
                # LLM 决定回复
                commenter_qq = item.get("commenter_qq", "")
                # host_qq：说说主人 QQ，外部空间回查时来自 item，自己空间评论时回退到 bot_qq
                host_qq = str(item.get("host_qq") or bot_qq)
                # QZone 楼中楼回复 API 的 commentId 必须是顶层一级评论的 tid。
                # 当被回复对象本身是二级评论（list_3，tid 形如 "1"/"2" 等局部序号）时，
                # 沿 parent_tid 链向上找到根（parent_tid 为空那条），用根 tid 作为 commentId。
                root_comment_tid = _resolve_root_comment_tid(
                    item.get("all_comments") or [], comment_tid
                )
                # 诊断日志：定位 -10049 是数据缺失还是真频控
                _all_c = item.get("all_comments") or []
                logger.debug(
                    f"reply 前 all_comments 摘要: total={len(_all_c)} "
                    f"target={comment_tid!r} parent={item.get('parent_tid')!r} "
                    f"resolved_root={root_comment_tid!r} "
                    f"tids=[{', '.join(str(c.get('comment_tid', '')) for c in _all_c[:20])}]"
                )
                # 预检：resolve 退化为局部序号说明 all_comments 缺一级父节点，
                # 强行 reply 必触发 -10049，直接跳过避免污染风控状态。
                if _is_local_seq_tid(root_comment_tid):
                    logger.warning(
                        f"resolve 退化为局部序号 root_tid={root_comment_tid!r}，"
                        f"all_comments 缺一级父节点，跳过 "
                        f"(feed_id={feed_id}, comment_tid={comment_tid})"
                    )
                    skipped_count += 1
                    await service.mark_comment_replied(feed_id, comment_tid)
                    continue

                if replies_sent > 0:
                    delay = random.uniform(15, 30)
                    logger.debug(f"回复发送间隔：等待 {delay:.1f}s")
                    await asyncio.sleep(delay)

                # 失败重试：最多尝试 3 次（首发 + 2 次重试），退避 3s/6s
                success = False
                last_err: Exception | None = None
                for attempt in range(3):
                    try:
                        success = await service.reply_comment(
                            feed_id=feed_id,
                            host_qq=host_qq,
                            target_name=commenter_name,
                            reply_text=reply_text,
                            comment_tid=root_comment_tid,
                            commenter_qq=commenter_qq,
                        )
                        if success:
                            break
                        last_err = None
                    except Exception as exc:
                        last_err = exc
                        success = False
                    if attempt < 2:
                        backoff = 3 * (attempt + 1)
                        logger.warning(
                            f"回复发送失败 (feed_id={feed_id}, comment_tid={comment_tid})，"
                            f"{backoff}s 后重试 ({attempt + 1}/2)"
                            + (f"：{last_err}" if last_err else "")
                        )
                        await asyncio.sleep(backoff)

                replies_sent += 1

                if success:
                    replied_count += 1
                    logger.info(
                        f"成功回复 '{commenter_name}' 的评论：'{reply_text}' "
                        f"(feed_id={feed_id}, comment_tid={comment_tid}, "
                        f"root_tid={root_comment_tid}, host_qq={host_qq})"
                    )
                else:
                    logger.error(
                        f"回复发送失败: feed_id={feed_id}, comment_tid={comment_tid}, "
                        f"root_tid={root_comment_tid}, host_qq={host_qq}"
                    )
            else:
                # LLM 决定不回复（或未提及该评论）
                skipped_count += 1
                logger.debug(
                    f"LLM 决定跳过 '{commenter_name}' 的评论 "
                    f"(feed_id={feed_id}, comment_tid={comment_tid})"
                )

            # 无论是否回复，均标记为已处理，避免下次轮询重复处理
            await service.mark_comment_replied(feed_id, comment_tid)

        logger.info(
            f"批量评论处理完成：共 {len(comment_items)} 条，"
            f"回复 {replied_count} 条，跳过 {skipped_count} 条。"
        )
        # 将已处理的批量消息从未读列表移入历史，避免重复处理
        await self.flush_unreads([message])

        yield Success(
            message=f"批量处理 {len(comment_items)} 条评论：回复 {replied_count} 条，跳过 {skipped_count} 条",
            data={"replied": replied_count, "skipped": skipped_count, "decisions": decision_map},
        )
