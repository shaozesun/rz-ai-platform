-- DCIM Kafka 接入 · 元数据表结构（阶段一：空间/设备/点位）
-- 设计依据：docs/dcim/dcim-kafka-ingest-design.md 第五节（当前态）与第六节（变更历史）
--
-- 用法（先建库，再连到 dcim 库执行本文件）：
--   createdb dcim                       # 或： psql -c 'CREATE DATABASE dcim;'
--   psql -d dcim -f deploy/sql/dcim_schema.sql
--
-- 约定：
-- - **园区维度**：对方是每个园区一个独立 Kafka 集群，13 个园区的数据进同一个库，
--   所以每张表都带 cluster（园区名，如 '廊坊A'）。它来自消息所在的集群，不是报文字段。
-- - 当前态表主键为**复合 (cluster, guid)**：不同园区出现同一 guid 时不会互相覆盖。
-- - 点位 guid = 设备 guid + '.' + 点位 id
-- - 当前态表保存最新状态；每次推送变更在历史表追加一条快照（append-only）
-- - 删除语义待与 DCIM 确认，当前按“软删”（is_deleted/deleted_at）处理
--
-- 与 service/dcim_ingest/schema.py 保持一致（worker 启动时会等价地幂等建表）。

-- ============ 当前态：空间 ============
CREATE TABLE IF NOT EXISTS dcim_spaces (
  cluster       TEXT NOT NULL,        -- 园区名（消息来自哪个集群）
  guid          TEXT NOT NULL,
  tag           TEXT,
  path          TEXT,
  name          TEXT,
  idc           TEXT,
  node_type     INTEGER,
  space_type    TEXT,
  parent_guid   TEXT,
  payload       JSONB,               -- 原始报文
  is_deleted    BOOLEAN NOT NULL DEFAULT FALSE,
  deleted_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster, guid)
);
CREATE INDEX IF NOT EXISTS idx_dcim_spaces_parent ON dcim_spaces (cluster, parent_guid);
CREATE INDEX IF NOT EXISTS idx_dcim_spaces_idc ON dcim_spaces (cluster, idc);

-- ============ 当前态：设备 ============
CREATE TABLE IF NOT EXISTS dcim_devices (
  cluster           TEXT NOT NULL,
  guid              TEXT NOT NULL,
  name              TEXT,
  path              TEXT,
  idc               TEXT,
  node_type         INTEGER,
  device_type       TEXT,            -- 规范设备类型编码
  device_name       TEXT,            -- 设备**类型**名称（如「精密空调」）；设备名称是 name
  extend_position   TEXT,
  space_guid        TEXT,            -- 所属空间（原 parent_id）
  payload           JSONB,
  is_deleted        BOOLEAN NOT NULL DEFAULT FALSE,
  deleted_at        TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster, guid)
);
CREATE INDEX IF NOT EXISTS idx_dcim_devices_space ON dcim_devices (cluster, space_guid);
CREATE INDEX IF NOT EXISTS idx_dcim_devices_idc ON dcim_devices (cluster, idc);
CREATE INDEX IF NOT EXISTS idx_dcim_devices_type ON dcim_devices (cluster, device_type);

-- ============ 当前态：点位 ============
CREATE TABLE IF NOT EXISTS dcim_points (
  cluster         TEXT NOT NULL,
  guid            TEXT NOT NULL,     -- 设备 guid + '.' + 点位 id
  point_id        TEXT,
  name            TEXT,
  idc             TEXT,
  node_type       INTEGER,
  point_type      INTEGER,           -- 1-AI / 2-DI / 3-通讯状态 / 4-AO / 5-DO
  unit            TEXT,
  status_map_raw  TEXT,              -- 原文，如 '1=开门;0=关门'
  status_map      JSONB,             -- 解析后，如 {"1":"开门","0":"关门"}
  device_guid     TEXT,              -- 所属设备（原 parent_id）
  -- 4 个告警阈值（9/29 版新增）：嵌套 EmsAlarmThreshold，整体存 JSONB
  hint_alarm      JSONB,             -- 提示告警设置
  general_alarm   JSONB,             -- 一般告警设置
  important_alarm JSONB,             -- 重要告警设置
  urgent_alarm    JSONB,             -- 紧急告警设置
  payload         JSONB,
  is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
  deleted_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster, guid)
);
CREATE INDEX IF NOT EXISTS idx_dcim_points_device ON dcim_points (cluster, device_guid);
CREATE INDEX IF NOT EXISTS idx_dcim_points_idc ON dcim_points (cluster, idc);

-- ============ 变更历史（append-only） ============
-- 配置类（空间/设备/点位）**不建历史表**（2026-10-21 移除）：上游是每小时全量重推，
-- 历史表退化成快照堆（430 万行里 77% 与上一条逐字节相同，单栋楼约 7 GB/天），
-- 且全仓库没有任何读取方。告警类的历史表见下方「告警事件」一节。

