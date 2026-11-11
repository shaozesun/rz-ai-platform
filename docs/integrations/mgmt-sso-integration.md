# 综合管理平台账号打通：SSO 登录 + 人员信息同步

> 日期：2026-08-27 | 开关：`MGMT_SSO_ENABLED`（默认关）

## 目标

1. **mgmt 账号直接登录 rz**（类三方 SSO），登录即自动开启「认证中心」的 mgmt 授权绑定（Agent 查询按本人身份走，免手动绑定）。
2. **非 mgmt 登录的 rz 用户**，注册时用「手机号 + 默认密码 666666」走 mgmt 接口拉一次人员档案（姓名/公司/**部门**）持久化，供 **审批管理 / 用户管理** 展示。

## 设计

### Path A — mgmt SSO 登录

`POST /api/v1/auth/mgmt-login`（`MGMT_SSO_ENABLED` 开启可用，走图形验证码）：
1. `mgmt_http._login_with_credentials(username, password)` 验证 + 拿档案（登录响应自带 name/occupation/own_dept_name/company_name/uid）
2. find-or-create rz 用户：**只按手机号匹配**（综合管理平台账号均为手机号，同人同号合并）；不存在则自动创建（status=ACTIVE + 默认角色 `MGMT_SSO_DEFAULT_ROLE`=dc_operator）
3. 自动开启认证中心授权：存 `mgmt_username` + `mgmt_password_enc`（Fernet）；已有 PENDING 用户激活（mgmt 登录证明身份），DISABLED 拒绝
4. 完善档案（apply_mgmt_profile 回写 name/company/dept_name/mgmt_uid）
5. `mgmt_http.cache_user_login` 缓存 token/档案上下文（免二次登录，Agent 按本人身份查平台）
6. 发 rz JWT，返回与 `/auth/login` 同结构

### Path B — 注册时一次性档案同步

`mgmt_http.fetch_mgmt_profile(phone)`：用「手机号 + `MGMT_DEFAULT_PASSWORD`」登录拉档案，**任何失败返回空 dict（不抛错）**。
- 触发点：`auth_service.register` 创建 PENDING 用户后，`asyncio.create_task(user_service.enrich_user_from_mgmt(...))` 后台持久化
- 取不到（默认密码已改）→ 静默跳过，注册照常
- **只在注册时拉一次；后续 rz 登录不调用 mgmt 接口**
- 要刷新档案 → 走 Path A（mgmt SSO 登录）

## 数据模型

- `User` + `mgmt_uid` / `dept_name`
- `UserPublic` / `get_user_public` + 这三个字段
- 审批列表 `list_applications` 按 user_id join 申请人档案（dept_name）

## 文件

**后端**
- `models/user.py` — User/UserPublic + 2 字段（mgmt_uid/dept_name）
- `config/settings.py` — `MGMT_SSO_ENABLED` / `MGMT_SSO_DEFAULT_ROLE`
- `service/query/mgmt/http.py` — `_login_with_credentials`；新增 `cache_user_login` / `fetch_mgmt_profile`
- `service/auth/user_service.py` — `create_user_from_mgmt` / `apply_mgmt_profile` / `enrich_user_from_mgmt`；`list_applications` join 档案；`get_user_public` 带出档案
- `service/auth/auth_service.py` — `mgmt_login()`；`register` 触发后台档案同步
- `api/v1/auth.py` — `POST /mgmt-login`

**前端**
- `web/src/api/auth.ts` — `mgmtLogin()`
- `web/src/pages/LoginPage.tsx` — 底部三态切换「手机号登录 / 综合管理平台登录 / 注册」，mgmt 模式输 账号+密码+验证码
- `web/src/types/index.ts` — AdminUser / Application + 档案字段
- `web/src/pages/admin/UsersPage.tsx` / `ApprovalsPage.tsx` — 展示 部门/专业

## 验证

1. `MGMT_SSO_ENABLED=true` 后，登录页切「综合管理平台登录」→ mgmt 账号+666666 → 登录成功，`/auth/me` 含 dept_name；认证中心显示已绑定
2. Agent 对话问「我的账号信息」→ 返回本人姓名/部门
3. 新用户注册 → 注册时拉到档案 → 审批列表/用户管理显示 部门（持久化）
4. mgmt 密码非 666666 的账号注册 → 拉取失败静默跳过，注册照常 PENDING
5. 注册后再次 rz 登录 → 不调 mgmt 接口，档案保持注册时定格
