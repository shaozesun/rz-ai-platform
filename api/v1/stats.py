"""
Dashboard 统计与活动 Feed API
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request

from config.mongodb_conn import mongodb_manager

router = APIRouter()


def _week_range(offset_weeks: int = 0) -> tuple[datetime, datetime]:
  """返回指定周的时间范围。

  offset_weeks=0: 本周（周一 00:00 至当前时刻）
  offset_weeks=-1: 上周（周一 00:00 至周日 23:59）
  """
  now = datetime.utcnow()
  monday = (now - timedelta(days=now.weekday())).replace(
    hour=0, minute=0, second=0, microsecond=0,
  )
  start = monday + timedelta(weeks=offset_weeks)
  if offset_weeks == 0:
    end = now
  else:
    end = (start + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=0)
  return start, end


def _pct_change(this: int, last: int) -> float | None:
  """计算环比百分比，上周为 0 时返回 None"""
  if last <= 0:
    return None
  return round(((this - last) / last) * 100, 1)


@router.get('/stats/overview')
async def get_overview(request: Request):
  """返回仪表盘概览统计（本周数据 + 较上周变化）。"""
  db = mongodb_manager.db
  this_start, this_end = _week_range(0)
  last_start, last_end = _week_range(-1)

  async def _count(collection: str, date_field: str = 'created_at',
                   extra: dict | None = None) -> tuple[int, int]:
    """返回 (本周, 上周) 计数"""
    base = {date_field: {}}
    if extra:
      base.update(extra)
    this_filt = {**base}
    this_filt[date_field] = {'$gte': this_start, '$lte': this_end}
    last_filt = {**base}
    last_filt[date_field] = {'$gte': last_start, '$lte': last_end}
    this_cnt = await db[collection].count_documents(this_filt)
    last_cnt = await db[collection].count_documents(last_filt)
    return this_cnt, last_cnt

  async def _token_sum(start: datetime, end: datetime) -> int:
    """指定范围内 tokens_in + tokens_out 总和"""
    result = await db.llm_usage.aggregate([
      {'$match': {'created_at': {'$gte': start, '$lte': end}}},
      {'$group': {
        '_id': None,
        'total': {'$sum': {'$add': ['$tokens_in', '$tokens_out']}},
      }},
    ]).to_list(1)
    return result[0]['total'] if result else 0

  # 全量统计（不按周）
  user_count = await db.users.count_documents({})
  doc_count = await db.kb_files.count_documents({})

  # 本周 + 上周统计
  msg_t, msg_l = await _count('messages')
  session_t, session_l = await _count('sessions')
  video_t, video_l = await _count('video_tasks')
  risk_t, risk_l = await _count('risk_checks', 'checked_at')
  fire_t, fire_l = await _count('llm_usage', extra={'caller': 'fire_safety'})

  api_call_t = msg_t + video_t + risk_t + fire_t
  api_call_l = msg_l + video_l + risk_l + fire_l

  compute_t = await _token_sum(this_start, this_end)
  compute_l = await _token_sum(last_start, last_end)

  return {
    'ok': True,
    'data': {
      'api_call_count': api_call_t,
      'api_call_change': _pct_change(api_call_t, api_call_l),
      'compute_usage': compute_t,
      'compute_usage_change': _pct_change(compute_t, compute_l),
      'user_count': user_count,
      'session_count': session_t,
      'session_change': _pct_change(session_t, session_l),
      'video_count': video_t,
      'video_change': _pct_change(video_t, video_l),
      'risk_check_count': risk_t,
      'risk_check_change': _pct_change(risk_t, risk_l),
      'fire_safety_count': fire_t,
      'fire_safety_change': _pct_change(fire_t, fire_l),
      'doc_count': doc_count,
    },
  }


@router.get('/stats/activity')
async def get_activity(request: Request, limit: int = Query(5, ge=1, le=50)):
    """返回当前用户的最近活动（视频 + 消防配置），按时间倒序。"""
    user_id = request.state.user_id
    db = mongodb_manager.db
    activities: list[dict] = []

    # 最近视频任务
    video_cursor = db.video_tasks.find({'user_id': user_id}).sort('created_at', -1).limit(limit)
    async for doc in video_cursor:
        name = doc.get('original_filename', '未命名')
        status = doc.get('status', '')
        text = _video_text(name, status)
        activities.append({
            'type': 'video',
            'text': text,
            'time': _fmt(doc.get('created_at')),
        })

    # 按时间倒序，取前 limit 条
    activities.sort(key=lambda a: a['time'], reverse=True)
    return {'ok': True, 'activities': activities[:limit]}


def _fmt(dt):
    if dt is None:
        return ''
    return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)


@router.get('/stats/trend')
async def get_trend(request: Request):
    """返回各功能近 7 天每日调用量。"""
    db = mongodb_manager.db
    now = datetime.utcnow()
    days = [(now - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
    labels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    day_labels = [labels[datetime.strptime(d, '%Y-%m-%d').weekday()] for d in days]

    async def count_by_day(collection: str, date_field: str = 'created_at') -> dict[str, int]:
        pipeline = [
            {'$match': {
                date_field: {
                    '$gte': datetime.strptime(days[0], '%Y-%m-%d'),
                    '$lte': now,
                },
            }},
            {'$group': {
                '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': f'${date_field}'}},
                'count': {'$sum': 1},
            }},
        ]
        result = {}
        async for doc in db[collection].aggregate(pipeline):
            result[doc['_id']] = doc['count']
        return result

    # 各功能每日调用计数
    chat_counts = await count_by_day('messages')
    video_counts = await count_by_day('video_tasks')
    risk_counts = await count_by_day('risk_checks', 'checked_at')

    # 消防配置：从 llm_usage 中 caller=fire_safety 统计
    fire_pipeline = [
        {'$match': {
            'caller': 'fire_safety',
            'created_at': {
                '$gte': datetime.strptime(days[0], '%Y-%m-%d'),
                '$lte': now,
            },
        }},
        {'$group': {
            '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$created_at'}},
            'count': {'$sum': 1},
        }},
    ]
    fire_counts: dict[str, int] = {}
    async for doc in db.llm_usage.aggregate(fire_pipeline):
        fire_counts[doc['_id']] = doc['count']

    trend = []
    for i, d in enumerate(days):
        trend.append({
            'day': day_labels[i],
            'date': d,
            '对话': chat_counts.get(d, 0),
            '视频': video_counts.get(d, 0),
            '隐患识别': risk_counts.get(d, 0),
            '消防配置': fire_counts.get(d, 0),
        })

    return {'ok': True, 'data': trend}


def _video_text(name: str, status: str) -> str:
    labels: dict[str, str] = {
        'pending': f'视频「{name}」排队中',
        'processing': f'视频「{name}」生成中',
        'completed': f'视频「{name}」已完成',
        'failed': f'视频「{name}」生成失败',
    }
    return labels.get(status, f'视频「{name}」{status}')
