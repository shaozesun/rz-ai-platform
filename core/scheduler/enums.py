from enum import IntEnum


class TaskType(IntEnum):
  """LLM 调用业务场景类型，数值影响基础优先级权重。"""
  CHAT = 1             # 第一优先级：RAG 对话、非 RAG 对话
  RISK_DETECTION = 2   # 第二优先级：图片隐患检测
  FIRE_SAFETY = 3      # 第二优先级：消防配置推荐
  INGESTION = 4        # 第三优先级：知识库文档上传
  VIDEO = 5            # 第三优先级：视频生成


class RoleLevel(IntEnum):
  """用户角色等级，数值影响优先级加成。"""
  ADMIN = 1
  USER = 2
  ANONYMOUS = 3
