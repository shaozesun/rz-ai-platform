"""飞书产物下载：签名 token + <file> 标签改写为可点 markdown 链接。

Agent 回复里的 `<file url name/>` 在飞书卡片里是纯文本，且原下载端点需要 JWT，
飞书浏览器侧没有登录态。这里为真实存在的产物生成签名 token，把标签改写为公开
端点 markdown 链接（token 即授权，TTL 内可多次下载）。

token 签发与标签改写均由 `service/channels/artifact_token.py` /
`service/channels/file_tags.py` 通用实现，飞书只传自己的端点前缀与 token 命名空间。
"""

from config.settings import settings
from service.channels import artifact_token, file_tags

_TOKEN_PREFIX = 'lark:artifact:'


def create_download_token(thread_id: str, dataset_id: str, filename: str) -> str:
  """为产物生成签名 token 并写入 Redis，返回 token。"""
  return artifact_token.create_download_token(
    thread_id, dataset_id, filename,
    prefix=_TOKEN_PREFIX, ttl=settings.LARK_ARTIFACT_TOKEN_TTL,
  )


def resolve_download_token(token: str) -> dict | None:
  """解析下载 token，返回 {thread_id, dataset_id, filename}；无效返回 None。"""
  return artifact_token.resolve_download_token(token, prefix=_TOKEN_PREFIX)


def rewrite_for_feishu(text: str, thread_id: str) -> str:
  """把 Agent 回复里的 Web 专属交付物标签降级为飞书可展示文本。

  1. <file> 产物 → 下载链接；records 表格额外读 CSV 前几行转纯文本。
  2. <chart> 图表 → 删除（飞书无法渲染，不显示占位）。
  3. <mermaid> 流程图 → 删除（同上）。
  """
  base = settings.PUBLIC_BASE_URL.rstrip('/')
  return file_tags.rewrite_for_channel(
    text, thread_id,
    url_prefix=f'{base}/api/v1/feishu/artifacts',
    token_prefix=_TOKEN_PREFIX,
    token_ttl=settings.LARK_ARTIFACT_TOKEN_TTL,
  )
