"""逐页解说词生成（平台自己的 LLM，不依赖 8010 插件）。

可靠性对齐 DeerFlow 的做法（这是它 podcast TTS 与 retry 中间件的成熟模式）：
- **小并发**：一次只发 ≤3 个请求，别把 LLM 网关打满（网关有限流）
- **指数退避 + 抖动重试**：只重试可恢复错误（429/5xx/超时），4xx 契约错误立即放弃
- **单页失败不整单失败**：该页降级为空解说词并记 warning，最后全页失败才抛错

语言/风格由 prompt 控制（我们掌握 system prompt），因此多语言只是换个参数。

**配音语言 ≠ 字幕语言**（如"英文解说 + 中文字幕"）时，一次调用同时产出 speech/subtitle
（JSON 出参）：调用次数不变，两份文案也不会各说各的。两种语言相同时完全走原路径。
"""

import asyncio
import json
import logging
import random
import re

from config.settings import settings

logger = logging.getLogger(__name__)

# 可重试的 HTTP 状态码（对齐 DeerFlow `_RETRIABLE_STATUS_CODES`）
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_BACKOFF_S = 8.0

_SYSTEM_PROMPT = (
  '你是数据中心运维领域的专业讲师，负责把汇报 PPT 的一页内容讲成一段'
  '自然、口语化、有信息量的解说词。要求：\n'
  '1. 只输出解说词本身，不要任何前缀、标题、markdown 标记或括号说明；\n'
  '2. 讲"这页在说什么、意味着什么"，不要逐字念幻灯片上的字；\n'
  '3. 数字、单位、专有名词必须与页面内容一致，**不得编造页面上没有的数据**；\n'
  '4. 页面信息很少时（如封面、目录），用一句话自然带过，不要硬凑内容；\n'
  '5. **不许写"版面说明"**：不要出现"这页/本页是封面""本页标志着…开始""这是本次汇报的封面页"'
  '"接下来看目录"之类只是在描述"这是第几页/什么页"的话——直接讲这页的主题或内容；\n'
  '6. 目标语言：{language}；控制在 {max_chars} 字以内。'
)

_DUAL_SYSTEM_PROMPT = (
  '你是数据中心运维领域的专业讲师，负责把汇报 PPT 的一页内容讲成一段解说词，'
  '并同时给出配套的画面字幕。要求：\n'
  '1. speech（配音稿）用 {language} 写，讲"这页在说什么、意味着什么"，不要逐字念幻灯片上的字；\n'
  '2. subtitle（画面字幕）用 {subtitle_language} 写，与 speech **语义一一对应、长度相近**，'
  '写成能一口气读完的一句话（是意译不是逐字直译，也别加括号补充）；\n'
  '3. 数字、单位、专有名词必须与页面内容一致，**不得编造页面上没有的数据**；\n'
  '4. 页面信息很少时（如封面、目录），两边都用一句话自然带过，不要硬凑；\n'
  '5. **不许写"版面说明"**：不要出现"这页/本页是封面""本页标志着…开始""这是本次汇报的封面页"'
  '"接下来看目录"之类只是在描述"这是第几页/什么页"的话——直接讲这页的主题或内容；\n'
  '6. speech 控制在 {max_chars} 字以内；\n'
  '7. 只输出一个 JSON 对象：{{"speech": "…", "subtitle": "…"}}，'
  '不要 markdown 代码块、不要任何解释文字。'
)

# 支持的语言只有中/英两种（用户会写「中文」「英文」「English」「en-US」……）
_EN_WORDS = ('en', 'en-us', 'en_us', 'english', '英文', '英语')

# 实测语速（TTS 真实产出，用来把"目标时长"换算成每页字数上限）：
#   中文任务 1571 字 / 296s ≈ 320 字/分钟；英文任务 19944 字符 / 1150s ≈ 1040 字符/分钟
CHARS_PER_MINUTE = {'zh': 320, 'en': 1040}
DEFAULT_MAX_CHARS = 200          # 没给目标时长时的每页字数上限（沿用旧行为）
MIN_MAX_CHARS, MAX_MAX_CHARS = 60, 400
REQUIREMENTS_MAX_CHARS = 800     # 用户附加要求的长度上限（每页都会拼一次，别失控）


