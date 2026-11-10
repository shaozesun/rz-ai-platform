"""微信凭据落盘：命令行脚本扫码接入后把 bot_token 写文件（0600），重启即自动恢复。

iLink 的 `bot_token` 会过期（`errcode -14`），过期后清掉此文件、重回「待接入」态。
凭据只存最小集：`{bot_token, ilink_bot_id, base_url}`。
"""

import json
import logging
import os
import stat
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)


def _path() -> Path:
  return Path(settings.WECHAT_STATE_FILE).expanduser()


def load_auth() -> dict:
  """读凭据；文件不存在/损坏返回空 dict。"""
  try:
    data = json.loads(_path().read_text(encoding='utf-8'))
    return data if isinstance(data, dict) else {}
  except FileNotFoundError:
    return {}
  except Exception as e:
    logger.warning('[WeChat] 读取凭据失败: %s', e)
    return {}


def save_auth(creds: dict) -> None:
  """写凭据到 state 文件（0600）。"""
  p = _path()
  try:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
  except OSError as e:
    logger.warning('[WeChat] 保存凭据失败: %s', e)


def clear_auth() -> None:
  """删凭据文件（token 过期/断开接入时调用）。"""
  try:
    _path().unlink()
  except FileNotFoundError:
    pass
  except OSError as e:
    logger.warning('[WeChat] 清除凭据失败: %s', e)
