"""事件统计表的 PG 表结构（幂等建表）。

只存**输入**：园区群里的飞书通报原文 + 匹配到的告警 id。22 列统计行每次按窗口
实时关联计算（告警/工单/空间都在库里，工单会后补，固化快照会漂移）。

与 `deploy/sql/dcim_schema.sql` 保持一致：该 .sql 供 DBA 审查，本模块在同步任务/
CLI 首次跑时幂等执行，免去手工建表步骤（不依赖 dcim-ingest worker 重启）。
"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
  '''
  CREATE TABLE IF NOT EXISTS dcim_incident_reports (
    id             BIGSERIAL PRIMARY KEY,
    chat_id        TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    cluster        TEXT,
    kind           TEXT NOT NULL,
    title          TEXT,
    create_time    BIGINT NOT NULL,
    event_time     BIGINT,
    text           TEXT,
    raw            JSONB,
    sender         TEXT,
    sender_id      TEXT,               -- 发送者 id：人=open_id，bot=app_id
    sender_id_type TEXT,               -- open_id / app_id
    report_value   DOUBLE PRECISION,   -- 报文里的【告警值】
    match_detail   TEXT,               -- 命中的匹配信号，如 time+value / time+value+llm
    event_log_id   TEXT,
    match_score    INTEGER,
    matched_at     TIMESTAMPTZ,
    llm_tried_at   TIMESTAMPTZ,        -- 已让模型仲裁过（判不出也标，一条只试一次）
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, message_id)
  )
  ''',
  # 表若已按旧结构建过，CREATE TABLE IF NOT EXISTS 不会补列 → 逐列 ALTER
  'ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS sender_id TEXT',
  'ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS sender_id_type TEXT',
  'ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS report_value'
  ' DOUBLE PRECISION',
  'ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS match_detail TEXT',
  'ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS llm_tried_at'
  ' TIMESTAMPTZ',
  'CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_cluster'
  ' ON dcim_incident_reports (cluster, create_time)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_chat'
  ' ON dcim_incident_reports (chat_id, create_time)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_event'
  ' ON dcim_incident_reports (event_log_id)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_kind'
  ' ON dcim_incident_reports (kind, create_time)',
)


def ensure_reports_schema(dsn: str) -> None:
  """连接 PG 并幂等建表（同步任务 / CLI 首次跑时调用）。"""
  if not dsn:
    raise RuntimeError('DCIM_PG_DSN 未配置，无法建事件通报表')
  import psycopg

  with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
      for stmt in SCHEMA_STATEMENTS:
        cur.execute(stmt)
    conn.commit()
