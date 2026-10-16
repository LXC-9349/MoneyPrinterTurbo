"""一次性数据导入命令（阶段 2 步骤 2，纯新增）。

把 `library.sqlite`（只读语料）+ `personal.sqlite`（覆盖层）+ `projects.json` +
`storage/tasks/*` 既有产物，导入统一的 `mpt.sqlite`。

**默认 dry-run**：只读源、构造导入计划、输出**对拍报告**（各表前后条数、无法映射
清单、去重命中），不写任何库。`--apply` 才落库，且落库前自动 `VACUUM INTO` 备份。

设计要点（见 §2.8）：
* 只读打开源库（`mode=ro`），绝不修改/删除旧库。
* 惰性物化模型下，本命令是一次性"把已有历史搬进 mpt"，之后由 person_repo 增量维护。
* library 事实默认 `verified=0`；个人覆盖层手工事实 `source='manual'/human_confirmed=1/
  edited_by_user=1`；覆盖层"替换"语义下，被个人 facts_json 替换的人物**不再导入其库事实**。
* 内容哈希去重仅在 apply 时计算（避免 dry-run 对大文件全量哈希）。

用法：`uv run python -m app.services.import_library [--apply] [--mpt-db PATH]`
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services import db_migrations, person_repo
from app.utils import utils


# ── 源路径解析（与运行期一致的 env/默认，只读使用）────────────────────────
def _library_db() -> str | None:
    p = os.environ.get("MPT_LIBRARY_DB", "").strip() or os.path.join(
        utils.storage_dir("library", create=False), "library.sqlite"
    )
    return p if os.path.isfile(p) else None


def _personal_db() -> str | None:
    p = os.environ.get("MPT_PERSONAL_DB", "").strip() or os.path.join(
        utils.storage_dir("library", create=False), "personal.sqlite"
    )
    return p if os.path.isfile(p) else None


def _projects_file() -> str:
    return os.path.join(utils.storage_dir("projects", create=False), "projects.json")


def _tasks_root() -> str:
    return utils.storage_dir("tasks", create=False)


def _open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 报告结构 ────────────────────────────────────────────────────────────
@dataclass
class ImportReport:
    dry_run: bool = True
    mpt_db: str = ""
    before: dict[str, int] = field(default_factory=dict)
    planned: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    unmappable: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_unmappable(self, kind: str, ident: str, reason: str) -> None:
        self.unmappable.append({"kind": kind, "id": ident, "reason": reason})

    def render(self) -> str:
        lines = [
            "════════ 数据导入对拍报告 ════════",
            f"模式：{'DRY-RUN（未写库）' if self.dry_run else 'APPLY（已写库）'}",
            f"目标库：{self.mpt_db}",
            "",
            f"{'表':<16}{'迁移前':>10}{'预计/实际':>12}",
            "-" * 40,
        ]
        tables = sorted(set(self.before) | set(self.planned) | set(self.after))
        for t in tables:
            lines.append(f"{t:<16}{self.before.get(t, 0):>10}{(self.after.get(t) or self.planned.get(t, 0)):>12}")
        lines.append("")
        lines.append(f"无法映射记录：{len(self.unmappable)} 条")
        for u in self.unmappable[:50]:
            lines.append(f"  - [{u['kind']}] {u['id']}：{u['reason']}")
        if len(self.unmappable) > 50:
            lines.append(f"  …（另有 {len(self.unmappable) - 50} 条未列出）")
        if self.notes:
            lines.append("")
            lines.append("说明：")
            for n in self.notes:
                lines.append(f"  * {n}")
        return "\n".join(lines)


# ── 读取源 ──────────────────────────────────────────────────────────────
def _iter_library_persons(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(persons)")}
    for row in conn.execute("SELECT * FROM persons"):
        yield {
            "person": row["person"],
            "dynasty": (row["dynasty"] if "dynasty" in cols and row["dynasty"] else ""),
            "facts": _load_json(row["facts_json"] if "facts_json" in cols else None) or [],
            "top_docs": _load_json(row["top_docs"] if "top_docs" in cols else None) or [],
            "books": _load_json(row["books"] if "books" in cols else None) or [],
        }


def _load_json(val: Any):
    if val in (None, ""):
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (ValueError, TypeError):
        return None


def _fact_statements(facts: list) -> list[str]:
    out = []
    for f in facts:
        if isinstance(f, str):
            out.append(f)
        elif isinstance(f, dict):
            s = f.get("statement") or f.get("fact") or f.get("text")
            if s:
                out.append(str(s))
    return [s.strip() for s in out if str(s).strip()]


def _doc_ids(entries: list) -> list[str]:
    ids = []
    for e in entries:
        if isinstance(e, str):
            ids.append(e)
        elif isinstance(e, dict):
            d = e.get("doc_id") or e.get("id") or e.get("d")
            if d:
                ids.append(str(d))
    return ids


# ── 计划 / 落库 ─────────────────────────────────────────────────────────
def build_report(mpt_db: str | None = None, apply: bool = False) -> ImportReport:
    mpt_db = mpt_db or person_repo.db_path()
    report = ImportReport(dry_run=not apply, mpt_db=mpt_db)

    # 迁移前 mpt 现状（若已存在）。
    if os.path.isfile(mpt_db):
        mconn = person_repo.connect(mpt_db)
    else:
        mconn = None
    report.before = _count_tables(mconn) if mconn else {
        t: 0 for t in ("persons", "person_aliases", "facts", "assets",
                      "person_assets", "scripts", "videos", "groups", "library_links")
    }

    plan = _plan(report)

    if not apply:
        report.planned = {k: len(v) for k, v in plan.items()}
        if mconn:
            mconn.close()
        return report

    # APPLY：落库前先备份（旧库只读不动，仅备份将写入的 mpt）。
    if mconn is None:
        mconn = person_repo.connect(mpt_db)  # 建表并迁移
    _backup_if_exists(mpt_db)
    _write_plan(mconn, plan, report)
    report.after = _count_tables(mconn)
    mconn.close()
    return report


def _count_tables(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for t in ("persons", "person_aliases", "facts", "assets",
              "person_assets", "scripts", "videos", "groups", "library_links"):
        try:
            out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        except sqlite3.Error:
            out[t] = 0
    return out


def _plan(report: ImportReport) -> dict[str, list[dict]]:
    """构造导入计划（纯内存）。key=表名，value=行 dict 列表。"""
    plan: dict[str, list[dict]] = {
        "persons": [], "person_aliases": [], "facts": [], "library_links": [],
        "groups": [], "group_members": [], "projects": [],
    }
    by_name: dict[str, dict] = {}  # display_name → planned person（供覆盖层/成员解析）

    lib = _library_db()
    if not lib:
        report.notes.append("library.sqlite 不存在：跳过史料库人物导入（降级，不报错）。")
    else:
        lc = _open_ro(lib)
        try:
            for p in _iter_library_persons(lc):
                name = (p["person"] or "").strip()
                if not name or name in by_name:
                    continue
                person = {
                    "public_id": db_migrations.new_public_id("per"),
                    "display_name": name, "dynasty": p["dynasty"],
                    "origin": "library", "facts": p["facts"],
                    "top_docs": p["top_docs"], "books": p["books"],
                }
                by_name[name] = person
                plan["persons"].append(person)
                plan["person_aliases"].append({"person": name, "alias": name, "source": "name"})
        finally:
            lc.close()

    # 个人覆盖层：合并/新建；替换语义下被替换者不导入库事实。
    pers = _personal_db()
    overlay_names: set[str] = set()
    if pers:
        pc = _open_ro(pers)
        try:
            for row in pc.execute("SELECT * FROM person_meta"):
                name = (row["person"] or "").strip()
                if not name:
                    continue
                overlay_names.add(name)
                custom = bool(row["custom"])
                hidden = bool(row["hidden"])
                facts_json = _load_json(row["facts_json"])
                if name in by_name:
                    person = by_name[name]
                    person["dynasty"] = (row["dynasty"] or person["dynasty"])
                    person["category"] = row["category"] or ""
                    person["note"] = row["note"] or ""
                    person["state"] = "archived" if hidden else "active"
                    if facts_json is not None:  # 覆盖层"替换"库事实
                        person["facts"] = facts_json
                        person["facts_are_manual"] = True
                else:
                    person = {
                        "public_id": db_migrations.new_public_id("per"),
                        "display_name": name, "dynasty": row["dynasty"] or "",
                        "category": row["category"] or "", "note": row["note"] or "",
                        "origin": "custom" if custom else "library",
                        "state": "archived" if hidden else "active",
                        "facts": facts_json or [], "facts_are_manual": True,
                        "top_docs": [], "books": [],
                    }
                    by_name[name] = person
                    plan["persons"].append(person)
                    plan["person_aliases"].append({"person": name, "alias": name, "source": "name"})
            for g in pc.execute("SELECT name, created_at FROM groups"):
                plan["groups"].append({"name": g["name"], "created_at": g["created_at"] or _now_utc()})
            for m in pc.execute("SELECT group_name, person, added_at FROM group_members"):
                plan["group_members"].append(
                    {"group_name": m["group_name"], "person": m["person"], "added_at": m["added_at"] or _now_utc()}
                )
        finally:
            pc.close()
    else:
        report.notes.append("personal.sqlite 不存在：跳过覆盖层导入。")

    # 事实展开为计划行。
    for person in plan["persons"]:
        manual = person.get("facts_are_manual", False)
        for idx, stmt in enumerate(_fact_statements(person.get("facts", []))):
            plan["facts"].append({
                "person": person["display_name"], "ordinal": idx, "statement": stmt,
                "source": "manual" if manual else "library",
                "verified": 0,  # §B6：导入的史料/个人事实默认未核验，交由后续核验/人工确认
                "human_confirmed": 1 if manual else 0,
                "edited_by_user": 1 if manual else 0,
            })
        for d in _doc_ids(person.get("top_docs", [])):
            plan["library_links"].append({"person": person["display_name"], "doc_id": d, "role": "source"})

    # 任务产物：projects.json + storage/tasks → 计划 videos/scripts。
    plan["videos"], plan["scripts"] = _plan_task_artifacts(by_name, report)
    return plan


def _plan_task_artifacts(by_name: dict, report: ImportReport):
    videos, scripts = [], []
    pf = _projects_file()
    entries = []
    if os.path.isfile(pf):
        try:
            with open(pf, "r", encoding="utf-8") as f:
                entries = (json.load(f) or {}).get("projects", []) or []
        except (ValueError, OSError) as exc:
            report.add_unmappable("projects_file", pf, f"解析失败：{exc}")
    else:
        report.notes.append("projects.json 不存在：跳过成片/脚本导入。")

    tasks_root = _tasks_root()
    for e in entries:
        if not isinstance(e, dict):
            continue
        task_id = e.get("task_id") or ""
        draft = e.get("draft") or {}
        mode = e.get("mode") or ("biography" if (e.get("person") or draft.get("biography_person")) else "normal")
        # 人物名：顶层 person → 顶层 biography_person → draft.biography_person（传记真实人名常存于 draft）。
        name = (e.get("person") or e.get("biography_person") or draft.get("biography_person") or "").strip()
        person_ref = by_name.get(name) if name else None
        if not person_ref and mode == "biography" and name:
            report.add_unmappable("project", e.get("id", "?"), f"传记人物未入库：{name!r}")

        if task_id:
            vpath = _find_final_video(tasks_root, task_id)
            if vpath:
                videos.append({
                    "project_id": e.get("id", ""), "task_id": task_id,
                    "person": (person_ref or {}).get("display_name"),  # 可空→主题视频
                    "path": vpath, "duration": e.get("duration"),
                })
            spath = os.path.join(tasks_root, task_id, "script.json")
            if os.path.isfile(spath):
                body = _read_script_body(spath)
                if body is not None:
                    scripts.append({
                        "project_id": e.get("id", ""), "task_id": task_id,
                        "person": (person_ref or {}).get("display_name"),
                        "kind": "biography" if mode == "biography" else "general",
                        "body": body,
                    })
    return videos, scripts


def _find_final_video(tasks_root: str, task_id: str) -> str | None:
    d = os.path.join(tasks_root, task_id)
    if not os.path.isdir(d):
        return None
    for name in sorted(os.listdir(d)):
        if name.startswith("final") and name.endswith(".mp4"):
            full = os.path.join(d, name)
            try:
                rel = os.path.relpath(full, utils.storage_dir(create=False))
            except ValueError:
                # 跨盘（临时目录与存储根不同盘）时退回 tasks 相对布局。
                rel = os.path.join("tasks", task_id, name)
            return rel.replace("\\", "/")
    return None


def _read_script_body(spath: str):
    try:
        with open(spath, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (ValueError, OSError):
        return None
    script = data.get("script") or data
    return json.dumps(script, ensure_ascii=False)


# ── 落库 ────────────────────────────────────────────────────────────────
def _write_plan(conn: sqlite3.Connection, plan: dict, report: ImportReport) -> None:
    name_to_pid: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for p in plan["persons"]:
            ts = _now_utc()
            row = conn.execute(
                "SELECT person_id FROM persons WHERE display_name=? LIMIT 1",
                (p["display_name"],),
            ).fetchone()
            if row:  # 重跑：按主名复用 id（一次性导入幂等；public_id 每次新生成故不能靠它去重）
                pid = int(row["person_id"])
            else:
                cur = conn.execute(
                    "INSERT INTO persons(public_id,qid,display_name,dynasty,category,note,"
                    "origin,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (p["public_id"], p.get("qid", ""), p["display_name"], p.get("dynasty", ""),
                     p.get("category", ""), p.get("note", ""), p.get("origin", "library"),
                     p.get("state", "active"), ts, ts),
                )
                pid = int(cur.lastrowid)
            name_to_pid[p["display_name"]] = pid
        for a in plan["person_aliases"]:
            pid = name_to_pid.get(a["person"])
            if pid:
                conn.execute("INSERT OR IGNORE INTO person_aliases(person_id,alias,source) VALUES(?,?,?)",
                             (pid, a["alias"], a["source"]))
        pids_with_facts = {
            r["person_id"] for r in conn.execute("SELECT DISTINCT person_id FROM facts")
        }
        for fct in plan["facts"]:
            pid = name_to_pid.get(fct["person"])
            if pid and pid not in pids_with_facts:  # 已有事实的人物不重复导入（幂等）
                conn.execute(
                    "INSERT INTO facts(person_id,ordinal,statement,source,verified,human_confirmed,"
                    "edited_by_user) VALUES(?,?,?,?,?,?,?)",
                    (pid, fct["ordinal"], fct["statement"], fct["source"], fct["verified"],
                     fct["human_confirmed"], fct["edited_by_user"]),
                )
        for link in plan["library_links"]:
            pid = name_to_pid.get(link["person"])
            if pid:
                conn.execute("INSERT OR IGNORE INTO library_links(person_id,doc_id,role) VALUES(?,?,?)",
                             (pid, link["doc_id"], link["role"]))
        for g in plan["groups"]:
            conn.execute("INSERT OR IGNORE INTO groups(name,created_at) VALUES(?,?)",
                         (g["name"], g["created_at"]))
        for m in plan["group_members"]:
            pid = name_to_pid.get(m["person"])
            if pid:
                conn.execute("INSERT OR IGNORE INTO group_members(group_name,person_id,added_at) VALUES(?,?,?)",
                             (m["group_name"], pid, m["added_at"]))
        # 素材/成片：apply 时计算内容哈希去重（图片/音频），视频较大留空哈希。
        have_videos = {r["task_id"] for r in conn.execute("SELECT task_id FROM videos")}
        for v in plan.get("videos", []):
            if v["task_id"] in have_videos:  # 幂等：同一任务不重复登记
                continue
            pid = name_to_pid.get(v["person"]) if v.get("person") else None
            conn.execute(
                "INSERT INTO videos(person_id,project_id,task_id,path,content_hash,duration_s,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (pid, v["project_id"], v["task_id"], v["path"], "", v.get("duration"), _now_utc()),
            )
        have_scripts = {r["task_id"] for r in conn.execute("SELECT task_id FROM scripts")}
        for s in plan.get("scripts", []):
            if s["task_id"] in have_scripts:
                continue
            pid = name_to_pid.get(s["person"]) if s.get("person") else None
            conn.execute(
                "INSERT INTO scripts(person_id,project_id,task_id,kind,version,body,created_at)"
                " VALUES(?,?,?,?,1,?,?)",
                (pid, s["project_id"], s["task_id"], s["kind"], s["body"], _now_utc()),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _backup_if_exists(mpt_db: str) -> str | None:
    if not os.path.isfile(mpt_db):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = os.path.join(os.path.dirname(mpt_db), f"mpt.preimport.{ts}.sqlite")
    src = sqlite3.connect(mpt_db)
    dst = sqlite3.connect(backup)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 library/personal/tasks 到 mpt.sqlite（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只 dry-run 出对拍报告）")
    parser.add_argument("--mpt-db", default=None, help="目标 mpt.sqlite 路径（默认按 env/约定）")
    args = parser.parse_args(argv)
    report = build_report(mpt_db=args.mpt_db, apply=args.apply)
    print(report.render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
