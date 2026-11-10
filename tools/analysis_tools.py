"""数据分析工具 — 全量数据物化 + 沙箱执行（DifySandbox/DeerFlow 模式）。

两个始终可见的 async 工具（走 raw_tools，不经同步异常包装）：

1. analysis_load — 调平台能力分页拉**全量**数据，落盘为数据集（CSV+meta），
   只向 LLM 返回摘要（dataset_id / rows / columns / 前几行预览），原始数据
   永不进入上下文。绕过 tools_factory 的 page_size 封顶，受
   AGENT_ANALYSIS_MAX_ROWS 上限保护。
2. analysis_exec — 在受限子进程跑用户 pandas 代码（预载 df），只返回计算结果。

用法边界（description 已告知 LLM）：单条/少量查询用平台能力工具；需要对
全量/大量数据做统计、聚合、趋势、分布分析时，先用 analysis_load 物化数据，
再用 analysis_exec 写 pandas 代码计算。
"""

import asyncio
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.agent.user_context import get_current_user_id
from core.sandbox.datasets import save_dataset
from core.sandbox.runner import run_python
from service.query.base.platform import get_capability, get_caller
from tools.analysis_file_tools import (
  _dataset_dir,
  _get_thread_id,
  _resolve_dataset_path,
  build_file_tools,
)

logger = logging.getLogger(__name__)

# 响应里可能的记录列表键（按优先级探测；实现时可按实际平台响应微调）
_RECORD_KEYS = ('list', 'records', 'rows', 'items', 'data', 'result')
# 响应里可能的总数键
_TOTAL_KEYS = ('total', 'totalCount', 'count', 'pageCounts')


async def _emit_status(tool: str, message: str) -> None:
  """向前端发阶段进度事件（纯反馈，失败绝不能影响工具执行）。"""
  try:
    from langchain_core.callbacks import adispatch_custom_event
    await adispatch_custom_event('analysis_status', {'tool': tool, 'message': message})
  except Exception:
    pass


_ANALYSIS_SLOT_KEY = 'agent:analysis:slots'
_ANALYSIS_SLOT_WAIT_SECONDS = 120     # 等槽位上限，超时 fail-open（不把交互卡死）
_ANALYSIS_SLOT_POLL_SECONDS = 0.5
_ANALYSIS_SLOT_LEASE_TTL = 900        # 计数键租约：worker 被 kill 漏掉的槽位最迟此时自愈


class _AnalysisGate:
  """analysis_exec 跨进程并发闸（Redis 计数；超时/Redis 故障一律 fail-open）。

  为什么需要：沙箱内存放宽到 4GB 后，生产 4 个 worker × 多会话同时跑 pandas 会成倍
  叠加，把宿主内存顶爆。只闸真正吃内存的子进程，不闸分页 I/O 的 analysis_load
  （串行化会明显拖慢交互）。
  """

  def __init__(self, limit: int, dist=None, redis_client=None, key: str = ''):
    self._limit = max(1, limit)
    self._dist = dist
    self._redis = redis_client
    self._key = key
    self._local = asyncio.Semaphore(self._limit)

  async def acquire(self) -> str:
    """返回占用方式（'redis' / 'local' / 'failopen'），release 时按它归还。"""
    if self._dist is None:
      await self._local.acquire()
      return 'local'
    deadline = time.monotonic() + _ANALYSIS_SLOT_WAIT_SECONDS
    while time.monotonic() < deadline:
      try:
        if await self._dist.try_acquire():
          await self._renew_lease()
          return 'redis'
      except Exception as e:            # Redis 抖动：放行，别让分析全部排队卡死
        logger.warning('[Analysis] 并发闸不可用，放行: %s', e)
        return 'failopen'
      await asyncio.sleep(_ANALYSIS_SLOT_POLL_SECONDS)
    logger.warning('[Analysis] 等分析槽位超时，放行')
    return 'failopen'

  async def release(self, mode: str) -> None:
    if mode == 'redis':
      try:
        await self._dist.release()
      except Exception as e:
        logger.warning('[Analysis] 释放槽位失败（计数偏高，靠 TTL 自愈）: %s', e)
    elif mode == 'local':
      self._local.release()

  async def _renew_lease(self) -> None:
    """给计数键续 TTL：DistributedSemaphore 无过期，worker 被 kill 会永久漏槽位。"""
    if self._redis is None or not self._key:
      return
    try:
      await self._redis.expire(self._key, _ANALYSIS_SLOT_LEASE_TTL)
    except Exception:
      pass


_analysis_gate: _AnalysisGate | None = None


def _get_analysis_gate() -> _AnalysisGate:
  """惰性构建分析并发闸（首次用到时连一次 Redis；失败降级进程内）。"""
  global _analysis_gate
  if _analysis_gate is not None:
    return _analysis_gate
  limit = max(1, settings.AGENT_ANALYSIS_MAX_CONCURRENCY)
  try:
    import redis.asyncio as aioredis

    from config.redis_conn import redis_manager
    from core.scheduler.semaphore import DistributedSemaphore

    client = aioredis.Redis(
      host=settings.REDIS_HOST, port=settings.REDIS_PORT,
      password=settings.REDIS_PASSWORD or None, db=settings.REDIS_DB,
      decode_responses=True, socket_timeout=5,
    )
    key = redis_manager.key(_ANALYSIS_SLOT_KEY)
    _analysis_gate = _AnalysisGate(
      limit, dist=DistributedSemaphore(client, key, limit), redis_client=client, key=key,
    )
    logger.info('[Analysis] 分析并发闸就绪：跨进程 Redis 限 %d', limit)
  except Exception as e:
    logger.warning('[Analysis] 并发闸降级为进程内限 %d: %s', limit, e)
    _analysis_gate = _AnalysisGate(limit)
  return _analysis_gate


def _extract_records(resp) -> tuple[list, str | None]:
  """从平台响应里通用抽取记录列表。"""
  if isinstance(resp, list):
    return resp, None
  if isinstance(resp, dict):
    for key in _RECORD_KEYS:
      value = resp.get(key)
      if isinstance(value, list) and value:
        return value, key
    # fallback：取首个非空 list 值
    for value in resp.values():
      if isinstance(value, list) and value:
        return value, None
  return [], None


_SCRIPT_RE = re.compile(r'<script\b([^>]*)>([\s\S]*?)</script>', re.IGNORECASE)


