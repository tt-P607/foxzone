"""QZone 场景人格提示词与时间信息。

从核心配置（core.toml personality 节）拼装 QZone 场景化的系统提示词，
含防 OOC / 防模板化表达准则。
"""

from __future__ import annotations

import datetime

from src.app.plugin_system.api.config_api import get_core_config


def get_personality_desc() -> str:
    """构建结构化的 QZone 场景系统提示词。

    采用 XML 标签结构分四段：``<introduce>``（QZone 场景定位）、
    ``<personality>``（人设字段）、``<expression_principles>``（防 OOC/防模板化原则）、
    ``<background_story>``（可选）。

    Returns:
        拼装完成的多段提示词文本。

    Raises:
        读取核心人格配置失败时直接抛出，由调用方处理。
    """
    personality = get_core_config().personality

    # 1. introduce：QZone 场景头
    introduce_block = (
        "<introduce>\n"
        "此刻你正活跃在 QQ 空间——一个朋友间分享日常、互相点赞评论的半公开社交场所。\n"
        "QQ 空间的互动是**异步、慢节奏**的：你看到的每条说说都是朋友某个瞬间的情绪切片，\n"
        "不像聊天那样需要即时来回。互动的价值在于「看见」与「被看见」，而不是「必须说点什么」。\n"
        "请像真实的朋友一样自然参与——可以共鸣、可以打趣、也可以只是默默点赞。\n"
        "</introduce>"
    )

    # 2. personality：从 core.toml 拼装
    personality_lines: list[str] = ["<personality>"]
    if personality.nickname:
        personality_lines.append(f"你的名字是 **{personality.nickname}**。")
    if personality.personality_core:
        personality_lines.append(f"你的核心人格：{personality.personality_core}")
    if personality.personality_side:
        personality_lines.append(f"你的人格侧面：{personality.personality_side}")
    if personality.identity:
        personality_lines.append(f"你的身份：{personality.identity}")
    if personality.reply_style:
        personality_lines.append(f"你的表达方式：{personality.reply_style}")
    personality_lines.append("</personality>")
    personality_block = "\n".join(personality_lines)

    # 3. expression_principles：QZone 场景下的核心表达准则（防 OOC + 防模板化）
    principles_block = (
        "<expression_principles>\n"
        "# 真实感优先\n"
        "像真实朋友间的随手互动，**不要书面化、不要堆砌人设标签词**。\n"
        "日常才是基调，偶尔的个性化点缀才是惊喜——绝对不要每条评论都强行体现人设。\n"
        "情绪有惯性：评论的基调由说说本身的氛围决定，而不是从中性状态硬启动一个固定模板。\n"
        "\n"
        "# 防模板化\n"
        "避免连续多条评论使用相似的开场词、感叹词或句尾点缀。\n"
        "你的人设标签词（人设中反复出现的核心意象）是底色，**不应该成为口癖**。\n"
        "回复偶尔「不那么像你」反而更真实——真实的人不会时时刻刻都在表演自己。\n"
        "\n"
        "# 场景边界\n"
        "QQ 空间是社交分享场合，**不是聊天对话**。\n"
        "- 不要在评论里 @ 对方，不要把评论写得像「邀请对方继续对话」；\n"
        "- 评论是顺手的一句感想，不是问候、不是追问、不是发起话题；\n"
        "- 若说说本身没什么共鸣，宁可只点赞——评论的价值在「有话说才说」。\n"
        "\n"
        "# 情绪与边界\n"
        "对悲伤、严肃、负面情绪的说说，立刻收起玩笑，用真诚而克制的语气；\n"
        "对炫耀、晒图、日常吐槽，自然延续氛围即可，不需要强行升华或夸张。\n"
        "</expression_principles>"
    )

    blocks: list[str] = [introduce_block, personality_block, principles_block]

    # 4. 可选 background_story
    if personality.background_story and len(personality.background_story) >= 10:
        blocks.append(
            "<background_story>\n"
            "（作为行动依据，不要在评论或回复中直接复述背景故事）\n"
            f"{personality.background_story}\n"
            "</background_story>"
        )

    return "\n\n".join(blocks)


def get_now_info() -> tuple[str, str]:
    """获取当前时间和星期信息。

    Returns:
        (格式化时间字符串, 中文星期名) 元组
    """
    now = datetime.datetime.now()
    current_time = now.strftime("%Y年%m月%d日 %H:%M")
    weekday_names = [
        "星期一",
        "星期二",
        "星期三",
        "星期四",
        "星期五",
        "星期六",
        "星期日",
    ]
    return current_time, weekday_names[now.weekday()]


def format_story_time(story_time: str | None) -> str:
    """将数据库时间转为更适合提示词的中文时间。

    Args:
        story_time: 形如 ``2026-01-01 12:00:00`` 的时间字符串；可为 None

    Returns:
        形如 ``01月01日 12:00`` 的短格式；解析失败时原样返回。
    """
    if not story_time:
        return ""
    try:
        dt = datetime.datetime.strptime(story_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return story_time
    return dt.strftime("%m月%d日 %H:%M")
