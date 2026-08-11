"""统一的批量图片视觉识别。

批量下载图片并调用视觉 LLM 生成描述，结果经 vision_cache 持久化缓存。
QZone 图片 URL 长期不变，跨重启缓存可避免对同一图片重复推理。
"""

from __future__ import annotations

import asyncio
import typing
from io import BytesIO

import aiohttp

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.types import Image, LLMPayload, ROLE, Text

if typing.TYPE_CHECKING:
    from ..vision_cache import ImageVisionCache

logger = get_logger("foxzone.vision", color=COLOR.ORANGE)

#: 单张图片识别的最大并发数。
_MAX_CONCURRENCY = 5
#: 单张图片 LLM 识别超时（秒）。
_RECOGNIZE_TIMEOUT = 30
#: 整批下载会话总超时（秒）。
_SESSION_TIMEOUT = 60

_RECOGNIZE_PROMPT = (
    "请描述这张图片，字数控制在100字以内。简要说明图片主题、核心元素及背景环境。"
    "如能识别图片来源（如动漫、游戏、影视等），仅在完全确认时才可简要注明，"
    "否则不得猜测或提及来源，直接客观描述即可。"
    "如果图片中包含任何文字或代码，请完整转述，这部分不计入字数限制，"
    "力求客观、生动地还原图片内容。"
)


async def describe_images(
    urls: list[str],
    vision_task: str,
    cache: "ImageVisionCache",
) -> dict[str, str]:
    """批量获取图片的视觉识别描述（有缓存则复用，否则调用 vision LLM）。

    Args:
        urls: 图片 URL 列表
        vision_task: model.toml 中的视觉模型任务名；为空则跳过识图
        cache: 持久化识别结果缓存

    Returns:
        ``{url: description}`` 字典，识别失败或未识别的 URL 不在结果中
    """
    task_name = vision_task.strip()
    if not task_name:
        return {}

    result: dict[str, str] = {}
    to_recognize: list[str] = []

    for url in urls:
        if not url:
            continue
        cached = cache.get(url)
        if cached:
            result[url] = cached
        else:
            to_recognize.append(url)

    if not to_recognize:
        return result

    try:
        model_set = get_model_set_by_task(task_name)
    except Exception as exc:
        logger.warning(f"视觉识别模型任务 '{task_name}' 不可用，跳过识图: {exc}")
        return result

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _recognize_one(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
        """下载并识别单张图片，返回 (url, description)；失败/超时返回空字符串。"""
        async with sem:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"下载图片失败（HTTP {resp.status}）: {url}")
                        return url, ""
                    image_data = await resp.read()

                request = create_llm_request(model_set, request_name="foxzone.vision")
                request.add_payload(
                    LLMPayload(
                        ROLE.USER,
                        [
                            Image(BytesIO(image_data)),  # type: ignore[arg-type]
                            Text(_RECOGNIZE_PROMPT),
                        ],
                    )
                )
                response = await asyncio.wait_for(
                    request.send(stream=False), timeout=_RECOGNIZE_TIMEOUT
                )
                description = (await response or "").strip()
                return url, description
            except asyncio.TimeoutError:
                logger.warning(f"识别图片超时（>{_RECOGNIZE_TIMEOUT}s）: {url}")
                return url, ""
            except Exception as exc:
                logger.warning(f"识别图片失败 {url}: {exc}")
                return url, ""

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_SESSION_TIMEOUT)
    ) as session:
        pairs = await asyncio.gather(*[_recognize_one(session, url) for url in to_recognize])

    for url, description in pairs:
        if description:
            cache.set(url, description)
            result[url] = description

    await cache.save()
    return result


async def download_images(urls: list[str]) -> list[str]:
    """下载图片 URL 为 base64 data 字符串（供多模态模式直接传给模型）。

    Args:
        urls: 图片 URL 列表

    Returns:
        ``base64|<bytes>`` 格式的 data 字符串列表；下载失败或非 200 的 URL 被跳过。
    """
    if not urls:
        return []

    import base64

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _download_one(session: aiohttp.ClientSession, url: str) -> str | None:
        """下载单张图片并编码为 base64 data；失败返回 None。"""
        async with sem:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"下载图片失败（HTTP {resp.status}）: {url}")
                        return None
                    image_data = await resp.read()
                encoded = base64.b64encode(image_data).decode("ascii")
                return f"base64|{encoded}"
            except Exception as exc:
                logger.warning(f"下载图片失败 {url}: {exc}")
                return None

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_SESSION_TIMEOUT)
    ) as session:
        results = await asyncio.gather(
            *[_download_one(session, url) for url in urls]
        )

    return [r for r in results if r]


async def fill_image_text(
    feed_items: list[dict],
    vision_task: str,
    cache: "ImageVisionCache",
) -> None:
    """收集 feed 项中的图片 URL、批量识别并回填 ``image_text`` 字段。

    原逻辑在 service 与 chatter 中重复出现两次，此处收敛为一处。

    Args:
        feed_items: 说说项列表，每项可含 ``images``（URL 列表）
        vision_task: 视觉模型任务名
        cache: 识别缓存
    """
    all_image_urls: list[str] = []
    for item in feed_items:
        all_image_urls.extend(str(u) for u in item.get("images", []) if u)
    if not all_image_urls:
        return

    logger.info(f"开始批量识别 {len(all_image_urls)} 张说说配图…")
    try:
        image_descs = await describe_images(all_image_urls, vision_task, cache)
        logger.info(f"图片识别完成：{len(image_descs)}/{len(all_image_urls)} 张。")
        for item in feed_items:
            urls: list[str] = item.get("images", [])
            if urls:
                item["image_text"] = "\n".join(
                    f"图片{j}：{image_descs.get(u, '[图片]')}"
                    for j, u in enumerate(urls, 1)
                )
    except Exception as exc:
        logger.warning(f"图片识别失败，使用占位符继续: {exc}")
