import uuid
from enum import Enum
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ==================== 枚举 ====================

class UserType(str, Enum):
  UNVERIFIED = 'UNVERIFIED'
  INTERNAL = 'INTERNAL'
  EXTERNAL = 'EXTERNAL'


class UserStatus(str, Enum):
  PENDING = 'PENDING'    # 注册待审批
  ACTIVE = 'ACTIVE'      # 已通过，可登录
  DISABLED = 'DISABLED'  # 启用后被禁用


class ApplicationStatus(str, Enum):
  PENDING = 'pending'
  APPROVED = 'approved'
  REJECTED = 'rejected'


# ==================== 权限 ====================

class Permission(BaseModel):
  perm_key: str
  resource: str
  action: str
  description: str = ''


class Role(BaseModel):
  role_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
  name: str
  description: str = ''
  permissions: list[str] = []  # ['ai:chat', 'ai:knowledge', ...]
  is_builtin: bool = False
  created_at: datetime = Field(default_factory=datetime.utcnow)
  updated_at: datetime = Field(default_factory=datetime.utcnow)


# ==================== 用户 ====================

class User(BaseModel):
  user_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
  phone: str
  name: str = ''
  email: Optional[str] = None
  company: Optional[str] = None
  user_type: UserType = UserType.UNVERIFIED
  status: UserStatus = UserStatus.PENDING
  roles: list[str] = []
  permissions: list[str] = []
  created_at: datetime = Field(default_factory=datetime.utcnow)
  last_login_at: Optional[datetime] = None
  last_login_ip: Optional[str] = None


class UserPublic(BaseModel):
  """返回给前端的用户信息 (不含敏感字段)"""
  user_id: str
  phone: str
  name: str
  email: Optional[str] = None
  company: Optional[str] = None
  user_type: UserType
  status: UserStatus
  roles: list[str] = []
  permissions: list[str] = []
  created_at: datetime


# ==================== 权限申请 ====================

class RoleApplication(BaseModel):
  application_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
  user_id: str
  phone: str
  name: str
  requested_roles: list[str]
  reason: str
  status: ApplicationStatus = ApplicationStatus.PENDING
  reviewer_id: Optional[str] = None
  review_reason: Optional[str] = None
  reviewed_at: Optional[datetime] = None
  created_at: datetime = Field(default_factory=datetime.utcnow)


# ==================== 审计日志 ====================

class AuditLog(BaseModel):
  log_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
  user_id: Optional[str] = None
  phone: Optional[str] = None
  action: str  # e.g., 'user.login', 'admin.approve', 'admin.reject'
  resource: str  # e.g., 'application', 'user', 'role'
  detail: str = ''
  ip: str = ''
  created_at: datetime = Field(default_factory=datetime.utcnow)


# ==================== 预置权限清单 ====================

BUILTIN_PERMISSIONS: list[Permission] = [
  Permission(perm_key='ai:chat', resource='ai', action='chat', description='AI 对话'),
  Permission(perm_key='ai:knowledge', resource='ai', action='knowledge', description='知识库管理'),
  Permission(perm_key='ai:risk', resource='ai', action='risk', description='隐患识别'),
  Permission(perm_key='ai:fire_safety', resource='ai', action='fire_safety', description='消防配置'),
  Permission(perm_key='ai:video', resource='ai', action='video', description='视频生成'),
  Permission(perm_key='ai:agent', resource='ai', action='agent', description='AI Agent 对话'),
  Permission(perm_key='system:admin', resource='system', action='admin', description='系统管理'),
]


# ==================== 预置角色 ====================

BUILTIN_ROLES: list[dict] = [
  {
    'role_id': 'admin',
    'name': '管理员',
    'description': '拥有所有 AI 能力和系统管理权限',
    'permissions': ['system:admin', 'ai:chat', 'ai:agent', 'ai:knowledge', 'ai:risk', 'ai:fire_safety', 'ai:video'],
    'is_builtin': True,
  },
  {
    'role_id': 'user',
    'name': '普通用户',
    'description': '新用户默认角色，仅可 AI 对话',
    'permissions': ['ai:chat'],
    'is_builtin': True,
  },
  {
    'role_id': 'power_user',
    'name': '全功能用户',
    'description': '可使用全部 AI 功能（知识库、隐患识别、消防配置、视频生成）',
    'permissions': ['ai:chat', 'ai:agent', 'ai:knowledge', 'ai:risk', 'ai:fire_safety', 'ai:video'],
    'is_builtin': True,
  },
  {
    'role_id': 'dc_operator',
    'name': '数据中心运维',
    'description': '可使用 AI Agent 对话和 DCIM 工具查询',
    'permissions': ['ai:chat', 'ai:agent', 'ai:knowledge'],
    'is_builtin': True,
  },
]
