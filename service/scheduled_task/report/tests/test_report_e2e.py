"""报告端到端回归闸：真实数据集 → 统计 → 事实 → HTML，比对上一次确认过的快照。

跑法（仓库根）：
  python -m service.scheduled_task.report.tests.test_report_e2e            # 比对
  python -m service.scheduled_task.report.tests.test_report_e2e --update   # 重建快照

**为什么需要**：其余自检都是组件级（各 compute 的纯函数断言），没有一条覆盖整条确定性链。
2026-10-15 日趋势轴线错位、2026-10-23 跨期比较算错，都发生在这条链上，只能靠真跑报告发现。

**样本来源**：`data/analysis/*/*-report-*/`（`data/` 已在 .gitignore，故快照也只留在本机）。
快照钉住具体数据集路径（`source`）；源不存在才退回同类最新，否则每产一份新报告就会换样本。
没有本地数据集时全部 SKIP 且退出码 0 —— 它是运维回归闸，不是 CI 用例。

**不比 PNG 字节**：matplotlib 版本差异会让同一份数据画出不同的 PNG，比哈希必然误报。
只比图名集合（出几张、叫什么），数据正确性由 `stats` 快照保证。
"""

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from service.scheduled_task.report.facts import build_facts
from service.scheduled_task.report.render import build_html, run_compute
from service.scheduled_task.report.spec import get_report_spec
from service.scheduled_task.report_templates import PERIODS

TICKETS = ('event', 'breakdown', 'change', 'question', 'threshold')
_REPORT_DIR = Path(__file__).resolve().parents[1]          # service/scheduled_task/report
_REPO_ROOT = _REPORT_DIR.parents[2]
_ANALYSIS = _REPO_ROOT / 'data' / 'analysis'
_GOLDEN = _ANALYSIS / '__golden__'
_FIXED_TS = '1970-01-01 00:00'      # HTML 里的生成时间，固定后哈希才可比
_MAX_DIFFS = 5


def _candidates(ticket: str) -> list[Path]:
  """同类报告的数据集目录（新→旧）；要求 data.csv + window.json 都在。"""
  out = []
  for win in _ANALYSIS.glob(f'*/*-report-*/window.json'):
    ds_dir = win.parent
    if ds_dir.name.split('-report-')[0] != ticket:
      continue
    if not (ds_dir / 'data.csv').is_file():
      continue
    out.append((win.stat().st_mtime, ds_dir))
  return [d for _, d in sorted(out, reverse=True)]


def _pick_source(ticket: str, golden: dict | None) -> Path | None:
  """优先用快照钉住的数据集；它不在了（容器重建/清理）才退回同类最新。"""
  pinned = (golden or {}).get('source')
  if pinned:
    cand = _REPO_ROOT / pinned
    if (cand / 'data.csv').is_file() and (cand / 'window.json').is_file():
      return cand
  cands = _candidates(ticket)
  return cands[0] if cands else None


def _snapshot(ticket: str, ds_dir: Path) -> dict:
  """在临时目录里跑统计（不污染源数据集），再生成事实与 HTML 快照。"""
  spec = get_report_spec(f'{ticket}__x__weekly')
  assert spec is not None, ticket
  with tempfile.TemporaryDirectory(prefix=f'rz-e2e-{ticket}-') as tmp:
    tmp = Path(tmp)
    shutil.copy(ds_dir / 'data.csv', tmp / 'data.csv')
    if (ds_dir / 'prev.csv').is_file():
      shutil.copy(ds_dir / 'prev.csv', tmp / 'prev.csv')
    shutil.copy(ds_dir / 'window.json', tmp / 'window.json')
    stats = asyncio.run(run_compute(tmp, _REPORT_DIR / f'{ticket}_compute.py'))
    charts = sorted(p.stem for p in (tmp / 'charts').glob('*.png')) \
      if (tmp / 'charts').is_dir() else []

  window = json.loads((ds_dir / 'window.json').read_text(encoding='utf-8'))
  period = window.get('period') or 'monthly'
  html = build_html(stats, {}, {}, spec, set(charts), generated_at=_FIXED_TS,
                    scope_label=str(window.get('scope') or ''),
                    period_label=PERIODS[period]['label'])
  return {
    'source': str(ds_dir.relative_to(_REPO_ROOT)),
    'stats': stats,
    'facts': {role: build_facts(stats, spec, role) for role in spec.roles},
    'charts': charts,
    'html_sha256': hashlib.sha256(html.encode('utf-8')).hexdigest(),
  }


def _diff(got, want, path='$', out=None) -> list[str]:
  """返回前 `_MAX_DIFFS` 条差异（`路径: 快照值 / 实际值`，路径形如 `$.stats.specialty[1]`）。"""
  if out is None:
    out = []
  if len(out) >= _MAX_DIFFS:
    return out
  if isinstance(want, dict) and isinstance(got, dict):
    for k in list(want) + [k for k in got if k not in want]:
      if len(out) >= _MAX_DIFFS:
        break
      if k not in got:
        out.append(f'{path}.{k}: 快照有、实际缺失')
      elif k not in want:
        out.append(f'{path}.{k}: 实际多出 {_short(got[k])}')
      else:
        _diff(got[k], want[k], f'{path}.{k}', out)
  elif isinstance(want, list) and isinstance(got, list):
    if len(want) != len(got):
      out.append(f'{path}: 长度 快照 {len(want)} / 实际 {len(got)}')
    for i in range(min(len(want), len(got))):
      if len(out) >= _MAX_DIFFS:
        break
      _diff(got[i], want[i], f'{path}[{i}]', out)
  elif got != want:
    out.append(f'{path}: 快照 {_short(want)} / 实际 {_short(got)}')
  return out


def _short(v) -> str:
  """差异里截断长值，避免整段 HTML/事实刷屏。"""
  s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
  return s if len(s) <= 80 else s[:77] + '...'


def main(argv: list[str]) -> int:
  update = '--update' in argv
  _GOLDEN.mkdir(parents=True, exist_ok=True)
  failed, ran, skipped = [], 0, 0

  for ticket in TICKETS:
    golden_path = _GOLDEN / f'{ticket}.json'
    golden = json.loads(golden_path.read_text(encoding='utf-8')) \
      if golden_path.is_file() else None
    src = _pick_source(ticket, golden)
    if src is None:
      skipped += 1
      print(f'  SKIP  {ticket}: data/analysis 下没有该类报告数据集')
      continue

    print(f'  RUN   {ticket}: {src.relative_to(_REPO_ROOT)}')
    got = _snapshot(ticket, src)
    if update:
      golden_path.write_text(json.dumps(got, ensure_ascii=False, indent=1),
                             encoding='utf-8')
      print(f'  WROTE {golden_path.relative_to(_REPO_ROOT)}')
      ran += 1
      continue
    if golden is None:
      failed.append(ticket)
      print(f'  FAIL  {ticket}: 尚无快照，先跑 --update')
      continue
    diffs = _diff(got, golden)
    if diffs:
      failed.append(ticket)
      print(f'  FAIL  {ticket}: {len(diffs)} 处差异')
      for d in diffs:
        print(f'          {d}')
    else:
      print(f'  OK    {ticket}: stats / facts / charts / html 与快照一致')
      ran += 1

  tail = f'{ran}/{ran + len(failed)} 通过'
  if skipped:
    tail += f'，{skipped} 类无数据集已跳过'
  if failed:
    tail += f'，失败：{failed}（确认改动无误后跑 --update 重建快照）'
  print(f'\n{tail}')
  return 1 if failed else 0


if __name__ == '__main__':
  raise SystemExit(main(sys.argv[1:]))
