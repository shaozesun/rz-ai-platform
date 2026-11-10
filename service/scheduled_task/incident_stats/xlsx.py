"""事件统计表 → xlsx（两个 Sheet：周表 + 事件汇总）。

样式对齐平台报告色板（`core/sandbox/report_style.py`）：表头浅蓝底 + 深蓝字，
其余无边框、横向细线。列宽按内容分配，日期单元格用 `yyyy-mm-dd` 数字格式，
首行冻结；长文本换行、行高按内容自适应（封顶 8 行，超出点单元格在编辑栏看全文）。
"""

import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from service.scheduled_task.incident_stats.build import COLUMNS

_TZ = ZoneInfo('Asia/Shanghai')
_HEAD_FILL = PatternFill('solid', fgColor='EAF1FA')
_HEAD_FONT = Font(color='134B70', bold=True, size=10)
_BODY_FONT = Font(size=10)
_THIN = Side(style='thin', color='DBE7F3')
_BORDER = Border(bottom=_THIN)
_WRAP_TOP = Alignment(vertical='top', wrap_text=True)
_CENTER = Alignment(horizontal='center', vertical='center')

# 22 列宽度：时间列固定 22（`YYYY年MM月DD日HH时MM分SS秒` 单行可读），
# 名称/原因/进展宽一些，留空的人工填写列窄；末两列是原文（对照用，够挡住大半屏即可）
_WIDTHS: dict[str, float] = {
  '序号': 6, '日期': 12, '发生时间': 22, '速报时间': 22, '首报时间': 22,
  '地点': 26, '事件等级': 9, '事件类型': 20, '事件名称': 50, '专业': 8,
  '事件原因': 34, '事件进展': 20, '是否完成': 9, '完成时间': 22,
  '处理结果': 20, '处理时长': 10, '过程有无违规': 18, '应急超15min违规': 16,
  '实时跟踪人员': 12, '经手人': 10, '速报内容': 56, '首报内容': 56,
}

# 行高按内容自适应，封顶 8 行（约 112pt）：超长原文不撑爆表格，
# 完整内容仍在单元格里，点单元格可在编辑栏看全文。
_MAX_ROW_LINES = 8
_LINE_HEIGHT = 13.5
_ROW_PADDING = 4


def _text_width(text: str) -> int:
  """显示宽度：CJK/全角按 2 计、ASCII 按 1 计（与前端交互表格同口径）。"""
  return sum(2 if ord(ch) > 255 else 1 for ch in text)


def _wrapped_lines(text: str, width: float) -> int:
  """按列宽折算换行后的行数；显式换行按段累加。"""
  capacity = max(4, int(width) - 1)
  return sum(
    max(1, math.ceil(_text_width(part) / capacity)) for part in str(text).split('\n')
  )


def _row_height(values: list[tuple[str, object]]) -> float:
  """该行所需高度：取各列换行行数的最大值，封顶 `_MAX_ROW_LINES`。"""
  lines = max([1] + [
    _wrapped_lines(value, _WIDTHS.get(name, 14))
    for name, value in values if value not in (None, '')
  ])
  return round(min(lines, _MAX_ROW_LINES) * _LINE_HEIGHT + _ROW_PADDING, 1)


def sheet_title(start_s: int) -> str:
  """统计期起始日的 ISO 周 → `41周`（示例表 Sheet 名同款）。"""
  iso = datetime.fromtimestamp(int(start_s), _TZ).isocalendar()
  return f'{iso[1]}周'


def _write_header(ws, columns: tuple[str, ...]) -> None:
  ws.append(list(columns))
  for i, name in enumerate(columns, 1):
    cell = ws.cell(row=1, column=i)
    cell.fill = _HEAD_FILL
    cell.font = _HEAD_FONT
    cell.alignment = _CENTER
    cell.border = _BORDER
    ws.column_dimensions[get_column_letter(i)].width = _WIDTHS.get(name, 14)
  ws.freeze_panes = 'A2'


def _write_rows(ws, rows: list[dict]) -> None:
  for row in rows:
    values = [(name, row.get(name, '')) for name in COLUMNS]
    ws.append([value for _name, value in values])
    r = ws.max_row
    ws.row_dimensions[r].height = _row_height(values)
    for i, name in enumerate(COLUMNS, 1):
      cell = ws.cell(row=r, column=i)
      cell.font = _BODY_FONT
      cell.border = _BORDER
      cell.alignment = _WRAP_TOP
      if name == '日期' and cell.value:
        cell.number_format = 'yyyy-mm-dd'
        cell.alignment = _CENTER
      elif name in ('序号', '事件等级', '专业', '是否完成', '处理时长'):
        cell.alignment = _CENTER


def _write_summary(ws, summary: list[dict]) -> None:
  columns = ('园区', 'I0', 'I1', 'I2', 'I3', 'I4', '总计')
  ws.append(list(columns))
  for i, name in enumerate(columns, 1):
    cell = ws.cell(row=1, column=i)
    cell.fill = _HEAD_FILL
    cell.font = _HEAD_FONT
    cell.alignment = _CENTER
    cell.border = _BORDER
    ws.column_dimensions[get_column_letter(i)].width = 12 if i == 1 else 8
  ws.freeze_panes = 'A2'
  for row in summary:
    ws.append([row.get(name, 0) for name in columns])
    r = ws.max_row
    for i in range(1, len(columns) + 1):
      cell = ws.cell(row=r, column=i)
      cell.font = _BODY_FONT
      cell.border = _BORDER
      cell.alignment = Alignment(horizontal='left' if i == 1 else 'center')


def write_workbook(path: str | Path, rows: list[dict], summary: list[dict],
                   start_s: int) -> Path:
  """写出 `{N}周` + `事件汇总` 两个 Sheet（0 行也出，表头完整）。"""
  wb = Workbook()
  ws = wb.active
  ws.title = sheet_title(start_s)
  _write_header(ws, COLUMNS)
  _write_rows(ws, rows)
  _write_summary(wb.create_sheet('事件汇总'), summary)
  target = Path(path)
  target.parent.mkdir(parents=True, exist_ok=True)
  wb.save(target)
  return target
