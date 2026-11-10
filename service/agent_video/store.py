"""讲解视频任务的持久化（集合 `agent_video_tasks`，与旧 `video_tasks` 完全隔离）。

为什么单独一个集合：用户要求新能力"纯加法"——旧 VideoPage 的列表/详情/删除接口
一律不动，所以新任务不能混进 `video_tasks`（否则会出现在旧列表里）。

每页一条记录（pages[]）是这套设计的核心：
- **可续跑**：进程崩溃/重启后，从已完成页继续（DeerFlow durable batch 的思路）
- **可增量改**：改一页只重跑该页（解说词 → TTS → 该页片段 → 重新 concat）
"""

import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

COLLECTION = 'agent_video_tasks'

# 任务状态：准备 → 渲染页面 → 写解说词 → 配音 → 合成 → 完成
STATUS_PENDING = 'pending'
STATUS_SLIDES = 'slides'
STATUS_NARRATION = 'narration'
STATUS_TTS = 'tts'
STATUS_COMPOSING = 'composing'
STATUS_SUCCESS = 'success'
STATUS_FAILED = 'failed'

# 单页状态
PAGE_PENDING = 'pending'
PAGE_OK = 'ok'
PAGE_DEGRADED = 'degraded'      # 该页降级（空解说词 / 用简版图）
PAGE_NEEDS_REVIEW = 'needs_review'  # 自检发现问题且自动修复失败，交付前需人工确认

_PROGRESS_TEXT = {
  STATUS_PENDING: '排队中…',
  STATUS_SLIDES: '渲染幻灯片…',
  STATUS_NARRATION: '撰写解说词…',
  STATUS_TTS: '合成语音…',
  STATUS_COMPOSING: '合成视频…',
  STATUS_SUCCESS: '完成',
}


def work_root(thread_id: str) -> Path:
  """会话下的讲解视频根目录：`{AGENT_ANALYSIS_DATA_DIR}/{thread}/agent-video/`。"""
  from core.sandbox import datasets as sandbox_datasets
  return sandbox_datasets.thread_dir(thread_id) / 'agent-video'


def work_dir(thread_id: str, task_id: str) -> Path:
  """任务工作目录：{AGENT_ANALYSIS_DATA_DIR}/{thread}/agent-video/{task_id}/

  放在会话目录下（与 datasets/uploads 同层），天然按会话隔离，也复用会话清理约定。
  """
  safe_task = ''.join(c for c in task_id if c.isalnum() or c in '-_') or uuid.uuid4().hex
  path = work_root(thread_id) / safe_task
  path.mkdir(parents=True, exist_ok=True)
  return path


# 任务目录里的中间产物（只服务"改一页只重编该页"；成片 out.mp4 不在列，永远保留）
_INTERMEDIATE_DIRS = ('segments', 'audio', 'slides', 'checks')


