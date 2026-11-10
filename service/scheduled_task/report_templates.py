"""分析报告预设 —— 系统预置的定期报告，**一份、共享、只读**。

定位（2026-10-13 与产品确认）：报告是**系统级共享产出**（像新闻）——不是每个用户各自的任务，
每个预设全平台只跑一份、所有人看同一份，用户只"看"，**没有订阅/启停/删除**。因此：
- 预设任务由后端**启动时自动 ensure**（`ensure_preset_tasks()`，按 key 幂等，`created_by='system'`）；
  绝不能给每个用户各建一份（N 个用户＝同一份报告重复跑 N 遍）。
- 查看侧只读且**不按人隔离**（数据本身就是全国/园区的公共口径），权限 `ai:agent`。

预设规模 = **5 类工单 ×（1 全国 + N 个园区）× 2 个时间维度**（供应商服务工单暂不出报告）：
- 空间：`nationwide`（全国：各园区汇总 + 对比）／**每个园区各一份**（园区内部 + 部门下钻）
- 时间：`weekly`（周报）／`monthly`（月报）
**园区不写死在代码里**：启动时从平台数据发现（`_list_pen_companies` 的可见公司 + `query_company_list`
的 `gardenName`），当前 5 个国内园区（廊坊/平湖/惠州/佛山/重庆）+ 全国 → 5 × 6 × 2 = 60 条；
问题单量太少，只出「全国 · 月度」→ 实际 **49 条**（`TICKETS` 条目的 `periods` 限制周期、
`national_only` 跳过园区）。
加类型/加园区都只是数据变化，代码只认 `TICKETS` 这张表。

三个设计要点：
1. **窗口由平台算好注入**（`window_hint()` → runner 前置到指令）：任务反复跑，绝对日期会让第二遍
   之后永远分析同一窗口；模型自算实测**错一整周**。
2. **范围必须显式**：全国级传 `all_companies=true`；园区级传 `company_id=<pen kingdee>` ——
   任务以**服务账号**身份跑，不写清楚会兜底到某个默认公司/部门。
3. **绝不出现身份步骤**：模型曾因"先确认本公司/部门"去调 `query_my_info` → 服务账号没有
   "当前用户" → 报错绕圈（实测一轮 1h52m 后失败）。
"""

import hashlib

# 周期定义（窗口 + 触发时段）
PERIODS: dict[str, dict] = {
  'weekly': {'label': '每周', 'window': '上一个完整自然周（上周一 00:00 至上周日 23:59，按公司本地时区）',
             'trend': '按日', 'dow': 1, 'day_of_month': None},
  # 月报**每月执行**（不做周更），落在**每月 1 日凌晨**（窗口=刚结束的那个自然月；
  # 例如 11-01 跑，分析的是 10-01~10-31 ✓）。1 日恰逢周一时会与周报叠在同一夜（共 49 份），
  # 但错峰 + 并发闸（默认 3）下约 1.5–2 小时跑完，仍在上班前。
  'monthly': {'label': '每月', 'window': '上一个完整自然月（上月 1 日 00:00 至该月最后一天 23:59）',
              'trend': '按日（必要时再按周汇总）', 'dow': None, 'day_of_month': 1},
}

# 触发时间（凌晨跑，不影响白天使用）：全国 03:00 起每类 +5 分钟；园区级 04:00 起、各园区散列到 30 个槽位
_NATIONWIDE_HOUR, _PARK_HOUR, _STEP_MIN = 3, 4, 5
_PARK_SLOTS = 30   # 30 × 5 = 150 分钟 → 04:00–06:25

