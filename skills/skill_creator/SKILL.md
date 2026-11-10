---
name: skill_creator
title: 创建技能
description: 引导用户通过对话创建可复用的自定义技能（技能 = 一个目录，含 SKILL.md 与可选支持文件）。用户在对话里说「创建技能/帮我写个技能/做一个 XX 技能/把这段流程做成技能」等意图时调用。流程：访谈需求（做什么/何时触发/输出格式）→ 起草 SKILL.md → 用 skill_manage 创建 → 校验反馈迭代。覆盖创建自定义技能、对话式技能生成、技能访谈。
category: skill
domain: skill
---

# 创建技能

帮用户把可复用的工作流/查询/生成流程沉淀为「技能」，之后随时一句话触发。

## 使用流程

1. **访谈三问**（每个问题用户回答后继续，不要一次性抛一堆）：
   - 这个技能用来做什么？（核心任务，要具体）
   - 什么情况下会触发它？（场景/关键短语，供 description 检索用）
   - 输出是什么格式？（如文本要点 / 表格 / 报告 / 一段固定流程）
   - 需要用到哪些支持文件吗？（如模板、脚本；不需要就跳过）
2. **起草 SKILL.md**：按下方 frontmatter 规范组织，正文写清「步骤式执行指南」，
   让加载该技能时能照着做，而不是模糊描述。
3. **调用 skill_manage 创建**：`skill_manage(action='create', name=<技能名>, content=<完整 SKILL.md>)`。
   被拒绝（重名/安全扫描/校验失败）时按错误信息修改后重试。
4. **校验反馈**：创建成功后告诉用户技能名与触发方式，请用户试用；需修改就
   用 skill_manage 的 edit/patch 调整。创建完成、或用户问「这技能靠谱吗」时，
   调 `skill__skill_reviewer` 按审计清单自查一遍，把 🔴 项改掉再说"可用了"。

## SKILL.md 规范

```markdown
---
name: <技能名>
title: <展示名>
description: <能触发检索的一句话：做什么 + 覆盖的场景与同义词；禁止尖括号>
category: skill
domain: skill
---

# <标题>

## 使用流程
（步骤式执行指南，第一步做什么、条件、输出）

## 约束
（禁止事项/边界）
```

- **name**：小写字母/数字/下划线，≤64 字符（如 `weekly_report`）；全局唯一，
  公开技能与其他用户技能不可重名。
- **description**：能否被 tool_search 命中的关键——写清技能做什么 + 覆盖的场景
  同义词（如「把巡检记录汇总成周报」），禁止 `<` `>` 尖括号，≤1024 字符。
- frontmatter 仅允许 name/title/description/category/domain/kind 六个字段。
- 技能默认只对你本人可见、只有你能调用，技能名全局唯一。

## 约束

- 创建/修改技能一律走 skill_manage 工具，禁止用沙箱写文件（analysis_write_file
  等）往 skills/ 目录落盘。
- 不要编造技能名：重名时先告诉用户已占用，建议新名字让用户确认。
- 创建失败先读错误信息自查（重名/校验/安全扫描），必要时展示给用户确认，不要反复盲试。

---

## 附：Skill Writing Guide（取自 DeerFlow skill-creator，保持英文原文）

> 本节是"怎么写得好"的规范（原样引用，未翻译）：描述要写全触发场景、正文用祈使句、输出格式用固定模板等。写 / 改技能时照此执行。

### Skill Writing Guide

#### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic/repetitive tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, icons, fonts)
```

#### Progressive Disclosure

Skills use a three-level loading system:
1. **Metadata** (name + description) - Always in context (~100 words)
2. **SKILL.md body** - In context whenever skill triggers (<500 lines ideal)
3. **Bundled resources** - As needed (unlimited, scripts can execute without loading)

These word counts are approximate and you can feel free to go longer if needed.

**Key patterns:**
- Keep SKILL.md under 500 lines; if you're approaching this limit, add an additional layer of hierarchy along with clear pointers about where the model using the skill should go next to follow up.
- Reference files clearly from SKILL.md with guidance on when to read them
- For large reference files (>300 lines), include a table of contents

**Domain organization**: When a skill supports multiple domains/frameworks, organize by variant:
```
cloud-deploy/
├── SKILL.md (workflow + selection)
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```
Claude reads only the relevant reference file.

#### Principle of Lack of Surprise

This goes without saying, but skills must not contain malware, exploit code, or any content that could compromise system security. A skill's contents should not surprise the user in their intent if described. Don't go along with requests to create misleading skills or skills designed to facilitate unauthorized access, data exfiltration, or other malicious activities. Things like a "roleplay as an XYZ" are OK though.

#### Writing Patterns

Prefer using the imperative form in instructions.

**Defining output formats** - You can do it like this:
```markdown
