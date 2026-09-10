"""飞书 IM 绑定 API（登录态，不加 require_permission）。

任何已登录用户可绑定/查看/解绑自己的飞书账号。绑定码经一次性码流转，最终由
飞书侧 `/connect <code>` 或卡片输入框消费，把 open_id 写回当前用户。
"""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from service.auth.user_service import user_service
from service.feishu import binding

router = APIRouter(prefix='/feishu', tags=['feishu'])


@router.get('/status')
async def feishu_status(request: Request):
  """查看当前用户飞书绑定状态。"""
  user = await user_service.get_by_id(request.state.user_id)
  if not user:
    raise HTTPException(404, '用户不存在')
  return {
    'ok': True,
    'data': {
      'lark_bound': bool(user.get('lark_open_id', '')),
      'lark_name': user.get('lark_name', ''),
    },
  }


@router.post('/connect')
async def feishu_connect(request: Request):
  """生成一次性绑定码，用户到飞书发 `/connect <code>` 或卡片输入框提交。"""
  user_id = request.state.user_id
  code = binding.create_connect_code(user_id)
  return {
    'ok': True,
    'data': {'code': code, 'instruction': f'/connect {code}'},
  }


@router.post('/unbind')
async def feishu_unbind(request: Request):
  """解绑当前用户的飞书账号。"""
  user_id = request.state.user_id
  await binding.unbind(user_id)
  return {'ok': True, 'message': '已解绑'}


@router.get('/artifacts/{token}')
async def feishu_artifact(token: str):
  """飞书产物下载（公开，签名 token 即授权，TTL 内可多次下载）。"""
  from service.feishu import artifacts

  info = artifacts.resolve_download_token(token)
  if not info:
    raise HTTPException(404, '下载链接无效或已过期')

  thread_id = info['thread_id']
  dataset_id = info['dataset_id']
  filename = Path(info['filename']).name

  from core.sandbox import datasets as sandbox_datasets

  ds_dir = None
  if not sandbox_datasets._SAFE_RE.search(dataset_id):
    try:
      ds_dir = sandbox_datasets.load_dataset_dir(thread_id, dataset_id)
    except sandbox_datasets.DatasetError:
      ds_dir = None
  if ds_dir is None:
    ds_dir = sandbox_datasets.find_artifact_dir(thread_id, filename)
  if ds_dir is None:
    raise HTTPException(404, '产物不存在')

  file_path = (ds_dir / filename).resolve()
  if not file_path.is_relative_to(ds_dir.resolve()) or not file_path.is_file():
    raise HTTPException(404, '产物不存在')

  from api.v1.agent import _MEDIA_TYPES, _ACTIVE_CONTENT_TYPES

  media_type = _MEDIA_TYPES.get(file_path.suffix.lower(), 'application/octet-stream')
  disposition = 'attachment' if media_type in _ACTIVE_CONTENT_TYPES else 'inline'
  encoded = quote(file_path.name)
  return FileResponse(
    file_path,
    media_type=media_type,
    headers={'Content-Disposition': f"{disposition}; filename*=UTF-8''{encoded}"},
  )
