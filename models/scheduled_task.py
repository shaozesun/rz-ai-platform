"""定时任务模型（定时报告/巡检：到点自动调 Agent 跑一轮）"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ScheduleType(str, Enum):
  """调度类型"""
  ONCE = 'once'        # 指定时间跑一次
  INTERVAL = 'interval'  # 每 N 秒
  CRON = 'cron'        # cron 表达式（需 croniter）


class RunStatus(str, Enum):
  """单次运行状态"""
  PENDING = 'pending'
  RUNNING = 'running'
  SUCCESS = 'success'
  FAILED = 'failed'


def _new_id() -> str:
  return uuid.uuid4().hex


class ScheduledTaskBase(BaseModel):
  name: str
  description: str = ''
  schedule_type: ScheduleType = ScheduleType.INTERVAL
  once_at: Optional[datetime] = None        # once 模式
  interval_seconds: Optional[int] = None    # interval 模式
  cron_expr: Optional[str] = None           # cron 模式
  prompt: str                               # 作为用户消息喂给 Agent 的任务指令
  task_mode: str = 'normal'                 # normal / full_analysis（全量分析）
  thread_strategy: str = 'new'              # 'new' 每次新会话 / 'reuse' 固定会话累积历史
  enabled: bool = True


class ScheduledTaskCreate(ScheduledTaskBase):
  pass


class ScheduledTaskUpdate(BaseModel):
  name: Optional[str] = None
  description: Optional[str] = None
  schedule_type: Optional[ScheduleType] = None
  once_at: Optional[datetime] = None
  interval_seconds: Optional[int] = None
  cron_expr: Optional[str] = None
  prompt: Optional[str] = None
  task_mode: Optional[str] = None
  thread_strategy: Optional[str] = None
  enabled: Optional[bool] = None


class ScheduledTask(ScheduledTaskBase):
  """定时任务定义（MongoDB scheduled_tasks，task_id 唯一）"""
  task_id: str = Field(default_factory=_new_id)
  next_run_at: Optional[datetime] = None
  created_by: str = ''
  created_at: datetime = Field(default_factory=datetime.utcnow)
  updated_at: datetime = Field(default_factory=datetime.utcnow)


class ScheduledTaskRun(BaseModel):
  """单次定时运行记录（MongoDB scheduled_task_runs，run_id 唯一）"""
  run_id: str = Field(default_factory=_new_id)
  task_id: str
  status: RunStatus = RunStatus.PENDING
  triggered_at: datetime = Field(default_factory=datetime.utcnow)
  final_reply: str = ''        # Agent 本轮最终文本回复（含 <file> 标签）
  artifact_urls: list[str] = []  # 产物下载 URL（/api/v1/chat/agent/artifacts/...）
  error: str = ''
  duration_ms: int = 0
  created_at: datetime = Field(default_factory=datetime.utcnow)
