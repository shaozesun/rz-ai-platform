"""数据集目录文件操作工具 — 迭代式生成复杂产物。

配合 analysis_exec 的迭代循环（执行 → 观察 → 局部修改 → 再执行）：
- analysis_write_file：把脚本/内容落盘（如 report.py）
- analysis_exec(file='report.py')：执行落盘脚本
- analysis_str_replace：局部精准改一行，不必重传整段代码
- analysis_read_file / analysis_ls / analysis_glob / analysis_grep：观察产物

安全模型：沙箱目录就在宿主文件系统（data/analysis/{thread_id}/{dataset_id}/），
这些 async 工具直接在宿主进程读写该目录（不起子进程），边界 = 会话归属
（thread_id 来自 RunnableConfig）+ dataset_id 净化 + 相对路径 resolve 校验
在数据集目录内，防路径穿越。
"""

import json
import logging
import re
from pathlib import Path

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.sandbox.datasets import DatasetError, _SAFE_RE, load_dataset_dir

logger = logging.getLogger(__name__)

# grep 单文件最大匹配行 / 单行截断
_GREP_PER_FILE_MAX = 20
_GREP_LINE_MAX = 200


def _get_thread_id(config: RunnableConfig | None) -> str:
  """从 RunnableConfig 取 LangGraph thread_id（会话隔离用）。"""
  if not config:
    return 'default'
  return (config.get('configurable') or {}).get('thread_id') or 'default'


def _err(msg: str) -> str:
  return json.dumps({'error': msg}, ensure_ascii=False)


def _dataset_dir(thread_id: str, dataset_id: str) -> Path | None:
  """校验 dataset_id 并定位数据集目录；失败（含非法字符/不存在）返回 None。"""
  if not dataset_id or _SAFE_RE.search(dataset_id):
    return None
  try:
    return load_dataset_dir(thread_id, dataset_id)
  except DatasetError:
    return None


def _resolve_dataset_path(thread_id: str, dataset_id: str, path: str) -> tuple[Path, Path] | None:
  """校验数据集 + 相对路径，返回 (ds_dir, target)；非法返回 None。

  target = ds_dir/path 经 resolve 后必须仍在 ds_dir 内（防 .. 穿越与软链逃逸）。
  """
  ds_dir = _dataset_dir(thread_id, dataset_id)
  if ds_dir is None:
    return None
  base = ds_dir.resolve()
  target = (base / (path or '.')).resolve()
  if not target.is_relative_to(base):
    return None
  return ds_dir, target


@tool
async def analysis_read_file(
  dataset_id: str,
  path: str,
  config: RunnableConfig = None,
) -> str:
  """读取数据集目录内的文件内容，用于查看已生成的脚本/中间产物/结果文件。

  适合迭代开发产物时检查上一步 write/exec 生成的脚本内容，或确认产物文件是否符合预期。
  path 相对数据集目录；读取结果截断到上限字符（二进制文件不建议读）。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    path: 文件相对路径，如 report.py
  """
  thread_id = _get_thread_id(config)
  resolved = _resolve_dataset_path(thread_id, dataset_id, path)
  if resolved is None:
    return _err('数据集不存在或路径非法')
  _, target = resolved
  if target.is_dir():
    return _err(f'{path} 是目录，请用 analysis_ls 查看目录内容')
  if not target.is_file():
    return _err(f'文件不存在：{path}')
  try:
    content = target.read_text(encoding='utf-8', errors='replace')
  except OSError as e:
    return _err(f'读取失败：{e}')
  max_chars = settings.AGENT_ANALYSIS_MAX_OUTPUT_CHARS
  if len(content) > max_chars:
    content = content[:max_chars] + f'\n…（内容过长已截断，仅前 {max_chars} 字符）'
  return json.dumps({'content': content}, ensure_ascii=False)


@tool
async def analysis_write_file(
  dataset_id: str,
  path: str,
  content: str,
  config: RunnableConfig = None,
) -> str:
  """把脚本/内容写入数据集目录（覆盖写），供 analysis_exec(file=...) 执行或作为产物交付。

  适合把多行代码落盘为脚本（如 report.py），再用 analysis_exec(file='report.py') 执行；
  落盘后的小改动用 analysis_str_replace 精准替换，省 token。单文件超大小上限会被拒绝。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    path: 目标文件相对路径，如 report.py
    content: 完整文件内容
  """
  thread_id = _get_thread_id(config)
  resolved = _resolve_dataset_path(thread_id, dataset_id, path)
  if resolved is None:
    return _err('数据集不存在或路径非法')
  _, target = resolved
  max_mb = settings.AGENT_ANALYSIS_MAX_ARTIFACT_MB
  size = len(content.encode('utf-8'))
  if max_mb and size > max_mb * 1024 * 1024:
    return _err(f'文件超过 {max_mb}MB 上限')
  try:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
  except OSError as e:
    return _err(f'写入失败：{e}')
  return json.dumps({'ok': True, 'size': size}, ensure_ascii=False)


