---
name: consulting_analysis
title: 专业分析报告
description: "Use this skill when the user requests a professional research report or a consulting-grade analysis document — market/industry analysis, competitive analysis, business plans, development plans, improvement proposals, or any structured analytical deliverable. It operates in two phases: (1) generating a structured analysis framework (chapter skeleton, analysis logic, per-chapter data requirements), and (2) producing the final report with narratives, embedded charts and strategic insights. This skill is written in English on purpose — follow it literally."
when_to_use: 用户要专业调研报告或咨询级分析文档（行业分析、竞品分析、商业计划、发展规划、改进方案）时使用；例：「做一份液冷行业的市场分析报告。」
category: report
domain: report
---

# Professional Research Report Skill

## Overview

This skill produces professional, consulting-grade research reports in Markdown format, covering domains such as **market analysis, consumer insights, brand strategy, financial analysis, industry research, competitive intelligence, investment research, and macroeconomic analysis**. It operates across two distinct phases:

1. **Phase 1 — Analysis Framework Generation**: Given a research subject, produce a rigorous analysis framework including chapter skeleton, per-chapter data requirements, analysis logic, and visualization plan.
2. **Phase 2 — Report Generation**: After data has been collected by other skills, synthesize all inputs into a final polished report.

The output adheres to McKinsey/BCG consulting voice standards. The report language follows the `output_locale` setting (default: `zh_CN` for Chinese).

## Delivery Convention (rz platform)

This skill describes the *content* of the report. How the file is **delivered** follows the rz platform rules:

- Deliverables go to the session workspace: `analysis_write_file(dataset_id='workspace', path='<title>.md', content=<full markdown>)`, then output one `<file url="..." name="..."/>` line using the URL returned by that tool (copy it verbatim).
- **Default format is Markdown.** PDF / PPT / Excel / Word are produced **only when the user explicitly names that format** (see the platform's report-generation spec) — never auto-generate them.
- The chat reply carries the summary + inline citations only; do not paste the full report text into the conversation.

## Data Authenticity Protocol

**Strict Adherence Rule**: All data presented in the report and visualized in charts MUST be derived directly from the provided **Data Summary** or **External Search Findings**.
- **NO Hallucinations**: Do not invent, estimate, or simulate data. If data is missing, state "Data not available" rather than fabricating numbers.
- **Traceable Sources**: Every major claim and chart must be traceable back to the input data package.

## Core Capabilities

- **Design analysis frameworks** from scratch given only a research subject and scope
- Transform raw data into structured, high-depth research reports
- Follow the **"Visual Anchor → Data Contrast → Integrated Analysis"** flow per sub-chapter
- Produce insights following the **"Data → User Psychology → Strategy Implication"** chain
- Embed pre-generated charts and construct comparison tables
- Generate inline citations formatted per **GB/T 7714-2015** standards
- Output reports in the language specified by `output_locale` with professional consulting tone
- Adapt analytical depth and structure to domain (marketing, finance, industry, etc.)

## When to Use This Skill

**Always load this skill when:**

- User asks for a market analysis, consumer insight report, financial analysis, industry research, or any consulting-grade analytical report
- User provides a research subject and needs a structured analysis framework before data collection
- User provides data summaries, analysis frameworks, or chart files to be synthesized into a report
- User needs a professional consulting-style research report
- The task involves transforming research findings into structured strategic narratives

---

## How to read this skill (progressive disclosure)

This skill is intentionally split so that nothing exceeds the tool output budget. Read only what the current phase needs:

1. **Phase 1 — design the analysis framework** (chapter skeleton, analysis logic, per-chapter data requirements, quality checklist):
   call `skill__consulting_analysis__phase1_analysis_framework`
2. **Phase 2 — write the report** once data is collected (visual anchor → data contrast → integrated analysis, charts, citations):
   call `skill__consulting_analysis__phase2_report_generation`

The two references above are the detailed instructions; this file only carries the overview, the Data Authenticity Protocol and the delivery convention. **Do not skip Phase 1** — jumping straight to prose is what produces unsourced, generic analysis.