# 工单类型（报告主体）
TICKETS: tuple[dict, ...] = (
  {'id': 'change', 'name': '变更工单', 'capability': 'mgmt.query_change_ticket',
   'dims': '**变更类型**（计划/非计划/紧急）、**变更等级**（低/中/高风险）、专业分布'},
  {'id': 'breakdown', 'name': '故障工单', 'capability': 'mgmt.query_breakdown_ticket',
   'dims': '**故障级别**、**故障设备类型**（按专业分表）、专业分布'},
  # 问题单量太少（实测周窗口常只有 1~3 单）：只出「全国 · 月度」，不做园区统计
  {'id': 'question', 'name': '问题工单', 'capability': 'mgmt.query_question_ticket',
   'dims': '**问题级别**、专业分布', 'periods': ('monthly',), 'national_only': True},
  {'id': 'event', 'name': '事件工单', 'capability': 'mgmt.query_event_ticket',
   'dims': '**事件级别**、专业分布'},
  {'id': 'threshold', 'name': '阈值工单', 'capability': 'mgmt.query_threshold_ticket',
   'dims': '专业分布、阶段（进度）分布'},
  # 供应商服务工单**暂不出报告**：列表接口无时间筛选、确定性 ReportSpec 未实现，
  # 只能走老 agent 路径且口径不清。`periods: ()` → 不生成预设 key，启动时
  # `disable_stale_system_tasks()` 把库里已有的 supplier 预设停用（不删除，可恢复）。
  # 条目本身保留：`parse_key` 仍认 supplier 前缀，历史 key / 名称不受影响。
  {'id': 'supplier', 'name': '供应商服务工单', 'capability': 'mgmt.query_supplier_ticket',
   'dims': '**服务类型**（例行维护/故障检修/技术培训/其他）、阶段分布', 'no_time': True,
   'periods': ()},
)
_TICKET_BY_ID = {t['id']: t for t in TICKETS}

_NATIONWIDE = 'nationwide'

# ==================== 园区清单（固定，不依赖接口） ====================

# 出报告的园区**写死在这里**，不再启动时调 pen/mgmt 接口发现。
# 为什么改成固定（2026-10-25 生产事故）：原 `discover_parks()` 两处对外调用都吞异常，
# 失败即返回 [] → 预设集合退化成 9 条全国口径 → `disable_stale_system_tasks()` 把 40 条
# 园区预设整批停用（只停用不删除）→ 报告页「空间」下拉（由预设清单推导）里园区全没了；
# 而 `ensure_system_task()` 不动 enabled，重启不会自愈。园区是低频变化的基础设施，
# 新增园区改这张表即可（slug 进 preset key，name 进报告标题，kingdee = pen 的 kingdeeCompanyId）。
PARKS: tuple[dict, ...] = (
  {'slug': '廊坊', 'name': '廊坊园区', 'kingdee': 'sA8AAAAAAEYM567U'},
  {'slug': '平湖', 'name': '平湖园区', 'kingdee': 'sA8AAAAAAULM567U'},
  {'slug': '惠州', 'name': '惠州园区', 'kingdee': 'sA8AAAAAAPjM567U'},
  {'slug': '佛山', 'name': '佛山园区', 'kingdee': 'sA8AAAAAAGLM567U'},
  {'slug': '重庆', 'name': '重庆园区', 'kingdee': 'sA8AAAAAAbrM567U'},
)


async def discover_parks() -> list[dict]:
  """出报告的园区清单（固定值，见 `PARKS`）。

  保留 async 签名：预设初始化、事件统计预设、报告窗口解析都按 `await` 调它，
  改同步要牵动三处调用点而毫无收益。返回副本，防调用方就地改到模块常量。
  """
  return [dict(p) for p in PARKS]


# ==================== key 编码 / 解码 ====================

def preset_key(ticket_id: str, park: str, period: str) -> str:
  """`{ticket}__{park}__{period}`；全国用 park='nationwide'。"""
  return f'{ticket_id}__{park}__{period}'


def parse_key(key: str) -> tuple[str, str, str] | None:
  """key → (ticket_id, park, period)；格式不符返回 None。"""
  parts = (key or '').split('__')
  if len(parts) != 3 or parts[0] not in _TICKET_BY_ID or parts[2] not in PERIODS:
    return None
  return parts[0], parts[1], parts[2]


def is_preset_key(key: str) -> bool:
  """是否为本模块定义的预设 key（runner 判断"要不要按报告逻辑处理"用）。"""
  return parse_key(key) is not None