def _validate_html_artifact(html_text: str) -> str | None:
  """校验自包含 HTML 看板的内联脚本语法，返回首个错误描述（无错返回 None）。

  JSON 块（type="application/json"）用 json.loads 校验；内联 JS 用 node --check 只查语法
  （不执行，故 echarts/document 未定义不会被误报）。node 不可用时降级跳过（返回 None）。
  校验失败的信息会经 analysis_exec 返回给 LLM，触发其重写修复（自愈闭环）。
  """
  node = shutil.which('node')
  for idx, (attrs, body) in enumerate(_SCRIPT_RE.findall(html_text), 1):
    attrs_lower = attrs.lower()
    if 'src=' in attrs_lower:
      return f'第 {idx} 个 <script> 引用了外部脚本（禁止外部 src，看板必须自包含）'
    body_stripped = body.strip()
    if not body_stripped:
      continue
    if 'application/json' in attrs_lower:
      try:
        json.loads(body_stripped)
      except Exception as e:
        return f'第 {idx} 个 <script>（JSON 数据块）解析失败：{e}'
      continue
    if node is None:
      continue
    try:
      with tempfile.NamedTemporaryFile(
        mode='w', suffix='.js', encoding='utf-8', delete=False
      ) as f:
        f.write(body_stripped)
        tmp = f.name
      try:
        proc = subprocess.run(
          [node, '--check', tmp], capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
          err = (proc.stderr or '').strip() or (proc.stdout or '').strip()
          snippet = body_stripped[:400]
          return f'第 {idx} 个 <script> JS 语法错误：\n{err}\n\n代码片段：\n{snippet}'
      finally:
        try:
          os.unlink(tmp)
        except OSError:
          pass
    except (OSError, subprocess.SubprocessError) as e:
      logger.warning('node --check 校验 HTML 脚本失败：%s', e)
  return None


def _is_blank_image(path: str) -> bool:
  """粗略判断图片是否空白（全透明 / 纯单色），防模型交付空图。

  空白典型成因：空数据画了空画布、没注册中文字体、保存成 transparent 背景。
  判定失败（读不了/非图）返回 False，不误拦。只拦截明显空图，不拦有内容图。
  """
  try:
    from PIL import Image
    with Image.open(path) as im:
      # 全透明（空画布 + transparent=True 保存）→ 空图
      if im.mode in ('RGBA', 'LA', 'P') or 'transparency' in im.info:
        alpha = im.convert('RGBA').getchannel('A')
        if alpha.getextrema() == (0, 0):
          return True
      rgb = im.convert('RGB')
      colors = rgb.getcolors(maxcolors=1 << 20)
      if not colors:
        return False  # 颜色种类多，非空白
      total = sum(c for c, _ in colors)
      top = max(c for _, c in colors)
      return len(colors) <= 1 or top / total > 0.999
  except Exception:
    return False


_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}

# 交互表格视图：少于这个行数不做整列空值校验（小数据误伤概率高）
_BLANK_SCAN_MIN_ROWS = 5
_EMPTY_CELLS = {'', 'nan', 'NaN', 'none', 'None', 'null', 'NULL', 'NaT'}


def _blank_columns(path: str) -> list[str]:
  """返回 view.csv 里**整列全空**的列名（读不了/列数不足返回空列表，不误拦）。

  只卡「100% 为空」——不卡「绝大多数为空」，避免误伤业务上本来就稀疏的列（如审批人）。
  典型的整列空是映射写错：编码列在数据里是 int64，而 map 的 key 写成字符串，
  匹配不上就整列 NaN（见 core/sandbox/datasets.py 回传 dtypes 的说明）。
  """
  try:
    import csv
    with open(path, newline='', encoding='utf-8') as f:
      rows = list(csv.DictReader(f))
  except Exception:
    return []
  if len(rows) < _BLANK_SCAN_MIN_ROWS:
    return []
  cols = list(rows[0].keys())
  blanks: list[str] = []
  for c in cols:
    if all(str(r.get(c, '')).strip() in _EMPTY_CELLS for r in rows):
      blanks.append(c)
  return blanks


def _extract_total(resp) -> int | None:
  """从响应里尝试取总数，取不到返回 None。"""
  if not isinstance(resp, dict):
    return None
  for key in _TOTAL_KEYS:
    try:
      return int(resp.get(key))
    except (TypeError, ValueError):
      continue
  return None


def _too_many_rows(total: int | None, max_rows: int, loaded: int = 0) -> str:
  """超过单次分析上限 → 拒绝物化，返回可被模型如实转述的 error。

  为什么拒绝而不是截断（2026-10-26 事故）：截断样本是「按时间倒序的最近一段」，
  模型会把它当全量算分布/占比，图和数值都是错的（当日 13.5 万条告警只取到最近 1.5 小时）。
  只给 `truncated` 标记靠提示词约束不可靠，必须在工具层拦住。
  """
  n = total if total is not None else loaded
  return json.dumps({
    'error': (
      f'数据源共 {n} 条，超过单次分析上限 {max_rows} 条，未执行物化与分析。'
      '请向用户说明数据量过大，请其指定更窄的时间范围或增加筛选条件'
      '（如园区 / 机房 / 告警级别 / 具体时段）后重试；'
      '不要自行缩小范围后直接出图或统计。'
    ),
    'total_rows': total,
    'max_rows': max_rows,
  }, ensure_ascii=False)


def _render_shapes(roles: dict) -> str:
  """把列角色渲染成「本数据集能算哪些分析形状」，挂在 analysis_load 返回值上。

  实测模型的实际顺序是「先把数算完 → 再去找图型规范」：它调 skill.chart 是在
  analysis_exec 之后，那时数据已定型成「类别+计数」，选型引导再全也用不上。
  所以把"还能怎么算"放在**写聚合之前**——analysis_load 是必经点，且必然早于任何
  analysis_exec（对齐 DeerFlow data-analysis 技能的 Step2 inspect schema → Step3 按
  schema 写查询）。
  """
  cats = list(roles.get('category') or [])
  nums = list(roles.get('numeric') or [])
  times = list(roles.get('time') or [])
  if not (cats or nums or times):
    return ''
  parts = []
  if cats:
    parts.append(f'分类列={" / ".join(cats[:8])}{"…" if len(cats) > 8 else ""}')
  if times:
    parts.append(f'时间列={" / ".join(times[:4])}')
  if nums:
    parts.append(f'数值列={" / ".join(nums[:8])}{"…" if len(nums) > 8 else ""}')
  shapes = []
  if cats:
    shapes.append('单列分布（分类列计数）→ column / bar / pie')
  if len(cats) >= 2:
    shapes.append(f'交叉对比（{cats[0]} × {cats[1]}）→ 堆叠 column / 透视表')
  if times:
    shapes.append(f'时间趋势（按 {times[0]} 分组）→ line / area')
  if nums:
    shapes.append(f'数值分布（{nums[0]} 分箱）→ histogram / boxplot')
  if len(nums) >= 2:
    shapes.append(f'数值关系（{nums[0]} × {nums[1]}）→ scatter / dual_axes')
  if cats and len(nums) >= 2:
    shapes.append(f'多指标对比（按 {cats[0]} 分组）→ radar')
  return (
    '【写聚合前先规划分析形状，不要只对分类列做 value_counts（那只出得了柱/条/饼）】\n'
    + '；'.join(parts) + '\n'
    + '本数据集可算的形状：' + '；'.join(shapes) + '\n'
    + '**至少覆盖两类不同形状**（如 单列分布 + 时间趋势/数值分布/交叉），'
      '不要 N 张图全是单列分布。一次 analysis_exec 可同时算出多个形状'
      '（result={"趋势": …, "分布": …}），算完再统一出图，图型须覆盖 ≥2 类。'
  )


# ── 出图选型卡（决策点引导）──────────────────────────────────────────────────
# 根因：模型写 <chart> 标签前最后一个必经 ToolMessage 是 analysis_exec 的返回，而技能
# 正文（skill__chart）是可选调用、qwen3.5 不主动调（实测 5 天 1 次，且 4/4 次晚于
# analysis_exec，形状已定型）。把「形状识别 + 形状→图型速查 + 会话图型计数 + 硬规则」
# 挂在 chart_data 返回上，正好落在决策时刻。

# 会话已出图形状计数：{thread_id: {形状: 次数}}；只用于选型卡文案语气（多 worker 下
# 可能偏小，可接受），上限防内存泄漏
_CHART_STATS: dict[str, dict[str, int]] = {}
_CHART_STATS_MAX_SESSIONS = 500

