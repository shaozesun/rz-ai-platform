"""分析报告的**统一样式唯一来源** —— 配色、字号、图表尺寸、HTML 版式都在这里定义。

为什么需要它：报告链路是「模型在沙箱里现写 pandas/matplotlib 代码」，而全仓库原本
**没有任何 matplotlib 样式定义**（只让模型自己 addfont 解决中文字体）→ 每期报告的
配色/字号/图尺寸/标题格式都不一样，用户实测直接反馈「每次图的颜色都不统一」。

两个消费者：

1. `core/sandbox/runner.py` 用 `RZ_MPL_SRC` 把样式注入沙箱子进程（`__RZ_STYLE__` 哨兵）。
   注入的是**定义**（常量 + 函数），不 import matplotlib —— 不画图的脚本一次都不会
   付 matplotlib 的 import 代价；`rz_setup()` 内部才 import。
2. 同一个 `RZ_MPL_SRC` 里把 `render_pdf` 的缺省 CSS 设成 `RZ_CSS`，于是 HTML/PDF
   的版式也统一，**模型不传 css 也自动套上**。

注意沙箱是 `python -I` 隔离执行：忽略 PYTHONPATH / user site，**不能放独立 py 文件让沙箱
import**，只能把源码文本塞进 `_WRAPPER_TEMPLATE` 字符串。因此本模块把源码作为**字符串**
导出（`RZ_MPL_SRC`），而不是让沙箱 import 本模块。
"""

# 配色**照 DcOps《IDC机房演练质量分析报告》的实际色值**（2026-10-14 从那份 PDF 逐像素取出，
# 不是自己配的）：深蓝主色 + 绿/橙作对比序列 + 极浅蓝作底 —— 低饱和、不刺眼、无棕/紫/橄榄绿。
RZ_PRIMARY = '#134b70'     # 深蓝：标题、表头文字、KPI 数值、单序列图的统一色
RZ_ACCENT = '#2d7fb9'      # 中蓝：强调、边框
RZ_GOOD = '#2a9d6e'        # 绿：对比图的第一序列（参考报告里是"本次/园区"）
RZ_WARN = '#d98c2a'        # 橙：对比图的第二序列（参考报告里是"基准/全国"）
RZ_TINT = '#e9f3fa'        # 卡片底（极浅蓝）
RZ_TINT_HEAD = '#eaf1fa'   # 表头底（极浅蓝）

# 热力图渐变（顺序色图）：白 → 浅琥珀 → 琥珀 → 深琥珀，全部取自上面的主色/警示色。
# 用自定义而非内置的原因：内置 `Blues` 与主色同族、热点不够跳；`YlOrRd` 会引入红、
# `YlOrBr` 是棕（用户明确否过棕色）。这条既醒目又在既定色板内，且**低值退成近白** ——
# 稀疏格子安静、密集格子跳出来，这正是热力图的意义。
RZ_HEAT: list[str] = ['#ffffff', '#fdf1da', '#f3ce8e', '#d98c2a', '#b3701f']

# 「各园区 × 级别结构」横向堆叠条色板 —— 源：用户提供的 AntV G2「条形图-diverging」示例
# （蓝→浅→橙→红）。**去掉近白的 #D9F0F6**：本报告图底是 #f4f9fd，近白色段几乎不可见。
# 按**级别编码升序**取色（`common._level_palette` 等跨度取），仅表示"第几级"、**不判严重度**。
RZ_LEVEL_COLORS: list[str] = ['#3561A7', '#80B2D3', '#EC7743', '#CB2920']

# 取色顺序：单序列图用 RZ_PRIMARY；堆叠等多类别按顺序往后取
RZ_COLORS: list[str] = [
  RZ_PRIMARY,   # 深蓝（主色）
  RZ_GOOD,      # 绿
  RZ_WARN,      # 橙
  RZ_ACCENT,    # 中蓝
  '#7ba7cf',    # 浅蓝
  '#8a8f98',    # 灰（兜底）
]

