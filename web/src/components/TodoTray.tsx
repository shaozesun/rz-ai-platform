import { useState } from 'react';
import { ChevronDown, ListTodo } from 'lucide-react';
import type { Todo } from '../types';
import { cn } from '@/lib/utils';

/** 任务进度圆点：空心=未开始，实心高亮=进行中，变暗=已完成（对齐 DeerFlow 的 QueueItemIndicator）。 */
function TodoDot({ status }: { status: Todo['status'] }) {
  return (
    <span
      className={cn(
        'mt-1 inline-block size-2 shrink-0 rounded-full border',
        status === 'completed' && 'border-muted-foreground/20 bg-muted-foreground/10',
        status === 'pending' && 'border-muted-foreground/50',
        status === 'in_progress' && 'border-primary/60 bg-primary/70',
      )}
    />
  );
}

/**
 * 计划模式的任务清单托盘：贴在输入框正上方，默认折叠，随 write_todos 实时更新。
 * 位置刻意不放进消息流——清单是「进度状态」不是一条对话内容（DeerFlow 同款布局）。
 */
export default function TodoTray({ todos }: { todos: Todo[] }) {
  const [collapsed, setCollapsed] = useState(true);
  if (!todos.length) return null;

  const done = todos.filter((t) => t.status === 'completed').length;

  return (
    <div className="mx-auto w-full max-w-3xl px-3">
      <div className="overflow-hidden rounded-t-lg border border-b-0 border-border bg-card/80">
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent/50"
        >
          <ListTodo className="size-3.5 shrink-0" />
          <span>任务清单</span>
          <span className="tabular-nums">
            {done}/{todos.length}
          </span>
          <ChevronDown
            className={cn('ml-auto size-3.5 transition-transform', !collapsed && 'rotate-180')}
          />
        </button>
        {!collapsed && (
          <ul className="max-h-40 space-y-1 overflow-y-auto px-3 pb-2">
            {todos.map((todo, i) => (
              <li key={`${i}-${todo.content}`} className="flex items-start gap-2 text-xs">
                <TodoDot status={todo.status} />
                <span
                  className={cn(
                    'min-w-0 break-words',
                    todo.status === 'completed'
                      ? 'text-muted-foreground/50 line-through'
                      : 'text-muted-foreground',
                    todo.status === 'in_progress' && 'text-primary/80',
                  )}
                >
                  {todo.content}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