# chart_data 跨 exec 累积：{(thread_id, dataset_id): {组名: 聚合结果}}
#
# 为什么要它：沙箱**每次 exec 都是新子进程**，`result` 只取最后一次。模型分两次算时，先算好的
# 维度会被后一次的 result 覆盖掉——2026-09-18 实测：exec#1 算过
# `df.groupby(['dcName','变更类型']).size()`，exec#2 重算单列后组装 result 没带交叉 →
# 那条维度**静静消失**（无图、无文字、无提示）。这里把上一次的聚合按数据集记住，本次缺的键补回来。
# 只按 (会话, 数据集) 累积聚合结果，不累积 stdout/artifacts；换数据集/新会话即换 key。
_CHART_DATA_CACHE: dict[tuple, dict] = {}
_CHART_DATA_CACHE_MAX = 200

# 同一次回答里同一类图的上限（对齐系统提示词约束 9：column/bar 一类、pie/donut 一类、line/area 一类）。
# 选型卡按「本次 chart_data 里同形状的**组数**」施压——一次 exec 出 4 张同类图正是要拦的情形。
MAX_SAME_TYPE_PER_ANSWER = 2

# 形状 → 候选图型速查（与 skills/chart/SKILL.md 选型引导对齐的紧凑版）
_SHAPE_CHART_HINT = (
  '形状→图型速查：时间趋势→line/area/dual_axes；'
  '类别对比→column 或 bar（纵向/横向等价，按标签长短自选，不要默认只用 bar；'
  '字段写法同为 xField=类别列 yField=数值列，bar 的转置由前端处理）；'
  '占比→pie；两分类交叉→堆叠column（groupField+stacked）；数值分布→histogram/boxplot/violin；'
  '两数值关系→scatter；多指标→radar；流程转化→funnel/sankey；层级→treemap；'
  '词频→word_cloud；进度→liquid。'
)

_TIME_LIKE_RE = re.compile(r'\d{4}[-/年]\d{1,2}|\d{1,2}月|\d{4}年')


def _is_number(v) -> bool:
  return isinstance(v, (int, float)) and not isinstance(v, bool)


def _detect_shape(chart_data) -> str:
  """轻量启发式识别 chart_data 的数据形状，用于选型卡针对性引导。识别不了返回 '其他'。"""
  rows = None
  if isinstance(chart_data, list) and chart_data and isinstance(chart_data[0], dict):
    rows = chart_data
  elif isinstance(chart_data, dict):
    # dict 承载多形状（result={'趋势':…,'分布':…}）时取首个 list 值识别
    for value in chart_data.values():
      if isinstance(value, list) and value and isinstance(value[0], dict):
        rows = value
        break
  if not rows:
    return '其他'
  first = rows[0]
  keys = list(first.keys())
  if len(keys) < 2:
    return '其他'
  col_types = []
  for key in keys:
    values = [row.get(key) for row in rows[:10]]
    if all(_is_number(v) for v in values if v is not None):
      col_types.append('num')
    elif all(isinstance(v, str) and _TIME_LIKE_RE.search(v) for v in values if v):
      col_types.append('time')
    else:
      col_types.append('cat')
  n_num = col_types.count('num')
  n_cat = col_types.count('cat')
  if col_types and col_types[0] == 'time' and n_num >= 1:
    return '时间序列'
  if len(keys) == 2 and n_cat >= 1 and n_num == 1:
    return '类别计数'
  if len(keys) == 3 and n_cat == 2 and n_num == 1:
    return '交叉'
  if n_num >= 2 and n_cat == 0:
    return '数值关系'
  return '其他'


def _detect_shapes(chart_data) -> dict[str, int]:
  """逐组识别 chart_data 的形状，返回 {形状: 组数}。

  与"一次 exec 只记一个形状"不同：一次 exec 常返回多个 key（多组聚合），必须按**每一组**统计——
  否则"4 组同类计数"只记成「类别计数×1」，模型的"该类图超限"压力被摊薄（一次 exec 出 4 张同类图
  正是要拦的情形，2026-09-18 实测）。
  """
  groups: list = []
  if isinstance(chart_data, list):
    groups = [chart_data]
  elif isinstance(chart_data, dict):
    groups = [v for v in chart_data.values() if isinstance(v, list)]
    if not groups:  # 不是 {名: 列表} 形态时当单组处理
      groups = [chart_data]
  out: dict[str, int] = {}
  for g in groups:
    shape = _detect_shape(g)
    out[shape] = out.get(shape, 0) + 1
  return out or {'其他': 1}


def _record_chart_shape(thread_id: str, shapes: dict[str, int]) -> dict[str, int]:
  """把本次各组形状计入本会话累计，返回累计计数（作为"本会话已算/已出图"的代理）。"""
  stats = _CHART_STATS.setdefault(thread_id, {})
  if len(_CHART_STATS) > _CHART_STATS_MAX_SESSIONS:
    # 粗暴防泄漏：超上限丢最早一批（会话计数只影响文案语气，不值得 LRU 精细化）
    _CHART_STATS.pop(next(iter(_CHART_STATS)))
  for shape, n in shapes.items():
    stats[shape] = stats.get(shape, 0) + n
  return dict(stats)


def _merge_chart_data(thread_id: str, dataset_id: str, chart_data):
  """把本次 chart_data 与「上一次同会话同数据集」的结果合并：本次缺的键**补回来**（同名以本次为准）。

  返回 `(merged, restored)`：`restored` 是"上次有、本次没有"的组名——正是被后一次 exec 覆盖掉的
  维度。调用方据此在 payload 里点名，否则它们会无声消失（2026-09-18 实测「园区×类型交叉」就是这样丢的）。
  非 dict 形态（如直接是列表）不做跨次合并，只原样记住。
  """
  key = (thread_id, dataset_id)
  prev = _CHART_DATA_CACHE.get(key)
  restored: list[str] = []
  merged = chart_data
  if isinstance(prev, dict) and isinstance(chart_data, dict):
    merged = dict(chart_data)
    for name, value in prev.items():
      if name not in merged:
        merged[name] = value
        restored.append(name)
  if len(_CHART_DATA_CACHE) >= _CHART_DATA_CACHE_MAX and key not in _CHART_DATA_CACHE:
    # 粗暴防泄漏：超上限丢最早一个（只影响"跨次补回"这一层兜底，不值得 LRU 精细化）
    _CHART_DATA_CACHE.pop(next(iter(_CHART_DATA_CACHE)))
  _CHART_DATA_CACHE[key] = merged if isinstance(merged, dict) else chart_data
  return merged, restored


def _render_chart_card(shapes: dict[str, int], session_stats: dict[str, int]) -> str:
  """渲染出图选型卡：挂在 analysis_exec 返回里，落在模型组装 <chart> 标签前的决策时刻。"""
  batch = '、'.join(f'{s}×{n}' for s, n in shapes.items())
  lines = [f'【出 <chart> 前必读】当前可出图的 chart_data 形状：{batch}']
  heavy = [f'{s}×{n}' for s, n in shapes.items() if n > MAX_SAME_TYPE_PER_ANSWER]
  if heavy:
    # 本次就同形状多组 —— 一次 exec 里出 4 张同类图的最常见来源，直接点名
    lines.append(
      f'⚠️ 本次有 {"、".join(heavy)} 组**同形状**——**同一次回答里同一类图最多 '
      f'{MAX_SAME_TYPE_PER_ANSWER} 张**（column/bar 算一类、pie/donut 算一类、line/area 算一类）：'
      f'只能挑最有信息量的 {MAX_SAME_TYPE_PER_ANSWER} 组出图，其余改用表格/文字，'
      '或先补算别的形状（时间趋势/数值分布/交叉/关系）再出。'
    )
  elif '类别计数' in shapes or '交叉' in shapes:
    lines.append(
      '⚠️ 这个形状直接出图只能是 column/bar/pie。若 analysis_load 返回的 analysis_shapes '
      '里还有未算的形状（时间趋势/数值分布/交叉/关系），先再跑一次 analysis_exec 算出'
      '（result 用 dict 承载多形状）再出图；数据里有数值字段（耗时/金额/次数）时'
      '禁止丢掉只做 value_counts。'
    )
  if session_stats:
    counts = '、'.join(f'{s}×{n}' for s, n in session_stats.items())
    lines.append(f'本会话累计已算形状：{counts} → 再出图优先换形状（对照下方速查），不要又是柱/饼。')
  lines.append(_SHAPE_CHART_HINT)
  return '\n'.join(lines)


