"""工具结果的结构化摘要（零 LLM，确定性）

为什么需要它：工具结果超预算时，原来的做法是**按字符硬切**（`text[:max_chars]`）——
会把一条记录从中间切断，模型拿到半截 JSON，既读不出结构也不知道自己缺什么。改成
「**归档全文 + 返回结构化摘要**」：摘要告诉模型这份数据是什么形状（记录数、字段、
样本），完整原文用 `history_read(source_id)` 回读。

思路照搬 DeerFlow `tool_output_synopsis.py`（超预算时生成 typed synopsis + 引用，
而不是截字符串），但只保留 rz 需要的部分：rz 的平台工具结果就是 JSON（object /
array / array&lt;object&gt;），不做 YAML/XML/code 那套多格式识别，也不引第三方解析器。

纯函数、无副作用、无 LLM 调用。
"""

from __future__ import annotations

import json
from typing import Any

_SAMPLE_ITEMS = 3
_CELL_CHARS = 120
_MAX_CHARS = 1400


def _cell(value: Any) -> Any:
  """单元格预览：长字符串截断，其余原样（保持数字类型可读）。"""
  if isinstance(value, str):
    return value[:_CELL_CHARS] + ('…' if len(value) > _CELL_CHARS else '')
  if isinstance(value, (list, tuple)):
    return f'<{type(value).__name__} len={len(value)}>'
  if isinstance(value, dict):
    return {'_keys': list(value.keys())[:8]}
  return value


def _brief_dict(obj: dict, keys: list[str] | None = None) -> dict:
  keys = keys if keys is not None else list(obj.keys())
  return {k: _cell(obj.get(k)) for k in keys[:12]}


def build_synopsis(obj: Any) -> str:
  """把工具结果归纳成一段紧凑的结构描述（JSON 文本）。

  形状：
  - object        → 字段清单 + 前若干字段的值预览
  - array<object> → 记录数 + 字段并集 + 前几条样本
  - array         → 长度 + 前几个元素预览
  - 其它          → 类型 + 截断后的值
  """
  try:
    if isinstance(obj, dict):
      desc = {'类型': 'object', '字段': list(obj.keys())[:40],
              '样本': _brief_dict(obj)}
    elif isinstance(obj, list):
      if obj and all(isinstance(x, dict) for x in obj):
        fields: list[str] = []
        for row in obj[:50]:
          for k in row:
            if k not in fields:
              fields.append(k)
        desc = {
          '类型': 'array<object>',
          '记录数': len(obj),
          '字段': fields[:40],
          '样本': [_brief_dict(r, fields) for r in obj[:_SAMPLE_ITEMS]],
        }
      else:
        desc = {'类型': 'array', '长度': len(obj),
                '样本': [_cell(x) for x in obj[:_SAMPLE_ITEMS]]}
    else:
      desc = {'类型': type(obj).__name__, '值': _cell(obj)}

    text = json.dumps(desc, ensure_ascii=False, separators=(',', ':'))
  except (TypeError, ValueError):
    return '（结果结构无法归纳）'

  if len(text) > _MAX_CHARS:
    text = text[:_MAX_CHARS] + '…'
  return text
