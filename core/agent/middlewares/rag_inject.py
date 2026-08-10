"""RAG 上下文自动注入 — 在首条 user message 前注入知识库检索结果"""

import logging

logger = logging.getLogger(__name__)

# 可选：自动检测用户问题中是否包含知识库查询意图，注入 RAG 上下文
# 当前 Phase 1 使用 @tool 方式让 Agent 自主决定是否调 RAG，此模块预留

RAG_INJECT_ENABLED = False


def build_rag_inject_message(question: str, group_id: str = 'default') -> str:
  """检索知识库并构建注入消息。当前为预留功能。"""
  if not RAG_INJECT_ENABLED:
    return ''

  try:
    from service.rag.pipeline.rag_service import rag_service

    result = rag_service.hybrid_retrieve(question=question, group_id=group_id)
    context = result.get('context', '')
    if not context:
      return ''

    return f'以下是与用户问题相关的知识库参考资料：\n\n{context}'

  except Exception:
    logger.exception('RAG 上下文注入失败')
    return ''
