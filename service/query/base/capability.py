"""平台无关的能力元数据 —— 所有平台（DCIM/综合管理平台/...）共用同一数据契约。

一条 Capability 描述「一个可查询/可执行的能力」的元数据（纯数据，非工具对象）。
平台目录（dcim/mgmt/...）只提供 Capability 列表 + 后端 caller，其余逻辑
（检索/工具生成/意图绑定）全由 base/ 统一层处理。

字段设计要点：
- id 全局唯一且带平台前缀（如 'dcim.query_alarm'），检索与工具生成据此天然隔离平台。
- description 是语义检索质量的关键，需覆盖同义词与使用场景。
- kind / intent_labels 为多标签编排预留：分类词汇表未定前可留空，定了再填，不改结构。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
  """一个平台能力的元数据。

  Attributes:
    id: 全局唯一标识，带平台前缀，形如 'dcim.query_alarm'，供路由与工具引用。
    name: 简短中文名，用于展示。
    description: 自然语言描述，检索质量的关键——写清「能查什么、什么场景用」。
    params: 参数 schema，{参数名: 说明}，供 Agent 组织调用入参。
    domain: 业务域（alarm/asset/capacity/...），便于分组与过滤。
    kind: 能力类型（检索/状态/资源/动作），供编排层区分处理（如动作类需二次确认）。
    intent_labels: 归属的意图标签（闭合词汇表），供绑定表聚合；分类未定前留空。
    required_perm: 调用所需权限，留空表示继承门面工具的权限。
  """

  id: str
  name: str
  description: str
  params: dict[str, str] = field(default_factory=dict)
  domain: str = ''
  kind: str = ''
  intent_labels: tuple[str, ...] = ()
  required_perm: str = ''