@tool
async def analysis_str_replace(
  dataset_id: str,
  path: str,
  old: str,
  new: str,
  config: RunnableConfig = None,
) -> str:
  """在已落盘的脚本/文件里精准替换一段文本（全部出现），用于局部修改。

  配合 write 落盘 + exec 执行的迭代流程：发现一处小 bug 用 str_replace 精准改，
  不必重写整个文件（省 token）。old 必须与文件内容精确匹配；若替换后需再次执行，
  用 analysis_exec(file='脚本名') 重跑。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    path: 文件相对路径
    old: 待替换的原文（要求精确匹配，可含换行）
    new: 替换后的新文本
  """
  thread_id = _get_thread_id(config)
  resolved = _resolve_dataset_path(thread_id, dataset_id, path)
  if resolved is None:
    return _err('数据集不存在或路径非法')
  _, target = resolved
  if not target.is_file():
    return _err(f'文件不存在：{path}')
  try:
    content = target.read_text(encoding='utf-8', errors='replace')
  except OSError as e:
    return _err(f'读取失败：{e}')
  if not old:
    return _err('old 不能为空')
  if old not in content:
    return _err(f'未找到要替换的文本（文件共 {len(content)} 字符），请核对 old 是否精确一致')
  count = content.count(old)
  content = content.replace(old, new)
  try:
    target.write_text(content, encoding='utf-8')
  except OSError as e:
    return _err(f'写入失败：{e}')
  return json.dumps({'ok': True, 'count': count}, ensure_ascii=False)


@tool
async def analysis_ls(
  dataset_id: str,
  path: str = '.',
  config: RunnableConfig = None,
) -> str:
  """列出数据集目录下的文件与子目录（名称/大小/类型），确认已生成的产物与脚本。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    path: 目录相对路径，默认根目录 '.'
  """
  thread_id = _get_thread_id(config)
  resolved = _resolve_dataset_path(thread_id, dataset_id, path)
  if resolved is None:
    return _err('数据集不存在或路径非法')
  _, target = resolved
  if not target.is_dir():
    return _err(f'{path} 不是目录')
  try:
    entries = sorted(target.iterdir(), key=lambda p: p.name)
  except OSError as e:
    return _err(f'读取目录失败：{e}')
  files = []
  for p in entries:
    is_dir = p.is_dir()
    size = 0
    if not is_dir:
      try:
        size = p.stat().st_size
      except OSError:
        size = 0
    files.append({'name': p.name, 'size': size, 'dir': is_dir})
  return json.dumps({'files': files}, ensure_ascii=False)


@tool
async def analysis_glob(
  dataset_id: str,
  pattern: str,
  config: RunnableConfig = None,
) -> str:
  """按通配符匹配数据集目录内的文件，返回相对路径列表。

  用于按类型找产物，如 *.png、report.py、*.xlsx。pattern 相对数据集目录。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    pattern: 通配符模式，如 '*.png'
  """
  thread_id = _get_thread_id(config)
  ds_dir = _dataset_dir(thread_id, dataset_id)
  if ds_dir is None:
    return _err('数据集不存在或已过期')
  base = ds_dir.resolve()
  matches: list[str] = []
  try:
    for p in base.glob(pattern):
      if p.is_file():
        r = p.resolve()
        if r.is_relative_to(base):
          matches.append(str(r.relative_to(base)))
  except (OSError, ValueError) as e:
    return _err(f'glob 失败：{e}')
  matches.sort()
  return json.dumps({'files': matches}, ensure_ascii=False)


@tool
async def analysis_grep(
  dataset_id: str,
  pattern: str,
  path: str = '.',
  config: RunnableConfig = None,
) -> str:
  """在数据集目录内按正则搜索文本，返回匹配行（文件/行号/内容）。

  适合在迭代产物时定位脚本里的某个调用、报错关键字等。每文件最多返回前
  20 行匹配，单行截断。跳过过大文件（含二进制）。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    pattern: 正则表达式，如 'TODO|matplotlib'
    path: 搜索目录相对路径，默认根目录 '.'
  """
  thread_id = _get_thread_id(config)
  resolved = _resolve_dataset_path(thread_id, dataset_id, path)
  if resolved is None:
    return _err('数据集不存在或路径非法')
  base, target = resolved
  if not target.is_dir():
    return _err(f'{path} 不是目录')
  try:
    regex = re.compile(pattern)
  except re.error as e:
    return _err(f'正则非法：{e}')
  matches: list[dict] = []
  max_bytes = settings.AGENT_ANALYSIS_MAX_OUTPUT_CHARS * 10
  try:
    for p in target.rglob('*'):
      if not p.is_file():
        continue
      try:
        if p.stat().st_size > max_bytes:
          continue
      except OSError:
        continue
      try:
        text = p.read_text(encoding='utf-8', errors='replace')
      except OSError:
        continue
      count = 0
      for lineno, line in enumerate(text.splitlines(), 1):
        if regex.search(line):
          matches.append({
            'file': str(p.relative_to(base)),
            'line': lineno,
            'text': line.strip()[:_GREP_LINE_MAX],
          })
          count += 1
          if count >= _GREP_PER_FILE_MAX:
            break
  except (OSError, ValueError) as e:
    return _err(f'搜索失败：{e}')
  return json.dumps({'matches': matches}, ensure_ascii=False)


def build_file_tools() -> list:
  """返回文件操作工具列表（供 build_analysis_tools 合并进 raw_tools）。"""
  return [
    analysis_read_file,
    analysis_write_file,
    analysis_str_replace,
    analysis_ls,
    analysis_glob,
    analysis_grep,
  ]
