"""
风险检测 API 端点：图片安全隐患检测、消防配置推荐、报告生成
"""
from __future__ import annotations

import json
import logging
import tempfile

from config.trace_id import get_trace_id
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, File, UploadFile, Body, Request, Query, Form
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from core.rbac import require_permission
from service.risk.detection_service import check_image, check_images_batch
from service.risk.history_service import (
  save_check_result,
  get_user_history,
  get_check_detail,
  delete_check_record,
  save_fire_safety_result,
  get_fire_safety_history,
  get_fire_safety_detail,
  delete_fire_safety_record,
)
from service.risk.fire_safety_service import recommend
from service.risk.report_service import (
  generate_markdown_report,
  generate_word_report,
  generate_word_report_filename,
  generate_fire_safety_word_report,
  generate_fire_safety_word_report_filename,
)
from models.risk.schemas import (
  CheckResult,
  FireSafetyRequest,
  FireSafetyResult,
  ReportRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/risk', tags=['risk'])

ALLOWED_MIME = {
  'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp',
}


@router.post('/check')
@require_permission('ai:risk')
async def risk_check(
  request: Request,
  file: UploadFile = File(...),
  description: str = Query('', description='场景补充描述'),
):
  """单张图片安全隐患检测（两阶段流水线）"""
  if not file.filename:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '文件名为空'})

  suffix = Path(file.filename).suffix.lower()
  if suffix not in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
    return JSONResponse(
      status_code=400,
      content={'ok': False, 'msg': f'不支持的图片格式: {suffix}'},
    )

  if file.content_type and file.content_type not in ALLOWED_MIME:
    return JSONResponse(
      status_code=400,
      content={'ok': False, 'msg': f'不支持的文件类型: {file.content_type}'},
    )

  content = await file.read()
  if len(content) == 0:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '文件为空'})

  user_id = request.state.user_id
  media_type = file.content_type or 'image/jpeg'
  logger.info('[RiskCheck] 单图检测: user_id=%s file=%s size=%.1fKB',
              user_id, file.filename, len(content) / 1024)

  try:
    result = await check_image(
      image_bytes=content,
      image_name=file.filename,
      media_type=media_type,
      user_description=description,
    )
    result.trace_id = get_trace_id()
    # 持久化到数据库
    if result.ok and result.check_id:
      try:
        await save_check_result(user_id, result, content)
      except Exception:
        logger.warning('[RiskCheck] 保存结果失败', exc_info=True)
    return result
  except Exception as e:
    logger.error('[RiskCheck] 检测失败: %s', e)
    return JSONResponse(
      status_code=500,
      content={'ok': False, 'msg': '检测失败', 'detail': str(e)},
    )


@router.post('/batch-check')
@require_permission('ai:risk')
async def risk_batch_check(
  request: Request,
  files: list[UploadFile] = File(...),
  description: str = Query('', description='场景补充描述'),
):
  """批量图片安全隐患检测"""
  if not files:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '至少上传一张图片'})
  if len(files) > 20:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '单次最多上传 20 张图片'})

  user_id = request.state.user_id
  logger.info('[RiskBatchCheck] 批量检测: user_id=%s count=%d', user_id, len(files))

  image_data_list: list[tuple[bytes, str, str]] = []
  for f in files:
    content = await f.read()
    if len(content) == 0:
      continue
    image_data_list.append((content, f.filename or 'unknown', f.content_type or 'image/jpeg'))

  if not image_data_list:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '没有有效的图片'})

  try:
    result = await check_images_batch(image_data_list, user_description=description)
    # 持久化到数据库
    for i, r in enumerate(result.results):
      if r.ok and r.check_id and i < len(image_data_list):
        try:
          await save_check_result(user_id, r, image_data_list[i][0])
        except Exception:
          logger.warning('[RiskBatchCheck] 保存结果失败 idx=%d', i, exc_info=True)
    return result
  except Exception as e:
    logger.error('[RiskBatchCheck] 批量检测失败: %s', e)
    return JSONResponse(
      status_code=500,
      content={'ok': False, 'msg': '批量检测失败', 'detail': str(e)},
    )


@router.post('/fire-safety')
@require_permission('ai:fire_safety')
async def fire_safety_recommend(
  request: Request,
  body: FireSafetyRequest = Body(...),
):
  """消防配置推荐"""
  user_id = request.state.user_id
  logger.info('[FireSafety] 请求: user_id=%s building_type=%s height=%.1f area=%.0f',
              user_id, body.building_type or body.room_type,
              body.building_height or body.height,
              body.building_area or body.area)
  result = await recommend(body)
  result.trace_id = get_trace_id()
  # 持久化到数据库
  if result.ok:
    try:
      await save_fire_safety_result(user_id, body, result)
    except Exception:
      logger.warning('[FireSafety] 保存结果失败', exc_info=True)
  return result