def preset_cron(key: str) -> str:
  """按预设 key 生成 cron（**只依赖 key**，保证 ensure 幂等、重启不抖动）。

  全国：03:00 起，按工单类型序号 +5 分钟（03:00–03:25）
  园区级：04:00 起，园区散列到 30 个槽位 ×5 分钟（04:00–06:25）
  """
  parsed = parse_key(key)
  if not parsed:
    return '0 3 * * 1'
  ticket_id, park, period = parsed
  t_idx = [t['id'] for t in TICKETS].index(ticket_id)
  if park == _NATIONWIDE:
    minute, hour = t_idx * _STEP_MIN, _NATIONWIDE_HOUR
  else:
    digest = int(hashlib.sha1(park.encode('utf-8')).hexdigest(), 16)
    slot = digest % _PARK_SLOTS
    hour, minute = _PARK_HOUR + (slot * _STEP_MIN) // 60, (slot * _STEP_MIN) % 60
  day = PERIODS[period]['day_of_month']
  if day:
    return f'{minute} {hour} {day} * *'
  return f'{minute} {hour} * * {PERIODS[period]["dow"]}'


def preset_name(ticket_id: str, park: str, period: str, park_name: str = '') -> str:
  space = '全国' if park == _NATIONWIDE else (park_name or f'{park}园区')
  return f'{_TICKET_BY_ID[ticket_id]["name"]}分析报告 · {space} · {PERIODS[period]["label"]}'


def preset_frequency(key: str) -> str:
  """触发频率文案（含具体时间）。"""
  parsed = parse_key(key)
  if not parsed:
    return ''
  _t, park, period = parsed
  cron = preset_cron(key).split()
  minute, hour = int(cron[0]), int(cron[1])
  day = PERIODS[period]['day_of_month']
  when = f'每月 {day} 日' if day else '每周一'
  return f'{when} {hour:02d}:{minute:02d}（{"上一个自然周" if period == "weekly" else "上一个自然月"}）'


# ==================== prompt 渲染 ====================

def _time_line(ticket: dict, period: str) -> str:
  if ticket.get('no_time'):
    return ('该接口**不支持时间筛选**（没有起止时间参数）→ 按**当前状态快照**统计，'
            '不要传 start_time/end_time，也不要与上期做环比（可以说明这是快照口径）。')
  return (f'以消息开头的「【本次报告窗口】」为准（{PERIODS[period]["window"]}），'
          '成对传给 `start_time` / `end_time`（只传一个会按全量历史返回）；**不要自行推算日期**。')


def _space_block(park: str, park_name: str) -> str:
  if park == _NATIONWIDE:
    return ('5. **各园区横向对比**：先给**全国汇总**（总量、级别/专业分布、处理时效），再按 `companyName` '
            '列出各园区数量与占比、与**上一同长度周期**的环比，点出异常升高/降低的园区并给出原因假设。')
  return (f'5. **园区内部下钻**：按**部门（`dc`，已是部门名）**列出本园区各分部的工单分布与占比，'
          f'并给出各部门的趋势与处理时效差异（本报告**只覆盖 {park_name}**，不要混入其他园区）。')


