"""命令行：出某一班次的不合规检查表（**只读 DB，不写 Mongo**）。

```bash
# 最近一个已结束的班次
python -m service.scheduled_task.compliance.cli --out /tmp/compliance.xlsx
# 指定班次与日期（日期 = 班次**结束**那天）
python -m service.scheduled_task.compliance.cli --shift night --date 2026-10-27 \
  --out /tmp/compliance_night.xlsx
```

用途：上线前在真实数据上核对四项口径（页面上线后暂无定时产出）。
"""

import argparse
import logging
from datetime import datetime

from service.scheduled_task.compliance import checks, xlsx


def _window(args) -> dict:
  if not args.shift:
    return checks.shift_window()
  end_day = (datetime.strptime(args.date, '%Y-%m-%d').date()
             if args.date else checks.latest_end_day(args.shift))
  return checks.shift_window_for(args.shift, end_day)


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description='运营监控中心告警不合规检查')
  parser.add_argument('--shift', choices=('night', 'day'),
                      help='班次；缺省取最近一个已结束的班次')
  parser.add_argument('--date', help='班次**结束**日期 YYYY-MM-DD（配合 --shift）')
  parser.add_argument('--out', help='输出 xlsx 路径（缺省 /tmp/compliance_<key>.xlsx）')
  args = parser.parse_args(argv)
  logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

  window = _window(args)
  try:
    result = checks.run_all(window['start_s'], window['end_s'])
  except RuntimeError as e:
    raise SystemExit(str(e))

  out = args.out or f"/tmp/compliance_{window['key']}.xlsx"
  title = f"{checks.TITLE} · {window['label']}"
  path = xlsx.write_workbook(out, result, title=title, period=window['label'])
  total = sum(item['rows'] for item in result['summary'])
  print(f"{window['label']}：命中 {total} 条 → {path}")
  for item in result['summary']:
    print(f"  {item['name']}: {item['rows']}")


if __name__ == '__main__':
  main()
