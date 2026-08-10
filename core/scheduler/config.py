from pydantic_settings import BaseSettings


class SchedulerSettings(BaseSettings):
  enabled: bool = True
  max_concurrent_llm_calls: int = 10
  queue_timeout: int = 120
  slot_acquire_timeout: int = 5
  stream_slot_timeout: int = 10
  semaphore_key: str = 'scheduler:slots'
  queue_key: str = 'scheduler:queue'
  notify_channel: str = 'scheduler:notify'

  model_config = {
    'env_prefix': 'SCHEDULER_',
    'case_sensitive': False,
    'extra': 'ignore',
  }


scheduler_settings = SchedulerSettings();
