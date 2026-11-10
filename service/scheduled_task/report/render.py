"""固定 HTML 版式 + 沙箱出 PDF —— **全确定性，0 次 LLM**。

版式统一由 `core/sandbox/report_style.RZ_CSS` 提供（`render_pdf` 的 css 缺省就是它），
所以这里只负责把统计结果与解读文字**填进固定骨架**，不产生任何样式决策。

所有来自数据的文本一律 `html.escape`：工单名称里出现 `<`、`&` 会破坏版式甚至注入 HTML。
"""

import html as _html
import json
import re
from pathlib import Path

from config.settings import settings
from core.sandbox.runner import run_python
from service.scheduled_task.report.common import (
  DEPT_TABLE_LIMIT, _delta_text, _human_minutes, _pct_text, _trim0,
)
from service.scheduled_task.report.facts import impact_level, is_weekly
from service.scheduled_task.report.spec import TERM_GLOSSARY

# 报告脚本比对话里的一次性分析重（8 张图 + PDF），超时给足
_REPORT_TIMEOUT = 300

# 周报逐单详情表的单格上限：几十条工单 × 3 列，全文会把 PDF 撑到几十页
_WEEKLY_CELL_CHARS = 120

# 附录标题：目录条目、正文指路句、附录卡片的 <h2> 共用，避免三处漂移
_APPENDIX_TITLE = '附录　本期工单详情'

# 第 7 章重复清单 + 关联告警（事件报告）：全量类目 + 每个类目的告警子表体量大，
# 放附录、正文只留一句带跳转链接的指路。
_REPEAT_APPENDIX_SHORT = '重复问题与关联告警'
_REPEAT_APPENDIX_TITLE = f'附录　{_REPEAT_APPENDIX_SHORT}'

# 月度专属术语：对应内容只在月报出现（节奏图 `monthly_only`），周报第 13 章不列
_MONTHLY_ONLY_TERMS = frozenset({'周内节奏'})


def _clip(value, limit: int) -> str:
  text = '' if value is None else str(value)
  return text if len(text) <= limit else text[:limit] + '…'


def _e(value) -> str:
  """HTML 转义（None → 空串）。"""
  return _html.escape('' if value is None else str(value))


def _score_text(value) -> str:
  """分值显示：整数不带小数（100 / 85），其余保留一位（81.8）。"""
  num = float(value)
  return str(int(num)) if num.is_integer() else f'{num:.1f}'


def _md(value) -> str:
  """极简 markdown：`` `code` `` → `<code>`、`**strong**` → `<b>`。

  规格声明里的口径文字是按 markdown 写的（docs 与提示词都这么写），直接塞进 HTML 会把
  `**` 和反引号原样印到 PDF 上。**先转义再替换**，所以不会引入注入面。
  """
  text = _e(value)
  text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
  return re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)


def _table_md(headers: list[str], rows: list[list]) -> str:
  """表格的「口径/说明」列走 `_md`（数据列仍走 `_e`，避免数据里的 * 被吃掉）。"""
  head = ''.join(f'<th>{_e(h)}</th>' for h in headers)
  body = ''.join('<tr>' + ''.join(f'<td>{_md(c)}</td>' for c in row) + '</tr>'
                 for row in rows)
  return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _card(label: str, value: str, sub: str) -> str:
  return (f'<div class="kpi-card"><div class="kpi-label">{_e(label)}</div>'
          f'<div class="kpi-value">{_e(value)}</div>'
          f'<div class="kpi-sub">{_e(sub)}</div></div>')


def _table(headers: list[str], rows: list[list], nowrap_from: int | None = None,
           nowrap_cols: tuple[int, ...] = ()) -> str:
  """`nowrap_from=N` 时第 N 列起的单元格不折行（口径列很长时数值列会被挤成「1.9 小/时」）；
  `nowrap_cols` 指定**单列**（后缀式覆盖不到首列 —— 全国逐单表前置的「园区」列曾被挤成一字一行）。

  默认 `None` / 空 → 其余表格输出**逐字不变**（e2e 快照只为调用了它的表变化）。
  """
  def _cell(idx: int, value) -> str:
    nw = (nowrap_from is not None and idx >= nowrap_from) or idx in nowrap_cols
    cls = ' class="nw"' if nw else ''
    return f'<td{cls}>{_e(value)}</td>'

  head = ''.join(f'<th>{_e(h)}</th>' for h in headers)
  body = ''.join('<tr>' + ''.join(_cell(i, c) for i, c in enumerate(row)) + '</tr>'
                 for row in rows)
  return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _summary(items) -> str:
  """章末「本章要点」——**数据（图/表）在前、说明在后**（用户要求：先图再说明）。
  带一个小标签，否则跟在表格后面会分不清是表的一部分还是章级结论。
  """
  body = _bullets(items)
  return f'<p class="sum-head">本章要点</p>{body}' if body else ''


def _bullets(items, cls: str = '') -> str:
  """分点列表（解读文字一律分点，不堆整段）。"""
  items = [x for x in (items or []) if str(x).strip()]
  if not items:
    return ''
  return ('<ul class="%s">' % cls if cls else '<ul>') + ''.join(
    f'<li>{_md(x)}</li>' for x in items) + '</ul>'


def _chart(fig_no: int, chart, existing: set[str], notes=(), title: str = '') -> str:
  """图：**标题在图上方**（深蓝加粗居中），图内不画标题；**紧接着放这张图自己的说明**。

  为什么说明必须就近放：原先每章把三张图排完、解读统一放章末，于是前两张图周围一片空白
  （跨页后更明显）—— 用户实测反馈「前两个图既没说明也没文字」。图号保留，正文要能引用。

  `title` 非空时覆盖 `chart.title`（周报改用详情口径沿用的同一张图，见 `Chart.weekly_title`）。
  """
  title = title or chart.title
  if chart.file not in existing:
    return f'<p class="note">图 {fig_no}（{_e(title)}）本次无可用数据，未生成。</p>'
  style = f' style="max-width:{_e(chart.width)}"' if chart.width else ''
  return (f'<div class="figblock"><div class="figure">图 {fig_no}　{_e(title)}</div>'
          f'<img src="charts/{_e(chart.file)}.png"{style}/></div>'
          + _bullets(notes, 'chart-notes'))


def _impact_level(ctx: '_Ctx') -> dict | None:
  """周报可用「级别 × 影响时长」矩阵时返回它，否则 None（月报/详情不足 → 走验收口径）。

  判据在 `facts.impact_level`（render 标题/表格、facts 事实、narrate 图表标题共用同一个），
  避免出现"标题写影响时长、表里却是验收时长"。详情在 `run_compute` 之后才取到，
  所以图由 `draw_level_duration` 二次绘制覆盖，统计阶段画不了。
  """
  return impact_level(ctx.stats, ctx.details)


def _sec(narr: dict, section_no: int) -> dict:
  """取某一章的解读（形如 {要点: [...], 图说明: {图键: [...]}}）；没有则空 dict。"""
  return ((narr or {}).get('章节') or {}).get(str(section_no)) or {}


def _narr_missing(narr: dict) -> str:
  if narr and (narr.get('章节') or narr.get('执行摘要')):
    return ''
  return ('<p class="note">（本轮解读文字未能生成——报告的数字、图表与结论口径均不受影响，'
          '由上一步确定性统计直接产出。）</p>')


class _Ctx:
  """每章渲染器共用的上下文：把「取解读 / 取图 / 拼表」等收在一处。

  这样渲染器可以写成模块级 `(ctx, no) -> str` 并放进**角色注册表** —— 「加一类报告」
  只需在 spec 里声明角色，渲染代码不动（定角色而非定章号）。
  """

  __slots__ = ('stats', 'details', 'narr', 'spec', 'existing', 'fig_no', 'appendix')

  def __init__(self, stats, details, narr, spec, existing, fig_no):
    self.stats, self.details, self.narr = stats, details, narr
    self.spec, self.existing, self.fig_no = spec, existing, fig_no
    # 附录内容：`(锚点, 标题, HTML)`。章节渲染器只负责登记，排到最后一章之后由
    # build_html 统一收尾；顺序即登记顺序（第 7 章 → 第 10 章）。
    self.appendix: list[tuple[str, str, str]] = []

  def sec(self, no: int) -> dict:
    return _sec(self.narr, no)

  def pts(self, no: int) -> list:
    return self.sec(no).get('要点') or []

  def chart(self, no: int, key: str) -> str:
    # 兜底：spec.charts 里没有这个键就跳过。踩过 —— 从 spec 里删图时忘了删渲染层的调用，
    # 直接 KeyError 让**整份报告失败**（2026-10-15）。spec 与渲染层是两处独立编辑的，值得这一行。
    if key not in self.fig_no or self.stats.get('charts_suppressed'):
      return ''
    chart = next(c for c in self.spec.charts if c.key == key)
    required = getattr(chart, 'requires_key', '')
    if required and not self.stats.get(required):
      return ''          # 本章维度不存在（如全国报告没有部门维度）：不出图，也不留占位
    period = (self.stats.get('window') or {}).get('period')
    if getattr(chart, 'monthly_only', False) and period != 'monthly':
      return ''          # 周报没有这张图的数据：不出图，也不留"未生成"占位
    # 周报改用详情口径时换标题（只有数据确实来自该口径才换，见 `_impact_level`）
    weekly_title = getattr(chart, 'weekly_title', '')
    title = weekly_title if (weekly_title and _impact_level(self)) else ''
    return _chart(self.fig_no[key], chart, self.existing,
                  (self.sec(no).get('图说明') or {}).get(key) or [], title=title)

  def charts(self, no: int) -> str:
    """本章的全部图，**按 spec.charts 的声明顺序** —— 不再在渲染层写死图键。"""
    return ''.join(self.chart(no, c.key) for c in self.spec.charts if c.section == no)

  def end(self, no: int) -> str:
    return _summary(self.pts(no))


# ==================== 章节渲染器（按 spec.roles 分派） ====================
# 角色相同的章两类工单**共用同一份实现**；差异（标签 / 行 / 列）全部来自 spec 声明。
# 固定角色 summary/kpi/action/notes 也在此注册，`build_html` 只按 roles 依次调用。

def _r_summary(ctx: _Ctx, no: int) -> str:
  points = (ctx.narr or {}).get('执行摘要') or []
  out = ('<ul>' + ''.join(f'<li>{_e(p)}</li>' for p in points) + '</ul>') if points else ''
  return out + _narr_missing(ctx.narr)


