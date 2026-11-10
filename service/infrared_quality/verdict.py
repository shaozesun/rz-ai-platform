"""模型标签 → 判定。口径（用户确认，fail-closed，2026-11-10 接口改版后）：

  sharp        → 合格
  borderline   → 待复核
  defocused    → 不合格
  其余（uncertain、空、未知、推理异常）→ 检测失败

合格/待复核两组标签走配置。**只认新标签**：旧中文输出（清晰/临界状态…）属
未识别，一律检测失败——按用户口径，旧任务勾「重新推理」重跑即可。
**认不出的一律检测失败**，绝不默认放过。
"""

from config.settings import settings

PASS = '合格'
REVIEW = '待复核'
BAD = '不合格'
FAILED = '检测失败'

_BAD_MARKERS = ('defocused',)


def _labels(raw: str) -> set[str]:
  out: set[str] = set()
  for token in str(raw or '').replace('，', ',').replace('；', ';').replace('、', ',').split(','):
    token = token.strip().lower()
    if token:
      out.add(token)
  return out


def pass_labels() -> set[str]:
  return _labels(settings.INFRARED_QUALITY_PASS_LABELS)


def review_labels() -> set[str]:
  return _labels(settings.INFRARED_QUALITY_REVIEW_LABELS)


def classify(label: str, error: str = '') -> tuple[str, str]:
  """返回 `(判定, 说明)`；说明只在与判定不同的情况下补原因。"""
  if error:
    return FAILED, f'推理异常：{error}'
  text = str(label or '').strip()
  if not text:
    return FAILED, '无预测结果'
  key = text.lower()
  if key in pass_labels():
    return PASS, ''
  if key in review_labels():
    return REVIEW, ''
  if any(mark in key for mark in _BAD_MARKERS):
    return BAD, ''
  return FAILED, f'模型输出未识别：{text}'


def download_failed(exc: Exception) -> tuple[str, str]:
  return FAILED, f'图片下载失败：{type(exc).__name__}'
