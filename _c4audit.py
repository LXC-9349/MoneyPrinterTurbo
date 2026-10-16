"""收尾 #4：测试数据清理 dry-run 审计（只读，绝不删除）。

分类“可证明由测试产生”的 DB 行与 storage 目录，给出数量与占用空间，
并显式列出必须保护的项（4 个已完成成片任务、18 条 projects.json 迁移记录、
任何无法证明是测试产物的内容）。运行：
    .venv\\Scripts\\python.exe _c4audit.py
"""

import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
STORAGE = ROOT / "storage"
TASKS = STORAGE / "tasks"
LIB = STORAGE / "library"
PROJECTS_JSON = STORAGE / "projects" / "projects.json"
MPT_DB = LIB / "mpt.sqlite"
PERSONAL_DB = LIB / "personal.sqlite"

# 明确可证明为测试产物的签名（保守：只有命中强签名才计入“可删”）。
TEST_PERSON_NAMES = {"Coffee", "测试人物", "test-person", "person-1", "p1"}
TEST_TASK_ID_PATTERNS = (
    "task-123", "task-test", "cx-", "int", "rs-fail", "e-none", "probe",
    "tmp", "fake", "unit-", "it-", "t-",
)
# 已知示例/真实任务：保护，绝不删除（463e3e04 是用户点名的示例任务，也仅只读）。
PROTECTED_TASKS = set()


def _dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def _fmt(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def audit_db():
    for db, label in ((MPT_DB, "mpt.sqlite"), (PERSONAL_DB, "personal.sqlite")):
        if not db.is_file():
            print(f"[{label}] 不存在，跳过")
            continue
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            print(f"\n=== {label} 各表行数 ===")
            for t in tables:
                try:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    print(f"  {t:24s} {n}")
                except sqlite3.Error:
                    print(f"  {t:24s} <error>")
            # persons 测试签名
            if "persons" in tables:
                rows = conn.execute(
                    "SELECT person_id,display_name,origin,state,created_at "
                    "FROM persons"
                ).fetchall()
                test_rows = [r for r in rows if (r["display_name"] or "") in TEST_PERSON_NAMES]
                empties = [r for r in rows if (r["state"] or "") == "draft"
                           or not (r["display_name"] or "").strip()]
                print(f"\n  persons 命中测试姓名: {len(test_rows)}")
                for r in test_rows:
                    print(f"    #{r['person_id']} {r['display_name']!r} origin={r['origin']} "
                          f"state={r['state']} created={r['created_at']}")
                print(f"  persons 空草稿/异常行: {len(empties)}")
                for r in empties[:20]:
                    print(f"    #{r['person_id']} {r['display_name']!r} origin={r['origin']} "
                          f"state={r['state']}")
            if "tasks" in tables:
                rows = conn.execute(
                    "SELECT task_id,status,title,created_at FROM tasks"
                ).fetchall()
                test_tasks = [
                    r for r in rows
                    if any(p in (r["task_id"] or "") for p in TEST_TASK_ID_PATTERNS)
                ]
                print(f"\n  tasks 命中测试 task_id: {len(test_tasks)} / 总 {len(rows)}")
                for r in test_tasks[:30]:
                    print(f"    {r['task_id']!r} status={r['status']} created={r['created_at']}")
        finally:
            conn.close()


def audit_tasks_dir():
    if not TASKS.is_dir():
        print("\n[storage/tasks] 不存在")
        return
    print("\n=== storage/tasks 目录 ===")
    test_like = []
    protected_like = []
    unknown = []
    for d in sorted(TASKS.iterdir()):
        if not d.is_dir():
            continue
        size = _dir_size(d)
        name = d.name.lower()
        has_video = any(
            (sub / "final-1.mp4").is_file() or (sub / "final.mp4").is_file()
            or list(sub.glob("*.mp4"))
            for sub in [d]
        )
        entry = (d.name, size, has_video, d.stat().st_mtime)
        if any(p in name for p in TEST_TASK_ID_PATTERNS):
            test_like.append(entry)
        elif has_video:
            protected_like.append(entry)  # 含成片，倾向保护
        else:
            unknown.append(entry)
    print(f"  命中测试前缀目录: {len(test_like)}，合计 {_fmt(sum(s for _,s,_,_ in test_like))}")
    for name, size, _v, _m in test_like:
        print(f"    {name:40s} {_fmt(size)}")
    print(f"  含成片(疑似真实, 保护): {len(protected_like)}，合计 {_fmt(sum(s for _,s,_,_ in protected_like))}")
    for name, size, _v, _m in protected_like:
        print(f"    {name:40s} {_fmt(size)}")
    print(f"  无法判定(默认保护): {len(unknown)}，合计 {_fmt(sum(s for _,s,_,_ in unknown))}")
    for name, size, _v, _m in unknown[:40]:
        print(f"    {name:40s} {_fmt(size)}")


def audit_projects():
    if not PROJECTS_JSON.is_file():
        print("\n[projects.json] 不存在")
        return
    data = json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("projects", [])
    print(f"\n[projects.json] 记录数: {len(items)}（迁移来的历史项目，保护）")
    test_p = [
        it for it in items
        if any(p in str(it.get("id", "")).lower() or p in str(it.get("task_id", "")).lower()
               for p in TEST_TASK_ID_PATTERNS)
    ]
    print(f"  其中疑似测试前缀: {len(test_p)}")
    for it in test_p[:20]:
        print(f"    id={it.get('id')!r} task_id={it.get('task_id')!r} title={it.get('title')!r}")


if __name__ == "__main__":
    print(f"ROOT={ROOT}")
    audit_db()
    audit_tasks_dir()
    audit_projects()
    print("\n提示：本脚本只读，不删除任何内容。清单交维护者确认后再执行清理。")