def _r_kpi(ctx: _Ctx, no: int) -> str:
  kpi = ctx.stats.get('kpi', {})
  # 周报的「超 30 天未关闭」实际是「超 3 天未关闭」：标签由 compute 按周期声明，月报无覆盖
  labels = ctx.stats.get('kpi_labels') or {}
  out = ('<div class="kpi-grid">'
         + ''.join(_card(labels.get(k.key, k.label), kpi[k.key]['value'],
                         kpi[k.key]['sub'])
                   for k in ctx.spec.kpi if k.key in kpi)
         + '</div>')
  if ctx.stats.get('charts_suppressed'):
    # 样本过少时不出图：一张只有 1~2 个柱子的图读不出分布，还会给出 0.0/0.2 这类无意义刻度
    out += (f'<p class="note">本期仅 {ctx.stats.get("total", 0)} 条工单，样本过少，'
            '图表不足以呈现分布，相关章节已省略图示，直接以表格列出明细。</p>')
    return out
  # 关键指标综合评分（仅声明了 scorecard 的报告）：雷达图 + 分值表，确定性产出、无 LLM
  sc = ctx.stats.get('scorecard') or {}
  rows = sc.get('rows') or []
  if rows:
    out += ctx.charts(no)
    body = []
    for row in rows:
      cur_val, prev_val = float(row['本期']), float(row['上一期'])
      delta = cur_val - prev_val
      delta_text = ('持平' if abs(delta) < 1e-9
                    else ('+' if delta > 0 else '') + _score_text(delta))
      body.append([row['维度'], _score_text(cur_val), _score_text(prev_val),
                   delta_text])
    out += _table(('评分维度', '本期', '上一期', '变化'), body)
    meanings = [(r.get('维度', ''), r.get('含义', '')) for r in rows
                if r.get('含义')]
    if meanings:
      out += ('<div class="dim-meanings"><p class="note"><b>评分维度含义</b></p><ul>'
              + ''.join(f'<li><b>{_e(d)}</b>：{_e(m)}</li>' for d, m in meanings)
              + '</ul></div>')
    out += ('<p class="note">每项 0–100 分，越高越好；'
            f'评分规则见第 {len(ctx.spec.sections)} 章「数据说明」。</p>')
  return out


def _r_month_rhythm(ctx: _Ctx, no: int) -> str:
  """月报专属：月内节奏（周表 + 节奏图 + 工作日/周末 + 星期分布）+ 环比变化 TOP。

  周报 `stats` 没有 `rhythm`/`mom_top` 两个键，直接返回空串 —— 周报章节逐字不变。
  """
  rhythm = ctx.stats.get('rhythm') or {}
  if not rhythm:
    return ''
  out = ('<p class="note"><b>月内节奏</b>：按自然周（周一为界）分组；首末周天数可能不足 '
         '7 天，比较各周请看<b>日均</b>，不要直接比单量。</p>')
  weeks = rhythm.get('weeks') or []
  out += _table(['周次', '区间', '天数', '单量', '日均', '工作日日均', '周末日均'],
                [[w['label'], f'{w["start"][5:]}~{w["end"][5:]}', w['days'], w['count'],
                  _trim0(w['avg']), _trim0(w['work_avg']), _trim0(w['rest_avg'])]
                 for w in weeks])
  out += ctx.chart(no, 'month_rhythm')
  split = rhythm.get('split') or []
  if split:
    out += '<p class="note"><b>工作日与周末</b></p>'
    out += _table(['口径', '天数', '单量', '日均'],
                  [[b['label'], b['days'], b['count'], _trim0(b['avg'])] for b in split])
  dow = rhythm.get('dow') or []
  if dow:
    out += '<p class="note"><b>星期分布</b></p>'
    out += _table(['星期', '单量'], [[d['label'], d['count']] for d in dow])
  mom = ctx.stats.get('mom_top') or []
  if mom:
    out += '<p class="note"><b>环比变化 TOP</b>（按变化量绝对值取前 5，各维度分别取行后混排）</p>'
    out += _table(['维度', '项目', '本期', '上一期', '增减', '占比变化'],
                  [[r['维度'], r['项目'], r['本期'], r['上一期'], _delta_text(r['增减']),
                    (_pct_text(r['占比变化'], signed=True).replace('%', ' 个百分点')
                     if r.get('占比变化') is not None else '—')] for r in mom])
  return out


def _r_trend(ctx: _Ctx, no: int) -> str:
  t = ctx.stats['trend']
  unit = getattr(ctx.spec, 'count_unit', '起')
  # 只取趋势图：月报的节奏图排在自己的表附近（`_r_month_rhythm`），不能一并提前渲染
  out = ctx.chart(no, 'trend')
  prev_days = t.get('prev_days') or []
  rows = []
  for i, (lab, cur) in enumerate(zip(t['labels'], t['cur'])):
    # 月报 31 天 vs 上期 30 天：末位没有对位日期 → 「—」而不是 0（0 会被读成"上期当天零单"）
    none = prev_days and i < len(prev_days) and not prev_days[i]
    prev = '—' if none else (t['prev'][i] if i < len(t['prev']) else '—')
    rows.append([lab, cur, prev])
  out += _table(['日期', '本期工单量', '上一期同日'], rows)
  total, prev_total, mom = ctx.stats['total'], ctx.stats['prev_total'], ctx.stats['mom']
  out += f'<p>本周期共 <b>{total}</b> {_e(unit)}，上一期 <b>{prev_total}</b> {_e(unit)}，'
  if int(mom['delta'] or 0) == 0:
    out += '环比持平'
  else:
    out += f'环比 <b>{_delta_text(mom["delta"])}</b> {_e(unit)}'
    if mom['pct'] is not None:
      out += f'（{_pct_text(mom["pct"], signed=True)}）'
  cur_vals = [int(v) for v in (t.get('cur') or [])]
  if cur_vals and len(set(cur_vals)) == 1:
    # 全平序列（周报常见）：峰谷是同一批日期，写出来只会自相矛盾
    out += f'。每日均为 {cur_vals[0]} {_e(unit)}（共 {len(cur_vals)} 天）。</p>'
  else:
    out += f'。峰值出现在 {_e(t["peak_day"])}（{t["peak_n"]} {_e(unit)}），' \
           f'低谷 {_e(t["low_day"])}（{t["low_n"]} {_e(unit)}）。</p>'
  return out + _r_month_rhythm(ctx, no) + ctx.end(no)


def _r_level(ctx: _Ctx, no: int) -> str:
  out = ctx.charts(no)
  out += _table([ctx.spec.level_label, '工单量', '占比'],
                [[r['label'], r['count'], f'{r["pct"]}%'] for r in ctx.stats['level']])
  # 级别 × 影响时长（周报·详情口径）：用户 2026-10-28 明确要求联合分析看**业务影响时长**，
  # 不是验收时长。只陈述数据，不下"处置能力好/差"的结论（等级判据不由平台硬写）。
  # 2026-10-29 用户要求**删掉各级 P50/P90/最长表**：档位构成在图上已能读出，分位表重复且
  # 挡在章末要点之前；只留一句读图与口径说明。分位数据仍下发给解读层（`facts.impact_level`）。
  if _impact_level(ctx):
    # 章号按 roles 算：插章/并章后「第 N 章」写死必错（事件并掉专业章后时长章前移一位）
    dur_no = ctx.spec.roles.index('duration') + 1 if 'duration' in ctx.spec.roles else None
    where = f'第 {dur_no} 章的' if dur_no else '「处理时效」章里的'
    out += ('<p class="note">级别数字越小等级越高（0 级最高），「级别 × 影响时长构成」图为'
            f'各级的影响时长档位构成。影响时长取自工单详情，与{where}验收时长是两个口径，'
            '不能互相换算。</p>')
    return out + ctx.end(no)
  # 月报（只取代表单详情）回退验收口径：单看级别分布看不出"高等级单是否处置更慢"，
  # 联合表按级别给分位与超时率。
  ld = ctx.stats.get('level_duration') or {}
  rows = ld.get('by_level') or []
  if rows:
    out += '<h2>级别 × 验收时长</h2>'
    out += ('<p class="note">级别数字越小等级越高（0 级最高）。表内是各级的验收时长分位与'
            '验收超时率，用于看高等级单是否处置更慢；时长档位构成见上图。</p>')
    out += _table([ctx.spec.level_label, '单数', '占比', 'P50', 'P90', '超时单数', '超时率'],
                  [[r['级别'], r['单数'], f'{r["占比"]}%',
                    _human_minutes(r['P50分钟']), _human_minutes(r['P90分钟']),
                    r['超时单数'],
                    f'{r["超时率"]}%' if r['超时率'] is not None else '—']
                   for r in rows])
  return out + ctx.end(no)


def _r_park(ctx: _Ctx, no: int) -> str:
  # 单园区预设的列表**不带 `companyName`** → 无跨园区数据；别再摆空表和"未生成"占位。
  # 变更单改用**部门维度补位**（`dept` 键只在单园区写入），其余四类仍是空态说明。
  # 专业表在单园区同样要有（专业构成不依赖跨园区对比）。
  if not ctx.stats.get('park'):
    changed = bool(ctx.stats.get('dept'))
    out = ctx.charts(no) if changed else ''
    if not changed:
      out += '<p class="note">本次统计范围为单个园区，无跨园区对比数据。</p>'
    out += _dept_compare_block(ctx)
    return out + _park_specialty_block(ctx) + ctx.end(no)
  out = ctx.charts(no)
  rows = []
  for p in ctx.stats['park']:
    delta = p['count'] - p['prev']
    pct = ('持平' if not delta else
           _pct_text(delta / p['prev'] * 100, signed=True) if p['prev'] else '—')
    rows.append([p['name'], p['count'], p['prev'], _delta_text(delta), pct, f'{p["pct"]}%'])
  out += _table(['园区', '本期', '上一期', '环比', '环比幅度', '占比'], rows)
  return out + _park_specialty_block(ctx) + ctx.end(no)


