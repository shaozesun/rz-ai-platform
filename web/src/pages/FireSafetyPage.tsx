import { useState } from 'react';
import { Building2, Shield, Flame, HardHat, ClipboardList, BookOpen, AlertTriangle, CheckCircle, FileText, Download, Home, ShoppingCart, Car, Wrench, Banknote, GraduationCap, Zap, Bell, Sprout } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { fireSafetyRecommend, downloadFireSafetyReport } from '@/api/risk';
import type { FireSafetyResult } from '@/types';

const BUILDING_TYPES = [
  '住宅建筑', '办公建筑', '商业建筑', '旅馆建筑', '餐饮建筑',
  '教育建筑', '医疗建筑', '老年人照料设施', '幼儿园/托儿所',
  '体育场馆', '交通枢纽', '博物馆/展览馆', '图书馆/档案馆',
  '数据中心', '娱乐场所', '丙类厂房', '丁戊类厂房',
  '甲/乙类厂房', '洁净厂房', '物流仓库', '丙类仓库',
  '甲/乙类仓库', '冷库', '汽车库', '地下建筑', '其他',
];

const FIRE_RESISTANCE = ['一级', '二级', '三级', '四级'];
const HAZARD_CATEGORIES = ['', '甲', '乙', '丙', '丁', '戊'];
const CONSTRUCTION_STATUS = ['新建', '改建', '扩建', '既有建筑'];
const STRUCTURAL_FORMS = [
  '钢筋混凝土框架结构', '钢筋混凝土剪力墙结构',
  '钢筋混凝土框架-剪力墙结构', '钢筋混凝土框架-核心筒结构',
  '钢结构', '钢-混凝土混合结构', '砖混结构', '木结构', '其他',
];

const RISK_LABELS: Record<string, string> = {
  none: '安全', low: '低风险', medium: '中风险', high: '高风险', critical: '严重风险', unknown: '未知',
};

const PRIORITY_CONFIG: Record<string, { label: string; color: string }> = {
  mandatory: { label: '强制性必配', color: '#dc2626' },
  recommended: { label: '推荐优化配置', color: '#d97706' },
  optional: { label: '因地制宜可选', color: '#2563eb' },
};

const PRESETS: Record<string, { label: string; icon: typeof Home; values: Record<string, string | boolean> }> = {
  office: {
    label: '高层办公建筑', icon: Home,
    values: { building_type: '办公建筑', building_height: '80', floor_count_above: '22', floor_count_below: '2', building_area: '35000', fire_resistance_rating: '一级', structural_form: '钢筋混凝土框架-核心筒结构', fire_hazard_category: '', occupancy_count: '2000', has_sprinkler_system: true, has_alarm_system: true, has_hydrant_system: true, construction_status: '新建', additional_notes: '一类高层办公建筑，标准层面积约 1500m²，设有消防控制室' },
  },
  commercial: {
    label: '大型商业综合体', icon: ShoppingCart,
    values: { building_type: '商业建筑', building_height: '30', floor_count_above: '6', floor_count_below: '1', building_area: '60000', fire_resistance_rating: '一级', structural_form: '钢筋混凝土框架结构', fire_hazard_category: '', occupancy_count: '5000', has_sprinkler_system: true, has_alarm_system: true, has_hydrant_system: true, construction_status: '新建', additional_notes: '大型商业综合体，含零售、餐饮、影院等业态，地下设超市和停车库' },
  },
  residential: {
    label: '高层住宅', icon: Home,
    values: { building_type: '住宅建筑', building_height: '60', floor_count_above: '20', floor_count_below: '1', building_area: '18000', fire_resistance_rating: '一级', structural_form: '钢筋混凝土剪力墙结构', fire_hazard_category: '', occupancy_count: '400', has_sprinkler_system: false, has_alarm_system: false, has_hydrant_system: true, construction_status: '新建', additional_notes: '一类高层住宅（>54m），每层 4 户，地下室设消防水池和泵房' },
  },
  factory: {
    label: '丙类厂房', icon: Wrench,
    values: { building_type: '丙类厂房', building_height: '12', floor_count_above: '3', floor_count_below: '0', building_area: '8000', fire_resistance_rating: '二级', structural_form: '钢结构', fire_hazard_category: '丙', occupancy_count: '200', has_sprinkler_system: false, has_alarm_system: false, has_hydrant_system: false, construction_status: '新建', additional_notes: '多层丙类厂房，单层面积约 2700m²' },
  },
  garage: {
    label: '地下车库', icon: Car,
    values: { building_type: '汽车库', building_height: '6', floor_count_above: '1', floor_count_below: '2', building_area: '15000', fire_resistance_rating: '一级', structural_form: '钢筋混凝土框架结构', fire_hazard_category: '', occupancy_count: '0', has_sprinkler_system: false, has_alarm_system: false, has_hydrant_system: false, construction_status: '新建', additional_notes: '地下二层汽车库，停车约 400 辆，设有充电桩区域' },
  },
  hotel: {
    label: '高层旅馆建筑', icon: Banknote,
    values: { building_type: '旅馆建筑', building_height: '50', floor_count_above: '15', floor_count_below: '1', building_area: '20000', fire_resistance_rating: '一级', structural_form: '钢筋混凝土框架-剪力墙结构', fire_hazard_category: '', occupancy_count: '600', has_sprinkler_system: true, has_alarm_system: true, has_hydrant_system: true, construction_status: '新建', additional_notes: '一类高层旅馆建筑，含客房 200 间，宴会厅、餐厅等配套设施' },
  },
  school: {
    label: '教育建筑', icon: GraduationCap,
    values: { building_type: '教育建筑', building_height: '20', floor_count_above: '5', floor_count_below: '0', building_area: '12000', fire_resistance_rating: '二级', structural_form: '钢筋混凝土框架结构', fire_hazard_category: '', occupancy_count: '1500', has_sprinkler_system: false, has_alarm_system: true, has_hydrant_system: true, construction_status: '新建', additional_notes: '多层教育建筑，含普通教室、实验室、多功能厅等' },
  },
  datacenter: {
    label: '丙类 A 级智算中心', icon: Zap,
    values: { building_type: '数据中心', building_height: '24', floor_count_above: '5', floor_count_below: '1', building_area: '25000', fire_resistance_rating: '一级', structural_form: '钢筋混凝土框架结构', fire_hazard_category: '丙', occupancy_count: '50', has_sprinkler_system: false, has_alarm_system: false, has_hydrant_system: false, construction_status: '新建', additional_notes: '智算中心，单机柜功率密度 20-40kW，采用行级/机柜级精密空调，主机房设气体灭火系统（七氟丙烷），双路市电+柴油发电机+UPS 供电' },
  },
};

