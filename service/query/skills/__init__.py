"""技能（skills）平台 —— 声明式 skill 运行时。

skills/<name>/SKILL.md（元信息 + 使用说明）+ references/<stem>.md（详规）
在启动时被 discover 自动发现为 Capability，走统一层（注册/检索/工具生成/
deferred/编排）。接入新 skill 零代码：丢一个目录即可。

参照 DeerFlow skills 机制的「文档解析 + 渐进披露」思路（元数据=Capability
description 进 tool_search 索引，主体=调用主能力返回 SKILL.md，附加=调用
子能力返回 references 详规）。
"""
