# TestFlow CI 后端镜像（含 Playwright Chromium）
#
# 说明：浏览器执行必须能访问被测环境，所以 worker 容器需要与目标网络连通。
# 若目标环境只在内网可达，请把 worker 部署到内网机器上，而不是只部署这个镜像。

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

WORKDIR /app

# 系统依赖：Playwright 运行 Chromium 需要这些库
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt "psycopg[binary]" \
    && python -m playwright install chromium \
    && python -m playwright install-deps chromium

COPY backend /app/backend
COPY demo_app /app/demo_app
COPY scripts /app/scripts

RUN mkdir -p /app/storage

EXPOSE 8000 8100

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
