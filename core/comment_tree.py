"""评论树工具模块。

QZone 评论区是两层结构：顶层一级评论 + list_3 楼中楼子回复。
顶层评论 tid 与 list_3 子评论 tid 各自从 1 开始编号，跨层级会复用相同
数字（如顶层 tid=1、某子评论 tid 也是 1），因此不能以全量 tid 字典
做索引，否则父子关系会串到错误的评论上。

顶层评论的 ``parent_tid`` 为空，且其 tid 在顶层内唯一；list_3 子回复
的 ``parent_tid`` 总是指向所属的顶层评论。本模块据此建立「仅含顶层
评论」的索引来渲染楼中楼视图。

本模块为纯函数实现，无任何框架依赖。
"""

from __future__ import annotations

import re
from typing import Any


def _build_top_index(
    comments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """构建「顶层评论」索引：仅收录 parent_tid 为空的评论。

    Args:
        comments: 扁平评论列表（含 list_3 子回复）

    Returns:
        ``{顶层评论 tid: 评论}``；顶层 tid 在顶层内唯一，重复时保留首个。
    """
    top_by_tid: dict[str, dict[str, Any]] = {}
    for c in comments:
        parent = str(c.get("parent_tid") or "").strip()
        if parent:
            continue
        ctid = str(c.get("comment_tid") or "").strip()
        if ctid and ctid not in top_by_tid:
            top_by_tid[ctid] = c
    return top_by_tid


def build_threaded_view(
    comments: list[dict[str, Any]],
    bot_qq: str | None,
    highlight_tid: str = "",
) -> str:
    """以楼中楼结构格式化评论区。

    顶层评论（``parent_tid`` 为空）按原始顺序罗列；子回复通过
    「顶层评论索引」归属到对应顶层之下，缩进展示。Bot 自己的评论
    显示为「你」，与 ``highlight_tid`` 匹配的评论加上 ``>>> `` 前缀。

    Args:
        comments: 扁平评论列表（已含 ``parent_tid`` 字段）
        bot_qq: Bot QQ 号，用于识别 Bot 自己发的评论
        highlight_tid: 当前需要决策的评论 tid

    Returns:
        多行字符串；评论列表为空时返回 "暂无评论"
    """
    if not comments:
        return "暂无评论"

    top_by_tid = _build_top_index(comments)
    top_levels: list[dict[str, Any]] = []
    children_map: dict[str, list[dict[str, Any]]] = {}
    for c in comments:
        parent = str(c.get("parent_tid") or "").strip()
        if not parent:
            top_levels.append(c)
            continue
        # 子回复：parent_tid 指向顶层评论；顶层缺失时降级当顶层
        if parent in top_by_tid:
            children_map.setdefault(parent, []).append(c)
        else:
            top_levels.append(c)

    def _render(c: dict[str, Any], indent: str) -> str:
        tid = str(c.get("comment_tid", ""))
        nickname = str(c.get("nickname", "未知"))
        content = str(c.get("content", ""))
        time_str = str(c.get("create_time", ""))

        if bot_qq and str(c.get("qq_account", "")) == str(bot_qq):
            display_name = "你"
            content = re.sub(r"^@\S+\s*", "", content)
        else:
            display_name = nickname

        marker = ">>> " if highlight_tid and tid == highlight_tid else ""
        return f"{indent}{marker}[{time_str}] {display_name}：{content}"

    lines: list[str] = []
    for top in top_levels:
        lines.append(_render(top, indent="· "))
        top_tid = str(top.get("comment_tid", ""))
        for child in children_map.get(top_tid, []):
            lines.append(_render(child, indent="    └─ "))

    return "\n".join(lines)