-- ============ 死信（解析失败的消息） ============
CREATE TABLE IF NOT EXISTS dcim_dlq (
  id          BIGSERIAL PRIMARY KEY,
  cluster     TEXT,
  topic       TEXT,
  partition   INTEGER,
  "offset"    BIGINT,
  error       TEXT,
  raw         TEXT,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dcim_dlq_cluster ON dcim_dlq (cluster);

-- ============ 时序库（TDengine）建模注册表 + 测点分级 ============
-- 配套设计：docs/dcim/dcim-timeseries-storage-design.md
-- 宽表模型：**一个设备类型一张超级表**（列 = 该类型的各测点），一台设备一张子表。

-- 设备类型 → 超级表名（写入端与查询端共用的唯一来源）
-- ⚠️ 主键 (device_type, point_group)：TDengine 单表列数上限 4,096，00AG/00WG 余量只剩
--    19~25 列，再冒一个新测点就会 ALTER 失败、整园区入库卡死 → 超大类型的测点拆到 g1/g2。
--    g0 沿用 st_<type>，g1 起是 st_<type>_g<n>。隐含「不同园区同一类型测点集合相同」的假设，
--    目前只在廊坊B1M2 验证过「同类型内设备之间一致」，跨园区未验。
CREATE TABLE IF NOT EXISTS dcim_td_stable (
  device_type  TEXT NOT NULL,
  point_group  TEXT NOT NULL DEFAULT 'g0',
  stable_name  TEXT NOT NULL,
  point_count  INTEGER NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (device_type, point_group)
);

-- 测点编码 → TDengine 列名。必须持久化：编码是任意字符串，列名有命名限制且可能撞名。
CREATE TABLE IF NOT EXISTS dcim_td_column (
  device_type TEXT NOT NULL,
  point_code  TEXT NOT NULL,
  td_column   TEXT NOT NULL,
  point_group TEXT NOT NULL DEFAULT 'g0',
  data_type   TEXT NOT NULL DEFAULT 'DOUBLE',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (device_type, point_code),
  UNIQUE (device_type, td_column)
);
CREATE INDEX IF NOT EXISTS idx_dcim_td_column_col ON dcim_td_column (td_column);

-- 存量库迁移（幂等，只增列 + 换主键，不动数据）：worker 启动时 schema.py 也会自动执行
ALTER TABLE dcim_td_column ADD COLUMN IF NOT EXISTS point_group TEXT NOT NULL DEFAULT 'g0';
ALTER TABLE dcim_td_stable ADD COLUMN IF NOT EXISTS point_group TEXT NOT NULL DEFAULT 'g0';
DO $$
DECLARE
  pk_cols INTEGER;
  pk_name TEXT;
BEGIN
  SELECT c.conname, cardinality(c.conkey)
    INTO pk_name, pk_cols
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
   WHERE t.relname = 'dcim_td_stable' AND c.contype = 'p';
  IF pk_cols = 1 THEN
    EXECUTE format('ALTER TABLE dcim_td_stable DROP CONSTRAINT %I', pk_name);
    ALTER TABLE dcim_td_stable ADD PRIMARY KEY (device_type, point_group);
  END IF;
END $$;

-- 测点分级：1 重要（进业务当前态表 + 1min/1h 聚合） / 2 普通（只进原始明细 + 1h）
-- 只影响冗余层，**原始明细一律全量落**，所以判错不会丢数据。
CREATE TABLE IF NOT EXISTS dcim_point_level (
  cluster    TEXT NOT NULL,
  guid       TEXT NOT NULL,
  level      SMALLINT NOT NULL,
  matched_by TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster, guid)
);
CREATE INDEX IF NOT EXISTS idx_dcim_point_level_level ON dcim_point_level (level);

