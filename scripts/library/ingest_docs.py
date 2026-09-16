"""Ingest source A: 中国名人古今人物传记 (~4942 GB18030 .doc files).

Idempotent: re-running only (re)adds changed/new files.

Usage: python scripts/library/ingest_docs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    SRC_A,
    cjk_seg,
    clean_body,
    decode_bytes,
    doc_id_for,
    norm_title,
    open_db,
)


def main() -> int:
    conn = open_db()
    have = {r[0]: r[1] for r in conn.execute(
        "SELECT doc_id, n_chars FROM documents WHERE source='A'")}
    added = skipped = bad = 0
    rows = []
    for p in sorted(SRC_A.rglob("*.doc")):
        did = doc_id_for(p)
        raw = p.read_bytes()
        text = decode_bytes(raw)
        if not text or len(text.strip()) < 80:
            bad += 1
            continue
        body = clean_body(text)
        if len(body) < 80:
            bad += 1
            continue
        if did in have and have[did] == len(body):
            skipped += 1
            continue
        category = p.parent.name
        title = norm_title(p.name)
        rows.append((did, "A", category, title, str(p), body, len(body)))

    with conn:
        for did, src, cat, title, path, body, n in rows:
            # INSERT OR REPLACE reuses a fresh internal rowid on replace;
            # delete the stale FTS partition first, then attach to the new rowid.
            old = conn.execute(
                "SELECT rowid FROM documents WHERE doc_id=?", (did,)
            ).fetchone()
            if old:
                conn.execute("DELETE FROM docs_fts WHERE rowid=?", (old[0],))
            conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (did, src, cat, title, path, body, n),
            )
            rid = conn.execute(
                "SELECT rowid FROM documents WHERE doc_id=?", (did,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO docs_fts (rowid, title, body) VALUES (?,?,?)",
                (rid, cjk_seg(title), cjk_seg(body[:20000])),
            )
            added += 1

    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
    print(f"docs: upserted={added} skipped={skipped} unreadable={bad} | total={total} fts={fts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
