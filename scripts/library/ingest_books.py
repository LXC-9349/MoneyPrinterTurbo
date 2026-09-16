"""Ingest source B: 人物传记合集 (mobi/epub/azw3 -> chapters -> SQLite).

Quality-gated and resumable:
  * --limit N        process only N books (smoke test first)
  * books.status     pending -> done | failed; re-runs only touch pending/failed
  * chapter bodies land in `chapters`; FTS is built by build_fts.py after all
    books are ingested, so a bad extraction never pollutes the index silently.

Usage:
  python scripts/library/ingest_books.py --limit 5
  N_WORKERS=4 python scripts/library/ingest_books.py
"""
import argparse
import os
import re
import sys
import shutil
import tempfile
import traceback
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    SRC_B,
    book_id_for,
    html_to_text,
    norm_title,
    open_db,
)

_MIN_CHAPTER_CHARS = 40


def _epub_chapters(epub_path: str):
    """Return (title, text) chapters in spine order; fall back to filename order."""
    with zipfile.ZipFile(epub_path) as z:
        names = z.namelist()
        opf_name = next((n for n in names if n.lower().endswith(".opf")), None)
        manifest, spine_ids = {}, []
        base = ""
        if opf_name:
            opf = z.read(opf_name).decode("utf-8", "ignore")
            base = str(Path(opf_name).parent.as_posix())
            if base == ".":
                base = ""
            for m in re.finditer(
                r"<item\b[^>]*?\bid=\"([^\"]+)\"[^>]*?\bhref=\"([^\"]+)\"[^>]*?/?>",
                opf,
            ):
                manifest[m.group(1)] = m.group(2)
            for m in re.finditer(r"<itemref\b[^>]*?\bidref=\"([^\"]+)\"", opf):
                spine_ids.append(m.group(1))

        ordered = []
        for sid in spine_ids:
            href = manifest.get(sid)
            if not href:
                continue
            name = href.lstrip("/")
            if base and not name.startswith(base):
                name = f"{base}/{name}"
            if name not in names:
                cand = [n for n in names if Path(n).name == Path(name).name]
                if cand:
                    name = cand[0]
            if name in names:
                ordered.append(name)
        if not ordered:
            ordered = sorted(
                n for n in names
                if n.lower().endswith((".xhtml", ".html", ".htm"))
            )

        chapters = []
        for name in ordered:
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            try:
                raw_bytes = z.read(name)
            except Exception:
                continue
            raw = None
            for enc in ("utf-8", "gb18030"):
                try:
                    raw = raw_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if raw is None:
                raw = raw_bytes.decode("utf-8", "ignore")
            text = html_to_text(raw)
            if len(text) < _MIN_CHAPTER_CHARS:
                continue
            title = ""
            mt = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
            if mt:
                title = re.sub(r"\s+", " ", mt.group(1)).strip()[:120]
            chapters.append((title, text))
        return chapters


def _split_long_text(text: str, max_chars: int = 8000):
    """Split bare-HTML books into pseudo-chapters at paragraph boundaries.

    A single 400k-char chapter would dominate FTS matches and give the script
    writer no usable citation granularity, so chunk on blank lines and keep the
    first short line of each chunk as its heading when it looks like one.
    """
    paras = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    chunks = []
    buf = []
    size = 0
    for p in paras:
        buf.append(p)
        size += len(p)
        if size >= max_chars:
            chunks.append(buf)
            buf, size = [], 0
    if buf:
        chunks.append(buf)
    out = []
    for chunk in chunks:
        body = "\n".join(chunk)
        first = chunk[0].split("\n")[0].strip()
        title = first[:80] if len(first) <= 40 and not first.endswith("。") else ""
        out.append((title, body))
    return out


def _patch_mobi_ncx_codec():
    """Tolerate mis-declared TOC encodings inside the mobi package.

    Some Kindle7 books declare utf-8 but embed GBK/CP936 text in the NCX
    nav map (巴菲特传/杰克·韦尔奇自传). A decode failure there aborts the
    whole extraction even though the book HTML is fine. Swap the two
    `.decode(self.mh.codec)` call sites for an error-tolerant variant.
    """
    import mobi.mobi_ncx as mn

    if not mn.__file__:
        return
    src = Path(mn.__file__).read_text(encoding="utf-8")
    patched = src.replace(
        "self.mh.codec)", "self.mh.codec, errors='replace')"
    )
    if patched == src:
        return
    # Re-exec into the ORIGINAL module dict: kindleunpack holds a direct
    # reference to this module object, so swapping sys.modules is invisible.
    exec(compile(patched, mn.__file__, "exec"), mn.__dict__)
    # kindleunpack did `from .mobi_ncx import ncxExtract` at import time, so
    # it still points at the OLD class; rebind that alias too.
    import mobi.kindleunpack as ku

    ku.ncxExtract = mn.ncxExtract


