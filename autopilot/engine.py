"""BatchSendEngine：批量发送执行器。

统一「批量决策后逐条发送」的通用骨架：随机抖动、失败重试、
限流分类、成功/限流/收尾回调。三条自治流程（自己说说评论回复、
好友动态评论、外部接力回复）共用本引擎，业务差异通过回调注入。

锁的获取/释放使用 ``async with``，从结构上杜绝旧版
``acquire()``/``release()`` 手动配对在异常路径上的锁泄漏。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from src.app.plugin_system.api.log_api import COLOR, get_logger

logger = get_logger("foxzone.batch_engine", color=COLOR.ORANGE)

#: 发送回调：接收 item，返回是否成功。抛 RuntimeError 视为不可重试错误。
SenderFunc = Callable[[dict[str, Any]], Awaitable[bool]]
#: 事件回调：接收 item。
ItemHook = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class BatchPolicy:
    """批量发送策略参数。

    Attributes:
        jitter_range: 相邻两次发送之间的随机等待秒数区间
        max_attempts: 单条最大尝试次数（首发 + 重试）
        backoff_base: 重试退避基数（第 n 次重试等待 backoff_base * n 秒）
        stop_batch_on_rate_limit: 遇限流时是否终止整批
            （好友监控场景为 True——剩余项保持未标记以便下轮再试；
            外部接力场景为 False——单条跳过继续处理后续）
    """

    jitter_range: tuple[float, float] = (15.0, 30.0)
    max_attempts: int = 3
    backoff_base: float = 3.0
    stop_batch_on_rate_limit: bool = False


@dataclass
class BatchResult:
    """批量执行结果。

    Attributes:
        succeeded: 成功发送条数
        skipped: 跳过条数（决策为 None 或限流跳过）
        rate_limited: 本批是否触发过限流
        lines: 每条的决策摘要行（供日志面板展示）
    """

    succeeded: int = 0
    skipped: int = 0
    rate_limited: bool = False
    lines: list[str] = field(default_factory=list)


class BatchSendEngine:
    """批量发送执行器：抖动 + 重试 + 限流分类 + 回调。"""

    def __init__(self, lock: asyncio.Lock, policy: BatchPolicy) -> None:
        """初始化引擎。

        Args:
            lock: 发送串行锁（应传入 runtime.reply_send_lock，跨批次串行发送）
            policy: 策略参数
        """
        self._lock = lock
        self._policy = policy

    async def run(
        self,
        items: list[dict[str, Any]],
        *,
        sender: SenderFunc,
        should_send: Callable[[dict[str, Any]], bool],
        label: Callable[[dict[str, Any]], str],
        on_success: ItemHook | None = None,
        on_rate_limited: ItemHook | None = None,
        on_finally: ItemHook | None = None,
    ) -> BatchResult:
        """执行批量发送。

        对每个 item：

        1. ``should_send(item)`` 为 False → 计入 skipped，仅执行 ``on_finally``；
        2. 否则发送（非首条前随机抖动），失败按退避重试；
        3. ``sender`` 抛 ``RuntimeError`` 视为不可重试（cookie 失效 / -10049 限流），
           含 "-10049" 或 "限流" 关键字时归类为限流；
        4. 成功 → ``on_success``；限流 → ``on_rate_limited``（若
           ``stop_batch_on_rate_limit`` 为 True 则终止整批）；
        5. 无论成败最后执行 ``on_finally``（如标记已处理）。

        Args:
            items: 待处理项列表
            sender: 发送回调
            should_send: 判断该项是否需要实际发送（False 表示决策为跳过）
            label: 生成该项的日志标签
            on_success: 发送成功后的回调（如标记互动、递增计数）
            on_rate_limited: 限流跳过时的回调（如标记已处理避免重复触发）
            on_finally: 每项收尾回调（无论成败均执行）

        Returns:
            批量执行结果
        """
        result = BatchResult()
        sent_so_far = 0

        async with self._lock:
            for item in items:
                item_label = label(item)

                if not should_send(item):
                    result.skipped += 1
                    result.lines.append(f"· {item_label} → 跳过")
                    if on_finally is not None:
                        await on_finally(item)
                    continue

                if sent_so_far > 0:
                    delay = random.uniform(*self._policy.jitter_range)
                    logger.debug(f"批量发送间隔：等待 {delay:.1f}s")
                    await asyncio.sleep(delay)

                ok = False
                rate_limited = False
                last_err: Exception | None = None
                for attempt in range(self._policy.max_attempts):
                    try:
                        ok = await sender(item)
                        if ok:
                            break
                        last_err = None
                    except RuntimeError as exc:
                        # 不可重试错误（cookie 失效 / QZone 限流）
                        last_err = exc
                        ok = False
                        rate_limited = "-10049" in str(exc) or "限流" in str(exc)
                        logger.warning(
                            f"发送遇到不可重试错误，停止重试 {item_label}: {exc}"
                        )
                        break
                    except Exception as exc:
                        last_err = exc
                        ok = False
                    if attempt < self._policy.max_attempts - 1:
                        backoff = self._policy.backoff_base * (attempt + 1)
                        logger.warning(
                            f"发送失败 {item_label}，{backoff:.0f}s 后重试 "
                            f"({attempt + 1}/{self._policy.max_attempts - 1})"
                            + (f"：{last_err}" if last_err else "")
                        )
                        await asyncio.sleep(backoff)

                sent_so_far += 1

                if ok:
                    result.succeeded += 1
                    result.lines.append(f"✓ {item_label} → 发送成功")
                    if on_success is not None:
                        await on_success(item)
                    if on_finally is not None:
                        await on_finally(item)
                    continue

                result.lines.append(f"✗ {item_label} → 发送失败")
                logger.error(f"批量发送失败 {item_label}")

                if rate_limited:
                    result.rate_limited = True
                    if on_rate_limited is not None:
                        await on_rate_limited(item)
                    result.skipped += 1
                    if self._policy.stop_batch_on_rate_limit:
                        logger.warning(
                            "QZone 限流，终止本批剩余发送（未处理项保持未标记，下轮再试）"
                        )
                        return result
                    # 单条跳过继续（外部接力路径）
                    logger.warning(
                        f"QZone 限流，跳过该项并继续处理后续: {item_label}"
                    )
                    if on_finally is not None:
                        await on_finally(item)
                    continue

                if on_finally is not None:
                    await on_finally(item)

        return result
