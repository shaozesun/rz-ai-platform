import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
  # ==================== 服务 ====================
  HOST: str = '0.0.0.0'
  PORT: int = 8000
  WORKERS: int = 4
  PROJECT_NAME: str = '润泽AI平台'
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

  # 模型调用参数
  LLM_TIMEOUT: int = 60
  LLM_MAX_RETRIES: int = 2
  LLM_TEMPERATURE: float = 0.7

  # ==================== RAG ====================
  CHUNK_SIZE: int = 1200
  CHUNK_OVERLAP: int = 150
  RAG_TOP_K: int = 10
  RAG_FETCH_K_GLOBAL: int = 150
  RAG_FILE_TOP_K: int = 5
  RAG_FUSION_TOP_K: int = 20
  RAG_FINAL_TOP_K: int = 6

  # RAG 融合权重
  RAG_FUSION_A: float = 0.3
  RAG_FUSION_B: float = 0.3
  RAG_FUSION_C: float = 0.2
  RAG_FUSION_D: float = 0.1
  RAG_FUSION_E: float = 0.1

  # BM25
  RAG_SPARSE_NORM: str = 'z_score'
  RAG_SIGMOID_SCALE: float = 8.0
  RAG_DENSE_WEIGHT: float = 0.6
  RAG_SPARSE_WEIGHT: float = 0.4

  # 重排序
  RAG_CROSS_ENCODER_ENABLED: bool = False
  RAG_INTENT_CLASSIFY_ENABLED: bool = False
  RAG_QUERY_REWRITE_ENABLED: bool = True

  # 检索缓存
  RAG_CACHE_ENABLED: bool = True
  RAG_CACHE_TTL: int = 3600
  # Parent-Child 切分
  RAG_A_PARENT_ENABLED: bool = False
  RAG_A_PARENT_CHUNK_SIZE: int = 2000
  RAG_A_PARENT_CHUNK_OVERLAP: int = 200
  RAG_A_CHILD_CHUNK_SIZE: int = 500
  RAG_A_CHILD_CHUNK_OVERLAP: int = 50

  RAG_DEBUG: bool = False

  # ==================== Milvus ====================
  MILVUS_URI: str = 'http://localhost:19530'
  MILVUS_TOKEN: str = ''
  MILVUS_DB_NAME: str = 'default'

  # ==================== 文件 ====================
  UPLOAD_DIR: str = str(Path(__file__).parent.parent / 'uploads')
  EMBEDDED_DIR: str = str(Path(__file__).parent.parent / 'embedded')
  SUPPORTED_EXTENSIONS: str = 'pdf,docx,xlsx,xls,csv,txt,md,png,jpg,jpeg'

  # ==================== Coze Studio ====================
  COZE_API_BASE: str = 'http://localhost:8888'
  COZE_PAT_TOKEN: str = ''

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
  MINERU_API_BASE: str = ''
  MINERU_API_TOKEN: str = ''
  MINERU_BACKEND: str = 'pipeline'
  MINERU_LANG: str = 'zh'
  MINERU_TIMEOUT: int = 600
  MINERU_API_POLL_INTERVAL: int = 5
  MINERU_API_MAX_POLL: int = 60

  # ==================== 部署 ====================
  ALLOWED_ORIGINS: str = '*'
  RATE_LIMIT_PER_MINUTE: int = 60
  RATE_LIMIT_SMS_PER_HOUR: int = 20

  # 权限全开模式 (开发/调试用, 认证用户自动获得全部权限)
  PERMISSION_OPEN_MODE: bool = False

  model_config = {
    'env_file': '.env',
    'env_file_encoding': 'utf-8',
    'case_sensitive': True,
  }


settings = Settings()
