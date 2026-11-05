# One image for the two small Python services (queue API and the stand-in source); compose picks the command.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
RUN pip install uv==0.11.6
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY sql ./sql
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" LAB_ROOT=/app
