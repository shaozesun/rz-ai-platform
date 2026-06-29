"""
Dashboard 统计与活动 Feed API
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request

from config.mongodb_conn import mongodb_manager

router = APIRouter()


@router.get('/stats/overview')
async def get_overview(request: Request):
    """返回仪表盘概览统计（全局聚合）。"""
    db = mongodb_manager.db
    user_count = await db.users.count_documents({})
    session_count = await db.sessions.count_documents({})
    video_count = await db.video_tasks.count_documents({})
    group_count = await db.knowledge_groups.count_documents({})
    risk_check_count = await db.risk_checks.count_documents({})
    fire_safety_count = await db.llm_usage.count_documents({'caller': 'fire_safety'})

    # API 调用总量（messages + video_tasks + risk_checks + fire_safety）
    api_call_count = (
        await db.messages.count_documents({})
        + video_count
        + risk_check_count
        + fire_safety_count
    )

    # 算力消耗（累计 Token 消耗量）
    pipeline = [
        {'$group': {
            '_id': None,
            'total': {'$sum': {'$add': ['$tokens_in', '$tokens_out']}},
        }}
    ]
    result = await db.llm_usage.aggregate(pipeline).to_list(1)
    compute_usage = result[0]['total'] if result else 0

    return {
        'ok': True,
        'data': {
            'api_call_count': api_call_count,
            'compute_usage': compute_usage,
            'user_count': user_count,
            'session_count': session_count,
            'video_count': video_count,
            'group_count': group_count,
            'risk_check_count': risk_check_count,
            'fire_safety_count': fire_safety_count,
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
