# 工单接口整体迁移到 ticket-api（2026-10-26）

> 现象：2026-10-26 起老 `https://api.ddbes.com/ticket/wiporder/*` 全站返回
> `{"code":-1,"msg":"服务器开小差了"}`，24 份预设周报全灭。
> 结论：**对方把工单服务整体重做了**，迁移到 `https://ticket-api.ddbes.com`，
> 认证换成 mgmt 同一套 `token` + `Deptid`。入参与返回信封都没变。

---

## 一、新老对照（实测）

| 项 | 老 | 新 |
|---|---|---|
| base | `https://api.ddbes.com` | `https://ticket-api.ddbes.com` |
| 列表路径 | `/ticket/wiporder/ticketlist/*` | `/wiporder/ticketlist/*` |
| 详情路径 | `/ticket/wiporder/{query/breakdown,event,question,threshold,supplier}/{id}` | `/wiporder/{...}/{id}` |
| 我的待办 | `/pen/pending/list/v1` | `/pending/list/v1` |
| 公司部门 | `/ticket/company/user/{kingdeeUid}` | `/company/user/{kingdeeUid}` |
| 认证 | `Authorization: Bearer <人员 token>` | `token: <mgmt token>` + `Deptid: <dept>` |
| 入参 | `{companyId, type, page, size, ...}` | **不变** |
| 信封 | `{code:1, data:{simpleTicketVOList,total}}` | **不变** |

## 二、实测数据（2026-10-26，廊坊园区，10-19~10-25）

| 能力 | 结果 |
|---|---|
| 变更列表 `/wiporder/ticketlist/modify` | `code:1`，total=282 |
| 事件列表 `/wiporder/ticketlist/event` | `code:1`，total=69 |
| 故障列表 `/wiporder/ticketlist/breakdown` | `code:1`，total=23 |
| 阈值列表 `/wiporder/ticketlist/threshold` | `code:1`，total=36 |
| 变更统计 `/wiporder/ticketlist/statistics/modify` | `code:1`，total=281 |
| 详情（变更/事件/故障/阈值） | `code:1`，字段齐全（`breakdownEquipments` 在） |
| 全国口径（`all_companies`） | total=481，5 家国内公司聚合正常 |
| 公司部门 `/company/user/{uid}` | 6 家公司 |
| 我的待办 `/pending/list/v1` | `code:1`（服务账号与测试用户均无待办，total=0） |

## 三、代码改动

1. `config/settings.py` 新增 `MGMT_TICKET_BASE_URL`（默认 `https://ticket-api.ddbes.com`）；
   `MGMT_PEN_BASE_URL` 保留不动。
2. `service/query/mgmt/http.py` 新增 `ticket_request()`：复用 `request()`（`token`+`Deptid`
   头、401 清缓存重登一次），`json_body` 有值走 POST，否则 GET。
3. `service/query/mgmt/client.py`：所有 `pen_request('/ticket/wiporder/...')` →
   `ticket_request('/wiporder/...')`；待办 → `/pending/list/v1`；公司部门 → `/company/user/{uid}`。
   `pen_request` 定义保留（不再被调用），按「不删已有业务代码」原则未移除。
4. 参数与统计口径**零改动**（新服务仍默认 `type:1`）。

## 四、踩过的坑

- **不能只换 base**：路径同时去掉了 `/ticket` 前缀，认证也不再是 Bearer 人员 token。
- **`type` 必填**：漏传会在新服务里报 `NullPointerException`（老代码默认带 `type:1`，所以没暴露）。
- **mgmt token 有 TTL**：直接用 Redis 里的 token 连续探测会中途 401；
  正常代码路径由 `request()` 的「401 → 清缓存重登一次」兜底。
- **`supplier` 列表**：带 `companyId + startTime/endTime` 时新服务报 NPE，暂未确认正确入参；
  供应商报告仍走 agent 路径，不影响五类确定性报告。

## 五、未确认项

- `/pending/list/inspection/v1`（巡检工单）是否也迁移，需对方确认。
- `supplier` 列表在新区间的正确入参。
- 供应商服务工单（`query_supplier_ticket`）是否为独立服务。

## 六、验证

```bash
python -m pyflakes service/query/mgmt config/settings.py 2>&1 | grep "undefined name"
python -m service.scheduled_task.report.tests.test_event_compute   # 其余四类同理

# 线上冒烟：五类列表 + 统计 + 详情 + 全国聚合（见本次执行记录）
# 单跑一份园区报告 → 状态 success、period_key=2026-W43、产物 PDF 已落盘
```
