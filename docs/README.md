# docs 索引

按用途分目录。新增文档请放进对应目录；事故复盘一律进 `problems/<域>/`。

## architecture/ — 平台与 Agent 架构（长期有效）

| 文档 | 内容 |
|---|---|
| [agent-architecture-design-v2.md](architecture/agent-architecture-design-v2.md) | Agent 系统架构设计 v2.0（图、中间件、工具集、记忆） |
| [capability-catalog-scaling-design.md](architecture/capability-catalog-scaling-design.md) | 能力注册与工具编排：四层分类 + 工具规模收敛 |
| [platform-capabilities.md](architecture/platform-capabilities.md) | 全部平台能力清单（mgmt / dcim_meta / web / browser / skills） |
| [agent-tool-center.md](architecture/agent-tool-center.md) | 工具目录小窗：分组、可见性、与部署开关的关系 |
| [agent-skill-management.md](architecture/agent-skill-management.md) | 技能中心：列表 / 详情 / 启停 / 删除 |

## dcim/ — 动环数据接入全链路

| 文档 | 内容 |
|---|---|
| [dcim-kafka-ingest-design.md](dcim/dcim-kafka-ingest-design.md) | Kafka 接入设计：配置类数据的解析、幂等落库、DLQ |
| [dcim-timeseries-storage-design.md](dcim/dcim-timeseries-storage-design.md) | 时序地基：宽表、4,096 列拆组、子表命名、跨库查询 |
| [dcim-pue-storage-query.md](dcim/dcim-pue-storage-query.md) | PUE / 能耗统计：独立超级表 `st_pue` 与查询口径 |
| [dcim-kafka-deploy-manual.md](dcim/dcim-kafka-deploy-manual.md) | 生产部署手册（Kafka / TDengine / ingest worker） |
| [dcim-kafka-relay-network.md](dcim/dcim-kafka-relay-network.md) | 中转网络方案与现实拓扑 |
| [dcim-kafka-待确认清单.md](dcim/dcim-kafka-待确认清单.md) | 与对方待对齐口径（部分项仍未关闭） |

## features/ — 单个功能模块设计

| 文档 | 内容 |
|---|---|
| [browser-capture.md](features/browser-capture.md) | 内网页面操作平台：配方截图 + 交互浏览 + 旁路容器 |
| [compliance-alarm-check.md](features/compliance-alarm-check.md) | 告警不合规检查：班次窗口、四项口径、Excel 产物 |
| [doc-update-check.md](features/doc-update-check.md) | 月度文档更新检查：各公司部门 × 25 项、内容哈希判据 |
| [event-report-alarm-link.md](features/event-report-alarm-link.md) | 事件报告关联 DCIM 告警：匹配口径、告警值、四档阈值 |
| [feishu-group-realtime-ingest.md](features/feishu-group-realtime-ingest.md) | 飞书群消息实时入库：WS 推送、游标补偿、撤回、HA |
| [infrared-quality-check.md](features/infrared-quality-check.md) | 红外巡检图片质量检测：勾选任务、Gradio 推理、缓存 |
| [wechat-channel-design.md](features/wechat-channel-design.md) | 微信 IM 通道（个人微信 iLink）：协议约束与接入 |

## integrations/ — 外部系统对接

| 文档 | 内容 |
|---|---|
| [mgmt-sso-integration.md](integrations/mgmt-sso-integration.md) | 综合管理平台账号打通：SSO 登录 + 人员档案同步 |
| [数据中心综合管理平台接口文档.md](integrations/数据中心综合管理平台接口文档.md) | 平台方提供的接口原文（大文件，按需检索） |
| [综合管理平台接口参数映射确认清单.md](integrations/综合管理平台接口参数映射确认清单.md) | 请求参数 snake_case ↔ 接口 camelCase 映射 |
| [field-mapping/工单列表接口字段映射确认清单.md](integrations/field-mapping/工单列表接口字段映射确认清单.md) | 工单列表返回字段与枚举码表（`ticket_codes.py` 依据） |
| [field-mapping/综合管理平台列表接口字段映射确认清单2.md](integrations/field-mapping/综合管理平台列表接口字段映射确认清单2.md) | 文档/巡检等列表接口字段映射 |

## operations/ — 部署与运维手册

| 文档 | 内容 |
|---|---|
| [tts-vllm-omni-deploy-manual.md](operations/tts-vllm-omni-deploy-manual.md) | Qwen3-TTS vLLM-Omni 部署（:8091）与并发配额 |
| [mage-vl-usage-manual.md](operations/mage-vl-usage-manual.md) | Mage-VL 视觉模型接口使用说明 |

## problems/ — 事故复盘与根因分析（按域）

| 目录 | 主题 |
|---|---|
| [agent/](problems/agent/) | Agent 链路：会话存储、Langfuse 空 trace、分析可靠性、历史问题 |
| [report/](problems/report/) | 确定性报告：跨期比较、机构名、语气偏置、正面评价、批量失败、预设停用、部门维度 |
| [dcim/](problems/dcim/) | DCIM 接入性能：GIL 多进程、幽灵列卡死、RAID check、写入成本实验 |
| [feishu/](problems/feishu/) | 飞书实时入库的 HA 取舍 |
| [incident/](problems/incident/) | 事件统计表空窗口、事件 ↔ 告警判定表 |
| [platform/](problems/platform/) | 工单接口迁移、红外检测失效、系统排查问题记录 |

> 复盘文档命名保持「领域前缀 + 主题」，移动子目录时**不改文件名**，减少引用漂移。
