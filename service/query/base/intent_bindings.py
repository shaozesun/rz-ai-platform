"""意图标签 → 能力 的绑定表（从注册表动态聚合，不手写静态 dict）。

多标签编排的 Step 2「工具路由」查此表：意图识别器输出 intent_labels，编排层
据此查出对应能力 id 组，预 promote 给 LLM。绑定关系来源于每条 Capability 的
intent_labels 字段——新增能力时在能力上填标签，绑定表自动跟着变，无需维护第二份配置。

分类词汇表未定前，各能力 intent_labels 可留空，此表即为空，编排层自然全走
tool_search fallback，不阻塞其余链路。
"""

from service.query.base.platform import all_capabilities


def build_intent_bindings() -> dict[str, list[str]]:
  """聚合「意图标签 → 能力 id 列表」。

  Returns:
    {intent_label: [capability_id, ...]}；能力未填 intent_labels 则不出现在表中。
  """
  result: dict[str, list[str]] = {}
  for cap in all_capabilities():
    for label in cap.intent_labels:
      result.setdefault(label, []).append(cap.id)
  return result


def all_intent_labels() -> list[str]:
  """返回全部已定义的意图标签，供意图识别器构造闭合词汇表约束。"""
  return sorted(build_intent_bindings().keys())
