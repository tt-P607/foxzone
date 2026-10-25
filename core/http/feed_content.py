"""好友动态 HTML 正文与图片解析。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import bs4
from bs4.element import NavigableString, Tag

_EXCLUDED_CLASSES = {
    "f-item-passive",
    "f-ct-video",
    "mod-comments",
    "comments-list",
    "ui-avatar",
    "avatar",
    "f-avatar",
    "f-user-avatar",
    "user-avatar",
    "comments-op",
}
_IMAGE_ATTRIBUTES = (
    "data-originurl",
    "data-url3",
    "data-url2",
    "data-url1",
    "data-smallurl",
    "truesrc",
    "data-src",
    "src",
)
_URL_VARIANTS = ("url3", "url2", "url1", "smallurl")


def _excluded(node: Tag, root: Tag) -> bool:
    """检查节点是否位于评论、转发或视频区域。"""
    parent = node.parent
    while isinstance(parent, Tag) and parent is not root:
        if _EXCLUDED_CLASSES.intersection(parent.get("class", [])):
            return True
        parent = parent.parent
    return False


def _elements(root: Tag, selector: str) -> Iterator[Tag]:
    """按文档顺序返回当前动态内未排除的匹配节点。"""
    for node in root.select(selector):
        if not _excluded(node, root):
            yield node


def _render(node: Tag) -> str:
    """保留正文换行和表情 alt，移除展开控件。"""
    chunks: list[str] = []

    def visit(item: Tag | NavigableString) -> None:
        if isinstance(item, NavigableString):
            chunks.append(str(item))
            return
        if not isinstance(item, Tag):
            return
        if item.name in {"script", "style"}:
            return
        if _EXCLUDED_CLASSES.intersection(item.get("class", [])):
            return
        if item.get("data-cmd") == "qz_toggle" or "load_img" in item.get("class", []):
            return
        if item.name == "br":
            chunks.append("\n")
            return
        if item.name == "img":
            chunks.append(str(item.get("alt", "")))
            return
        block = item.name in {"div", "p", "li"}
        if block and chunks and not chunks[-1].endswith("\n"):
            chunks.append("\n")
        for child in item.children:
            visit(child)
        if block and chunks and not chunks[-1].endswith("\n"):
            chunks.append("\n")

    visit(node)
    return "".join(chunks).replace("\xa0", " ").strip()


def _image_url(value: Any) -> str | None:
    """规范化一个 HTTP(S) 图片地址。"""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return raw


def _image_from_item(item: Tag) -> str | None:
    """按动态 HTML 和结构化图片的服务端优先级选图。"""
    for attribute in _IMAGE_ATTRIBUTES[:5]:
        image = _image_url(item.get(attribute))
        if image:
            return image
    for image_node in item.select("img"):
        for attribute in _IMAGE_ATTRIBUTES:
            image = _image_url(image_node.get(attribute))
            if image:
                return image
    for field in _URL_VARIANTS:
        image = _image_url(item.get(field))
        if image:
            return image
    return None


def _count(value: Any) -> int | None:
    """将服务端非负整数字段转换为整数，保留未知状态。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 9_007_199_254_740_991 else None
    if isinstance(value, str) and value.isdecimal() and len(value) <= 16:
        number = int(value)
        return number if number <= 9_007_199_254_740_991 else None
    return None


def parse_feed_content(
    html: str,
    key: str,
    uin: str | int,
    appid: str | int,
    like_count: Any = None,
    comment_count: Any = None,
) -> dict[str, Any]:
    """解析单条好友动态，不读取评论、转发及视频封面内容。"""
    soup = bs4.BeautifulSoup(html, "html.parser")
    body = soup.find("div", class_="f-item", attrs={"data-key": key})
    result: dict[str, Any] = {
        "tid": None,
        "content": None,
        "content_complete": None,
        "images": None,
        "like_count": _count(like_count),
        "comment_count": _count(comment_count),
    }
    if not isinstance(body, Tag):
        return result

    metadata = next(
        (
            item
            for item in _elements(body, '[name="feed_data"]')
            if item.get("data-uin") == str(uin) and item.get("data-fkey") == key
        ),
        None,
    )
    raw_tid = metadata.get("data-tid") if metadata else None
    if str(appid) == "311" and isinstance(raw_tid, str) and raw_tid.strip():
        result["tid"] = raw_tid.strip()

    content_nodes = list(_elements(body, ".f-info"))
    complete = next(
        (
            item
            for item in content_nodes
            if "qz_info_complete" in item.get("class", []) and _render(item)
        ),
        None,
    )
    content = complete or next(
        (item for item in content_nodes if "qz_info_complete" not in item.get("class", [])),
        None,
    )
    if content is not None:
        result["content"] = _render(content)
        result["content_complete"] = complete is not None or (
            "qz_info_cut" not in content.get("class", [])
            and not bool(content.select('[data-cmd="qz_toggle"]'))
        )

    images: list[str] = []
    seen: set[str] = set()
    for item in _elements(body, ".img-item"):
        if item.has_attr("data-v_itemid") or item.has_attr("data-videotype"):
            continue
        image = _image_from_item(item)
        if image and image not in seen:
            images.append(image)
            seen.add(image)
    result["images"] = images

    root = body
    for parent in body.parents:
        if isinstance(parent, Tag) and "f-single" in parent.get("class", []):
            root = parent
            break
    like = next(_elements(root, ".qz_like_btn_v3"), None)
    if like is not None:
        result["like_count"] = result["like_count"] if result["like_count"] is not None else _count(
            like.get("data-likecnt")
        )
    return result