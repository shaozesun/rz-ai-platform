import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
  # ==================== 服务 ====================
  HOST: str = '0.0.0.0'
  PORT: int = 8000
  WORKERS: int = 4
  PROJECT_NAME: str = '智能运维平台'
  VERSION: str = '1.0.0'

  # ==================== JWT ====================
  JWT_PRIVATE_KEY: str = ''
  JWT_PUBLIC_KEY: str = ''
  ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
  REFRESH_TOKEN_EXPIRE_DAYS: int = 7

  # ==================== 短信 ====================
  SMS_PROVIDER: str = 'aliyun'  # aliyun / tencent
  SMS_ACCESS_KEY: str = ''
  SMS_SECRET_KEY: str = ''
  SMS_SIGN_NAME: str = '润泽科技'
  SMS_TEMPLATE_CODE: str = ''
  SMS_DEV_MODE: bool = False     # 开发模式跳过真实发送

  # ==================== MongoDB ====================
  MONGO_HOST: str = 'localhost'
  MONGO_PORT: int = 27017
  MONGO_USER: str = ''
  MONGO_PASSWORD: str = ''
  MONGO_DB_NAME: str = 'rz_ai_platform'
  MONGO_MAX_POOL_SIZE: int = 50
  MONGO_MIN_POOL_SIZE: int = 5
  MONGO_REPLICA_SET: str = ''     # 副本集名称，空则不启用

  # ==================== Redis ====================
  REDIS_HOST: str = 'localhost'
  REDIS_PORT: int = 6379
  REDIS_PASSWORD: str = ''
  REDIS_DB: int = 0
  REDIS_PREFIX: str = 'rz:'

  # ==================== LLM / Embedding ====================
  # 主模型 (对话/意图分类/查询重写)
  LLM_BASE_URL: str = ''
  LLM_MODEL: str = 'qwen3.5'
  LLM_API_KEY: str = ''

  # 视觉模型 (隐患检测)
  VISION_BASE_URL: str = ''
  VISION_MODEL: str = 'qwen3.5-vl'
  VISION_API_KEY: str = ''

  # Embedding 模型
  EMBEDDING_BASE_URL: str = ''
  EMBEDDING_MODEL: str = 'bge-m3'
  EMBEDDING_API_KEY: str = ''

  # Reranker 模型 (Qwen3-Reranker-8B)
  RERANKER_BASE_URL: str = ''
  RERANKER_MODEL: str = 'Qwen3-Reranker-8B'
  RERANKER_API_KEY: str = ''

  # 模型调用参数
  LLM_TIMEOUT: int = 60
  LLM_MAX_RETRIES: int = 2
  LLM_TEMPERATURE: float = 0.7

  # Agent 容错：事件流最大静默间隔（秒），防整轮挂起「一直调用中」。
  # 必须大于 AGENT_LLM_TIMEOUT（否则 LLM 允许的慢流会被外层守卫先掐断）。
  AGENT_EVENT_TIMEOUT: int = 360

  # 首 token 兜底：本轮开始 → 第一个「有意义事件」（正文/工具调用/工具结果/进度）
  # 之间不得超过这个秒数。与上面 360s 分工不同 —— 360s 管的是中途静默，而调度长
  # 工具（沙箱分析/报表）时静默分钟级是合法的，不能缩短；但「模型已建立流却一个
  # token 都不吐」是异常（实测事故：上游 200 OK 后静默 34s，用户只能干等直到取消）。
  # 30 须大于压上下文后的冷启动 prefill（32k 上下文 ≈14s），否则会误杀正常长会话。
  AGENT_FIRST_TOKEN_TIMEOUT: int = 30

  # Agent 模型专属超时：报表流程里 LLM 携带大上下文推理，chunk 间隔远大于
  # 普通对话的 LLM_TIMEOUT=60。单独配置避免 60s 就 ReadTimeout → 「回复生成中断」。
  AGENT_LLM_TIMEOUT: int = 300
  # 超时多是模型慢，重试整个流只会让用户等更久，1 次即止。
  AGENT_LLM_MAX_RETRIES: int = 1

  # 循环检测：**同一工具累计调用**的告警/中止上限（原为硬编码常量 20，2026-10-15 起可配）。
  # 临时放宽到 40 的原因：综合管理平台有一批接口按部门返回（`query_user_list` 的 dept_id 必填），
  # 「查全公司人员」要跨 25 个部门各调一次 → 20 的闸会在第 20 次**中止整轮**、任务直接失败。
  # ⚠️ 放宽是**临时**措施，代价是真跑飞时多烧的时间/成本上限翻倍；正解是**把扇出下沉到
  # 服务端**（对齐客户端的 `all_companies`，见 `client._aggregate_companies`），做完即可调回 20。
  # 注意这与「连续 N 次完全相同调用」那道闸无关（扇出每次参数不同，不触发它；那道闸管真死循环）。
  AGENT_TOOL_FREQ_HARD: int = 40
  AGENT_TOOL_FREQ_WARN: int = 10

  # Agent 单轮最大 super-step 数（模型调用 + 工具调用各计一步）。
  # 默认 25 偏紧：报表流程（数据拉取 → analysis_load → 多次 exec/write/str_replace
  # 迭代）会超过。放宽到 100 给足余量；真正的死循环由 LoopDetector 拦截
  # （连续相同调用 ≥5 / 单工具累计 ≥20 即中止），不受此影响。
  AGENT_RECURSION_LIMIT: int = 100

  # ==================== 日志 ====================
  LOG_LEVEL: str = 'INFO'
  LOG_DIR: str = 'logs'
  LOG_FILE_ENABLED: bool = True
  LOG_FILE_RETENTION: int = 30
  LOG_AUDIT_ENABLED: bool = True

  # ==================== RAG ====================
  CHUNK_SIZE: int = 1200
  CHUNK_OVERLAP: int = 150
  RAG_TOP_K: int = 6
  RAG_FETCH_K_GLOBAL: int = 300
  RAG_FETCH_K_PER_FILE: int = 12
  RAG_FILE_TOP_K: int = 10
  RAG_FUSION_TOP_K: int = 50
  RAG_FINAL_TOP_K: int = 10
  RAG_FILE_SCAN_LIMIT: int = 10000
  RAG_DENSE_RECALL_LIMIT: int = 250
  RAG_SPARSE_RECALL_LIMIT: int = 250

  # RAG 融合权重 (v2)
  RAG_FUSION_A: float = 0.20
  RAG_FUSION_B: float = 0.10
  RAG_FUSION_C: float = 0.50
  RAG_FUSION_D: float = 0.20
  RAG_FUSION_E: float = 0.0

  # RAG 融合权重 (v1 兼容)
  RAG_FUSION_ALPHA: float = 0.20
  RAG_FUSION_BETA: float = 0.40
  RAG_FUSION_GAMMA: float = 0.40

  # BM25
  RAG_SPARSE_NORM: str = 'max'
  RAG_SIGMOID_SCALE: float = 1.5
  RAG_SPARSE_SIGMOID_SCALE: float = 0.5
  RAG_BM25_SIGMOID_SCALE: float = 2.0
  RAG_DENSE_WEIGHT: float = 0.65
  RAG_SPARSE_WEIGHT: float = 0.35

  # 重排序
  RAG_CROSS_ENCODER_ENABLED: bool = True
  RAG_CROSS_ENCODER_PRE_FILTER: int = 50
  RAG_CROSS_ENCODER_WEIGHT: float = 0.75
  RAG_INTENT_CLASSIFY_ENABLED: bool = False
  RAG_QUERY_REWRITE_ENABLED: bool = True

  # 检索缓存
  RAG_CACHE_ENABLED: bool = False
  RAG_CACHE_TTL: int = 3600

  # Parent-Child 切分
  RAG_A_PARENT_ENABLED: bool = True
  RAG_A_PARENT_CHUNK_SIZE: int = 1500
  RAG_A_PARENT_CHUNK_OVERLAP: int = 80
  RAG_A_CHILD_CHUNK_SIZE: int = 600
  RAG_A_CHILD_CHUNK_OVERLAP: int = 60
  RAG_A_FETCH_K_CHILD: int = 500

  RAG_PER_SOURCE_CAP: int = 5
  RAG_PER_FILE_CAP: int = 5
  RAG_PRIMARY_FILE_CHUNKS: int = 3
  RAG_PRIMARY_MIN_SCORE: float = 0.40
  RAG_PRIMARY_GAP_THRESHOLD: float = 0.075
  RAG_RERANK_LOG_TOP_N: int = 50
  RAG_MERGED_LOG_TOP_N: int = 20
  RAG_BM25_K1: float = 1.2
  RAG_BM25_B: float = 0.75
  RAG_RRF_K: int = 60
  RAG_FILE_SIM_THRESHOLD: float = 0.40
  RAG_SPARSE_GATE_THRESHOLD: float = 0.05
  RAG_SPARSE_GATE_DECAY: float = 0.35
  RAG_RERANK_LEXICAL_WEIGHT: float = 0.55

  # RAG 服务 URL (远程检索用)
  RAG_SERVICE_URL: str = 'http://localhost:8000'

  RAG_DEBUG: bool = False

  # ==================== Milvus ====================
  MILVUS_URI: str = 'http://localhost:19530'
  MILVUS_TOKEN: str = ''
  MILVUS_DB_NAME: str = 'default'

  # ==================== 文件 ====================
  UPLOAD_DIR: str = str(Path(__file__).parent.parent / 'uploads')
  EMBEDDED_DIR: str = str(Path(__file__).parent.parent / 'embedded')
  VIDEO_DIR: str = str(Path(__file__).parent.parent / 'videos')
  SUPPORTED_EXTENSIONS: str = 'pdf,docx,xlsx,xls,csv,txt,md,png,jpg,jpeg'

  # ==================== Video Generation Plugins ====================
  PLUGIN_API_TOKEN: str = ''
  PLUGIN_LLM_GENERATOR_URL: str = 'http://localhost:8010'
  PLUGIN_IMAGE_CONVERTER_URL: str = 'http://localhost:8020'
  PLUGIN_VIDEO_RENDERER_URL: str = 'http://localhost:8030'
  PUBLIC_BASE_URL: str = 'http://localhost:8000'

  # TTS
  TTS_PROVIDER: str = 'qwen'  # openai / qwen
  TTS_API_URL: str = 'http://localhost:8000'
  # 上游 TTS 已换 vLLM-Omni 后端（单请求 ≈0.6s、并发上限 64，见 docs/tts-vllm-omni-deploy-manual.md）；
  # 生产 --workers 4，进程内信号量限不住全局，故用 Redis 跨进程配额（见 service/video/pipeline.py）
  TTS_MAX_CONCURRENCY: int = 64
  TTS_LOCAL_CONCURRENCY: int = 64  # 单进程在飞上限，防几十页一次性堆成几十个等待者
  # 单次请求读超时：单段合成实测 ≈50s，两槽都忙时新请求还要再等一次周转（≈50s），
  # 合计 ≈100s —— 原来的 120s 余量太薄，负载下必然边界性超时
  TTS_REQUEST_TIMEOUT: int = 300
  # 只是「忙」必须等下去：超时时间要覆盖整个排队周期，否则等待者成批放行、
  # 上游瞬间超并发（2026-09-20 事故）
  TTS_SLOT_WAIT_TIMEOUT: int = 600
  # 槽位计数器租约（DistributedSemaphore 无 TTL，worker 被 kill 会漏槽位）；
  # 须大于单请求最长耗时（120s 超时 × 3 次重试 + 退避 ≈ 400s）
  TTS_SLOT_LEASE_TTL: int = 600

  # ==================== 对话窗口「讲解视频」（agent_video，纯加法能力）====================
  # 与表单页视频功能（service/video/，走 8010/8020/8030 插件）**完全独立**：独立模块、
  # 独立集合（agent_video_tasks）、独立端点。开关关闭时对话侧无任何新按钮/工具，
  # 旧链路也不受任何影响。
  VIDEO_AGENT_ENABLED: bool = False
  VIDEO_AGENT_SLIDES_DPI: int = 144        # PPT→PNG 渲染 DPI（144 够 1080p）
  VIDEO_AGENT_MAX_PAGES: int = 60          # 单份 PPT 页数上限（防超长任务）
  VIDEO_AGENT_SOFFICE_TIMEOUT: int = 300   # soffice 转 PDF 超时（秒）
  VIDEO_AGENT_FFMPEG_TIMEOUT: int = 600    # ffmpeg 合成超时（秒）
  # 解说词：并发 ≤3（别把 LLM 网关打满）；失败重试 3 次，退避 1s→8s + 抖动（照 DeerFlow）
  VIDEO_AGENT_NARRATION_CONCURRENCY: int = 3
  # 配音并发：**与解说词分开**——解说词受 LLM 网关限流（小并发才稳），而 TTS 侧的配额
  # 是 TTS_MAX_CONCURRENCY（64，Redis 跨进程信号量），没必要跟着一起压到 3。
  VIDEO_AGENT_TTS_CONCURRENCY: int = 8
  # 工具**阻塞等待**任务跑完的上限（秒）：等到完成，回复里才能直接带上这个视频（不靠前端
  # 另外补卡片）；超时则不取消任务，退回"还在后台跑，完成后卡片自己出现"。50 页 ≈10 分钟。
  VIDEO_AGENT_WAIT_TIMEOUT: int = 1200
  # 每个会话保留最近 N 个视频任务的**完整目录**（可继续"只改某一页"）；更旧的只留 out.mp4，
  # 清掉 segments/audio/slides/checks——中间产物远大于成片（50 页任务 140M+ vs 成片 32M），
  # 而目录只增不减（实测单会话 732M）。
  # **默认 0 = 不清理**（用户要求先关掉：清掉中间产物后，旧任务就不能再"只改某一页"了）。
  # 需要时在 deploy/config/.env.production 里设成 3/5 之类即可，代码路径不用动。
  VIDEO_AGENT_KEEP_FULL_TASKS: int = 0
  VIDEO_AGENT_RETRY_ATTEMPTS: int = 3
  # RITL 自检：渲染后先做确定性检查（空白/全黑/页数），再用视觉模型看 contact sheet
  VIDEO_AGENT_RITL_ENABLED: bool = True
  VIDEO_AGENT_RITL_SHEET_PAGES: int = 6    # 一次 contact sheet 拼几页
  # 播放/下载链接的签名有效期（秒）。**不把用户真 JWT 放进 URL**（会进访问日志/浏览器历史，
  # 且 access token 只有 30 分钟，长视频后段 seek 会 401）——改用 Redis 短期 token。
  VIDEO_URL_TOKEN_TTL: int = 3600
  # 静图动效（Ken Burns 运镜）：none（默认，静态）/ zoom_in / zoom_out / pan_left / pan_right
  # / mixed（按页在四种运镜间轮换）。
  # **默认 none**：讲解以内容可读为先，静态画面配 `-tune stillimage` 编码又快又小；
  # 动效只是"锦上添花"，需要时用户说一句或改这个开关即可。片头/片尾另有 0.4s 淡入淡出。
  VIDEO_AGENT_MOTION: str = 'none'

  # 解说词烧成画面底部字幕（drawtext，见 service/agent_video/subtitles.py）。
  # 默认开；若某类 PPT 底部内容（页脚/表格）被字幕挡住，可在 .env 里关掉。
  VIDEO_AGENT_SUBTITLE: bool = True

  # ==================== MinerU PDF 解析 ====================
  MINERU_ENABLED: bool = True
  MINERU_LOCAL_API_URL: str = ''  # 自建 mineru-api，如 http://10.102.170.26:18000
  MINERU_API_BASE: str = ''
  MINERU_API_TOKEN: str = ''
  MINERU_BACKEND: str = 'pipeline'
  MINERU_LANG: str = 'ch'
  MINERU_TIMEOUT: int = 600
  MINERU_API_POLL_INTERVAL: int = 5
  MINERU_API_MAX_POLL: int = 60

  # ==================== Scheduler ====================
  SCHEDULER_ENABLED: bool = True
  SCHEDULER_MAX_CONCURRENT_LLM_CALLS: int = 10
  SCHEDULER_QUEUE_TIMEOUT: int = 120
  SCHEDULER_SLOT_ACQUIRE_TIMEOUT: int = 5
  SCHEDULER_STREAM_SLOT_TIMEOUT: int = 10

  # ==================== 定时任务（定时报告/巡检，复用 Agent 跑一轮） ====================
  SCHEDULED_TASKS_ENABLED: bool = False     # 总开关，默认关闭
  SCHEDULED_TASK_POLL_INTERVAL: int = 30    # 后台轮询到期任务间隔（秒）
  SCHEDULED_TASK_LOCK_TTL: int = 600        # Redis 锁 TTL（秒），防多 worker 重复触发
  # 定时任务跑 Agent 用身份：空 = 服务账号（MGMT_USERNAME/MGMT_PASSWORD，mgmt 查询按服务身份）；
  # 非空 = 指定 rz 用
  # 户（用该用户的 mgmt 绑定身份查询）
  SCHEDULED_TASK_SERVICE_USER_ID: str = ''
  # 服务账号权限（逗号分隔）：授予 Agent 对话 + 平台工具访问，按需增减
  SCHEDULED_TASK_SERVICE_PERMISSIONS: str = 'ai:agent,query:platform/access'
  # cron 表达式按此本地时区计算（用户填的时间是本地时间）
  # 预设分析报告失败自动重试（仅预设报告；用户自定义任务不重试）：
  # 上游对长流式生成不稳（实测 7 轮里 4 轮 `StreamChunkTimeoutError` 中途卡死），
  # 报告无人值守，抖一次就丢一份 → 对「流式中断/超时/5xx」这类瞬时错误自动重试。
  SCHEDULED_TASK_REPORT_RETRY: int = 2          # 重试次数（0=不重试）。实测单发长报告失败率 ~1/4~1/2
                                                # （上游长流式偶发卡死，非我们超时误杀）→ 重试 2 次后收敛到 ~6%
  SCHEDULED_TASK_REPORT_RETRY_DELAY: int = 10   # 重试前等待秒数（另加 0~5s 抖动）
  # 周报逐单取详情的上限：周报要与月报形成区分（逐单看根因/处置），但详情接口是**串行**的
  # （pen 同账号×接口只允许 1 个在途，不能并发），上限即运行时长保护。超限只取代表单优先的前 N 个。
  SCHEDULED_TASK_REPORT_WEEKLY_DETAIL_MAX: int = 200
  # 事件工单 ↔ DCIM 告警「判定表」（alarm_decision，键 = 工单号+发生时间+口径）日常直接复用；
  # 判定规则变更、或要按新规则重判时置 True：读取被绕过、重算后按同一 _id 覆盖写回。
  # 日常保持 False，避免每份报告重复点查/仲裁。
  SCHEDULED_TASK_REPORT_ALARM_REFRESH: bool = False
  # 定时任务并发闸（跨进程 Redis 计数）：调度器是"到点就 asyncio.create_task"，**原本无上限** ——
  # 多份重任务撞在一起会同时打上游，而上游对长流式生成本就不稳（并发越高越易被抢占）。
  # 上限只为保护、不为提速（报告仍按错峰逐档跑）。
  SCHEDULED_TASK_MAX_CONCURRENCY: int = 3
  # 槽位租约：DistributedSemaphore 是 INCR/DECR 计数、无过期，worker 被 kill 会永久漏一个槽位。
  # 给计数键加 TTL 让泄漏自愈（须大于单次运行时长上限：报告含重试最长约 15–20 分钟）。
  SCHEDULED_TASK_SLOT_LEASE_TTL: int = 1800
  SCHEDULED_TASK_TIMEZONE: str = 'Asia/Shanghai'
  # 预设报告失败的通知接收人（逗号分隔 rz user_id）。预设的 created_by='system'，
  # 原来的 create_notification 直接发给它 → 任何登录用户都看不到（通知黑洞）：
  # 2026-10-26 24 份报告集体失败，页面上一条提示都没有。留空则自动取内置管理员
  # 角色（roles 含 admin/root）的 ACTIVE 用户。用户自建任务失败仍只通知本人。
  SCHEDULED_TASK_ALERT_USER_IDS: str = ''

  # ==================== 部署 ====================
  ALLOWED_ORIGINS: str = '*'  # 生产环境应改为具体域名 (逗号分隔)，如 https://fire.whitegiveking.cn
  RATE_LIMIT_PER_MINUTE: int = 60
  RATE_LIMIT_SMS_PER_HOUR: int = 20

  # Cloudflare Turnstile
  TURNSTILE_SITE_KEY: str = ''
  TURNSTILE_SECRET_KEY: str = ''

  # 验证码方案: 'pillow' (本地数学算式) | 'turnstile' (Cloudflare)
  CAPTCHA_PROVIDER: str = 'pillow'

  # 受保护的管理员手机号（其他 admin 不能改其角色/权限/状态/密码）
  PROTECTED_ADMIN_PHONE: str = '18888888888'
  
  # 权限全开模式 (开发/调试用, 认证用户自动获得全部权限)
  PERMISSION_OPEN_MODE: bool = False

  # ==================== Agent ====================
  AGENT_ENABLED: bool = False     # 总开关，默认关闭，开发环境手动开启

  # ==================== Agent Checkpointer (参照 DeerFlow database 块) ====================
  # 后端类型: 'memory' | 'postgres'
  AGENT_CHECKPOINT_BACKEND: str = 'memory'

  # PostgreSQL 连接 DSN（仅 postgres 后端需要）
  AGENT_CHECKPOINT_POSTGRES_URL: str = ''

  # Checkpoint 存储模式（参照 DeerFlow checkpoint_channel_mode）
  # 'full' — 全量快照（默认）；'delta' — 增量存储，长对话大幅降低存储开销
  AGENT_CHECKPOINT_CHANNEL_MODE: str = 'full'

  # Delta 模式全量快照间隔（参照 DeerFlow checkpoint_delta_snapshot_frequency）
  AGENT_CHECKPOINT_DELTA_SNAPSHOT_FREQUENCY: int = 1000

  # ==================== Agent 摘要压缩（参照 DeerFlow summarization 块） ====================
  # 总开关
  AGENT_SUMMARIZE_ENABLED: bool = True

  # 模型上下文窗口（真实 token 数）。fraction 类触发/保留条件按它换算，不再写死 128000。
  # 实测来源 error.log："This model's maximum context length is 262144 tokens"
  AGENT_MODEL_MAX_INPUT_TOKENS: int = 262144

  # 触发条件（OR 逻辑，命中任一即触发压缩）
  # 格式: "type:value"，如 "tokens:32000" / "messages:50" / "fraction:0.8"
  # 多个条件逗号分隔，示例: "tokens:32000,fraction:0.9"
  #
  # **用绝对 token，不用窗口比例**（2026-09-17 实测事故）：原来配 fraction:0.6 ×
  # 262144 = 157k tokens 才压，而长会话实际只到 63k → 永不触发，上下文只增不减，
  # 冷启动 prefill 首 token 涨到 27s（实测本端点 ≈2300 tokens/s，1 万 token ≈4.3s），
  # 用户体感「一直正在思考」。窗口比例是给 8K 小窗口模型的建议（DeerFlow 文档原例：
  # 8K 用 4000-6000），套到 256k 窗口等于关闭压缩。
  # 32k 对齐 DeerFlow 的 config.example.yaml 默认（trigger: tokens 32000，
  # keep: messages 10），冷启动 ≈14s。DeerFlow 性能文档同样写「aggressive
  # summarization, value: 8000 # Lower threshold for faster response」。
  AGENT_SUMMARIZE_TRIGGER: str = 'tokens:32000'

  # 保留近期上下文策略
  # 格式: "type:value"，如 "messages:10" / "tokens:6000" / "fraction:0.3"
  # **用 token 型不用 messages 型**：本项目单条工具结果封顶 12000 字符（≈6000
  # token 估算），10 条内的 token 量可差 10 倍；而 trigger 的 reported 含固定开销
  # （SYSTEM_PROMPT 14069 字符 + 工具 schema，约 15~25k），32k 触发时 messages
  # 常常只有 7~9 条 → messages:10 的 cutoff 恒为 0，触发了却压不动。
  # 6000 约等于最坏情况 1 条工具结果，保证压缩后总量一定有明显下降；
  # 不理想时改回 "messages:10" 即可回滚（只改配置，无需改代码）。
  AGENT_SUMMARIZE_KEEP: str = 'tokens:6000'

  # 送给摘要模型的 token 上限（参照 DeerFlow trim_tokens_to_summarize）
  AGENT_SUMMARIZE_TRIM_TOKENS: int = 4000

  # 压缩时必须原样保留其工具结果的工具名（逗号分隔）。默认这两个是「字典类」权威对照：
  # 公司/园区名 ↔ companyId/kingdeeCompanyId、部门树 —— 体量小、价值高，一旦被压掉，
  # 模型就只能靠摘要转述推断「园区名对应哪个 id」（实测事故：把园区名说错）。
  AGENT_SUMMARIZE_PROTECTED_TOOLS: str = (
    'mgmt__query_company_list,mgmt__query_dept_list'
  )

  # 自定义摘要 prompt（空 = 使用默认 prompt）
  AGENT_SUMMARIZE_PROMPT: str = ''

  # 工具输出进上下文前封顶：page_size 上限 + 单条工具结果最大字符数
  AGENT_MAX_PAGE_SIZE: int = 20
  # 12000 对齐 DeerFlow 的 tool_output_config.externalize_min_chars；工具结果是
  # 上下文最大来源（实测某长会话里占 87%），降上限直接减小每轮 prefill。超出部分
  # 不会丢：走 tools_factory 的「归档全文 + build_synopsis() + history_read 回读指引」。
  AGENT_MAX_TOOL_OUTPUT_CHARS: int = 12000

  # ==================== Agent 数据分析沙箱（参照 DifySandbox / DeerFlow 模式） ====================
  AGENT_ANALYSIS_ENABLED: bool = False        # 总开关，默认关闭
  AGENT_ANALYSIS_DATA_DIR: str = str(Path(__file__).parent.parent / 'data' / 'analysis')
  AGENT_ANALYSIS_TIMEOUT: int = 60            # 单次代码执行超时（秒）
  # 单数据集最大行数（拉取上限）。2026-10-25 由 10000 放宽到 50000：生产 153GB 内存、
  # 最大落盘数据集约 5.2KB/行，5 万行宽表在 4GB 沙箱内可跑；再高会同时顶高
  # backend 进程（records + DataFrame 双份）。调小请同步看 MAX_MEMORY_MB。
  AGENT_ANALYSIS_MAX_ROWS: int = 50000
  AGENT_ANALYSIS_PAGE_SIZE: int = 500         # 拉取平台数据的分页大小
  AGENT_ANALYSIS_MAX_MEMORY_MB: int = 4096    # 沙箱子进程内存上限（RLIMIT_AS）
  # 跨进程同时在跑的 analysis_exec 上限（Redis 信号量）：沙箱内存放宽后，
  # 4 个 worker × 多会话并发会成倍叠加，必须限住。Redis 不可用时 fail-open。
  AGENT_ANALYSIS_MAX_CONCURRENCY: int = 2
  AGENT_ANALYSIS_MAX_OUTPUT_CHARS: int = 4000 # 单次执行返回结果上限
  AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION: int = 20
  AGENT_ANALYSIS_MAX_ARTIFACT_MB: int = 20     # 单产物大小上限（MB），超限剔除
  AGENT_ANALYSIS_MAX_ARTIFACTS: int = 10       # 单次执行产物数量上限，超限保留最近的
  # 沙箱内报告中文字体（捆绑的文泉驿微米黑 .ttc，跨平台；经 RZ_CJK_FONT env 传给沙箱脚本）
  RZ_CJK_FONT: str = str(Path(__file__).parent.parent / 'assets' / 'fonts' / 'wqy-microhei.ttc')
  # WeasyPrint 依赖的 Pango/Cairo 动态库搜索路径（非系统默认位置时配置，如 conda env 的 lib 目录；
  # 注入 DYLD_LIBRARY_PATH/LD_LIBRARY_PATH。Linux/Docker 下 apt 装系统库则留空）
  AGENT_ANALYSIS_EXTRA_LIB_PATH: str = ''

  # ==================== Agent 对话文件上传（会话级，不跨会话持久化） ====================
  AGENT_UPLOAD_MAX_FILES_PER_SESSION: int = 20  # 单会话最大上传文件数，超限拒绝新上传
  AGENT_UPLOAD_MAX_FILE_MB: int = 20            # 单文件大小上限（MB），超限拒绝
  # PPT 单独放宽：带图的汇报稿常 20~80MB（实测用户 93PR 操作流程 PPT 被 20MB 上限挡掉，
  # 前端又没能显示拒绝原因 → 用户以为已上传、agent 却查不到）。nginx 侧 client_max_body_size
  # 已放宽到 200m，故这里给到 100MB；「讲解视频」要的是原文件，不能只存解析结果。
  AGENT_UPLOAD_MAX_FILE_MB_PPT: int = 100

  # ==================== Agent 用户自建技能（复刻 DeerFlow skill-creator） ====================
  AGENT_SKILL_LLM_SCAN: bool = True  # skill_manage 写盘前 LLM 语义安全扫描开关（fail-closed）

  # ==================== 可观测性追踪（Langfuse，自建部署，数据不出内网） ====================
  LANGFUSE_ENABLED: bool = False       # 默认关闭；未装 langfuse 包时也不影响 Agent 主链路
  LANGFUSE_PUBLIC_KEY: str = ''        # 自建 Langfuse 实例创建项目后获取
  LANGFUSE_SECRET_KEY: str = ''        # 自建 Langfuse 实例创建项目后获取
  LANGFUSE_HOST: str = 'http://langfuse:3000'  # 自建实例内网地址（docker 服务名），不出内网

  # ==================== DCIM 集成 ====================
  DCIM_ENABLED: bool = False           # DCIM 门面工具开关，默认关闭
  DCIM_BASE_URL: str = ''              # DCIM API 基地址（真实接口就绪后配置）
  DCIM_API_TOKEN: str = ''             # DCIM 服务级访问 token
  DCIM_TIMEOUT: int = 30               # DCIM 调用超时（秒）
  DCIM_RETRIEVAL_TOP_K: int = 8        # discover 检索返回的能力条数上限

  # ==================== DCIM Kafka 接入（阶段一：空间/设备/点位配置数据） ====================
  # 见 docs/dcim-kafka-ingest-design.md；broker 与三个 topic 由 DCIM 提供，我方只消费。
  # 对方是**每个园区一个独立集群**（13 个），清单在 DCIM_CLUSTERS_FILE 指向的 JSON 里；
  # KAFKA_BOOTSTRAP_SERVERS 仅供本地单集群测试，生产不再使用。
  DCIM_KAFKA_ENABLED: bool = False      # worker 总开关，默认关闭
  DCIM_CLUSTERS_FILE: str = 'deploy/config/dcim-clusters.json'  # 集群清单（相对仓库根）
  KAFKA_BOOTSTRAP_SERVERS: str = 'localhost:9092'   # 仅供本地测试（生产走集群清单）
  KAFKA_GROUP_ID: str = 'rz-dcim-ingest'
  # topic 名以对方 9/29 版《DCIM Kafka 接口文档》为准：**带 `-rz` 后缀**
  # （廊坊B1M2 上同时存在带/不带 `-rz` 两套，不带的那套不是给我们的）
  KAFKA_TOPIC_SPACE: str = 'ems-config-space-rz'
  KAFKA_TOPIC_DEVICE: str = 'ems-config-device-rz'
  KAFKA_TOPIC_POINT: str = 'ems-config-point-rz'
  KAFKA_SECURITY_PROTOCOL: str = 'PLAINTEXT'   # 按对方 broker 实际配置（SASL_PLAINTEXT / SASL_SSL）
  KAFKA_SASL_MECHANISM: str = 'PLAIN'
  KAFKA_SASL_USERNAME: str = ''                # 由 DCIM 下发的账号
  KAFKA_SASL_PASSWORD: str = ''
  KAFKA_BATCH_SIZE: int = 500                  # 每批写库条数
  DCIM_PG_DSN: str = ''                        # dcim 元数据库，如 postgresql://user:pwd@host:5432/dcim

  # ==================== DCIM 实时数据（阶段二：点位数值时序 → TDengine） ====================
  # 与配置类的分工：定义/字典/阈值在 PG（上面那套），**点位数值的时间序列**在这里。
  DCIM_REALTIME_ENABLED: bool = False          # 独立开关；关掉只影响实时数据，配置类照常
  KAFKA_TOPIC_REAL_DATA: str = 'ems-real-data-rz'

  # ==================== DCIM 告警事件（触发 / 刷新 / 恢复 → PG） ====================
  # ⚠️ **不进 TDengine** —— 告警是**事件**不是时序；且 `point_guid`/`device_guid` 就是 PG 里的
  #    guid，**同库可直接 JOIN**，不需要跨库两段式。
  # 落库：`dcim_alarms`（当前态，主键 `(cluster, event_log_id)`，三态 upsert 成一条）
  #      + `dcim_alarms_history`（每次状态流转追加一条）。
  DCIM_ALARM_ENABLED: bool = False             # 独立开关；对方 topic 建好后再打开
  KAFKA_TOPIC_ALARM: str = 'ems-alarm-data-rz'
  # 同实时数据的理由：**独立消费组** —— 和配置类同组的话，配置线程重启触发的 rebalance
  # 会把告警消费一起带停（同组内不同 topic 的成员是一起 rebalance 的）
  KAFKA_ALARM_GROUP_ID: str = 'rz-dcim-alarm'

  # ==================== DCIM 告警工单（人工处理告警 → PG） ====================
  # 文档 §10：告警被人工响应/处理时推送（status 1-处理中 / 2-已处理），
  # 通过 `eventLogId` 关联 `dcim_alarms.guid`。
  # 落库：`dcim_alarm_orders`（当前态，主键 `(cluster, orderNo)`，同一工单 upsert 成一条）
  #      + `dcim_alarm_orders_history`（每次推送追加一条）。
  # ⚠️ 文档标题写 `ems-alarm-order-rz`、表格写 `ems-alarm-order`（与告警事件同样的不一致）；
  #    实测 2026-10-20：廊坊A 的 `-rz` topic 为空、base `ems-alarm-order` 持续有数据 ——
  #    先按文档 `-rz` 接入，对方把订单流接到 `-rz` 后即可收到；要立刻用 base 数据测试
  #    只需把 `KAFKA_TOPIC_ALARM_ORDER` 改成 `ems-alarm-order`（不改代码）。
  DCIM_ALARM_ORDER_ENABLED: bool = False
  KAFKA_TOPIC_ALARM_ORDER: str = 'ems-alarm-order-rz'
  KAFKA_ALARM_ORDER_GROUP_ID: str = 'rz-dcim-alarm-order'

  # ==================== 数据中心事件统计表（以飞书园区群通报为入口） ====================
  # 一行 = 一次已通报事件（速报 + 首报），关联告警与工单补全原因/进展/时长。
  # 通报原文落 PG `dcim_incident_reports`，往期窗口可直接重算，不固化 22 列快照。
  # 总开关：开启才建预设任务（**只有一条 `incident__all__shift`**，全部已配置园区合并成
  # 一张班次表；旧的 per-park 预设自动停用）+ 后台同步。周报不再生成。
  INCIDENT_STATS_ENABLED: bool = False
  # 园区 → 群名（或 oc_ 开头的 chat_id），JSON 对象：
  #   {"廊坊": "廊坊园区动环通报群", "平湖": "oc_xxx", ...}
  # 所有登记的园区合并成一张表；新增园区只加这里 + FEISHU_GROUP_INGEST_CHATS，不新增预设。
  INCIDENT_FEISHU_PARK_CHATS: str = ''
  INCIDENT_SYNC_INTERVAL_SECONDS: int = 300     # 后台同步间隔
  INCIDENT_SOS_LIMIT_SECONDS: int = 120         # 速报 ≤ 发生 + 2min
  INCIDENT_FIRST_LIMIT_SECONDS: int = 300       # 首报 ≤ 发生 + 5min
  # 已停用：匹配改为「发生时间精确同秒」查候选，不再捞时间窗（2026-11-04）
  INCIDENT_MATCH_WINDOW_MINUTES: int = 10
  # 同秒并列候选：先看兄弟证据（同群同秒另一条通报已配上的 guid），仍分不出的
  # 交给模型仲裁（一次独立模型调用）：输入通报原文 + 候选告警，输出选中的序号；
  # 判不出留空（沿「不配优于配错」）。不设每轮次数/候选条数上限。
  INCIDENT_MATCH_LLM_ENABLED: bool = True
  # 报文字段抽取兜底：确定性规则取不到发生时间/告警值时，让模型从原文抽。
  # 结果仍要过 `feishu.value_to_epoch` 校验，抽不到就留空（绝不写推测时刻）。
  INCIDENT_EXTRACT_LLM_ENABLED: bool = True
  INCIDENT_EXTRACT_LLM_MAX_PER_ROUND: int = 20  # 每轮最多调几次（串行，防烧钱）
  INCIDENT_EXTRACT_LLM_TIMEOUT: int = 15
  # 卡片通报「进展同步」原地追加：飞书没有消息更新事件、增量游标也回不到老消息，
  # 只能定点 `im.v1.message.get` 回捞窗口内的 interactive 消息，正文变了才回写。
  INCIDENT_CARD_REFRESH_MINUTES: int = 60       # 回捞间隔（每小时一轮）
  INCIDENT_CARD_REFRESH_HOURS: int = 72         # 回看窗口（拉满飞书 message.get 保留期）
  INCIDENT_CARD_REFRESH_BUILD_HOURS: int = 24   # 出表前补捞窗口（覆盖两个班次足够）

  # ==================== 运营监控中心告警不合规检查（班次 xlsx） ====================
  # 早 09:00 查前一晚班（昨日 17:30 ~ 今日 08:30）、晚 18:00 查当日白班（08:30 ~ 17:30），
  # 每班次产出一份 xlsx（一个检查项一个 sheet）。**当前默认关闭**：页面先上线，
  # 只有把这里改成 True 才会建预设任务、跑定时产出。
  COMPLIANCE_ENABLED: bool = False
  COMPLIANCE_ALARM_TIMEOUT_MINUTES: int = 5     # 响应超时：发生超 N 分钟仍无人响应
  COMPLIANCE_REPEAT_MIN_COUNT: int = 3          # 重复告警：同测点同内容 ≥N 次

  # ==================== 飞书群消息实时入库（WS 推送） ====================
  # 与上面的 `INCIDENT_STATS_ENABLED` **独立**：那个开关还会建事件统计表的定时预设，
  # 而实时入库只是「群里发的通报立即落库」，不该被它牵连。
  # 依赖 `LARK_ENABLED=true`（WS 长连接是唯一入口）；需要飞书 `im:message.group_msg`
  # 敏感权限，才能真正收到群里**所有**消息（不只是 @ 机器人的）。
  FEISHU_GROUP_INGEST_ENABLED: bool = False
  # chat_id 白名单（逗号分隔，`oc_xxx`）。**为空 = 不启用**（fail-closed）：
  # 群消息读权限是敏感权限，不提供「默认全收」。
  # 与 `INCIDENT_FEISHU_PARK_CHATS` 的 chat_id 必须一致（缺一边启动时会 warning：
  # 缺这里收不到实时推送、只能靠回补；缺那边能入库但配不上告警、也进不了表）。
  FEISHU_GROUP_INGEST_CHATS: str = ''
  # 补偿回补（分钟）：每 N 分钟跑一次「按水位补缺口」，救断线/进程重启丢掉的推送。
  # 起点 = min(Redis 水位, 库内最新消息时间) − 60s，**不是**固定滑窗——实时写入会把
  # 库内最新时间推到「此刻」，按固定滑窗补不到中间的空洞（详见 `sync.sync_gaps`）。
  FEISHU_GROUP_INGEST_BACKFILL_MINUTES: int = 30
  # 单次回补最多往回追多久（小时）：停机一整天后重启，按水位会补 24h；超过这个上限
  # 的部分需要手工 `cli --sync` 回填。拉取本身还有 `feishu._MAX_MESSAGES=5000` 兜底。
  FEISHU_GROUP_INGEST_MAX_BACKFILL_HOURS: int = 72

  # 实时数据用**独立消费组**：和配置类同组的话，配置线程重启触发的 rebalance
  # 会把实时消费一起带停（同组内不同 topic 的成员是一起 rebalance 的）
  KAFKA_REAL_DATA_GROUP_ID: str = 'rz-dcim-realdata'
  # 每园区实时消费线程数：**同一消费组多起消费者**，Kafka 自动按分区分摊
  # （topic 16 分区 → 4 条线程各分 4 个）。单线程实测只有 600~900 行/s，
  # 而 5,706 msg/s × ≈2.6 样本/msg 需要 ~14,800 行/s，必须并行才追得平。
  # 只影响线程数与带宽，不改任何写入语义；<1 按 1 处理。
  # 单个园区可在 `deploy/config/dcim-clusters.json` 用 `concurrency` 覆盖本值
  # （廊坊A 配 16：16 个分区一区一条；其余园区流量小，沿用本值）。
  DCIM_REALTIME_CONCURRENCY: int = 4
  # 接入进程数（多进程分片，**默认 1 = 旧行为**）。
  # 实时写入是**单进程 GIL 饱和**：实测单进程约 6.5k 行/s（≈1 核），再加线程/下发并发
  # 都不涨（`top -H` 几个 Python 线程合计 ≈100%）。>1 时 fork 出 N 个子进程，工作线程按
  # 「扁平序号 % N」均分，**每个进程一个 GIL**。线程总数与 PG 连接数不变，只多占内存。
  # ⚠️ 各园区实时线程数（全局值或集群清单覆盖值）都要 ≥ 本值，否则会有进程分不到
  # 实时线程（启动日志会告警）。
  DCIM_INGEST_PROCESSES: int = 1
  # 单个消费线程内**并行发往 TDengine 的 INSERT 条数**（默认 4；1 = 旧串行行为）。
  # 实测串行下发时客户端只有 ~1 条在途、服务端平均并发仅 2.38，而 taosd 只用 3.9/16 核、
  # 55% CPU 空闲 —— 并发下发是当前吞吐的主要杠杆（不改任何写入语义）。
  DCIM_WRITE_CONCURRENCY: int = 4
  # 写入宽表时的「零阶保持」最长间隔（秒）——见 `dcim_ingest/wide_writer.py::merge_values`。
  # 对方的即时推送只带刚采到的测点（实测 15% 的行缺 ht_temp），所以写入时把上一个值带过来补齐；
  # 但**超过这个间隔就不补、如实留空**：设备离线一小时被补成一条直线会**掩盖故障**。
  # 正常推送间隔 10 秒 → 60 是 6 倍余量（正常稀疏都补上、真断档留空）。
  DCIM_CARRY_MAX_SECONDS: int = 60
  # 实时数据时间戳的最大回溯天数（写入校验用）。**必须 ≤ 库的 KEEP**：TDengine 对早于保留期
  # 的时间戳**整条 INSERT 拒写**（`Timestamp data out of range`），而拒的是整条合并语句 ——
  # 一个点的坏值就能卡死一条消费线程（2026-11-08 生产实测：对方推了 `timestamp=0`，
  # 该分区位点再也无法前进，积压 11.8 亿条）。KEEP 现为 129600m = 90 天。
  DCIM_TS_MAX_AGE_DAYS: int = 90
  TAOS_HOST: str = 'tdengine'                  # compose 里的服务名
  TAOS_PORT: int = 6030                        # native 协议（写入走这个；REST 是 6041）
  TAOS_USER: str = 'root'
  TAOS_PASSWORD: str = ''
  TAOS_DB: str = 'dcim'                        # 建在 TDengine 里的库名
  # ⚠️ 下面是**建库参数**，只在第一次建库时生效（设计见 docs/dcim-timeseries-storage-design.md §5/§6）。
  # **可改性各不相同**（2026-10-07 按官方 DDL 参考更正；此处此前笼统写成"三个都不可改"，不准确）：
  #   - `VGROUPS` ❌ **建后不可改**（至少不可减少）→ 建库前**必须**按「实分给 VM 的 vCPU 数」定好
  #     （⚠️ **不是宿主总核数** —— 共享机器上照宿主核数推会翻倍，2026-10-09 踩过）
  #   - `DURATION` / `KEEP` ✅ 可 `ALTER DATABASE ...` 改（KEEP 须 ≥ 3×DURATION）
  #   - `CACHEMODEL` / `COMP` / `BUFFER` / `WAL_LEVEL` / `PRECISION` ✅ 可改
  #
  # 定值依据（生产库实测 108 测点 / 815 万行 / 7.4 天）：
  #   **10,140 行/测点/天**（≈10 秒周期）· 压缩后 **2.25 字节/行**（原始 24 字节/行 → 压缩比 ≈10:1）
  #   → **22.8 KB/测点/天**。据此 90 天：10 万测点≈205GB、100 万测点≈2.0TB（8T 盘放得下）。
  # 分片数。**官方口径「每核服务 1~2 个 vnode」**（`vnode = vgroups × 副本数`；单副本即 = 本值）。
  # ⚠️ 这个「核数」指**实分给这台 VM 的 vCPU 数**，**不是宿主总核数**（共享机器上最容易算错的一步）：
  #    宿主是 24 核 Xeon Gold 5418Y，但**与其他 VM 共享**，数据库 VM 只分 16 vCPU
  #    → 16 × 2（官方上限 1 核 : 2 vnode）= **32**。单 vgroup 约 10~50 万子表。
  TAOS_VGROUPS: int = 32
  TAOS_DURATION: str = '1d'                    # 时间分区粒度（数据文件按此切片）
  TAOS_KEEP_DAYS: int = 90                     # 原始数据保留天数（超期自动删最老切片）
  # ⚠️ 默认 **none**（2026-11-04 实测改）：`last_row` 会在每张子表上驻留整行，
  #    4,000 列宽表下这是写侧 CPU 的主因 —— 关掉后 124.3 → 22.0 µs/行（−82%），
  #    生产写入 7,718 → 15k~26k 行/s、读侧无退化。调 COMP 无用，别再当压缩问题处理。
  #    要恢复缓存：`ALTER DATABASE dcim CACHEMODEL 'last_row'`（在线、可逆）。
  #    见 docs/problems/tdengine-write-cost-probe.md
  TAOS_CACHEMODEL: str = 'none'
  # ⚠️ 下面四项本可以靠 TDengine 默认值，**显式写死是为了换版本不漂**——此前它们只出现在
  # 设计文档的示例 DDL 里，从没落到代码，实测"恰好等于默认值"而已。
  TAOS_COMP: int = 2                           # 压缩等级 0/1/2（二级压缩，实测压缩比 ≈10:1）
  # ⚠️ `BUFFER` 是**按 vnode 算**的：32 vgroups × 256MB = **8GB**（VM 64GB 的 12.5%）。
  #    定值：先算上限 `BUFFER(MB) × VGROUPS ≲ VM 内存的 25%`（防吃光内存），**再按实际负载往下取**。
  #    本例上限是 512（16GB），但**我们稳态写入仅 ~0.26 MB/s（压缩后）** —— 256（TDengine 默认）
  #    已远超需求，剩下 56GB 全留给 page cache（TDengine 的读性能靠它）。
  #    ⚠️ **VGROUPS 建后不可改、BUFFER 可 `ALTER DATABASE`** —— 两者冲突时先动 BUFFER。
  TAOS_BUFFER: int = 256                       # 每个 vnode 的写入内存池（MB）
  TAOS_WAL_LEVEL: int = 1                      # 1=写 WAL 但不每次 fsync（实时数据可丢，换吞吐）
  TAOS_PRECISION: str = 'ms'                   # 时间戳精度；报文给的是毫秒

  # ── 归档层（Parquet → MinIO；**全量归档、兼作热层备份**）──
  # 为什么全量（不等数据过期才归档）：归档那份要兼作备份 —— 等过期再归档的话，
  # 热层一过期、中间那段就没有第二份了。所以是**持续/近实时**写。
  DCIM_ARCHIVE_ENABLED: bool = False           # 独立开关；关掉只影响归档
  ARCHIVE_MINIO_ENDPOINT: str = 'minio:9000'   # compose 里的服务名（宿主机上是 127.0.0.1:9000）
  ARCHIVE_MINIO_ACCESS_KEY: str = ''
  ARCHIVE_MINIO_SECRET_KEY: str = ''
  ARCHIVE_MINIO_BUCKET: str = 'dcim-archive'
  ARCHIVE_MINIO_SECURE: bool = False           # 内网自建 MinIO 默认 http
  ARCHIVE_FLUSH_ROWS: int = 50000              # 攒够多少行落一个文件
  ARCHIVE_FLUSH_SECONDS: float = 60.0          # 或攒够多少秒（先到先落，避免低流量时文件太碎）
  # 缓冲上限：MinIO 长时间不可用时，缓冲不能无限涨到把进程撑爆。
  # 超限就**丢最旧的桶并计数**（明确日志：这些行可由 TDengine 侧重放补——KEEP 期还在），
  # 而不是让"归档兼作备份"变成"归档把内存撑爆"。
  ARCHIVE_MAX_BUFFER_ROWS: int = 500000
  # 批量写 TDengine 无需单独配置：每轮从 Kafka 拉 KAFKA_BATCH_SIZE 条报文，
  # 每条报文展开成 N 个点值，一轮 flush 一次 —— 批量大小天然由它决定。

  # ==================== 综合管理平台集成 ====================
  MGMT_ENABLED: bool = False           # 综合管理平台开关，默认关闭
  MGMT_BASE_URL: str = ''              # 综合管理平台 API 基地址
  MGMT_INSPECTION_BASE_URL: str = ''   # 巡检服务 API 基地址（根路径 /inspection，无 /center 前缀）
  MGMT_API_TOKEN: str = ''             # 服务级访问 token（若后端提供，跳过登录）
  MGMT_USERNAME: str = ''              # 登录账号（服务账号，最高权限）
  MGMT_PASSWORD: str = ''              # 登录密码
  MGMT_TOKEN_TTL: int = 3600           # token + deptId 缓存秒数
  MGMT_REF_CACHE_TTL: int = 86400      # 静态参考数据（公司/部门/pen 公司部门）缓存秒数，默认 24h
  MGMT_TIMEOUT: int = 30               # 调用超时（秒）
  MGMT_PEN_BASE_URL: str = ''          # pen 流程服务 API 基地址（如 https://api.ddbes.com，mgmt 能力延伸，非新平台）
  # 工单服务基地址（2026-10-26 对方把工单接口整体迁到这里：/wiporder/*、/pending/list/v1、
  # /company/user/{uid}；认证与 mgmt 同一套 token+Deptid，不再用 pen 的 Bearer 人员 token）。
  MGMT_TICKET_BASE_URL: str = 'https://ticket-api.ddbes.com'
  MGMT_PEN_TOKEN_TTL: int = 3600       # pen 人员 token 缓存秒数（失效由 401 重登兜底）
  MGMT_PASSWORD_KEY: str = ''          # 认证中心密码加密 Fernet 密钥（空则从 JWT_PRIVATE_KEY 派生）
  MGMT_DEFAULT_PASSWORD: str = '666666'  # 未绑定用户的默认 mgmt 账号密码（默认平台账号）

  # 综合管理平台账号打通（SSO 登录 + 注册时档案同步）
  MGMT_SSO_ENABLED: bool = False          # mgmt 账号登录 rz 总开关，默认关闭
  # mgmt 账号**首次登录时自动创建**的 rz 用户拿到的默认角色。
  # **只影响将来新创建的账号**：已有账号按手机号匹配、沿用原角色，不会被这里改动。
  # power_user（全功能用户）= ai:chat/agent/knowledge/risk/fire_safety/video；
  # 仍**不含** scheduled_task:manage（定时任务）与 system:admin（系统管理）——那两个只有 admin 有。
  MGMT_SSO_DEFAULT_ROLE: str = 'power_user'

  # ==================== 文档更新检查（月度 sha256 快照） ====================
  # 默认按「部门共享文档」逐部门检查 25 条月度项（部门 × 项矩阵），每月 10 日跑一期。
  # **只需 MGMT_* 已配好**（复用 mgmt 登录与文件接口），本组开关默认关闭。
  DOC_UPDATE_CHECK_ENABLED: bool = False   # 开启后：建每月预设 + 页面/工具可用
  # 启动时自动跑首期基线：默认关。开启后也只在「从没有过部门口径快照」时跑一次，
  # 不按月份/重启重跑（否则每次 reload 都静默跑一遍全量，页面一直显示「检查中」）。
  DOC_UPDATE_AUTO_BASELINE: bool = False
  DOC_UPDATE_SCOPE: str = 'dept'           # dept = 逐部门共享文档；tree = 旧单树（回滚用）
  DOC_UPDATE_TREE_ID: str = '24CiSBO7cxt'  # 旧单树模式的「数据中心运维管理体系」整树 id
  DOC_UPDATE_CRON: str = '0 3 10 * *'      # 每月 10 日 03:00
  DOC_UPDATE_TIMEOUT: int = 30             # 单文件下载超时（秒）
  DOC_UPDATE_MAX_FILES: int = 1000         # 安全阀（仅「原地更新型」才下载，按期提交型不下载）
  DOC_UPDATE_COMPANY_ID: str = '27WqDiazDVr'   # 旧格式/tree 模式的默认公司（润泽）
  # 固定「公司 × 部门」清单。格式 `标签:companyId:部门,…`，组间 `;`（两段时标签用 id）；
  # **不带 `:` 的旧值**仍按单公司处理（套 DOC_UPDATE_COMPANY_ID）。
  # 页面列/检查范围都以它为准；新增/撤掉园区或部门改这里（`--refresh-depts` 重解析 id/根目录）。
  DOC_UPDATE_DEPTS: str = (
    '廊坊:27WqDiazDVr:A1,A2,A3,A5,A6,A7,A8,A9,A10,A11,A12,A18;'
    '平湖:27gIIjyTQ7n:A1,A2;惠州:27gIKJhwwZX:A2,A3;'
    '佛山:27gIL8vGj8F:A1,A2,A3;重庆:27oHS2fq6yp:A1,A2'
  )
  DOC_UPDATE_DEPT_PATTERN: str = r'^A\d+$'     # 从接口行里认部门名（不作列清单）
  DOC_UPDATE_PERIOD_GRACE_MONTHS: int = 1      # 本期应到 = 当前月回退 N 月（兼容按报告期/交付月命名）

  # ==================== 红外巡检图片质量检测 ====================
  # 用户按「部门 → 巡检任务」勾选检测，判定走外部 Gradio 服务（不调 VLM）。
  # sharp=合格 / borderline=待复核 / defocused=不合格 / 其余（uncertain、推理失败）=检测失败，
  # fail-closed。只认新英文标签（2026-11-10 接口改版），旧中文输出按未识别处理。
  INFRARED_QUALITY_ENABLED: bool = False
  INFRARED_QUALITY_API_URL: str = 'http://10.100.17.152:18080/'
  INFRARED_QUALITY_API_NAME: str = '/predict_images'
  # 权重路径是 **Gradio 服务端** 的文件路径（不是本机），与 /gradio_api/info 的下拉枚举一致
  INFRARED_QUALITY_MODEL: str = (
    '/home/rangeidc/thermal-quality/data/特殊数据集/runs/best.pt'
  )
  INFRARED_QUALITY_DEVICE: str = 'auto'
  INFRARED_QUALITY_TIMEOUT: int = 300           # 单批推理（upload+SSE）总超时
  INFRARED_QUALITY_DOWNLOAD_TIMEOUT: int = 30
  INFRARED_QUALITY_DOWNLOAD_CONCURRENCY: int = 4
  INFRARED_QUALITY_BATCH: int = 20              # 每批上传/推理张数（分批只是传输方式，不丢数据）
  INFRARED_QUALITY_PASS_LABELS: str = 'sharp'
  INFRARED_QUALITY_REVIEW_LABELS: str = 'borderline'
  INFRARED_QUALITY_DEPTS: str = ''              # 空 = 复用 DOC_UPDATE_DEPTS
  INFRARED_QUALITY_TASK_CACHE_TTL: int = 600    # 任务列表 Redis 缓存（秒）
  # 检测时把图片留一份到本地：mgmt 给的是 S3 预签名地址（7 天有效），过期后原图就取不到了
  # （判定结果仍在 Mongo，丢的是图片证据）。按任务分目录，明细读库时改走平台图片端点。
  INFRARED_QUALITY_IMAGE_DIR: str = str(
    Path(__file__).parent.parent / 'data' / 'infrared_quality'
  )

  # ==================== 安防平台集成 ====================
  # 占位注册：暂无接口文档，capabilities 为空元组，client.call 直接抛异常。
  # 接口文档到位后按 mgmt 的接入模式（EndpointSpec 声明式映射）填实即可。
  SECURITY_ENABLED: bool = False       # 安防平台开关，默认关闭
  SECURITY_BASE_URL: str = ''          # 安防平台 API 基地址
  SECURITY_API_TOKEN: str = ''         # 服务级访问 token
  SECURITY_TIMEOUT: int = 30           # 调用超时（秒）

  # ==================== 联网能力（web 平台：检索 + 抓取） ====================
  # 只在 task_mode='research'（联网调研模式）或前端「联网搜索」开关下可用：web 平台列在
  # service/query/base/platform.py 的 MODE_GATED_PLATFORMS 里，其他模式连存在都看不到。
  # 另注意登录响应里的 web_search_enabled（api/v1/auth.py）——前端据此决定是否渲染开关。
  # 搜索走自建 SearXNG（出站 chokepoint 唯一、可审计），**默认关闭**。
  WEB_ENABLED: bool = False                    # 总开关，默认关闭
  WEB_SEARCH_BASE_URL: str = 'http://localhost:8088'  # 自建 SearXNG 地址
  WEB_TIMEOUT: int = 20                        # 检索/抓取超时（秒）
  WEB_MAX_RESULTS: int = 5                     # 单次检索返回条数默认值（硬上限 10）
  WEB_FETCH_MAX_CHARS: int = 8000              # 单次抓取正文进上下文上限（字符）
  WEB_FETCH_MAX_REDIRECTS: int = 3             # 重定向跟跳上限（每跳都做 SSRF 复校验）
  WEB_AUDIT_COLLECTION: str = 'web_queries'    # 出站审计集合名（query/url 原文落库）

  # ==================== 页面截图（browser 平台：Playwright 旁路容器） ====================
  # 生产是 uvicorn --workers 4，进程内有状态浏览器会话必然裂开，故浏览器跑在独立旁路容器，
  # 后端只发「声明式配方」（固定页面路径 + 参数占位 + 就绪条件 + 抽取规则），一次调用完成
  # 导航 → 设筛选 → 等就绪 → 截图 → 抽表格。BROWSER_ENABLED 默认关闭。
  BROWSER_ENABLED: bool = False                 # 总开关，默认关闭
  BROWSER_SERVICE_URL: str = 'http://browser:8100'  # 旁路服务地址（内网）
  BROWSER_SERVICE_TOKEN: str = ''               # X-Browser-Token 校验（旁路服务侧比对）
  BROWSER_TIMEOUT: int = 60                     # 单次捕获总超时（秒）
  BROWSER_MAX_CONCURRENCY: int = 2              # 旁路服务同时执行的捕获数上限（Chromium 内存约 1GB/实例）
  BROWSER_ALLOWED_HOSTS: str = ''               # 允许导航的主机白名单（逗号分隔，空=不限——仅限可信内网）
  BROWSER_MAX_IMAGE_MB: float = 8.0             # 单张截图大小上限（MB），超限判失败
  BROWSER_STATE_DIR: str = '/state'             # 旁路容器内 storageState 落点（docker 命名卷）
  # 交互浏览（默认关）：7 个逐步操作工具
  # （navigate/snapshot/click/get_text/screenshot/record/close）。
  # 会话常驻在旁路容器（单进程，后端 4 worker 共享同一份会话表），一次工具调用只做一步。
  BROWSER_EXPLORE_ENABLED: bool = False
  BROWSER_MAX_SESSIONS: int = 4                 # 旁路容器同时保活的会话数（每个约 1GB 内存）
  BROWSER_SESSION_IDLE_SECONDS: int = 900       # 空闲会话回收秒数
  # 录屏（browser__record）：在当前页面录固定时长，交付关键帧 + 可选 webm。
  # 旁路服务读同名环境变量做钳制与合成，后端读值做上限校验。
  BROWSER_RECORD_SECONDS: int = 30              # 默认录制时长（秒）
  BROWSER_MAX_RECORD_SECONDS: int = 120         # 单次录制上限（秒）
  BROWSER_RECORD_KEYFRAME_SECONDS: int = 5      # 关键帧默认间隔（秒）
  BROWSER_MAX_KEYFRAMES: int = 12               # 关键帧数量上限（过密自动放大间隔）
  BROWSER_MAX_VIDEO_MB: float = 30.0            # webm 大小上限（MB，超限只交付关键帧）
  BROWSER_RECORD_FPS: int = 10                  # 视频回放帧率（旁路服务合成用，≤15）
  # 页面根地址：配方里的路径拼在它后面。留空时分别回退 DCIM_BASE_URL / MGMT_BASE_URL
  # （注意 MGMT_BASE_URL 是 API 前缀 …/center，页面根可能不同，建议显式配置）。
  BROWSER_DCIM_URL: str = ''
  BROWSER_MGMT_URL: str = ''
  # 服务账号（配方登录步骤里以 {username}/{password} 插入；登录态优先用 storageState，
  # 只有 storageState 缺失/失效且配方声明了 login 时才会用到账号密码）。
  BROWSER_DCIM_USERNAME: str = ''
  BROWSER_DCIM_PASSWORD: str = ''
  BROWSER_MGMT_USERNAME: str = ''
  BROWSER_MGMT_PASSWORD: str = ''

  # ==================== 飞书 IM 机器人 ====================
  LARK_ENABLED: bool = False           # 总开关，默认关闭
  LARK_APP_ID: str = ''                # 飞书自建应用 App ID
  LARK_APP_SECRET: str = ''            # 飞书自建应用 App Secret
  LARK_VERIFICATION_TOKEN: str = ''    # 事件订阅 Verification Token（未开加密留空）
  LARK_ENCRYPT_KEY: str = ''           # 事件订阅 Encrypt Key（未开加密留空）
  LARK_BOT_NAME: str = ''              # 群聊 @提及去重用（@机器人 后文本里的机器人名）
  LARK_CONNECT_CODE_TTL: int = 600     # 一次性绑定码有效期（秒）
  LARK_HISTORY_LIMIT: int = 50         # 读消息工具的单次拉取条数（模型可传 limit 覆盖）
  LARK_SUMMARY_MAX_MESSAGES: int = 1000  # 群总结单次分析的消息上限（**安全阀**，正常不该触发；
                                          # 总结按**时间窗口**取全量，不按条数硬截断，触顶时会在回复里告知）
  LARK_ARTIFACT_TOKEN_TTL: int = 3600  # 产物下载链接有效期（秒）
  LARK_LEADER_TTL: int = 30            # 多 worker 选主锁 TTL（秒）

  # ==================== 个人微信 iLink 机器人（ClawBot） ====================
  WECHAT_ENABLED: bool = False         # 总开关，默认关闭
  WECHAT_BOT_TOKEN: str = ''           # 扫码拿到的 bot_token（会过期，errcode -14 需重扫）；
                                        # 命令行脚本扫码接入后落 WECHAT_STATE_FILE，此处可留空
  WECHAT_ILINK_BOT_ID: str = ''        # 扫码拿到的 ilink_bot_id（同上，state 文件优先）
  WECHAT_BASE_URL: str = 'https://ilinkai.weixin.qq.com'  # iLink 服务地址
  WECHAT_CHANNEL_VERSION: str = '1.0'  # base_info.channel_version（DeerFlow 实测默认 1.0）
  WECHAT_ALLOWED_USERS: str = ''       # 可选白名单（逗号分隔的裸 user_id），空=不限制
  WECHAT_POLL_TIMEOUT: int = 35        # getupdates 长轮询 hold 秒数（服务端上限）
  WECHAT_RETRY_DELAY: int = 5          # 长轮询/发送失败后的重试间隔（秒）
  WECHAT_CONNECT_CODE_TTL: int = 600   # 一次性绑定码有效期（秒）
  WECHAT_ARTIFACT_TOKEN_TTL: int = 3600  # 产物下载链接有效期（秒）
  WECHAT_LEADER_TTL: int = 30          # 多 worker 选主锁 TTL（秒）
  WECHAT_STATE_FILE: str = 'data/wechat/auth.json'  # 扫码接入后的凭据落盘位置（0600）
  WECHAT_QR_BOT_TYPE: int = 3          # 取二维码的 bot_type（DeerFlow 实测 3）
  WECHAT_QR_TIMEOUT: int = 180         # 单个扫码 session 的有效期（秒）
  WECHAT_QR_POLL_INTERVAL: int = 2     # 前端轮询二维码状态的建议间隔（秒）

  model_config = {
    'env_file': '.env',
    'env_file_encoding': 'utf-8',
    'case_sensitive': True,
    'extra': 'ignore',
  }


settings = Settings()
