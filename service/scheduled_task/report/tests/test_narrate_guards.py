"""解读层两道闸的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_narrate_guards`

覆盖：

1. **name_guard（机构名审计，只记日志）** —— 抽取 `X分部` / `X园区` / `X数据中心` /
   `X部` 形态的片段，与本章下发载荷里出现过的机构名比对，命中的由 `narrate.py`
   打 warning 留档，**不删句、不丢条目、不置空**。2026-10-30 撤下删除行为：起因事件
   （2026-10-29 园区级变更周报第 12 章 P0 的「B1M4 分部」「技术保障部」）复核后是误杀，
   两个名字都是该数据集里的真实部门，只是没进前 12 行表；删句在 24 份报告里误删 55 句
   真部门论述。故这里的断言只测**抽取与比对**的准确性，不测处置动作。
2. **meta_guard（缺数据 → 无法判断）** —— 同一句同时含缺失标记与推断标记才丢弃；
   只说「数据未提供 X」不带推断、或只说「无法评估」不带缺失标记，一律放行。

**不依赖任何外部数据/网络**。
"""

from service.scheduled_task.report import meta_guard, name_guard
from service.scheduled_task.report.narrate import _validate_section


# ---------------- name_guard ----------------

def test_tokens_extracts_org_names():
  got = name_guard.tokens('A1分部与 B1M4分部、惠州A3分部、技术保障部均有滞留')
  assert {'A1分部', 'B1M4分部', '惠州A3分部', '技术保障部'} <= got, got


def test_tokens_skips_generic_prose_park_phrases():
  """纯中文「…园区」是泛称，不进 token —— 2026-10-30 实测一次生成误伤 4 句。

  「与其他园区」「单一园区」「无法展开园区」「但该园区」都不是机构名。
  """
  got = name_guard.tokens(
    '各园区级别构成数据缺失，无法对比廊坊园区与其他园区；按规则转为部门维度，'
    '共覆盖 12 个主要部门；无法展开园区间差异对比，本章仅基于单一园区统计范围。')
  assert not got, got


def test_tokens_skips_generic_words():
  """部门/部分/内部/各园区 这类通用词不是机构名（贪婪截取也不能算进去）。"""
  got = name_guard.tokens('各部门与部分专业在公司内部对比，各园区表现接近')
  assert not got, got


def test_tokens_tolerates_spaced_org_name():
  """2026-10-30 三份全国月报各误杀 3 句：模型写「A2 分部」，载荷是「A2分部」。"""
  got = name_guard.tokens('M2-304 机房单点高温 30 单，全部集中在 A2 分部')
  assert got == {'A2分部'}, got
  assert name_guard.violations('全部集中在 A2 分部', {'A2分部'}) == []
  assert name_guard.violations('全部集中在 A3 分部', {'A2分部'}) == ['A3分部']


def test_tokens_skips_bu_tail_prose():
  """「表明该分部」「反映两分部」「局部聚集」不是机构名。

  `部(?!分)` 只管后一个字，管不住把「分」吃进 body 的截法（该分部 → 表明该分 + 部）。
  """
  got = name_guard.tokens('A2 分部包揽前三，表明该分部机房整体存在散热问题')
  assert got == {'A2分部'}, got          # 只认真名，不把「表明该分部」当机构
  for text in ('与前一章形成差异，反映两分部故障表现不同',
               '头部几类集中在少数机房，说明风险分布呈现明显的局部聚集特征'):
    assert not name_guard.tokens(text), (text, name_guard.tokens(text))


def test_tokens_skips_sentence_shaped_park_tokens():
  """「66单占该园区」这类带量词/指点词的截取是句子，不是机构名。

  2026-10-30 全国事件周报 14 期共误杀 17 句，其中 8 句属于这一类。
  """
  for text in ('制冷专业 66单占该园区其他专业之和',
               '单点高温 49单远超该园区其余机房',
               '11单而其余四园区合计不足 3 单',
               '仅发生 1 级事件共 2 起，3.9% 的占比说明 1级事件的园区分布集中'):
    assert not name_guard.tokens(text), (text, name_guard.tokens(text))


def test_violations_allows_connective_joined_real_names():
  """「A2 及 A3 分部」两个真名连写：拆开后逐段比对，不该判编造。"""
  allowed = {'A2分部', 'A3分部'}
  assert name_guard.violations('A2及A3分部重复出现', allowed) == []
  # 拆开后有一段不在白名单 → 仍要拦（不能因为能拆就放行）
  bad = name_guard.violations('A2及B1M4分部', {'A2分部'})
  assert bad, bad


def test_allowed_from_payload_walks_nested():
  payload = {'本章数据': {'部门分布': [{'部门': 'A1分部'}, {'部门': '技术保障部'}],
                     '说明': {'园区': 'A3数据中心'}}}
  allowed = name_guard.allowed_from_payload(payload)
  assert {'A1分部', '技术保障部', 'A3数据中心'} <= allowed, allowed


