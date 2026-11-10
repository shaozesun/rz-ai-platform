"""「平台每月所需更新文件目录（修订版）」的 36 条清单。

来源：PDF 单页 36 条（25 月度 / 4 实时 / 3 年度 / 1 年底 / 1 周 / 1 季度 / 1 半年度）。
本功能**只判定月度项**（`MONTHLY_ITEMS`，25 条），其余保留在清单里只为完整对照
（页面/工具可以说明「不在本功能范围」），不参与取数与判定。

字段说明：
- `numbers`：文档名开头的编号前缀。PDF 里 `1.6.6.1-1.6.6.3` 是区间，展开成三条；
  `1.7.1.6全量图纸、1.7.1.7全量设备资料` 是并列两条。
- `keywords`：同名不同表的消歧词（`4.1.5` 有四种隐患排查表）。**任一命中即可**，
  留空表示不按名称再过滤。
- `folders`：真实共享树里见到的目录/文件名（带前导编号，按实测登记）。PDF 里的清单名只是
  模糊叫法，**实际名称优先匹配**，`numbers` 作兜底。发现新写法只改这张表，不动匹配逻辑。
- `note`：PDF 的第三列（线上更新 / PDF / PPT / 文档），只作展示，不参与判定。
"""

import re
from dataclasses import dataclass

CADENCE_MONTHLY = '月度'
IN_SCOPE_CADENCES: tuple[str, ...] = (CADENCE_MONTHLY,)


@dataclass(frozen=True)
class DocItem:
  seq: int
  numbers: tuple[str, ...]
  name: str
  cadence: str
  note: str = ''
  keywords: tuple[str, ...] = ()
  folders: tuple[str, ...] = ()

  @property
  def in_scope(self) -> bool:
    return self.cadence in IN_SCOPE_CADENCES

  @property
  def number_label(self) -> str:
    return '-'.join(self.numbers)


CATALOG: tuple[DocItem, ...] = (
  DocItem(1, ('1.2.2',), '负载管理统计分析表', '月度', '线上更新'),
  # 实测同一份月报在 A1 树里有三处写法，1.4.5 那份最全（只登记编号会漏掉另两处）
  DocItem(2, ('1.4.5',), '能源月报', '月度', 'PPT', ('能源',), (
    '1.4.5能源管理报告', '1.2.3容量管理报告', '8.1.2.3能源管理月报',
  )),
  DocItem(3, ('1.6.3.2',), '高低压配电柜继保整定表', '月度', '线上更新'),
  DocItem(4, ('1.6.3.3',), '动环报警阈值管理表', '月度', '线上更新'),
  DocItem(5, ('1.6.4',), '验证测试管理', '实时', '新项目相关测试文件需上传'),
  DocItem(6, ('1.6.6.1', '1.6.6.2', '1.6.6.3'), '供配电程序文档目录', '月度', '线上更新'),
  DocItem(7, ('1.7.1.6', '1.7.1.7'), '全量图纸与全量设备资料', '实时', '线上更新'),
  DocItem(8, ('2.2.3',), '设备配置状态表', '实时', '线上更新'),
  DocItem(9, ('2.2.4',), '设备操作记录表', '年度', 'PDF'),
  DocItem(10, ('2.2.5',), '灭火器检查卡', '年度', 'PDF'),
  DocItem(11, ('2.2.6',), '电池更换统计表', '实时', '线上更新'),
  DocItem(12, ('2.2.8',), '视频监控录像查询记录单', '月度', 'PDF'),
  DocItem(13, ('2.2.9',), '视频监控录像拷贝记录单', '月度', 'PDF'),
  DocItem(14, ('2.2.10',), '应急PDU月检记录表', '年底', 'PDF'),
  DocItem(15, ('2.3.2.2',), '应急卡使用登记表（中控室）', '月度', 'PDF'),
  DocItem(16, ('2.3.2.3',), '钥匙使用登记表（中控室）', '月度', 'PDF'),
  DocItem(17, ('2.3.2.4',), '窗户封条编号及更换登记表', '月度', 'PDF'),
  DocItem(18, ('2.3.2.5',), '打火机使用登记表（中控室）', '月度', 'PDF'),
  DocItem(19, ('2.3.2.6',), '客户IT库房应急卡领取记录表', '月度', 'PDF'),
  DocItem(20, ('2.3.3.2',), '交接班签字表（中控室）', '月度', 'PDF'),
  DocItem(21, ('2.3.4.4',), '门禁巡检报警记录表', '月度', 'PDF'),
  DocItem(22, ('2.3.4.6',), '值班巡检记录表（部长 工程师 班长）', '月度', 'PDF'),
  DocItem(23, ('2.8.4',), '物品使用登记表（中控室）', '月度', 'PDF'),
  DocItem(24, ('2.10.5',), '卫生清洁记录表（保洁、技术员）', '月度', 'PDF'),
  DocItem(25, ('3.8.5',), '应急卡使用核查记录表', '周', '文档'),
  DocItem(26, ('3.8.6',), '门禁权限核查记录表', '季度', '文档'),
  DocItem(27, ('3.8.7',), '货物运输通道使用记录单', '月度', 'PDF'),
  DocItem(28, ('4.1.6.5',), '电伴热安全检查表', '月度', 'PDF'),
  DocItem(29, ('4.1.5',), '供配电隐患排查表', '月度', 'PDF', ('供配电',)),
  DocItem(30, ('4.1.5',), '制冷隐患排查表', '月度', 'PDF', ('制冷',)),
  DocItem(31, ('4.1.5',), '智能化隐患排查表', '月度', 'PDF', ('智能化',)),
  DocItem(32, ('4.1.5',), '班组级隐患排查记录表', '月度', 'PDF', ('班组',)),
  DocItem(33, ('4.1.6',), '梯子季度专项安全检查表', '年度', 'PDF', ('梯子',)),
  DocItem(34, ('6.3.1',), '人员排班计划表', '月度', '线上更新'),
  DocItem(35, ('6.3.2',), '考勤统计表', '月度', '线上更新'),
  DocItem(36, ('7.6.4',), '供应商考核信息统计表', '半年度', '线上更新'),
)

