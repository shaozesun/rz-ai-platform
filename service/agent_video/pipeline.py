"""讲解视频编排：PPT → 逐页图 → 自检 → 逐页解说词 → 逐页配音 → 合成 MP4。

设计要点（都是为了"能改"与"不整单失败"）：

- **每页产物落库**（`agent_video_tasks.pages[]`）：进程崩溃可续跑；改一页只重跑该页
- **单页失败降级**：某页解说词/配音失败 → 该页静音 + 标记 `degraded`，其余页照常交付；
  **全部页失败**才整单失败（DeerFlow「失败要吵」：不静默产出残缺品）
- **进度按页推进**：`progress_text` 写"第 N/M 页"，用户/agent 都能看到卡在哪一页
- **分阶段计时**：跑完打一行 `slides=… ritl=… narration=… tts=… compose=…`
  （回答"时间卡在哪"只能靠这个——阶段之间量级差一个数量级，光看总时长看不出来）
- 后台任务（`asyncio.create_task`）由工具层发起，不阻塞对话
"""

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from config.settings import settings

from . import audio, compose, narration, ritl, slides, store, texts

logger = logging.getLogger(__name__)


class _Stages:
  """分阶段计时累加器（只服务于日志，不落库）。"""

  def __init__(self) -> None:
    self.marks: list[tuple[str, float]] = []

  def mark(self, name: str, seconds: float) -> None:
    self.marks.append((name, seconds))

  def total(self) -> float:
    return sum(s for _, s in self.marks)

  def summary(self) -> str:
    if not self.marks:
      return '（无）'
    return ' '.join(f'{n}={s:.1f}s' for n, s in self.marks) + f' 合计={self.total():.1f}s'


@contextlib.asynccontextmanager
async def _stage(stages: _Stages, name: str):
  """给一个阶段计时（异常也照记，失败时才知道卡在哪一步）。"""
  t0 = time.perf_counter()
  try:
    yield
  finally:
    stages.mark(name, time.perf_counter() - t0)


def _read_outline(task: dict) -> str:
  """读取文本任务的原始大纲（落盘的 outline.md）；读不到回退空串。"""
  path = task.get('outline_path') or ''
  try:
    return Path(path).read_text(encoding='utf-8') if path else ''
  except Exception:  # noqa: BLE001 - 读不到就当作空大纲，后续按"无内容"处理
    logger.warning('读取大纲失败: %s', path)
    return ''


def _build_pages(images: list[str], page_texts: list[str], scripts: list[dict],
                 checks: dict[int, dict]) -> list[dict]:
  pages: list[dict] = []
  for i, image in enumerate(images):
    script_info = scripts[i] if i < len(scripts) else {'script': '', 'error': '未来得及生成'}
    check = checks.get(i + 1, {'ok': True, 'reason': ''})
    check_ok = check.get('ok', True)
    # 三种结局分开记（此前"自检存疑"被并进 degraded，PAGE_NEEDS_REVIEW 定义了却从没赋过值，
    # 与 SKILL/文档里承诺的"问题页标 needs_review"不符）：
    #   无解说词 → degraded（静音页交付）；有内容但自检存疑 → needs_review（请用户确认）；
    #   正常 → pending，等合成完成后由 _compose_all 改成 ok
    if not script_info['script']:
      status = store.PAGE_DEGRADED
    elif not check_ok:
      status = store.PAGE_NEEDS_REVIEW
    else:
      status = store.PAGE_PENDING
    pages.append({
      'index': i + 1,
      'text': (page_texts[i] if i < len(page_texts) else '')[:2000],
      'script': script_info['script'],
      # 字幕文本：双语时是另一份（如英文解说配中文字幕），否则与配音稿同一份
      'subtitle': script_info.get('subtitle') or script_info['script'],
      'image': image,
      'audio': '',
      'duration': 0.0,
      'segment': '',
      'status': status,
      'attempts': 0,
      'error': script_info.get('error', '') or ('' if check_ok else check.get('reason', '')),
      'checks': check,
    })
  return pages