@tool
async def analysis_load(
  capability_id: str,
  params: dict | None = None,
  dataset_name: str = 'data',
  config: RunnableConfig = None,
) -> str:
  """把平台能力的数据全量物化为数据集，供沙箱分析或做右侧可交互表格。

  两类使用场景：
  1. **统计分布/占比/汇总/趋势**（如「XX部门人员专业分布」「各状态工单占比」
     「各专业人数」）：物化全量数据后，用 analysis_exec 聚合出 chart_data 出图，
     禁止拿单页明细工具的结果当样本统计。
  2. **查看/列出超过 20 条的全部明细**（如「列出全部 200 条」「把所有工单都列出来」）：
     物化全量数据；先用 analysis_exec 把 df 整理成可读视图（字段改中文/保留关键列/
     枚举解码，存 view.csv）——整理成功后 analysis_exec 会返回 records_url，用它输出
     <file url=.../>，右侧表格即展示整理后的可读内容（本工具只回数据集摘要，明细不
     出现在上下文里）。**本工具不再直接给 records_url**，跳过整理就拿不到 URL、也就
     出不了表格卡片。

  仅当参数范围能覆盖全量数据时使用；只查单条/少量（≤20 条）明细用平台能力工具直接列即可。
  **结果超过单次上限（AGENT_ANALYSIS_MAX_ROWS）时直接拒绝并返回 error**，不会物化截断
  样本 —— 此时请让用户缩小时间范围或增加筛选条件后重试。

  Args:
    capability_id: 平台能力 id，如 'mgmt.query_breakdown_ticket'（点号形式）；
      也兼容工具名形式 'mgmt__query_breakdown_ticket'（双下划线，自动转换）
    params: 查询筛选参数（除分页外，如 uid/dept_id/company_id/year 等），
      分页由工具自动翻页拉全量
    dataset_name: 数据集展示名，用于 dataset_id 前缀
  """
  thread_id = _get_thread_id(config)

  # 兼容两种写法：模型手里拿到的常是工具名（mgmt__query_breakdown_ticket，双下划线），
  # 能力表按点号 capability_id（mgmt.query_breakdown_ticket）注册 → 归一后再查。
  from service.query.base.tools_factory import tool_name_to_capability
  cap_id = capability_id if '.' in capability_id else tool_name_to_capability(capability_id)

  cap = get_capability(cap_id)
  if cap is None:
    return json.dumps({
      'error': f'未知能力 {cap_id}，请先从分类清单确认可用的能力 id',
    }, ensure_ascii=False)
  caller = get_caller(cap_id)
  if caller is None:
    return json.dumps({'error': f'能力 {cap_id} 无可用后端'}, ensure_ascii=False)

  max_rows = settings.AGENT_ANALYSIS_MAX_ROWS
  page_size = settings.AGENT_ANALYSIS_PAGE_SIZE

  await _emit_status('analysis_load', '正在物化数据…')
  records: list[dict] = []
  page = 1
  truncated = False
  total: int | None = None
  while True:
    if len(records) >= max_rows:
      truncated = True
      break
    resp = await caller(cap_id, {
      **(params or {}),
      'page': str(page),
      'page_size': str(page_size),
    }, on_behalf_of=get_current_user_id())
    page_records, _ = _extract_records(resp)
    if not page_records:
      break
    records.extend(page_records)
    total = _extract_total(resp)
    if total is not None:
      if total > max_rows:
        # 首页就拿到总数：直接拒绝，别再翻几十页把内存和时间浪费在截断样本上
        return _too_many_rows(total, max_rows)
      if len(records) >= total:
        break
    elif len(page_records) < page_size:
      break
    page += 1
    # 工具执行期间事件流默认静默（直到 on_tool_end），全量分页可能超过
    # AGENT_EVENT_TIMEOUT 触发整轮超时；周期发进度既防静默超时，又给用户实时反馈
    if records and page % 5 == 0:
      await _emit_status('analysis_load', f'正在物化数据…已加载 {len(records)} 行')

  if not records:
    return json.dumps({
      'error': '该查询未返回数据，请调整筛选参数后重试',
    }, ensure_ascii=False)
  if truncated:
    # 兜底：接口不返回 total 时只能靠触顶判断 —— 同样拒绝，不物化截断样本
    return _too_many_rows(total, max_rows, len(records))

  await _emit_status('analysis_load', f'数据已就绪（{len(records)} 行）')
  info = save_dataset(thread_id, cap_id, params or {}, records, dataset_name)
  if total is not None:
    info['total_rows'] = total
  # 分析形状引导：必须在模型写聚合**之前**到达（实测它先算 value_counts 再去找图型规范，
  # 那时形状已定型）。单列成字段而不是塞进 note，免得被那一大段说明淹没。
  shapes = _render_shapes(info.get('column_roles') or {})
  if shapes:
    info['analysis_shapes'] = shapes
  # 刻意不在这里发 records_url：卡片 URL 与「整理视图」强绑定——只有 analysis_exec 把
  # view.csv 整理好后才发。否则模型拿到 URL 就能跳过整理直接出卡，表格全是英文原始列名。
  info['note'] = (
    '数据已物化到沙箱，原始记录未进入上下文。'
    '**注意字段类型（见返回的 dtypes）**：状态/专业/级别等编码列在数据里是**整数**（int64），'
    '写 code→中文 的映射时 key 要用整数（{1: ...}），或先 astype(str) 再 map——'
    "写成 {'1': ...} 会整列匹配不上变空。"
    '若用户是要查看/浏览完整列表或明细：先调用一次 analysis_exec 用 pandas 把数据整理成'
    '可读视图——字段名改成中文、只保留有意义的关键列、状态/类型等编码列解码成中文标签；'
    '保留的每一列都必须给中文表头，拿不准含义的列宁可不保留、禁止残留英文列名'
    '（依据你掌握的字段含义与枚举映射），把整理结果存为 '
    'df.to_csv(\'view.csv\', index=False)；右侧表格会优先展示 view.csv 的整理结果，'
    '因此不要直接展示原始英文字段。**先看本工具返回的预览样例确认字段真实结构与取值，'
    '再写重命名/取值/映射（不要凭猜测硬编码字段 key 或按数字硬转）；整理后必须自检：'
    '保留的列若整列（或绝大多数）为空，说明取值/映射写错 → 修正映射或用原始列兜底，'
    '绝不允许交付整列为空的列；不确定的列宁可删掉。**'
    '对象型列（如 发起人/审批人 是 {userId,name,headimg} 这类 JSON）'
    '只保留其中的名称字段（name/姓名/label），不要整段输出 JSON；'
    '只含内部 ID 的列（列名含 id/uid/编号、值是不可读的长数字且没有配套名称列）不保留，'
    '部门/公司 ID 有名称列就用名称展示。'
    '枚举码列（如 专业 specialty、状态 status、阶段 stage、类型 type 等）整理时按业务含义'
    '转成中文，不要保留纯数字码。已知稳定映射：专业 specialty 1=供配电/2=制冷/4=智能化/8=其他；'
    '故障阶段 stage -1=驳回期/7=故障派发/2=故障处理/3=故障验收/5=故障关闭；其它枚举按你从该能力描述见过的'
    'code→含义转（不确定的列宁可去掉也别留数字码）。'
    '（视图文件固定命名 view.csv、会被自动剔除，'
    '不要另存成别的文件名当下载产物；不要询问用户是否要生成表格。）'
    '**整理成功后 analysis_exec 的返回里才会带 records_url（未整理则不给）**，'
    '用它输出一行 <file url="<records_url>" name="<能力名>列表"/>，用户即可在右侧查看。'
    '物化成交互表是列表的必需一步、不属于“自动分析”；出完表就此结束，'
    '不要再基于已物化的数据自动做统计/多维度分析或出图。'
    '仅当用户明确要求统计/分布/占比/汇总/对比/趋势等分析（或处于全量分析模式）时，'
    '才调用 analysis_exec 聚合出 chart_data 出图。'
  )
  return json.dumps(info, ensure_ascii=False)