def max_chars_for_target(target_minutes: float, page_count: int, language: str) -> int:
  """把"目标总时长（分钟）"换算成"每页解说词字数上限"。

  近似值：总字数 ≈ 目标分钟 × 语速，再按页数平摊。写进提示词后模型会照着控制篇幅，
  但**不可能精确**（同样的字数，语速、停顿、数字读法都会影响时长），所以成片后要如实
  把实际时长告诉用户，而不是宣称"精确 5 分钟"。
  """
  if not target_minutes or target_minutes <= 0 or page_count <= 0:
    return DEFAULT_MAX_CHARS
  rate = CHARS_PER_MINUTE['en' if norm_language(language) == 'en' else 'zh']
  per_page = int(round(target_minutes * rate / page_count))
  return max(MIN_MAX_CHARS, min(MAX_MAX_CHARS, per_page))


class NarrationError(Exception):
  """全部页面的解说词都生成失败。"""


def norm_language(value: str) -> str:
  """语言标签归一化成 `'zh'` / `'en'`（认不出来按中文）。"""
  return 'en' if (value or '').strip().lower() in _EN_WORDS else 'zh'


def speech_language(value: str) -> str:
  """TTS 的 `speech_language` 取值（tts_proxy 认 `zh-CN` / `en-US`）。"""
  return 'en-US' if norm_language(value) == 'en' else 'zh-CN'


def needs_dual(language: str, subtitle_language: str) -> bool:
  """是否需要"配音语言 ≠ 字幕语言"的双份文案（没填字幕语言 = 与配音同语言）。"""
  return bool((subtitle_language or '').strip()) and (
    norm_language(subtitle_language) != norm_language(language)
  )


def _parse_dual(raw: str) -> tuple[str, str] | None:
  """解析双语 JSON；容忍 ```json 围栏与前后废话，解析不出来返回 None。"""
  text = (raw or '').strip()
  if text.startswith('```'):
    text = re.sub(r'^```[A-Za-z]*\s*', '', text)
    text = re.sub(r'\s*```$', '', text).strip()
  start, end = text.find('{'), text.rfind('}')
  if start < 0 or end <= start:
    return None
  try:
    data = json.loads(text[start:end + 1])
  except Exception:  # noqa: BLE001 - 模型偶尔会带尾逗号之类
    return None
  speech = str(data.get('speech') or '').strip()
  subtitle = str(data.get('subtitle') or '').strip()
  if not speech:
    return None
  return speech, subtitle or speech


def _is_retryable(exc: Exception) -> bool:
  status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
  if isinstance(status, int):
    return status in _RETRYABLE_STATUS
  name = type(exc).__name__
  return name in ('APITimeoutError', 'APIConnectionError', 'ReadTimeout', 'ConnectError')