async def prune_old_intermediates(thread_id: str, *, keep: int) -> list[str]:
  """保留最近 `keep` 个任务的完整目录，更旧的**只留 out.mp4**，返回被清理的 task_id 列表。

  为什么清理：中间产物远大于成片（实测 50 页任务：页图 50M + 音频 53M + 片段 32M + 自检 8M，
  而 out.mp4 只有 32M），一个会话跑几十个任务就是几个 G，而目录**只增不减**。
  为什么留成片：那是用户要的东西（播放/下载都靠它）；中间产物只服务增量重跑，越旧越不会再改。
  被清理的任务会打 `intermediates_pruned` 标记 → 再想"只改某一页"时给明确提示，而不是报一堆
  找不到文件的怪错。
  """
  root = work_root(thread_id)
  if not root.is_dir():
    return []
  dirs = sorted((p for p in root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime, reverse=True)
  pruned: list[str] = []
  for d in dirs[max(0, keep):]:
    try:
      for name in _INTERMEDIATE_DIRS:
        shutil.rmtree(d / name, ignore_errors=True)
      (d / 'segments.txt').unlink(missing_ok=True)
      await _collection().update_one({'task_id': d.name},
                                     {'$set': {'intermediates_pruned': True}})
      pruned.append(d.name)
    except Exception as e:  # noqa: BLE001 - 清理失败不能影响任何业务流程
      logger.warning('清理旧任务中间产物失败（跳过）: %s → %s', d.name, e)
  if pruned:
    logger.info('讲解视频：清理 %d 个旧任务的中间产物（保留成片）: %s', len(pruned), pruned)
  return pruned


def new_task_id() -> str:
  return uuid.uuid4().hex[:16]


def _collection():
  from config.mongodb_conn import mongodb_manager
  return mongodb_manager.db[COLLECTION]


async def create_task(
  *, thread_id: str, user_id: str, session_id: str, ppt_path: str = '',
  language: str, voice: str, aspect_ratio: str, page_count: int = 0,
  motion: str = '', source_kind: str = 'ppt', title: str = '', outline_path: str = '',
  subtitle_language: str = '', subtitles: bool = True, target_minutes: float = 0,
  requirements: str = '',
) -> str:
  """建任务（状态 pending），返回 task_id。"""
  task_id = new_task_id()
  now = datetime.now()
  doc = {
    'task_id': task_id,
    'thread_id': thread_id,
    'session_id': session_id,
    'user_id': user_id,
    'source_name': title or (Path(ppt_path).name if ppt_path else '讲解视频'),
    'source_path': ppt_path,
    # source_kind='ppt' 走上传播放页图；'text' 走 text_pages 本地排版（大纲落 outline.md）
    'source_kind': source_kind,
    'outline_path': outline_path,
    'motion': motion or '',
    'language': language,
    # 字幕语言：空 = 与配音同语言（此时每页 subtitle 字段不单独存，直接用 script）
    'subtitle_language': subtitle_language or '',
    # 本条视频要不要烧字幕（服务端另有全局开关 VIDEO_AGENT_SUBTITLE；这里是按任务的）
    'subtitles': bool(subtitles),
    # 目标总时长（分钟，0=不限）：只在生成/重写解说词时用来折算每页字数上限（近似控制）
    'target_minutes': float(target_minutes or 0),
    # 用户确认时提的附加要求（受众/语气/片尾提示…），拼进解说词提示词
    'requirements': requirements or '',
    'voice': voice,
    'aspect_ratio': aspect_ratio,
    'status': STATUS_PENDING,
    'progress': 0,
    'progress_text': _PROGRESS_TEXT[STATUS_PENDING],
    'pages': [],
    'page_count': page_count,
    'video_path': '',
    'error': '',
    'created_at': now,
    'updated_at': now,
  }
  await _collection().insert_one(doc)
  return task_id


async def get_task(task_id: str, *, thread_id: str | None = None) -> dict | None:
  """取任务；传 thread_id 时做会话归属校验（工具/端点都走这条路，防跨会话读取）。"""
  query: dict = {'task_id': task_id}
  if thread_id:
    query['thread_id'] = thread_id
  return await _collection().find_one(query, {'_id': 0})


async def list_tasks(thread_id: str, limit: int = 20) -> list[dict]:
  cursor = _collection().find({'thread_id': thread_id}, {'_id': 0}).sort('created_at', -1).limit(limit)
  return [d async for d in cursor]


async def set_status(
  task_id: str, status: str, *, progress: int | None = None,
  progress_text: str | None = None, error: str = '',
) -> None:
  update: dict = {'status': status, 'updated_at': datetime.now()}
  if progress is not None:
    update['progress'] = progress
  update['progress_text'] = progress_text or _PROGRESS_TEXT.get(status, status)
  if error:
    update['error'] = error
  await _collection().update_one({'task_id': task_id}, {'$set': update})


async def set_pages(task_id: str, pages: list[dict], *, page_count: int | None = None) -> None:
  update: dict = {'pages': pages, 'updated_at': datetime.now()}
  if page_count is not None:
    update['page_count'] = page_count
  await _collection().update_one({'task_id': task_id}, {'$set': update})


async def update_one_page(task_id: str, index: int, patch: dict) -> None:
  """按页号更新单页字段（`pages.$[p]` 定位数组元素，避免整表覆盖的并发丢更新）。"""
  await _collection().update_one(
    {'task_id': task_id, 'pages.index': index},
    {'$set': {f'pages.$.{k}': v for k, v in patch.items()},
     '$currentDate': {'updated_at': True}},
  )


async def set_video(task_id: str, video_path: str, *, duration: float = 0.0) -> None:
  await _collection().update_one({'task_id': task_id}, {'$set': {
    'video_path': video_path,
    'duration': duration,
    'status': STATUS_SUCCESS,
    'progress': 100,
    'progress_text': _PROGRESS_TEXT[STATUS_SUCCESS],
    'updated_at': datetime.now(),
  }})


async def set_options(task_id: str, *, language: str | None = None, voice: str | None = None,
                      subtitle_language: str | None = None) -> None:
  update = {k: v for k, v in (('language', language), ('voice', voice),
                             ('subtitle_language', subtitle_language)) if v}
  if update:
    update['updated_at'] = datetime.now()
    await _collection().update_one({'task_id': task_id}, {'$set': update})


async def mark_failed(task_id: str, error: str) -> None:
  await _collection().update_one({'task_id': task_id}, {'$set': {
    'status': STATUS_FAILED, 'error': error, 'updated_at': datetime.now(),
  }})