def process_book(path_str: str):
    path = Path(path_str)
    ext = path.suffix.lower()
    tmp = None
    try:
        if ext in (".mobi", ".azw3"):
            import mobi

            _patch_mobi_ncx_codec()

            out = mobi.extract(str(path))
            if isinstance(out, tuple):
                tmp, produced = out[0], out[1]
            else:
                produced = out
                tmp = str(Path(produced).parent.parent)
            # mobi7/KF7 books extract to a bare .html; KF8 -> .epub
            if produced.lower().endswith(".epub"):
                chapters = _epub_chapters(produced)
            else:
                raw = Path(produced).read_bytes().decode("utf-8", "ignore")
                text = html_to_text(raw)
                chapters = _split_long_text(text) if len(text) >= _MIN_CHAPTER_CHARS else []
        elif ext == ".epub":
            chapters = _epub_chapters(str(path))
        else:
            return "failed", f"unsupported format {ext}", [], 0
        total = sum(len(t) for _, t in chapters)
        return "done", "", chapters, total
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}"[:280], [], 0
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def register_books(conn):
    """Ensure every source book has a row; return list of (book_id, path) to do."""
    existing = dict(conn.execute("SELECT book_id, status FROM books"))
    todo = []
    for p in sorted(SRC_B.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".mobi", ".epub", ".azw3"):
            continue
        bid = book_id_for(p)
        if bid not in existing:
            title = norm_title(p.name)
            m = re.search(r"作者[：:]\s*([^._/]+)", p.name)
            author = m.group(1).strip() if m else ""
            conn.execute(
                "INSERT OR IGNORE INTO books "
                "(book_id, source, title, author, path, format, status) "
                "VALUES (?, 'B', ?, ?, ?, ?, 'pending')",
                (bid, title, author, str(p), p.suffix.lstrip(".")),
            )
            todo.append((bid, str(p)))
        elif existing[bid] in ("pending", "failed"):
            todo.append((bid, str(p)))
    conn.commit()
    return todo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sample", type=int, default=None,
                    help="pick N random pending books across formats (quality probe)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    conn = open_db()
    todo = register_books(conn)
    if args.sample:
        import random

        rng = random.Random(args.seed)
        by_fmt = {}
        for item in todo:
            fmt = Path(item[1]).suffix.lower()
            by_fmt.setdefault(fmt, []).append(item)
        picked = []
        per = max(1, args.sample // max(len(by_fmt), 1))
        for fmt, items in by_fmt.items():
            picked.extend(rng.sample(items, min(per, len(items))))
        rng.shuffle(picked)
        todo = picked[: args.sample]
    if args.limit:
        todo = todo[: args.limit]
    print(f"books to process: {len(todo)} (workers={os.environ.get('N_WORKERS','4')})", flush=True)

    workers = int(os.environ.get("N_WORKERS", "4"))
    done = failed = 0

    def commit_one(bid, path_str, result):
        nonlocal done, failed
        status, err, chapters, n_chars = result
        title = norm_title(Path(path_str).name)
        with conn:
            if status == "done" and chapters:
                conn.execute("DELETE FROM chapters WHERE book_id=?", (bid,))
                conn.executemany(
                    "INSERT INTO chapters (book_id, idx, title, body) VALUES (?,?,?,?)",
                    [(bid, i, t, b) for i, (t, b) in enumerate(chapters)],
                )
                conn.execute(
                    "UPDATE books SET status='done', n_chapters=?, n_chars=?, "
                    "error='', processed_at=datetime('now') WHERE book_id=?",
                    (len(chapters), n_chars, bid),
                )
                done += 1
            else:
                conn.execute(
                    "UPDATE books SET status='failed', error=?, "
                    "processed_at=datetime('now') WHERE book_id=?",
                    (err or "no chapters", bid),
                )
                failed += 1
                print(f"  FAIL {title[:30]}: {err}", flush=True)

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(process_book, p): (bid, p) for bid, p in todo}
        for fut in as_completed(futs):
            bid, p = futs[fut]
            try:
                result = fut.result()
            except Exception:
                result = ("failed", traceback.format_exc()[-200:], [], 0)
            commit_one(bid, p, result)
            if (done + failed) % 50 == 0:
                print(f"progress done={done} failed={failed}/{len(todo)}", flush=True)

    print(f"BOOKS done={done} failed={failed} | table total="
          f"{conn.execute('SELECT COUNT(*) FROM books').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