async def _generate_one(
  index: int, page_text: str, language: str, max_chars: int, subtitle_language: str = '',
  requirements: str = '',
) -> tuple[int, str, str, str]:
  """生成一页文案；返回 (页号, 配音稿, 字幕文本, 错误)。失败不抛，交给调用方降级。

  字幕文本为空表示"与配音稿同语言"（用同一份，不必重复存）。
  """
  from core.model_gateway import model_gateway

  dual = needs_dual(language, subtitle_language)
  user_text = f'第 {index + 1} 页内容：\n{page_text or "（本页无文字，可能是图表或图片）"}'
  system = (
    _DUAL_SYSTEM_PROMPT.format(language=language, subtitle_language=subtitle_language,
                              max_chars=max_chars)
    if dual else _SYSTEM_PROMPT.format(language=language, max_chars=max_chars)
  )
  if (requirements or '').strip():
    # 用户在本轮确认时提的附加要求（受众/语气/片尾提示…）：拼在系统提示词末尾，
    # 明确"不得违背上面的硬规则"，避免它借此绕过"不许编造数据"这类约束。
    # **按 REQUIREMENTS_MAX_CHARS 截断**：这段是模型从对话里抄进参数的，不设上限会
    # 每页重复占上下文（N 页 × 同长度），也可能被塞进无关长文
    system += (f'\n\n【用户附加要求】（在不违背上面规则的前提下满足）'
               f'{(requirements or "").strip()[:REQUIREMENTS_MAX_CHARS]}')
  attempts = max(1, settings.VIDEO_AGENT_RETRY_ATTEMPTS)

  last_err = ''
  for attempt in range(attempts):
    try:
      raw = await model_gateway.chat(
        messages=[{'role': 'system', 'content': system},
                  {'role': 'user', 'content': user_text}],
        temperature=0.6,
        max_tokens=1024,
        caller='agent_video_narration',
      )
      text = (raw or '').strip()
      if not dual:
        return index, text, '', ''
      parsed = _parse_dual(text)
      if parsed is None:
        # 没按 JSON 返回：整段当配音稿用，字幕沿用同一份（退化为同语言字幕），不判失败
        logger.warning('第 %d 页双语解说未按 JSON 返回，字幕退化为与配音同文', index + 1)
        return index, text, '', ''
      speech, subtitle = parsed
      return index, speech, subtitle, ''
    except Exception as e:  # noqa: BLE001 - 逐页兜底，任何异常都降级
      last_err = f'{type(e).__name__}: {e}'
      if attempt + 1 >= attempts or not _is_retryable(e):
        break
      delay = min(2 ** attempt, _MAX_BACKOFF_S) + random.uniform(0, 1)
      logger.warning('解说词重试 第%d页 attempt=%d/%d %.1fs 后重试: %s',
                     index + 1, attempt + 1, attempts, delay, last_err)
      await asyncio.sleep(delay)

  logger.warning('第 %d 页解说词生成失败（该页降级为无解说）: %s', index + 1, last_err)
  return index, '', '', last_err


async def translate_text(text: str, target_language: str) -> str:
  """把一段解说词改成目标语言的**字幕**文本（只给"二次修改"用；失败返回空串）。

  为什么单开一个：用户手改某页文案时（`video_update_page`）没有"双语 JSON"那一步了，
  而配音语言 ≠ 字幕语言的组合下字幕不能直接沿用新文案，得单独换算一份。
  """
  from core.model_gateway import model_gateway

  src = (text or '').strip()
  if not src:
    return ''
  try:
    out = await model_gateway.chat(
      messages=[
        {'role': 'system', 'content':
         f'把用户给的解说词改写成 {target_language} 的**画面字幕**：语义一一对应、长度相近、'
         '是意译不是逐字直译，不含换行。只输出字幕文本本身。'},
        {'role': 'user', 'content': src},
      ],
      temperature=0.2,
      max_tokens=1024,
      caller='agent_video_subtitle_translate',
    )
    return (out or '').strip()
  except Exception as e:  # noqa: BLE001 - 翻译失败就让字幕沿用配音稿，不阻断重跑
    logger.warning('字幕文案改写失败（该页字幕沿用配音稿）: %s', e)
    return ''


async def generate_scripts(
  page_texts: list[str], *, language: str = '中文', max_chars: int = 200,
  subtitle_language: str = '', requirements: str = '',
) -> list[dict]:
  """并发生成全部页面的文案（并发 ≤VIDEO_AGENT_NARRATION_CONCURRENCY）。

  Args:
    language: 配音语言。
    subtitle_language: 字幕语言；空或与 `language` 同语言时只生成一份（`subtitle=''`）。
    requirements: 用户附加要求（受众/语气/片尾提示等），拼进提示词末尾。

  Returns:
    与 page_texts 等长的列表，每项 `{script, subtitle, error}`；单页失败时 script='' 且带 error。
    **全部失败**才抛 NarrationError（对应 DeerFlow「失败要吵」：不产出残缺品）。
  """
  if not page_texts:
    return []

  sem = asyncio.Semaphore(max(1, settings.VIDEO_AGENT_NARRATION_CONCURRENCY))

  async def _run(i: int, text: str) -> tuple[int, str, str, str]:
    async with sem:
      return await _generate_one(i, text, language, max_chars, subtitle_language, requirements)

  results = await asyncio.gather(*(_run(i, t) for i, t in enumerate(page_texts)))
  results.sort(key=lambda x: x[0])

  if all(not script for _, script, _, _ in results):
    raise NarrationError('全部页面的解说词生成失败，请检查模型服务后重试')

  return [{'script': script, 'subtitle': subtitle, 'error': err}
          for _, script, subtitle, err in results]