def _dept_compare_block(ctx: _Ctx) -> str:
  """单园区报告第 5 章的部门横比表（全国级没有 `dept` 键 → 返回空串，章内逐字不变）。"""
  rows = ((ctx.stats.get('dept') or {}).get('rows') or [])[:DEPT_TABLE_LIMIT]
  if not rows:
    return ''
  # 只留结构（单量/环比）与两类时效：紧急占比在第 7 章、按时率在第 9 章、驳回率在第 8 章异常表
  body = [[r['name'], r['count'], r['prev'], _delta_text(r['delta']), f'{r["pct"]}%',
           _human_minutes(r.get('approve_p50')), _human_minutes(r.get('exec_p50'))]
          for r in rows]
  return ('<p class="sum-head">部门效率对比</p>'
          + _table(['部门', '本期', '上一期', '环比', '占比', '审批 P50', '实施 P50'], body)
          + f'<p class="note">部门取自变更单的「变更机房」字段（按工单量降序，'
            f'最多 {DEPT_TABLE_LIMIT} 行）；P50 样本少于 3 单时显示「—」。</p>')


def _park_specialty_block(ctx: _Ctx) -> str:
  """园区章追加的「各专业总量」（事件单并章用，2026-10-29）。

  表就是同章「各园区 × 专业构成」堆叠图的列合计，不再单独成章；`park_specialty_table`
  为假（故障/变更/问题/阈值）或没有专业数据时返回空串，那些报告的第 5 章逐字不变。
  """
  if not getattr(ctx.spec, 'park_specialty_table', False) or not ctx.stats.get('specialty'):
    return ''
  return '<p class="sum-head">各专业总量</p>' + _specialty_table(ctx)


def _specialty_table(ctx: _Ctx) -> str:
  return _table(['专业', '本期', '上一期', '占比'],
                [[r['label'], r['count'], r['prev'] if r.get('prev') is not None else '—',
                  f'{r["pct"]}%'] for r in ctx.stats['specialty']])


def _r_specialty(ctx: _Ctx, no: int) -> str:
  # 变更单仍用这个角色（园区章不并专业）。事件单 2026-10-29 起把专业并入园区章
  # （`_r_park` 的 `_park_specialty_block`），不再走这里。
  return ctx.charts(no) + _specialty_table(ctx) + ctx.end(no)


def _r_equipment_specialty(ctx: _Ctx, no: int) -> str:
  """专业 + **设备类型**分布（故障单专属）：专业表之后补一张设备类型 Top10 表。

  设备类型是故障单最有治理价值的维度（直接指向"哪类设备在坏"），其帕累托图由 spec.charts
  声明在本章（`equipment_pareto`），随 `ctx.charts` 一起出。
  """
  out = ctx.charts(no) + _specialty_table(ctx)
  eq = (ctx.stats.get('equipment') or {}).get('rows') or []
  if eq:
    out += _table(['设备类型', '单量', '占比'],
                  [[r['name'], r['count'], f'{r["pct"]}%'] for r in eq[:10]])
  return out + ctx.end(no)


def _r_equipment(ctx: _Ctx, no: int) -> str:
  """故障单专属：专业构成 + **设备类型分布**（帕累托 + 频次×时长散点）。

  设备类型是故障单最有治理价值的维度（事件单没有），故专业表之后是设备类型图与 Top 表。
  """
  out = _specialty_table(ctx)
  out += ctx.charts(no)          # 04 帕累托 + 05 散点
  eq = (ctx.stats.get('equipment') or {}).get('rows') or []
  if eq:
    out += _table(['设备类型', '单量', '占比'],
                  [[r['name'], r['count'], f'{r["pct"]}%'] for r in eq[:10]])
  return out + ctx.end(no)


def _r_dept(ctx: _Ctx, no: int) -> str:
  """故障单专属：**故障部门分布与环比**（发散条形图 + 部门表）—— 事件单没有的维度。"""
  out = ctx.charts(no)
  dept = (ctx.stats.get('dept') or {}).get('rows') or []
  if dept:
    out += _table(['部门', '本期单量', '上期单量', '环比增减'],
                  [[r['name'], r['count'], r.get('prev', 0), _delta_text(r['delta'])]
                   for r in dept[:10]])
  return out + ctx.end(no)


def _duration_value(token: str, a: dict) -> str:
  """第 7 章某一行（token ∈ 闭集）→ 显示值。**单位换算只在这里做**，解读层不得自算。"""
  r = a.get('readable') or {}
  if token == 'valid':
    miss = (a.get('rows') or 0) - (a.get('valid') or 0)
    return f'{a["valid"]} / {a["rows"]} 单' + (f'（未回填 {miss} 单）' if miss > 0 else '')
  if token in ('p50', 'p90', 'p95'):
    # 只给可读时长（>60 分钟自动成小时）——同时印原始分钟会出现「1958.8 分钟」这种长数字
    return r.get(token) or _human_minutes(a.get(token))
  if token == 'mean_max':
    return (f'{r.get("mean") or _human_minutes(a.get("mean"))} / '
            f'{r.get("max") or _human_minutes(a.get("max"))}')
  if token == 'timeout':
    return f'{a["timeout_n"]} / {a["timeout_base"]} 单（{a["timeout_rate"]}%）'
  if token == 'over_1d':
    return f'{a["over_1d_n"]} / {a["rows"]} 单（{a["over_1d_rate"]}%）'
  return '—'


def _r_impact(ctx: _Ctx) -> str:
  """「影响时长（业务影响口径）」小节：只在 spec 声明了 `impact_fields` 时出现。

  与上面的验收时长是**两个口径**（详情 vs 列表），刻意分小节呈现，不做加减、不做换算。
  月报只取代表单详情 → 不给分布，只写一句指向周报。
  """
  if not getattr(ctx.spec, 'impact_fields', ()):
    return ''
  imp = (ctx.details or {}).get('impact') or {}
  if not imp:
    return ''
  period = (ctx.stats.get('window') or {}).get('period')
  if period != 'weekly':
    return ('<p class="note">影响时长需要逐单详情，月报只取代表单详情，'
            '故本期不做影响时长分布分析（见同期周报）。</p>')
  if not imp.get('available'):
    return f'<p class="note">{_e(imp.get("reason") or "影响时长数据不足")}，本期不做分布分析。</p>'
  out = '<h2>影响时长（业务影响口径）</h2>'
  out += (f'<p class="note">影响时长取自工单详情的「影响时长」字段，衡量业务受影响的持续时间；'
          f'与上面的{_e(ctx.spec.duration_label)}是两个口径，不能互相换算。'
          f'本期详情 {imp["n"]} 单中 {imp["valid"]} 单有可解析的影响时长'
          + (f'，{imp["missing"]} 单未填。' if imp['missing'] else '。') + '</p>')
  hist = imp['hist']
  out += _table(['指标', '数值'],
                [['影响时长 P50 / P90', f'{imp["readable"]["p50"]} / {imp["readable"]["p90"]}'],
                 ['最长影响时长', imp['readable']['max']],
                 ['影响不足 1 小时的单', f'{imp["short_pct"]}%']])
  out += _table(['影响时长档', '单数'],
                [[label, n] for label, n in zip(hist['labels'], hist['counts'])])
  # 按级别的分位表已在第 4 章「级别 × 影响时长」给出，这里不再重复（同一维度两份没有新信息）
  return out


def _r_duration(ctx: _Ctx, no: int) -> str:
  out = ctx.charts(no)
  a = ctx.stats['accept']
  if a.get('available'):
    out += _table(['指标', '数值'],
                  [[label, _duration_value(token, a)]
                   for token, label in ctx.spec.duration_rows])
  else:
    out += (f'<div class="callout">本周期没有可用的{_e(ctx.spec.duration_label)}记录，'
            f'该口径数据不可得。</div>')
  return out + _r_impact(ctx) + ctx.end(no)


def _r_repeat(ctx: _Ctx, no: int) -> str:
  rep = ctx.stats.get('repeat') or []
  out = ctx.charts(no)      # 问题报告 §9 有图；事件/故障无图为空串
  if not rep:               # 空表只剩表头，读者会以为数据丢了
    return out + '<p class="note">本期没有识别到重复问题。</p>' + ctx.end(no)
  link = ctx.details.get('alarm_links') or {}
  # **全部类目**（不截断）：用户 2026-11-02 明确要求全量，上限不是他定的
  table = _table([ctx.spec.repeat_title, '次数', '涉及园区', '涉及机房'],
                 [[r['name'], r['count'], '、'.join(r['parks']), '、'.join(r['dcs'])]
                  for r in rep])
  block = table + _alarm_link_block(link)
  if getattr(ctx.spec, 'repeat_appendix', False):
    # 事件报告：全量清单 + 告警子表体量大，整体放附录，正文留一句跳转
    ctx.appendix.append(('sec-repeat-appendix', _REPEAT_APPENDIX_TITLE, block))
    linked = int(link.get('linked_n') or 0) if link.get('available') else 0
    out += (f'<p class="note">本期识别到 {len(rep)} 类重复问题'
            + (f'，其中 {linked} 类关联到 DCIM 告警' if linked else '')
            + f'。完整清单与关联告警见 <a href="#sec-repeat-appendix">'
              f'附录「{_e(_REPEAT_APPENDIX_SHORT)}」</a>。</p>')
  else:
    out += block
  return out + ctx.end(no)


_ALARM_HEADERS = ('时间', '等级', '设备 / 测点', '告警内容', '告警值',
                  '提示', '一般', '重要', '紧急', '越限档')


def _reason_distribution(clusters: list[dict]) -> str:
  """未关联类目 → 按逐次发生的判定原因分组计数（一行 = 一次发生）。"""
  reasons: dict[str, int] = {}
  for cluster in clusters:
    for occ in (cluster.get('occurrences') or [cluster.get('occurrence') or {}]):
      reason = str((occ or {}).get('reason') or '未判定')
      reasons[reason] = reasons.get(reason, 0) + 1
  return '、'.join(f'{_e(r)} {n} 次'
                  for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]))