def render_prompt(key: str, park_name: str = '', kingdee: str = '') -> str:
  """渲染任务指令。园区级必须传 kingdee（写进 params，避免兜底到默认公司）。"""
  parsed = parse_key(key)
  if not parsed:
    raise ValueError(f'未知的报告预设: {key}')
  ticket_id, park, period = parsed
  ticket, meta = _TICKET_BY_ID[ticket_id], PERIODS[period]
  if park == _NATIONWIDE:
    scope = ('- 空间范围：**全国所有公司/园区** —— params 里传 `all_companies=true`'
             '（服务端逐公司合并，每条记录带 `companyName`）；**不要**只查某一个公司或部门。')
  else:
    scope = (f'- 空间范围：**只统计「{park_name or park}」** —— params 里传 `company_id=\'{kingdee}\'`'
             '（pen 的 kingdeeCompanyId），**不要**传 `all_companies`。')
  return f'''请产出一份《{preset_name(ticket_id, park, period, park_name)}》，读者是数据中心运维管理层。
**这是深度分析报告，不是数据罗列** —— 每个数字都要有解读，每个结论都要落到可执行的动作。

## 数据与范围（严格遵守）
- 数据源：用 `analysis_load(capability_id='{ticket["capability"]}', params={{...}})` 把**全量**物化到沙箱
  （工具会自动翻页；**禁止**只拿单页明细当样本统计）。
{scope}
- 时间范围：{_time_line(ticket, period)}
- 本任务以**服务账号**身份运行、**没有「当前用户」**：**不要调用 `query_my_info` 等「我的…」类工具**，
  也不需要先确认公司/部门（范围已在上文给定）。

## 必做维度
1. 规模与趋势：周期内**{meta["trend"]}**工单量，指出峰值/低谷并解释可能原因
2. 类型特征：{ticket["dims"]}
3. 处理时效：从发起到了结的时长分布，指出超时与长期未闭环的积压
4. 重复发生：按名称/专业聚类，指出反复出现的同类问题
{_space_block(park, park_name or park)}

## 产出要求
- 每个维度都要给出**结论**而非数字堆砌：异常点 → 可能原因 → **可执行的改进建议**（写清动作与责任维度）。
- 图表：产出 2–4 张 `<chart>`（趋势必选，另配分布/对比），数据必须来自上面的聚合结果；
  数据点少于 3 个或只有一个类别时改用文字说明。
- **数据真实性**：结论必须可追溯到物化数据；拿不到的写「数据不可得」，**禁止估算或编造**。
- **交付格式只允许 PDF**（**不要**生成 .docx / .xlsx / .pptx）：图先存 PNG（`charts/*.png`），
  HTML 用**相对路径**引用 `<img src="charts/x.png"/>`，再用沙箱内置的
  **`render_pdf('report.html', '报告.pdf')`** 生成（**不要自己调 WeasyPrint**：沙箱禁网会连 urllib
  一起禁，图片会**静默丢失**）。生成后自检图片数 ≥ 图表数，并**只把 PDF 用 `<file>` 交付**。
- 对话里**只给结论摘要**（3–5 条要点 + 关键数字），正文放 PDF 里，不要在对话里复述全文。'''


# ==================== 窗口（平台算好、注入） ====================

def window_for(period: str, now=None) -> tuple:
  """按**触发时刻**算本期窗口（上一完整自然周/月），返回 (start_date, end_date)。"""
  from datetime import datetime, timedelta
  from zoneinfo import ZoneInfo
  from config.settings import settings

  try:
    tz = ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE)
  except Exception:
    tz = ZoneInfo('Asia/Shanghai')
  today = (now or datetime.now(tz)).astimezone(tz).date()
  if period == 'monthly':
    first_this = today.replace(day=1)
    last_end = first_this - timedelta(days=1)
    return last_end.replace(day=1), last_end
  this_monday = today - timedelta(days=today.weekday())
  return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def prev_window(period: str, now=None) -> tuple:
  """**上一期**窗口（环比口径），返回 (start_date, end_date)。

  为什么平台要算：模型自算上一期窗口实测**错一整周**（10-12 周一应取 10-05~10-11，取了
  09-28~10-04）。更糟的是模板要求「与上一同长度周期环比」而只给了本期窗口时，模型干脆
  **整段回避**（上一期事件报告里「环比」出现 0 次）——所以环比期必须和本期一样由平台算好注入。
  """
  from datetime import timedelta

  start, _end = window_for(period, now)
  if period == 'monthly':
    anchor = start - timedelta(days=1)      # 上一期的最后一天 = 本期 start 的前一天
    return anchor.replace(day=1), anchor
  return start - timedelta(days=7), start - timedelta(days=1)


def period_of(preset_key: str, now=None) -> tuple[str, str]:
  """预设 key → （期号 key, 期号文案），如 ('2026-W41', '2026 第 41 周')；非预设返回 ('','')。

  期号由**平台按窗口算好**落库（前端「期刊目录」按期分组/筛选要用，不必解析产物文件名）。
  agent 报告路径与确定性工作流共用这一份（两处都别各写一套日期推算）。
  `now` 只给补跑历史期次用（传目标期的「次月 1 日」即得该期）；正常运行不传。
  """
  parsed = parse_key(preset_key)
  if not parsed:
    return '', ''
  period = parsed[2]
  start, _end = window_for(period, now)
  if period == 'monthly':
    return f'{start.year:04d}-{start.month:02d}', f'{start.year} 年 {start.month} 月'
  iso_year, iso_week, _ = start.isocalendar()
  return f'{iso_year:04d}-W{iso_week:02d}', f'{iso_year} 第 {iso_week} 周'


