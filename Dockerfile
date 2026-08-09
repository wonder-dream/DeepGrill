FROM python:3.13-slim

# uv（依赖管理）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 系统依赖：LibreOffice（DOC 解析）+ MinerU 运行库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-core \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.lock ./
# 服务器为 CPU 环境：把 GPU pin（+cu124）替换为 PyPI CPU 版
RUN sed -i 's/torch==2.6.0+cu124/torch==2.6.0/; s/torchvision==0.21.0+cu124/torchvision==0.21.0/' requirements.txt \
    && uv pip install --system -r requirements.txt \
       --index-strategy unsafe-best-match \
       --extra-index-url https://download.pytorch.org/whl/cpu

COPY app ./app
COPY config.yaml ./
COPY scripts ./scripts

# MinerU 模型由首次解析时自动下载（HF 缓存挂载卷持久化）
ENV HF_HUB_DISABLE_TELEMETRY=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