def test_violations_flags_name_absent_from_payload():
  allowed = name_guard.allowed_from_payload({'部门分布': [{'部门': 'A1分部'}]})
  bad = name_guard.violations('重点解决 B1M4分部、A1分部及技术保障部的滞留问题', allowed)
  assert 'B1M4分部' in bad and '技术保障部' in bad, bad
  assert 'A1分部' not in bad, bad


def test_violations_allows_over_capture_suffix():
  """「由安全技术保障部」这类过度截取，只要以白名单名字结尾就放行。"""
  allowed = {'技术保障部'}
  assert name_guard.violations('由安全技术保障部牵头处置', allowed) == []
  # 反过来：白名单是「保障部」时，「技术保障部」也算合法（后缀匹配）
  assert name_guard.violations('技术保障部的单最多', {'保障部'}) == []


def test_filter_texts_drops_only_offending_items():
  allowed = {'A1分部'}
  kept, dropped = name_guard.filter_texts(
    ['A1分部的审批 P50 为 1.9 小时', 'B1M4分部积压最严重'], allowed)
  assert kept == ['A1分部的审批 P50 为 1.9 小时'], kept
  assert dropped and 'B1M4分部' in dropped[0][1], dropped


# ---------------- meta_guard ----------------

def test_meta_guard_drops_missing_then_cannot_conclude():
  text = '本章缺少审批与验收时长的分位数据，无法评估各环节的真实差异。'
  assert meta_guard.violations(text), text


def test_meta_guard_drops_second_real_case():
  text = '数据未提供各园区或各部门的级别构成对比，无法判断是否存在结构性问题。'
  assert meta_guard.violations(text), text


def test_meta_guard_drops_cannot_analyze_variant():
  """2026-10-30 实测漏网句：「无法分析」不在词表里就漏过去了。"""
  text = '由于数据未提供各园区或各部门的级别构成对比，无法分析廊坊园区内部的结构差异。'
  assert meta_guard.violations(text), text


def test_meta_guard_keeps_missing_without_conclusion():
  """只陈述「数据未提供某字段」、不下推断 —— 放行（宁可漏拦，不可误杀）。"""
  assert meta_guard.violations('数据未提供关闭时间戳，全流程口径不含关闭期。') == []


def test_meta_guard_keeps_conclusion_without_missing_marker():
  assert meta_guard.violations('审批 P50 为 1.9 小时，无法评估这一水平是否合理。') == []


def test_meta_guard_filter_reports_dropped_sentence():
  kept, dropped = meta_guard.filter_texts(
    ['紧急变更占比 9.5%，属正常水平。',
     '本章未提供分等级时效，无法比较各等级快慢。'])
  assert kept == ['紧急变更占比 9.5%，属正常水平。'], kept
  assert dropped and '本章未提供分等级时效' in dropped[0][0], dropped


# ---------------- 接入 _validate_section ----------------

def _chapter(text):
  return {'要点': [{'文本': text, '依据': ['F1']}]}


def test_validate_section_keeps_org_outside_payload():
  """载荷外机构名**只记日志、不丢句**（2026-10-30 撤下删除行为，起因事件是误杀）。"""
  data = _chapter('B1M4分部积压最严重，需重点解决。')
  out, dropped = _validate_section(data, [], {'F1'}, require_evidence=True,
                                   allowed_orgs={'A1分部'})
  assert out['要点'] == ['B1M4分部积压最严重，需重点解决。'] and dropped == 0, out


def test_validate_section_drops_meta_complaint():
  data = _chapter('本章缺少审批分位数据，无法评估时效。')
  out, dropped = _validate_section(data, [], {'F1'}, require_evidence=True,
                                   allowed_orgs=set())
  assert out['要点'] == [] and dropped == 1, out


def test_validate_section_keeps_allowed_org():
  data = _chapter('A1分部积压最严重，需重点解决。')
  out, dropped = _validate_section(data, [], {'F1'}, require_evidence=True,
                                   allowed_orgs={'A1分部'})
  assert out['要点'] == ['A1分部积压最严重，需重点解决。'] and dropped == 0, out


def test_validate_section_without_allowed_orgs_skips_guard():
  """`allowed_orgs=None` 表示该调用点没启用这道闸（老调用点行为不变）。"""
  data = _chapter('B1M4分部积压最严重。')
  out, dropped = _validate_section(data, [], {'F1'}, require_evidence=True)
  assert out['要点'] and dropped == 0, out


def main() -> int:
  """无框架跑法：逐个跑测试函数，打印结果并返回退出码。"""
  tests = [(n, f) for n, f in sorted(globals().items())
           if n.startswith('test_') and callable(f)]
  failed = []
  for name, fn in tests:
    try:
      fn()
      print(f'  OK    {name}')
    except AssertionError as e:
      failed.append(name)
      print(f'  FAIL  {name}: {e}')
    except Exception as e:                        # noqa: BLE001 — 自检脚本要报告一切异常
      failed.append(name)
      print(f'  ERROR {name}: {type(e).__name__}: {e}')
  print(f'\n{len(tests) - len(failed)}/{len(tests)} 通过'
        + (f'，失败：{failed}' if failed else ''))
  return 1 if failed else 0


if __name__ == '__main__':
  raise SystemExit(main())