const OCCUPANCY_RELEVANT = new Set([
  '住宅建筑', '办公建筑', '商业建筑', '旅馆建筑', '餐饮建筑',
  '教育建筑', '医疗建筑', '老年人照料设施', '幼儿园/托儿所',
  '体育场馆', '交通枢纽', '博物馆/展览馆', '图书馆/档案馆', '娱乐场所',
]);

interface FireHistoryItem {
  id: number;
  request: Record<string, unknown>;
  response: FireSafetyResult;
  created_at: string;
}

const HISTORY_KEY = 'fire_safety_history';
const MAX_HISTORY = 20;

function loadHistory(): FireHistoryItem[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function pad(n: number) { return String(n).padStart(2, '0'); }
function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function priBadge(p: string) {
  switch (p) {
    case 'mandatory': return <Badge variant="destructive">强制</Badge>;
    case 'recommended': return <Badge variant="warning">推荐</Badge>;
    case 'optional': return <Badge variant="outline">可选</Badge>;
    default: return <Badge>{p}</Badge>;
  }
}

interface FormState {
  building_type: string;
  custom_building_type: string;
  building_height: string;
  floor_count_above: string;
  floor_count_below: string;
  building_area: string;
  fire_resistance_rating: string;
  structural_form: string;
  fire_hazard_category: string;
  occupancy_count: string;
  has_sprinkler_system: boolean;
  has_alarm_system: boolean;
  has_hydrant_system: boolean;
  construction_status: string;
  additional_notes: string;
}

const defaultForm: FormState = {
  building_type: '',
  custom_building_type: '',
  building_height: '',
  floor_count_above: '',
  floor_count_below: '0',
  building_area: '',
  fire_resistance_rating: '二级',
  structural_form: '',
  fire_hazard_category: '',
  occupancy_count: '',
  has_sprinkler_system: false,
  has_alarm_system: false,
  has_hydrant_system: false,
  construction_status: '新建',
  additional_notes: '',
};

export default function FireSafetyPage() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<FireSafetyResult | null>(null);
  const [form, setForm] = useState<FormState>(defaultForm);
  const [errors, setErrors] = useState<string[]>([]);
  const [aboveGroundFireArea, setAboveGroundFireArea] = useState('');
  const [belowGroundFireArea, setBelowGroundFireArea] = useState('');
  const [previewOpen, setPreviewOpen] = useState(false);
  const [history, setHistory] = useState<FireHistoryItem[]>(loadHistory());
  const [compOpen, setCompOpen] = useState(false);

  const update = (key: string, value: string | boolean) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  // Derived values
  const h = Number(form.building_height) || 0;
  const area = Number(form.building_area) || 0;
  const fa = Number(form.floor_count_above) || 0;
  const fb = Number(form.floor_count_below) || 0;
  const bt = form.building_type === '其他' ? form.custom_building_type : form.building_type;

  const isHighRise = bt ? (bt === '住宅建筑' ? h > 27 : h > 24) : false;
  const isIndustrial = bt.includes('厂房') || bt.includes('仓库');
  const isWarehouse = bt.includes('仓库');

  // Suggested max fire area
  let sMax = 2500;
  if (h > 0 && bt) {
    if (form.fire_resistance_rating === '三级') sMax = 1200;
    if (form.fire_resistance_rating === '四级') sMax = 600;
    if (isHighRise) sMax = Math.min(sMax, 1500);
    if (isIndustrial && !isWarehouse) {
      if (form.fire_hazard_category === '甲' || form.fire_hazard_category === '乙') {
        sMax = h <= 24 ? 3000 : 2000;
      } else {
        sMax = Math.max(sMax, h <= 24 ? 6000 : 4000);
      }
      if (form.fire_resistance_rating === '三级') sMax = Math.min(sMax, h <= 24 ? 3000 : 2000);
      if (form.fire_resistance_rating === '四级') sMax = Math.min(sMax, h <= 24 ? 1200 : 0);
    }
    if (isWarehouse) {
      sMax = form.fire_resistance_rating === '三级' ? (fa > 1 ? 1200 : 2800)
        : form.fire_resistance_rating === '四级' ? (fa > 1 ? 0 : 1200)
        : fa > 1 ? 2000 : 4000;
    }
    if (form.has_sprinkler_system) sMax *= 2;
  }
  const suggestedMaxFireArea = sMax;
  const suggestedBasementMax = form.has_sprinkler_system ? 1000 : 500;

  const areaWarn = area > 0 && area > suggestedMaxFireArea;
  const showComp = areaWarn || fb > 0;

  // Compliance warnings
  const compWarnings: string[] = [];
  if (h > 0 && area > 0 && form.fire_resistance_rating && bt) {
    if (isHighRise && (form.fire_resistance_rating === '三级' || form.fire_resistance_rating === '四级')) {
      compWarnings.push(`耐火等级 ${form.fire_resistance_rating} 不满足高层建筑要求（≥二级），《建筑设计防火规范》GB 50016 第 5.1.1 条`);
    }
    if (fa > 5 && form.fire_resistance_rating === '三级') {
      compWarnings.push(`三级耐火等级最多允许 5 层，当前地上 ${fa} 层超出限值，《建筑设计防火规范》GB 50016 第 5.3.1 条`);
    }
    if (fa > 2 && form.fire_resistance_rating === '四级') {
      compWarnings.push(`四级耐火等级最多允许 2 层，当前地上 ${fa} 层超出限值，《建筑设计防火规范》GB 50016 第 5.3.1 条`);
    }
    if (!aboveGroundFireArea) {
      if (area > suggestedMaxFireArea * 1.5) {
        compWarnings.push(`总建筑面积 ${area.toLocaleString()} m² 远超单个防火分区最大允许面积 ${suggestedMaxFireArea.toLocaleString()} m²${form.has_sprinkler_system ? '（已含喷淋加倍）' : ''}，必须设置防火墙划分防火分区，《建筑设计防火规范》GB 50016 第 5.3.1 条`);
      } else if (area > suggestedMaxFireArea) {
        compWarnings.push(`总建筑面积 ${area.toLocaleString()} m² 超过单个防火分区最大允许 ${suggestedMaxFireArea.toLocaleString()} m²，需划分至少 2 个防火分区，《建筑设计防火规范》GB 50016 第 5.3.1 条`);
      }
      if (fb > 0) {
        compWarnings.push(`地下室防火分区面积限值 ${suggestedBasementMax} m²${form.has_sprinkler_system ? '（设喷淋加倍至 1000m²）' : '（无喷淋 500m²）'}，须独立划分防火分区，《建筑设计防火规范》GB 50016 第 5.3.1 条`);
      }
    }
    if (isIndustrial && !form.fire_hazard_category) {
      compWarnings.push('工业建筑/厂房/仓库必须填写火灾危险性类别（甲/乙/丙/丁/戊）');
    }
    if (bt === '数据中心' && form.fire_resistance_rating === '三级') {
      compWarnings.push('数据中心耐火等级不应低于二级（《数据中心设计规范》GB 50174-2017），推荐一级耐火等级');
    }
    if ((bt === '老年人照料设施' || bt === '幼儿园/托儿所') && fb > 0) {
      compWarnings.push(`${bt}严禁设置在地下、半地下，当前地下 ${fb} 层，违反《建筑设计防火规范》GB 50016 第 5.4.4 条`);
    }
    if (bt === '老年人照料设施' && fa > 3) {
      compWarnings.push(`老年人照料设施不应布置在 3 层以上，当前地上 ${fa} 层，《建筑设计防火规范》GB 50016 第 5.3.2A 条`);
    }
    if (bt === '幼儿园/托儿所' && fa > 4) {
      compWarnings.push(`幼儿园/托儿所不应布置在 4 层及以上，当前地上 ${fa} 层，《建筑设计防火规范》GB 50016 第 5.4.4 条`);
    }
    if (fa > 0 && h / fa < 2.5) {
      compWarnings.push(`平均层高 ${(h / fa).toFixed(1)}m，低于常规值（≥3.0m），请检查数据`);
    }
    if (fa > 0 && h / fa > 8) {
      compWarnings.push(`平均层高 ${(h / fa).toFixed(1)}m，超出常规值，请检查数据`);
    }
  }

  // High rise note
  let highRiseNote = '';
  if (h > 0 && bt) {
    const threshold = bt === '住宅建筑' ? 27 : 24;
    if (h > threshold) {
      const label = h > 100 ? '超高层' : h > 50 ? '一类高层' : '二类高层';
      highRiseNote = `判定为${label}建筑（${bt === '住宅建筑' ? '住宅' : '公建'}高层阈值 ${threshold}m），消防配置按高层规范核算`;
    }
  }

  // Height warning
  let heightWarning = '';
  if (h > 100) {
    heightWarning = `建筑高度 ${h}m 为超高层建筑（>100m）：必须全楼设自动喷水灭火系统、火灾自动报警系统，每 50m 设避难层，设消防电梯和直升机停机坪，耐火等级不应低于一级。`;
  } else if (h > 50) {
    heightWarning = `建筑高度 ${h}m 为一类高层（>50m）：必须设自动喷水灭火系统、火灾自动报警系统、机械防烟系统、消防电梯，前室采用防烟楼梯间。`;
  }

  // Area limit hint
  let areaLimitHint = '';
  if (h > 0 && area > 0 && bt) {
    const factors: string[] = [];
    if (isIndustrial) {
      if (isWarehouse) factors.push('丙类仓库');
      else factors.push(`${form.fire_hazard_category || '丙/丁/戊'}类厂房`);
    }
    if (isHighRise) factors.push(h > 100 ? '超高层（>100m）' : '高层建筑');
    else if (!isIndustrial) factors.push('单多层建筑');
    if (form.has_sprinkler_system) factors.push('设喷淋（面积可加倍）');
    if (form.fire_resistance_rating === '三级') factors.push('三级耐火等级（限值降低）');
    if (form.fire_resistance_rating === '四级') factors.push('四级耐火等级（限值严格受限）');
    areaLimitHint = `《建筑设计防火规范》GB 50016 第 5.3.1 条 | ${factors.join(' · ')} | 地上防火分区限值 ${suggestedMaxFireArea.toLocaleString()} m²`;
  }

  // Compartment summary
  let compSummary = '';
  if (showComp) {
    const parts: string[] = [];
    const av = Number(aboveGroundFireArea);
    if (av > 0) {
      parts.push(`地上单分区设计 ${av.toLocaleString()} m²（限值 ${suggestedMaxFireArea.toLocaleString()} m²，约 ${Math.ceil(area / av)} 个分区）`);
    } else {
      parts.push(`地上防火分区限值 ${suggestedMaxFireArea.toLocaleString()} m²，待填写设计分区面积`);
    }
    if (fb > 0) {
      const bv = Number(belowGroundFireArea);
      if (bv > 0) {
        parts.push(`地下单分区设计 ${bv.toLocaleString()} m²（限值 ${suggestedBasementMax.toLocaleString()} m²）`);
      } else {
        parts.push(`地下防火分区限值 ${suggestedBasementMax.toLocaleString()} m²，待填写设计分区面积`);
      }
    }
    compSummary = parts.join('；');
  }

  // Existing facilities text
  const existingItems: string[] = [];
  if (form.has_sprinkler_system) existingItems.push('自动喷水灭火系统');
  if (form.has_alarm_system) existingItems.push('火灾自动报警系统');
  if (form.has_hydrant_system) existingItems.push('室内消火栓系统');
  const existingText = existingItems.length > 0 ? existingItems.join('、') + '（已安装）' : '';

  // Summary stats
  let stats: { total: number; mandatory: number; recommended: number; optional: number } | null = null;
  if (result?.fire_facilities && result?.building_materials && result?.safety_preparations) {
    const all = [...result.fire_facilities, ...result.building_materials, ...result.safety_preparations];
    stats = {
      total: all.length,
      mandatory: all.filter((i) => i.priority === 'mandatory').length,
      recommended: all.filter((i) => i.priority === 'recommended').length,
      optional: all.filter((i) => i.priority === 'optional').length,
    };
  }

  const showOccupancy = OCCUPANCY_RELEVANT.has(form.building_type);

  // Preset
  const applyPreset = (key: string) => {
    const p = PRESETS[key];
    if (!p) return;
    setForm({ ...defaultForm, ...p.values } as FormState);
    setAboveGroundFireArea('');
    setBelowGroundFireArea('');
    setCompOpen(false);
    setErrors([]);
  };

  // Validate
  const validate = () => {
    const errs: string[] = [];
    const t = form.building_type === '其他' ? form.custom_building_type : form.building_type;
    if (!t) errs.push('请选择建筑类型');
    if (area <= 0) errs.push('请填写建筑面积');
    if (h <= 0) errs.push('请填写建筑高度');
    if (fa < 0) errs.push('地上层数不能为负数');
    if (fb < 0) errs.push('地下层数不能为负数');
    if (!form.fire_resistance_rating) errs.push('请选择耐火等级');
    setErrors(errs);
    return errs.length === 0;
  };

  // History
  const saveHist = (items: FireHistoryItem[]) => {
    setHistory(items);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items));
  };

  const addToHistory = (req: Record<string, unknown>, resp: FireSafetyResult) => {
    const item: FireHistoryItem = { id: Date.now(), request: { ...req }, response: { ...resp }, created_at: new Date().toISOString() };
    saveHist([item, ...history].slice(0, MAX_HISTORY));
  };

  const selectHist = (item: FireHistoryItem) => {
    setResult(item.response);
    const r = item.request as Record<string, unknown>;
    const btype = String(r.building_type || '');
    setForm({
      ...defaultForm,
      building_type: BUILDING_TYPES.includes(btype) ? btype : '其他',
      custom_building_type: BUILDING_TYPES.includes(btype) ? '' : btype,
      building_height: String(r.building_height || ''),
      floor_count_above: String(r.floor_count_above || ''),
      floor_count_below: String(r.floor_count_below || ''),
      building_area: String(r.building_area || ''),
      fire_resistance_rating: String(r.fire_resistance_rating || '二级'),
      structural_form: String(r.structural_form || ''),
      fire_hazard_category: String(r.fire_hazard_category || ''),
      occupancy_count: String(r.occupancy_count || ''),
      has_sprinkler_system: Boolean(r.has_sprinkler_system),
      has_alarm_system: Boolean(r.has_alarm_system),
      has_hydrant_system: Boolean(r.has_hydrant_system),
      construction_status: String(r.construction_status || '新建'),
      additional_notes: String(r.additional_notes || ''),
    });
    setAboveGroundFireArea('');
    setBelowGroundFireArea('');
  };

  // Submit
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) return;
    setLoading(true);
    setResult(null);
    try {
      let notes = form.additional_notes;
      if (showComp && Number(aboveGroundFireArea) > 0) {
        const parts: string[] = [`地上单个防火分区设计面积：${aboveGroundFireArea} m²`];
        if (fb > 0 && Number(belowGroundFireArea) > 0) parts.push(`地下单个防火分区设计面积：${belowGroundFireArea} m²`);
        notes = notes ? notes + '\n' + parts.join('；') : parts.join('；');
      }
      const res = await fireSafetyRecommend({
        building_type: form.building_type === '其他' ? form.custom_building_type : form.building_type,
        building_height: h,
        floor_count_above: fa,
        floor_count_below: fb,
        building_area: area,
        fire_resistance_rating: form.fire_resistance_rating,
        structural_form: form.structural_form,
        fire_hazard_category: form.fire_hazard_category,
        occupancy_count: form.occupancy_count ? Number(form.occupancy_count) : 0,
        has_sprinkler_system: form.has_sprinkler_system,
        has_alarm_system: form.has_alarm_system,
        has_hydrant_system: form.has_hydrant_system,
        construction_status: form.construction_status,
        additional_notes: notes,
      });
      setResult(res);
      if (!res.error) addToHistory({
        building_type: form.building_type === '其他' ? form.custom_building_type : form.building_type,
        building_height: h, floor_count_above: fa, floor_count_below: fb, building_area: area,
        fire_resistance_rating: form.fire_resistance_rating, structural_form: form.structural_form,
        fire_hazard_category: form.fire_hazard_category,
        occupancy_count: Number(form.occupancy_count),
        has_sprinkler_system: form.has_sprinkler_system, has_alarm_system: form.has_alarm_system,
        has_hydrant_system: form.has_hydrant_system, construction_status: form.construction_status,
        additional_notes: notes,
      }, res);
    } catch {
      setResult({
        ok: false, building_type: '', building_height: 0, building_area: 0,
        structural_form: '', risk_level: '', summary: '',
        fire_facilities: [], building_materials: [], safety_preparations: [],
        applicable_standards: [], recommendations: [], room_type: '',
        notes: '', regulation_refs: '', error: '推荐生成失败，请稍后重试',
      });
    } finally {
      setLoading(false);
    }
  };

  // Download
  const handleDownload = () => {
    if (!result) return;
    downloadFireSafetyReport({
      ...result,
      structural_form: form.structural_form,
      fire_resistance_rating: form.fire_resistance_rating,
      floor_count_above: fa,
      floor_count_below: fb,
      construction_status: form.construction_status,
      fire_hazard_category: form.fire_hazard_category,
      occupancy_count: Number(form.occupancy_count),
    } as FireSafetyResult);
  };

  // Auto-open compartment
  if (areaWarn && !compOpen && !loading) {
    // Use setTimeout to avoid setState during render
  }

  return (
    <PlatformShell title="消防配置" description="输入建筑参数，AI 智能生成消防设施、建材和安全管理方案">
      <div className="mx-auto max-w-4xl">
        {/* Form */}
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Building2 className="size-5 text-primary" />
              <CardTitle>建筑参数</CardTitle>
            </div>
            <CardDescription>填写建筑基本信息，生成消防配置推荐方案</CardDescription>
          </CardHeader>
          <CardContent>
            {/* Quick-fill */}
            <div className="mb-5 flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-primary/40 bg-primary/5 px-3 py-2.5">
              <span className="text-xs text-muted-foreground whitespace-nowrap">快速填充：</span>
              {Object.entries(PRESETS).map(([key, preset]) => {
                const Icon = preset.icon;
                return (
                  <Button key={key} variant="outline" size="xs" onClick={() => applyPreset(key)}>
                    <Icon className="size-3" />{preset.label}
                  </Button>
                );
              })}
            </div>

            <form onSubmit={handleSubmit} className="space-y-6">
              {/* Basic info */}
              <div>
                <p className="mb-3 flex items-center gap-1.5 text-sm font-medium">
                  <Building2 className="size-3.5" /> 基本信息
                </p>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-sm">建筑类型 *</label>
                    <Select value={form.building_type} onChange={(e) => {
                      const val = e.target.value;
                      update('building_type', val);
                      if (val && !OCCUPANCY_RELEVANT.has(val)) update('occupancy_count', '');
                    }}>
                      <option value="">请选择</option>
                      {BUILDING_TYPES.map((t) => (<option key={t} value={t}>{t}</option>))}
                    </Select>
                    {form.building_type === '其他' && (
                      <Input className="mt-2" placeholder="请输入具体建筑类型（不超过20字）" maxLength={20}
                        value={form.custom_building_type}
                        onChange={(e) => update('custom_building_type', e.target.value)} />
                    )}
                  </div>
                  <div>
                    <label className="mb-1 block text-sm">耐火等级 *</label>
                    <Select value={form.fire_resistance_rating} onChange={(e) => update('fire_resistance_rating', e.target.value)}>
                      {FIRE_RESISTANCE.map((r) => (<option key={r} value={r}>{r}</option>))}
                    </Select>
                  </div>
                </div>
              </div>

              {/* Volume */}
              <div>
                <p className="mb-3 flex items-center gap-1.5 text-sm font-medium">建筑体量</p>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-sm">建筑高度 (m) *</label>
                    <Input type="number" placeholder="如 24" min={0} step={0.1}
                      value={form.building_height}
                      onChange={(e) => update('building_height', e.target.value)} />
                    {highRiseNote && <p className="mt-1.5 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs text-amber-700">{highRiseNote}</p>}
                    {heightWarning && <p className="mt-1.5 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-700 leading-relaxed">{heightWarning}</p>}
                  </div>
                  <div>
                    <label className="mb-1 block text-sm">总建筑面积 (m²) *</label>
                    <Input type="number" placeholder="如 5000" min={0}
                      value={form.building_area}
                      onChange={(e) => update('building_area', e.target.value)} />
                    {areaLimitHint && <p className="mt-1.5 rounded-md border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs text-blue-700 leading-relaxed">{areaLimitHint}</p>}
                  </div>
                </div>
              </div>

              {/* Fire compartment */}
              {showComp && (
                <div className={`rounded-lg border-2 p-4 ${aboveGroundFireArea && Number(aboveGroundFireArea) > 0 && (!fb || Number(belowGroundFireArea) > 0) ? 'border-green-300 bg-green-50/50' : 'border-orange-200 bg-orange-50/50'}`}>
                  <button type="button" className="flex w-full items-center justify-between text-sm font-medium"
                    onClick={() => setCompOpen(!compOpen)}>
                    <span className="flex items-center gap-2">
                      <AlertTriangle className="size-4 text-amber-500" />
                      防火分区设计参数（面积超标，请补充）
                    </span>
                    <span className="text-muted-foreground text-xs">{compOpen ? '收起 ▲' : '展开 ▼'}</span>
                  </button>
                  {compOpen && (
                    <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
                      <div>
                        <label className="mb-1 block text-xs text-muted-foreground">地上单个防火分区设计面积 (m²)</label>
                        <Input type="number" min={1} step={100} placeholder={`建议 ≤ ${suggestedMaxFireArea}`}
                          value={aboveGroundFireArea}
                          onChange={(e) => setAboveGroundFireArea(e.target.value)} />
                        <p className="mt-1 text-xs text-muted-foreground">按《建筑设计防火规范》GB 50016 第 5.3.1 条，当前限值 {suggestedMaxFireArea.toLocaleString()} m²</p>
                      </div>
                      {fb > 0 && (
                        <div>
                          <label className="mb-1 block text-xs text-muted-foreground">地下单个防火分区设计面积 (m²)</label>
                          <Input type="number" min={1} step={100} placeholder={`建议 ≤ ${suggestedBasementMax}`}
                            value={belowGroundFireArea}
                            onChange={(e) => setBelowGroundFireArea(e.target.value)} />
                          <p className="mt-1 text-xs text-muted-foreground">地下室防火分区限值 {suggestedBasementMax} m²（《建筑设计防火规范》GB 50016 第 5.3.1 条）</p>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Structure */}
              <div>
                <p className="mb-3 flex items-center gap-1.5 text-sm font-medium">结构信息</p>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-sm">地上层数 *</label>
                    <Input type="number" placeholder="如 6" min={0}
                      value={form.floor_count_above}
                      onChange={(e) => update('floor_count_above', e.target.value)} />
                  </div>
                  <div>
                    <label className="mb-1 block text-sm">地下层数</label>
                    <Input type="number" placeholder="如 1" min={0}
                      value={form.floor_count_below}
                      onChange={(e) => update('floor_count_below', e.target.value)} />
                  </div>
                  <div className="sm:col-span-2">
                    <label className="mb-1 block text-sm">结构形式 *</label>
                    <Select value={form.structural_form} onChange={(e) => update('structural_form', e.target.value)}>
                      <option value="">请选择结构形式（钢结构须做防火保护层）</option>
                      {STRUCTURAL_FORMS.map((s) => (<option key={s} value={s}>{s}</option>))}
                    </Select>
                  </div>
                </div>
              </div>

              {/* Risk & status */}
              <div>
                <p className="mb-3 flex items-center gap-1.5 text-sm font-medium">
                  <AlertTriangle className="size-3.5" /> 火灾风险与建设状态
                </p>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  <div>
                    <label className="mb-1 block text-sm">火灾危险性类别</label>
                    <Select value={form.fire_hazard_category} onChange={(e) => update('fire_hazard_category', e.target.value)}>
                      <option value="">不填</option>
                      {HAZARD_CATEGORIES.filter(Boolean).map((c) => (<option key={c} value={c}>{c} 类</option>))}
                    </Select>
                    <p className="mt-1 text-xs text-muted-foreground">工业建筑必填</p>
                  </div>
                  <div>
                    <label className="mb-1 block text-sm">建设状态</label>
                    <Select value={form.construction_status} onChange={(e) => update('construction_status', e.target.value)}>
                      {CONSTRUCTION_STATUS.map((s) => (<option key={s} value={s}>{s}</option>))}
                    </Select>
                  </div>
                  {showOccupancy && (
                    <div>
                      <label className="mb-1 block text-sm">预计容纳人数</label>
                      <Input type="number" placeholder="0=不确定" min={0}
                        value={form.occupancy_count}
                        onChange={(e) => update('occupancy_count', e.target.value)} />
                      <p className="mt-1 text-xs text-muted-foreground">选填，仅人员密集建筑填写</p>
                    </div>
                  )}
                </div>
              </div>

              {/* Existing facilities */}
              {form.construction_status !== '新建' && (
                <div>
                  <p className="mb-2 flex items-center gap-1.5 text-sm font-medium">
                    <HardHat className="size-3.5" /> 现有消防设施（仅改扩建项目填写）
                  </p>
                  <p className="mb-2 text-xs text-muted-foreground">请勾选已安装的消防设施，AI 将避免重复推荐并提供升级补充建议</p>
                  <div className="flex flex-wrap gap-3">
                    {[
                      { key: 'has_sprinkler_system', icon: Flame, label: '自动喷水灭火系统' },
                      { key: 'has_alarm_system', icon: Bell, label: '火灾自动报警系统' },
                      { key: 'has_hydrant_system', icon: Flame, label: '室内消火栓系统' },
                    ].map((f) => {
                      const Icon = f.icon;
                      return (
                        <label key={f.key} className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 cursor-pointer hover:bg-accent/50">
                          <input type="checkbox" checked={Boolean(form[f.key as keyof FormState])}
                            onChange={(e) => update(f.key, e.target.checked)} className="size-4" />
                          <Icon className="size-4 text-muted-foreground" />
                          <span className="text-sm">{f.label}</span>
                        </label>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Notes */}
              <div>
                <label className="mb-1 block text-sm">补充说明</label>
                <Input placeholder="特殊要求或补充信息" value={form.additional_notes}
                  onChange={(e) => update('additional_notes', e.target.value)} />
              </div>

              {/* Compliance warnings */}
              {compWarnings.length > 0 && (
                <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600" />
                  <div>
                    <p className="mb-1 text-sm font-medium text-amber-800">参数合规风险提示</p>
                    <ul className="space-y-0.5 text-xs text-amber-700">
                      {compWarnings.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                  </div>
                </div>
              )}

              {/* Validation errors */}
              {errors.length > 0 && (
                <div className="flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                  <div>{errors.map((e, i) => <p key={i}>{e}</p>)}</div>
                </div>
              )}

              <Button type="submit" disabled={loading} size="lg" className="w-full">
                <Shield className="size-4" />
                {loading ? 'AI 分析中...' : '生成消防配置推荐'}
              </Button>
            </form>
          </CardContent>
        </Card>

        {/* Loading */}
        {loading && (
          <Card className="mt-6">
            <CardContent className="flex items-center gap-3 py-8">
              <div className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
              <p className="text-sm text-muted-foreground">AI 正在分析建筑参数并生成消防配置推荐，请稍候...</p>
            </CardContent>
          </Card>
        )}

        {/* History */}
        {history.length > 0 && (
          <Card className="mt-6">
            <CardHeader className="flex flex-row items-center justify-between">
              <div>
                <CardTitle>历史记录</CardTitle>
                <CardDescription>点击可查看过往方案详情</CardDescription>
              </div>
              <Button variant="outline" size="xs" onClick={() => saveHist([])}>清空记录</Button>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                {history.map((item) => (
                  <div key={item.id}
                    className="group relative cursor-pointer rounded-lg border border-border p-3 hover:shadow-sm transition-shadow"
                    style={{ borderTopWidth: '3px', borderTopColor: item.response.risk_level === 'critical' || item.response.risk_level === 'high' ? '#ef4444' : item.response.risk_level === 'medium' ? '#f97316' : item.response.risk_level === 'low' ? '#3b82f6' : '#9ca3af' }}
                    onClick={() => selectHist(item)}>
                    <button className="absolute right-2 top-2 rounded p-0.5 text-muted-foreground opacity-0 hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100 transition-opacity"
                      onClick={(e) => { e.stopPropagation(); saveHist(history.filter((h) => h.id !== item.id)); }}>
                      &times;
                    </button>
                    <p className="text-sm font-medium text-foreground pr-6 truncate">{String(item.request.building_type || '')}</p>
                    <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                      <Badge variant={item.response.risk_level === 'high' || item.response.risk_level === 'critical' ? 'destructive' : item.response.risk_level === 'medium' ? 'warning' : 'success'} className="text-xs">
                        {RISK_LABELS[item.response.risk_level] || '未知'}
                      </Badge>
                      <span>{fmtTime(item.created_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        )}

        {/* Result */}
        {result && !loading && (
          <div className="mt-6 space-y-6">
            {/* Error */}
            {result.error && (
              <Card className="border-destructive/50 bg-destructive/5">
                <CardContent className="flex items-center gap-3 py-4">
                  <AlertTriangle className="size-5 text-destructive" />
                  <p className="text-sm text-destructive">{result.error}</p>
                </CardContent>
              </Card>
            )}

            {/* Summary */}
            {result.summary && (
              <Card className="bg-linear-to-br from-muted/50 to-muted/30">
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <CheckCircle className="size-5 text-success" />
                      <CardTitle>消防配置推荐总览</CardTitle>
                    </div>
                    <Badge variant={result.risk_level === 'critical' || result.risk_level === 'high' ? 'destructive' : result.risk_level === 'medium' ? 'warning' : 'success'}>
                      {RISK_LABELS[result.risk_level] || '未知'}消防安全风险
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent>
                  {stats && (
                    <div className="mb-4 grid grid-cols-3 gap-3">
                      <div className="flex flex-col items-center rounded-lg border border-red-200 bg-red-50 px-3 py-2.5">
                        <span className="text-xl font-bold text-red-600">{stats.mandatory}</span>
                        <span className="text-xs text-muted-foreground">强制性必配</span>
                      </div>
                      <div className="flex flex-col items-center rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5">
                        <span className="text-xl font-bold text-amber-600">{stats.recommended}</span>
                        <span className="text-xs text-muted-foreground">推荐优化</span>
                      </div>
                      <div className="flex flex-col items-center rounded-lg border border-blue-200 bg-blue-50 px-3 py-2.5">
                        <span className="text-xl font-bold text-blue-600">{stats.optional}</span>
                        <span className="text-xs text-muted-foreground">因地制宜可选</span>
                      </div>
                    </div>
                  )}
                  <p className="text-sm leading-relaxed text-muted-foreground">{result.summary}</p>
                  <table className="mt-4 w-full text-sm">
                    <tbody>
                      {[
                        ['建筑类型', result.building_type],
                        ['耐火等级', form.fire_resistance_rating],
                        ...(form.structural_form ? [['结构形式', form.structural_form]] as const : []),
                        ['建筑高度', `${result.building_height} m`],
                        ['建筑面积', `${result.building_area.toLocaleString()} m²`],
                        ['地上 / 地下', `${fa} 层 / ${fb} 层`],
                        ['建设状态', form.construction_status],
                        ...(form.fire_hazard_category ? [['火灾危险性', `${form.fire_hazard_category} 类`]] as const : []),
                        ...(form.occupancy_count && Number(form.occupancy_count) > 0 ? [['容纳人数', `${Number(form.occupancy_count).toLocaleString()} 人`]] as const : []),
                        ...(compSummary ? [['防火分区设计', compSummary]] as const : []),
                        ...(existingText ? [['现有设施', existingText]] as const : []),
                      ].map(([label, value], i) => (
                        <tr key={i}><td className="py-1.5 pr-3 text-muted-foreground w-24">{label}</td><td className="py-1.5 text-xs">{value}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </CardContent>
              </Card>
            )}

            {/* Fire Facilities */}
            {result.fire_facilities && result.fire_facilities.length > 0 && (
              <Card>
                <CardHeader>
                  <div className="flex items-center gap-2">
                    <Flame className="size-5 text-destructive" />
                    <CardTitle>消防设施配置</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {result.fire_facilities.map((item, i) => (
                    <div key={i} className="rounded-lg border border-border p-4" style={{ borderLeftColor: PRIORITY_CONFIG[item.priority]?.color || '#e5e7eb', borderLeftWidth: '3px' }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium text-sm">{item.name}</span>
                        {priBadge(item.priority)}
                        <Badge variant="outline" className="text-xs">{item.category}</Badge>
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-1.5 text-sm text-muted-foreground sm:grid-cols-2">
                        <div><span className="font-medium text-foreground">规格：</span>{item.specification || '-'}</div>
                        <div><span className="font-medium text-foreground">数量/配置：</span>{item.quantity_or_coverage || '-'}</div>
                        <div className="sm:col-span-2"><span className="font-medium text-foreground">安装位置：</span>{item.installation_location || '-'}</div>
                        {item.regulation_ref && (
                          <div className="sm:col-span-2 text-xs text-muted-foreground border-t border-border pt-2 mt-1">
                            <span className="font-medium text-foreground">法规依据：</span>{item.regulation_ref}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Building Materials */}
            {result.building_materials && result.building_materials.length > 0 && (
              <Card>
                <CardHeader>
                  <div className="flex items-center gap-2">
                    <HardHat className="size-5 text-warning" />
                    <CardTitle>建筑材料要求</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {result.building_materials.map((item, i) => (
                    <div key={i} className="rounded-lg border border-border p-4" style={{ borderLeftColor: PRIORITY_CONFIG[item.priority]?.color || '#e5e7eb', borderLeftWidth: '3px' }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium text-sm">{item.name}</span>
                        {priBadge(item.priority)}
                        <Badge variant="outline" className="text-xs">{item.category}</Badge>
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-1.5 text-sm text-muted-foreground sm:grid-cols-2">
                        <div><span className="font-medium text-foreground">规格：</span>{item.specification || '-'}</div>
                        <div><span className="font-medium text-foreground">应用部位：</span>{item.application_location || '-'}</div>
                        {item.regulation_ref && (
                          <div className="sm:col-span-2 text-xs text-muted-foreground border-t border-border pt-2 mt-1">
                            <span className="font-medium text-foreground">法规依据：</span>{item.regulation_ref}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Safety Preparations */}
            {result.safety_preparations && result.safety_preparations.length > 0 && (
              <Card>
                <CardHeader>
                  <div className="flex items-center gap-2">
                    <ClipboardList className="size-5 text-primary" />
                    <CardTitle>消防安全管理</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {result.safety_preparations.map((item, i) => (
                    <div key={i} className="rounded-lg border border-border p-4" style={{ borderLeftColor: PRIORITY_CONFIG[item.priority]?.color || '#e5e7eb', borderLeftWidth: '3px' }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium text-sm">{item.name}</span>
                        {priBadge(item.priority)}
                        <Badge variant="outline" className="text-xs">{item.category}</Badge>
                      </div>
                      <div className="mt-2 text-sm text-muted-foreground">
                        <div><span className="font-medium text-foreground">具体要求：</span>{item.requirement || '-'}</div>
                        {item.regulation_ref && (
                          <div className="text-xs text-muted-foreground border-t border-border pt-2 mt-1.5">
                            <span className="font-medium text-foreground">法规依据：</span>{item.regulation_ref}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Standards */}
            {result.applicable_standards && result.applicable_standards.length > 0 && (
              <Card>
                <CardHeader>
                  <div className="flex items-center gap-2">
                    <BookOpen className="size-5 text-primary" />
                    <CardTitle>适用标准</CardTitle>
                  </div>
                </CardHeader>
                <CardContent>
                  <ul className="space-y-1">
                    {result.applicable_standards.map((s, i) => (
                      <li key={i} className="flex items-center gap-2 text-sm text-muted-foreground">
                        <span className="size-1.5 rounded-full bg-primary" />{s}
                      </li>
                    ))}
                  </ul>
                </CardContent>
              </Card>
            )}

            {/* Simplified fallback */}
            {result.recommendations && result.recommendations.length > 0 && (!result.fire_facilities || result.fire_facilities.length === 0) && (
              <Card>
                <CardHeader><CardTitle>推荐配置</CardTitle></CardHeader>
                <CardContent className="space-y-2">
                  {result.recommendations.map((item, i) => (
                    <div key={i} className="flex items-center justify-between rounded-lg border border-border px-3 py-2">
                      <div>
                        <p className="text-sm font-medium">{item.name}</p>
                        <p className="text-xs text-muted-foreground">{item.specification} · {item.location}</p>
                        <p className="text-xs text-muted-foreground">依据: {item.reason}</p>
                      </div>
                      <Badge>{item.quantity}</Badge>
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Notes */}
            {(result.notes || result.regulation_refs) && (
              <Card>
                <CardContent className="space-y-2 p-4 text-sm">
                  {result.notes && <p><span className="font-medium">注意事项：</span>{result.notes}</p>}
                  {result.regulation_refs && <p><span className="font-medium">参考规范：</span>{result.regulation_refs}</p>}
                </CardContent>
              </Card>
            )}

            {/* Actions */}
            <div className="flex justify-center gap-4">
              <Button variant="outline" size="lg" onClick={() => setPreviewOpen(true)}>
                <FileText className="size-4" /> 预览报告
              </Button>
              <Button size="lg" onClick={handleDownload}>
                <Download className="size-4" /> 导出 Word
              </Button>
            </div>

            {/* Preview modal */}
            {previewOpen && (
              <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setPreviewOpen(false)}>
                <div className="mx-4 max-h-[90vh] w-full max-w-4xl overflow-y-auto rounded-xl bg-card p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
                  <div className="flex items-center justify-between mb-4">
                    <h2 className="text-lg font-bold">消防安全配置推荐报告预览</h2>
                    <button onClick={() => setPreviewOpen(false)} className="rounded-lg p-1.5 hover:bg-accent">&times;</button>
                  </div>
                  <p className="text-center text-xs text-muted-foreground mb-4">
                    生成时间：{new Date().toLocaleString('zh-CN')}
                    &nbsp;|&nbsp;
                    风险等级：<Badge variant={result.risk_level === 'high' || result.risk_level === 'critical' ? 'destructive' : result.risk_level === 'medium' ? 'warning' : 'success'}>{RISK_LABELS[result.risk_level]}</Badge>
                  </p>

                  <h3 className="mb-2 border-l-[3px] border-primary pl-2 text-sm font-semibold">一、建筑参数与总体评估</h3>
                  <div className="mb-4 grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
                    <div><span className="text-muted-foreground">建筑类型：</span>{result.building_type}</div>
                    <div><span className="text-muted-foreground">耐火等级：</span>{form.fire_resistance_rating}</div>
                    {form.structural_form && <div><span className="text-muted-foreground">结构形式：</span>{form.structural_form}</div>}
                    <div><span className="text-muted-foreground">建筑高度：</span>{result.building_height} m</div>
                    <div><span className="text-muted-foreground">建筑面积：</span>{result.building_area.toLocaleString()} m²</div>
                    <div><span className="text-muted-foreground">地上 / 地下：</span>{fa} 层 / {fb} 层</div>
                    <div><span className="text-muted-foreground">建设状态：</span>{form.construction_status}</div>
                    {compSummary && <div className="col-span-2"><span className="text-muted-foreground">防火分区设计：</span>{compSummary}</div>}
                  </div>

                  {result.fire_facilities && result.fire_facilities.length > 0 && (
                    <>
                      <h3 className="mb-2 border-l-[3px] border-destructive pl-2 text-sm font-semibold">二、消防设施配置（{result.fire_facilities.length} 项）</h3>
                      <div className="mb-4 overflow-x-auto">
                        <table className="w-full text-xs border-collapse">
                          <thead><tr className="bg-primary text-primary-foreground"><th className="p-1.5 text-left">优先级</th><th className="p-1.5 text-left">名称</th><th className="p-1.5 text-left">规格</th><th className="p-1.5 text-left">数量</th><th className="p-1.5 text-left">位置</th></tr></thead>
                          <tbody>
                            {result.fire_facilities.map((f, i) => (
                              <tr key={i} className="border-b border-border hover:bg-muted/50"><td className="p-1.5">{priBadge(f.priority)}</td><td className="p-1.5">{f.name}</td><td className="p-1.5">{f.specification}</td><td className="p-1.5">{f.quantity_or_coverage}</td><td className="p-1.5">{f.installation_location}</td></tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  )}

                  {result.building_materials && result.building_materials.length > 0 && (
                    <>
                      <h3 className="mb-2 border-l-[3px] border-warning pl-2 text-sm font-semibold">三、建筑材料要求（{result.building_materials.length} 项）</h3>
                      <div className="mb-4 overflow-x-auto">
                        <table className="w-full text-xs border-collapse">
                          <thead><tr className="bg-primary text-primary-foreground"><th className="p-1.5 text-left">优先级</th><th className="p-1.5 text-left">名称</th><th className="p-1.5 text-left">规格</th><th className="p-1.5 text-left">应用部位</th></tr></thead>
                          <tbody>
                            {result.building_materials.map((m, i) => (
                              <tr key={i} className="border-b border-border hover:bg-muted/50"><td className="p-1.5">{priBadge(m.priority)}</td><td className="p-1.5">{m.name}</td><td className="p-1.5">{m.specification}</td><td className="p-1.5">{m.application_location}</td></tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  )}

                  {result.safety_preparations && result.safety_preparations.length > 0 && (
                    <>
                      <h3 className="mb-2 border-l-[3px] border-primary pl-2 text-sm font-semibold">四、消防安全管理（{result.safety_preparations.length} 项）</h3>
                      <div className="overflow-x-auto">
                        <table className="w-full text-xs border-collapse">
                          <thead><tr className="bg-primary text-primary-foreground"><th className="p-1.5 text-left">优先级</th><th className="p-1.5 text-left">名称</th><th className="p-1.5 text-left">具体要求</th></tr></thead>
                          <tbody>
                            {result.safety_preparations.map((s, i) => (
                              <tr key={i} className="border-b border-border hover:bg-muted/50"><td className="p-1.5">{priBadge(s.priority)}</td><td className="p-1.5">{s.name}</td><td className="p-1.5">{s.requirement}</td></tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </PlatformShell>
  );
}