def _alarm_link_block(link: dict) -> str:
  """重复类目下挂关联告警子表（事件单专属，数据来自 `details['alarm_links']`）。

  **全部关联上的类目都印**（不截断）；未关联的类目按逐次发生的判定原因分组计数，
  不静默消失；全部没关联上时出一句覆盖说明，不出空表 ——
  「未关联」不等于「没有告警」，措辞必须交代这一点。
  """
  from service.scheduled_task.report import alarm_link

  if not link:
    return ''
  if not link.get('available'):
    reason = link.get('reason') or link.get('note') or ''
    return f'<p class="note">未关联到 DCIM 告警（{_e(reason)}）。</p>' if reason else ''
  clusters = [c for c in (link.get('clusters') or []) if isinstance(c, dict)]
  linked = [c for c in clusters if c.get('alarms')]
  unlinked = [c for c in clusters if not c.get('alarms')]
  if not linked:
    dist = _reason_distribution(unlinked)
    if dist:
      return (f'<p class="note">本期 {len(unlinked)} 类重复问题均未关联到 DCIM 告警'
              f'（{dist}）。{_e(link.get("note") or "")}</p>')
    reason = link.get('reason') or link.get('note') or ''
    return f'<p class="note">未关联到 DCIM 告警（{_e(reason)}）。</p>'
  linked.sort(key=lambda c: int(c.get('count') or 0), reverse=True)
  out = ''
  for cluster in linked:
    rows = []
    for alarm in (cluster.get('alarms') or []):
      thresholds = alarm.get('thresholds') or {}
      rows.append([
        alarm.get('time') or '—',
        alarm.get('level') or '—',
        ' / '.join(x for x in (alarm.get('device'), alarm.get('point')) if x) or '—',
        _clip(alarm.get('content'), 40),
        alarm_link.format_value(alarm.get('value'), alarm.get('unit') or ''),
        *(alarm_link.format_trigger(thresholds.get(label))
          for label in ('提示', '一般', '重要', '紧急')),
        '、'.join(alarm.get('breached') or []) or '—',
      ])
    out += (f'<h3>{_e(cluster.get("name"))}（{_e(cluster.get("count"))} 次）</h3>'
            + _table(list(_ALARM_HEADERS), rows, nowrap_from=4))
    total = int(cluster.get('total') or 0)
    if total:
      matched_n = int(cluster.get('matched_n') or 0)
      # 次数与上面的「（N 次）」保持同一口径（按工单行数）；检索时同单号同时刻会去重，
      # 但展示上不引入第二个数字，避免同一页出现 6 和 5 两种说法。
      count = int(cluster.get('count') or 0) or total
      note = (f'本期该类共 {count} 次发生，均关联到 DCIM 告警。' if matched_n >= count
              else f'本期该类共 {count} 次发生，其中 {matched_n} 次关联到 DCIM 告警。')
      out += f'<p class="note">{note}</p>'
  if unlinked:
    # 未关联类目不再静默消失：按逐次发生的判定原因分组计数（一行 = 一次发生）
    out += (f'<p class="note">其余 {len(unlinked)} 类未关联'
            f'（{_reason_distribution(unlinked)}）。</p>')
  out += f'<p class="note">{_e(link.get("note") or "")}</p>'
  return out


# 详情质检各行的说明（「疑似」措辞是刻意的：规则只能识别形态，不判定填报内容真伪）
_QUALITY_ROWS = (
  ('已取详情', 'n', '参与质检的详情条数（取数失败的工单不计入）'),
  ('依据具体', 'specific_n', '原因非空、不笼统，且含设备/机房/数值等可核查证据'),
  ('原因笼统', 'vague_reason_n', '原因不足 10 字，或整句为「无/暂无/按需/按要求/例行维护」等'),
  ('疑似复制', 'copy_n', '处置总结与调整原因高度重合（二元组相似度 ≥ 0.7 或为连续子串）'),
  ('总结模板化', 'boilerplate_n', '总结仅含「已完成/已处理/运行正常」等套话且无数字或单位'),
  ('未填总结', 'no_summary_n', '处置总结为空'),
)


def _quality_block(quality: dict) -> str:
  """详情质检表 + 字段汇总表（详情质检只在声明了 quality_fields 的三类报告里产出）。

  放在逐单表**之前**：先给结论（多少单依据具体/疑似复制），再让读者逐单对照。
  """
  n = int(quality.get('n') or 0)
  if not n:
    return ''
  rows = [[label, quality.get(key, 0),
           f'{round(quality.get(key, 0) / n * 100)}%', desc]
          for label, key, desc in _QUALITY_ROWS]
  out = '<p class="sum-head">详情质检（按规则识别）</p>'
  out += _table_md(['指标', '单数', '占比', '规则说明'], rows)
  out += ('<p class="note">以上为**启发式提示**：规则只识别文本形态（是否具体、是否与原因重合、'
          '是否为套话），不判定填报内容的真伪。「疑似复制」不等于处置未发生。</p>')
  sf = quality.get('summary_fields') or []
  if sf:
    rows2 = []
    for item in sf:
      top = '；'.join(f'{_clip(v["value"], 40)}（{v["n"]} 单）'
                     for v in item.get('top') or [])
      rows2.append([item.get('field', ''), item.get('filled_n', 0), top or '—'])
    out += '<p class="sum-head">详情字段汇总</p>'
    out += _table(['字段', '填写单数', '高频取值（单数）'], rows2)
    out += (f'<p class="note">共 {n} 条详情；「填写单数」为字段非空的条数，'
            '高频取值按原始文本归一化后统计 Top3。</p>')
  return out


def _r_root_cause(ctx: _Ctx, no: int) -> str:
  """第 10 章：详情列**由 spec.detail_columns 声明**（两类字段名不同）。

  刻意**不渲染** `details['note']`：那是工具的内部提示（"一次最多取 N 条"），给模型看的，
  不该出现在给管理层读的报告里（用户实测见到过）。

  周报取**本期全部工单**详情 → 3 列紧凑视图（`weekly_detail_columns`，每格截断 120 字）
  **排到文末附录**（表下注明条数与未取到数），本章只留一句指路；月报仍是 5 条代表单、
  全文列、留在本章，行为不变。

  变更/问题/阈值三类在详情之后跑过 `quality.assess_details`：本章顶部加「详情质检」与
  「详情字段汇总」两张表，逐单表末尾加「质检」列。事件/故障未声明 quality_fields，行为不变。
  """
  weekly = is_weekly(ctx.stats)
  cols = (ctx.spec.weekly_detail_columns if weekly else ()) or ctx.spec.detail_columns
  items = (ctx.details or {}).get('rows', []) or []
  # 全国口径才有 `park`（`workflow` 按 `companyName` 回填）：前置园区列，否则保持不变。
  show_park = any(str(item.get('park') or '').strip() for item in items)

  def _with_park(cells: list, item: dict) -> list:
    if not show_park:
      return cells
    return [str(item.get('park') or '').strip() or '—'] + cells

  quality = (ctx.details or {}).get('quality') or {}
  q_rows = {r.get('ticketNo'): r for r in (quality.get('rows') or [])}
  show_q = bool(quality.get('n'))
  # 长文本归纳（阈值单）：有摘要用摘要，没有就显示全文（**不再截断加省略号**）；
  # 其他报告不声明该字段，仍走原来的 120 字截断。
  condense = tuple(getattr(ctx.spec, 'detail_condense_fields', ()) or ())
  condense_set = set(condense)
  summaries = (ctx.details or {}).get('summaries') or {}
  rows, failed, truncated = [], 0, False
  for item in items:
    f = item.get('fields') or {}
    cells = [f.get(c) for c in cols]
    if not cells or cells[0] in (None, ''):
      cells = [item.get('ticketNo', '')] + cells[1:]
    cells = ['—' if c in (None, '') else c for c in cells]
    if item.get('error'):
      failed += 1
      if weekly:              # 保留编号，让读者知道是哪一单没取到
        cells = [cells[0]] + ['（详情未取到）'] * (len(cells) - 1)
        if show_q:
          cells.append('—')
        rows.append(_with_park(cells, item))
        continue
    sums = summaries.get(str(item.get('ticketNo') or '')) or {}
    if weekly:
      rendered = []
      for col, c in zip(cols, cells):
        if sums.get(col):
          rendered.append(sums[col])
        elif col in condense_set:
          rendered.append(c)                  # 归纳不可用 → 原文全文，不截断
        else:
          truncated = truncated or len(str(c)) > _WEEKLY_CELL_CHARS
          rendered.append(_clip(c, _WEEKLY_CELL_CHARS))
      cells = rendered
    elif sums:                                # 月报不截断：有归纳就替换
      cells = [sums.get(col) or c for col, c in zip(cols, cells)]
    if show_q:
      marks = (q_rows.get(item.get('ticketNo')) or {}).get('flags') or []
      cells.append('、'.join(marks) if marks else '—')
    rows.append(_with_park(cells, item))
  if not rows:
    return '<p class="note">本轮未能取到代表单的详情，根因原文不可得。</p>' + ctx.end(no)
  out = _quality_block(quality)
  head = (['园区'] if show_park else []) + list(cols) + (['质检'] if show_q else [])
  # 园区列内容短、后面三列是 120 字长文本 → 自动布局会把它压到「一个字一行」，单独禁止折行
  table = _table(head, rows, nowrap_cols=(0,) if show_park else ())
  if weekly:
    note = f'共 {len(items)} 条，按工单编号去重'
    if condense:
      note += f'；{"、".join(condense)} 由平台归纳（≤50 字），未能归纳的显示原文'
    if truncated:
      note += f'；超过 {_WEEKLY_CELL_CHARS} 字已截断'
    if failed:
      note += f'；{failed} 条未取到详情'
    # 周报是全量逐单列表（实测 178 行）：留在正文会把本章的质检/成因/要点挤到最后，
    # 统一移到文末附录，正文只留一句指路
    ctx.appendix.append(('sec-appendix', _APPENDIX_TITLE,
                         table + f'<p class="note">{note}。</p>'))
    out += (f'<p class="note">逐单详情（共 {len(items)} 条）'
            f'见文末附录「{_APPENDIX_TITLE}」。</p>')
  else:
    out += table
  return out + _impact_detail_block(ctx) + ctx.end(no)


def _impact_detail_block(ctx: _Ctx) -> str:
  """影响时长最长单的处置过程 + 成因归类（按单首命中）。

  只对声明了 `impact_fields` 的报告出现（当前仅事件单周报）：长时段单逐单看处置过程，
  成因归类用来做**整体归纳**（按单投票，不是关键词命中次数）。
  """
  imp = (ctx.details or {}).get('impact') or {}
  if not imp.get('available') or not is_weekly(ctx.stats):
    return ''
  out = ''
  tail = imp.get('long_tail') or []
  if tail:
    out += '<p class="sum-head">影响时长最长的处置过程</p>'
    out += _table(['编号', '事件名称', '级别', '影响时长', '原因', '处置过程', '处理结果'],
                  [[r['编号'], r['事件名称'], r['级别'], r['影响时长'], r['原因摘要'],
                    r['过程摘要'], r['结果摘要']] for r in tail])
  causes = imp.get('causes') or {}
  ranked = [(k, v) for k, v in causes.items() if k not in ('可归类', '未归入')]
  if ranked:
    ranked.sort(key=lambda kv: (-kv[1], kv[0]))
    out += '<p class="sum-head">成因归类（按单首命中）</p>'
    out += _table(['成因类别', '单数'], [[k, v] for k, v in ranked])
    out += (f'<p class="note">按详情原文归类，每单只计 1 次：可归类 {causes.get("可归类", 0)} 单，'
            f'未归入 {causes.get("未归入", 0)} 单。</p>')
  return out


