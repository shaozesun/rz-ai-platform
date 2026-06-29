"""
风险检测相关数据模型
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ViolationItem(BaseModel):
  """单项违规/隐患"""
  category: str = Field(default='', description='违规类别')
  description: str = Field(default='', description='违规行为描述')
  regulation: str = Field(default='', description='依据的国家法规条文')
  suggestion: str = Field(default='', description='整改建议')


class HazardItem(BaseModel):
  """单个隐患项（简化兼容，保留原有字段）"""
  category: str = Field(default='', description='隐患类别：电气/消防/结构/环境/操作')
  severity: str = Field(default='', description='严重程度：高/中/低')
  location: str = Field(default='', description='隐患位置描述')
  description: str = Field(default='', description='隐患描述')
  recommendation: str = Field(default='', description='整改建议')
  reference: str = Field(default='', description='参考规范/标准')


class CheckResult(BaseModel):
  """单图检测结果"""
  ok: bool = True
  check_id: str = Field(default='', description='检测 ID')
  image_name: str = Field(default='', description='图片文件名')
  cabinet_type: Optional[str] = Field(default=None, description='机柜类型，非机柜为 null')
  has_risk: Optional[bool] = Field(default=None, description='是否存在风险')
  risk_level: str = Field(default='unknown', description='风险等级：none/low/medium/high/critical/unknown')
  risk_categories: list[str] = Field(default_factory=list, description='风险类别列表')
  description: str = Field(default='', description='总体评估描述')
  suggestion: str = Field(default='', description='总体整改建议')
  violations: list[ViolationItem] = Field(default_factory=list, description='违规项列表')
  hazards: list[HazardItem] = Field(default_factory=list, description='隐患列表（兼容旧字段）')
  confidence: float = Field(default=0.0, description='置信度 0-1')
  summary: str = Field(default='', description='检测摘要')
  error: str = Field(default='', description='错误信息')
  trace_id: str = Field(default='', description='请求追踪 ID')
  checked_at: datetime = Field(default_factory=datetime.now)


class BatchCheckResult(BaseModel):
  """批量检测结果"""
  ok: bool = True
  total: int = Field(default=0, description='图片总数')
  results: list[CheckResult] = Field(default_factory=list)
  summary: str = Field(default='', description='批次汇总')
  task_id: str = Field(default='', description='异步任务 ID')
  status: str = Field(default='done', description='任务状态')


# ==================== 消防配置推荐 ====================

class FireFacilityItem(BaseModel):
  """消防设施推荐项"""
  category: str = Field(default='', description='设施类别：灭火器/消火栓/自动喷水灭火/火灾报警/防排烟/应急照明/其他')
  name: str = Field(default='', description='设施名称')
  specification: str = Field(default='', description='规格型号要求')
  quantity_or_coverage: str = Field(default='', description='数量或配置要求')
  installation_location: str = Field(default='', description='安装位置建议')
  regulation_ref: str = Field(default='', description='依据的法规条文')
  priority: str = Field(default='mandatory', description='mandatory/recommended/optional')


class BuildingMaterialItem(BaseModel):
  """建筑材料推荐项"""
  category: str = Field(default='', description='材料类别：防火分隔/防火涂料/防火门窗/防火封堵/耐火结构/保温材料/其他')
  name: str = Field(default='', description='材料名称')
  specification: str = Field(default='', description='规格要求')
  application_location: str = Field(default='', description='应用部位')
  regulation_ref: str = Field(default='', description='依据的法规条文')
  priority: str = Field(default='mandatory', description='mandatory/recommended/optional')


class SafetyPreparationItem(BaseModel):
  """消防安全管理建议项"""
  category: str = Field(default='', description='建议类别：疏散与逃生/标识与照明/消防管理/应急预案/培训演练/其他')
  name: str = Field(default='', description='建议事项名称')
  requirement: str = Field(default='', description='具体要求描述')
  regulation_ref: str = Field(default='', description='依据的法规条文')
  priority: str = Field(default='mandatory', description='mandatory/recommended/optional')


class FireSafetyRequest(BaseModel):
  """消防配置推荐请求"""
  building_type: str = Field(
    default='',
    description='建筑类型：住宅建筑/办公建筑/商业建筑/旅馆建筑/餐饮建筑/教育建筑/医疗建筑/'
    '老年人照料设施/幼儿园/托儿所/体育场馆/交通枢纽/博物馆/展览馆/图书馆/档案馆/'
    '数据中心/娱乐场所/丙类厂房/丁戊类厂房/甲/乙类厂房/洁净厂房/物流仓库/丙类仓库/'
    '甲/乙类仓库/冷库/汽车库/地下建筑/其他',
  )
  building_height: float = Field(default=0, ge=0, description='建筑高度（米）')
  floor_count_above: int = Field(default=0, ge=0, description='地上层数')
  floor_count_below: int = Field(default=0, ge=0, description='地下层数')
  building_area: float = Field(default=0, gt=0, description='总建筑面积（平方米）')
  fire_resistance_rating: str = Field(
    default='二级',
    description='耐火等级：一级/二级/三级/四级',
  )
  structural_form: str = Field(default='', description='结构形式：钢筋混凝土框架结构/钢结构等')
  fire_hazard_category: str = Field(
    default='',
    description='火灾危险性类别（工业建筑必填）：甲/乙/丙/丁/戊',
  )
  occupancy_count: int = Field(default=0, ge=0, description='预计最大容纳人数，0 表示不确定')
  has_sprinkler_system: bool = Field(default=False, description='是否已安装自动喷水灭火系统')
  has_alarm_system: bool = Field(default=False, description='是否已安装火灾自动报警系统')
  has_hydrant_system: bool = Field(default=False, description='是否已安装室内消火栓系统')
  construction_status: str = Field(default='新建', description='建设状态：新建/改建/扩建/既有建筑')
  additional_notes: str = Field(default='', description='用户补充说明或特殊要求')

  # 兼容旧字段
  room_type: str = Field(default='', description='房间类型（简化模式，兼容旧接口）')
  area: float = Field(default=0, description='面积（简化模式）')
  height: float = Field(default=0, description='层高（简化模式）')
  equipment: str = Field(default='', description='主要设备（简化模式）')
  special_requirements: str = Field(default='', description='特殊要求（简化模式）')


class FireSafetyItem(BaseModel):
  """消防配置项（简化兼容）"""
  name: str = Field(default='', description='设备/系统名称')
  quantity: str = Field(default='', description='建议数量')
  specification: str = Field(default='', description='规格要求')
  location: str = Field(default='', description='安装位置建议')
  reason: str = Field(default='', description='配置依据')


class FireSafetyResult(BaseModel):
  """消防配置推荐结果"""
  ok: bool = True
  building_type: str = Field(default='')
  building_height: float = Field(default=0.0)
  building_area: float = Field(default=0.0)
  structural_form: str = Field(default='')
  risk_level: str = Field(default='unknown', description='风险等级')
  summary: str = Field(default='', description='总体评估')
  fire_facilities: list[FireFacilityItem] = Field(default_factory=list, description='消防设施配置')
  building_materials: list[BuildingMaterialItem] = Field(default_factory=list, description='建筑材料要求')
  safety_preparations: list[SafetyPreparationItem] = Field(default_factory=list, description='安全管理建议')
  applicable_standards: list[str] = Field(default_factory=list, description='适用标准列表')
  recommendations: list[FireSafetyItem] = Field(default_factory=list, description='推荐配置（简化兼容）')
  room_type: str = Field(default='')
  notes: str = Field(default='', description='注意事项')
  regulation_refs: str = Field(default='', description='参考规范')
  error: str = Field(default='')
  trace_id: str = Field(default='')


class ReportRequest(BaseModel):
  """报告生成请求"""
  check_ids: list[str] = Field(default_factory=list, description='检测 ID 列表')
  title: str = Field(default='', description='报告标题')
  results: list[CheckResult] = Field(default_factory=list, description='检测结果列表')
  format: str = Field(default='json', description='输出格式: json / md / docx')
