#!/bin/bash
# 服务器一键部署脚本（在 /opt/interview-assistant 目录执行）
set -e

echo "==> 1/4 安装 Docker（官方源）"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
docker --version

echo "==> 2/4 检查 .env"
if [ ! -f .env ]; then
  echo "缺少 .env，请先：cp .env.example .env 并填入 LLM_API_KEY"
  exit 1
fi

echo "==> 3/4 构建镜像（首次构建含 MinerU 依赖，约 5-10 分钟）"
docker compose build

echo "==> 4/4 启动"
docker compose up -d
sleep 3
docker compose ps

echo ""
echo "部署完成。查看日志：docker compose logs -f app"
echo "首次上传 PDF 简历时 MinerU 会自动下载模型（约 1.2GB，只一次）"
