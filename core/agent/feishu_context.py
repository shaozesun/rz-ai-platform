"""当前飞书 IM 会话上下文 — contextvars 运行时传递 chat_id / open_id / chat_type。

飞书通道处理消息时调用 set_current_feishu()，工具（读群消息）运行期读取。
Web / 定时任务等非飞书通道不设置，工具据此返回「仅飞书群聊可用」。
与 guardrail.py 的 permissions、user_context.py 的 user_id 同一套 contextvars 机制。
"""

import contextvars

_current_feishu: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
  'feishu_chat', default=None,
)


def set_current_feishu(chat_id: str, open_id: str, chat_type: str) -> None:
  """设置当前飞书会话上下文（在飞书消息处理入口调用）。"""
  _current_feishu.set({'chat_id': chat_id, 'open_id': open_id, 'chat_type': chat_type})


def get_current_feishu() -> dict | None:
  """获取当前飞书会话上下文；非飞书通道返回 None。"""
  return _current_feishu.get()
