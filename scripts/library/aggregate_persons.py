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
            # Facts belong to the article's primary subject, not to every
            # person mentioned in it. Only keep facts when this person is the
            # first-listed (main) subject, otherwise e.g. Li Shimin's profile
            # inherits Wu Zetian's facts from an article listing both.
            persons = obj.get("persons") or []
            primary = bool(persons) and persons[0].strip() == p
            if primary:
                # Even as the primary subject, a multi-topic article can emit
                # facts about other figures; keep only facts that mention this
                # person (or carry no other person's name at all).
                others = {o.strip() for o in persons[1:] if o.strip()}
                for f in obj.get("key_facts") or []:
                    if len(a["facts"]) >= 12 or f in a["facts"]:
                        continue
                    if p in f or not any(o in f for o in others):
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
    # dynasty fallback map from the static timeline (name -> era_en lookup)
    dyn_by_book: dict[str, str] = {}
    for (j,) in conn.execute("SELECT json FROM annotations WHERE target_type='book'"):
        try:
            obj = json.loads(j)
        except json.JSONDecodeError:
            continue
        subj = (obj.get("subject") or "").strip()
        if subj and obj.get("dynasty"):
            dyn_by_book.setdefault(subj, obj["dynasty"])

    for person, a in doc_agg.items():
        books_for_person = book_subjects.get(person, [])
        if a["n"] < 3 and not books_for_person:
            continue
        top_docs = [
            did for did, t in all_titles
            if person in t
        ][:40]
        dynasty = max(a["dynasties"], key=a["dynasties"].get) if a["dynasties"] else ""
        if not dynasty:
            # no article gave a dynasty: borrow the book annotation's, so a
            # modern figure like 钱学森 still resolves to 当代
            dynasty = dyn_by_book.get(person, "")
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
