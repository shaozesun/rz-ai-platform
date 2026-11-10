"""确定性报告工作流 —— 一条固定流水线 + 每类工单一份声明式规格。

为什么不再用 agent 跑报告（2026-10-14 实测数字）：报告走 agent 要付 ~29.5k tokens 的固定开销
（96 个平台工具 schema ≈19.2k + 系统提示词全文 ≈6.6k + 分析工具 schema ≈3.7k），而一份报告
实际只用得到 ~8 个工具；一次报告 ≈ 25 次主模型调用，每次都要 prefill 40~50k 上下文，
上游对长流式不稳时成片失败（当天 4 份报告 8 次尝试只成 1 次）。副作用还有：模型自己增删
章节、把「处理时效」写成"数据不可得"、把「环比」整段跳过、图配色每期不同。

现在的形态：

  1 取数   平台按 capability + 窗口拉全量（本期 + 上一期）
  2 统计   固定 pandas → stats.json                     ← 0 次 LLM
  3 出图   固定绘图 → PNG（统一色板/尺寸）                ← 0 次 LLM
  4 排版   固定 HTML 模板 + render_pdf → PDF            ← 0 次 LLM
  5 解读   一次无工具的 LLM 调用，只读 stats.json         ← 1 次调用，失败不影响 PDF

于是每期数字必然一致（可写断言）、口径可跨期比较、**即使解读失败 PDF 照样出**。

加一类报告 = 加一份 `ReportSpec` 声明 + 一份同契约的统计实现，**不是**再写一条流程。
"""

from service.scheduled_task.report.spec import ReportSpec, get_report_spec

__all__ = ['ReportSpec', 'get_report_spec']
