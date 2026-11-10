"""循环检测 — 借鉴 DeerFlow 的双层检测算法

Pattern A: 连续相同 tool_call ≥ warn_threshold 次 → warn, ≥ hard_limit → stop
Pattern B: 同一工具累计调用 ≥ warn_threshold → warn, ≥ hard_limit → stop
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

WARN_THRESHOLD = 3
# 连续 N 次**完全相同**的调用 → 中止。这是抓"真死循环"的那道闸，**不要放宽**：
# 跨部门扇出的每次调用参数不同（dept_id 不同），本来就不会触发它。
HARD_LIMIT = 5

# 同一工具**累计**调用次数的兜底默认值。运行时以 settings 为准（可 .env 调），
# 常量只作 settings 读取失败时的兜底 —— 见 `_freq_limits()`。
TOOL_FREQ_WARN = 10
TOOL_FREQ_HARD = 20


def _freq_limits() -> tuple[int, int]:
  """读「同一工具累计调用」的 (warn, hard) 上限。

  **为什么可配置**（2026-10-15）：综合管理平台有一批接口按部门返回（`query_user_list` 的
  `dept_id` 必填），「查全公司人员」要跨 25 个部门各调一次 → 写死 20 会在第 20 次中止整轮、
  任务直接失败。放宽只是临时措施，正解是把扇出下沉到服务端（对齐 `all_companies`）。
  """
  try:
    from config.settings import settings

    return int(settings.AGENT_TOOL_FREQ_WARN), int(settings.AGENT_TOOL_FREQ_HARD)
  except Exception:      # settings 不可用（测试/极端环境）→ 用兜底常量
    return TOOL_FREQ_WARN, TOOL_FREQ_HARD


@dataclass
class LoopDetector:
  """单次运行的循环检测器，在 stream 生成器生命周期内使用"""

  # Pattern A: 连续相同 tool_call
  consecutive_count: int = 0
  last_tool_hash: str = ''

  # Pattern B: 各工具累计调用次数
  tool_call_counts: dict[str, int] = field(default_factory=dict)

  # 是否已触发 hard stop
  stopped: bool = False
  stop_reason: str = ''

  @staticmethod
  def _hash_tool_call(tool_name: str, args: dict) -> str:
    """稳定哈希：工具名 + 参数"""
    payload = json.dumps({'name': tool_name, 'args': args}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

  def feed(self, tool_name: str, args: dict) -> str | None:
    """记录一次工具调用，返回 None=正常，str=错误消息"""
    if self.stopped:
      return self.stop_reason

    call_hash = self._hash_tool_call(tool_name, args)

    # Pattern A: 连续相同调用
    if call_hash == self.last_tool_hash:
      self.consecutive_count += 1
    else:
      self.consecutive_count = 1
      self.last_tool_hash = call_hash

    # Pattern B: 工具累计次数
    self.tool_call_counts[tool_name] = self.tool_call_counts.get(tool_name, 0) + 1
    freq_count = self.tool_call_counts[tool_name]
    freq_warn, freq_hard = _freq_limits()

    # 检查阈值
    if self.consecutive_count >= HARD_LIMIT:
      self.stopped = True
      self.stop_reason = (
        f'检测到连续 {self.consecutive_count} 次相同工具调用 ({tool_name})，'
        f'已超过上限 {HARD_LIMIT}，Agent 运行已中止。请重新描述问题。'
      )
      return self.stop_reason

    if freq_count >= freq_hard:
      self.stopped = True
      self.stop_reason = (
        f'工具 "{tool_name}" 累计调用 {freq_count} 次，已超过上限 {freq_hard}，'
        f'Agent 运行已中止。（上限由 AGENT_TOOL_FREQ_HARD 配置；'
        f'跨部门/跨公司这类需要多次调用的查询，应改用服务端聚合参数而不是逐条循环。）'
      )
      return self.stop_reason

    if self.consecutive_count >= WARN_THRESHOLD:
      logger.warning(
        'LoopDetect Pattern A warn: tool=%s consecutive=%d',
        tool_name, self.consecutive_count,
      )

    if freq_count >= freq_warn:
      logger.warning(
        'LoopDetect Pattern B warn: tool=%s frequency=%d',
        tool_name, freq_count,
      )

    return None
