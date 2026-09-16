"""LLM batch annotation for the biography library.

Two passes (both resumable via the `annotations` table):
  docs  : per-article labels  -> persons, dynasty, years, doc_type, key_facts
  books : per-book labels     -> is_biography, subject person, dynasty, era

LLM: reads [app] oneapi/moonshot/... provider from config.toml via the
project's own llm service (no new HTTP code).

Usage:
  python scripts/library/annotate.py --target docs  --limit 30   # quality probe
  python scripts/library/annotate.py --target docs                # full run
  python scripts/library/annotate.py --target books
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import open_db  # noqa: E402

_dyn = json.loads(
    (Path(__file__).resolve().parent / "data" / "dynasty_timeline.json").read_text(encoding="utf-8")
)
DYNASTY_LIST = "、".join(d["name"] for d in _dyn) if _dyn else "先秦、秦汉、三国、两晋南北朝、隋唐、五代十国、宋、元、明、清、民国、当代、外国"

DOC_PROMPT = """你是历史文献标注员。对下面每篇短文，输出严格 JSON 数组（每篇一个对象），字段：
- id: 给定的编号（整数）
- persons: 文中核心人物姓名列表（1-5个，中文，主传主在前；无明确人物则 []）
- dynasty: 所属时代，只能从这里选：{dyn}；不属于任何中国朝代的外国内容填 "外国"
- year_start, year_end: 事件/人物活动的大致公元年份（整数，公元前为负；不确定填 null）
- doc_type: biography_entry | battle | anecdote | evaluation | mystery | other
- key_facts: 0-4条可核查的硬事实（时间/地点/人名/数字），不要观点
- reliable: 高(史实为主)/中(有演绎)/低(野史戏说猎奇)，依据内容风格判断，野史秘闻类多为低

只输出 JSON，不要解释。

{items}"""

BOOK_PROMPT = """你在为一套传记素材库打标签。书名：《{title}》，作者:{author}
正文开头摘录（可能含噪声）：
---
{head}
---
输出严格 JSON（单对象）：
{{"is_biography": true/false, "subject": "主传主人名（中文，非传记或无法判断填 \\"\\")",
"dynasty": "时代，只能从 {dyn} 选，外国填\\"外国\\"",
"era_hint": "适合 Wikimedia Commons 检索该人物/时代的英文关键词，1-3个词",
"genre": "person|event|war|autobiography|essay|history|other",
"country": "中国|外国|混合"}}
只输出 JSON。"""


def _llm(prompt: str) -> str:
    from app.services import llm

    return llm._generate_response(prompt)


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if not m:
        raise ValueError(f"no json in response: {text[:80]!r}")
    return json.loads(m.group(0))


def annotate_docs(conn, limit, batch_size=5, workers=4):
    have = {r[0] for r in conn.execute(
        "SELECT target_id FROM annotations WHERE target_type='doc'")}
    rows = [r for r in conn.execute(
        "SELECT doc_id, category, title, body FROM documents ORDER BY doc_id")
        if r[0] not in have]
    if limit:
        rows = rows[:limit]
    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    print(f"docs to annotate: {len(rows)} ({len(batches)} batches)", flush=True)

    def run_batch(batch):
        items = "\n\n".join(
            f"--- id={n} 分类={cat} 标题={title}\n正文(截断2500字):\n{body[:2500]}"
            for n, (_did, cat, title, body) in enumerate(batch)
        )
        prompt = DOC_PROMPT.format(dyn=DYNASTY_LIST, items=items)
        for attempt in range(4):
            resp = _llm(prompt)
            if resp.startswith("Error: "):
                time.sleep(5 * (attempt + 1))
                continue
            try:
                data = _extract_json(resp)
                if isinstance(data, dict):
                    data = [data]
                out = {}
                for obj in data:
                    oid = obj.get("id")
                    if isinstance(oid, int) and 0 <= oid < len(batch):
                        out[batch[oid][0]] = obj
                return out
            except Exception:
                time.sleep(2)
        return {}

    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(run_batch, b): b for b in batches}
        for fut in cf.as_completed(futs):
            result = fut.result()
            batch = futs[fut]
            with conn:
                for did, obj in result.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO annotations VALUES ('doc',?,?,?,datetime('now'))",
                        (did, json.dumps(obj, ensure_ascii=False),
                         os.environ.get("ANNOT_MODEL", "default")),
                    )
            done += 1
            if done % 20 == 0:
                print(f"  batches {done}/{len(batches)}", flush=True)
    annotated = conn.execute("SELECT COUNT(*) FROM annotations WHERE target_type='doc'").fetchone()[0]
    print(f"docs annotated total in db: {annotated}")


def annotate_books(conn, limit, workers=4):
    have = {r[0] for r in conn.execute(
        "SELECT target_id FROM annotations WHERE target_type='book'")}
    rows = conn.execute(
        "SELECT book_id, title, author FROM books WHERE status='done'").fetchall()
    rows = [r for r in rows if r[0] not in have]
    if limit:
        rows = rows[:limit]
    print(f"books to annotate: {len(rows)}", flush=True)

    def one(row):
        # Reader threads must NOT share the main connection: concurrent
        # cursors on one sqlite3 connection raise "another row available".
        rconn = open_db(check_same_thread=False)
        bid, title, author = row
        head = rconn.execute(
            "SELECT substr(body,1,1500) FROM chapters WHERE book_id=? AND idx BETWEEN 1 AND 2",
            (bid,)).fetchall()
        rconn.close()
        head_text = "\n".join(h[0] for h in head)[:1500]
        prompt = BOOK_PROMPT.format(title=title, author=author,
                                    head=head_text or "(无正文)", dyn=DYNASTY_LIST)
        for attempt in range(3):
            resp = _llm(prompt)
            if resp.startswith("Error: "):
                time.sleep(4 * (attempt + 1))
                continue
            try:
                return bid, _extract_json(resp)
            except Exception:
                time.sleep(2)
        return bid, None

    done = fail = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for bid, obj in pool.map(one, rows):
            if obj:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO annotations VALUES ('book',?,?,?,datetime('now'))",
                        (bid, json.dumps(obj, ensure_ascii=False),
                         os.environ.get("ANNOT_MODEL", "default")))
                done += 1
            else:
                fail += 1
            if (done + fail) % 50 == 0:
                print(f"  {done} ok / {fail} fail", flush=True)
    print(f"books annotated: {done} ok, {fail} fail")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["docs", "books"], required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    conn = open_db(check_same_thread=False)
    if args.target == "docs":
        annotate_docs(conn, args.limit, args.batch, args.workers)
    else:
        annotate_books(conn, args.limit, args.workers)


if __name__ == "__main__":
    main()
