"""Build a dense, timeline-ordered fact pack for one person (phase 3 backbone).

The first attempt at grounding pulled only ~6 short passages, so the LLM fell
back to a 150-char Wikipedia stub and produced repetitive filler. The corpus
actually holds far more: e.g. 爱因斯坦 has 89 chapters / 715k chars across the
indexed books, 李世民 76 chapters / 132k chars plus 63 article titles.

This module mines that properly:

  1. pick the book(s) whose TITLE names the person (biographies are ordered
     chronologically, so chapter order is roughly life order),
  2. harvest paragraphs that carry concrete detail (a year, a place, a number),
  3. de-duplicate near-identical paragraphs across books,
  4. cap each excerpt and the total size so the prompt stays affordable,
  5. attach provenance (book, chapter index) for the verbatim-risk check and
     the compliance sidecar.

Copyright posture: excerpts are used ONLY as input material for fact
extraction. They never reach the finished narration — ``biography.py`` forbids
quoting the source and ``check_verbatim`` flags any shot that overlaps a
source paragraph too closely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# A workable prompt budget: enough detail to write a 4-minute script without
# blowing up the context or the per-call latency.
DEFAULT_MAX_CHARS = 9000
MAX_EXCERPT_CHARS = 420
# Paragraphs must be at least this long to be worth injecting.
MIN_EXCERPT_CHARS = 60

_YEAR = re.compile(r"(公元前?\s*\d{1,4}\s*年|1[0-9]{3}\s*年|20[0-2][0-9]\s*年|\b1[0-9]{3}\b)")
# Detail markers: numbers, ages, titles, places, works — signals of substance.
_DETAIL = re.compile(
    r"(\d+\s*岁|\d+\s*年|第[一二三四五六七八九十]+|《[^》]{2,20}》|"
    r"诺贝尔|教授|博士|大学|学院|皇帝|宰相|将军|总统|首相|战争|战役|"
    r"[0-9]{3,}|[一二三四五六七八九十]十[一二三四五六七八九]?年)"
)
# Paragraphs that are editorial furniture rather than biography.
_NOISE = re.compile(
    r"(目录|版权|免责声明|责任编辑|出版发行|ISBN|内容简介|序言|后记|参考书目|"
    r"本书由|扫描|校对|整理制作|手机阅读|微信|公众号)"
)


@dataclass
class Excerpt:
    book: str
    chapter_idx: int
    chapter_title: str
    text: str
    has_year: bool = False

    def render(self) -> str:
        year_mark = "【含年份】" if self.has_year else ""
        title = f" {self.chapter_title}" if self.chapter_title else ""
        return f"[{self.book}{title}{year_mark}] {self.text}"


@dataclass
class FactPack:
    person: str
    excerpts: list[Excerpt] = field(default_factory=list)
    article_titles: list[str] = field(default_factory=list)
    article_facts: list[str] = field(default_factory=list)
    books: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(e.text) for e in self.excerpts) + sum(len(f) for f in self.article_facts)

    def render(self) -> str:
        parts: list[str] = []
        if self.books:
            parts.append("参考书目: " + "; ".join(self.books[:5]))
        if self.article_titles:
            parts.append(
                f"相关史料文章标题({len(self.article_titles)} 篇,可参考其选题角度): "
                + "、".join(self.article_titles[:12])
            )
        # Article-derived facts come first: the article collection is
        # encyclopaedic and dated, so these are the cleanest fact statements,
        # whereas book passages may be novelised scene-setting.
        if self.article_facts:
            parts.append("史料要点(逐条可直接引用为史实):")
            for idx, fact in enumerate(self.article_facts, start=1):
                parts.append(f"F{idx}. {fact}")
        if self.excerpts:
            parts.append("生平记载(按书籍章节顺序,时间线自上而下;含场景描写,仅供提取事实):")
            for idx, ex in enumerate(self.excerpts, start=1):
                parts.append(f"{idx}. {ex.render()}")
        return "\n".join(parts)


def _norm(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text)


# Dialogue-heavy or scene-setting fiction reads poorly as fact material.
_DIALOGUE = re.compile(r'[“"][^”"]{4,}[”"]')
_QUOTE_RATIO_LIMIT = 0.22


def _dialogue_ratio(para: str) -> float:
    quoted = sum(len(m.group(0)) for m in _DIALOGUE.finditer(para))
    return quoted / max(len(para), 1)


def _mentions_person(para: str, names: set[str]) -> bool:
    return any(name and name in para for name in names)


def _person_names(person: str) -> set[str]:
    """Names that identify the subject inside a passage.

    Biographies switch between full name, given name and style names, so a
    single string is not enough; we accept the full name plus any part >= 2
    chars that is distinctive (e.g. 李世民 / 世民, 爱因斯坦 / 爱因斯坦).
    """
    names = {person}
    if len(person) >= 4:
        # 欧阳修 -> 欧阳修;  李世民 -> 世民 is risky, but for 3-char names the
        # trailing 2 chars are the common short form in Chinese biography.
        names.add(person[-2:])
    elif len(person) == 3:
        names.add(person[-2:])
    return {n for n in names if len(n) >= 2}


def _shingles(text: str, n: int = 12) -> set[str]:
    t = _norm(text)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _is_near_duplicate(candidate: str, kept: list[set[str]], threshold: float = 0.5) -> bool:
    """Cheap near-duplicate test via 12-char shingle overlap."""
    sh = _shingles(candidate)
    if not sh:
        return True
    for existing in kept:
        if not existing:
            continue
        overlap = len(sh & existing) / min(len(sh), len(existing))
        if overlap >= threshold:
            return True
    return False


def _paragraphs(body: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"\n+", body or ""):
        para = re.sub(r"\s+", " ", raw).strip()
        if len(para) < MIN_EXCERPT_CHARS or _NOISE.search(para):
            continue
        out.append(para)
    return out


def _score(para: str, names: set[str]) -> tuple[int, int]:
    """Rank a paragraph for fact value.

    The first attempt weighted year density alone, which surfaced background
    chapters (dynasty politics, city records) that never mention the subject.
    Facts must be ABOUT the person, so mentioning them dominates the score;
    years and concrete detail then break ties. Dialogue-heavy passages (novel
    style) are pushed down hard because quoted speech is not usable as fact.
    """
    mentions = sum(para.count(n) for n in names)
    years = len(_YEAR.findall(para))
    details = len(_DETAIL.findall(para))
    score = mentions * 10 + years * 3 + details
    if _dialogue_ratio(para) > _QUOTE_RATIO_LIMIT:
        score -= 25
    if not mentions:
        score -= 12
    return (score, -len(para))


def _sentence_facts(body: str, names: set[str], max_facts: int = 3) -> list[str]:
    """Extract citable fact sentences about a person from an article.

    The article collection (4942 historical pieces) is encyclopaedic and dated,
    which makes it a cleaner fact source than novelised book prose. We pull
    short sentences that mention the subject AND carry a year or hard detail.

    Sentences must be self-contained: a fact that starts with a dangling quote
    or a continuation marker (``”快到天亮…``, ``这是…之后走出的第一步棋``) reads
    as broken context in the prompt, so those are rejected.
    """
    facts: list[str] = []
    for raw in re.split(r"[。;!?\n]+", body or ""):
        sent = raw.strip().strip("“”\"' ")
        if len(sent) < 20 or len(sent) > 120:
            continue
        if _NOISE.search(sent):
            continue
        # Dangling fragments: leading quote punctuation, or opening with a
        # pronoun connective that only makes sense with the previous sentence.
        if raw.strip()[:1] in ("”", "」", "』", "’"):
            continue
        if re.match(r"^(这|那|它|他|她|于是|因此|所以|但是|然而|而且|并且|同时|后来|接着|结果|其中|此外)", sent):
            continue
        if sent.endswith(("的", "了", "和", "与", "在", "是", "有", "、", "，")):
            continue
        if not _mentions_person(sent, names):
            continue
        if not (_YEAR.search(sent) or _DETAIL.search(sent)):
            continue
        facts.append(sent)
        if len(facts) >= max_facts:
            break
    return facts


def _harvest_article_facts(conn, person: str, names: set[str], max_facts: int = 30) -> tuple[list[str], list[str]]:
    """Return (titles, fact sentences) for articles naming the person.

    Prefers articles whose TITLE names the subject (those are about the person)
    and then fills from body matches, so a much-covered figure yields a rich
    set instead of the handful a single-query pass produces.
    """
    titles: list[str] = []
    facts: list[str] = []
    seen_shingles: list[set[str]] = []
    rows = []
    for query in (
        "SELECT title, body FROM documents WHERE title LIKE ? ORDER BY n_chars ASC LIMIT 80",
        "SELECT title, body FROM documents WHERE body LIKE ? ORDER BY n_chars DESC LIMIT 80",
    ):
        try:
            rows.extend(conn.execute(query, (f"%{person}%",)).fetchall())
        except Exception:
            continue
    seen_titles: set[str] = set()
    for row in rows:
        if len(facts) >= max_facts:
            break
        if row["title"] in seen_titles:
            continue
        seen_titles.add(row["title"])
        titles.append(row["title"])
        for fact in _sentence_facts(row["body"], names, max_facts=3):
            if len(facts) >= max_facts:
                break
            sh = _shingles(fact, 10)
            if any(sh and len(s) and len(sh & s) / max(len(sh), 1) >= 0.6 for s in seen_shingles):
                continue
            seen_shingles.append(sh)
            facts.append(fact)
    return titles, facts


def build_fact_pack(
    person: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_excerpts: int = 48,
    excerpts_per_chapter: int = 1,
) -> FactPack | None:
    """Mine the local corpus for dense, ordered biographical detail.

    Excerpt budget is per-CALL not per-book: a single biography easily yields
    28 usable paragraphs, so the cap must allow several books to contribute
    (otherwise the first book consumes the whole allowance and the timeline is
    written from one author's framing).
    """
    pack = FactPack(person=person)
    try:
        from app.services import library

        conn = library.get_conn()
        if conn is None:
            return None
    except Exception as exc:
        logger.warning(f"fact pack: library unavailable ({type(exc).__name__})")
        return None

    # ---------- articles: titles + citable fact sentences ----------
    names = _person_names(person)
    try:
        pack.article_titles, pack.article_facts = _harvest_article_facts(
            conn, person, names
        )
    except Exception as exc:
        logger.warning(f"fact pack: article harvest failed ({type(exc).__name__})")

    # ---------- books: chapter-ordered concrete detail ----------
    try:
        # Rank documented biographies first: titles with 传/评传/大传 are
        # written as history, while novelised retellings ("卑鄙的圣人 曹操")
        # dramatise scenes and make poor fact material. SQLite has no regex, so
        # rank on several LIKE signals.
        #
        # The source library also holds duplicate copies of some books (same
        # title, near-identical size); dedupe by title so one book cannot take
        # two slots and burn the excerpt budget twice.
        rows = conn.execute(
            "SELECT book_id, title, n_chars FROM books WHERE status='done' "
            "AND title LIKE ? "
            "ORDER BY ("
            "  CASE WHEN title LIKE ? THEN 0 "
            "       WHEN title LIKE ? THEN 1 "
            "       ELSE 2 END), "
            "n_chars DESC LIMIT 24",
            (f"%{person}%", "%传%", f"%{person}·%"),
        ).fetchall()
        books = []
        seen_titles: set[str] = set()
        for row in rows:
            if row["title"] in seen_titles:
                continue
            seen_titles.add(row["title"])
            books.append(row)
            if len(books) >= 6:
                break
    except Exception:
        books = []

    if not books:
        # No title match: fall back to an FTS search over book bodies for the
        # person's name so rarely-titled subjects still get material.
        try:
            hits = conn.execute(
                "SELECT b.book_id, b.title, b.n_chars FROM books_fts f "
                "JOIN books b ON b.rowid = f.rowid "
                "WHERE books_fts MATCH ? LIMIT 4",
                (library._match_query(person),),
            ).fetchall()
            books = hits
        except Exception:
            books = []

    kept_shingles: list[set[str]] = []
    total = sum(len(f) for f in pack.article_facts)
    years_kept = 0
    # Per-book quota so one prolific biography cannot consume the whole budget.
    per_book_cap = max(8, max_excerpts // max(len(books), 1))

    for book in books:
        book_id = book["book_id"]
        book_title = book["title"]
        pack.books.append(book_title)
        book_excerpts = 0
        try:
            chapters = conn.execute(
                "SELECT idx, title, body FROM chapters WHERE book_id=? ORDER BY idx",
                (book_id,),
            ).fetchall()
        except Exception:
            continue

        for ch in chapters:
            if (
                total >= max_chars
                or len(pack.excerpts) >= max_excerpts
                or book_excerpts >= per_book_cap
            ):
                break
            paras = _paragraphs(ch["body"])
            if not paras:
                continue
            # Best paragraphs of this chapter, keeping original order so the
            # timeline stays coherent. Multi-excerpt chapters are allowed for
            # long chapters; the shingle dedup stops repetition.
            per_ch = excerpts_per_chapter
            if len(ch["body"] or "") > 6000:
                per_ch = max(per_ch, 3)
            ranked = sorted(
                ((_score(p, names), i, p) for i, p in enumerate(paras)),
                key=lambda x: x[0],
                reverse=True,
            )[:per_ch]
            for score, _pos, para in sorted(ranked, key=lambda x: x[1]):
                if total >= max_chars or len(pack.excerpts) >= max_excerpts:
                    break
                if score[0] <= 0:
                    continue
                if _is_near_duplicate(para, kept_shingles):
                    continue
                excerpt_text = para[:MAX_EXCERPT_CHARS]
                has_year = bool(_YEAR.search(excerpt_text))
                pack.excerpts.append(
                    Excerpt(
                        book=book_title[:28],
                        chapter_idx=ch["idx"],
                        chapter_title=(ch["title"] or "")[:24],
                        text=excerpt_text,
                        has_year=has_year,
                    )
                )
                kept_shingles.append(_shingles(excerpt_text))
                total += len(excerpt_text)
                book_excerpts += 1
                if has_year:
                    years_kept += 1
        if total >= max_chars or len(pack.excerpts) >= max_excerpts:
            break

    logger.info(
        f"fact pack for {person!r}: {len(pack.excerpts)} excerpts "
        f"({years_kept} with years), {total} chars from {len(pack.books)} books, "
        f"{len(pack.article_titles)} article titles"
    )
    if not pack.excerpts and not pack.article_titles:
        return None
    return pack


def check_verbatim(shots: list[dict[str, Any]], pack: FactPack | None, n: int = 14) -> list[dict]:
    """Flag shots whose narration overlaps a source excerpt too closely.

    Guards the copyright posture: facts are free, expression is not. Uses the
    same shingle idea as the dedup step so a copied clause is caught even when
    surrounding wording changes.
    """
    if not pack or not pack.excerpts:
        return []
    source = [_shingles(e.text, n) for e in pack.excerpts]
    flagged = []
    for idx, shot in enumerate(shots):
        text = _norm(str(shot.get("narration") or ""))
        if len(text) < n:
            continue
        sh = _shingles(text, n)
        worst = 0.0
        for src in source:
            if not src:
                continue
            overlap = len(sh & src) / max(len(sh), 1)
            worst = max(worst, overlap)
        if worst >= 0.25:
            flagged.append({
                "shot": idx,
                "overlap": round(worst, 3),
                "narration": str(shot.get("narration") or "")[:100],
            })
            shot["verbatim_risk"] = round(worst, 3)
    return flagged


def check_verbatim_against(shots: list[dict[str, Any]], corpus_list: list[str], n: int = 14) -> list[dict]:
    """Flag shots whose narration overlaps any given corpus string.

    基础脚本模式专用:与 ``check_verbatim`` 同样的 n-gram shingle 判重思路,
    但语料直接是一组字符串(用户勾选的基础脚本全文),而不是挖掘得到的 FactPack。
    据此可在基础模式里对"照抄所选文章"做版权/照抄守卫,
    同时不触发自动事实挖掘(build_fact_pack)。
    """
    if not shots or not corpus_list:
        return []
    sources = []
    for text in corpus_list:
        src = _shingles(str(text), n)
        if src:
            sources.append(src)
    flagged: list[dict[str, Any]] = []
    for idx, shot in enumerate(shots):
        text = _norm(str(shot.get("narration") or ""))
        if len(text) < n:
            continue
        sh = _shingles(text, n)
        worst = 0.0
        for src in sources:
            if not src:
                continue
            overlap = len(sh & src) / max(len(sh), 1)
            worst = max(worst, overlap)
        if worst >= 0.25:
            flagged.append({
                "shot": idx,
                "overlap": round(worst, 3),
                "narration": str(shot.get("narration") or "")[:100],
            })
            shot["verbatim_risk"] = round(worst, 3)
    return flagged
