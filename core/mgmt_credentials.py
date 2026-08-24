"""认证中心密码加密 —— Fernet 对称加密。

综合管理平台账号密码加密落库用。密钥必须稳定（重启不变）：优先 `MGMT_PASSWORD_KEY`，
否则由 `JWT_PRIVATE_KEY` 经 SHA-256 派生（生产已配 RSA 私钥）。`cryptography` 惰性
import——仅绑定/查询 mgmt 时用到，未装包不影响 Agent 主链路。
"""

import base64
import hashlib

from config.settings import settings


def _fernet_key() -> bytes:
  secret = settings.MGMT_PASSWORD_KEY or settings.JWT_PRIVATE_KEY
  if not secret:
    raise RuntimeError('未配置 MGMT_PASSWORD_KEY / JWT_PRIVATE_KEY，无法加密 mgmt 账号密码')
  return base64.urlsafe_b64encode(hashlib.sha256(secret.encode('utf-8')).digest())


def encrypt_password(plain: str) -> str:
  from cryptography.fernet import Fernet
  return Fernet(_fernet_key()).encrypt(plain.encode('utf-8')).decode('ascii')


def decrypt_password(enc: str) -> str:
  from cryptography.fernet import Fernet
  return Fernet(_fernet_key()).decrypt(enc.encode('ascii')).decode('utf-8')
