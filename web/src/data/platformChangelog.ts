export interface ChangelogEntry {
  version: string;
  date: string;
  title: string;
  body: string;
}

const PLATFORM_CHANGELOG: ChangelogEntry[] = [
  {
    version: 'v1.4',
    date: '2026-06-25',
    title: '对话助手预设问答上线',
    body: '内置平台功能介绍、消防配置指南、隐患检测说明等 40+ 预设问答，点击建议问题即可获得即时回复。',
  },
  {
    version: 'v1.3',
    date: '2026-06-20',
    title: '视频生成支持批量任务管理',
    body: '视频生成页面新增统计面板，展示历史记录总数与完成状态；每个任务支持独立下载和删除。',
  },
  {
    version: 'v1.2',
    date: '2026-06-15',
    title: '知识库分组管理上线',
    body: '支持创建多个知识库分组，按业务场景分类管理文档。所有知识库跨用户共享，团队成员上传的文档全员可见。',
  },
  {
    version: 'v1.1',
    date: '2026-06-10',
    title: '消防配置"快速填充"上线',
    body: '内置高层办公、商业综合体、高层住宅、丙类厂房、地下车库 5 种预设模板，一键填入典型建筑参数。',
  },
  {
    version: 'v1.0',
    date: '2026-06-01',
    title: '润泽智能化平台正式发布',
    body: '整合对话助手、知识库管理、隐患识别、消防配置、视频生成、权限中心六大模块，企业 AI 能力一站式平台。',
  },
];

export default PLATFORM_CHANGELOG;