@tool
async def analysis_exec(
  dataset_id: str,
  python_code: str | None = None,
  file: str | None = None,
  config: RunnableConfig = None,
) -> str:
  """在受限沙箱中执行 pandas 代码分析已物化的数据集。

  两种提供代码的方式（二选一）：
  - python_code：直接给一小段代码，适合临时/简单计算；
  - file：先 analysis_write_file 把脚本落盘（如 report.py），再传 file='report.py'
    执行，配合 analysis_str_replace 局部改代码，适合几十上百行的复杂产物脚本
    （多页 PPT / 带排版的 PDF 报告），省 token 且精准修改。

  代码内可用预载变量 `df`（DataFrame）。print() 输出文字结果；如需图表数据，
  请把最终结果赋值给变量 `result`（DataFrame / Series / list[dict] / dict）。
  **一次执行尽量算完全部所需聚合**：把多组结果放进一个 dict 赋给 `result`
  （键=用途名，值可为 DataFrame/Series/records），如
  `result = {'专业分布': 表1, '级别分布': 表2}`——chart_data 会按该 dict 原样返回，
  供多处 <chart> 引用；**不要为每张图各调一次 analysis_exec**（每次都是新子进程，很慢）。
  组装 <chart> 标签时原样引用数值，禁止改写。
  **写聚合前必须先定图型，再按图型备数据**：先看 analysis_load 返回的 columns 与预览样例，
  确认有哪些时间列/数值列/分类列，按分析目的选好图型，再写产出该数据形状的代码——
  不要先跑 value_counts 看手里剩什么形状，那只出得了柱/条/饼。对照表（分析目的 → 数据形状 → 写法 → 图型）：
  - 时间趋势：时间 + 数值两列，按时间升序 `df.groupby('日期')...sort_values('日期')` → line/area
  - 类别对比/占比：类别 + 数值 `value_counts()` → column/bar/pie（**仅纯类别对比/占比场景用**）
  - 两个分类维度交叉：每组一行三列 `df.groupby(['A','B']).size().reset_index(name='数量')` → 堆叠/分组 column
  - 数值分布：一列数值，先分箱 `pd.cut(df['耗时'], bins=8)` 或保留原值 → histogram/boxplot/violin
  - 两个数值字段关系：两列数值 `df[['温度','负载']].dropna()` → scatter/dual_axes
  - 多指标对比：每个对象一行、多指标多列 → radar
  - 流向/转化：源 + 目标 + 数值三列 `df.groupby(['来源','去向']).size().reset_index(name='数量')` → sankey/funnel
  数据里有数值字段（耗时/温度/容量/金额/次数等）时优先出分布/关系类图，不要把数值丢掉只数类别。
  如需生成可下载的产物文件（CSV/Excel/图表图片/PPT/PDF），在代码里把文件写到
  当前目录（如 df.to_csv('xxx.csv')、fig.savefig('xxx.png')），工具会自动回传，
  返回的 artifacts 列表里每条有 url 和 name——组装 <file url="..." name="..."/>
  标签时原样引用，禁止改写。**若只是把数据集整理成“右侧交互列表”的视图，文件必须固定命名
  view.csv（会被自动剔除、不作为下载产物），不要另存成别的 .csv 当交付。**
  **图片产物必须真实画出内容**：空数据画空画布、
  没按 RZ_CJK_FONT 注册中文字体、或以透明背景保存，都会得到空白图——工具会丢弃
  空白/全透明图片并要求重画，请确保背景不透明（plt.savefig(..., transparent=False)）
  且数据/文字确实绘制到画布上。计算应简洁（聚合/分布/趋势），不要打印原始全量数据。
  输出截断为上限字符。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    python_code: 待执行的 pandas 代码。**先定图型，再按该图型备数据**——只会 value_counts
      就只出得了柱/饼。示例：趋势 `result = df.groupby('日期').size().reset_index(name='数量')`；
      分布 `result = pd.cut(df['耗时'], bins=8).value_counts().reset_index(name='数量')`；
      关系 `result = df[['温度','负载']].dropna()`。
    file: 已落盘脚本的相对路径（与 python_code 二选一），如 'report.py'
  """
  thread_id = _get_thread_id(config)
  if bool(python_code) == bool(file):
    return json.dumps({
      'error': 'python_code 与 file 需二选一提供（一个为 None 另一个必填）',
    }, ensure_ascii=False)
  if file:
    resolved = _resolve_dataset_path(thread_id, dataset_id, file)
    if resolved is None:
      return json.dumps({'error': '数据集不存在或脚本路径非法'}, ensure_ascii=False)
    ds_dir, script = resolved
    if not script.is_file():
      return json.dumps({
        'error': f'脚本 {file} 不存在，请先用 analysis_write_file 落盘',
      }, ensure_ascii=False)
    # 非 .py 文件（如 dashboard.html）本身就是交付产物，直接返回鉴权下载 URL，
    # 不再当 Python 执行（否则 HTML 当脚本跑 → SyntaxError → 产物被吞）。
    if script.suffix.lower() != '.py':
      if script.suffix.lower() == '.html':
        try:
          html_text = script.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
          return json.dumps({'error': f'读取产物失败：{e}'}, ensure_ascii=False)
        invalid = _validate_html_artifact(html_text)
        if invalid:
          return json.dumps({
            'error': f'看板 HTML 内联脚本有语法错误，请用 analysis_write_file 修正后重试：\n{invalid}',
          }, ensure_ascii=False)
      try:
        size = script.stat().st_size
      except OSError as e:
        return json.dumps({'error': f'读取产物失败：{e}'}, ensure_ascii=False)
      max_mb = settings.AGENT_ANALYSIS_MAX_ARTIFACT_MB
      if max_mb and size > max_mb * 1024 * 1024:
        return json.dumps({'error': f'产物 {file} 超过 {max_mb}MB 上限'}, ensure_ascii=False)
      await _emit_status('analysis_exec', '正在生成产物…')
      url = (
        f'/api/v1/chat/agent/artifacts/{quote(thread_id)}/{quote(dataset_id)}/'
        f'{quote(script.name)}'
      )
      return json.dumps({
        'result': f'文件 {file} 已就绪',
        'artifacts': [{'name': script.name, 'url': url, 'size': size}],
      }, ensure_ascii=False)
    try:
      python_code = script.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
      return json.dumps({'error': f'读取脚本失败：{e}'}, ensure_ascii=False)
  else:
    # 走 _dataset_dir 而不是 load_dataset_dir：前者认 workspace（成文/文档类脚本的保留
    # 目录，不要求 data.csv），后者对它必抛「数据集不存在或已过期」——两条分支口径要一致
    # （file= 分支走的就是 _resolve_dataset_path → _dataset_dir）。
    ds_dir = _dataset_dir(thread_id, dataset_id)
    if ds_dir is None:
      return json.dumps(
        {'error': f'数据集 {dataset_id} 不存在或已过期。请先用 analysis_load 拉取数据，'
                  "或改用 dataset_id='workspace' 做无数据集的成文/文档类任务。"},
        ensure_ascii=False,
      )

  await _emit_status('analysis_exec', '正在分析…')
  # 沙箱子是真正吃内存的那一步：跨进程限住同时运行的子进程数（Redis 不可用则放行）
  gate = _get_analysis_gate()
  mode = await gate.acquire()
  try:
    result = await run_python(
      python_code,
      cwd=str(ds_dir),
      timeout=settings.AGENT_ANALYSIS_TIMEOUT,
      max_output_chars=settings.AGENT_ANALYSIS_MAX_OUTPUT_CHARS,
      max_memory_mb=settings.AGENT_ANALYSIS_MAX_MEMORY_MB,
      max_artifact_mb=settings.AGENT_ANALYSIS_MAX_ARTIFACT_MB,
      max_artifacts=settings.AGENT_ANALYSIS_MAX_ARTIFACTS,
      cjk_font_path=settings.RZ_CJK_FONT,
      extra_lib_path=settings.AGENT_ANALYSIS_EXTRA_LIB_PATH or None,
    )
  finally:
    await gate.release(mode)

  if result.get('artifacts'):
    await _emit_status('analysis_exec', '正在生成产物…')
  else:
    await _emit_status('analysis_exec', '分析完成')

  if result['timed_out']:
    return json.dumps({'error': result['error']}, ensure_ascii=False)
  if result['error'] and not result['stdout'].strip() and not result.get('artifacts'):
    detail = result['stderr'].strip()[-1500:]
    return json.dumps({'error': result['error'], 'stderr': detail}, ensure_ascii=False)

  out = result['stdout'].strip() or '（代码执行无输出）'
  if result['stderr'].strip():
    out += f'\n\n[stderr]\n{result["stderr"].strip()[:1000]}'

  # 产物文件 → 生成鉴权下载 URL（URL 由工具权威生成，LLM 只原样引用）。
  # 先校验图片产物：空白/全透明空图直接丢弃并在返回里说明，让 LLM 修正后重画
  # （自愈闭环，防交付空图；与 analysis_exec 的 HTML 校验同一思路）。
  artifacts = result.get('artifacts') or []
  if artifacts:
    kept: list = []
    dropped: list[str] = []
    for a in artifacts:
      name = a.get('name', '')
      if name.rsplit('.', 1)[-1].lower() in _IMAGE_EXTS:
        fp = os.path.abspath(os.path.join(str(ds_dir), name))
        if os.path.isfile(fp) and _is_blank_image(fp):
          dropped.append(name)
          continue
      kept.append(a)
    if dropped:
      out += (
        '\n\n[图片校验] 图片产物「' + '、'.join(dropped) + '」内容为空白/全透明空图，未交付。'
        '请修正绘图代码后重画再交付：确认真实把数据绘制到画布、中文按 RZ_CJK_FONT 注册字体、'
        '保存为不透明背景（如 plt.savefig(..., transparent=False)）。'
      )
    artifacts = kept
  # view.csv 是「整理后供 records 接口渲染」的保留文件，不是可下载产物，剔除避免重复卡片
  artifacts = [a for a in artifacts if a.get('name') != 'view.csv']

  # 交互表格视图自检：整列全空说明取值/映射写错（编码列是 int64、map key 写成字符串是
  # 最常见的一种）。这里把错误退回给模型让它重做——**不替它删列/补值**。校验发生在模型
  # 输出 <file records_url> 之前，所以用户不会先看到空表再看到修正。
  view_path = os.path.join(str(ds_dir), 'view.csv')
  if os.path.isfile(view_path):
    blanks = _blank_columns(view_path)
    if blanks:
      return json.dumps({
        'error': f'view.csv 以下列整列为空：{"、".join(blanks)}',
        'suggestion': (
          '说明取值/映射写错。最常见原因：编码列在数据里是整数（如 int64），而 map 的 key 写成了'
          "字符串——{'1': '供配电'} 匹配不上 1，会整列变空；改成整数 key，或先 "
          "df['列'].astype(str) 再 map。两条出路任选一条：① 修正映射后重写 view.csv；"
          '② 确实不知道这些编码的含义 → 直接删掉这几列，或保留原始编码列（不要编造中文标签，'
          '也不要留空列）。请勿用同样写法重试。'
        ),
      }, ensure_ascii=False)

  payload = {'result': out}
  # 整理好的可读视图就绪时才发 records_url（与 analysis_load 的「不发」配对）：模型必须
  # 走完「整理 view.csv → 拿 URL → 出卡」链条，否则跳过整理出卡会让表格退回英文原始列名。
  if os.path.isfile(view_path):
    payload['records_url'] = (
      f'/api/v1/chat/agent/artifacts/{quote(thread_id, safe="")}/'
      f'{quote(dataset_id, safe="")}/records'
    )
  if result.get('chart_data') is not None:
    # 跨 exec 累积：本次缺的组从上次补回（防"先算好的维度被后一次 result 覆盖掉"），并把补回的组名
    # 明写在注记里——否则它们会无声消失（2026-09-18 实测「园区×变更类型交叉」就是这么丢的）。
    merged, restored = _merge_chart_data(thread_id, dataset_id, result['chart_data'])
    payload['chart_data'] = merged
    if restored:
      payload['chart_data_note'] = (
        f'⚠️ 本次 result 里没有「{"、".join(restored)}」（共 {len(restored)} 组），'
        '已**沿用上一次 exec 的结果补回**。注意：**每次 exec 都是新子进程、result 不跨次累积**——'
        '多个维度要放进**同一次** result（dict）一次算完，否则后一次会把前一次算好的维度覆盖掉。'
      )
    # 出图选型卡：落在模型组装 <chart> 标签前最后一个必经 ToolMessage 上。
    # 按**每一组聚合**的形状计数（一次 exec 常返回多组），这样"4 组同类计数"能被卡当场点出来。
    shapes = _detect_shapes(merged)
    payload['chart_guide'] = _render_chart_card(shapes, _record_chart_shape(thread_id, shapes))
  if result.get('chart_note'):
    payload['note'] = result['chart_note']
  if artifacts:
    payload['artifacts'] = [{
      'name': a['name'],
      'url': (
        f'/api/v1/chat/agent/artifacts/{quote(thread_id)}/{quote(dataset_id)}/'
        f'{quote(a["name"])}'
      ),
      'size': a['size'],
    } for a in artifacts]
  if result.get('artifact_note'):
    payload['artifact_note'] = result['artifact_note']
  return json.dumps(payload, ensure_ascii=False)