-- 分级规则（可配置）：改规则不用改代码
CREATE TABLE IF NOT EXISTS dcim_level_rule (
  id         BIGSERIAL PRIMARY KEY,
  priority   INTEGER NOT NULL DEFAULT 100,
  match_on   TEXT NOT NULL,               -- device_name | device_type | point_code | point_name
  pattern    TEXT NOT NULL,               -- 关键词（子串匹配）
  level      SMALLINT NOT NULL,
  enabled    BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dcim_level_rule_pri ON dcim_level_rule (enabled, priority);

-- ============ 告警事件（ems-alarm-data，2026-10-13 新增） ============
-- ⚠️ `guid` 列存的是报文的 **event_log_id（告警 ID）** —— 共用落库路径（apply_batch）的
--    主键列就叫 guid，沿用它可以不改共用代码。
-- **三态 upsert 成一行**：同一事件推 触发(1)/刷新(2)/恢复(3)，谁最后推谁是最新状态。
-- ⚠️ `remove_time`/`remove_value` **仅 status=3 时写**；判「是否已恢复」**只能看 status**，
--    不能看它 != 0（设备真实恢复值可能就是 0 —— 与阈值那次 maxAlarm:0 同类坑）。
CREATE TABLE IF NOT EXISTS dcim_alarms (
  cluster       TEXT NOT NULL,
  guid          TEXT NOT NULL,           -- = 报文的 event_log_id（三态共用同一个）
  space_guid    TEXT,                    -- 房间 → dcim_spaces.guid
  status        TEXT,                    -- 1-触发 / 2-刷新 / 3-恢复
  alarm_type    INTEGER,                 -- 0-普通告警 / 1-通讯告警
  device_guid   TEXT,                    -- → dcim_devices.guid
  device_name   TEXT,
  point_guid    TEXT,                    -- → dcim_points.guid
  point_name    TEXT,
  alarm_level   INTEGER,                 -- 1-提示 2-一般 3-重要 4-紧急
  normal_range  TEXT,
  content       TEXT,
  alarm_time    BIGINT,                  -- status=1/2 时的告警时间（秒级 epoch）
  alarm_value   DOUBLE PRECISION,        -- status=1/2 时的告警值
  remove_time   BIGINT,                  -- 仅 status=3 时写
  remove_value  DOUBLE PRECISION,        -- 仅 status=3 时写
  payload       JSONB,
  -- 告警不用软删，但共用落库路径的 ON CONFLICT SET 会写这两列 → 必须存在
  is_deleted    BOOLEAN NOT NULL DEFAULT FALSE,
  deleted_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster, guid)
);
CREATE INDEX IF NOT EXISTS idx_dcim_alarms_space ON dcim_alarms (cluster, space_guid);
CREATE INDEX IF NOT EXISTS idx_dcim_alarms_status ON dcim_alarms (cluster, status);
CREATE INDEX IF NOT EXISTS idx_dcim_alarms_time ON dcim_alarms (cluster, alarm_time);

-- 每次状态流转追加一条（触发/刷新/恢复各一条）
CREATE TABLE IF NOT EXISTS dcim_alarms_history (
  id          BIGSERIAL PRIMARY KEY,
  cluster     TEXT NOT NULL,
  guid        TEXT NOT NULL,
  op          TEXT NOT NULL,
  payload     JSONB,
  valid_from  TIMESTAMPTZ,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  kafka_topic TEXT,
  partition   INTEGER,
  "offset"    BIGINT,
  UNIQUE (cluster, kafka_topic, partition, "offset")
);
CREATE INDEX IF NOT EXISTS idx_dcim_alarms_history_guid
  ON dcim_alarms_history (cluster, guid, ingested_at);
-- 按触发态时间反查（事件统计表匹配用）：当前态 alarm_time 会被刷新/恢复推送覆盖，
-- 只有历史表里 status='1' 那条的 timestamp 不变。
CREATE INDEX IF NOT EXISTS idx_dcim_alarms_history_trigger_ts
  ON dcim_alarms_history (((payload->>'timestamp')), cluster)
  WHERE (payload->>'status') = '1';

-- ============ 告警工单（ems-alarm-order，2026-10-20 新增） ============
-- 人工处理告警时推送（1-处理中 / 2-已处理）；同一 orderNo 多次推送 → upsert 成最新状态。
-- `guid` 存 `orderNo`（同告警事件用 event_log_id 一个道理）。
-- ⚠️ 实推报文比文档 §10 全：额外带 addrId/addrName、mgrObjId/mgrObjName、
--    mgrObjAttributeId/Name、level/content/value/alarmTime —— 已映射成与 dcim_alarms 同名的列。
CREATE TABLE IF NOT EXISTS dcim_alarm_orders (
  cluster       TEXT NOT NULL,
  guid          TEXT NOT NULL,           -- = 报文的 orderNo（工单 id）
  event_log_id  TEXT,                    -- → dcim_alarms.guid
  status        INTEGER,                 -- 1-处理中 / 2-已处理
  state         INTEGER,
  alarm_level   INTEGER,
  space_guid    TEXT,                    -- = addrId → dcim_spaces.guid
  space_name    TEXT,                    -- = addrName
  device_guid   TEXT,                    -- = mgrObjId → dcim_devices.guid
  device_name   TEXT,                    -- = mgrObjName
  point_guid    TEXT,                    -- = mgrObjAttributeId（测点编码）
  point_name    TEXT,                    -- = mgrObjAttributeName
  content       TEXT,
  alarm_value   DOUBLE PRECISION,        -- = value
  alarm_time    BIGINT,                  -- = alarmTime（秒级 epoch）
  response_time BIGINT,                  -- 响应时间（秒级 epoch）
  star          INTEGER,                 -- 0~3
  responder     TEXT,
  writer        TEXT,
  handler       TEXT,
  start_time    BIGINT,
  end_time      BIGINT,
  process       TEXT,
  process_result INTEGER,                -- 1-确认告警 / 2-已处理（已恢复正常）
  processing_operator       TEXT,
  processing_operator_name  TEXT,
  processed_operator        TEXT,
  processed_operator_name   TEXT,
  processed_operator_time   BIGINT,
  payload       JSONB,
  is_deleted    BOOLEAN NOT NULL DEFAULT FALSE,
  deleted_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster, guid)
);
CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_event
  ON dcim_alarm_orders (cluster, event_log_id);
CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_status
  ON dcim_alarm_orders (cluster, status);
CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_time
  ON dcim_alarm_orders (cluster, alarm_time);

CREATE TABLE IF NOT EXISTS dcim_alarm_orders_history (
  id          BIGSERIAL PRIMARY KEY,
  cluster     TEXT NOT NULL,
  guid        TEXT NOT NULL,
  op          TEXT NOT NULL,
  payload     JSONB,
  valid_from  TIMESTAMPTZ,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  kafka_topic TEXT,
  partition   INTEGER,
  "offset"    BIGINT,
  UNIQUE (cluster, kafka_topic, partition, "offset")
);
CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_history_guid
  ON dcim_alarm_orders_history (cluster, guid, ingested_at);

-- ============ 事件统计表：飞书园区群通报原文（2026-10-20） ============
-- 只存**输入**：群消息 + 匹配到的告警 guid。20 列统计行每次按窗口实时关联告警/工单，
-- 不固化快照（口径可变、工单会后补）。写入方：service/scheduled_task/incident_stats/，
-- 同一份 DDL 也在 schema.py 里幂等执行（CLI/同步任务首次跑时自动建表）。
CREATE TABLE IF NOT EXISTS dcim_incident_reports (
  id             BIGSERIAL PRIMARY KEY,
  chat_id        TEXT NOT NULL,
  message_id     TEXT NOT NULL,
  cluster        TEXT,               -- 园区名（由群配置映射）
  kind           TEXT NOT NULL,      -- sos(速报) | first(首报) | other
  title          TEXT,
  create_time    BIGINT NOT NULL,    -- 消息时间（秒级 epoch）
  event_time     BIGINT,             -- 正文【发生时间】解析结果（秒级 epoch，可空）
  text           TEXT,               -- 归一化纯文本（卡片已抽平）
  raw            JSONB,
  sender         TEXT,
  sender_id      TEXT,               -- 发送者 id：人=open_id，bot=app_id
  sender_id_type TEXT,               -- open_id / app_id
  report_value   DOUBLE PRECISION,   -- 报文里的【告警值】
  match_detail   TEXT,               -- 命中的匹配信号，如 time+value / time+value+llm
  event_log_id   TEXT,               -- 匹配到的 dcim_alarms.guid（可空）
  match_score    INTEGER,
  matched_at     TIMESTAMPTZ,
  llm_tried_at   TIMESTAMPTZ,        -- 已让模型仲裁过（判不出也标，一条只试一次）
  ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (chat_id, message_id)
);
-- 表若已按旧结构建过，上面的 CREATE 不会补列 → 逐列 ALTER
ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS sender_id TEXT;
ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS sender_id_type TEXT;
ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS report_value DOUBLE PRECISION;
ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS match_detail TEXT;
ALTER TABLE dcim_incident_reports ADD COLUMN IF NOT EXISTS llm_tried_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_cluster
  ON dcim_incident_reports (cluster, create_time);
CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_chat
  ON dcim_incident_reports (chat_id, create_time);
CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_event
  ON dcim_incident_reports (event_log_id);
CREATE INDEX IF NOT EXISTS idx_dcim_incident_reports_kind
  ON dcim_incident_reports (kind, create_time);

-- ============ 常用查询示例（均按园区隔离） ============
-- 空间树（递归 CTE）：
--   WITH RECURSIVE t AS (
--     SELECT * FROM dcim_spaces WHERE cluster = $1 AND guid = $2
--     UNION ALL
--     SELECT s.* FROM dcim_spaces s JOIN t
--       ON s.parent_guid = t.guid AND s.cluster = t.cluster
--   ) SELECT * FROM t;
-- 某设备下的点位（含单位/状态映射）：
--   SELECT guid, name, point_type, unit, status_map FROM dcim_points
--   WHERE cluster = $1 AND device_guid = $2;
-- 回溯某条告警的状态流转：
--   SELECT op, payload, ingested_at FROM dcim_alarms_history
--   WHERE cluster = $1 AND guid = $2 ORDER BY ingested_at;
-- 按园区看各表规模：
--   SELECT cluster, count(*) FROM dcim_devices GROUP BY cluster ORDER BY 2 DESC;