@router.post('/report')
@require_permission('ai:risk')
async def generate_report(
  request: Request,
  body: ReportRequest = Body(None),
  data: str = Form(None),
):
  """根据检测结果生成报告（支持 Markdown 和 Word 格式）"""
  if body is None and data:
    body = ReportRequest(**json.loads(data))
  if body is None:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '请提供检测结果'})
  user_id = request.state.user_id
  logger.info('[RiskReport] 生成报告: user_id=%s check_ids=%s format=%s',
              user_id, body.check_ids, body.format)

  results: list[CheckResult] = []
  if body.results:
    results = body.results
    for i, r in enumerate(results):
      if not r.check_id:
        r.check_id = body.check_ids[i] if i < len(body.check_ids) else ''

  if not results:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '无可用于生成报告的检测结果'})

  if body.format == 'docx':
    doc_bytes = generate_word_report(results, title=body.title)
    filename = generate_word_report_filename(body.title)
    return Response(
      content=doc_bytes,
      media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      headers={
        'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}",
      },
    )

  if body.format == 'md':
    markdown = generate_markdown_report(results, title=body.title)
    return PlainTextResponse(markdown, media_type='text/markdown; charset=utf-8')

  return {'ok': True, 'markdown': generate_markdown_report(results, title=body.title)}


@router.post('/fire-safety/report')
@require_permission('ai:fire_safety')
async def download_fire_safety_report(
  request: Request,
  data: str = Form(...),
):
  """接收消防配置推荐结果 JSON，返回 Word 文档"""
  user_id = request.state.user_id
  logger.info('[FireSafetyReport] 生成消防报告: user_id=%s', user_id)

  try:
    raw = json.loads(data)
    result = FireSafetyResult(**raw)
    doc_bytes = generate_fire_safety_word_report(result)
    filename = generate_fire_safety_word_report_filename()
    return Response(
      content=doc_bytes,
      media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      headers={
        'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}",
      },
    )
  except Exception as e:
    logger.error('[FireSafetyReport] 生成失败: %s', e)
    return JSONResponse(
      status_code=500,
      content={'ok': False, 'msg': '报告生成失败', 'detail': str(e)},
    )


@router.get('/history')
@require_permission('ai:risk')
async def risk_history(
  request: Request,
  limit: int = Query(10, ge=1, le=50),
  offset: int = Query(0, ge=0),
):
  """获取用户检测历史列表"""
  user_id = request.state.user_id
  records = await get_user_history(user_id, limit=limit, offset=offset)
  return {'ok': True, 'records': records}


@router.get('/history/{check_id}')
@require_permission('ai:risk')
async def risk_history_detail(
  request: Request,
  check_id: str,
):
  """获取单条检测详情"""
  user_id = request.state.user_id
  detail = await get_check_detail(user_id, check_id)
  if not detail:
    return JSONResponse(status_code=404, content={'ok': False, 'msg': '记录不存在'})
  return {'ok': True, 'detail': detail}


@router.delete('/history/{check_id}')
@require_permission('ai:risk')
async def risk_history_delete(
  request: Request,
  check_id: str,
):
  """删除检测记录"""
  user_id = request.state.user_id
  deleted = await delete_check_record(user_id, check_id)
  if not deleted:
    return JSONResponse(status_code=404, content={'ok': False, 'msg': '记录不存在或已删除'})
  return {'ok': True}


@router.get('/fire-safety/history')
@require_permission('ai:fire_safety')
async def fire_safety_history(
  request: Request,
  limit: int = Query(10, ge=1, le=50),
  offset: int = Query(0, ge=0),
):
  """获取用户消防推荐历史列表"""
  user_id = request.state.user_id
  records = await get_fire_safety_history(user_id, limit=limit, offset=offset)
  return {'ok': True, 'records': records}


@router.get('/fire-safety/history/{record_id}')
@require_permission('ai:fire_safety')
async def fire_safety_history_detail(
  request: Request,
  record_id: str,
):
  """获取单条消防推荐详情"""
  user_id = request.state.user_id
  detail = await get_fire_safety_detail(user_id, record_id)
  if not detail:
    return JSONResponse(status_code=404, content={'ok': False, 'msg': '记录不存在'})
  return {'ok': True, 'detail': detail}


@router.delete('/fire-safety/history/{record_id}')
@require_permission('ai:fire_safety')
async def fire_safety_history_delete(
  request: Request,
  record_id: str,
):
  """删除消防推荐记录"""
  user_id = request.state.user_id
  deleted = await delete_fire_safety_record(user_id, record_id)
  if not deleted:
    return JSONResponse(status_code=404, content={'ok': False, 'msg': '记录不存在或已删除'})
  return {'ok': True}