def _r_change_type(ctx: _Ctx, no: int) -> str:
  """变更单专属：类型分布（计划/非计划/紧急）+ 类型×等级 + 各园区紧急占比。"""
  ct = ctx.stats.get('change_type') or {}
  out = ctx.charts(no)
  rows = [[r['label'], r['count'], r.get('prev', 0), f'{r["pct"]}%']
          for r in ct.get('rows') or []]
  if rows:
    out += _table(['变更类型', '本期', '上一期', '占比'], rows)
  bl = ct.get('by_level') or []
  if bl:
    out += '<p class="sum-head">类型 × 变更等级</p>'
    out += _table(['类型', '单数', '低风险', '中风险', '高风险', '高风险占比'],
                  [[r['类型'], r['单数'], r.get('低风险', 0), r.get('中风险', 0),
                    r.get('高风险', 0),
                    f'{r["高风险占比"]}%' if r.get('高风险占比') is not None else '—']
                   for r in bl])
  bp = ct.get('by_park') or []
  if bp:
    out += '<p class="sum-head">各园区紧急变更占比</p>'
    out += _table(['园区', '总单数', '紧急单数', '紧急占比'],
                  [[r['园区'], r['总单数'], r['紧急单数'],
                    f'{r["紧急占比"]}%' if r.get('紧急占比') is not None else '—']
                   for r in bp])
  return out + ctx.end(no)


def _r_flow(ctx: _Ctx, no: int) -> str:
  """变更单专属：阶段/状态快照 + 超时 + 异常/未关闭积压（单园区另有各环节耗时）。"""
  fl = ctx.stats.get('flow') or {}
  out = ctx.charts(no)
  # 阶段与状态是同一批单的两个视角（数字一一对应），合并成一张联合表，不再印两遍
  ss = [[r['阶段'], r['状态'], r['单数'], f'{r["占比"]}%'] for r in fl.get('stage_status') or []]
  if ss:
    out += '<p class="sum-head">阶段与状态（统计时点快照）</p>'
    out += _table(['阶段', '状态', '单数', '占比'], ss)
    out += ('<p class="note">阶段与状态都是当前时点快照、不是流转史；'
            '「延期中」等状态与阶段正交，故按两列联合呈现。</p>')

  # 「审批与验收」表已删：P50/P90/P95 在「各环节耗时」里同口径重复，只留最易被忽略的超时
  to = []
  for label, block in (('审批', fl.get('approve') or {}), ('验收', fl.get('accept') or {})):
    if block.get('timeout_base'):
      to.append(f'{label}超时 {block.get("timeout_n")} / {block.get("timeout_base")} 单'
                f'（{block.get("timeout_rate")}%）')
  if to:
    out += '<p class="note">' + '；'.join(to) + '。</p>'

  # 单园区专属（全国级没有这两个键 → 本章逐字不变）
  dur = fl.get('durations') or []
  if dur:
    out += '<p class="sum-head">各环节耗时</p>'
    # 前两列（环节、口径）可折行；后面是数值，不折行否则会被挤成「1.9 小/时」逐字断行
    out += _table(['环节', '口径', '可统计单数', 'P50', 'P90'],
                  [[r['环节'], r['口径'], r['可统计'], r.get('p50可读') or '—',
                    r.get('p90可读') or '—'] for r in dur], nowrap_from=2)
    out += ('<p class="note">关闭期没有时间戳，审批＋实施＋验收三段时间之和不等于「全流程」'
            '（中间的空闲与等待期未计入）；各环节口径不同，不可相加。</p>')

  # 「分等级时效（P50）」表已删：与图 13（各等级审批/实施/验收 P50）同源，9 个数一字不差
  exc = fl.get('exceptions') or []
  rates = {r['异常']: r['占比'] for r in fl.get('exception_rates') or []}
  if exc and rates:
    out += '<p class="sum-head">流程异常</p>'
    out += _table(['异常类型', '单数', '占比'],
                  [[r['异常'], r['单数'],
                    f'{rates[r["异常"]]}%' if r['异常'] in rates else '—']
                   for r in exc])
    out += '<p class="note">口径为当前状态快照（不是流转史），同一张单可能同时落在多类异常中。</p>'
  elif exc:
    out += '<p class="sum-head">流程异常</p>'
    out += _table(['异常类型', '单数'], exc)

  op = fl.get('open') or {}
  out += (f'<p>统计期末仍未关闭的变更单共 <b>{op.get("n", 0)}</b> 单'
          f'（占 {op.get("rate")}%），其中已过计划结束时间 <b>{op.get("overdue_n", 0)}</b> 单。</p>'
          if op.get('rate') is not None
          else f'<p>统计期末仍未关闭的变更单共 <b>{op.get("n", 0)}</b> 单。</p>')
  if op.get('rows'):
    # 单园区报告的 `companyName` 为空（只有全国聚合才补）→ 不出空的园区列
    has_park = any(r.get('园区') for r in op['rows'])
    head = ['工单编号'] + (['园区'] if has_park else []) + ['项目名称', '当前阶段', '距计划结束']
    body = []
    for r in op['rows']:
      days = r.get('超期天数')
      if days is None:
        age = '—'
      elif days > 0:
        age = f'已超期 {_trim0(days)} 天'
      else:
        # days 恰为 0.0 时 `{-days:g}` 会印成「-0」（计划结束日 = 统计期末）；交给 `_trim0` 归一
        age = f'未到期（还剩 {_trim0(-days)} 天）'
      body.append(([r.get('ticketNo', '')] + ([r.get('园区', '')] if has_park else [])
                   + [r.get('项目名称', ''), r.get('阶段', ''), age]))
    out += _table(head, body)
  # 「审批最慢部门」表已删：第 5 章部门表已有各部的审批 P50，重复且易被读成两组不同口径
  return out + ctx.end(no)


def _r_schedule(ctx: _Ctx, no: int) -> str:
  """变更单专属（本报告重点）：计划 vs 实际时长、按时率、偏差分档、按类型/园区、最严重单。"""
  sc = ctx.stats.get('schedule') or {}
  out = ctx.charts(no)
  rows = []
  for label, key in (('计划时长', 'planned'), ('实际时长', 'actual')):
    d = sc.get(key) or {}
    rd = d.get('readable') or {}
    rows.append([label, rd.get('p50', '—'), rd.get('p90', '—'), rd.get('p95', '—'),
                 rd.get('max', '—')])
  out += _table(['时长口径', 'P50', 'P90', 'P95', '最长'], rows)

  sd = sc.get('start_delay') or {}
  out += ('<p>实际开始时间较计划开始时间：延迟 <b>{}</b> 单（占 {}%），'
          '延迟中位 {}、P90 {}。</p>').format(
    sd.get('late_n', 0), sd.get('late_rate') if sd.get('late_rate') is not None else '—',
    _human_minutes(sd.get('p50')), _human_minutes(sd.get('p90')))

  if sc.get('on_time_base'):
    out += (f'<p>按时完成 <b>{sc.get("on_time_n")}</b> / '
            f'<b>{sc.get("on_time_base")}</b> 单，按时率 '
            f'<b>{sc.get("on_time_rate")}%</b>；延迟 <b>{sc.get("late_n")}</b> 单'
            f'（{sc.get("late_rate")}%），提前完成 {sc.get("early_n")} 单。</p>')
  else:
    out += ('<div class="callout">本周期计划结束或实施结束时间未回填，'
            '没有可计算按时完成率的变更单。</div>')

  hist = [[r['档位'], r['单数']] for r in sc.get('delay_hist') or []]
  if hist:
    out += '<p class="sum-head">完成时间偏差分档（实际结束 − 计划结束）</p>'
    out += _table(['档位', '单数'], hist)

  bt = sc.get('by_type') or []
  if bt:
    out += '<p class="sum-head">按变更类型的按时率</p>'
    out += _table(['变更类型', '参与统计', '按时单数', '按时率'],
                  [[r['类型'], r['参与统计'], r['按时单数'],
                    '—' if r.get('按时率') is None else f'{r["按时率"]}%'] for r in bt])
  bp = sc.get('by_park') or []
  if bp:
    out += '<p class="sum-head">各园区按时率</p>'
    out += _table(['园区', '参与统计', '按时单数', '按时率'],
                  [[r['园区'], r['参与统计'], r['按时单数'], f'{r["按时率"]}%']
                   for r in bp])

  worst = sc.get('worst') or []
  if worst:
    out += '<p class="sum-head">延期最严重的变更单</p>'
    has_park = any(r.get('园区') for r in worst)     # 单园区报告不出空园区列
    out += _table(['工单编号'] + (['园区'] if has_park else []) + ['项目名称', '延期时长'],
                  [[r.get('ticketNo', '')] + ([r.get('园区', '')] if has_park else [])
                   + [r.get('项目名称', ''), r.get('延迟可读', '—')] for r in worst])
  return out + ctx.end(no)


# ==================== 问题工单专属渲染 ====================

def _r_level_stage(ctx: _Ctx, no: int) -> str:
  """问题单专属：级别 × 当前处理阶段（矩阵表 + 横向堆叠图）。"""
  ls = ctx.stats.get('level_stage') or {}
  stages = ls.get('stages') or []
  rows = ls.get('rows') or []
  out = ctx.charts(no)
  if rows and stages:
    out += _table(['问题级别'] + stages + ['合计'],
                  [[r['级别']] + [r.get(s, 0) for s in stages] + [r['合计']]
                   for r in rows])
  else:
    out += '<p class="note">本周期没有问题单，无级别与阶段分布。</p>'
  return out + ctx.end(no)