# ==================== 详情回读（报告「根因分析」章用） ====================

# 详情接口返回的字段白名单：**只回根因分析用的标量**。
# 为什么必须裁剪：详情响应实测 6.4k~9.7k 字符/条，里面绝大多数是噪音（`headimg` 头像 URL、
# `picture` 图片列表、8 个人的审批人数组）。原样进上下文，取 3 条就 +2.5k tokens，
# 而报告本来就是上下文吃紧的作业（42k 常驻）。
_DETAIL_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
  # 故障单详情：品牌/型号/备件/处置原文是"故障根因与备件治理"的唯一来源（列表没有这些字段）。
  # 三元组的第 3 项＝**join 键**：路径取到的是 list[dict] 时，逐条取该子字段拼接
  # （字段名 2026-10-15 用真实故障单实测确认：describe/reasonAnalysis/summarize、
  #  sparePartsName/sparePartsBrand/sparePartsModel/sparePartsCount）。
  'mgmt.query_breakdown_ticket_detail': (
    ('工单编号', 'ticketContent.ticketNo'),
    ('故障名称', 'ticketContent.breakdownName'),
    ('故障描述', 'ticketContent.describe'),
    ('发生时间', 'ticketContent.occurrenceTime'),
    ('发生地点', 'ticketContent.occurrenceSite'),
    ('故障级别', 'ticketContent.level'),
    ('专业', 'ticketContent.specialty'),
    ('设备类型', 'ticketContent.breakdownEquipmentType'),
    ('设备品牌', 'ticketContent.equipmentBrand'),
    ('设备型号', 'ticketContent.equipmentModel'),
    ('处置过程', 'executeDetails.executeContentList', 'describe'),
    ('原因分析', 'executeDetails.executeContentList', 'reasonAnalysis'),
    ('处置总结', 'executeDetails.executeContentList', 'summarize'),
    ('更换备件', 'executeDetails.replaceSparePartsVOS', 'sparePartsName'),
  ),
  'mgmt.query_event_ticket_detail': (
    ('工单编号', 'ticketContent.ticketNo'),
    ('事件名称', 'ticketContent.eventName'),
    ('发生时间', 'ticketContent.occurrenceTime'),
    ('完成时间', 'ticketContent.eventCompletionTime'),
    ('影响时长', 'ticketContent.influenceDuration'),
    ('影响范围', 'ticketContent.influenceScope'),
    ('原因分析', 'ticketContent.eventReasonAnalyse'),
    ('处理结果', 'ticketContent.eventProcessesResult'),
    ('处理过程', 'ticketContent.eventHandleProcesses'),
    ('事件描述', 'ticketContent.eventDescribe'),
    ('级别', 'ticketContent.level'),
    ('专业', 'ticketContent.specialty'),
    ('阶段', 'ticketContent.eventStage'),
  ),
  # 变更单详情（字段 2026-10-16 实测）：变更管理的过程质量只能从详情原文判断 ——
  # 风险识别/控制措施/回退方案是否具体，是第 10 章的核心证据。
  'mgmt.query_change_ticket_detail': (
    ('工单编号', 'ticketContent.ticketNo'),
    ('项目名称', 'ticketContent.projectName'),
    ('变更原因', 'ticketContent.reason'),
    ('变更方案', 'ticketContent.scheme'),
    ('风险识别', 'ticketContent.riskIdentification'),
    ('风险控制措施', 'ticketContent.riskControlMeasure'),
    ('回退方案', 'ticketContent.sendBackScheme'),
    ('影响范围', 'ticketContent.influenceScope'),
    ('执行内容', 'executeDetails.executeContentList', 'describe'),
    ('执行原因分析', 'executeDetails.executeContentList', 'reasonAnalysis'),
    ('执行总结', 'executeDetails.executeContentList', 'summarize'),
  ),
  # 问题单详情（字段 2026-10-17 实测）：问题描述与处置原文是根因归类与整改跟踪的唯一来源
  'mgmt.query_question_ticket_detail': (
    ('工单编号', 'ticketContent.ticketNo'),
    ('问题名称', 'ticketContent.questionName'),
    ('问题来源', 'ticketContent.questionSource'),
    ('发生地点', 'ticketContent.occurrenceSite'),
    ('涉及设备', 'ticketContent.involveEquipment'),
    ('问题描述', 'ticketContent.describe'),
    ('处置总结', 'executeDetails.executeContentList', 'summarize'),
    ('处置过程', 'executeDetails.executeProgressList', 'content'),
  ),
  # 阈值单详情（字段 2026-10-17 实测）：调整原因与涉及设备是"调整依据是否具体"的证据
  'mgmt.query_threshold_ticket_detail': (
    ('工单编号', 'ticketContent.ticketNo'),
    ('项目名称', 'ticketContent.projectName'),
    ('调整原因', 'ticketContent.reason'),
    ('涉及设备', 'ticketContent.equipmentName'),
    ('维护时长', 'ticketContent.maintenanceTime'),
    ('处置总结', 'executeDetails.executeContentList', 'summarize'),
    ('处置过程', 'executeDetails.executeProgressList', 'content'),
  ),
}

