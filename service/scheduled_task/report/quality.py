"""第 10 章「详情质检」 —— **启发式提示，不判真伪**（服务端纯函数，不进沙箱）。

为什么放在这里：详情在 `run_compute`（沙箱）**之后**才取（`workflow.py`），沙箱里根本
看不到详情，所以质检只能在服务端进程做（同 `facts.py` 的定位）。

要回答的问题是用户实测提出的：「处置总结基本就是把调整原因复制一份，这样的分析有意义吗」。
本模块按确定性规则数出：多少单的依据**具体**、多少单原因**笼统**、多少单总结**疑似复制**
原因 / 模板化 / 未填。报告统一表述为「按规则识别」，只作填报质量提示。

规则（全部确定性，可单测）：

- **疑似复制**：总结与原因归一化后字符二元组 Jaccard ≥ 0.7，或总结是原因的连续子串
  （且长度 ≥ 6）。复制本身不一定错（例行调整），但批量出现说明总结没提供新信息。
- **原因笼统**：长度 < 10，或整句命中「无/暂无/按需/按要求/日常巡检/例行维护/正常」这类
  无信息量的表述。
- **总结模板化**：长度 < 20、只含「已完成/已处理/已调整/运行正常/恢复正常/按计划完成」，
  且不含数字或单位（台/℃/%/小时/分钟…）。
- **未填总结**：处置总结为空。
- **依据具体**：原因非空、不笼统，且含设备/机房/数值证据（数字、℃、%、机房、设备、台、
  点位、回路）。
"""

import re

# 无信息量的原因表述（整句完全命中才判「笼统」，避免「正常巡检发现3台UPS异常」被误伤）
_VAGUE_RE = re.compile(r'^(无|暂无|按需|按要求|日常巡检|例行维护|正常)$')
# 模板化总结用语
_BOILERPLATE = ('已完成', '已处理', '已调整', '运行正常', '恢复正常', '按计划完成')
# 「具体」证据：数字/温度/百分比/设备类名词
_EVIDENCE_RE = re.compile(r'\d|℃|%|机房|设备|台|点位|回路')
_UNIT_RE = re.compile(r'\d|℃|%|台|小时|分钟')

FLAG_COPY = '疑似复制'
FLAG_VAGUE = '原因笼统'
FLAG_BOILERPLATE = '总结模板化'
FLAG_NO_SUMMARY = '未填总结'


def _norm(value) -> str:
  """去首尾、折叠连续空白（与源数据同口径）。"""
  return ' '.join(str(value).split()) if value is not None else ''


def _bigrams(text: str) -> set:
  return {text[i:i + 2] for i in range(len(text) - 1)}


def _similar(a: str, b: str) -> bool:
  """疑似复制：二元组 Jaccard ≥ 0.7，或较短者是较长的连续子串（长度 ≥ 6）。"""
  if not a or not b:
    return False
  short, long_ = (a, b) if len(a) <= len(b) else (b, a)
  if len(short) >= 6 and short in long_:
    return True
  sa, sb = _bigrams(a), _bigrams(b)
  if not sa or not sb:
    return False
  return len(sa & sb) / len(sa | sb) >= 0.7


def _vague(reason: str) -> bool:
  return len(reason) < 10 or bool(_VAGUE_RE.match(reason))


def _boilerplate(summary: str) -> bool:
  if len(summary) >= 20 or _UNIT_RE.search(summary):
    return False
  return any(summary.strip('。. ') == word for word in _BOILERPLATE) or (
    any(word in summary for word in _BOILERPLATE)
    and not _EVIDENCE_RE.search(summary)
  )


def _specific(reason: str) -> bool:
  return bool(reason) and not _vague(reason) and bool(_EVIDENCE_RE.search(reason))


def assess_details(details: dict | None, spec, fields: tuple = ()) -> dict:
  """对详情行做质检与字段汇总。

  `spec.quality_fields = (原因字段, 总结字段)`；为空（事件/故障）时只做字段汇总，
  `n`/各计数均为 0，渲染层据此不出质检表。

  `fields` 可显式覆盖字段对（供需要临时换字段对的调用方用），
  不改变第 10 章的渲染（那仍按 spec 的 `quality_fields` 走）。
  """
  rows_in = [r for r in ((details or {}).get('rows') or []) if (r.get('fields') or {})]
  reason_field, summary_field = (fields or getattr(spec, 'quality_fields', ()) or ('', ''))
  out_rows: list[dict] = []
  copy_n = vague_n = boiler_n = no_summary_n = specific_n = 0

  if reason_field and summary_field:
    for item in rows_in:
      fields = item.get('fields') or {}
      reason = _norm(fields.get(reason_field))
      summary = _norm(fields.get(summary_field))
      flags: list[str] = []
      if not summary:
        flags.append(FLAG_NO_SUMMARY)
        no_summary_n += 1
      else:
        if _similar(reason, summary):
          flags.append(FLAG_COPY)
          copy_n += 1
        if _boilerplate(summary):
          flags.append(FLAG_BOILERPLATE)
          boiler_n += 1
      if reason and _vague(reason):
        flags.append(FLAG_VAGUE)
        vague_n += 1
      spec_ok = _specific(reason)
      if spec_ok:
        specific_n += 1
      out_rows.append({
        'ticketNo': str(item.get('ticketNo') or '').strip(),
        'flags': flags,
        'specific': spec_ok,
        'reason_len': len(reason),
        'summary_len': len(summary),
      })

  summary_fields = []
  for field in (getattr(spec, 'detail_summary_fields', ()) or ()):
    filled = [_norm((item.get('fields') or {}).get(field)) for item in rows_in]
    filled = [v for v in filled if v]
    counts: dict[str, int] = {}
    for value in filled:
      counts[value] = counts.get(value, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    summary_fields.append({
      'field': field,
      'filled_n': len(filled),
      'top': [{'value': k, 'n': v} for k, v in top],
    })

  n = len(out_rows)
  return {
    'n': n,
    'copy_n': copy_n,
    'vague_reason_n': vague_n,
    'boilerplate_n': boiler_n,
    'no_summary_n': no_summary_n,
    'specific_n': specific_n,
    'rows': out_rows,
    'summary_fields': summary_fields,
  }