def _r_aging(ctx: _Ctx, no: int) -> str:
  """问题单专属：存量老化（仅未关闭）——分档、分位、园区结构与最老单。"""
  ag = ctx.stats.get('aging') or {}
  out = ctx.charts(no)
  if not ag.get('open_n'):
    out += '<p class="note">统计期末没有问题仍处于未关闭状态，无存量老化数据。</p>'
    return out + ctx.end(no)
  rd = ag.get('readable') or {}
  # 周报老化档位只到 7 天：超期口径由 compute 声明（3 天），月报仍是 30 天
  over_label = ag.get('over_label') or '超 30 天未关闭'
  over_n = ag.get('over_n', ag.get('over_30_n', 0))
  out += _table(['指标', '数值'], [
    ['期末未关闭', f'{ag["open_n"]} 单（占 {ag["rate"]}%）'],
    [over_label, f'{over_n} 单'],
    ['滞留时长 P50', rd.get('p50', '—')],
    ['滞留时长 P90', rd.get('p90', '—')],
    ['平均滞留', rd.get('mean', '—')],
    ['最长滞留', rd.get('max', '—')],
  ])
  bins = ag.get('bins') or []
  counts = ag.get('counts') or []
  if bins:
    out += '<p class="sum-head">未关闭问题滞留时长分档</p>'
    out += _table(['滞留档位', '问题单量'],
                  [[lab, n] for lab, n in zip(bins, counts)])
  old = ag.get('rows') or []
  if old:
    has_space = any(r.get('空间') for r in old)
    head = ['工单编号'] + (['空间'] if has_space else []) + ['问题名称', '当前阶段', '滞留天数']
    body = []
    for r in old[:5]:
      days = r.get('滞留天数')
      body.append([r.get('ticketNo', '')] + ([r.get('空间', '')] if has_space else [])
                  + [r.get('问题', ''), r.get('阶段', ''),
                     '—' if days is None else f'{_trim0(days)} 天'])
    out += '<p class="sum-head">滞留最久的未关闭问题</p>'
    out += _table(head, body)
  return out + ctx.end(no)


def _r_park_specialty(ctx: _Ctx, no: int) -> str:
  """问题单专属：园区 + 专业分布（单园区时不出空的园区表）。"""
  out = ctx.charts(no)
  park = ctx.stats.get('park') or []
  if park:
    rows = []
    for p in park:
      delta = p['count'] - p['prev']
      pct = ('持平' if not delta else
             _pct_text(delta / p['prev'] * 100, signed=True) if p['prev'] else '—')
      rows.append([p['name'], p['count'], p['prev'], _delta_text(delta), pct, f'{p["pct"]}%'])
    out += _table(['园区', '本期', '上一期', '环比', '环比幅度', '占比'], rows)
  else:
    out += '<p class="note">本次统计范围为单个园区，无跨园区对比数据。</p>'
  sp = ctx.stats.get('specialty') or []
  if sp:
    out += '<p class="sum-head">各专业问题单量</p>'
    out += _specialty_table(ctx)
  return out + ctx.end(no)


def _r_problem_objects(ctx: _Ctx, no: int) -> str:
  """问题单专属：涉及设备/系统 Top10（自由文本切分，非结构化字段）。"""
  ob = ctx.stats.get('problem_objects') or {}
  rows = ob.get('rows') or []
  out = ctx.charts(no)
  if rows:
    out += _table(['涉及设备/系统', '涉及问题单量'],
                  [[r['name'], r['count']] for r in rows])
    out += (f'<p class="note">共识别 {ob.get("classes", 0)} 个对象；'
            f'{ob.get("covered_n", 0)} 单填写了该字段，'
            f'{ob.get("blank_n", 0)} 单未填写。同一张单的多个对象分别计数，'
            f'合计可大于问题单量。</p>')
  else:
    out += '<p class="note">本周期问题单均未填写「涉及设备/系统」，无该维度数据。</p>'
  return out + ctx.end(no)


def _r_entry_lag(ctx: _Ctx, no: int) -> str:
  """问题单专属：录入及时性（建单 − 发生），负值与缺失单独计数。"""
  el = ctx.stats.get('entry_lag') or {}
  out = ctx.charts(no)
  rd = el.get('readable') or {}
  out += _table(['指标', '数值'], [
    ['可统计录入滞后的单量', f'{el.get("valid", 0)} 单'],
    ['负滞后（补录/脏数据）', f'{el.get("invalid_n", 0)} 单（已剔除）'],
    ['时间缺失', f'{el.get("missing_n", 0)} 单'],
    ['录入滞后 P50', rd.get('p50', '—')],
    ['录入滞后 P90', rd.get('p90', '—')],
    ['平均录入滞后', rd.get('mean', '—')],
    ['最长录入滞后', rd.get('max', '—')],
  ])
  bins = el.get('bins') or []
  counts = el.get('counts') or []
  if bins:
    out += '<p class="sum-head">录入滞后分档</p>'
    out += _table(['滞后档位', '问题单量'], [[lab, n] for lab, n in zip(bins, counts)])
  return out + ctx.end(no)


# ==================== 阈值工单专属渲染 ====================

def _r_threshold_intent(ctx: _Ctx, no: int) -> str:
  """阈值单专属：调整意图（固定关键词归类，未识别单独陈列）。"""
  it = ctx.stats.get('intent') or {}
  rows = it.get('rows') or []
  out = ctx.charts(no)
  if rows:
    out += _table(['调整意图', '本期', '上一期', '占比'],
                  [[r['label'], r['count'], r.get('prev', 0), f'{r["pct"]}%']
                   for r in rows])
  ip = ctx.stats.get('intent_park') or {}
  if ip.get('intents') and ip.get('parks'):
    out += '<p class="sum-head">调整意图 × 园区</p>'
    out += _table(['调整意图'] + list(ip['parks']),
                  [[label] + list(matrix_row)
                   for label, matrix_row in zip(ip['intents'], ip['matrix'])])
  if it.get('unrecognized_n'):
    out += (f'<p class="note">另有 {it["unrecognized_n"]} 单的问题名称无法按固定关键词'
            f'归类，已记入「未识别」，报告不做推断。</p>')
  return out + ctx.end(no)


def _r_threshold_scope(ctx: _Ctx, no: int) -> str:
  """阈值单专属：专业分布 + 发起人集中度。"""
  out = ctx.charts(no)
  sp = ctx.stats.get('specialty') or []
  if sp:
    out += _specialty_table(ctx)
  ini = ctx.stats.get('initiator') or {}
  top = ini.get('top') or []
  if top:
    out += '<p class="sum-head">发起人 Top10</p>'
    out += _table(['发起人', '单量', '占比'],
                  [[r['name'], r['count'], f'{r["pct"]}%'] for r in top])
    out += (f'<p class="note">共 {ini.get("classes", 0)} 位发起人，首位占比 '
            f'{ini.get("top_share", 0)}%'
            + (f'；{ini["unknown_n"]} 单未能解析发起人姓名，记入「未标注」。'
               if ini.get('unknown_n') else '。') + '</p>')
  ii = ctx.stats.get('initiator_intent') or {}
  if ii.get('initiators') and ii.get('intents'):
    out += '<p class="sum-head">主要发起人 × 调整意图（Top5）</p>'
    out += _table(['发起人'] + list(ii['intents']),
                  [[name] + list(matrix_row)
                   for name, matrix_row in zip(ii['initiators'], ii['matrix'])])
  return out + ctx.end(no)


def _r_hot_items(ctx: _Ctx, no: int) -> str:
  """阈值单专属：热点调整对象 + 批量规模（口径＝名称并列对象片段数）。"""
  hot = ctx.stats.get('hot_items') or {}
  bt = ctx.stats.get('batch') or {}
  out = ctx.charts(no)
  rows = hot.get('rows') or []
  if rows:
    out += _table(['调整对象（问题名称）', '单量', '占比'],
                  [[r['name'], r['count'], f'{r["pct"]}%'] for r in rows])
    out += (f'<p class="note">共 {hot.get("classes", 0)} 个不同的调整对象名称；'
            f'{hot.get("blank_n", 0)} 单名称为空已剔除。</p>')
  out += '<p class="sum-head">批量规模（名称片段数）</p>'
  out += _table(['指标', '数值'], [
    ['可统计的单量', f'{bt.get("valid", 0)} 单'],
    ['片段数 P50', str(bt.get('p50') if bt.get('p50') is not None else '—')],
    ['最大片段数', str(bt.get('max', 0))],
  ])
  hist = bt.get('hist') or {}
  if hist.get('labels'):
    out += _table(['片段数档位', '阈值单量'],
                  [[lab, n] for lab, n in zip(hist['labels'], hist['counts'])])
  out += ('<p class="note">该口径是问题名称里按标点并列的对象片段数，'
          '用于区分"逐项微调"与"整批调整"，不是设备精确台数。</p>')
  return out + ctx.end(no)


def _r_lead_time(ctx: _Ctx, no: int) -> str:
  """阈值单专属：计划提前期（计划开始 − 创建）。"""
  ld = ctx.stats.get('lead') or {}
  rd = ld.get('readable') or {}
  out = ctx.charts(no)
  out += _table(['指标', '数值'], [
    ['可统计提前期的单量', f'{ld.get("valid", 0)} 单'],
    ['负提前期（补录/脏数据）', f'{ld.get("invalid_n", 0)} 单（已剔除）'],
    ['时间缺失', f'{ld.get("missing_n", 0)} 单'],
    ['提前期 P50', rd.get('p50', '—')],
    ['提前期 P90', rd.get('p90', '—')],
    ['平均提前期', rd.get('mean', '—')],
    ['最长提前期', rd.get('max', '—')],
  ])
  hist = ld.get('hist') or {}
  if hist.get('labels'):
    out += '<p class="sum-head">计划提前期分档</p>'
    out += _table(['提前期档位', '阈值单量'],
                  [[lab, n] for lab, n in zip(hist['labels'], hist['counts'])])
  return out + ctx.end(no)


