"""Build FTS5 indexes for books (from chapters) and docs (full bodies).

Resumable via a small book_fts_state table. Chinese is per-char tokenized
(common.cjk_seg) so multi-char phrases work with unicode61.

Usage: python scripts/library/build_fts.py [--books-only|--docs-only]
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cjk_seg, open_db  # noqa: E402

BODY_CAP = 400_000  # per-book FTS text cap; ~0.4M chars covers full books


def ensure_state(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fts_state ("
        " book_id TEXT PRIMARY KEY, indexed_at TEXT DEFAULT (datetime('now')))"
    )


def index_books(conn):
    ensure_state(conn)
    done = {r[0] for r in conn.execute("SELECT book_id FROM fts_state")}
    todo = [
        (rid, bid)
        for rid, bid in conn.execute(
            "SELECT rowid, book_id FROM books WHERE status='done'"
        )
        if bid not in done
    ]
    print(f"books to FTS: {len(todo)}", flush=True)
    t0 = time.time()
    n = 0
    # The FTS tables are contentless (content=''): they cannot be DELETEd.
    # If we are (re)indexing a non-trivial set from scratch, recreate them.
    if len(todo) > 0:
        indexed = conn.execute("SELECT COUNT(*) FROM fts_state").fetchone()[0]
        if indexed == 0:
            with conn:
                conn.execute("DROP TABLE IF EXISTS books_fts")
                conn.execute(
                    "CREATE VIRTUAL TABLE books_fts USING fts5(title, author, body, content='')"
                )
    for rowid, bid in todo:
        meta = conn.execute(
            "SELECT title, author FROM books WHERE book_id=?", (bid,)
        ).fetchone()
        parts = [f"{meta[0]} {meta[1]}\n"]
        for (body,) in conn.execute(
            "SELECT body FROM chapters WHERE book_id=? ORDER BY idx", (bid,)
        ):
            parts.append(body)
            if sum(len(p) for p in parts) > BODY_CAP:
                break
        full = cjk_seg("".join(parts))[: BODY_CAP * 4]
        with conn:
            try:
                conn.execute(
                    "INSERT INTO books_fts(books_fts, rowid) VALUES('delete', ?)",
                    (rowid,),
                )
            except sqlite3.Error:
                pass  # fresh/empty table: nothing to remove
            conn.execute(
                "INSERT INTO books_fts (rowid, title, author, body) VALUES (?,?,?,?)",
                (rowid, cjk_seg(meta[0]), cjk_seg(meta[1]), full),
            )
            conn.execute("INSERT OR REPLACE INTO fts_state(book_id) VALUES (?)", (bid,))
        n += 1
        if n % 50 == 0:
            print(f"  {n}/{len(todo)} ({time.time()-t0:.0f}s)", flush=True)
    print(f"books_fts indexed: {n} in {time.time()-t0:.0f}s")


def rebuild_docs_fts(conn):
    print("rebuilding docs_fts (full bodies)...", flush=True)
    t0 = time.time()
    with conn:
        conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('delete-all')")
        for rowid, title, body in conn.execute(
            "SELECT rowid, title, body FROM documents"
        ):
            conn.execute(
                "INSERT INTO docs_fts (rowid, title, body) VALUES (?,?,?)",
                (rowid, cjk_seg(title), cjk_seg(body)),
            )
    cnt = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
    print(f"docs_fts rebuilt: {cnt} in {time.time()-t0:.0f}s")


def main() -> int:
    conn = open_db()
    if "--docs-only" not in sys.argv:
        index_books(conn)
    if "--books-only" not in sys.argv:
        rebuild_docs_fts(conn)
    # sanity probes
    for q in ["曹操", "钱学森", "滑铁卢", "拿破仑"]:
        nb = conn.execute(
            "SELECT COUNT(*) FROM books_fts WHERE books_fts MATCH ?", (cjk_seg(q),)
        ).fetchone()[0]
        nd = conn.execute(
            "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH ?", (cjk_seg(q),)
        ).fetchone()[0]
        print(f"probe {q!r}: books={nb} docs={nd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
