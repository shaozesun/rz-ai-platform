"""RZ Agent 状态定义（参照 DeerFlow ThreadState 多 channel 设计）"""

from typing import Annotated
from langgraph.graph.message import add_messages
from langgraph.graph import MessagesState

from core.agent.deferred import PromotedTools, merge_promoted


class RzAgentState(MessagesState):
  """RZ Agent 共享状态。

  参照 DeerFlow ThreadState 的多 channel 模式（messages / summary_text / artifacts），
  在 MessagesState 基础上增加 summary / promoted / task_history 三个 channel。
  """
  messages: Annotated[list, add_messages]
  # 上下文摘要（旧消息压缩结果），参照 DeerFlow ThreadState.summary_text
  summary: str | None
  # deferred 工具 promote 状态（catalog_hash + promoted tool names）。
  # **必须带 reducer**：`tool_search` 每命中一次就写一次 promoted，若用 last-write-wins，
  # 会把编排层预 promote 的工具（如联网调研模式的 web__search/web__fetch）整体顶掉 ——
  # 实测 bug：模型一轮里只要调过一次 tool_search，联网工具就再也看不见了。
  promoted: Annotated[PromotedTools | None, merge_promoted]
  # 被压缩消息的归档索引（batches / omitted_records / status），原文在
  # {AGENT_ANALYSIS_DATA_DIR}/{thread_id}/task_history/history.jsonl，供 history_read 回读
  task_history: dict | None
