"""评论树工具模块。

QZone 评论区是「一级评论 + list_3 楼中楼」的两层结构，但楼中楼内部
可以互相 @ 回复形成任意深度的 parent_tid 链。本模块提供：

- 沿 parent_tid 链向上溯源到顶层一级评论（reply 接口的 commentId 必须是根 tid）
- 局部序号 tid 判定（msglist_v6 内嵌 list_3 的 tid 是局部序号，不可作 commentId）
- 楼中楼结构的文本渲染（供 LLM 提示词使用）

本模块为纯函数实现，无任何框架依赖，是全插件单元测试的首选目标。
"""

from __future__ import annotations

import re
from typing import Any

#: 溯源与渲染时的最大向上跳数（防御循环引用）。
_MAX_TRAVERSE_DEPTH = 10


def resolve_root_comment(
    all_comments: list[dict[str, Any]], current_tid: str
) -> tuple[str, str]:
    """沿 parent_tid 链向上找到顶层一级评论的 (tid, qq_account)。

    QZone 楼中楼回复 API 同时需要 commentId（顶层一级评论 tid）和
    被回复者信息。两者都必须指向同一根节点，否则 QZone 服务端会以
    -10049 拒绝。

    带循环保护（最多 10 跳）；任一环节查不到时降级返回当前已知 tid。

    Args:
        all_comments: 该说说下的全部评论（含 list_3 二级回复，平铺为字典列表）
        current_tid: 要回复的目标评论 tid

    Returns:
        (root_tid, root_uin) 元组；root_uin 未知时为空字符串
    """
    cur_tid = str(current_tid).strip()
    if not cur_tid:
        return cur_tid, ""
    by_tid: dict[str, dict[str, Any]] = {}
    for c in all_comments:
        ctid = str(c.get("comment_tid") or "").strip()
        if ctid:
            by_tid[ctid] = c
    seen: set[str] = set()
    cursor: dict[str, Any] | None = by_tid.get(cur_tid)
    last_known_tid = cur_tid
    last_known_uin = str((cursor or {}).get("qq_account") or "").strip() if cursor else ""
    for _ in range(_MAX_TRAVERSE_DEPTH):
        if cursor is None or last_known_tid in seen:
            return last_known_tid, last_known_uin
        seen.add(last_known_tid)
        parent = str(cursor.get("parent_tid") or "").strip()
        if not parent:
            return last_known_tid, last_known_uin
        next_node = by_tid.get(parent)
        if next_node is None:
            # 找不到 parent 节点：返回 parent tid 但 uin 未知（保留当前已知 uin）
            return parent, last_known_uin
        last_known_tid = parent
        last_known_uin = str(next_node.get("qq_account") or "").strip()
        cursor = next_node
    return last_known_tid, last_known_uin


def resolve_root_comment_tid(
    all_comments: list[dict[str, Any]], current_tid: str
) -> str:
    """沿 parent_tid 链向上找到顶层一级评论的 tid。

    Args:
        all_comments: 该说说下的全部评论（平铺字典列表）
        current_tid: 要回复的目标评论 tid

    Returns:
        顶层一级评论 tid（字符串）
    """
    root_tid, _ = resolve_root_comment(all_comments, current_tid)
    return root_tid


def is_local_seq_tid(tid: str) -> bool:
    """判断 tid 是否为 QZone list_3 二级评论的局部序号（如 ``"1"`` / ``"9"``）。

    QZone 一级评论 tid 是 24 位 hex 字符串（如 ``"8a38cf8285a4f9699eab0900"``），
    list_3 二级评论 tid 则是局部序号（纯数字、长度通常 < 10）。
    若 ``resolve_root_comment_tid`` 返回的根 tid 仍然是这种短数字形态，
    说明 ``all_comments`` 中缺该二级评论对应的一级父节点，此时把短数字当
    commentId 传给 reply 接口必然触发 -10049。

    Args:
        tid: 待判定的评论 tid 字符串

    Returns:
        True 表示形似局部序号、不可作为 reply 接口的 commentId
    """
    s = str(tid).strip()
    return s.isdigit() and len(s) < 16


def build_threaded_view(
    comments: list[dict[str, Any]],
    bot_qq: str | None,
    highlight_tid: str = "",
) -> str:
    """以楼中楼结构格式化评论区。

    顶层评论（``parent_tid`` 为空）按原始顺序罗列；指向某条顶层评论的
    子回复紧跟其下，缩进展示。Bot 自己的评论显示为「你」，
    与 ``highlight_tid`` 匹配的评论加上 ``>>> `` 前缀。

    Args:
        comments: 扁平评论列表（已含 ``parent_tid`` 字段）
        bot_qq: Bot QQ 号，用于识别 Bot 自己发的评论
        highlight_tid: 当前需要决策的评论 tid

    Returns:
        多行字符串；评论列表为空时返回 "暂无评论"
    """
    if not comments:
        return "暂无评论"

    by_tid: dict[str, dict[str, Any]] = {
        str(c.get("comment_tid", "")): c for c in comments if c.get("comment_tid")
    }

    def _resolve_root(c: dict[str, Any]) -> str:
        """沿 parent_tid 链向上查找顶层评论 tid（子回复彼此 @ 时也会嵌套）。"""
        seen: set[str] = set()
        cur = c
        for _ in range(_MAX_TRAVERSE_DEPTH):
            pid = str(cur.get("parent_tid") or "").strip()
            if not pid or pid in seen:
                return str(cur.get("comment_tid", ""))
            seen.add(pid)
            parent = by_tid.get(pid)
            if parent is None:
                return str(cur.get("comment_tid", ""))
            cur = parent
        return str(cur.get("comment_tid", ""))

    top_levels: list[dict[str, Any]] = []
    children_map: dict[str, list[dict[str, Any]]] = {}
    for c in comments:
        parent = str(c.get("parent_tid") or "").strip()
        if not parent:
            top_levels.append(c)
        else:
            root_tid = _resolve_root(c)
            if root_tid and root_tid != str(c.get("comment_tid", "")):
                children_map.setdefault(root_tid, []).append(c)
            else:
                # 父评论缺失，降级当顶层
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
