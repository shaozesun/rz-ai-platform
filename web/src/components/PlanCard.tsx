import { useState } from 'react';
import { ClipboardList, Plus, Trash2 } from 'lucide-react';
import type { ExecutionPlan, PlanStep } from '../types';
import { useChatStore } from '../stores/chatStore';
import { Button } from '@/components/ui/button';

function emptyStep(): PlanStep {
  return { title: '', description: '' };
}

export default function PlanCard({ plan }: { plan: ExecutionPlan }) {
  const confirmPlan = useChatStore((s) => s.confirmPlan);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const [goal, setGoal] = useState(plan.goal || '');
  const [steps, setSteps] = useState<PlanStep[]>(
    plan.steps?.length ? plan.steps : [emptyStep()],
  );
  const [status, setStatus] = useState<'editing' | 'confirmed' | 'cancelled'>('editing');

  const updateStep = (idx: number, patch: Partial<PlanStep>) => {
    setSteps((prev) => prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)));
  };

  const removeStep = (idx: number) => {
    setSteps((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== idx) : prev));
  };

  const onConfirm = () => {
    if (status !== 'editing' || isStreaming) return;
    const cleaned = steps
      .map((s) => ({ title: s.title.trim(), description: s.description.trim() }))
      .filter((s) => s.title || s.description);
    setStatus('confirmed');
    confirmPlan({ goal: goal.trim(), steps: cleaned });
  };

  const disabled = status !== 'editing' || isStreaming;

  return (
    <div className="my-2 rounded-lg border border-primary/30 bg-primary/5 p-3">
      <div className="mb-2 flex items-center gap-2">
        <ClipboardList className="size-4 shrink-0 text-primary" />
        <span className="text-sm font-semibold">执行计划</span>
        {status === 'confirmed' && (
          <span className="ml-auto text-xs text-muted-foreground">已确认，开始执行…</span>
        )}
        {status === 'cancelled' && (
          <span className="ml-auto text-xs text-muted-foreground">已取消</span>
        )}
      </div>

      {status === 'editing' ? (
        <>
          <input
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="目标"
            className="mb-2 w-full rounded-md border border-border bg-card px-2.5 py-1.5 text-sm outline-none focus:border-ring"
          />
          <div className="space-y-2">
            {steps.map((step, i) => (
              <div key={i} className="flex items-start gap-2">
                <span className="mt-1.5 shrink-0 text-xs font-medium text-muted-foreground">
                  {i + 1}
                </span>
                <div className="flex-1 space-y-1.5">
                  <input
                    value={step.title}
                    onChange={(e) => updateStep(i, { title: e.target.value })}
                    placeholder="步骤标题"
                    className="w-full rounded-md border border-border bg-card px-2.5 py-1 text-sm outline-none focus:border-ring"
                  />
                  <textarea
                    value={step.description}
                    onChange={(e) => updateStep(i, { description: e.target.value })}
                    placeholder="这步做什么 / 查什么 / 产出什么"
                    rows={2}
                    className="w-full resize-none rounded-md border border-border bg-card px-2.5 py-1 text-xs outline-none focus:border-ring"
                  />
                </div>
                <button
                  onClick={() => removeStep(i)}
                  disabled={steps.length <= 1}
                  className="mt-1 shrink-0 rounded p-1 text-muted-foreground hover:text-destructive disabled:opacity-40"
                  title="删除步骤"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
          <button
            onClick={() => setSteps((prev) => [...prev, emptyStep()])}
            className="mt-2 inline-flex items-center gap-1 text-xs text-primary hover:underline"
          >
            <Plus className="size-3.5" /> 添加步骤
          </button>

          <div className="mt-3 flex items-center justify-end gap-2">
            <Button size="sm" variant="outline" onClick={() => setStatus('cancelled')}>
              取消
            </Button>
            <Button size="sm" onClick={onConfirm} disabled={disabled}>
              确认执行
            </Button>
          </div>
        </>
      ) : (
        <ol className="list-decimal space-y-1 pl-5 text-sm">
          {steps.map((step, i) => (
            <li key={i}>
              <span className="font-medium">{step.title}</span>
              {step.description ? `：${step.description}` : ''}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
