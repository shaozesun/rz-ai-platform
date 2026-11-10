"""不合规检查 → xlsx（总览 + 每个检查项一个 sheet）。

样式对齐平台报告色板（`core/sandbox/report_style.py`）：表头浅蓝底 + 深蓝字，
横向细线、首行冻结、列宽按内容。**sheet 清单与列全部由 `checks.CHECKS` 生成** ——
以后加检查项不用改这里。0 行也出完整表头（「本班次没查到」也要能看出查过什么）。
"""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from service.scheduled_task.compliance import checks

OVERVIEW_SHEET = '总览'
OVERVIEW_COLUMNS = ('检查项', '周期', '命中条数', '口径说明')
TOTAL_COLUMN = '总计'

_HEAD_FILL = PatternFill('solid', fgColor='EAF1FA')
_HEAD_FONT = Font(color='134B70', bold=True, size=10)
_TITLE_FONT = Font(color='134B70', bold=True, size=12)
_BODY_FONT = Font(size=10)
_THIN = Side(style='thin', color='DBE7F3')
_BORDER = Border(bottom=_THIN)
_WRAP_TOP = Alignment(vertical='top', wrap_text=True)
_CENTER = Alignment(horizontal='center', vertical='center')

_WIDTHS: dict[str, float] = {
  '序号': 6, '园区': 8, '空间': 30, '设备': 24, '测点': 20, '级别': 8, '等级': 8,
  '告警内容': 28, '发生时间': 20, '告警时间': 20, '响应时间': 20, '开始处理时间': 20,
  '关单时间': 20, '已持续(分钟)': 13, '告警值': 12, '工单号': 20,
  '响应时长(分钟)': 14, '未关单时长(分钟)': 16, '响应人': 10, '处理人': 10,
  '当前告警状态': 12, '处理时长(分钟)': 14, '处理结果': 14, '告警已持续(分钟)': 16,
  '日期': 12, '次数': 8, '首次时间': 20, '末次时间': 20, '跨度(分钟)': 11,
  # 重复告警（对齐值守台账）
  '大楼名称': 12, '地址名称': 40, '设备名称': 24, '监控项名称': 20,
  '处理过程': 40, '问题是否解决': 12, '解决办法': 20, '填报人': 10,
  '检查项': 16, '周期': 8, '命中条数': 10, '口径说明': 62, TOTAL_COLUMN: 10,
}

_CENTER_COLUMNS = {
  '序号', '日期', '园区', '级别', '等级', '次数', '周期', '命中条数', '告警值',
  TOTAL_COLUMN, '大楼名称', '告警时间', '首次时间', '问题是否解决', '填报人',
  '已持续(分钟)', '响应时长(分钟)', '未关单时长(分钟)', '处理时长(分钟)',
  '告警已持续(分钟)', '跨度(分钟)', '当前告警状态', '处理结果',
  '开始处理时间', '响应时间',
}


def _width_for(name: str) -> float:
  width = _WIDTHS.get(name)
  if width is not None:
    return width
  # 未登记的列（如矩阵里的检查项名）：按字宽估一个，中文按 2 字宽算
  return min(30, max(12, len(name) * 2 + 4))


def _apply_width(ws, index: int, name: str) -> None:
  """取「已有宽度」与「本列宽度」的较大者（总览两张表共用 A 列）。"""
  letter = get_column_letter(index)
  current = ws.column_dimensions[letter].width
  width = _width_for(name)
  if current is None or current < width:
    ws.column_dimensions[letter].width = width


def _write_title(ws, text: str, row: int = 1) -> int:
  """写标题行，返回表头行号（无标题时原样返回）。"""
  if not text:
    return row
  ws.cell(row=row, column=1, value=text).font = _TITLE_FONT
  return row + 1


def _write_header(ws, columns: tuple[str, ...], row: int) -> None:
  for i, name in enumerate(columns, 1):
    cell = ws.cell(row=row, column=i, value=name)
    cell.fill = _HEAD_FILL
    cell.font = _HEAD_FONT
    cell.alignment = _CENTER
    cell.border = _BORDER
    _apply_width(ws, i, name)


def _write_rows(ws, columns: tuple[str, ...], rows: list[dict], start_row: int) -> int:
  """写数据行，返回下一可用行号。"""
  row = start_row
  for data in rows:
    for i, name in enumerate(columns, 1):
      value = data.get(name, '')
      cell = ws.cell(row=row, column=i, value='' if value is None else value)
      cell.font = _BODY_FONT
      cell.border = _BORDER
      cell.alignment = _CENTER if name in _CENTER_COLUMNS else _WRAP_TOP
    row += 1
  return row


def _write_overview(ws, result: dict, title: str = '') -> None:
  row = 1
  if title:
    ws.cell(row=row, column=1, value=title).font = _TITLE_FONT
    row += 2
  first_header_row = row
  _write_header(ws, OVERVIEW_COLUMNS, row)
  row += 1
  for item in result.get('summary') or []:
    row = _write_rows(ws, OVERVIEW_COLUMNS, [{
      '检查项': item.get('name', ''),
      '周期': item.get('cadence', ''),
      '命中条数': item.get('rows', 0),
      '口径说明': item.get('description', ''),
    }], row)
  row += 1
  matrix = result.get('matrix') or []
  matrix_columns = ('园区', *[check['name'] for check in checks.CHECKS], TOTAL_COLUMN)
  _write_header(ws, matrix_columns, row)
  _write_rows(ws, matrix_columns, matrix, row + 1)
  ws.freeze_panes = f'A{first_header_row}'


def write_workbook(path: str | Path, result: dict, title: str = '',
                   period: str = '') -> Path:
  """写出「总览」+ 每个检查项一个 sheet，返回落盘路径。

  `result` 是 `checks.run_all()` 的返回：`{'rows', 'summary', 'matrix'}`。
  每张检查项 sheet 的**第 1 行是标题**（`{check['title']} · {period}`）、第 2 行表头、
  第 3 行起数据 —— 与值守台账的排版一致；接口按「非空单元格 ≥ 2」识别表头行，见
  `api/v1/scheduled_tasks.py: _sheet_rows`。
  """
  wb = Workbook()
  overview = wb.active
  overview.title = OVERVIEW_SHEET
  _write_overview(overview, result, title)

  rows_by_key = result.get('rows') or {}
  for check in checks.CHECKS:
    ws = wb.create_sheet(check['sheet'])
    columns = checks.COLUMNS[check['key']]
    name = check.get('title') or check['name']
    header_row = _write_title(ws, f'{name} · {period}' if period else name, 1)
    _write_header(ws, columns, header_row)
    _write_rows(ws, columns, rows_by_key.get(check['key']) or [], header_row + 1)
    ws.freeze_panes = f'A{header_row + 1}'

  target = Path(path)
  target.parent.mkdir(parents=True, exist_ok=True)
  wb.save(target)
  return target
