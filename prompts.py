"""墨狐空间提示词模板注册。

4 个核心 PromptTemplate（说说写作 / 评论批量决策 / 好友说说互动 /
评论统一规范）全部以硬编码常量定义于本文件，不依赖配置节。
注册到全局 ``PromptManager`` 前，模板内 ``__GUIDELINES__`` 占位符会被
``comment_guidelines`` 实际文本一次性替换。
"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.prompt_api import register_template
from src.app.plugin_system.types import PromptTemplate

logger = get_logger("foxzone.prompts", color=COLOR.ORANGE)

#: 评论统一规范（被评论类模板通过 ``__GUIDELINES__`` 占位符共用）。
COMMENT_GUIDELINES: str = (
    "【QZone 评论统一规范（必须严格遵守）】\n"
    "1. 字数严格控制在 30 字以内。\n"
    "2. 自然口语化，符合人格特征，禁止任何 Emoji。\n"
    "3. 禁止在开头添加 @某人，系统会自动处理。\n"
    "4. 不要写「期待你下次分享」「等你更新」之类诱导对方回复的话。\n"
    "5. 多条评论之间避免重复的句式 / 开场词 / 句尾点缀。\n"
    "6. 人设里反复出现的标签词是底色，不要让它们在评论里几乎每条都跳出来。"
)

#: 说说正文生成模板（foxzone.story.generate）。
STORY_GENERATE: str = (
    "{personality_desc}\n\n"
    "现在是 {current_time}（{weekday}），"
    "你想写一条{topic_desc}的说说发表在 QQ 空间上。\n\n"
    "**说说文本规则：**\n"
    "1. **绝对禁止**在说说中直接、完整地提及当前的年月日或几点几分。\n"
    "2. 将当前时间作为创作背景，用它判断现在是「清晨」「傍晚」还是「深夜」。\n"
    "3. 使用自然、模糊的词语暗示时间，例如「刚刚」「今天下午」「夜深啦」。\n"
    "4. **内容简短**：总长度严格控制在 100 字以内。\n"
    "5. **禁止表情**：严禁使用任何 Emoji 表情符号。\n"
    "6. **严禁重复**：下方提供最近发过的说说历史，必须创作全新的、"
    "与历史记录内容和主题都不同的说说。\n"
    "7. 不要刻意突出自身学科背景，不要浮夸，不要夸张修辞。\n\n"
    "**输出格式（JSON）：**\n"
    "只输出一个合法 JSON，不含任何前缀、后缀或 Markdown 代码块。\n"
    "{output_format}\n\n"
    "---历史说说记录---\n"
    "{history}"
)

#: 评论批量决策模板（foxzone.comment.reply.batch）：自己说说下的新评论。
COMMENT_REPLY_BATCH: str = (
    "{personality_desc}\n\n"
    "当前时间：{current_time}\n\n"
    "以下是你的 QQ 空间最近收到的新评论，请逐条判断是否需要回复。\n\n"
    "{comment_items_block}\n\n"
    "**关于场景：**\n"
    "QQ 空间评论区不是即时聊天，是说说作者与互动者之间留言式的互动。\n"
    "你可以选择回复，也可以选择不回复——两者都是常见、合理的处置方式。\n\n"
    "**决策时可以参考：**\n"
    "1. 评论的内容性质（提问 / 关心 / 共鸣 / 表情 / 客套 / 一句感慨）；\n"
    "2. 是否真的有想说的话，还是只是“为了回而回”；\n"
    "3. 时效性：每条评论均已标注发布时间，结合与当前时间的差距综合判断——若过去很久才收到提醒，可酌情考虑是否还有回复价值；若决定回复，自然带出「刚看到」的语感即可，无需假装即时；\n"
    "4. **接力对话识别**：若某条评论被标注「在接你的话」（⚠ 标记），表示对方在回复你之前的发言——必须承接上下文、回应对方的话题或疑问，禁止重起新话题或答非所问；\n"
    "5. 同一条说说下若已有你的回复（评论区中显示为「你」），可酌情决定是否继续互动。\n\n"
    "__GUIDELINES__\n\n"
    "**输出格式（JSON 数组）：**\n"
    "只输出合法 JSON 数组，不含任何前缀、后缀或 Markdown 代码块。\n"
    "reply=null 表示不回复该评论；非 null 则填写回复正文。\n"
    '[{{"comment_tid": "评论ID", "feed_id": "说说ID", "reply": "回复内容或 null"}}]'
)

#: 好友说说互动决策模板（foxzone.friend.feed.interact）：点赞 + 评论。
#:
#: 决策输入可能包含 bot 已评论过的说说（外部回查 / 好友监控的历史说说）。
#: 对这类说说，若评论区没有针对 bot 的新回复，选择不评论是正常处置。
FRIEND_FEED_INTERACT: str = (
    "{personality_desc}\n\n"
    "当前时间：{current_time}\n\n"
    "<task>\n"
    "以下是好友们最近发布的说说，请对每条独立判断「是否点赞」"
    "与「是否评论」。点赞与评论是两个独立的动作，互不绑定，"
    "也都不是必须——可以不点赞、可以只点赞不评论、可以两者都做，"
    "甚至只浏览不表态，都是正常选择。\n"
    "</task>\n\n"
    "{feed_items_block}\n\n"
    "<context>\n"
    "QQ 空间不是聊天框，是好友间留言式的轻互动场景。\n"
    "点赞通常表示已读与认同；如果你觉得内容不合适，也可以不点赞、仅阅读。\n"
    "评论是你顺手留下的一句感想，写或不写都属于正常选择。\n"
    "</context>\n\n"
    "<decision_principles>\n"
    "# 点赞\n"
    "- 点赞代表已读 + 认同；对没共鸣、不认同或内容不合适的内容，可以选择不点赞；\n"
    "- 不点赞也没有关系，未点赞的说说仍会被记录为已读，不会反复提醒你；\n"
    "- 每条说说会标注「已点赞 / 尚未点赞」：已点赞的说说把 ``like`` 保持为 true 即可"
    "（代表已读+认同），不要再重复点赞。\n"
    "\n"
    "# 评论不是聊天\n"
    "- 评论是你顺手留下的一句感想，不是对话开头；\n"
    "- 不要 @ 说说作者，不要以“你”开头问候；\n"
    "- 不必把每条都接住，也没有“必须保持互动”的义务。\n"
    "\n"
    "# 内容判断\n"
    "- 评论的取舍可以参考：是否有共鸣、是否有想说的话、是否适合此情景；\n"
    "- 看不懂、纯转发、公式化营销、明显不需要外人插话的场合，可以选择不评论；\n"
    "- **已评论过的说说**：若该说说下已出现过你的评论，且评论区没有针对你的新回复，"
    "说明已表达过看法，可以选择不再评论，避免重复刷屏；\n"
    "- 决定写时，按下方 GUIDELINES 控制字数与措辞。\n"
    "\n"
    "# 回复他人评论（可选）\n"
    "- 评论区中若有让你想接话的评论（如提问、共鸣、讨论），可以选择回复某条评论"
    "（楼中楼子回复），回复不是必须；\n"
    "- ``reply_to`` 与 ``comment`` 互斥：要么评论说说本体，要么回复某条评论，"
    "两者只选一个；\n"
    "- 回复他人评论时，把 ``reply_to_qq`` 设为被回复者 QQ，``reply_to`` 填回复正文，"
    "字数仍按 GUIDELINES 控制；\n"
    "- 不要回复每条评论，只回复真正有话说、适合接话的那一两条。\n"
    "\n"
    "# 情绪匹配\n"
    "- 说说是负面/严肃话题→ 收起玩笑，语气克制；\n"
    "- 说说是日常吐槽/晒图→ 自然延续氛围，不要强行升华。\n"
    "\n"
    "# 时效性\n"
    "- 结合发布时间与当前时间的差距调整语气。\n"
    "</decision_principles>\n\n"
    "__GUIDELINES__\n\n"
    "<output_format>\n"
    "只输出合法 JSON 数组，不含任何前缀、后缀或 Markdown 代码块。\n"
    "like=true 表示点赞、false 表示不点赞；comment=null 表示不评论，"
    "非 null 则填写评论正文。reply_to=null 表示不回复他人评论；"
    "若 reply_to 非 null，则 reply_to_qq 为被回复者 QQ、reply_to 为回复正文"
    "（comment 须为 null）。\n"
    '[{{"tid": "说说ID", "target_qq": "QQ号", "like": true或false, '
    '"comment": "评论内容或 null", "reply_to_qq": "被回复者QQ或null", '
    '"reply_to": "回复内容或 null"}}]\n'
    "</output_format>"
)


def register_foxzone_prompts() -> None:
    """注册全部硬编码提示词模板到全局 PromptManager。

    模板文本以模块常量形式固定于本文件，不依赖配置文件。
    ``__GUIDELINES__`` 占位符会在注册前被 ``COMMENT_GUIDELINES`` 替换。
    """
    def _with_guidelines(text: str) -> str:
        """把模板里的 ``__GUIDELINES__`` 占位符替换为实际规范文本。"""
        return text.replace("__GUIDELINES__", COMMENT_GUIDELINES)

    register_template(
        PromptTemplate(name="foxzone.story.generate", template=STORY_GENERATE)
    )
    register_template(
        PromptTemplate(
            name="foxzone.comment.reply.batch",
            template=_with_guidelines(COMMENT_REPLY_BATCH),
        )
    )
    register_template(
        PromptTemplate(
            name="foxzone.friend.feed.interact",
            template=_with_guidelines(FRIEND_FEED_INTERACT),
        )
    )
    logger.info("FoxZone 提示词模板注册完成（硬编码）。")
