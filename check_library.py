import sqlite3

conn = sqlite3.connect('storage/library/library.sqlite')
cursor = conn.execute("PRAGMA table_info(persons)")
columns = [row[1] for row in cursor.fetchall()]
print("=== persons 表结构 ===")
for col in columns:
    print(f"- {col}")

# 查看前 5 个人物
cursor = conn.execute("SELECT * FROM persons LIMIT 5")
print("\n=== 前 5 个人物数据 ===")
for row in cursor.fetchall():
    print(row)
    print("-" * 80)

conn.close()
