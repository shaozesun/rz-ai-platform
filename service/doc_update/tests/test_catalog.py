"""文件名解析自检（纯 assert、不连库）：编号 / 清单项名 / 消歧关键词三级匹配。

跑法：`python -m service.doc_update.tests.test_catalog`
"""

from service.doc_update.catalog import MONTHLY_ITEMS, resolve_items


def _seqs(query: str) -> list[int]:
  return [i.seq for i in resolve_items(query)]


def test_exact_name():
  # 用户按文件名问（不是编号）→ 唯一命中
  assert _seqs('负载管理统计分析表') == [1]


def test_partial_name_with_brackets():
  # 清单项名带「（中控室）」，用户只念前半段也要命中
  assert _seqs('应急卡使用登记表') == [15]


def test_number_with_keyword():
  # 「编号 + 消歧词」→ 四条 4.1.5 里唯一的一条
  assert _seqs('4.1.5制冷') == [30]
  assert _seqs('4.1.5 制冷') == [30], '全角/半角空格都要能归一'


def test_ambiguous_returns_multiple():
  # 多义不猜：交给调用方让用户指明。班组级叫「隐患排查记录表」、不含「隐患排查表」
  # 四字，需念全名才命中，故这里只有 3 条
  assert _seqs('隐患排查表') == [29, 30, 31]


def test_number_prefix_only():
  # 编号匹配：一条清单项可展开多个前缀（1.6.6.1-1.6.6.3 是同一项）
  assert _seqs('1.6.6.2') == [6]
  assert _seqs('1.6.6.2供配电程序文档目录') == [6]


def test_real_uploaded_filename():
  # 真实上传文件名里含清单项名 → 能命中
  assert _seqs('1.2.2润泽科技-廊坊-AX-负载管理统计分析表(1).xlsx') == [1]


def test_actual_folder_name():
  # 清单名只是模糊叫法：真实目录名（含/不含编号）都应解析到同一项
  assert _seqs('能源管理报告') == [2]
  assert _seqs('能源管理月报') == [2]
  assert _seqs('1.4.5能源管理报告（能源月报同步）') == [2]


def test_unknown_returns_empty():
  assert _seqs('三元催化转化器说明书') == []
  assert _seqs('   ') == []


def test_only_monthly_scope():
  # 周/季度等不在范围：即使名字完全对上也不返回（避免去刷范围外的项）
  assert _seqs('应急卡使用核查记录表') == []
  assert all(i.in_scope for i in MONTHLY_ITEMS)


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
