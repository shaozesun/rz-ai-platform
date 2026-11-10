"""Agent 基础工具 —— 本地无外部依赖的那些

原先这里还有一个 `query_dcim_alarm`（写死假告警），属于 DCIM 门面 mock 的一部分，
随 `service/query/dcim/` 一并删除（DCIM 真实接口未接，能查回假数据会误导 Agent）。
DCIM 侧数据后续走 Kafka 接入（见 `service/dcim_ingest/`）。
"""

import json
from datetime import datetime
from langchain.tools import tool


@tool
def get_current_time() -> str:
  """获取当前系统时间。"""
  now = datetime.now()
  return json.dumps({
    'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
    'weekday': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()],
    'timestamp': int(now.timestamp()),
  }, ensure_ascii=False)
