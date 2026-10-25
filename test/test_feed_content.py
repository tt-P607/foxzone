"""好友动态正文和图片解析测试。"""

from __future__ import annotations

from plugins.foxzone.core.http.feed_content import parse_feed_content


def _parse(body: str, **counts: object) -> dict[str, object]:
    """包装单条动态 HTML 供解析测试使用。"""
    html = f'<div class="f-item" data-key="post">{body}</div>'
    return parse_feed_content(html, "post", "10002", 311, **counts)


def test_script_style_and_sibling_foot_statistics_are_scoped() -> None:
    """正文不含脚本样式文本，点赞统计可来自同动态的页脚。"""
    parsed = parse_feed_content(
        '<li class="f-single"><div class="f-item" data-key="post">'
        '<div class="f-info">Body<script>hidden()</script><style>hidden</style></div>'
        '</div><div class="f-single-foot">'
        '<div class="mod-comments"><a class="qz_like_btn_v3" data-likecnt="99"></a></div>'
        '<a class="qz_like_btn_v3" data-likecnt="0"></a></div></li>',
        "post", "100", 311,
    )
    assert parsed["content"] == "Body"
    assert parsed["like_count"] == 0


def test_prefers_full_body_and_removes_expand_controls() -> None:
    """折叠摘要和完整正文同时存在时返回完整正文。"""
    result = _parse(
        '<div class="f-info qz_info_cut">摘要<a data-cmd="qz_toggle">展开全文</a></div>'
        '<div class="f-info qz_info_complete">完整正文<img alt="[笑]" src="face.gif"></div>'
    )
    assert result["content"] == "完整正文[笑]"
    assert result["content_complete"] is True


def test_empty_complete_body_does_not_replace_excerpt_and_cut_class_is_incomplete() -> None:
    """空完整节点不覆盖摘要，折叠类本身足以标识正文不完整。"""
    empty_complete = _parse(
        '<div class="f-info qz_info_cut">摘要</div>'
        '<div class="f-info qz_info_complete"></div>'
    )
    cut_without_control = _parse('<div class="f-info qz_info_cut">摘要</div>')
    assert empty_complete["content"] == "摘要"
    assert empty_complete["content_complete"] is False
    assert cut_without_control["content_complete"] is False


def test_reports_folded_and_unknown_content_distinctly() -> None:
    """只有摘要时标记不完整，无法识别模板时保留未知值。"""
    folded = _parse('<div class="f-info">正文<a data-cmd="qz_toggle">展开</a></div>')
    unknown = parse_feed_content("<div>unknown</div>", "post", "10002", 311)
    assert folded["content"] == "正文"
    assert folded["content_complete"] is False
    assert unknown["content"] is None
    assert unknown["content_complete"] is None
    assert unknown["images"] is None


def test_counts_preserve_zero_and_do_not_use_comment_markup() -> None:
    """服务端零值保留，未提供的统计不从评论 HTML 推算。"""
    result = _parse(
        '<div class="f-info"></div><div class="mod-comments">visible comment</div>',
        like_count="0",
        comment_count="unknown",
    )
    assert result["content"] == ""
    assert result["content_complete"] is True
    assert result["images"] == []
    assert result["like_count"] == 0
    assert result["comment_count"] is None


def test_excludes_nested_content_and_deduplicates_only_valid_images() -> None:
    """评论、转发、视频封面和无效 URL 不进入说说字段。"""
    result = _parse(
        '<i name="feed_data" data-uin="10002" data-fkey="post" data-tid="verified"></i>'
        '<div class="f-info">主文<br>第二行<img alt="[赞]" src="face.gif"></div>'
        '<a class="img-item" data-originurl="https://example.com/a.jpg">'
        '<img src="https://example.com/thumb.jpg"></a>'
        '<a class="img-item" data-originurl="https://example.com/a.jpg"></a>'
        '<a class="img-item"><img truesrc="//example.com/b.jpg"></a>'
        '<a class="img-item"><img src="javascript:bad()"></a>'
        '<a class="img-item" data-v_itemid="video"><img src="https://example.com/v.jpg"></a>'
        '<div class="f-item-passive"><div class="f-info">转发</div>'
        '<a class="img-item"><img src="https://example.com/r.jpg"></a></div>'
        '<div class="mod-comments"><div class="f-info">评论</div>'
        '<a class="img-item"><img src="https://example.com/c.jpg"></a></div>'
    )
    assert result["tid"] == "verified"
    assert result["content"] == "主文\n第二行[赞]"
    assert result["images"] == ["https://example.com/a.jpg", "https://example.com/b.jpg"]


def test_html_origin_and_url_variants_precede_thumbnails_and_ignore_controls() -> None:
    """优先选择 HTML 原图/大图，且不把头像、评论和操作控件并入正文。"""
    result = _parse(
        '<div class="f-info">正文<span class="comments-op">回复</span>'
        '<span class="ui-avatar"><img alt="头像" src="https://example.com/avatar.jpg"></span>'
        '<img alt="[笑]" src="face.gif"></div>'
        '<a class="img-item" data-url3="javascript:bad()" '
        'data-url2="https://example.com/large.jpg">'
        '<img truesrc="https://example.com/thumbnail.jpg"></a>'
        '<a class="img-item" data-originurl="//example.com/original.jpg">'
        '<img src="https://example.com/thumbnail.jpg"></a>'
        '<a class="img-item"><img data-originurl="https://example.com/inline-origin.jpg" '
        'src="https://example.com/inline-thumbnail.jpg"></a>'
        '<div class="comments-list"><a class="img-item">'
        '<img src="https://example.com/comment.jpg"></a></div>'
    )
    assert result["content"] == "正文[笑]"
    assert result["images"] == [
        "https://example.com/large.jpg",
        "https://example.com/original.jpg",
        "https://example.com/inline-origin.jpg",
    ]


def test_tid_requires_matching_post_metadata_and_counts_reject_unsafe_integers() -> None:
    """说说 ID 必须来自当前动态元数据，超出安全范围的统计未知。"""
    result = parse_feed_content(
        '<div class="f-item" data-key="post">'
        '<i name="feed_data" data-uin="10003" data-fkey="post" data-tid="foreign"></i>'
        '<div class="f-info">正文</div></div>',
        "post",
        "10002",
        311,
        like_count="9007199254740992",
    )
    assert result["tid"] is None
    assert result["like_count"] is None