import { useState } from 'react';
import { CalendarRange, ChevronDown } from 'lucide-react';
import { Popover } from '@base-ui/react/popover';
import { DayPicker, type DateRange } from 'react-day-picker';
import { zhCN } from 'date-fns/locale';
import 'react-day-picker/style.css';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const pad = (n: number) => String(n).padStart(2, '0');
const mmdd = (d: Date) => `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function triggerLabel(range?: DateRange): string {
  if (!range?.from) return '全部时间';
  if (!range.to) return `${mmdd(range.from)} 起`;
  if (mmdd(range.from) === mmdd(range.to)) return mmdd(range.from);
  return `${mmdd(range.from)} ~ ${mmdd(range.to)}`;
}

/**
 * 日期区间筛选（起止）。触发器样式对齐 `FilterSelect`，弹层用 base-ui Popover 承载
 * react-day-picker 的双月区间日历；选中值以 `DateRange` 交回调用方，由调用方格式化成
 * `YYYY-MM-DD` 发给后端（后端按期号日期区间过滤）。
 */
export function DateRangeFilter({
  value, onChange,
}: {
  value: DateRange | undefined;
  onChange: (range: DateRange | undefined) => void;
}) {
  const [open, setOpen] = useState(false);
  const active = Boolean(value?.from);
  const today = new Date();

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger
        aria-label="按日期区间筛选"
        className={cn(
          'flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-sm transition-all outline-none',
          active
            ? 'border-primary/40 bg-primary/5 text-foreground'
            : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
        )}
      >
        <CalendarRange className="size-4" />
        <span className="tabular-nums">{triggerLabel(value)}</span>
        <ChevronDown className="size-3.5 opacity-70" />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner sideOffset={6} align="start" className="z-50">
          <Popover.Popup className="max-w-[calc(100vw-2rem)] overflow-x-auto rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-lg outline-none">
            <DayPicker
              mode="range"
              numberOfMonths={2}
              showOutsideDays
              locale={zhCN}
              weekStartsOn={1}
              selected={value}
              defaultMonth={value?.from}
              disabled={{ after: today }}
              onSelect={(next: DateRange | undefined) => onChange(next)}
            />
            <div className="mt-2 flex items-center justify-between gap-3 border-t border-border pt-2">
              <span className="text-xs tabular-nums text-muted-foreground">
                {triggerLabel(value)}
              </span>
              <Button
                size="xs"
                variant="ghost"
                disabled={!active}
                onClick={() => onChange(undefined)}
              >
                清除
              </Button>
            </div>
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
