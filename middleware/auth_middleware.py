"""JWT 认证中间件"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from config.settings import settings
from core.auth_engine import verify_access_token, is_token_blacklisted
from core.rbac import load_role_permissions
from models.user import BUILTIN_PERMISSIONS
from service.auth.user_service import user_service

logger = logging.getLogger(__name__)

# 无需认证的路径
PUBLIC_PATHS = {
    '/', '/api/v1/health',
    '/api/v1/auth/login', '/api/v1/auth/register', '/api/v1/auth/refresh',
    '/api/v1/auth/reset-password', '/api/v1/auth/captcha',
    '/docs', '/openapi.json', '/redoc',
}


class AuthMiddleware(BaseHTTPMiddleware):
    """JWT 认证中间件 — 验证 token, 注入用户上下文"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip('/')

        # 公开路径跳过认证
        if (path in PUBLIC_PATHS
                or path.startswith('/docs')
                or path.startswith('/openapi')
                or path.startswith('/api/v1/video/audio')):
            return await call_next(request)

        # 提取 token（支持 Authorization header 和视频下载的 ?token= query param）
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.removeprefix('Bearer ').strip()
        if not token:
            token = (request.query_params.get('token') or '').strip()
        if not token:
            logger.warning('Auth 拒绝: token 缺失, path=%s, ip=%s',
                         path, request.client.host if request.client else 'unknown')
            return JSONResponse(
                {'ok': False, 'error': '未登录'}, status_code=401
            )

        # 验证 JWT
        try:
            payload = verify_access_token(token)
        except Exception as e:
            logger.warning('Auth 拒绝: token 验证失败, path=%s, error=%s',
                         path, str(e))
            return JSONResponse(
                {'ok': False, 'error': 'token 无效或已过期, 请刷新'}, status_code=401
            )

        # 检查黑名单
        jti = payload.get('jti', '')
        if is_token_blacklisted(jti):
            logger.warning('Auth 拒绝: token 已注销, user_id=%s, path=%s',
                         payload.get('user_id'), path)
            return JSONResponse(
                {'ok': False, 'error': 'token 已注销'}, status_code=401
            )

        # 加载用户
        user = await user_service.get_by_id(payload['user_id'])
        if not user or user.get('status') != 'ACTIVE':
            logger.warning('Auth 拒绝: 用户不存在或已禁用, user_id=%s, path=%s',
                         payload.get('user_id'), path)
            return JSONResponse(
                {'ok': False, 'error': '用户不存在或已禁用'}, status_code=403
            )

        # 加载权限 = 角色权限 + 用户直接权限
        role_ids = user.get('roles', [])
        permissions = await load_role_permissions(role_ids)
        permissions.update(user.get('permissions', []))
        # system:admin 只跟 admin 角色走（兜底，防历史脏数据）
        if 'admin' not in role_ids:
            permissions.discard('system:admin')
        # 权限全开模式 (开发/调试) — 认证用户自动获得所有权限
        if settings.PERMISSION_OPEN_MODE:
            permissions = {p.perm_key for p in BUILTIN_PERMISSIONS}
        else:
            # 展开通配符 (ai:* → ai:chat, ai:knowledge, ...)
            expanded = set()
            all_perm_keys = {p.perm_key for p in BUILTIN_PERMISSIONS}
            for p in permissions:
                if p.endswith(':*'):
                    prefix = p[:-2]
                    expanded.update(k for k in all_perm_keys if k.startswith(prefix))
                else:
                    expanded.add(p)
            permissions = expanded

        # 注入上下文
        request.state.current_user = user
        request.state.user_permissions = permissions
        request.state.user_id = user['user_id']

        logger.debug('Auth 成功: user_id=%s, phone=%s, path=%s',
                    user['user_id'], user.get('phone', ''), path)

        return await call_next(request)