_NOISE_KEY_RE = re.compile(r'headimg|avatar|url|sign|token|password|fileId|hash', re.I)
_DETAIL_MAX_CHARS = 3500
_DETAIL_MAX_ROWS = 3


def _dig(obj, path: str):
  """按点号路径取值；任一层缺失返回 None。"""
  cur = obj
  for seg in path.split('.'):
    if not isinstance(cur, dict) or seg not in cur:
      return None
    cur = cur[seg]
  return cur


def _flatten_detail(obj: dict) -> dict:
  """未登记白名单的工单类型：通用裁剪（只留标量、丢噪音、列表只报条数）。"""
  out: dict = {}

  def walk(o, prefix: str, depth: int) -> None:
    if depth > 4 or len(out) >= 60:
      return
    if isinstance(o, dict):
      for k, v in o.items():
        if _NOISE_KEY_RE.search(str(k)):
          continue
        walk(v, f'{prefix}{k}.', depth + 1)
    elif isinstance(o, list):
      out[prefix.rstrip('.')] = f'（{len(o)} 项）'
    elif o is None or o == '':
      return
    else:
      out[prefix.rstrip('.')] = str(o)[:200]

  walk(obj, '', 0)
  trimmed: dict = {}
  used = 0
  for k, v in out.items():
    cost = len(k) + len(str(v)) + 6
    if used + cost > _DETAIL_MAX_CHARS:
      break
    trimmed[k] = v
    used += cost
  return trimmed


def _pick_detail_fields(detail_cap: str, resp: dict) -> dict:
  """按白名单抽字段；未登记的工单类型走通用裁剪。

  白名单条目可带**第 3 项 join 键**：路径取到 `list[dict]` 时，逐条取该子字段拼接
  （详情里的处置过程/原因分析/备件清单都是这种形状，`_dig` 直接取值只能得到整串 repr）。
  """
  spec = _DETAIL_FIELDS.get(detail_cap)
  if not spec:
    return _flatten_detail(resp)
  picked: dict = {}
  for entry in spec:
    label, path = entry[0], entry[1]
    join_key = entry[2] if len(entry) > 2 else ''
    v = _dig(resp, path)
    if join_key and isinstance(v, list):
      parts = [str(d.get(join_key)) for d in v
               if isinstance(d, dict) and d.get(join_key)]
      v = '；'.join(parts)
    if v is None or v == '':
      continue
    picked[label] = str(v)[:600]
  return picked