MONTHLY_ITEMS: tuple[DocItem, ...] = tuple(i for i in CATALOG if i.in_scope)


def summary() -> dict:
  """清单概览（页面/工具说明用，避免两处各写一份数字）。"""
  counts: dict[str, int] = {}
  for item in CATALOG:
    counts[item.cadence] = counts.get(item.cadence, 0) + 1
  return {
    'total': len(CATALOG),
    'in_scope': len(MONTHLY_ITEMS),
    'cadence_counts': counts,
  }


def _norm_text(text: str) -> str:
  """去所有空白（含全角空格）+ 转小写，供名称匹配。"""
  return ''.join((text or '').split()).lower()


_LEADING_NUMBER = re.compile(r'^\s*\d+(?:\.\d+)*')


def _alias_names(item: DocItem) -> tuple[str, ...]:
  """真实目录名去掉前导编号后的业务名（`1.4.5能源管理报告` → `能源管理报告`）。"""
  out = []
  for folder in item.folders:
    name = _norm_text(_LEADING_NUMBER.sub('', folder, count=1))
    if name:
      out.append(name)
  return tuple(out)


def resolve_items(query: str) -> list[DocItem]:
  """把用户说的「文件名」解析到清单项（只查 25 条月度项）。

  匹配有优先级，命中即返回：① 编号（`1.6.6.2` / `4.1.5制冷` 这类以编号开头的）
  ② 清单项名互相包含（`负载管理统计分析表`、真实上传文件名里也含它），
  **实际目录名也算**（`能源管理报告` → 能源月报）
  ③ 消歧关键词（`4.1.5` 四条靠 供配电/制冷/智能化/班组）。

  多义（「隐患排查表」「中控室」）返回多项，由调用方让用户指明；未命中返回 `[]`。
  """
  q = _norm_text(query)
  if not q:
    return []

  hits = [
    item for item in MONTHLY_ITEMS
    if any(q == n or q.startswith(f'{n}.') for n in item.numbers)
  ]
  if hits:
    return hits

  hits = [
    item for item in MONTHLY_ITEMS
    if q in _norm_text(item.name) or _norm_text(item.name) in q
    or any(q in a or a in q for a in _alias_names(item))
  ]
  if hits:
    return hits

  return [item for item in MONTHLY_ITEMS
          if any(_norm_text(k) in q for k in item.keywords)]