# 报告 HTML 版式：标题层级 / 正文 / 表格 / KPI 指标卡片 / 图容器。
# 卡片用 inline-block 而不是 flex —— WeasyPrint 的 flex 支持有限，inline-block 一定不崩。
RZ_CSS = '''
@page { size: A4; margin: 20mm 15mm 16mm 15mm;
        @top-left { content: string(run-date); font-size: 8pt; color: #8a97a6; }
        @top-center { content: string(run-title); font-size: 8pt; color: #6b7a8d; }
        @bottom-right { content: counter(page) "/" counter(pages);
                        font-size: 8pt; color: #8a97a6; } }
body { font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
       font-size: 10.5pt; line-height: 1.75; color: #22303f; margin: 0; }

/* 封面区（不套卡片） */
.cover { margin: 0 0 12px 0; }
h1 { font-size: 19pt; color: #134b70; margin: 0 0 4px 0;
     string-set: run-title content(); bookmark-level: 1; }
.subtitle { font-size: 11pt; color: #6b7a8d; margin: 0 0 2px 0; }
.meta { font-size: 9pt; color: #8a97a6; margin-top: 2px; }
/* 页眉左上只显示生成时间：单独一个元素承载 string-set，别让整段 meta 被抽走
   （踩过：把 string-set 挂在 .meta 上 → 页眉里塞进了整段"报告周期/环比基准"文字，
   撑成 6 行并压到正文标题上） */
.gen { string-set: run-date content(); }

/* 章节 = 白色圆角卡片（对齐参考件：每章一个卡片、章间留白） */
.section { background: #ffffff; border: 1px solid #e6eef7; border-radius: 10px;
           padding: 13px 15px 15px 15px; margin: 0 0 12px 0; }
/* 每章进 PDF 侧边栏书签（标签默认取标题文字），与目录条目一一对应 */
h2 { font-size: 13pt; color: #134b70; margin: 0 0 8px 0; padding-bottom: 6px;
     border-bottom: 1px solid #eef4fa; bookmark-level: 1; }

/* 附录（周报逐单详情等长表）：单独起页；**不设 break-inside: avoid** —— 上百行必然
   跨页，表头由 WeasyPrint 自动重复。书签与目录页码沿用上面的 h2 / target-counter。 */
.appendix { page-break-before: always; break-before: page; }

/* 目录（与封面同页，之后强制分页）：条目点击跳转到对应章节；
   页码由 `target-counter` 按章节锚点自动补，点线用 `leader()` 撑开 —— 加章不用改这里。
   块宽 76% 并水平居中：满宽时点线一直拉到右页边、页码贴着页边，读起来像列表不像目录
   （用户 2026-10-28 反馈「目录可以居中一点」）。条目仍左对齐 —— `leader()` 要求左起、
   右端收在块右边界；只改这一个百分比即可调宽窄。
   上边距 32px（≈24pt）：原来的 12px 与封面 `.cover` 的下边距折叠后只剩 12px，
   「目录」几乎贴在 `.meta` 那行上（用户 2026-10-30 反馈「贴得太紧」）。 */
.toc { margin: 32px auto 0 auto; width: 76%; page-break-after: always;
       break-after: page; }
.toc-title { font-size: 15pt; font-weight: 700; color: #134b70; margin: 0 0 6px 0;
             padding-bottom: 6px; border-bottom: 1px solid #eef4fa; }
.toc ul { list-style: none; margin: 8px 0 0 0; padding: 0; }
.toc li { margin: 6px 0; font-size: 10.5pt; }
.toc a { color: #22303f; text-decoration: none; }
.toc a::after { content: leader('.') target-counter(attr(href), page); color: #8a97a6; }
h3 { font-size: 11.5pt; color: #2d7fb9; margin: 14px 0 6px 0; }
p { margin: 6px 0; }
ul, ol { margin: 6px 0 6px 18px; padding: 0; }
li { margin: 2px 0; }

/* KPI 卡：浅蓝底、无边框、大圆角、居中（对齐参考件） */
.kpi-grid { font-size: 0; margin: 4px 0 0 0; }
.kpi-card { display: inline-block; vertical-align: top; width: 23%; margin: 4px 1%;
            border-radius: 10px; padding: 10px 8px; text-align: center;
            background: #e9f3fa; box-sizing: border-box; }
.kpi-label { font-size: 8.5pt; color: #6b7a8d; }
.kpi-value { font-size: 16pt; font-weight: 700; color: #134b70; line-height: 1.25; }
.kpi-sub { font-size: 8pt; color: #8a97a6; }

/* 表格：**只有横向细线**，无竖边框（对齐参考件） */
table { width: 100%; border-collapse: collapse; font-size: 9.5pt; margin: 8px 0; }
th { background: #eaf1fa; color: #134b70; font-weight: 600; text-align: left;
     padding: 6px 8px; border: none; border-bottom: 1px solid #dbe7f3; }
td { padding: 6px 8px; border: none; border-bottom: 1px solid #e9f0f8; }
tbody tr:last-child td { border-bottom: none; }
/* 不折行的单元格（`_table(nowrap_from=…)`）：口径列很长时数值列会被挤成「1.9 小/时」 */
td.nw { white-space: nowrap; }

/* 图：标题在图**上方**、深蓝加粗居中（对齐参考件；图内不画标题）。
   标题与图**必须包在同一个不可分页的块里** —— 踩过：分成两个 div 时，分页会把标题留在上一页、
   图跑到下一页（实测图 6 就是这么断的）。 */
.figblock { page-break-inside: avoid; break-inside: avoid; text-align: center;
            margin: 12px 0 10px 0; }
/* ⚠️ 图必须限宽到容器宽（`max-width:100%`）—— 图片按原始像素(1440px≈381mm)排版会超出页宽，
   导致「标题留在本页、图被挤到下一页」，中间空出一整页（实测踩过 P9 只印了个图标题）。 */
.figblock img { max-width: 100%; height: auto; }
.figure { font-size: 10.5pt; font-weight: 700; color: #134b70;
          text-align: center; margin: 0 0 4px 0; }
.chart img { max-width: 100%; }

/* 章末「本章要点」的小标签（数据在前、说明在后） */
.sum-head { font-size: 9.5pt; color: #6b7a8d; margin: 10px 0 2px 0; }

/* 「综合评价与改进方向」章首的正面结论：正文结论不是脚注 —— 用正常字号（10.5pt）而不是
   `.note`（9pt 灰），整块淡绿底 + 左侧 3px 绿条（DcOps 的 #2a9d6e），与下方橙黄改进项
   形成「成绩 / 待改进」对照。只在本章使用，不影响别处的 `.note` 口径说明。 */
.panel-head { font-size: 10.5pt; font-weight: 700; color: #134b70; margin: 12px 0 4px 0; }
.sum-panel { background: #f4faf7; border-left: 3px solid #2a9d6e; border-radius: 4px;
             padding: 8px 12px; margin: 6px 0 12px 0; }
.sum-panel p { margin: 3px 0; font-size: 10.5pt; color: #22303f; }

/* 「综合评价与改进方向」的行动计划：按优先级分档；P0 用橙黄标记（左侧色条 + 极淡底），一眼看出轻重 */
.act-group { margin: 4px 0 12px 0; border-radius: 4px; }
.act-head { font-size: 10.5pt; font-weight: 700; margin: 0 0 4px 0; }
.act-group ol { margin: 2px 0 2px 20px; padding: 0; }
.act-group li { margin: 3px 0; }
.act-meta { font-size: 9pt; color: #6b7a8d; }
/* 三档**都用浅黄底**（用户要求），靠左侧色条与标题色的深浅区分轻重：P0 最重、P2 最轻 */
.act-group { background: #fdf7ee; padding: 7px 10px 7px 8px; border-left: 3px solid #e8d0ab; }
.pri-p0 { border-left-color: #d98c2a; } .pri-p0 .act-head { color: #b3701f; }
.pri-p1 { border-left-color: #e5b166; } .pri-p1 .act-head { color: #a97a33; }
.pri-p2 { border-left-color: #e8d0ab; } .pri-p2 .act-head { color: #8a7a5f; }

/* 图下方的说明：紧跟在图后面（每张图都有自己的 1~2 条），分点、字号略小于正文 */
.chart-notes { font-size: 9.5pt; color: #33475b; margin: 2px 0 12px 18px; }
.chart-notes li { margin: 2px 0; }

/* 说明块：浅蓝底 + 蓝色左边框（参考件的「计算明细」样式） */
.callout { background: #e9f3fa; border-left: 3px solid #2d7fb9; border-radius: 4px;
           padding: 7px 10px; font-size: 9.5pt; margin: 8px 0; }
.callout p { margin: 3px 0; }
.callout ul { margin: 3px 0 3px 16px; }
.note { font-size: 9pt; color: #6b7a8d; }
'''