async def _synthesize_pages(
  task_id: str, pages: list[dict], work: Path, voice: str, *, progress_base: int = 50,
  speech_language: str = 'zh-CN',
) -> list[dict]:
  """逐页配音（并发受 TTS 跨进程配额限制）；单页失败降级为静音页。

  并发用 `VIDEO_AGENT_TTS_CONCURRENCY`（**不是**解说词那个 3）：两者限制来源不同——
  解说词要迁就 LLM 网关，TTS 侧的真配额是 `TTS_MAX_CONCURRENCY`（64，Redis 跨进程信号量）。
  实测 3 并发下 TTS 阶段 50 页 ≈35s，提到 8 后 ≈13s。
  """
  total = len(pages)
  sem = asyncio.Semaphore(max(1, settings.VIDEO_AGENT_TTS_CONCURRENCY))
  done = {'n': 0}

  async def _one(page: dict) -> None:
    idx = page['index']
    try:
      async with sem:
        info = await audio.synthesize_page(idx - 1, page.get('script', ''), voice, work,
                                           speech_language)
      page['audio'] = info['audio']
      page['duration'] = info['duration']
      if not page.get('script'):
        page['status'] = store.PAGE_DEGRADED
    except Exception as e:  # noqa: BLE001 - 单页失败不整单失败
      logger.warning('第 %d 页配音失败（降级为静音页）: %s', idx, e)
      page['status'] = store.PAGE_DEGRADED
      page['error'] = f'配音失败: {e}'
    finally:
      done['n'] += 1
      await store.update_one_page(task_id, idx, {
        'audio': page['audio'], 'duration': page['duration'],
        'status': page['status'], 'error': page['error'],
      })
      pct = progress_base + int(20 * done['n'] / max(1, total))
      await store.set_status(task_id, store.STATUS_TTS, progress=min(95, pct),
                             progress_text=f'合成语音 第 {done["n"]}/{total} 页')

  await asyncio.gather(*(_one(p) for p in pages))

  if all(not p.get('audio') for p in pages):
    raise audio.AudioError('全部页面配音失败，请检查 TTS 服务后重试')
  return pages


async def _compose_all(
  task_id: str, pages: list[dict], work: Path, aspect_ratio: str, *,
  reuse: bool = True, motion: str = '', subtitles: bool = True,
) -> Path:
  """逐页编码 + concat（复用已存在的片段，实现"只重跑改动的页"）。

  动效（Ken Burns 运镜）在段内完成：`motion='mixed'` 时按页轮换运镜；只有**首段淡入、
  末段淡出**，页间不做淡变——页间淡出会忽明忽暗，交叉溶解则要整条重编码（毁掉增量重跑）。
  """
  segments: list[str] = []
  total = len(pages)
  encoded: list[tuple[int, float]] = []      # (页号, 编码秒数)，用于打"最慢的几页"
  for i, page in enumerate(pages, start=1):
    idx = page['index']
    seg = work / 'segments' / f'page_{idx:03d}.mp4'
    # 复用判据：片段存在且不早于该页图片/音频的修改时间（增量重跑的关键——
    # 只改了第 3 页时，其余页直接复用旧片段，不重编码）
    need = True
    if reuse and seg.is_file():
      try:
        newest_input = max(
          Path(p).stat().st_mtime
          for p in (page.get('image', ''), page.get('audio', ''))
          if p and Path(p).is_file()
        )
        need = seg.stat().st_mtime < newest_input
      except Exception:  # noqa: BLE001 - 判断不了就重编，安全侧
        need = True
    if need:
      idx_in_task = page['index']
      t0 = time.perf_counter()
      await compose.compose_page(
        image=page['image'], audio=page.get('audio', ''), out_path=seg,
        aspect_ratio=aspect_ratio,
        motion=compose.motion_for(motion, idx_in_task),
        fade_in=(i == 1),
        fade_out=(i == total),
        # 字幕烧在段内 → 改一页只需重编该页；文本用 subtitle（双语时与配音稿不同）。
        # 用户明确说"不要字幕"时传空串（drawtext 不生成 → 该片段无字幕）
        subtitle=(page.get('subtitle') or page.get('script', '')) if subtitles else '',
      )
      encoded.append((idx, time.perf_counter() - t0))
    page['segment'] = str(seg)
    # 片段就位（无论本轮新编还是复用）即算这一页交付完成；
    # degraded / needs_review 保持不动（它们是要提示用户的状态，不能被"完成"覆盖掉）；
    # 老任务里没有 status 字段的页按 pending 处理（别 KeyError）
    status = page.get('status') or store.PAGE_PENDING
    if status == store.PAGE_PENDING:
      status = store.PAGE_OK
    page['status'] = status
    segments.append(str(seg))
    await store.update_one_page(task_id, idx, {'segment': str(seg), 'status': status})
    await store.set_status(task_id, store.STATUS_COMPOSING,
                           progress=95 + int(4 * i / max(1, total)),
                           progress_text=f'合成视频 第 {i}/{total} 页')

  if encoded:
    # 逐页编码是**串行**的，最慢那页往往就是整体卡点（长页/复杂图/运镜），值得单独看一眼
    slowest = sorted(encoded, key=lambda x: -x[1])[:3]
    logger.info('合成编码 %d 页（复用 %d 页）: 平均 %.2fs/页，最慢 %s',
                len(encoded), total - len(encoded), sum(s for _, s in encoded) / len(encoded),
                '、'.join(f'第{p}页 {s:.1f}s' for p, s in slowest))

  out = work / 'out.mp4'
  # concat 列表用**相对于工作目录**的路径不保险，统一用绝对路径
  #
  # **先写临时文件再原子替换**：重跑时任务状态一度还是 success，前端据此补卡片并开始播放，
  # 而此刻 ffmpeg 正在重写 out.mp4 → 浏览器读到"写到一半的文件"就报加载失败（实测）。
  # 原子替换后读端要么拿到旧成片、要么拿到新成片，绝不会读到半成品。
  tmp = work / 'out.tmp.mp4'
  await compose.concat_segments(segments, tmp)
  tmp.replace(out)
  return out


