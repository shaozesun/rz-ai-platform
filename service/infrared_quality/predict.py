"""调外部 Gradio 服务判清晰度（纯 httpx，不引入 gradio_client）。

三步（实测 2026-11-06）：
1. `POST /gradio_api/upload`（multipart `files`）→ 服务器路径数组
2. `POST /gradio_api/call/predict_images` body `{"data": [[FileData…], 模型路径, "auto"]}`
   → `{"event_id": "…"}`。**Gradio 6 起文件入参必须是 FileData 对象**
   （`{"path": …, "orig_name": …, "meta": {"_type": "gradio.FileData"}, "_type": …}`），
   传裸路径字符串会被 pydantic 拒：`The 'meta' field must be explicitly provided`
   （2026-11-09 在 6.31.0 实测）
3. `GET /gradio_api/call/predict_images/{event_id}`（SSE），`event: complete` 的
   `data` 是三元组，`[2]` 形如 `{headers: ["文件名","Prediction","sharp",
   "borderline","defocused","错误"], data: [[文件名, 标签, "97.9%", …, 错误], …]}`
   （2026-11-10 改版实测）。每张输入图片一行；`Prediction` 为
   sharp / borderline / defocused / uncertain；三个概率列是**带 `%` 的字符串**
   （未校准 softmax）；失败时标签与概率留空、`错误` 列写原因。

单批失败（连接/5xx/SSE error）重试 1 次。标签到判定的映射在 `verdict.py`。
"""

import json
import logging
from collections import Counter

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


class InfraredUnavailable(Exception):
  """推理服务不可用（预检失败 / 连接拒绝）。"""


def chunks(items: list, size: int) -> list[list]:
  """按 size 切批；size<=0 时整批。"""
  size = int(size or 0)
  if size <= 0:
    return [list(items)] if items else []
  return [items[i:i + size] for i in range(0, len(items), size)]


def name_key(path: str) -> str:
  """路径/文件名归一：取最后一段（后端可能拼上服务器目录）。"""
  return str(path or '').replace('\\', '/').rsplit('/', 1)[-1]


def file_data(local: str, server: str) -> dict:
  """Gradio 6 的文件入参对象；缺 `meta` 会被 pydantic 直接拒。"""
  return {
    'path': server,
    'orig_name': name_key(local),
    'meta': {'_type': 'gradio.FileData'},
    '_type': 'gradio.FileData',
  }


def map_rows(paths: list[str], rows: list[list],
             name_idx: int = 0) -> list[list | None]:
  """把模型返回行按输入顺序对齐。

    同名（上传前同名，极小概率）无法按名区分 → 该名字退化为按位置取；
    位置也不存在时留 None（该图按检测失败处理）。
  """
  keys = [name_key(p) for p in paths]
  counts = Counter(keys)
  by_name = {k: i for i, k in enumerate(keys) if counts[k] == 1}
  out: list[list | None] = [None] * len(paths)
  for i, row in enumerate(rows):
    name = name_key(row[name_idx]) if row and name_idx < len(row) else ''
    idx = by_name.get(name)
    if idx is None:
      idx = i if i < len(paths) else None
    if idx is not None and out[idx] is None:
      out[idx] = row
  return out


def parse_sse(lines) -> list[tuple[str, str]]:
  """SSE 行 → [(event, data)]；空行结束一个事件，注释行（`:` 开头）忽略。"""
  events: list[tuple[str, str]] = []
  event = ''
  data: list[str] = []
  for raw in lines:
    line = raw.rstrip('\n')
    if not line:
      if event or data:
        events.append((event or 'message', '\n'.join(data)))
      event, data = '', []
      continue
    if line.startswith(':'):
      continue
    if line.startswith('event:'):
      event = line[len('event:'):].strip()
    elif line.startswith('data:'):
      data.append(line[len('data:'):].lstrip())
  if event or data:
    events.append((event or 'message', '\n'.join(data)))
  return events


def _result_table(payload) -> tuple[list[str], list[list]]:
  """从 `complete` 事件的 data 里找结果表 → `(headers, rows)`（容忍位置变化）。"""
  if isinstance(payload, dict):
    payload = [payload]
  if not isinstance(payload, list):
    return [], []
  for item in payload:
    if isinstance(item, dict) and isinstance(item.get('data'), list):
      headers = item.get('headers')
      rows = [r for r in item['data'] if isinstance(r, list)]
      return ([str(h or '') for h in headers] if isinstance(headers, list)
              else []), rows
  for item in payload:
    if isinstance(item, list) and item and all(isinstance(r, list) for r in item):
      return [], item
  return [], []


_PROB_COLUMNS = ('sharp', 'borderline', 'defocused', 'uncertain')
_NAME_HEADERS = ('文件名', 'filename', 'file', 'name')
_LABEL_HEADERS = ('prediction', '预测')
_ERROR_HEADERS = ('错误', 'error')


def _find_column(headers: list[str], names: tuple[str, ...], default: int) -> int:
  """按表头名（大小写不敏感）定位列；无表头时回退旧位置。"""
  lowered = [str(h or '').strip().lower() for h in headers]
  for name in names:
    if name in lowered:
      return lowered.index(name)
  return default


