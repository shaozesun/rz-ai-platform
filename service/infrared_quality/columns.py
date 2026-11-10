"""结果表的列与行键 —— 唯一来源。

行直接以中文列名为键落库（`rows[]`），records 端点无需再映射，`COLUMNS` 与行键
逐字一致由自检钉住。`序号` 在整期合并时重排。
"""

from datetime import datetime, timedelta, timezone

COLUMNS = [
  '序号', '公司', '部门', '巡检任务', '巡检人', '设备类型', '设备名称',
  '设备编号', '安装位置', '检测时间', '最高温度', '上限值', '判定',
  '模型分数', '说明', '图片链接',
]

_TZ = timezone(timedelta(hours=8))


def fmt_time(ms) -> str:
  """epoch 毫秒 → `YYYY-MM-DD HH:MM:SS`（Asia/Shanghai）；0/空返回空串。"""
  try:
    value = int(ms or 0)
  except (TypeError, ValueError):
    return ''
  if value <= 0:
    return ''
  return datetime.fromtimestamp(value / 1000, tz=_TZ).strftime('%Y-%m-%d %H:%M:%S')


def build_row(*, seq: int, company: str, dept: str, task_id: str, inspector: str,
              type_name: str, name: str, num: str, code: str, site: str,
              detection_time, max_temp: str, upper: str, verdict: str,
              score, note: str, picture: str) -> dict:
  return {
    '序号': seq,
    '公司': company,
    '部门': dept,
    '巡检任务': task_id,
    '巡检人': inspector,
    '设备类型': type_name,
    '设备名称': name or code,
    '设备编号': num,
    '安装位置': site,
    '检测时间': fmt_time(detection_time),
    '最高温度': max_temp,
    '上限值': upper,
    '判定': verdict,
    '模型分数': '' if score is None else str(score),
    '说明': note,
    '图片链接': picture,
  }


def counts_of(rows: list[dict]) -> dict[str, int]:
  out = {'合格': 0, '待复核': 0, '不合格': 0, '检测失败': 0}
  for row in rows:
    key = str(row.get('判定') or '')
    if key in out:
      out[key] += 1
  return out
