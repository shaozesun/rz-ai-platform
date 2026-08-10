"""RAG 知识库检索工具"""

from langchain.tools import tool


@tool
def rag_knowledge_search(query: str, group_id: str = 'default') -> str:
  """检索知识库中的运维标准操作流程（SOP）、应急预案和技术文档。

  Args:
    query: 检索关键词或问题，如 'UPS 故障处理流程'、'制冷系统巡检标准'
    group_id: 知识库组 ID，默认 'default'
  """
  try:
    from service.rag.pipeline.rag_service import rag_service

    result = rag_service.hybrid_retrieve(
      question=query,
      group_id=group_id,
    )

    docs = result.get('final_docs', [])
    if not docs:
      return '知识库中未找到相关内容。请尝试换个关键词搜索。'

    context = result.get('context', '')
    if context:
      return context

    # 如果没有 context 但有 docs，手动拼接
    parts = []
    for i, doc in enumerate(docs, 1):
      content = doc.page_content if hasattr(doc, 'page_content') else str(doc)
      source = doc.metadata.get('source', '未知来源') if hasattr(doc, 'metadata') else ''
      parts.append(f'### 文档 {i}（来源：{source}）\n{content}')

    return '\n\n'.join(parts)

  except Exception as e:
    return f'知识库检索失败: {str(e)}。请告知用户稍后重试。'