def _r_execution(ctx: _Ctx, no: int) -> str:
  """阈值单专属：执行达成（按时率、开始偏差、计划/实际时长、按专业）。"""
  ex = ctx.stats.get('execution') or {}
  out = ctx.charts(no)
  rows = []
  for label, key in (('计划实施时长', 'planned'), ('实际实施时长', 'actual')):
    d = ex.get(key) or {}
    rd = d.get('readable') or {}
    rows.append([label, rd.get('p50', '—'), rd.get('p90', '—'), rd.get('max', '—')])
  out += _table(['时长口径', 'P50', 'P90', '最长'], rows)

  if ex.get('on_time_base'):
    out += (f'<p>按时执行 <b>{ex.get("on_time_n")}</b> / '
            f'<b>{ex.get("on_time_base")}</b> 单，按时率 '
            f'<b>{ex.get("on_time_rate")}%</b>（实施结束不晚于计划结束）。</p>')
  else:
    out += ('<div class="callout">本周期计划结束或实施结束时间未回填，'
            '没有可计算按时执行率的阈值单。</div>')

  sd = ex.get('start_delay') or {}
  if sd.get('late_rate') is not None:
    out += (f'<p>实际开始晚于计划开始 <b>{sd.get("late_n")}</b> 单'
            f'（占 {sd.get("late_rate")}%），延迟中位 {_human_minutes(sd.get("p50"))}、'
            f'P90 {_human_minutes(sd.get("p90"))}。</p>')
  hist = sd.get('hist') or []
  if hist:
    out += '<p class="sum-head">实际开始相对计划开始的偏差分档</p>'
    out += _table(['偏差档位', '阈值单量'], [[r['档位'], r['单数']] for r in hist])

  bs = ex.get('by_specialty') or []
  if bs:
    out += '<p class="sum-head">各专业按时执行率</p>'
    out += _table(['专业', '参与统计', '按时单数', '按时率'],
                  [[r['专业'], r['参与统计'], r['按时单数'],
                    '—' if r.get('按时率') is None else f'{r["按时率"]}%'] for r in bs])
  return out + ctx.end(no)


def _r_threshold_flow(ctx: _Ctx, no: int) -> str:
  """阈值单专属：当前阶段/状态快照 + 未关闭积压（超期只计正天数）。"""
  fl = ctx.stats.get('threshold_flow') or {}
  out = ctx.charts(no)
  stage = [[r['label'], r['count'], f'{r["pct"]}%'] for r in fl.get('stage') or []]
  status = [[r['label'], r['count'], f'{r["pct"]}%'] for r in fl.get('status') or []]
  if stage:
    out += '<p class="sum-head">阶段分布（统计时点快照）</p>'
    out += _table(['阶段', '单数', '占比'], stage)
  if status:
    out += '<p class="sum-head">状态分布（统计时点快照）</p>'
    out += _table(['状态', '单数', '占比'], status)

  op = fl.get('open') or {}
  out += (f'<p>统计期末仍未关闭的阈值单共 <b>{op.get("n", 0)}</b> 单'
          f'（占 {op.get("rate")}%），其中已过计划结束时间 <b>{op.get("overdue_n", 0)}</b> 单；'
          f'另有驳回 <b>{fl.get("rejected_n", 0)}</b> 单、审批中 '
          f'<b>{fl.get("pending_approval_n", 0)}</b> 单。</p>')
  if op.get('rows'):
    has_space = any(r.get('空间') for r in op['rows'])   # 单园区报告不出空园区列
    head = ['工单编号'] + (['空间'] if has_space else []) + ['项目名称', '当前阶段', '距计划结束']
    body = []
    for r in op['rows']:
      days = r.get('超期天数')
      if days is None:
        age = '—'
      elif days > 0:
        age = f'已超期 {_trim0(days)} 天'
      else:
        age = f'未到期（还剩 {_trim0(-days)} 天）'
      body.append([r.get('ticketNo', '')] + ([r.get('空间', '')] if has_space else [])
                  + [r.get('项目名称', ''), r.get('阶段', ''), age])
    out += _table(head, body)
  return out + ctx.end(no)


def _r_action(ctx: _Ctx, no: int) -> str:
  """「综合评价与改进方向」：**先说积极的** —— 第一步印平台确定性生成的「本期整体表现」
  （`highlights` 的逐句 `sentences`，无 LLM 也照出），再印总体评价，最后按**优先级分档**
  渲染改进项（P0 立即执行／P1 本月完成／P2 下季度完成），每档编号列表；P0 整块加橙黄
  标记（左侧色条 + 极淡底），让"马上要做的"一眼跳出来。
  改进项可以是 0 条 —— **没有改进项时正面段与总体评价仍要印**。

  正面段与总体评价是**正文结论**，用正常字号（此前误用 `.note` 9pt 灰字，用户 2026-10-29
  反馈「综合评价那部分为什么是小字」）；正面段整块加淡绿底 + 左侧绿条，与下方橙黄的改进项
  形成「成绩 / 待改进」对照，逐句一个 `<p>`。
  总体评价也在这块绿底里，与数值句连排、块内不分隔（用户 2026-10-30）；两者都空时不产绿块。"""
  highlights = (ctx.details or {}).get('highlights') or {}
  sentences = list(highlights.get('sentences') or [])
  if not sentences and highlights.get('summary'):     # 兼容只给 summary 的调用方
    sentences = [s.strip() for s in str(highlights['summary']).split('。') if s.strip()]
  overall = (ctx.narr or {}).get('总体评价') or ''
  body = ''.join(f'<p>{_md(s)}。</p>' for s in sentences)
  if overall:                                         # 与数值句同一块绿底（用户 2026-10-30）
    body += f'<p>{_md(overall)}</p>'
  lead = (f'<p class="panel-head">本期整体表现</p>'
          f'<div class="sum-panel">{body}</div>' if body else '')
  actions = (ctx.narr or {}).get('行动计划') or []
  if not actions:
    return lead + '<p class="note">本期各维度未发现需要专项干预的项。</p>'
  blocks = []
  # 周报时限按周口径（与 narrate 的提示词保持一致），月报文案不变
  timeboxes = (('P0', '立即执行'), ('P1', '本周完成'), ('P2', '下周完成')) \
    if is_weekly(ctx.stats) else \
    (('P0', '立即执行'), ('P1', '本月完成'), ('P2', '下季度完成'))
  for pri, label in timeboxes:
    items = [a for a in actions if a.get('优先级') == pri]
    if not items:
      continue
    lis = ''.join(
      f'<li>{_md(a.get("行动项", ""))}'
      + (f'<span class="act-meta">{_e(a.get("责任维度", ""))}'
         + (f'　·　{_e(a.get("时限", ""))}' if a.get('时限') else '')
         + '</span>' if a.get('责任维度') or a.get('时限') else '')
      + '</li>' for a in items)
    blocks.append(f'<div class="act-group pri-{pri.lower()}">'
                  f'<p class="act-head">{pri}｜{label}</p>'
                  f'<ol>{lis}</ol></div>')
  return lead + ''.join(blocks)


def _r_risk_analysis(ctx: _Ctx, no: int) -> str:
  """「质量与风险分析」：确定性分级表（**无 LLM**），给改进项一个数据依据。

  2026-10-29 起质量指标不再单独成章（它与风险分析是同一组指标的两份视图，用户明确指出
  重复）→ 本章一张表同时给「指标 / 本期表现 / 口径 / 等级 / 判据 / 建议排查方向」，
  质量行**恒列**（未达阈值等级记「—」，指标值不隐藏）。故障单的「现存风险清单」原本在
  另一章，现作为本章第二张子表。数据全空才只留一句说明。
  """
  risk = (ctx.details or {}).get('risk_analysis') or {}
  rows = risk.get('rows') or []
  if not rows:
    return ('<p class="note">本期各项指标均未达到中风险阈值，'
            '未发现需要专项排查的风险项。</p>')
  out = ('<p class="note"><b>本期风险等级：'
         f'{_e(risk.get("overall") or "—")}</b>。等级由平台按表中判据确定性计算'
         '（取自本期统计；未达中风险阈值的项等级记「—」，指标值照常给出）；'
         f'每项给出建议排查方向，第 {no + 1} 章的改进项据此展开。</p>')
  head = ''.join(f'<th>{_e(h)}</th>'
                 for h in ('指标', '本期表现', '口径', '等级', '判据', '建议排查方向'))
  body = ''.join(
    '<tr>'
    + ''.join(f'<td>{_e(c)}</td>' for c in (r['风险项'], r['本期表现'],
                                            r.get('口径') or '—'))
    + ('<td><b>高</b></td>' if r['等级'] == '高' else f'<td>{_e(r["等级"])}</td>')
    + ''.join(f'<td>{_e(c)}</td>' for c in (r['判据'], r['建议排查方向']))
    + '</tr>' for r in rows)
  out += f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
  rrows = []
  for r in (ctx.stats.get('risk') or {}).get('rows') or []:
    reps = '、'.join(r.get('代表工单') or []) or '—'
    rrows.append([r['风险'], r['单数'], r['口径'], reps])
  if rrows:
    out += '<p class="sum-head">现存风险清单（代表工单）</p>'
    out += _table(['风险类型', '单数', '口径', '代表工单'], rrows)
  out += '<p class="note">数据里没有误报/自愈/确认标记，故不提供误报率、自愈率等指标。</p>'
  return out + ctx.end(no)


def _r_notes(ctx: _Ctx, no: int) -> str:
  spec = ctx.spec
  win = ctx.stats.get('window') or {}
  cur_win = ' ~ '.join(win.get('cur') or [])
  prev_win = ' ~ '.join(win.get('prev') or [])
  scope = win.get('scope') or ''
  out = _table(['项目', '值'], [
    ['数据来源', spec.data_source],
    ['统计范围', scope or '—'],
    ['本期窗口', cur_win],
    ['环比基准窗口', prev_win],
    ['参与统计的记录数', f'{ctx.stats["total"]} 条'],
  ])
  sf = ctx.stats.get('scope_filter')
  if sf:
    # 问题单缺的是"问题发生时间"，变更/阈值缺的是"计划开始时间"——由 compute 声明
    missing_label = sf.get('missing_label') or '缺计划开始时间'
    fallback_n = sf.get('fallback_n') or 0
    out += (f'<p class="note">按{_e(sf.get("basis"))}归属统计期：接口返回 {sf.get("raw")} 条，'
            f'保留 {sf.get("kept")} 条；剔除统计期外 {sf.get("out_of_window")} 条、'
            f'{_e(missing_label)} {sf.get("missing_start")} 条'
            + (f'；其中 {fallback_n} 条用创建时间补位' if fallback_n > 0 else '')
            + '。</p>')
  out += '<div class="callout"><p><b>口径说明</b></p><ul>' + ''.join(
    f'<li>{_md(n)}</li>'
    for n in (list(spec.data_notes) + list(ctx.stats.get('notes_extra') or []))) + '</ul></div>'
  # 月度专属术语（节奏图 monthly_only）：周报里没有对应内容，不列，保持周报输出不变
  monthly = (ctx.stats.get('window') or {}).get('period') == 'monthly'
  terms = [(t, TERM_GLOSSARY[t]) for t in spec.glossary
           if t in TERM_GLOSSARY and (monthly or t not in _MONTHLY_ONLY_TERMS)]
  if terms:
    out += '<div class="callout"><p><b>术语说明</b></p><ul>' + ''.join(
      f'<li><b>{_e(t)}</b>：{_e(d)}</li>' for t, d in terms) + '</ul></div>'
  out += '<div class="callout"><p><b>本报告不提供的项</b></p><ul>' + ''.join(
    f'<li>{_md(v)}</li>' for v in spec.unavailable) + '</ul></div>'
  return out