async def run_task(task_id: str) -> None:
  """执行一次完整生成（后台任务入口）。任何异常都落到任务状态里，不向外抛。"""
  task = await store.get_task(task_id)
  if not task:
    logger.error('讲解视频任务不存在: %s', task_id)
    return
  thread_id = task['thread_id']
  work = store.work_dir(thread_id, task_id)

  stages = _Stages()
  try:
    # ── 1. 页面图 ──
    await store.set_status(task_id, store.STATUS_SLIDES, progress=5)
    async with _stage(stages, 'slides'):
      if task.get('source_kind') == 'text':
        # 纯文字/大纲：本地排版成页图（无需 soffice），每页文字由 text_pages 给出
        from . import text_pages
        outline = _read_outline(task)
        # 分页要看画幅（9:16 一页装得下的字比 16:9 少），所以这里必须传 aspect_ratio：
        # 页文本与页图、逐页解说词是**一一对应**的，渲染时才发现装不下就晚了（会截断/错位）
        page_texts = text_pages.split_pages(outline, aspect_ratio=task['aspect_ratio'])
        images = await text_pages.render_pages(
          page_texts, work, task.get('source_name', ''), aspect_ratio=task['aspect_ratio'],
        )
      else:
        images = await slides.ppt_to_images(task['source_path'], work)
        page_texts = await texts.page_texts_with_vision(task['source_path'], images)

    # ── 2. RITL 自检（先看再讲）──
    async with _stage(stages, 'ritl'):
      checks = await ritl.check_pages(images, work)
    bad = [n for n, r in checks.items() if not r.get('ok')]
    if bad:
      logger.warning('自检发现 %d 页疑似有问题: %s', len(bad), bad)

    # ── 3. 逐页解说词（配音语言与字幕语言不同时一次调用出两份）──
    await store.set_status(task_id, store.STATUS_NARRATION, progress=15)
    async with _stage(stages, 'narration'):
      scripts = await narration.generate_scripts(
        page_texts, language=task['language'],
        subtitle_language=task.get('subtitle_language', ''),
        # 目标时长 → 每页字数上限（近似控制篇幅；成片实际时长由 TTS 决定）
        max_chars=narration.max_chars_for_target(
          float(task.get('target_minutes') or 0), len(page_texts), task['language'],
        ),
        requirements=task.get('requirements', ''),
      )

    pages = _build_pages(images, page_texts, scripts, checks)
    await store.set_pages(task_id, pages, page_count=len(images))

    # ── 4. 逐页配音 ──
    async with _stage(stages, 'tts'):
      pages = await _synthesize_pages(
        task_id, pages, work, task['voice'],
        speech_language=narration.speech_language(task['language']),
      )
    await store.set_pages(task_id, pages)

    # ── 5. 合成 ──
    await store.set_status(task_id, store.STATUS_COMPOSING, progress=95)
    async with _stage(stages, 'compose'):
      video = await _compose_all(task_id, pages, work, task['aspect_ratio'],
                                motion=task.get('motion', ''),
                                subtitles=task.get('subtitles', True))
    await store.set_video(task_id, str(video), duration=compose.total_duration(pages))
    # 清理旧任务的中间产物（保留成片）：放在任务成功之后、best-effort——清理失败不影响交付
    try:
      keep = int(settings.VIDEO_AGENT_KEEP_FULL_TASKS)
      if keep > 0:
        await store.prune_old_intermediates(task['thread_id'], keep=keep)
    except Exception as e:  # noqa: BLE001
      logger.warning('清理旧任务中间产物异常（忽略）: %s', e)
    logger.info('讲解视频完成: task=%s 页数=%d 时长=%.1fs | 分阶段 %s',
                task_id, len(pages), compose.total_duration(pages), stages.summary())
  except Exception as e:  # noqa: BLE001 - 后台任务，必须自己收尾
    # 失败也把已完成阶段的耗时带出来——"卡在哪一步"通常比异常栈更直接
    logger.exception('讲解视频失败: task=%s | 分阶段 %s', task_id, stages.summary())
    await store.mark_failed(task_id, f'{type(e).__name__}: {e}')


