"""Shared helpers for the biography library pipeline.

Sources (read-only):
  A: /mnt/d/ai/中国名人古今人物传记   (~4942 GB18030 text .doc files)
  B: /mnt/d/ai/人物传记合集           (~1256 mobi/epub/azw3 books)

Output lives under storage/library/ (gitignored):
  library.sqlite  - documents, books, chapters, FTS5 index, annotations
"""
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / "storage" / "library"
DB_PATH = LIB_DIR / "library.sqlite"

SRC_A = Path("/mnt/d/ai/中国名人古今人物传记")
SRC_B = Path("/mnt/d/ai/人物传记合集")

# trailing SEO junk in A filenames: "…(汉朝刘彻的儿子哪个继皇位).doc"
_TRAILING_PAREN = re.compile(r"[(（][^()（）]*[)）]\s*$")

# trailing "免责声明：以上内容源自网络…" noise in A bodies
_DISCLAIMER = re.compile(
    r"免责声明[:：].*$|本作品为作者原创创作.{0,80}$|用户上传于本书网.{0,60}$",
    re.S,
)


def decode_bytes(raw: bytes) -> str | None:
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def clean_body(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _DISCLAIMER.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # strip zero-width / control chars except newline/tab
    text = re.sub(r"[\ufeff\u200b\u200e\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def norm_title(filename: str) -> str:
    """Strip .doc/.mobi suffix and trailing SEO parenthetical from a title."""
    name = re.sub(r"\.(doc|mobi|epub|azw3)$", "", filename, flags=re.I)
    prev = None
    while prev != name:
        prev = name
        name = _TRAILING_PAREN.sub("", name).strip()
    return name


def cjk_seg(text: str) -> str:
    """Per-character tokenization of CJK for FTS5 unicode61 (substring search).

    ASCII words are kept whole (lowercased); digits are dropped as standalone.
    """
    out = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            out.append(" ")
            out.append(ch)
            out.append(" ")
        elif ch.isalnum() and ch.isascii():
            out.append(ch.lower())
        else:
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,      -- stable hash of source path
    source      TEXT NOT NULL,         -- 'A'
    category    TEXT,                  -- 风云人物/战史风云/...
    title       TEXT NOT NULL,
    path        TEXT NOT NULL,         -- absolute source path (read-only)
    body        TEXT NOT NULL,
    n_chars     INTEGER NOT NULL,
    extracted_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS books (
    book_id     TEXT PRIMARY KEY,      -- stable hash of source path
    source      TEXT NOT NULL,         -- 'B'
    title       TEXT NOT NULL,
    author      TEXT DEFAULT '',
    path        TEXT NOT NULL,
    format      TEXT NOT NULL,         -- mobi/epub/azw3
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/done/failed
    n_chapters  INTEGER DEFAULT 0,
    n_chars     INTEGER DEFAULT 0,
    error       TEXT DEFAULT '',
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS chapters (
    book_id     TEXT REFERENCES books(book_id),
    idx         INTEGER,
    title       TEXT DEFAULT '',
    body        TEXT NOT NULL,
    PRIMARY KEY (book_id, idx)
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, body, content=''
);
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, author, body, content=''
);
CREATE TABLE IF NOT EXISTS annotations (
    target_type TEXT NOT NULL,         -- 'doc' | 'book'
    target_id   TEXT NOT NULL,
    json        TEXT NOT NULL,
    model       TEXT,
    annotated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_books_status ON books(status);
"""


def open_db(check_same_thread: bool = True) -> sqlite3.Connection:
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    return conn


def doc_id_for(path: Path) -> str:
    import hashlib

    return "d-" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def book_id_for(path: Path) -> str:
    import hashlib

    return "b-" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</h[1-6]>|</li>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t]+", " ", text)
    # keep blank-line paragraph boundaries (consumed by _split_long_text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
