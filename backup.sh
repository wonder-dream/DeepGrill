#!/usr/bin/env bash
# 每日备份 interview.db（WAL 安全复制：容器内 sqlite .backup API），保留最近 7 份
# 部署到 /opt/interview-assistant/backup.sh，cron：3 3 * * * 调用
set -euo pipefail
cd /opt/interview-assistant
mkdir -p backups
STAMP=$(date +%Y%m%d_%H%M%S)
docker compose exec -T app python -c "import sqlite3; src=sqlite3.connect('/app/data/interview.db'); dst=sqlite3.connect('/tmp/backup_interview.db'); src.backup(dst); dst.close(); src.close()"
docker compose cp app:/tmp/backup_interview.db "backups/interview_$STAMP.db"
docker compose exec -T app rm -f /tmp/backup_interview.db
ls -1t backups/interview_*.db | tail -n +8 | xargs -r rm -f
