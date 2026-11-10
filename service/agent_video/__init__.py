"""对话窗口的「讲解视频」能力（纯加法，与 service/video 的表单页链路完全独立）。

链路：PPT → (LibreOffice) PDF → (pypdfium2) 逐页 PNG → 逐页解说词（平台 LLM）
      → 逐页 TTS（复用 service/video/tts.py 的 qwen 后端 + 同一个跨进程配额）
      → (ffmpeg) 每页片段 → concat 成 MP4 → 落 agent_video_tasks 集合。

为什么自己实现而不调 8010/8020/8030 三个插件：那三个服务不可控（8010 实测卡死
60~90s、8030 实测 500），且不落中间产物 → 无法"只改一页"。本模块每页产物落库，
因此支持增量重跑（改一页只重跑该页）与 RITL 自检（渲染后看图纠错）。
"""
