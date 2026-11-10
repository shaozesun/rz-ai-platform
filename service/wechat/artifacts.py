"""微信产物下载：签名 token + <file> 标签改写为可点 markdown 链接。

与飞书同源（`service/channels/artifact_token.py` + `file_tags.py`），只是端点前缀与
token 命名空间换成微信自己的。iLink 里 markdown 链接以纯文本展示，用户可点开在
浏览器下载。
"""

from config.settings import settings
from service.channels import artifact_token, file_tags

_TOKEN_PREFIX = 'wechat:artifact:'


def create_download_token(thread_id: str, dataset_id: str, filename: str) -> str:
  return artifact_token.create_download_token(
    thread_id, dataset_id, filename,
    prefix=_TOKEN_PREFIX, ttl=settings.WECHAT_ARTIFACT_TOKEN_TTL,
  )


def resolve_download_token(token: str) -> dict | None:
  return artifact_token.resolve_download_token(token, prefix=_TOKEN_PREFIX)


def rewrite_for_wechat(text: str, thread_id: str) -> str:
  """把 <file> 标签降级为下载链接；<chart>/<mermaid> 删除（微信不渲染）。"""
  base = settings.PUBLIC_BASE_URL.rstrip('/')
  return file_tags.rewrite_for_channel(
    text, thread_id,
    url_prefix=f'{base}/api/v1/wechat/artifacts',
    token_prefix=_TOKEN_PREFIX,
    token_ttl=settings.WECHAT_ARTIFACT_TOKEN_TTL,
  )
