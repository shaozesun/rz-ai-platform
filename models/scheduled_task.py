"""定时任务模型（定时报告/巡检：到点自动调 Agent 跑一轮）"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _ensure_utc(v):
  """naive → 补 UTC tz；aware → 转 UTC。保证 JSON 序列化带 Z，前端能正确转本地时间。"""
  if v is None:
    return None
  if isinstance(v, datetime):
    if v.tzinfo is None:
      return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)
  return v


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
  thread_id: Optional[str] = None           # reuse 时复用指定会话（对话建任务时取当前会话 thread_id）
  enabled: bool = True
  # 由「分析报告」页从模板创建时写的来源标记（普通自定义任务为空）——
  # 用来把报告类任务从同一张表里挑出来，避免为此另建集合。
  template_id: Optional[str] = None

  @field_validator('once_at')
  @classmethod
  def _v_once_at(cls, v):
    return _ensure_utc(v)


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

  @field_validator('once_at')
  @classmethod
  def _v_once_at(cls, v):
    return _ensure_utc(v)


class ScheduledTask(ScheduledTaskBase):
  """定时任务定义（MongoDB scheduled_tasks，task_id 唯一）"""
  task_id: str = Field(default_factory=_new_id)
  next_run_at: Optional[datetime] = None
  created_by: str = ''
  created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
  updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

  @field_validator('next_run_at', 'created_at', 'updated_at')
  @classmethod
  def _v_times(cls, v):
    return _ensure_utc(v)


class ScheduledTaskRun(BaseModel):
  """单次定时运行记录（MongoDB scheduled_task_runs，run_id 唯一）"""
  run_id: str = Field(default_factory=_new_id)
  task_id: str
  thread_id: str = ''          # 本轮运行的 Agent 线程 id（rz-agent-sched-...），供「查看对话」拉会话消息
  status: RunStatus = RunStatus.PENDING
  triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
  final_reply: str = ''        # Agent 本轮最终文本回复（含 <file> 标签）
  # 报告期号（仅预设报告有）：由平台在触发时按窗口算好落库 —— 前端据此按「期」分组/筛选，
  # 不必解析产物文件名（2026-10-13 加，配合报告页改版）
  preset_key: str = ''
  period_key: str = ''         # 如 2026-W41 / 2026-09
  period_label: str = ''       # 如「2026 第 41 周」/「2026 年 9 月」
  # 不合规检查：各检查项命中数 {check_key: rows}。生成时落库，列表页卡片直接用，
  # 免得为了拿数字在列表接口里逐个打开 xlsx（2026-10-27 加）
  check_counts: dict[str, int] = {}
  artifact_urls: list[str] = []  # 产物下载 URL（/api/v1/chat/agent/artifacts/...）
  error: str = ''
  duration_ms: int = 0
  created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

  @field_validator('triggered_at', 'created_at')
  @classmethod
  def _v_times(cls, v):
    return _ensure_utc(v)


class ScheduledTaskNotification(BaseModel):
  """定时任务完成通知（MongoDB scheduled_task_notifications，按创建人关联）"""
  notification_id: str = Field(default_factory=_new_id)
  user_id: str = ''            # 任务创建人（铃铛/首页动态按此关联）
  task_id: str = ''
  task_name: str = ''
  run_id: str = ''
  status: str = ''             # success / failed
  title: str = ''
  body: str = ''
  batch_key: str = ''          # 合并批次键（同批失败聚合成一条；空＝单份一条）
  count: int = 0               # 该条聚合了几份运行
  created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

  @field_validator('created_at')
  @classmethod
  def _v_time(cls, v):
    return _ensure_utc(v)