def _cell(row: list, idx: int) -> str:
  if idx < 0 or idx >= len(row):
    return ''
  value = row[idx]
  return '' if value is None else str(value).strip()


def parse_rows(headers: list[str], rows: list[list],
               paths: list[str]) -> dict[str, dict]:
  """按表头定位列，返回 `{输入路径: {label, score, error}}`。

    `score` 取**预测类别那一列**的原值（如 `97.9%`，是未校准概率、保留原字符串）；
    预测为 uncertain 或留空时 `score` 为 None。
  """
  name_idx = _find_column(headers, _NAME_HEADERS, 0)
  label_idx = _find_column(headers, _LABEL_HEADERS, 1)
  error_idx = _find_column(headers, _ERROR_HEADERS, 3)
  prob_idx: dict[str, int] = {}
  for name in _PROB_COLUMNS:
    idx = _find_column(headers, (name,), -1)
    if 0 <= idx < len(headers):
      prob_idx[name] = idx

  aligned = map_rows(paths, rows, name_idx=name_idx)
  out: dict[str, dict] = {}
  for path, row in zip(paths, aligned):
    if not row:
      out[path] = {'label': '', 'score': None, 'error': '结果缺失'}
      continue
    label = _cell(row, label_idx)
    error = _cell(row, error_idx)
    col = prob_idx.get(label.lower())
    score = _cell(row, col) if col is not None else ''
    out[path] = {'label': label, 'score': score or None, 'error': error}
  return out


class _Client:
  def __init__(self):
    base = (settings.INFRARED_QUALITY_API_URL or '').rstrip('/')
    name = settings.INFRARED_QUALITY_API_NAME or '/predict_images'
    if not name.startswith('/'):
      name = '/' + name
    self.base = base
    self.name = name
    self.timeout = settings.INFRARED_QUALITY_TIMEOUT

  async def preflight(self) -> None:
    """5s 探活：服务没起时直接给出明确结论，别让几百张图逐个失败。"""
    if not self.base:
      raise InfraredUnavailable('未配置 INFRARED_QUALITY_API_URL')
    try:
      async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f'{self.base}/config')
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
      raise InfraredUnavailable(
        f'检测服务不可达（{self.base}）：{type(exc).__name__}') from exc

  async def _upload(self, client: httpx.AsyncClient, paths: list[str]) -> list[dict]:
    files = []
    handles = []
    try:
      for path in paths:
        fh = open(path, 'rb')
        handles.append(fh)
        files.append(('files', (name_key(path), fh, 'application/octet-stream')))
      resp = await client.post(f'{self.base}/gradio_api/upload', files=files)
      resp.raise_for_status()
      data = resp.json()
    finally:
      for fh in handles:
        fh.close()
    if not isinstance(data, list):
      raise RuntimeError(f'上传返回异常：{str(data)[:200]}')
    return [file_data(local, str(server)) for local, server in zip(paths, data)]

  async def _call(self, client: httpx.AsyncClient,
                  file_data_list: list[dict]) -> tuple[list[str], list[list]]:
    body = {'data': [file_data_list,
                     settings.INFRARED_QUALITY_MODEL or '',
                     settings.INFRARED_QUALITY_DEVICE or 'auto']}
    resp = await client.post(f'{self.base}/gradio_api/call{self.name}', json=body)
    resp.raise_for_status()
    event_id = (resp.json() or {}).get('event_id')
    if not event_id:
      raise RuntimeError(f'未拿到 event_id：{resp.text[:200]}')

    lines = []
    async with client.stream(
      'GET', f'{self.base}/gradio_api/call{self.name}/{event_id}',
      headers={'Accept': 'text/event-stream'},
    ) as stream:
      stream.raise_for_status()
      async for line in stream.aiter_lines():
        lines.append(line)
        if line.startswith('event:') and line.split(':', 1)[1].strip() == 'error':
          raise RuntimeError('推理服务返回 error 事件')
    for event, data in parse_sse(lines):
      if event != 'complete':
        continue
      try:
        payload = json.loads(data)
      except ValueError as exc:
        raise RuntimeError(f'结果解析失败：{data[:200]}') from exc
      return _result_table(payload)
    raise RuntimeError('未收到 complete 事件')

  async def _predict_once(self, paths: list[str]) -> tuple[list[str], list[list]]:
    async with httpx.AsyncClient(timeout=self.timeout) as client:
      server_paths = await self._upload(client, paths)
      return await self._call(client, server_paths)

  async def predict_batch(self, paths: list[str]) -> dict[str, dict]:
    """一批图片 → `{输入路径: {label, score, error}}`；整批失败重试 1 次。"""
    if not paths:
      return {}
    headers: list[str] = []
    rows: list[list] = []
    last_exc: Exception | None = None
    for attempt in (1, 2):
      try:
        headers, rows = await self._predict_once(paths)
        last_exc = None
        break
      except Exception as exc:  # noqa: BLE001
        last_exc = exc
        logger.warning('红外推理第 %s 次失败（%s 张）: %s', attempt, len(paths), exc)
    if last_exc is not None:
      raise last_exc
    return parse_rows(headers, rows, paths)


def client() -> _Client:
  return _Client()
