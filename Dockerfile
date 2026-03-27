# Use Playwright's official Docker image with browsers pre-installed
# Python 3.10 (jammy), includes Chromium/Firefox/WebKit
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# 清除可能继承的代理设置和apt代理配置
ENV http_proxy=""
ENV https_proxy=""
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""

# 清除apt代理配置并配置国内镜像源（Ubuntu jammy ARM64 使用华为云镜像）
RUN rm -f /etc/apt/apt.conf.d/*proxy* 2>/dev/null || true && \
    echo 'Acquire::http::Proxy "false";' > /etc/apt/apt.conf.d/00proxy && \
    echo 'Acquire::https::Proxy "false";' >> /etc/apt/apt.conf.d/00proxy && \
    sed -i 's|http://ports.ubuntu.com/ubuntu-ports|https://repo.huaweicloud.com/ubuntu-ports|g' /etc/apt/sources.list && \
    sed -i 's|http://archive.ubuntu.com/ubuntu|https://repo.huaweicloud.com/ubuntu|g' /etc/apt/sources.list && \
    sed -i 's|http://security.ubuntu.com/ubuntu|https://repo.huaweicloud.com/ubuntu|g' /etc/apt/sources.list

# 设置时区（在安装其他包之前）
ENV TZ=Asia/Shanghai
ENV DEBIAN_FRONTEND=noninteractive

# Install additional build dependencies
# 包含文档处理工具: pandoc, libreoffice, poppler, tesseract
# 包含 Chrome/Playwright 依赖和 Xvfb
# 包含 tzdata 用于时区支持
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    gcc \
    python3-dev \
    pandoc \
    libreoffice \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-chi-tra \
    tesseract-ocr-eng \
    git \
    curl \
    wget \
    openssh-client \
    docker.io \
    xvfb \
    # Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies (使用华为云 PyPI 镜像，清除代理)
COPY requirements.txt .
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
    pip install --no-cache-dir --proxy "" -i https://repo.huaweicloud.com/repository/pypi/simple/ --trusted-host repo.huaweicloud.com -r requirements.txt

# 安装 Playwright Chromium 浏览器（web_search 需要，清除代理）
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
    playwright install chromium

# 安装 Patchright Chromium（Google/DuckDuckGo 协议层反检测，独立的浏览器实例）
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
    patchright install chromium || echo "WARN: patchright chromium install failed, will fallback to playwright"

# 预下载 browserforge 贝叶斯模型文件（避免运行时首次 import 下载延迟）
RUN python -m browserforge update || echo "WARN: browserforge model download failed, will auto-download at runtime"

# 创建系统 Chrome 软链接，让 undetected-chromedriver 的 find_chrome_executable() 能找到浏览器
# Ubuntu 22.04 ARM64 无法通过 apt 安装 Chrome（只有 snap 过渡包），复用 Playwright 自带的 Chromium
RUN ln -sf /ms-playwright/chromium-1208/chrome-linux/chrome /usr/bin/google-chrome && \
    ln -sf /ms-playwright/chromium-1208/chrome-linux/chrome /usr/bin/chromium

# 设置 Chrome 环境变量（Selenium 降级方案）
ENV CHROME_BIN=/usr/bin/google-chrome
ENV DISPLAY=:99

# Copy application
COPY app/ ./app/
COPY agent_core/ ./agent_core/
COPY .claude/ ./.claude/

# Create necessary directories
RUN mkdir -p /app/app/data /app/logs /app/app/static/components

# Create symlink for skills module access (from skills.xxx import)
RUN ln -sf /app/.claude/skills /app/skills

# 生成 SSH 密钥（用于连接 toolbox）
RUN mkdir -p /root/.ssh && \
    ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" -C "agent-service" && \
    chmod 600 /root/.ssh/id_ed25519

# Set permissions
RUN chmod +x /app

# Environment
ENV PYTHONPATH=/app:/app/.claude
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV TZ=Asia/Shanghai

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# 使用 8 workers（10GB 内存主机，每个 worker ~350MB，留足余量给 SDK 子进程和 Playwright）
# --limit-max-requests: 设为 10000，避免高频健康检查请求（/health 每隔几秒一次）
#   过早触发 worker 轮换导致正在执行的长时间请求（Phase 2 可达 2+ 分钟）被中断。
#   健康检查应通过 proxy 或监控层过滤，不计入业务请求计数。
#   实际内存泄露防护通过定期重启（docker-compose restart policy）或内存监控实现。
# --limit-concurrency: 每个 worker 最大并发连接数
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "8", "--limit-max-requests", "10000", "--limit-concurrency", "100"]
