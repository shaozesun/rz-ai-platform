import { useState } from 'react';
import {
  Lightbulb,
  ChevronDown,
  ChevronUp,
  Sparkles,
  Info,
  Send,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { submitFeedback } from '@/api/stats';

export default function InnovationPage() {
  const [aiExpanded, setAiExpanded] = useState(false);
  const [appExpanded, setAppExpanded] = useState(false);
  const [feedbackText, setFeedbackText] = useState('');
  const [feedbackSending, setFeedbackSending] = useState(false);
  const [feedbackDone, setFeedbackDone] = useState(false);

  const handleSubmitFeedback = async () => {
    if (!feedbackText.trim()) return;
    setFeedbackSending(true);
    try {
      await submitFeedback(feedbackText.trim());
      setFeedbackText('');
      setFeedbackDone(true);
      setTimeout(() => setFeedbackDone(false), 3000);
    } catch {
      // ignore
    } finally {
      setFeedbackSending(false);
    }
  };

  return (
    <PlatformShell
      title="创新空间"
      description="智能平台功能探索与用户反馈"
    >
      <div className="flex flex-col gap-6">
        {/* Notice banner */}
        <div className="flex items-start gap-3 rounded-lg bg-primary/5 border border-primary/15 px-4 py-3">
          <Sparkles className="size-5 shrink-0 mt-0.5 text-primary" />
          <p className="text-sm text-muted-foreground leading-relaxed">
            智能平台的功能正在探索，并非最终版功能，欢迎大家积极体验与反馈
          </p>
        </div>

        {/* Info cards */}
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {/* 了解AI */}
          <div className="rounded-lg border border-border bg-card">
            <button
              onClick={() => setAiExpanded(!aiExpanded)}
              className="flex w-full items-center justify-between px-4 py-3 hover:bg-muted/50 transition-colors rounded-t-lg"
            >
              <div className="flex items-center gap-2">
                <Info className="size-4 text-primary" />
                <span className="font-medium text-sm">了解 AI</span>
              </div>
              {aiExpanded
                ? <ChevronUp className="size-4 text-muted-foreground" />
                : <ChevronDown className="size-4 text-muted-foreground" />}
            </button>
            {aiExpanded && (
              <div className="px-4 pb-4 text-sm text-muted-foreground leading-relaxed space-y-3">
                <div>
                  <p className="font-medium text-foreground mb-1">AI 能做什么</p>
                  <ul className="list-disc pl-4 space-y-1">
                    <li>理解和生成自然语言，进行多轮对话</li>
                    <li>从大量文档中检索信息并给出溯源回答（RAG）</li>
                    <li>识别图像内容，进行视觉分析与比对</li>
                    <li>根据规则和参数自动生成方案与报告</li>
                  </ul>
                </div>
                <div>
                  <p className="font-medium text-foreground mb-1">AI 的局限性</p>
                  <ul className="list-disc pl-4 space-y-1">
                    <li>可能产生"幻觉"，生成看似合理但不准确的内容</li>
                    <li>对专业领域知识理解有限，需要人工审核关键结论</li>
                    <li>图像识别准确率受光照、角度、遮挡等因素影响</li>
                    <li>不具备真正的推理能力，复杂逻辑判断仍需人工介入</li>
                  </ul>
                </div>
                <div>
                  <p className="font-medium text-foreground mb-1">使用建议</p>
                  <ul className="list-disc pl-4 space-y-1">
                    <li>提问尽量具体、明确，避免模糊表述</li>
                    <li>重要结论请结合专业知识进行二次验证</li>
                    <li>上传图片前确保清晰、角度正对目标</li>
                    <li>合理预期 AI 能力边界，善用人工复核</li>
                  </ul>
                </div>
              </div>
            )}
          </div>

          {/* 了解应用 */}
          <div className="rounded-lg border border-border bg-card">
            <button
              onClick={() => setAppExpanded(!appExpanded)}
              className="flex w-full items-center justify-between px-4 py-3 hover:bg-muted/50 transition-colors rounded-t-lg"
            >
              <div className="flex items-center gap-2">
                <Info className="size-4 text-primary" />
                <span className="font-medium text-sm">了解应用</span>
              </div>
              {appExpanded
                ? <ChevronUp className="size-4 text-muted-foreground" />
                : <ChevronDown className="size-4 text-muted-foreground" />}
            </button>
            {appExpanded && (
              <div className="px-4 pb-4 text-sm text-muted-foreground leading-relaxed space-y-4">
                <p>润泽智能化平台整合六大 AI 能力模块，覆盖企业智能化核心场景：</p>

                <div>
                  <p className="font-medium text-foreground mb-1">对话助手</p>
                  <p>基于 RAG（检索增强生成）技术的企业级智能问答系统。用户上传企业文档后，AI 能够从知识库中检索相关内容并生成精准回答，回答可溯源到具体文档和段落。支持流式输出、多轮对话、Markdown 格式呈现，适用于企业知识问答、智能客服、办公协助等场景。</p>
                </div>

                <div>
                  <p className="font-medium text-foreground mb-1">知识库管理</p>
                  <p>文档管理与向量化平台，支持 PDF、Word、Excel、CSV、TXT、Markdown 等 7 种格式的批量上传。系统自动对文档进行切片和向量化处理，建立语义索引。支持按知识库分组管理（如"技术文档"、"产品手册"），所有知识库对平台用户可见和共享。</p>
                </div>

                <div>
                  <p className="font-medium text-foreground mb-1">隐患识别</p>
                  <p>AI 驱动的安全隐患检测系统，采用两阶段检测流水线：Phase 1 由 VLM 视觉大模型进行场景识别，判断上传图片是否为 4 种消防控制柜之一；Phase 2 触发精准比对——优先使用 CV 计算机视觉技术（ORB 特征提取 + FLANN 匹配 + RANSAC 单应矩阵验证 + LAB 色彩空间灯位亮灭分析），CV 失败时自动降级为 VLM 视觉对比。支持批量上传、历史回溯，输出 Markdown + Word 双格式检测报告。</p>
                </div>

                <div>
                  <p className="font-medium text-foreground mb-1">消防配置</p>
                  <p>基于建筑参数自动推荐消防设施配置方案。支持完整模式（13 项建筑参数，包括建筑类型、层数、面积、火灾危险性等级、耐火等级、疏散距离等）、简化模式（仅建筑类型和面积）和快速填充模式（5 种预设场景：高层办公、商业综合体、高层住宅、丙类厂房、地下车库）。输出方案包含设备清单、安装位置建议和合规性说明，可导出 Word 格式报告。</p>
                </div>

                <div>
                  <p className="font-medium text-foreground mb-1">视频生成</p>
                  <p>PPT 自动转解说视频工具。上传 .pptx 文件后，系统自动提取内容 → 生成解说词 → TTS 语音合成 → 图片渲染 → 输出 MP4 视频。全自动流程无需手动编辑，支持批量任务管理，每个任务可单独下载和删除，采用插件化架构便于扩展。</p>
                </div>

                <div>
                  <p className="font-medium text-foreground mb-1">权限中心</p>
                  <p>基于 RBAC（角色访问控制）模型的权限管理系统。用户可查看当前角色和拥有的权限范围，申请更高权限（如知识库上传、风险检测、视频生成等），跟踪申请审批进度。系统预置多种角色：普通用户（AI 对话）、知识库管理员、风险检测用户、风险专家等，管理员可自定义角色和权限。</p>
                </div>

                <p className="text-xs">各功能详情请点击左侧导航菜单进入对应页面查看。</p>
              </div>
            )}
          </div>
        </div>

        {/* Feedback form */}
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center gap-2 mb-3">
              <Lightbulb className="size-4 text-primary" />
              <span className="font-medium text-sm">优化与建议</span>
            </div>
            <Textarea
              placeholder="分享您的想法、优化建议或好的 idea…"
              className="min-h-[80px] resize-none"
              value={feedbackText}
              onChange={(e) => setFeedbackText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  handleSubmitFeedback();
                }
              }}
            />
            <div className="mt-3 flex items-center justify-between">
              <p className="text-xs text-muted-foreground">Ctrl + Enter 快捷提交</p>
              <div className="flex items-center gap-2">
                {feedbackDone && (
                  <span className="text-xs text-green-600">感谢您的反馈！</span>
                )}
                <Button
                  size="sm"
                  onClick={handleSubmitFeedback}
                  disabled={!feedbackText.trim() || feedbackSending}
                >
                  <Send className="size-3.5" />
                  {feedbackSending ? '提交中…' : '提交反馈'}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </PlatformShell>
  );
}
