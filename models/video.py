import uuid
from enum import Enum
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class VideoTaskStatus(str, Enum):
  PENDING = 'pending'
  EXTRACTING = 'extracting'
  GENERATING_SCRIPT = 'generating_script'
  CONVERTING_IMAGES = 'converting_images'
  SYNTHESIZING_AUDIO = 'synthesizing_audio'
  RENDERING = 'rendering'
  SUCCESS = 'success'
  FAILED = 'failed'


class VideoTaskParams(BaseModel):
  aspect_ratio: str = '16:9'
  resolution: str = '1080p'
  voice: str = 'neutral'
  subtitle_style: str = 'default'
  # 解说词语言（送 TTS）与字幕语言，取值 zh-CN / en-US；默认值让旧任务无需迁移
  speech_language: str = 'zh-CN'
  subtitle_language: str = 'zh-CN'


class VideoTask(BaseModel):
  task_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
  user_id: str
  user_phone: str = ''
  original_filename: str = ''
  file_path: str = ''
  params: VideoTaskParams = Field(default_factory=VideoTaskParams)
  status: VideoTaskStatus = VideoTaskStatus.PENDING
  progress: int = 0  # 0-100
  progress_text: str = ''
  result_url: Optional[str] = None
  error: Optional[str] = None
  created_at: datetime = Field(default_factory=datetime.now)
  updated_at: datetime = Field(default_factory=datetime.now)


class VideoTaskPublic(BaseModel):
  """返回给前端的任务信息"""
  task_id: str
  original_filename: str
  params: VideoTaskParams
  status: VideoTaskStatus
  progress: int
  progress_text: str
  result_url: Optional[str] = None
  error: Optional[str] = None
  created_at: datetime
