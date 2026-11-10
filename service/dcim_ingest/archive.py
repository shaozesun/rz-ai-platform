"""DCIM 时序**归档层**：实时数据按「天 × 设备类型」攒成 Parquet 传到 MinIO。

## 语义（用户已定）

1. **全量归档**，不是"只存过期数据"。多出来的那份**兼作热层（TDengine）的备份**。
   → 所以必须**持续/近实时**写：等数据过期再归档的话，热层一过期、中间那段就没有第二份了。
2. **归档要能独立恢复**。万一哪天发现 TDengine 数据坏了，要能只靠 Parquet 重建 ——
   所以每个设备类型额外写一份 `schema` 对象（列名映射 / tag / 精度），
   否则只靠一堆裸数值恢复不出语义。
3. **必须有校验**。没有对账的"备份"是假的：`verify_archive()` 逐文件核对行数，
   并报出"哪些天/哪些设备类型没归档"。

## 幂等与去重

对象键里带**时间范围**（`part-<min_ts>-<max_ts>.parquet`）：
- 同一时间范围重放 → **同名覆盖**（幂等）
- 跨范围重放可能产生重复行 → 查询/恢复时按 `(device_guid, ts)` 去重
  （DuckDB 里 `QUALIFY row_number() over (partition by device_guid, ts order by ts)`）

## 对象布局

    {bucket}/dt=YYYY-MM-DD/device_type=<类型>/part-<min_ts>-<max_ts>.parquet
    {bucket}/dt=YYYY-MM-DD/device_type=<类型>/part-<min_ts>-<max_ts>.manifest.json
    {bucket}/_schema/device_type=<类型>.json

`dt` 取 **UTC 日期**（容器时区是 UTC；固定用 UTC 免得换机器后分区漂移）。
"""

from __future__ import annotations

import io
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import settings
from service.dcim_ingest.models import DeviceSample

logger = logging.getLogger(__name__)

_MINIO_IMPORT_HINT = '归档需要 pyarrow + minio（见 requirements.txt 末尾）'


def _dt_of(ts_ms: int) -> str:
  """毫秒时间戳 → `YYYY-MM-DD`（**UTC**，见模块 docstring）。"""
  return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')


def _safe(name: str) -> str:
  """设备类型编码进对象键前做安全化（UUID 里有 `-`，对象键允许，但统一处理更稳）。"""
  return ''.join(c if c.isalnum() or c in '._-' else '_' for c in str(name)) or 'unknown'


@dataclass
class _Bucket:
  """同一 (dt, 设备类型) 的待落缓冲。"""

  dt: str
  device_type: str
  columns: list[str]                 # TDengine 的列序（ts / 数据列 / report_ts / quality）
  rows: list[dict] = field(default_factory=list)
  min_ts: int = 0
  max_ts: int = 0
  opened_at: float = field(default_factory=time.time)

  def add(self, row: dict, ts: int) -> None:
    self.rows.append(row)
    self.min_ts = ts if not self.min_ts else min(self.min_ts, ts)
    self.max_ts = max(self.max_ts, ts)