# 注入沙箱的源码：常量 + rz_setup() + rz_fig() + rz_save()，并把 render_pdf 的缺省 CSS 换成 RZ_CSS。
# - 只定义不 import：不画图的脚本零代价
# - rz_setup() 里 addfont 读的是**环境变量** RZ_CJK_FONT（不是命名空间变量，见 runner.py）
# - axes.prop_cycle 是"颜色硬约束"的关键：模型不显式指定颜色就自动套统一色板
_MPL_TEMPLATE = r'''
# 3.7 统一样式（唯一来源：core/sandbox/report_style.py）
RZ_COLORS = __RZ_COLORS_LITERAL__
RZ_HEAT = __RZ_HEAT_LITERAL__
RZ_LEVEL_COLORS = __RZ_LEVEL_COLORS_LITERAL__
_RZ_CSS = __RZ_CSS_LITERAL__


def rz_setup():
    """统一图表样式：中文字体 + 字号 + 配色 + 网格 + 负号。**画图脚本开头调一次**，返回 plt。

    不指定颜色时所有图形自动取 RZ_COLORS 顺序色（axes.prop_cycle），所以同一份报告
    多张图的同一个类别总是同色。
    """
    import os as _os
    import matplotlib
    matplotlib.use('Agg')          # 无 DISPLAY 环境下必须；否则交互后端起不来
    families = ['DejaVu Sans']
    _font = _os.environ.get('RZ_CJK_FONT')
    if _font and _os.path.isfile(_font):
        from matplotlib import font_manager as _fm
        _fm.fontManager.addfont(_font)
        _name = _fm.FontProperties(fname=_font).get_name()
        families = [_name, 'Noto Sans CJK SC', 'DejaVu Sans']
    import matplotlib.pyplot as _plt
    _plt.rcParams.update({
        'font.sans-serif': families,
        'font.family': 'sans-serif',
        'axes.unicode_minus': False,      # 负号用 ASCII，否则中文黑体里会缺字
        # 9.6×4.6 太宽扁（用户 2026-10-29：柱状图"都很扁"，看着像一条横带）→ 改近 4:3
        'figure.figsize': (8.2, 5.2),
        'figure.dpi': 150,
        'savefig.dpi': 150,
        'savefig.bbox': 'tight',
        'figure.facecolor': '#f4f9fd',   # 图区自带浅蓝底（对齐参考件）
        'axes.facecolor': '#f4f9fd',
        'savefig.facecolor': '#f4f9fd',
        'font.size': 11,
        'axes.titlesize': 13.5,
        # 别设 'bold'：文泉驿没有粗体字形，matplotlib 会往 stderr 打
        # "findfont: Failed to find font weight bold, now using 400" ——
        # 沙箱里任何"Failed/失败"字样都会被模型当成环境故障（同 WeasyPrint 那段告警的坑）。
        # 标题靠字号区分即可。
        'axes.titleweight': 'normal',
        'axes.labelsize': 10.5,
        'axes.grid': True,
        'axes.grid.axis': 'y',     # **只画横向网格线**：竖向网格线会正好落在柱子上（用户反馈过）
        'axes.axisbelow': True,    # 网格沉到柱子/折线**下面**，否则横线会横穿柱体
        'axes.prop_cycle': matplotlib.cycler(color=RZ_COLORS),
        'grid.color': '#e3eaf3',
        'grid.linewidth': 0.8,
        'grid.alpha': 0.9,
        'axes.edgecolor': '#b8c2ce',
        'axes.linewidth': 0.8,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'xtick.labelsize': 10,
        'ytick.labelsize': 10,
        'legend.fontsize': 10,
        'legend.frameon': False,
    })
    return _plt


def rz_fig(w=9.6, h=4.6):
    """统一尺寸的 figure（需要自己 subplots 时用）。内部会先套 rz_setup()。"""
    _plt = rz_setup()
    return _plt.figure(figsize=(w, h))


def rz_save(fig, name):
    """把图统一存到 charts/<name>.png（统一 dpi/留白/白底），返回相对路径。"""
    from pathlib import Path as _P
    _P('charts').mkdir(exist_ok=True)
    _p = _P('charts') / (str(name) if str(name).endswith('.png') else str(name) + '.png')
    try:
        fig.tight_layout()
    except Exception:
        pass
    fig.savefig(str(_p), dpi=150, bbox_inches='tight', facecolor='white')
    return str(_p)


if 'render_pdf' in dir():
    _rz_render_pdf_orig = render_pdf

    def render_pdf(html_path, pdf_path, css=None):
        """同平台版 render_pdf，但 css 缺省套统一版式（RZ_CSS）。"""
        return _rz_render_pdf_orig(html_path, pdf_path, css if css is not None else _RZ_CSS)
'''

RZ_MPL_SRC: str = (
  _MPL_TEMPLATE
  .replace('__RZ_COLORS_LITERAL__', repr(RZ_COLORS))
  .replace('__RZ_CSS_LITERAL__', repr(RZ_CSS))
  .replace('__RZ_HEAT_LITERAL__', repr(RZ_HEAT))
  .replace('__RZ_LEVEL_COLORS_LITERAL__', repr(RZ_LEVEL_COLORS))
)
