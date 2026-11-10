"""事件统计表的确定性规则（纯函数，可独立 import 断言）。

覆盖示例表 40 种「事件类型」的人工归纳、通报违规判定、处理时长与时间文案。
**所有数值关系在服务端算好**：模型/人工都不做减法（平台既有原则，见报告侧事故）。
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo('Asia/Shanghai')

# 通报合规：速报 ≤ 发生 +2min，首报 ≤ 发生 +5min（示例表反证：121/124s 判违规、
# 118/120s 不判；304/312s 判违规、297/298s 不判）
SOS_LIMIT_SECONDS = 120
FIRST_LIMIT_SECONDS = 300

# 园区短码（cluster 前两个汉字 → 代码）；示例表用 LF-A7 这类短码，本表输出中文全称
_PARK_CODES = {'廊坊': 'LF', '平湖': 'PH', '佛山': 'FS', '惠州': 'HZ', '重庆': 'CQ'}
_PARK_NAMES = tuple(_PARK_CODES)

# 事件类型规则：**顺序即优先级**，先具体后笼统（否则「空调进水温度过高」会被
# 「温度过高」抢走、「电池开关柜通讯中断」会被「电池通讯中断」抢走）。
_RULES: tuple[tuple[str, str], ...] = (
  # 电池
  (r'内阻低|内阻偏低', '电池内阻低'),
  (r'内阻高|内阻偏高', '电池内阻高'),
  (r'均差值.*高', '电池均差值过高'),
  (r'整组电压.*过高|整组电压.*高', '电池组电压过高'),
  (r'整组电压.*过低', '电池组电压过低'),
  (r'电池温度.*过低', '电池温度低'),
  (r'电池温度.*过高|电池温度.*高', '电池温度高'),
  (r'内阻', '电池内阻异常'),
  # 通讯（具体到设备，再回落通用）
  (r'空调配电柜.*通讯中断', '空调配电柜设备通讯中断'),
  (r'电池开关柜.*通讯中断', '电池开关柜通讯中断'),
  (r'低压柜.*通讯中断', '低压柜通讯中断'),
  (r'温湿度.*通讯中断', '温湿度通讯中断'),
  (r'空调.*通讯中断', '空调通讯中断'),
  (r'电池.*通讯中断', '电池通讯中断'),
  (r'嵌入式通讯中断', '嵌入式通讯中断'),
  (r'通讯中断|通讯状态', '设备通讯中断'),
  # 温度（具体设备先于「温度过高」）
  (r'进水温度.*高', '空调进水温度过高'),
  (r'冷塔.*水槽.*温度', '冷塔水槽温度高'),
  (r'冷塔.*供水压力低', '冷塔供水压力低'),
  (r'温度过高', '机房单点高温'),
  # 配电
  (r'电压过低|电压波动', '市电波动'),
  (r'主进.*断开', 'UPS主进断开'),
  (r'开关断开|开关状态', '机柜单路掉电'),
  (r'旁路频率越限', 'UPS旁路频率越限'),
  (r'二次泵.*断开', '二次泵AB路断开'),
  (r'单线电流归零', '列头柜单线电流归零'),
  (r'电流归零', '列头柜电流归零'),
  (r'总告警', '列头柜总告警'),
  # 暖通 / 水
  # 带捕获组的标签走 `pattern.sub()`，所以 pattern 要覆盖**整段**（含「冷机」），
  # 否则保留的前缀会与替换文本叠成「冷机冷机报警代码12」
  (r'冷机.*?报警代码\s*(\d+)', r'冷机报警代码\1'),
  (r'恒湿机.*水位|恒湿机.*低水位', '恒湿机水位低'),
  (r'恒湿机.*(故障|过载)', '恒湿机故障'),
  (r'除湿机.*漏水', '除湿机漏水'),
  (r'漏水绳', '漏水绳断开'),
  (r'水泵房.*漏水', '水泵房漏水'),
  (r'空调.*漏水', '空调间漏水'),
  (r'漏水', '漏水报警'),
  (r'油箱.*液位.*高', '油箱液位高'),
)

_COMPILED: tuple[tuple[re.Pattern, str], ...] = tuple(
  (re.compile(pat), label) for pat, label in _RULES
)

_ORDER_RESULT_LABELS = {1: '确认告警', 2: '已恢复正常'}
_ALARM_STATUS_RECOVERED = '3'


def event_type(name: str) -> str:
  """告警名称 → 事件类型短标签；未命中时取名称最后一段并去掉报警/告警后缀。"""
  text = (name or '').strip()
  if not text:
    return ''
  for pattern, label in _COMPILED:
    if pattern.search(text):
      return pattern.sub(label, text) if '\\' in label else label
  tail = re.split(r'[-－—]', text)[-1].strip()
  tail = re.sub(r'(报警|告警|告警告警)$', '', tail).strip()
  return tail or text


def event_progress(order_no: str | None, process_result, order_status,
                   alarm_status, matched: bool) -> str:
  """事件进展：工单处理结果优先；无工单时按告警是否恢复降级。"""
  if order_no:
    try:
      result = int(process_result)
    except (TypeError, ValueError):
      result = None
    if result in _ORDER_RESULT_LABELS:
      return _ORDER_RESULT_LABELS[result]
    return '处理中'
  if not matched:
    return ''
  return '已解除' if str(alarm_status) == _ALARM_STATUS_RECOVERED else '未处理'


def _over_text(seconds: int) -> str:
  if seconds < 60:
    return f'{seconds}秒'
  minutes, rest = divmod(seconds, 60)
  return f'{minutes}分{rest}秒' if rest else f'{minutes}分钟'


def violation(occurred_s: int | None, sos_s: int | None, first_s: int | None,
              sos_limit: int = SOS_LIMIT_SECONDS,
              first_limit: int = FIRST_LIMIT_SECONDS) -> str:
  """过程有无违规文案。

  - 超时秒数 = 通报时间 − 发生时间 − 阈值（示例表口径：「速报超时1秒」= 121s − 120s）
  - 缺通报 → 「缺速报」/「缺首报」；发生时间不明 → 空串（不猜）
  """
  if not occurred_s:
    return ''
  parts: list[str] = []
  if sos_s is None:
    parts.append('缺速报')
  else:
    over = int(sos_s) - int(occurred_s) - sos_limit
    if over > 0:
      parts.append(f'速报超时{_over_text(over)}')
  if first_s is None:
    parts.append('缺首报')
  else:
    over = int(first_s) - int(occurred_s) - first_limit
    if over > 0:
      parts.append(f'首报超时{_over_text(over)}')
  return '、'.join(parts) if parts else '无'


def duration_text(start_s, end_s) -> str:
  """处理时长：完成 − 开始，格式 `N分钟` / `X小时Y分钟`；缺失或倒挂留空。"""
  try:
    start, end = int(start_s), int(end_s)
  except (TypeError, ValueError):
    return ''
  if start <= 0 or end <= 0 or end < start:
    return ''
  minutes = (end - start) // 60
  if minutes < 1:
    return '不足1分钟'
  hours, rest = divmod(minutes, 60)
  if hours and rest:
    return f'{hours}小时{rest}分钟'
  return f'{hours}小时' if hours else f'{rest}分钟'


def to_epoch(value) -> int | None:
  """秒级 epoch；容错毫秒、0/None。"""
  try:
    n = int(value)
  except (TypeError, ValueError):
    return None
  if n <= 0:
    return None
  return n // 1000 if n >= 1_000_000_000_000 else n


def fmt_event_time(value) -> str:
  """epoch 秒 → `YYYY年MM月DD日HH时MM分SS秒`（北京时间）。"""
  ts = to_epoch(value)
  if ts is None:
    return ''
  return datetime.fromtimestamp(ts, _TZ).strftime('%Y年%m月%d日%H时%M分%S秒')


def fmt_date(value) -> str:
  ts = to_epoch(value)
  if ts is None:
    return ''
  return datetime.fromtimestamp(ts, _TZ).strftime('%Y-%m-%d')


def park_of(cluster: str | None) -> tuple[str, str]:
  """cluster → (园区名, 短码)。未登记时原名返回。"""
  name = (cluster or '').strip()
  for park in _PARK_NAMES:
    if name.startswith(park):
      return park, _PARK_CODES[park]
  return name, name


def place(cluster: str | None, building: str | None) -> str:
  """地点文案：`{园区}园区{building}数据中心`；楼宇名已含「数据中心」则不重复追加。"""
  park, _ = park_of(cluster)
  bld = (building or '').strip()
  if not bld:
    return park
  if '数据中心' in bld:
    return bld if bld.startswith(park) else f'{park}园区{bld}'
  return f'{park}园区{bld}数据中心'
