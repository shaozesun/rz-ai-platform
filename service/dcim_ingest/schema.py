"""DCIM 元数据表结构（worker 启动时自动执行，幂等）。

与 `deploy/sql/dcim_schema.sql` 保持一致：该 .sql 供 DBA 审查/手工执行，
worker 侧用本模块在启动时 `CREATE TABLE IF NOT EXISTS`，免去手工建表步骤。

**园区维度（cluster）**：对方是每个园区一个独立集群，13 个园区的数据进同一个库。
- 每张表都带 `cluster`，值是园区名（如 '廊坊A'），来自消息所在的 Kafka 集群，不是报文字段
- 当前态表用**复合主键 (cluster, guid)**：不同园区出现同 guid 时不会互相覆盖
  （若用单列 guid 主键，后到的园区会静默覆盖前一个，属数据丢失）

除各实体的当前态表 +（仅告警类的）历史表 + DLQ 外，还包含**时序库建模注册表与测点分级**（宽表模型的配套）：
- `dcim_td_stable` / `dcim_td_column`：设备类型 → TDengine 超级表、测点编码 → 列名（映射必须持久化）
- `dcim_point_level` / `dcim_level_rule`：哪些测点算「重要」（进业务当前态表 + 多档聚合）

配套设计：`docs/dcim/dcim-timeseries-storage-design.md`
"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
  # ── 当前态：空间 ──
  '''
  CREATE TABLE IF NOT EXISTS dcim_spaces (
    cluster       TEXT NOT NULL,
    guid          TEXT NOT NULL,
    tag           TEXT,
    path          TEXT,
    name          TEXT,
    idc           TEXT,
    node_type     INTEGER,
    space_type    TEXT,
    parent_guid   TEXT,
    payload       JSONB,
    is_deleted    BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster, guid)
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_spaces_parent ON dcim_spaces (cluster, parent_guid)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_spaces_idc ON dcim_spaces (cluster, idc)',
  # ── 当前态：设备 ──
  '''
  CREATE TABLE IF NOT EXISTS dcim_devices (
    cluster           TEXT NOT NULL,
    guid              TEXT NOT NULL,
    name              TEXT,
    path              TEXT,
    idc               TEXT,
    node_type         INTEGER,
    device_type       TEXT,
    device_name       TEXT,            -- 设备**类型**名称（如「精密空调」），非设备名称（那是 name）
    extend_position   TEXT,
    space_guid        TEXT,
    payload           JSONB,
    is_deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster, guid)
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_devices_space ON dcim_devices (cluster, space_guid)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_devices_idc ON dcim_devices (cluster, idc)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_devices_type ON dcim_devices (cluster, device_type)',
  # ── 当前态：点位 ──
  '''
  CREATE TABLE IF NOT EXISTS dcim_points (
    cluster         TEXT NOT NULL,
    guid            TEXT NOT NULL,
    point_id        TEXT,
    name            TEXT,
    idc             TEXT,
    node_type       INTEGER,
    point_type      INTEGER,
    unit            TEXT,
    status_map_raw  TEXT,
    status_map      JSONB,
    device_guid     TEXT,
    -- 4 个告警阈值（9/29 版新增）：嵌套结构 EmsAlarmThreshold，
    -- 含 maxUse/minUse/maxAlarm/minAlarm/maxEvent/minEvent；整体存 JSONB
    hint_alarm      JSONB,
    general_alarm   JSONB,
    important_alarm JSONB,
    urgent_alarm    JSONB,
    payload         JSONB,
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster, guid)
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_points_device ON dcim_points (cluster, device_guid)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_points_idc ON dcim_points (cluster, idc)',
  # 配置类（空间/设备/点位）**不建历史表**：上游是每小时全量重推，历史表退化成快照堆
  # （430 万行里 77% 与上一条相同），且全仓库零读取方。见 models.TABLES（2026-10-21 移除）。
  # 告警类的历史表在下面各自的位置建。
  # ── 当前态：告警事件（`ems-alarm-data`）──
  # ⚠️ `guid` 列存的是报文的 **`event_log_id`（告警 ID）** —— 共用落库路径
  #    （repository.apply_batch）的主键列就叫 guid，沿用它可以不改共用代码。
  # **三态 upsert 成一行**：同一事件推 触发(1)/刷新(2)/恢复(3)，谁最后推谁是最新状态。
  '''
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
    alarm_level   INTEGER,                 -- 1-提示 2-一般 3-重要 4-紧急（与我们的 4 档 1:1）
    normal_range  TEXT,
    content       TEXT,
    alarm_time    BIGINT,                  -- status=1/2 时的告警时间（秒级 epoch）
    alarm_value   DOUBLE PRECISION,        -- status=1/2 时的告警值
    remove_time   BIGINT,                  -- ⚠️ **仅 status=3 时写**；判恢复只能看 status，别看它 != 0
    remove_value  DOUBLE PRECISION,        -- ⚠️ 同上（设备真实恢复值可能就是 0）
    payload       JSONB,
    -- 下面两列告警自己不用软删，但共用落库路径的 ON CONFLICT SET 会写它们 → 必须存在
    is_deleted    BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster, guid)
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarms_space ON dcim_alarms (cluster, space_guid)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarms_status ON dcim_alarms (cluster, status)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarms_time ON dcim_alarms (cluster, alarm_time)',
  '''
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
    -- 幂等：同一条 Kafka 消息（集群+topic+分区+offset 唯一）重复消费时不重复追加历史
    UNIQUE (cluster, kafka_topic, partition, "offset")
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarms_history_guid'
  ' ON dcim_alarms_history (cluster, guid, ingested_at)',
  # 按**触发态时间**反查告警（事件统计表匹配用）：当前态 alarm_time 会被刷新/恢复
  # 推送覆盖，只有历史表里 status='1' 那条的 timestamp 不变。部分表达式索引，
  # 只收触发推送。
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarms_history_trigger_ts'
  " ON dcim_alarms_history (((payload->>'timestamp')), cluster)"
  " WHERE (payload->>'status') = '1'",
  # ── 当前态：告警工单（`ems-alarm-order-rz`）──
  # 文档 §10 只列了 orderNo/eventLogId/status/responder/… 这十几项，但**实推报文更全**：
  # 还带 addrId/addrName（空间）、mgrObjId/mgrObjName（设备）、mgrObjAttributeId/Name（测点）、
  # level/content/value/alarmTime。这些正是报告里要的上下文，故一并落列；
  # 其余字段（state/isSuccess/reportProgress…）留在 payload。
  # `guid` 存 `orderNo`：共用 `repository.apply_batch` 的主键列就叫 guid。
  '''
  CREATE TABLE IF NOT EXISTS dcim_alarm_orders (
    cluster       TEXT NOT NULL,
    guid          TEXT NOT NULL,           -- = 报文的 orderNo（工单 id）
    event_log_id  TEXT,                    -- → dcim_alarms.guid（告警事件）
    status        INTEGER,                 -- 1-处理中 / 2-已处理
    state         INTEGER,                 -- 对方内部状态（文档未列，实推有）
    alarm_level   INTEGER,                 -- = 报文 level（1-提示 ~ 4-紧急）
    space_guid    TEXT,                    -- = addrId → dcim_spaces.guid
    space_name    TEXT,                    -- = addrName
    device_guid   TEXT,                    -- = mgrObjId → dcim_devices.guid
    device_name   TEXT,                    -- = mgrObjName
    point_guid    TEXT,                    -- = mgrObjAttributeId（测点编码）
    point_name    TEXT,                    -- = mgrObjAttributeName
    content       TEXT,                    -- 告警描述
    alarm_value   DOUBLE PRECISION,        -- = value
    alarm_time    BIGINT,                  -- = alarmTime（秒级 epoch）
    response_time BIGINT,                  -- 响应时间（秒级 epoch）
    star          INTEGER,                 -- 星级 0~3
    responder     TEXT,                    -- 响应人
    writer        TEXT,                    -- 填写人
    handler       TEXT,                    -- 处理人
    start_time    BIGINT,                  -- 开始处理时间（秒级；未填为 0 → NULL）
    end_time      BIGINT,                  -- 处理结束时间（秒级；未填为 0 → NULL）
    process       TEXT,                    -- 事件原因
    process_result INTEGER,                -- 1-确认告警 / 2-已处理（已恢复正常）
    processing_operator       TEXT,        -- 响应实际操作者 id
    processing_operator_name  TEXT,        -- 响应实际操作者名称
    processed_operator        TEXT,        -- 已处理实际填写者 id
    processed_operator_name   TEXT,
    processed_operator_time   BIGINT,
    payload       JSONB,
    -- 共用落库路径的 ON CONFLICT SET 会写这三列 → 必须存在
    is_deleted    BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster, guid)
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_event'
  ' ON dcim_alarm_orders (cluster, event_log_id)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_status'
  ' ON dcim_alarm_orders (cluster, status)',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_time'
  ' ON dcim_alarm_orders (cluster, alarm_time)',
  '''
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
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_alarm_orders_history_guid'
  ' ON dcim_alarm_orders_history (cluster, guid, ingested_at)',
  # ── 死信 ──
  '''
  CREATE TABLE IF NOT EXISTS dcim_dlq (
    id          BIGSERIAL PRIMARY KEY,
    cluster     TEXT,
    topic       TEXT,
    partition   INTEGER,
    "offset"    BIGINT,
    error       TEXT,
    raw         TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_dlq_cluster ON dcim_dlq (cluster)',

  # ── 时序库（TDengine）建模注册表 ──
  # 宽表模型：**一个设备类型一张超级表**（列 = 该类型的各个测点），
  # 一台设备一张子表。本表记「设备类型 → 超级表名」，是写入端与查询端共用的唯一来源。
  #
  # ⚠️ 主键是 `(device_type, point_group)`：设备类型编码对方给的、全局一致；
  # `point_group` 是测点分组（g0 沿用 `st_<type>`，g1 起是 `st_<type>_g<n>`）。
  # **为什么拆组**：TDengine 单表列数上限 4,096，`00AG`(4,068 数据列) / `00WG`(4,043)
  # 余量只剩 19~25 列，再冒一个新测点就会 ALTER 失败、整园区入库卡死。容量线与分组规则
  # 见 `td_schema.GROUP_MAX_COLUMNS` 与 `docs/dcim/dcim-timeseries-storage-design.md`。
  # 隐含假设：**不同园区的同一 device_type 测点集合相同**。目前只在廊坊B1M2 验证过
  # 「同类型内设备之间一致」，跨园区还没验 —— 若将来冲突，可改成 (cluster, device_type)。
  '''
  CREATE TABLE IF NOT EXISTS dcim_td_stable (
    device_type  TEXT NOT NULL,             -- 设备类型编码（对方下发，UUID 形式）
    point_group  TEXT NOT NULL DEFAULT 'g0',-- 测点分组：g0 / g1 / g2…
    stable_name  TEXT NOT NULL,             -- TDengine 超级表名（归一化后）
    point_count  INTEGER NOT NULL DEFAULT 0,-- 已注册列数（冗余，便于巡检）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_type, point_group)
  )
  ''',

  # 测点编码 → TDengine 列名。
  # **必须持久化**：测点编码是任意字符串（如 `HT_Temp`），而列名受 TDengine 命名限制，
  # 归一化后还可能撞名。映射一旦不稳定，历史数据就再也对不上了。
  # 经验：行业里「文档编码规范 vs 实际推送编码」不一致极常见（多因固件版本差异），
  # 所以映射是**数据**，不是代码里的常量。
  '''
  CREATE TABLE IF NOT EXISTS dcim_td_column (
    device_type TEXT NOT NULL,
    point_code  TEXT NOT NULL,              -- 对方原始点位编码（如 'HT_Temp'）
    td_column   TEXT NOT NULL,              -- 归一化后的列名（如 'ht_temp'）
    point_group TEXT NOT NULL DEFAULT 'g0', -- 所属测点分组（存量一律 g0，永不换组）
    data_type   TEXT NOT NULL DEFAULT 'DOUBLE',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_type, point_code),
    UNIQUE (device_type, td_column)         -- 同一类型内列名不可重复
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_td_column_col ON dcim_td_column (td_column)',

  # ── 迁移：存量库补 point_group、主键改复合（`CREATE TABLE IF NOT EXISTS` 不会改已存在的表）──
  # 只增列 + 换主键，**不动任何一行数据**：存量测点默认落 g0，即继续用原来的 `st_<type>`。
  'ALTER TABLE dcim_td_column ADD COLUMN IF NOT EXISTS point_group'
  " TEXT NOT NULL DEFAULT 'g0'",
  'ALTER TABLE dcim_td_stable ADD COLUMN IF NOT EXISTS point_group'
  " TEXT NOT NULL DEFAULT 'g0'",
  # 幂等：只在主键还是单列（旧结构）时重建；新库建表时已是复合主键，直接跳过
  '''
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
  ''',

  # ── 测点分级：决定「哪些测点进业务当前态表」「哪些建多档聚合」 ──
  # 1 = 重要（电表 / UPS / PDU / 空调 / 机房温湿度…）：进当前态表 + 建 1min+1h 聚合
  # 2 = 普通：只进原始明细 + 1h 聚合
  # 分级只影响**冗余层**（当前态 / 聚合档位），**原始明细一律全量落**，所以判错不会丢数据。
  '''
  CREATE TABLE IF NOT EXISTS dcim_point_level (
    cluster    TEXT NOT NULL,
    guid       TEXT NOT NULL,               -- 测点 guid
    level      SMALLINT NOT NULL,           -- 1 重要 / 2 普通
    matched_by TEXT,                        -- 命中的规则（优先级 + 模式），便于自查
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster, guid)
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_point_level_level ON dcim_point_level (level)',

  # 分级规则（可配置）：按设备名称 / 设备类型 / 测点编码关键词匹配，`priority` 小的先生效。
  # 「哪些算重要」会随分析需求变化 —— 做成**数据**，改规则不用改代码、不用发版。
  '''
  CREATE TABLE IF NOT EXISTS dcim_level_rule (
    id         BIGSERIAL PRIMARY KEY,
    priority   INTEGER NOT NULL DEFAULT 100,
    match_on   TEXT NOT NULL,               -- device_name | device_type | point_code | point_name
    pattern    TEXT NOT NULL,               -- 关键词（子串匹配）
    level      SMALLINT NOT NULL,           -- 命中后给的级别
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )
  ''',
  'CREATE INDEX IF NOT EXISTS idx_dcim_level_rule_pri ON dcim_level_rule (enabled, priority)',
)


def ensure_schema(dsn: str) -> None:
  """连接 PG 并幂等建表（worker 启动时调用）。"""
  import psycopg

  with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
      for stmt in SCHEMA_STATEMENTS:
        cur.execute(stmt)
    conn.commit()