_SECTION_RENDERERS: dict[str, object] = {
  'summary': _r_summary, 'kpi': _r_kpi, 'trend': _r_trend, 'level': _r_level,
  'park': _r_park, 'specialty': _r_specialty, 'equipment_specialty': _r_equipment_specialty,
  'equipment': _r_equipment, 'dept': _r_dept,
  'duration': _r_duration, 'repeat': _r_repeat,
  'root_cause': _r_root_cause, 'change_type': _r_change_type, 'flow': _r_flow,
  'schedule': _r_schedule,
  # 变更/问题/阈值根因与事件/故障同构：同一份「详情列」契约，直接复用根因渲染器
  'change_cause': _r_root_cause, 'question_cause': _r_root_cause,
  'threshold_cause': _r_root_cause,
  # 问题工单专属
  'level_stage': _r_level_stage, 'aging': _r_aging,
  'park_specialty': _r_park_specialty, 'problem_objects': _r_problem_objects,
  'entry_lag': _r_entry_lag,
  # 阈值工单专属
  'threshold_intent': _r_threshold_intent, 'threshold_scope': _r_threshold_scope,
  'hot_items': _r_hot_items, 'lead_time': _r_lead_time, 'execution': _r_execution,
  'threshold_flow': _r_threshold_flow,
  # 质量与风险分析（确定性分级表）：质量行恒列 + 补充风险项，故障另带现存风险清单
  'risk_analysis': _r_risk_analysis,
  'action': _r_action, 'notes': _r_notes,
}


def build_html(stats: dict, details: dict, narr: dict, spec, existing: set[str],
               generated_at: str = '', scope_label: str = '', period_label: str = '') -> str:
  """把统计结果 + 详情 + 解读填进固定骨架，返回完整 HTML 字符串。

  **按 `spec.roles` 逐章取渲染器**（角色而非章号）——加一类报告只需在 spec 声明，这里不动。
  """
  win = stats.get('window') or {}
  cur_win = ' ~ '.join(win.get('cur') or [])
  prev_win = ' ~ '.join(win.get('prev') or [])
  # 图号只给**本次真会出现的图**编号：周报跳过 `monthly_only` 的节奏图，若不剔除，
  # 其后所有图的编号会整体 +1（周报输出就会变）。月报照常给它编号。
  shown = [c for c in spec.charts
           if not (getattr(c, 'monthly_only', False) and win.get('period') != 'monthly')]
  fig_no = {c.key: i for i, c in enumerate(shown, 1)}
  ctx = _Ctx(stats, details, narr, spec, existing, fig_no)

  subtitle = ' · '.join(x for x in (scope_label, period_label) if x) or spec.title
  body = ['<div class="cover">',
          f'<h1>{_e(spec.title)}</h1>',
          # 副标题写**人话**：空间 · 周期（原来印的是内部 id「event口径」——
          # `spec.ticket` 是代码里的键，不该出现在给管理层看的报告上）
          f'<p class="subtitle">{_e(subtitle)}</p>',
          f'<p class="meta"><span class="gen">{_e(generated_at)}</span>'
          f'　｜　报告周期：{_e(cur_win)}　｜　'
          f'环比基准：{_e(prev_win)}　｜　共 {stats["total"]} 条记录</p>',
          '</div>']
  # 目录（封面同页，目录后强制分页）：条目即 `spec.sections` —— 加章不用改这里。
  # 章节用 `id="sec-N"` 做锚点；PDF 右侧页码由 CSS 的 `target-counter` 补，
  # 侧边栏书签由 `h2 { bookmark-level }` 生成。
  # 每章包一个白色圆角卡片（对齐参考件：章节是卡片、章间留白）。
  # **必须先渲染章节、再拼目录**：附录（周报逐单详情）是章节渲染器写进 `ctx.appendix` 的，
  # 目录据此多一条无编号条目。无附录时输出与旧版逐字节一致。
  chapters = []
  for i, title in enumerate(spec.sections):
    role = spec.roles[i] if i < len(spec.roles) else ''
    render = _SECTION_RENDERERS.get(role)
    inner = render(ctx, i + 1) if callable(render) else ''
    chapters.append(f'<div class="section" id="sec-{i + 1}">'
                    f'<h2>{i + 1}. {_e(title)}</h2>{inner}</div>')
  toc = [f'<li><a href="#sec-{i + 1}">{i + 1}. {_e(title)}</a></li>'
         for i, title in enumerate(spec.sections)]
  for anchor, title, _ in ctx.appendix:
    toc.append(f'<li><a href="#{anchor}">{_e(title)}</a></li>')
  body.append('<nav class="toc"><p class="toc-title">目录</p><ul>'
              + ''.join(toc) + '</ul></nav>')
  body.extend(chapters)
  for anchor, title, html in ctx.appendix:
    body.append(f'<div class="section appendix" id="{anchor}">'
                f'<h2>{_e(title)}</h2>{html}</div>')
  return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
          f'<title>{_e(spec.title)}</title></head><body>' + ''.join(body)
          + '</body></html>')


async def run_in_sandbox(code: str, ds_dir: Path, *, label: str) -> dict:
  """在沙箱子进程里跑一段固定脚本（统计出图 / HTML→PDF 都走它）。

  为什么继续走沙箱而不是服务端进程内：WeasyPrint 依赖的 Pango/Cairo 与中文字体
  只在子进程被验证过（`extra_lib_path` + `RZ_CJK_FONT`），服务端进程还要自己复刻
  字体注册与本地资源 url_fetcher，漏一处就**静默丢图**。
  """
  result = await run_python(
    code,
    cwd=str(ds_dir),
    timeout=_REPORT_TIMEOUT,
    max_memory_mb=max(settings.AGENT_ANALYSIS_MAX_MEMORY_MB, 1024),
    max_artifact_mb=settings.AGENT_ANALYSIS_MAX_ARTIFACT_MB,
    max_artifacts=settings.AGENT_ANALYSIS_MAX_ARTIFACTS,
    cjk_font_path=settings.RZ_CJK_FONT,
    extra_lib_path=settings.AGENT_ANALYSIS_EXTRA_LIB_PATH or None,
  )
  if result.get('timed_out'):
    raise RuntimeError(f'{label} 超时（{_REPORT_TIMEOUT}s）')
  if result.get('error'):
    raise RuntimeError(f'{label} 失败：{result["error"]}｜stderr：'
                       f'{(result.get("stderr") or "").strip()[-800:]}')
  return result


async def run_compute(ds_dir: Path, compute_file: Path) -> dict:
  """跑固定统计脚本（源码注入沙箱）；返回 stats.json 的内容。"""
  # 注入**两段源码**：共用的 common.py + 该类型的 compute.py。沙箱是 `python -I`，
  # 不能 import 项目模块，所以只能拼接；两段在同一命名空间，compute 里那句
  # `try: from ...common import …` 在沙箱里失败即忽略（名字已由这里提供）。
  common_file = compute_file.with_name('common.py')
  code = ((common_file.read_text(encoding='utf-8') if common_file.is_file() else '')
          + '\n' + compute_file.read_text(encoding='utf-8')
          + '\nmain(rz_setup(), rz_save, RZ_HEAT)\n')
  await run_in_sandbox(code, ds_dir, label='统计出图')
  stats_path = ds_dir / 'stats.json'
  if not stats_path.is_file():
    raise RuntimeError('统计脚本未产出 stats.json')
  return json.loads(stats_path.read_text(encoding='utf-8'))


async def draw_level_duration(ds_dir: Path, by_level: dict) -> None:
  """用**影响时长**矩阵覆盖第 4 章的 `02b` 图（文件名与图号都不变）。

  为什么必须二次绘制：详情在 `run_compute` 之后才取到（`workflow` 先用列表数据出统计与图），
  统计阶段根本没有影响时长可用。月报只取代表单详情 → 不调用本函数，仍是 compute 画的验收口径。
  """
  common_file = Path(__file__).with_name('common.py')
  data = json.dumps({'levels': by_level.get('levels') or [],
                     'bins': by_level.get('bins') or [],
                     'matrix': by_level.get('matrix') or []}, ensure_ascii=False)
  # `plt` 不能直接用：common.py 里只有函数参数名，模块级是 `import ast/pandas`。
  # 沙箱 wrapper 注入了 `rz_setup()`（返回 plt，且设好中文字体/Agg 后端/配色），必须调一次。
  code = ((common_file.read_text(encoding='utf-8') if common_file.is_file() else '')
          + '\nimport json as _json\n'
          + '_plt = rz_setup()\n'
          + f'_ld = _json.loads({data!r})\n'
          + "_fig, _ax = _plt.subplots(figsize=(9.4, max(2.6, 0.52 * len(_ld['levels']) + 1.6)))\n"
          # 一档一色（用户 2026-10-29 指定的 5 色，见 `common._IMPACT_LADDER`）。
          # 多余的档补末色而不是循环取色 —— 循环会让最慢一档拿到最冷色（档数=5 由自检钉住）
          + "_colors = [_IMPACT_LADDER[j] if j < len(_IMPACT_LADDER)\n"
          + "           else _IMPACT_LADDER[-1] for j in range(len(_ld['bins']))]\n"
          + "_stacked_barh(_ax, _ld['levels'], _ld['bins'], _ld['matrix'], _colors,\n"
          + "              ncol=len(_ld['bins']))\n"
          + "_emit(_fig, '02b_level_duration', _plt, rz_save)\n")
  await run_in_sandbox(code, ds_dir, label='影响时长出图')


async def render_pdf(ds_dir: Path, pdf_name: str) -> Path:
  """把 ds_dir/report.html 用沙箱内置的 render_pdf（缺省套统一 CSS）转成 PDF。"""
  code = f"render_pdf('report.html', {pdf_name!r})\nprint('pdf ready')\n"
  await run_in_sandbox(code, ds_dir, label='PDF 渲染')
  pdf_path = ds_dir / pdf_name
  if not pdf_path.is_file():
    raise RuntimeError(f'PDF 未生成：{pdf_name}')
  return pdf_path
