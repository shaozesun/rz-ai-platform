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

  # Agent 模型专属超时：报表流程里 LLM 携带大上下文推理，chunk 间隔远大于
  # 普通对话的 LLM_TIMEOUT=60。单独配置避免 60s 就 ReadTimeout → 「回复生成中断」。
  AGENT_LLM_TIMEOUT: int = 300
  # 超时多是模型慢，重试整个流只会让用户等更久，1 次即止。
  AGENT_LLM_MAX_RETRIES: int = 1

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
  TTS_PROVIDER: str = 'edge'  # edge / openai / qwen
  TTS_API_URL: str = 'http://localhost:8000'

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

  # 触发条件（OR 逻辑，命中任一即触发压缩）
  # 格式: "type:value"，如 "tokens:32000" / "messages:50" / "fraction:0.8"
  # 多个条件逗号分隔，示例: "tokens:32000,fraction:0.8"
  AGENT_SUMMARIZE_TRIGGER: str = 'tokens:32000'

  # 保留近期上下文策略
  # 格式: "type:value"，如 "messages:10" / "tokens:3000" / "fraction:0.3"
  AGENT_SUMMARIZE_KEEP: str = 'messages:10'

  # 送给摘要模型的 token 上限（参照 DeerFlow trim_tokens_to_summarize）
  AGENT_SUMMARIZE_TRIM_TOKENS: int = 4000

  # 自定义摘要 prompt（空 = 使用默认 prompt）
  AGENT_SUMMARIZE_PROMPT: str = ''

  # 工具输出进上下文前封顶：page_size 上限 + 单条工具结果最大字符数
  AGENT_MAX_PAGE_SIZE: int = 20
  AGENT_MAX_TOOL_OUTPUT_CHARS: int = 12000  # ≈6k token；紧凑序列化后正常/较宽 20 条页面全量放得下

  # ==================== Agent 数据分析沙箱（参照 DifySandbox / DeerFlow 模式） ====================
  AGENT_ANALYSIS_ENABLED: bool = False        # 总开关，默认关闭
  AGENT_ANALYSIS_DATA_DIR: str = str(Path(__file__).parent.parent / 'data' / 'analysis')
  AGENT_ANALYSIS_TIMEOUT: int = 30            # 单次代码执行超时（秒）
  AGENT_ANALYSIS_MAX_ROWS: int = 10000        # 单数据集最大行数（全量拉取上限）
  AGENT_ANALYSIS_PAGE_SIZE: int = 100         # 拉取平台数据的分页大小
  AGENT_ANALYSIS_MAX_MEMORY_MB: int = 512     # 沙箱子进程内存上限
  AGENT_ANALYSIS_MAX_OUTPUT_CHARS: int = 4000 # 单次执行返回结果上限
  AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION: int = 20
  AGENT_ANALYSIS_MAX_ARTIFACT_MB: int = 20     # 单产物大小上限（MB），超限剔除
  AGENT_ANALYSIS_MAX_ARTIFACTS: int = 10       # 单次执行产物数量上限，超限保留最近的
  # 沙箱内报告中文字体（捆绑的文泉驿微米黑 .ttc，跨平台；经 RZ_CJK_FONT env 传给沙箱脚本）
  RZ_CJK_FONT: str = str(Path(__file__).parent.parent / 'assets' / 'fonts' / 'wqy-microhei.ttc')
  # WeasyPrint 依赖的 Pango/Cairo 动态库搜索路径（非系统默认位置时配置，如 conda env 的 lib 目录；
  # 注入 DYLD_LIBRARY_PATH/LD_LIBRARY_PATH。Linux/Docker 下 apt 装系统库则留空）
  AGENT_ANALYSIS_EXTRA_LIB_PATH: str = ''

  # ==================== Agent 用户自建技能（复刻 DeerFlow skill-creator） ====================
  AGENT_SKILL_LLM_SCAN: bool = True  # skill_manage 写盘前 LLM 语义安全扫描开关（fail-closed）

  # ==================== DCIM 集成 ====================
  DCIM_ENABLED: bool = False           # DCIM 门面工具开关，默认关闭
  DCIM_BASE_URL: str = ''              # DCIM API 基地址（真实接口就绪后配置）
  DCIM_API_TOKEN: str = ''             # DCIM 服务级访问 token
  DCIM_TIMEOUT: int = 30               # DCIM 调用超时（秒）
  DCIM_RETRIEVAL_TOP_K: int = 8        # discover 检索返回的能力条数上限

  # ==================== 综合管理平台集成 ====================
  MGMT_ENABLED: bool = False           # 综合管理平台开关，默认关闭
  MGMT_BASE_URL: str = ''              # 综合管理平台 API 基地址
  MGMT_INSPECTION_BASE_URL: str = ''   # 巡检服务 API 基地址（根路径 /inspection，无 /center 前缀）
  MGMT_API_TOKEN: str = ''             # 服务级访问 token（若后端提供，跳过登录）
  MGMT_USERNAME: str = ''              # 登录账号（服务账号，最高权限）
  MGMT_PASSWORD: str = ''              # 登录密码
  MGMT_TOKEN_TTL: int = 3600           # token + deptId 缓存秒数
  MGMT_TIMEOUT: int = 30               # 调用超时（秒）
  MGMT_PEN_BASE_URL: str = ''          # pen 流程服务 API 基地址（如 https://api.ddbes.com，mgmt 能力延伸，非新平台）
  MGMT_PEN_TOKEN_TTL: int = 3600       # pen 人员 token 缓存秒数（失效由 401 重登兜底）

  # ==================== 安防平台集成 ====================
  # 占位注册：暂无接口文档，capabilities 为空元组，client.call 直接抛异常。
  # 接口文档到位后按 mgmt 的接入模式（EndpointSpec 声明式映射）填实即可。
  SECURITY_ENABLED: bool = False       # 安防平台开关，默认关闭
  SECURITY_BASE_URL: str = ''          # 安防平台 API 基地址
  SECURITY_API_TOKEN: str = ''         # 服务级访问 token
  SECURITY_TIMEOUT: int = 30           # 调用超时（秒）

  model_config = {
    'env_file': '.env',
    'env_file_encoding': 'utf-8',
    'case_sensitive': True,
    'extra': 'ignore',
  }


settings = Settings()
