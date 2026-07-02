"""
检测报告生成服务：支持 Markdown 和 Word (docx) 两种格式
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from urllib.parse import quote

from models.risk.schemas import CheckResult
from models.risk.schemas import FireSafetyResult

logger = logging.getLogger(__name__)


def _severity_icon(severity: str) -> str:
  icons = {'高': '🔴', '中': '🟡', '低': '🟢'}
  return icons.get(severity, '⚪')


# ==================== Markdown 报告 ====================

def generate_markdown_report(
  results: list[CheckResult],
  title: str = '',
) -> str:
  """根据检测结果生成 Markdown 格式报告。"""
  now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
  report_title = title or '安全隐患检测报告'

  lines = [
    f'# {report_title}',
    '',
    f'**生成时间**: {now}',
    f'**检测图片数**: {len(results)}',
    '',
    '---',
    '',
    '## 检测概要',
    '',
  ]

  total_risks = sum(1 for r in results if r.has_risk)
  high = sum(1 for r in results if r.risk_level in ('high', 'critical'))
  mid = sum(1 for r in results if r.risk_level == 'medium')
  low = total_risks - high - mid

  lines.append(f'- 检测图片: {len(results)} 张')
  lines.append(f'- 发现风险: {total_risks} 项')
  lines.append(f'  - 🔴 高风险: {high} 项')
  lines.append(f'  - 🟡 中风险: {mid} 项')
  lines.append(f'  - 🟢 低风险: {low} 项')
  lines.append('')

  # 按风险等级排序
  severity_order = {'high': 0, 'critical': 0, 'medium': 1, 'low': 2}
  all_violations = sorted(
    [(v, r.image_name) for r in results for v in r.violations],
    key=lambda x: severity_order.get(
      x[0].category, 3
    ),
  )

  if all_violations:
    lines.append('---')
    lines.append('')
    lines.append('## 违规明细')
    lines.append('')
    lines.append('| # | 类别 | 图片 | 描述 | 法规依据 | 整改建议 |')
    lines.append('|---|------|------|------|----------|----------|')
    for i, (v, image_name) in enumerate(all_violations, 1):
      lines.append(
        f'| {i} | {v.category} | {image_name} '
        f'| {v.description} | {v.regulation} | {v.suggestion} |'
      )
    lines.append('')

  # 各图详情
  lines.append('---')
  lines.append('')
  lines.append('## 各图检测详情')
  lines.append('')

  for idx, result in enumerate(results, 1):
    lines.append(f'### {idx}. {result.image_name}')
    if result.cabinet_type:
      lines.append(f'**机柜类型**: {result.cabinet_type}')
    lines.append(f'**风险等级**: {result.risk_level}')
    lines.append(f'**置信度**: {result.confidence * 100:.0f}%')
    lines.append(f'**评估**: {result.description}')
    lines.append('')
    if result.violations:
      for v in result.violations:
        lines.append(f'- **[{v.category}]** {v.description}')
        if v.regulation:
          lines.append(f'  - 法规: {v.regulation}')
        if v.suggestion:
          lines.append(f'  - 建议: {v.suggestion}')
    else:
      lines.append('✅ 未发现安全隐患')
    lines.append('')

  lines.append('---')
  lines.append(f'*报告由智能运维平台自动生成 | {now}*')

  return '\n'.join(lines)


# ==================== Word 报告 ====================

def generate_word_report(
  results: list[CheckResult],
  title: str = '',
) -> bytes:
  """根据检测结果生成 Word (.docx) 报告。"""
  from docx import Document
  from docx.shared import Pt, Cm, RGBColor
  from docx.enum.text import WD_ALIGN_PARAGRAPH
  from docx.enum.table import WD_TABLE_ALIGNMENT
  from docx.oxml.ns import qn

  now = datetime.now()
  now_str = now.strftime('%Y年%m月%d日')
  today_short = now.strftime('%Y%m%d')
  risk_label = {
    'none': '安全', 'low': '低风险', 'medium': '中风险',
    'high': '高风险', 'critical': '严重风险', 'unknown': '未知',
  }

  doc = Document()

  # 页面设置
  section = doc.sections[0]
  section.page_width = Cm(21)
  section.page_height = Cm(29.7)
  section.left_margin = Cm(2)
  section.right_margin = Cm(2)
  section.top_margin = Cm(2)
  section.bottom_margin = Cm(2)

  # 默认字体
  style = doc.styles['Normal']
  font = style.font
  font.name = 'SimSun'
  font.size = Pt(10.5)
  style.element.rPr.rFonts.set(qn('w:eastAsia'), 'SimSun')

  # 标题
  report_title = title or '润泽科技发展有限公司生产排查报告'
  title_para = doc.add_paragraph()
  title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
  run = title_para.add_run(report_title)
  run.bold = True
  run.font.size = Pt(18)

  # 排查信息
  risk_levels = [r.risk_level for r in results]
  worst_level = 'none'
  for lv in ('critical', 'high', 'medium', 'low', 'none'):
    if lv in risk_levels:
      worst_level = lv
      break

  info = doc.add_paragraph()
  info.alignment = WD_ALIGN_PARAGRAPH.CENTER
  run = info.add_run(
    f'排查日期：{now_str}    |    风险等级：{risk_label.get(worst_level, "未知")}    |    排查图片：{len(results)} 张'
  )
  run.font.size = Pt(10)
  run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

  doc.add_paragraph()

  # 各图结果
  for idx, result in enumerate(results, 1):
    heading = doc.add_paragraph()
    run = heading.add_run(f'{idx}. {result.image_name}')
    run.bold = True
    run.font.size = Pt(12)

    doc.add_paragraph(f'风险等级：{risk_label.get(result.risk_level, "未知")}')
    doc.add_paragraph(f'置信度：{result.confidence * 100:.0f}%')
    if result.cabinet_type:
      doc.add_paragraph(f'机柜类型：{result.cabinet_type}')
    doc.add_paragraph(f'综合描述：{result.description}')

    if result.violations:
      doc.add_paragraph()
      # 表格
      table = doc.add_table(rows=1 + len(result.violations), cols=4)
      table.style = 'Table Grid'
      table.alignment = WD_TABLE_ALIGNMENT.CENTER

      headers = ['风险类别', '违规行为', '依据法规', '整改建议']
      for i, text in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = ''
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        run.bold = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shading = cell._element.get_or_add_tcPr()
        shading_elm = shading.makeelement(qn('w:shd'), {
          qn('w:fill'): '1a5ce4',
          qn('w:val'): 'clear',
        })
        shading.append(shading_elm)

      for row_idx, v in enumerate(result.violations):
        row = table.rows[row_idx + 1]
        for col, text in enumerate([v.category, v.description, v.regulation, v.suggestion]):
          cell = row.cells[col]
          cell.text = ''
          p = cell.paragraphs[0]
          run = p.add_run(text)
          run.font.size = Pt(9)

    doc.add_paragraph()

  # 页脚
  doc.add_paragraph()
  footer = doc.add_paragraph()
  footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
  run = footer.add_run('本报告由 AI 智能安全检测系统自动生成')
  run.font.size = Pt(9)
  run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
  footer2 = doc.add_paragraph()
  footer2.alignment = WD_ALIGN_PARAGRAPH.CENTER
  run = footer2.add_run(f'生成时间：{now_str}')
  run.font.size = Pt(9)
  run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

  output = io.BytesIO()
  doc.save(output)
  return output.getvalue()


def generate_word_report_filename(title: str = '') -> str:
  """生成 Word 报告文件名"""
  today = datetime.now().strftime('%Y%m%d')
  return f"润泽科技生产排查报告_{today}.docx"


def generate_fire_safety_word_report(result: FireSafetyResult) -> bytes:
  """根据消防配置推荐结果生成 Word (.docx) 报告。"""
  from docx import Document
  from docx.shared import Pt, Cm, RGBColor
  from docx.enum.text import WD_ALIGN_PARAGRAPH
  from docx.enum.table import WD_TABLE_ALIGNMENT
  from docx.oxml.ns import qn

  now = datetime.now()
  now_str = now.strftime('%Y年%m月%d日 %H:%M')
  today_short = now.strftime('%Y%m%d')
  risk_label = {
    'none': '安全', 'low': '低风险', 'medium': '中风险',
    'high': '高风险', 'critical': '严重风险', 'unknown': '未知',
  }
  priority_label = {
    'mandatory': '强制性必配', 'recommended': '推荐优化', 'optional': '因地制宜可选',
  }

  doc = Document()

  section = doc.sections[0]
  section.page_width = Cm(21)
  section.page_height = Cm(29.7)
  section.left_margin = Cm(2)
  section.right_margin = Cm(2)
  section.top_margin = Cm(2)
  section.bottom_margin = Cm(2)

  style = doc.styles['Normal']
  font = style.font
  font.name = 'SimSun'
  font.size = Pt(10.5)
  style.element.rPr.rFonts.set(qn('w:eastAsia'), 'SimSun')

  def _add_heading(text, level=1):
    h = doc.add_paragraph()
    run = h.add_run(text)
    run.bold = True
    run.font.size = Pt(16 if level == 1 else 14)
    if level == 1:
      h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return h

  def _add_table(headers, rows_data, col_widths=None):
    table = doc.add_table(rows=1 + len(rows_data), cols=len(headers))
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, text in enumerate(headers):
      cell = table.rows[0].cells[i]
      cell.text = ''
      p = cell.paragraphs[0]
      p.alignment = WD_ALIGN_PARAGRAPH.CENTER
      run = p.add_run(text)
      run.bold = True
      run.font.size = Pt(9)
      run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
      shading = cell._element.get_or_add_tcPr()
      shading_elm = shading.makeelement(qn('w:shd'), {
        qn('w:fill'): '1a5ce4',
        qn('w:val'): 'clear',
      })
      shading.append(shading_elm)
    for idx, row_data in enumerate(rows_data):
      row = table.rows[idx + 1]
      for col, text in enumerate(row_data):
        cell = row.cells[col]
        cell.text = ''
        p = cell.paragraphs[0]
        run = p.add_run(str(text))
        run.font.size = Pt(9)
    if col_widths:
      for i, width in enumerate(col_widths):
        for row in table.rows:
          row.cells[i].width = width
    return table

  # 标题
  _add_heading('消防安全配置推荐报告', 1)

  info = doc.add_paragraph()
  info.alignment = WD_ALIGN_PARAGRAPH.CENTER
  run = info.add_run(f'生成时间：{now_str}    |    风险等级：{risk_label.get(result.risk_level, "未知")}')
  run.font.size = Pt(10)
  run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

  doc.add_paragraph()

  # 一、建筑参数与总体评估
  _add_heading('一、建筑参数与总体评估', 2)
  doc.add_paragraph(f'建筑类型：{result.building_type or "-"}')
  doc.add_paragraph(f'建筑高度：{result.building_height} m    总建筑面积：{result.building_area:,.0f} m²')
  if result.structural_form:
    doc.add_paragraph(f'结构形式：{result.structural_form}')
  doc.add_paragraph(f'风险等级：{risk_label.get(result.risk_level, "未知")}')
  doc.add_paragraph(f'总体评估：{result.summary}')

  doc.add_paragraph()

  # 二、消防设施配置
  if result.fire_facilities:
    _add_heading('二、消防设施配置', 2)
    rows = []
    for f in result.fire_facilities:
      rows.append([
        priority_label.get(f.priority, f.priority),
        f.name, f.specification,
        f.quantity_or_coverage, f.installation_location,
      ])
    _add_table(
      ['优先级', '设施名称', '规格要求', '数量/配置', '安装位置'],
      rows,
      [Cm(1.8), Cm(2.5), Cm(2.5), Cm(3), Cm(3.5)],
    )
    doc.add_paragraph()

  # 三、建筑材料要求
  if result.building_materials:
    _add_heading('三、建筑材料要求', 2)
    rows = []
    for m in result.building_materials:
      rows.append([
        priority_label.get(m.priority, m.priority),
        m.name, m.specification, m.application_location,
      ])
    _add_table(
      ['优先级', '材料名称', '规格要求', '应用部位'],
      rows,
      [Cm(1.8), Cm(3), Cm(4), Cm(4.5)],
    )
    doc.add_paragraph()

  # 四、消防安全管理
  if result.safety_preparations:
    _add_heading('四、消防安全管理', 2)
    rows = []
    for s in result.safety_preparations:
      rows.append([
        priority_label.get(s.priority, s.priority),
        s.name, s.requirement,
      ])
    _add_table(
      ['优先级', '事项名称', '具体要求'],
      rows,
      [Cm(1.8), Cm(3), Cm(8.5)],
    )
    doc.add_paragraph()

  # 五、适用标准
  if result.applicable_standards:
    _add_heading('五、适用标准', 2)
    for s in result.applicable_standards:
      p = doc.add_paragraph(s)
      p.style.font.size = Pt(9)

  doc.add_paragraph()

  # 页脚
  footer = doc.add_paragraph()
  footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
  run = footer.add_run('本报告由 AI 消防安全配置推荐系统自动生成，仅供参考')
  run.font.size = Pt(9)
  run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
  footer2 = doc.add_paragraph()
  footer2.alignment = WD_ALIGN_PARAGRAPH.CENTER
  run = footer2.add_run(f'生成时间：{now_str}')
  run.font.size = Pt(9)
  run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

  output = io.BytesIO()
  doc.save(output)
  return output.getvalue()


def generate_fire_safety_word_report_filename() -> str:
  today = datetime.now().strftime('%Y%m%d')
  return f'消防安全配置推荐报告_{today}.docx'
