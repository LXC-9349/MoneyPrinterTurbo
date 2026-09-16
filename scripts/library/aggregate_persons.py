"""Aggregate person profiles from annotations + FTS into a persons table.

Person normalization: full-name exact match (LLM already outputs canonical
Chinese names). Alias merging (曹操/孟德/阿瞒) happens downstream in the app
layer via Wikipedia/Wikidata — out of scope for the offline pipeline.

Creates:
  persons(person, dynasty, doc_count, battle/anecdote/..., top_doc_ids, book_ids)

Usage: python scripts/library/aggregate_persons.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import open_db  # noqa: E402


def main() -> int:
    conn = open_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS persons ("
        " person TEXT PRIMARY KEY,"
        " dynasty TEXT,"
        " doc_count INTEGER,"
        " reliable_high INTEGER,"
        " facts_json TEXT,"
        " top_docs TEXT,"
        " books TEXT)"
    )

    doc_agg: dict = {}
    for (j,) in conn.execute(
        "SELECT json FROM annotations WHERE target_type='doc'"
    ):
        obj = json.loads(j)
        for p in obj.get("persons") or []:
            p = (p or "").strip()
            if not p or len(p) > 12:
                continue
            a = doc_agg.setdefault(p, {
                "dynasties": {}, "n": 0, "high": 0, "facts": [], "docs": []})
            a["n"] += 1
            dyn = obj.get("dynasty")
            if dyn:
                a["dynasties"][dyn] = a["dynasties"].get(dyn, 0) + 1
            if obj.get("reliable") == "高":
                a["high"] += 1
            for f in obj.get("key_facts") or []:
                if len(a["facts"]) < 12:
                    a["facts"].append(f)

    book_subjects = defaultdict(list)
    for (j,) in conn.execute(
        "SELECT json FROM annotations WHERE target_type='book'"
    ):
        obj = json.loads(j)
        if obj.get("is_biography") and obj.get("subject"):
            book_subjects[obj["subject"].strip()].append(obj)

    # doc ids for each person: match by title contains person name + annotate persons list
    rows = []
    all_titles = conn.execute("SELECT doc_id, title FROM documents").fetchall()
    for person, a in doc_agg.items():
        if a["n"] < 3:
            continue
        top_docs = [
            did for did, t in all_titles
            if person in t
        ][:40]
        dynasty = max(a["dynasties"], key=a["dynasties"].get) if a["dynasties"] else ""
        books = json.dumps(
            [{"era_hint": b.get("era_hint"), "country": b.get("country")}
             for b in book_subjects.get(person, [])], ensure_ascii=False)
        rows.append((
            person, dynasty, a["n"], a["high"],
            json.dumps(a["facts"], ensure_ascii=False),
            json.dumps(top_docs), books))

    with conn:
        conn.execute("DELETE FROM persons")
        conn.executemany("INSERT OR REPLACE INTO persons VALUES (?,?,?,?,?,?,?)", rows)

    n = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(f"persons aggregated: {n}")
    for r in conn.execute(
        "SELECT person, dynasty, doc_count, reliable_high FROM persons "
        "ORDER BY doc_count DESC LIMIT 15"):
        print("  ", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
