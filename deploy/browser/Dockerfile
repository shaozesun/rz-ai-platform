# Playwright 旁路服务：基镜像自带 Chromium 与全部系统依赖，后端镜像不膨胀。
# 版本钉死，避免浏览器/驱动漂移导致选择器行为变化。
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 录屏（browser__record）合成 webm 需要系统 ffmpeg（Playwright 自带的是内部构件，不是 CLI）。
# jammy 的 ffmpeg 带 libvpx（VP8/VP9），约 +100MB。
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# test_app.py 一并拷入：容器内 `python test_app.py` 做 fixture 自测（约 10KB，不影响运行）
COPY app.py test_app.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8100

# 必须单进程：浏览器实例常驻进程内，多 worker 会各自拉起一份 Chromium。
# 并发靠容器内的信号量（BROWSER_MAX_CONCURRENCY）控制。
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
