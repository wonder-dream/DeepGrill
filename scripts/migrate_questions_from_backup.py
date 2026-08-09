"""一次性迁移：从备份 zip 复制公共题库层（sources/questions）到目标库。

用法：python scripts/migrate_questions_from_backup.py 备份.zip 目标interview.db
例（服务器容器内）：python scripts/migrate_questions_from_backup.py /root/interview_backup_xxx.zip /app/data/interview.db

- 只迁移公共题库：sources + questions（保留 id 与 embedding BLOB，2293 题向量免重算）
- 不碰 users / user_tokens / user_picks（服务器已注册用户不覆盖）
- 幂等：目标库已存在的 source/question id 跳过，可重复执行
- 本地判分数据（sessions/attempts/judgments）默认不迁移（如需迁到 owner 名下另行处理）
"""
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path


def main(backup_zip: str, target_db: str) -> int:
    with zipfile.ZipFile(backup_zip) as zf:
        db_names = [n for n in zf.namelist() if n.endswith(".db")]
        if not db_names:
            raise SystemExit(f"zip 中未找到 .db 文件：{backup_zip}")
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        try:
            with open(tmp_fd, "wb") as out:
                out.write(zf.read(db_names[0]))
            return _migrate(tmp_path, target_db)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def _migrate(src_db: str, target_db: str) -> int:
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(target_db)
    try:
        total = 0
        for table in ("sources", "questions"):
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            placeholders = ",".join("?" * len(cols))
            existing = {r[0] for r in dst.execute(f"SELECT id FROM {table}")}
            for row in src.execute(f"SELECT * FROM {table}"):
                if row[0] in existing:
                    continue
                dst.execute(
                    f"INSERT INTO {table} VALUES ({placeholders})", tuple(row)
                )
                total += 1
        dst.commit()
        return total
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法：python scripts/migrate_questions_from_backup.py 备份.zip 目标interview.db")
    n = main(sys.argv[1], sys.argv[2])
    print(f"迁移完成：新增 {n} 行（sources + questions，含 embedding）")
