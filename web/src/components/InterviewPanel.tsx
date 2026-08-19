import { useState } from 'react';
import { ArrowLeft, ArrowRight, ClipboardList, MessageSquare, Sparkles, Type } from 'lucide-react';
import type { InterviewAnswers, InterviewQuestion } from '../types';
import { useChatStore } from '../stores/chatStore';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface Props {
  questions: InterviewQuestion[];
}

// 需求访谈面板（Claude Code AskUserQuestion 风格）：
// 每次展示一个问题，选项列表（推荐项高亮），底部三个固定动作每页都在；
// 多问题分页，作答后自动进入下一题，末题作答即提交 → 后端生成计划。
export default function InterviewPanel({ questions }: Props) {
  const submitInterviewAnswers = useChatStore((s) => s.submitInterviewAnswers);
  const skipInterview = useChatStore((s) => s.skipInterview);
  const chatAboutThis = useChatStore((s) => s.chatAboutThis);

  const [page, setPage] = useState(0);
  const [answers, setAnswers] = useState<InterviewAnswers>({});
  const [customMode, setCustomMode] = useState(false);
  const [customText, setCustomText] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const q = questions[page];
  const total = questions.length;
  const isLast = page === total - 1;

  const submit = (finalAnswers: InterviewAnswers) => {
    setSubmitted(true);
    submitInterviewAnswers(finalAnswers);
  };

  // 选中一个选项：记录答案；非末题自动下一页，末题提交
  const pick = (value: string) => {
    const next = { ...answers, [q.id]: value };
    setAnswers(next);
    setCustomMode(false);
    if (isLast) submit(next);
    else setPage((p) => p + 1);
  };

  // 自定义回答确认（Enter 或「确定」）
  const confirmCustom = () => {
    const val = customText.trim();
    if (!val) return;
    const next = { ...answers, [q.id]: val };
    setAnswers(next);
    setCustomText('');
    setCustomMode(false);
    if (isLast) submit(next);
    else setPage((p) => p + 1);
  };

  if (submitted) {
    return (
      <div className="my-2 rounded-lg border border-primary/30 bg-primary/5 p-3 text-sm text-muted-foreground">
        需求已确认，正在生成计划…
      </div>
    );
  }

  return (
    <div className="my-2 rounded-lg border border-primary/30 bg-primary/5 p-3">
      <div className="mb-2 flex items-center gap-2">
        <ClipboardList className="size-4 shrink-0 text-primary" />
        <span className="text-sm font-semibold">确认需求</span>
        {total > 1 && (
          <span className="ml-auto text-xs text-muted-foreground">
            第 {page + 1} / {total} 题
          </span>
        )}
      </div>

      <p className="mb-2 text-sm">{q.question}</p>

      {!customMode ? (
        <div className="flex flex-col gap-1.5">
          {q.options.map((opt) => {
            const selected = answers[q.id] === opt.value;
            return (
              <button
                key={opt.value}
                onClick={() => pick(opt.value)}
                className={cn(
                  'flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors',
                  selected
                    ? 'border-primary bg-primary/10'
                    : 'border-border bg-card hover:border-primary/50',
                )}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {opt.recommended && (
                    <span className="inline-flex shrink-0 items-center gap-1 rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                      <Sparkles className="size-3" /> 推荐
                    </span>
                  )}
                  <span className="truncate">{opt.label}</span>
                </span>
                {opt.value !== opt.label && (
                  <span className="shrink-0 text-xs text-muted-foreground">{opt.value}</span>
                )}
              </button>
            );
          })}
        </div>
      ) : (
        <div className="flex gap-2">
          <input
            autoFocus
            value={customText}
            onChange={(e) => setCustomText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') confirmCustom();
            }}
            placeholder="输入你的回答…"
            className="flex-1 rounded-md border border-border bg-card px-2.5 py-2 text-sm outline-none focus:border-ring"
          />
          <Button size="sm" onClick={confirmCustom} disabled={!customText.trim()}>
            确定
          </Button>
        </div>
      )}

      {/* 固定三动作（每页都在） */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => setCustomMode((v) => !v)}>
          <Type className="mr-1 size-3.5" />
          输入自定义回答
        </Button>
        <Button size="sm" variant="outline" onClick={() => chatAboutThis()}>
          <MessageSquare className="mr-1 size-3.5" />
          聊点别的
        </Button>
        <Button size="sm" variant="ghost" onClick={() => skipInterview()}>
          跳过提问，直接计划
        </Button>
      </div>

      {/* 分页导航 */}
      <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
        {page > 0 && (
          <button
            onClick={() => {
              setPage((p) => p - 1);
              setCustomMode(false);
            }}
            className="inline-flex items-center gap-1 hover:text-foreground"
          >
            <ArrowLeft className="size-3.5" /> 上一步
          </button>
        )}
        {!isLast && (
          <button
            onClick={() => {
              setPage((p) => p + 1);
              setCustomMode(false);
            }}
            className="ml-auto inline-flex items-center gap-1 hover:text-foreground"
          >
            下一题 <ArrowRight className="size-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
