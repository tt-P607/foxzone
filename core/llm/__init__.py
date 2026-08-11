"""FoxZone LLM 能力包。

按职责拆分：

- :mod:`personality`：QZone 场景人格提示词与时间信息
- :mod:`formatters`：提示词文本块拼装
- :mod:`parsers`：LLM 响应解析与容错
- :mod:`vision`：统一批量识图
- :mod:`generators`：ContentService 主类（3 个 generate_* 入口）
"""

from __future__ import annotations

from .generators import ContentService, log_llm_prompt

__all__ = ["ContentService", "log_llm_prompt"]
