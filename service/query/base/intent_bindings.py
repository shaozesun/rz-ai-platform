"""意图标签 → 能力 的绑定表（从注册表动态聚合，不手写静态 dict）。

多标签编排的 Step 2「工具路由」查此表：意图识别器输出 intent_labels，编排层
据此查出对应能力 id 组，预 promote 给 LLM。绑定关系来源于每条 Capability——

**标签来源规则**：能力 `intent_labels` 非空时用它；为空时**回退用 `category`**
（L2 功能分类）。这样只维护 category 一套词汇，不再有 intent_labels/category
两套漂移；新增能力只需填 `category`，绑定表与分类清单自动跟着变。
"""

from service.query.base.platform import all_capabilities


def build_intent_bindings() -> dict[str, list[str]]:
  """聚合「意图标签 → 能力 id 列表」。

  Returns:
    {label: [capability_id, ...]}；能力 intent_labels 为空时回退用 category
    （category 也为空的能力不出现——正常情况不应发生，声明能力时 category 必填）。
  """
  result: dict[str, list[str]] = {}
  for cap in all_capabilities():
    if cap.owner:
      continue  # 用户自建技能不进路由词汇表（按需 tool_search 命中，不预 promote）
    labels = cap.intent_labels if cap.intent_labels else ((cap.category,) if cap.category else ())
    for label in labels:
      result.setdefault(label, []).append(cap.id)
  return result


def all_intent_labels() -> list[str]:
  """返回全部已定义的意图标签，供意图识别器构造闭合词汇表约束。

  因绑定表已收敛到 category 词汇，此返回值即「分类词汇表」。
  """
  return sorted(build_intent_bindings().keys())
