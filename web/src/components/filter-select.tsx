import { Check, ChevronDown, Layers } from 'lucide-react';
import { Menu } from '@base-ui/react/menu';
import { cn } from '@/lib/utils';

export interface Opt {
  value: string | number;
  label: string;
}

/**
 * 统一样式的下拉（与对话输入栏同款：base-ui Menu，不是原生 select）。
 * 分析报告页与不合规检查页共用；`active` 表示偏离默认值（空串 / 0）。
 */
export function FilterSelect({
  icon: Icon, value, options, onChange, ariaLabel, className,
}: {
  icon?: typeof Layers;
  value: string | number;
  options: Opt[];
  onChange: (v: string | number) => void;
  ariaLabel: string;
  /** 只用于补齐调用方的控件高度（如整屏看板工具条统一 h-8）；不传时样式不变 */
  className?: string;
}) {
  const current = options.find((o) => o.value === value) ?? options[0];
  const active = current.value !== '' && current.value !== 0;
  return (
    <Menu.Root>
      <Menu.Trigger
        aria-label={ariaLabel}
        className={cn(
          'flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-sm transition-all outline-none',
          active
            ? 'border-primary/40 bg-primary/5 text-foreground'
            : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
          className,
        )}
      >
        {Icon && <Icon className="size-4" />}
        <span>{current.label}</span>
        <ChevronDown className="size-3.5 opacity-70" />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner sideOffset={6} align="start" className="z-[70]">
          <Menu.Popup className="max-h-80 min-w-44 overflow-y-auto rounded-lg border border-border bg-popover p-1 shadow-lg outline-none">
            {options.map((o) => (
              <Menu.Item
                key={String(o.value)}
                onClick={() => onChange(o.value)}
                className="flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-1.5 text-sm outline-none data-highlighted:bg-muted"
              >
                <Check className={cn('size-3.5', o.value === value ? 'opacity-100' : 'opacity-0')} />
                <span>{o.label}</span>
              </Menu.Item>
            ))}
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
