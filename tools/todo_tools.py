"""write_todos 工具 —— 计划模式下的任务清单（对齐 DeerFlow 的 write_todos）。

语义与 DeerFlow 一致：模型**执行过程中**边干边维护这份清单，每次提交**完整列表**
（last-write-wins，不是增量 patch），前端在输入框上方的托盘里实时显示进度。
它**不是**「先出计划等用户确认」——我们原来的 plan 确认门已按此重做删掉。

只有内容 + 状态两个字段（同 LangChain `Todo`）：
    [{'content': '查变更单', 'status': 'pending'|'in_progress'|'completed'}, ...]

工具正文只有「怎么用」的纪律；触发时机与「清单别念给用户听」在
`core/agent/prompts.py` 的 PLAN_MODE_INSTRUCTION（计划模式每轮注入）。
"""

import json
import logging

from langchain.tools import tool

logger = logging.getLogger(__name__)

# 清单条数上限：纯展示用，防模型把整篇分析拆成几十条把托盘撑爆（DeerFlow 无上限，
# 但其工具描述同样要求「复杂任务才用」；这里加硬上限兜底）。
MAX_TODOS = 20

_STATUSES = ('pending', 'in_progress', 'completed')


async def _emit_todos(todos: list[dict]) -> None:
  """把清单广播给前端（纯反馈，失败绝不能影响工具执行）。"""
  try:
    from langchain_core.callbacks import adispatch_custom_event
    await adispatch_custom_event('todos', {'todos': todos})
  except Exception:
    logger.debug('write_todos 事件广播失败（忽略）', exc_info=True)


def normalize_todos(raw) -> tuple[list[dict], str | None]:
  """规范化入参清单，返回 (清单, 错误说明或 None)。

  - 非 list / 空列表：视为清空清单（空列表是合法输入，DeerFlow 语义里也让空列表清空）；
  - 每项取 content（非空才留）+ status（非法值归 pending）；
  - 超过 MAX_TODOS 截断并给出说明（截断说明不回给模型，只记日志）。
  """
  if raw is None:
    return [], 'todos 不能为空，请按 [{"content": "...", "status": "pending"}] 传完整清单'
  if isinstance(raw, str):
    try:
      raw = json.loads(raw)
    except ValueError:
      return [], 'todos 需要是数组，如 [{"content": "查变更单", "status": "in_progress"}]'
  if not isinstance(raw, list):
    return [], 'todos 需要是数组，如 [{"content": "查变更单", "status": "in_progress"}]'

  out: list[dict] = []
  for item in raw:
    if isinstance(item, str):
      item = {'content': item, 'status': 'pending'}
    if not isinstance(item, dict):
      continue
    content = str(item.get('content') or '').strip()
    if not content:
      continue
    status = str(item.get('status') or '').strip().lower()
    if status not in _STATUSES:
      status = 'pending'
    out.append({'content': content, 'status': status})

  if len(out) > MAX_TODOS:
    logger.info('write_todos 清单超上限，截断 %d → %d 条', len(out), MAX_TODOS)
    out = out[:MAX_TODOS]
  return out, None


def _summary(todos: list[dict]) -> str:
  done = sum(1 for t in todos if t['status'] == 'completed')
  doing = sum(1 for t in todos if t['status'] == 'in_progress')
  if not todos:
    return '任务清单已清空。'
  return f'任务清单已更新：共 {len(todos)} 项（{done} 项已完成，{doing} 项进行中）。'


@tool
async def write_todos(todos: list) -> str:
  """创建/更新任务清单，让用户实时看到执行进度（仅在计划模式下使用）。

  每次调用请提交**完整清单**（不是增量），状态变化就重写整份。

  Args:
    todos: 任务项数组，每项 {"content": "要做什么（简短）", "status": "pending|in_progress|completed"}。
      pending=未开始 / in_progress=进行中 / completed=已完成。始终只保留一项 in_progress。
  """
  normalized, error = normalize_todos(todos)
  if error:
    return error
  await _emit_todos(normalized)
  return _summary(normalized)
