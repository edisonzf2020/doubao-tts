FROM ghcr.io/astral-sh/uv:python3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    DOUBAO_TTS_HOST=0.0.0.0 \
    DOUBAO_TTS_PORT=8000

WORKDIR /app

# 先只拷依赖清单，命中缓存层：源码变动时不必重装依赖
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# 再拷源码（.dockerignore 已排除 .cookie/.env/.venv 等）
COPY . .

EXPOSE 8000

# /health 会返回 cookie 剩余天数与音色加载状态；比单纯探端口更有意义
HEALTHCHECK --interval=1m --start-period=15s --timeout=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"DOUBAO_TTS_PORT\",\"8000\")}/health',timeout=5).status==200 else 1)"

CMD ["python", "openai_server.py"]