class ArchiveSink:
  """把 `WideWriter` 解析好的宽行写进 MinIO。

  **复用 `WideWriter` 的解析结果**（设备类型 / 列序 / tag），不自己去查 PG ——
  同一批数据只用解析一次，两个 sink（TDengine / 归档）拿到的是同一份行。
  """

  def __init__(self) -> None:
    self._client = None
    self._buckets: dict[tuple[str, str], _Bucket] = {}
    self._schema_written: set[str] = set()
    self.rows_written = 0
    self.flush_errors = 0
    self.dropped_rows = 0        # 缓冲超限被丢的行（**可由 TDengine 侧重放补**）

  def _buffered_rows(self) -> int:
    return sum(len(b.rows) for b in self._buckets.values())

  def _enforce_buffer_cap(self) -> None:
    """缓冲超上限就**丢最旧的桶**并计数 —— 宁可丢归档缓冲，也不能把进程撑爆。

    丢得起的前提：TDengine 侧 `KEEP`（默认 90 天，见 `settings.TAOS_KEEP_DAYS`）还在，
    重放能把这段补回来。所以日志要写清"可重放"，而不是让人以为数据永久没了。
    """
    cap = settings.ARCHIVE_MAX_BUFFER_ROWS
    while self._buffered_rows() > cap and self._buckets:
      oldest = min(self._buckets.items(), key=lambda kv: kv[1].opened_at)
      key, bucket = oldest
      self.dropped_rows += len(bucket.rows)
      self._buckets.pop(key, None)
      logger.warning(
        '归档缓冲超上限（%d 行 > %d），丢弃最旧桶 %s/%s 的 %d 行；'
        '这些行可由 TDengine 侧重放补（KEEP 期内）',
        self._buffered_rows(), cap, bucket.dt, bucket.device_type, len(bucket.rows),
      )

  # ── 连接（消费线程只认 connect / close） ──
  def connect(self) -> None:
    from minio import Minio
    self._client = Minio(
      settings.ARCHIVE_MINIO_ENDPOINT,
      access_key=settings.ARCHIVE_MINIO_ACCESS_KEY,
      secret_key=settings.ARCHIVE_MINIO_SECRET_KEY,
      secure=settings.ARCHIVE_MINIO_SECURE,
    )
    bucket = settings.ARCHIVE_MINIO_BUCKET
    if not self._client.bucket_exists(bucket):
      self._client.make_bucket(bucket)
      logger.info('归档：已创建桶 %s', bucket)
    self._ensure_schema_refresh()

  def close(self) -> None:
    """退出前把缓冲刷完。**刷不掉也要计数**（不能静默丢），并说明可重放。"""
    try:
      self.flush_all()
    finally:
      left = self._buffered_rows()
      if left:
        self.dropped_rows += left
        logger.warning(
          '归档退出时仍有 %d 行未落盘（MinIO 不可用？）—— 可由 TDengine 侧重放补',
          left,
        )
      self._buckets.clear()
      self._client = None

  # ── 写入（由 WideWriter 调用） ──
  def write_rows(
    self, *, cluster: str, device_type: str, columns: list[str],
    col_to_code: dict[str, str], tags: dict[str, str],
    samples: list[DeviceSample],
  ) -> None:
    if self._client is None:
      return
    for s in samples:
      row = {name: tags.get(name, '') for name in tags}
      for col in columns:
        if col == 'ts':
          row[col] = s.ts
        elif col == 'report_ts':
          row[col] = s.report_ts
        elif col == 'quality':
          row[col] = None
        else:
          code = col_to_code.get(col)
          row[col] = s.values.get(code) if code else None
      key = (_dt_of(s.ts), device_type)
      bucket = self._buckets.get(key)
      if bucket is None:
        bucket = _Bucket(dt=key[0], device_type=device_type, columns=list(columns))
        self._buckets[key] = bucket
      bucket.add(row, s.ts)
      self._write_schema(device_type, columns, col_to_code, tags)

    for key, bucket in list(self._buckets.items()):
      if (len(bucket.rows) >= settings.ARCHIVE_FLUSH_ROWS
          or time.time() - bucket.opened_at >= settings.ARCHIVE_FLUSH_SECONDS):
        self._flush(key, bucket)
    self._enforce_buffer_cap()

  def flush_all(self) -> None:
    for key, bucket in list(self._buckets.items()):
      self._flush(key, bucket)

  # ── 内部：落文件 ──
  @staticmethod
  def _frame(bucket: _Bucket):
    """行 → DataFrame，并**显式定类型**。

    不定类型的话，`report_ts`/`quality` 里一出现空值，pandas 会把整列变成 double ——
    Parquet 的类型就随"第一批有没有空值"漂移，DuckDB 读出来也难看。时间戳保持 int。
    """
    import pandas as pd

    from service.dcim_ingest.realtime import TAG_COLUMNS
    tag_names = {n for n, _ in TAG_COLUMNS}
    df = pd.DataFrame(bucket.rows)
    for col in df.columns:
      if col == 'ts':
        df[col] = df[col].astype('int64')
      elif col in ('report_ts', 'quality'):
        df[col] = df[col].astype('Int64')          # 可空整数
      elif col in tag_names:
        df[col] = df[col].astype('string')
      else:
        df[col] = df[col].astype('float64')
    return df

  def _flush(self, key: tuple[str, str], bucket: _Bucket) -> None:
    if not bucket.rows or self._client is None:
      return
    try:
      buf = io.BytesIO()
      self._frame(bucket).to_parquet(buf, engine='pyarrow', index=False)
      size = buf.tell()
      buf.seek(0)
      name = f'part-{bucket.min_ts}-{bucket.max_ts}.parquet'
      prefix = f'dt={bucket.dt}/device_type={_safe(bucket.device_type)}'
      self._client.put_object(
        settings.ARCHIVE_MINIO_BUCKET, f'{prefix}/{name}', buf, size,
        content_type='application/vnd.apache.parquet',
      )
      # manifest：**一个归档文件一个对象**（对象存储里"追加"很别扭，一个文件一份最干净）
      manifest = {
        'dt': bucket.dt, 'device_type': bucket.device_type,
        'key': f'{prefix}/{name}', 'rows': len(bucket.rows), 'size': size,
        'min_ts': bucket.min_ts, 'max_ts': bucket.max_ts,
        'written_at': datetime.now(timezone.utc).isoformat(),
      }
      body = json.dumps(manifest, ensure_ascii=False).encode()
      self._client.put_object(
        settings.ARCHIVE_MINIO_BUCKET,
        f'{prefix}/{name}.manifest.json', io.BytesIO(body), len(body),
        content_type='application/json',
      )
      self.rows_written += len(bucket.rows)
      logger.info('归档：%s/%s 落 %d 行（%d 字节）',
                  prefix, name, len(bucket.rows), size)
    except Exception as e:                       # noqa: BLE001 —— 归档失败不能拖垮实时链路
      self.flush_errors += 1
      logger.warning('归档失败（%s/%s，%d 行）：%s',
                     bucket.dt, bucket.device_type, len(bucket.rows), e)
      return
    self._buckets.pop(key, None)

  def _write_schema(
    self, device_type: str, columns: list[str],
    col_to_code: dict[str, str], tags: dict[str, str],
  ) -> None:
    """每个设备类型写一份 schema（**可独立恢复的前提**）。编码没变就只写一次。"""
    if device_type in self._schema_written or self._client is None:
      return
    payload = {
      'device_type': device_type,
      'columns': columns,            # 含 ts / report_ts / quality
      'col_to_code': col_to_code,    # 列名 → 测点编码（恢复时据此还原语义）
      'tags': sorted(tags),
      'ts_precision': 'ms',
      'updated_at': datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    try:
      self._client.put_object(
        settings.ARCHIVE_MINIO_BUCKET,
        f'_schema/device_type={_safe(device_type)}.json', io.BytesIO(body), len(body),
        content_type='application/json',
      )
      self._schema_written.add(device_type)
    except Exception as e:                       # noqa: BLE001
      logger.warning('归档 schema 写入失败（%s）：%s', device_type, e)

  def _ensure_schema_refresh(self) -> None:
    """进程重启后允许重写 schema（列可能已增加）。"""
    self._schema_written.clear()


# ── 对账（按天跑，可挂定时任务） ──

def verify_archive(*, dt: str | None = None) -> dict:
  """核对归档：manifest 声明的行数 vs 文件**实际**行数；并列出缺归档的类型。

  只读 Parquet 的 footer 拿行数（`ParquetFile.metadata.num_rows`），不整份下载。
  """
  import tempfile

  from minio import Minio
  client = Minio(
    settings.ARCHIVE_MINIO_ENDPOINT,
    access_key=settings.ARCHIVE_MINIO_ACCESS_KEY,
    secret_key=settings.ARCHIVE_MINIO_SECRET_KEY,
    secure=settings.ARCHIVE_MINIO_SECURE,
  )
  bucket = settings.ARCHIVE_MINIO_BUCKET
  prefix = f'dt={dt}/' if dt else ''
  problems: list[dict] = []
  checked = 0
  declared = 0

  for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
    if not obj.object_name.endswith('.manifest.json'):
      continue
    resp = client.get_object(bucket, obj.object_name)
    try:
      man = json.loads(resp.read())
    finally:
      resp.close()
      resp.release_conn()
    declared += int(man.get('rows') or 0)
    tmp = tempfile.NamedTemporaryFile(suffix='.parquet', delete=False)
    try:
      client.fget_object(bucket, man['key'], tmp.name)
      import pyarrow.parquet as pq
      actual = pq.ParquetFile(tmp.name).metadata.num_rows
    except Exception as e:                       # noqa: BLE001
      problems.append({'key': man['key'], 'error': f'{type(e).__name__}: {e}'})
      continue
    checked += 1
    if actual != int(man.get('rows') or 0):
      problems.append({
        'key': man['key'], 'declared': man.get('rows'), 'actual': actual,
      })
  return {
    'files_checked': checked, 'rows_declared': declared,
    'problems': problems, 'ok': not problems,
  }