async def fetch_all_pages(
  capability_id: str,
  params: dict | None = None,
  *,
  user_id: str | None = None,
  max_rows: int | None = None,
) -> tuple[list[dict], bool]:
  """按页把一个平台能力的**全量**记录拉回来。返回 (records, truncated)。

  `analysis_load`（给对话 agent 用）与报告工作流共用这一段翻页逻辑，
  所以不依赖任何 LangGraph 上下文。找不到能力/后端时抛 RuntimeError（由调用方决定怎么记）。
  """
  if get_capability(capability_id) is None:
    raise RuntimeError(f'未知能力 {capability_id}')
  caller = get_caller(capability_id)
  if caller is None:
    raise RuntimeError(f'能力 {capability_id} 无可用后端')

  limit = max_rows or settings.AGENT_ANALYSIS_MAX_ROWS
  page_size = settings.AGENT_ANALYSIS_PAGE_SIZE
  records: list[dict] = []
  truncated = False
  page = 1
  while True:
    if len(records) >= limit:
      truncated = True
      break
    resp = await caller(capability_id, {
      **(params or {}), 'page': str(page), 'page_size': str(page_size),
    }, on_behalf_of=user_id)
    page_records, _ = _extract_records(resp)
    if not page_records:
      break
    records.extend(page_records)
    total = _extract_total(resp)
    if total is not None:
      if len(records) >= total:
        break
    elif len(page_records) < page_size:
      break
    page += 1
  return records, truncated


async def fetch_row_details(
  thread_id: str,
  dataset_id: str,
  ticket_nos: list[str],
  capability_id: str = '',
  user_id: str | None = None,
  max_rows: int = _DETAIL_MAX_ROWS,
) -> dict:
  """按工单编号取裁剪后的详情（**核心实现，不依赖 LangGraph 上下文**）。

  `analysis_row_detail`（给对话 agent 用）与报告工作流（`service/scheduled_task/report/`，
  没有 RunnableConfig）共用这一段，所以 `thread_id`/`user_id` 都是显式入参。
  返回值是可直接 json.dumps 的 dict（错误也走 `{'error': ...}`，不抛）。

  关键点：详情接口要的是 19 位长整型 id，而按编号反查 id 由**本函数在服务端完成** ——
  id 不出机器。抄错这种数字不报错、会查到另一张工单，管理层报告里就出现错误的根因。
  """
  ds_dir = _dataset_dir(thread_id, dataset_id)
  if ds_dir is None:
    return {'error': f'数据集不存在或 id 非法: {dataset_id}'}

  nos = [str(n).strip() for n in (ticket_nos or []) if str(n).strip()]
  if not nos:
    return {'error': 'ticket_nos 不能为空（用列表里的 ticketNo 编号）'}
  # 上限是**给对话 agent 的护栏**（防它逐条乱调）；报告工作流是平台侧调用，
  # 自己要几条就给几条（传 max_rows），不该被这个默认值截断
  note = ''
  if len(nos) > max_rows:
    note = f'一次最多取 {max_rows} 条，已只取前 {max_rows} 个'
    nos = nos[:max_rows]

  # 数据来源能力：优先入参，否则读数据集 meta（analysis_load 落盘时写了 capability_id）
  src_cap = capability_id
  if not src_cap:
    meta_path = os.path.join(str(ds_dir), 'meta.json')
    try:
      with open(meta_path, encoding='utf-8') as f:
        src_cap = (json.load(f) or {}).get('capability_id') or ''
    except (OSError, ValueError):
      src_cap = ''
  if not src_cap:
    return {'error': '无法确定数据来源能力，请显式传 capability_id'}

  # 列表能力 → 详情能力的命名约定（mgmt.query_event_ticket → ..._ticket_detail）
  detail_cap = f'{src_cap}_detail'
  if get_capability(detail_cap) is None:
    return {'error': f'能力 {src_cap} 没有对应的详情能力 {detail_cap}（该类型暂不支持取详情）'}
  caller = get_caller(detail_cap)
  if caller is None:
    return {'error': f'能力 {detail_cap} 无可用后端'}

  # 读 data.csv 建立 ticketNo → 主键 id 映射（id 列名由实测确定：事件单是 ticketId）
  # 一个编号可能对应**多条记录**（实测本期 168 行里 7 组同编号不同 id——同一告警重复开单），
  # 所以值是列表：取第一条，并把"同编号多单"如实带出，而不是静默挑一条。
  id_map: dict[str, list[str]] = {}
  try:
    with open(os.path.join(str(ds_dir), 'data.csv'), encoding='utf-8', newline='') as f:
      for row in csv.DictReader(f):
        no, tid = row.get('ticketNo'), row.get('ticketId')
        if no and tid:
          ids = id_map.setdefault(str(no).strip(), [])
          if str(tid).strip() not in ids:
            ids.append(str(tid).strip())
  except OSError as e:
    return {'error': f'读取 data.csv 失败: {e}'}

  rows: list[dict] = []
  for no in nos:
    ids = id_map.get(no)
    if not ids:
      rows.append({'ticketNo': no, 'error': 'data.csv 里没有该编号（请用列表里的 ticketNo）'})
      continue
    try:
      resp = await caller(detail_cap, {'ticket_id': ids[0]}, on_behalf_of=user_id)
    except Exception as e:
      rows.append({'ticketNo': no, 'error': f'{type(e).__name__}: {str(e)[:160]}'})
      continue
    if not isinstance(resp, dict):
      rows.append({'ticketNo': no, 'error': '详情返回结构异常'})
      continue
    item: dict = {'ticketNo': no, 'fields': _pick_detail_fields(detail_cap, resp)}
    if len(ids) > 1:
      item['note'] = (f'该编号在数据里有 {len(ids)} 条记录（同编号多单，通常同一告警重复开单），'
                      '已取第一条；报告中可如实说明这一现象')
    rows.append(item)

  payload: dict = {'dataset_id': dataset_id, 'rows': rows}
  if note:
    payload['note'] = note
  if detail_cap == 'mgmt.query_change_ticket_detail':
    payload['note_tail'] = (
      '变更意图与风险控制请以「变更原因/风险控制措施/回退方案」原文为准；'
      '「执行原因分析/执行总结」是实施后的复盘。取不到的写"该单详情不可得"，不要猜。'
    )
  else:
    payload['note_tail'] = (
      '「发生时间/完成时间」是**毫秒时间戳**；「影响时长」是平台算好的可读串。'
      '根因请以「原因分析/处理结果/处理过程」为准，取不到的写"该单详情不可得"，不要猜。'
    )
  return payload


@tool
async def analysis_row_detail(
  dataset_id: str,
  ticket_nos: list[str],
  capability_id: str = '',
  config: RunnableConfig = None,
) -> str:
  """按**工单编号**批量取详情（报告「根因分析」章用），只返回裁剪后的关键字段。

  为什么要有这个工具（别绕过它直接调 *_detail 工具）：详情接口要的是 19 位长整型 id，
  而模型在沙箱里 print 出来再手抄这种数字**极易抄错，且抄错不报错——会查到另一张工单**，
  管理层报告里就会出现错误的根因。本工具由**服务端**读数据集的 data.csv，按 `ticketNo`
  解出 id 再调详情，**id 不出机器**。

  Args:
    dataset_id: 数据集 id（analysis_load 返回的那个）
    ticket_nos: 工单编号列表，如 ['RZKJ-PH-B1M3M4-GPD-SJ-20261010-1']；
      **最多 3 个**（超出只取前 3 个），报告只对重复次数最高的问题取详情
    capability_id: 数据来源能力 id；留空则从数据集 meta 自动推断
  """
  payload = await fetch_row_details(
    _get_thread_id(config), dataset_id, ticket_nos, capability_id,
    user_id=get_current_user_id(),
  )
  return json.dumps(payload, ensure_ascii=False)


def build_analysis_tools() -> list:
  """返回分析工具列表（供 agent_service 追加到 raw_tools）。

  物化 + 执行 + 详情回读 + 文件操作（write/read/str_replace/ls/glob/grep）一起装配，
  agent_service 无需改动。
  """
  return [analysis_load, analysis_exec, analysis_row_detail] + build_file_tools()
