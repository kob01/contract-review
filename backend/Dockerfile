# --- 构建阶段：用 uv 生成与锁文件一致的虚拟环境 ---
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
# 依赖独立成层，改代码不会触发重新解析依赖
RUN uv sync --frozen --no-dev --no-install-project

# --- 运行阶段 ---
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONIOENCODING=utf-8

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY app ./app

RUN mkdir -p /app/storage/contracts

EXPOSE 8000

# 镜像内不带 --reload：生产要单进程可预测，开发由本地脚本负责热重载
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