async def rerun_pages(task_id: str, page_numbers: list[int] | None = None) -> dict:
  """只重跑指定页（默认全部）：解说词可选重写 → 配音 → 该页片段 → 重新 concat。

  用于二次修改（改某页文案、换语言/音色）与修复自检发现的问题页。
  """
  task = await store.get_task(task_id)
  if not task:
    raise ValueError(f'任务不存在: {task_id}')
  work = store.work_dir(task['thread_id'], task_id)
  pages: list[dict] = task.get('pages') or []
  if not pages:
    raise ValueError('任务还没有页面产物，无法重跑（请等待首次生成完成）')

  targets = {int(n) for n in (page_numbers or [p['index'] for p in pages])}
  selected = [p for p in pages if p['index'] in targets]
  if not selected:
    raise ValueError(f'指定的页不存在: {sorted(targets)}')

  # **先把状态拨离 success**：否则"重跑刚开始那一小段"任务仍显示 success + 旧 video_url，
  # 前端轮询会据此补卡片并开始播——而此刻成片正在被重写（实测：读到半成品导致加载失败）
  await store.set_status(task_id, store.STATUS_TTS, progress=55,
                         progress_text=f'准备重跑 {len(selected)} 页…')

  # 重新写解说词（语言/音色可能已变），再配音 + 重编该页
  texts_for_pages = [p.get('text', '') for p in selected]
  scripts = await narration.generate_scripts(
    texts_for_pages, language=task['language'],
    subtitle_language=task.get('subtitle_language', ''),
    max_chars=narration.max_chars_for_target(
      float(task.get('target_minutes') or 0), len(texts_for_pages), task['language'],
    ),
    requirements=task.get('requirements', ''),
  )
  for p, s in zip(selected, scripts):
    if s['script']:
      p['script'] = s['script']
      p['subtitle'] = s.get('subtitle') or s['script']
      p['status'] = store.PAGE_PENDING
      p['error'] = s.get('error', '')
    await store.update_one_page(task_id, p['index'], {
      'script': p['script'], 'subtitle': p.get('subtitle', ''),
      'status': p['status'], 'error': p['error'],
    })

  await store.set_status(task_id, store.STATUS_TTS, progress=60,
                         progress_text=f'重跑 {len(selected)} 页配音…')
  await _synthesize_pages(task_id, selected, work, task['voice'], progress_base=60,
                          speech_language=narration.speech_language(task['language']))

  await store.set_status(task_id, store.STATUS_COMPOSING, progress=95,
                         progress_text=f'重合成视频（{len(selected)} 页）…')
  video = await _compose_all(task_id, pages, work, task['aspect_ratio'], reuse=True,
                            motion=task.get('motion', ''),
                            subtitles=task.get('subtitles', True))
  await store.set_video(task_id, str(video), duration=compose.total_duration(pages))
  return {'ok': True, 'task_id': task_id, 'pages': sorted(targets),
          'video_path': str(video), 'duration': compose.total_duration(pages)}