def window_hint(key: str, now=None) -> str:
  """预设的「本次窗口」提示行（**非预设 / 快照类返回空串**），由 runner 在触发时前置到指令里。"""
  parsed = parse_key(key)
  if not parsed:
    return ''
  ticket_id, _park, period = parsed
  if _TICKET_BY_ID[ticket_id].get('no_time'):
    return ('【本次口径】当前状态**快照**（该接口不支持时间筛选）：不要传起止时间，'
            '也不要做环比；在报告封面写明"数据为生成时点的状态快照"。')
  start, end = window_for(period, now)
  label = PERIODS[period]['label']
  p_start, p_end = prev_window(period, now)
  return (f'【本次报告窗口】{start.isoformat()} ~ {end.isoformat()}'
          f'（{label}｜一个完整自然{"周" if period == "weekly" else "月"}，含首尾）。\n'
          f'【上一期窗口（做环比用）】{p_start.isoformat()} ~ {p_end.isoformat()}'
          f'（同长度，含首尾）。\n'
          f'**请严格按上面这两个区间筛选数据**（本期=主体，上一期=环比），'
          f'报告封面与图表标题都写本期区间；**不要自行推算日期**。')


# ==================== 预设清单（从库里的 system 任务反推） ====================

def preset_meta(key: str, name: str = '') -> dict | None:
  """key → 前端要的元信息（类型/空间/园区/周期/频率）；格式不符返回 None。"""
  parsed = parse_key(key)
  if not parsed:
    return None
  ticket_id, park, period = parsed
  space_label = '全国' if park == _NATIONWIDE else f'{park}园区'
  return {
    'key': key,
    'name': name or preset_name(ticket_id, park, period),
    'ticket_type': ticket_id,
    'ticket_name': _TICKET_BY_ID[ticket_id]['name'],
    'space': 'nationwide' if park == _NATIONWIDE else 'by_park',
    'space_label': space_label,
    'park': park,               # 'nationwide' 或园区短名
    'park_label': space_label,
    'period': period,
    'period_label': PERIODS[period]['label'],
    'capability_id': _TICKET_BY_ID[ticket_id]['capability'],
    'cron': preset_cron(key),
    'frequency': preset_frequency(key),
  }


# ==================== ensure（启动时建/同步预设任务） ====================

async def ensure_preset_tasks() -> list[str]:
  """启动时把预设落成「系统任务」（多 worker 安全：加锁后只由一个 worker 执行）。

  4 个 uvicorn worker 的 lifespan 都会调这里，而 `ensure_system_task` 是「先查后插」：
  并发执行会为同一预设各插一条（2026-10-19 实测 49 个预设被建出 87 条启用任务，
  重复任务同时运行 → 重复产出 + pen 同接口并发「请勿重复请求」）。故用 Redis 选主锁
  只让一个 worker 初始化；Redis 抖动时 fail-open，退回原行为（幂等仍成立）。
  """
  import logging

  from service.channels.leader import LeaderLock

  log = logging.getLogger(__name__)
  lock = LeaderLock('sched:preset-ensure', ttl=300)
  try:
    acquired = lock.try_acquire()
  except Exception as e:
    log.warning('预设初始化锁获取失败（继续执行）: %s', e)
    acquired = True
  if not acquired:
    log.info('预设任务已由其他 worker 初始化，跳过')
    return []
  try:
    return await _ensure_preset_tasks_inner()
  finally:
    lock.release()


