"""开发期探针：看库当前有什么。用法：python tools/_probe.py [库路径]"""

import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1] if len(sys.argv) > 1 else "data/interview.db")
print(f"库: {db}  存在: {db.exists()}  大小: {db.stat().st_size if db.exists() else 0} 字节")

c = sqlite3.connect(str(db))
tables = [
    r[0]
    for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
]
print(f"\n表 {len(tables)} 张:")
for t in tables:
    print("  ", t)

indexes = [
    r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
]
print(f"\n索引 {len(indexes)} 个: {', '.join(indexes)}")

print("\n外键关系:")
for t in tables:
    if t == "sqlite_sequence":
        continue
    for r in c.execute(f"PRAGMA foreign_key_list({t})"):
        # r = (id, seq, table, from, to, on_update, on_delete, match)
        print(f"  {t}.{r[3]} -> {r[2]}.{r[4]}")

print("\n谁指向 knowledge_points（三层结构是否在库里连通）:")
found = False
for t in tables:
    for r in c.execute(f"PRAGMA foreign_key_list({t})"):
        if r[2] == "knowledge_points":
            print(f"  {t}.{r[3]} -> knowledge_points.{r[4]}")
            found = True
if not found:
    print("  （没有任何表指向它）")

print("\n谁指向 domains:")
found = False
for t in tables:
    for r in c.execute(f"PRAGMA foreign_key_list({t})"):
        if r[2] == "domains":
            print(f"  {t}.{r[3]} -> domains.{r[4]}")
            found = True
if not found:
    print("  （没有任何表指向它 —— 注意 domains 自己指向自己，那是层级）")

try:
    rows = c.execute("SELECT filename, applied_at FROM schema_migrations").fetchall()
    print(f"\nschema_migrations 记账 {len(rows)} 条:")
    for r in rows:
        print("  ", r)
except sqlite3.OperationalError as e:
    print(f"\nschema_migrations: {e}")
