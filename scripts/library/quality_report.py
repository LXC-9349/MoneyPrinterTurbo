"""Quality report for ingested books. Read-only against library.sqlite.

Flags suspicious extractions:
  * empty / too-short books
  * garbled text (replacement-char or CJK-poor ratio for Chinese books)
  * single-chapter monsters (extraction missed structure)
  * truncation smell: ends mid-sentence without punctuation

Usage: python scripts/library/quality_report.py [--top 15]
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import open_db  # noqa: E402

_SENT_END = re.compile(r"[。！？…”」）>】.!?\u201d\u2019)\"']\s*$")


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    sample = text[:20000]
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    return cjk / max(len(sample), 1)


def mojibake_ratio(text: str) -> float:
    sample = text[:20000]
    bad = len(re.findall(r"[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]", sample))
    return bad / max(len(sample), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    conn = open_db()
    books = conn.execute(
        "SELECT book_id, title, format, status, n_chapters, n_chars, error FROM books WHERE status!='pending'"
    ).fetchall()
    processed = [b for b in books if b[3] in ("done", "failed")]
    done = [b for b in processed if b[3] == "done"]
    failed = [b for b in processed if b[3] == "failed"]

    issues = []
    for bid, title, fmt, status, nch, nchars, err in done:
        heads = conn.execute(
            "SELECT idx, title, body FROM chapters WHERE book_id=? ORDER BY idx LIMIT 1", (bid,)
        ).fetchone()
        last3 = conn.execute(
            "SELECT body FROM chapters WHERE book_id=? ORDER BY idx DESC LIMIT 3", (bid,)
        ).fetchall()
        tails = last3[0][0] if last3 else ""
        body_sample = (heads[2] if heads else "") + "".join(t[0] for t in last3)
        cr = cjk_ratio(body_sample)
        mr = mojibake_ratio(body_sample)
        # many books end with a TOC/plates/back-matter chunk, so only flag when
        # none of the final three chapters terminates like real prose
        truncated = bool(last3) and not any(_SENT_END.search(t[0][-40:]) for t in last3)
        flags = []
        if nchars < 5000:
            flags.append("too-short")
        if fmt in ("mobi", "epub", "azw3") and cr < 0.05 and re.search(r"[\u4e00-\u9fff]", title):
            flags.append("cjk-poor")
        if mr > 0.005:
            flags.append(f"mojibake:{mr:.3f}")
        if nch == 1 and nchars > 200000:
            flags.append("single-monster")
        if truncated and nchars > 50000:
            flags.append("maybe-truncated")
        if flags:
            issues.append((title[:36], fmt, nch, nchars, ",".join(flags)))

    print(f"processed={len(processed)} done={len(done)} failed={len(failed)}")
    sizes = sorted(b[5] for b in done)
    if sizes:
        print(f"book chars: p10={sizes[len(sizes)//10]} median={sizes[len(sizes)//2]} p90={sizes[len(sizes)*9//10]}")
    print(f"flagged issues: {len(issues)} / {len(done)} done-books")
    for it in issues[: args.top]:
        print("  ISSUE", it)
    if failed:
        print(f"failed (top {min(args.top,len(failed))}):")
        from collections import Counter

        errs = Counter((f[6] or "")[:60] for f in failed)
        for e, n in errs.most_common(args.top):
            print(f"  x{n} {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