async def _ensure_preset_tasks_inner() -> list[str]:
  """建/同步全部预设任务（调用方负责多 worker 互斥）。

  - 园区清单来自固定常量 `PARKS`（新增园区改那张表）→ 园区预设随之增减；
    下线园区由 `disable_stale_system_tasks()` 停用（不删除）
  - 只维护一条/预设（`created_by='system'`），**不按用户复制**
  - 已存在时同步 name / cron / prompt（模板升级自动生效），但**不动** enabled 与 next_run_at；
    例外是「在清单内却被停用」的预设会被 `enable_system_tasks()` 恢复（见该函数说明）
  """
  import logging

  from service.scheduled_task import repository as repo

  log = logging.getLogger(__name__)
  parks = await discover_parks()
  keys: list[str] = []
  cron_by_key: dict[str, str] = {}
  for ticket in TICKETS:
    # 类型可声明只出全国（`national_only`）：单量太少的类型不做园区级统计
    park_scopes = ([] if ticket.get('national_only')
                   else [(p['slug'], p['name'], p['kingdee']) for p in parks])
    for park, park_name, kingdee in ([(_NATIONWIDE, '全国', '')] + park_scopes):
      # 类型可声明只出哪些周期（`periods` 缺省＝全部）；该类型未声明的周期
      # 由 `disable_stale_system_tasks()` 在启动时停用
      for period in ticket.get('periods', PERIODS):
        key = preset_key(ticket['id'], park, period)
        cron = preset_cron(key)
        try:
          await repo.ensure_system_task(
            key,
            name=preset_name(ticket['id'], park, period, park_name),
            description=f'{ticket["name"]}：{"全国" if park == _NATIONWIDE else park_name}口径的'
                        f'{PERIODS[period]["label"]}深度分析（趋势/类型/时效/重复问题 + 图表），产出 PDF',
            cron_expr=cron,
            prompt=render_prompt(key, park_name=park_name, kingdee=kingdee),
            task_mode='full_analysis',
            thread_strategy='new',
          )
          keys.append(key)
          cron_by_key[key] = cron
        except Exception as e:  # 单个预设失败不该拦住启动
          log.warning('预设报告 %s 初始化失败（跳过）: %s', key, e)

  # 在清单内却被停用的预设 → 恢复。`ensure_system_task()` 刻意不动 enabled，而
  # `disable_stale_system_tasks()` 曾因园区发现失败把整批园区预设停掉（2026-10-25）：
  # 系统预设没有用户侧启停入口，disabled 只可能来自代码清理，所以按清单反向恢复是安全的。
  # 必须在 stale 之前：先恢复在清单内的，再停用不在清单内的。
  if keys:
    try:
      revived = await repo.enable_system_tasks(cron_by_key)
      if revived:
        log.warning('已恢复被停用但在清单内的预设任务 %d 条: %s', len(revived), revived)
    except Exception as e:
      log.warning('恢复停用预设失败（不影响启动）: %s', e)

  # 已下线预设（key 不在本次集合里）→ 停用，否则会继续按旧模板跑出重复报告。
  # 事件统计表预设是另一族（`incident__`），由它自己的 ensure 维护；开关打开时排除，
  # 打开期间不能被这边当「旧预设」停掉（开关关闭时不排除 → 正好被统一停用）。
  exclude: tuple[str, ...] = ()
  try:
    from service.scheduled_task.incident_stats import presets as incident_presets

    if incident_presets.enabled():
      exclude = (incident_presets.PREFIX,)
  except Exception:
    pass
  # 告警不合规检查预设（`compliance__`）同理：它自己的 ensure 维护，开关打开时排除。
  try:
    from service.scheduled_task.compliance import presets as compliance_presets

    if compliance_presets.enabled():
      exclude += (compliance_presets.PREFIX,)
  except Exception:
    pass
  # 文档更新检查预设（`doc_update__`）同理。
  try:
    from service.doc_update import presets as doc_update_presets

    if doc_update_presets.enabled():
      exclude += (doc_update_presets.PREFIX,)
  except Exception:
    pass
  # 清单为空说明代码侧出了岔子（正常情况下至少有全国 9 条）——此时绝不能当成
  # 「园区全部下线」去清库，否则一次异常就停掉全部预设且不会自愈。
  if not keys:
    log.error('预设清单为空，跳过停用检查（不做任何 stale 清理）')
    return keys
  try:
    stale = await repo.disable_stale_system_tasks(keys, exclude_prefixes=exclude)
    if stale:
      log.warning('已停用下线预设任务: %s', stale)
  except Exception as e:
    log.warning('清理下线预设失败（不影响启动）: %s', e)

  # 启动竞态遗留的重复副本 → 每个 key 只留最早一条（不删除，可人工恢复）
  try:
    dup = await repo.disable_duplicate_system_tasks(keys)
    if dup:
      log.warning('已停用 %d 条重复的预设任务: %s', len(dup), dup)
  except Exception as e:
    log.warning('清理重复预设失败（不影响启动）: %s', e)
  return keys
