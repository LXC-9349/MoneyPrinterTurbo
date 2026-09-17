"""Structured biography script generation + lightweight fact checking (phase 3).

The upstream script prompt produces a flat 60-second口播 for stock footage.
A biography video needs a *structured* script because every shot must declare
what kind of visual it needs (real historical photo vs. atmosphere b-roll), and
the material dispatcher routes on that field.

Output contract (JSON):
  {
    "person": "爱因斯坦",
    "hook": "开场钩子",
    "timeline": [
      {"narration": "…", "duration_hint": 12,
       "visual_type": "historical_photo|generated_scene|b_roll",
       "visual_query": "English archive search phrase",
       "year": 1905, "verified": true, "unverified_reasons": []}
    ],
    "closing": "结尾升华",
    "facts_check": {"checked": 12, "unverified": 2}
  }

Fact checking is deliberately cheap and offline: a year/place/name mentioned in
a shot must appear in the grounding corpus (Wikipedia summary or the local
biography library). Anything unsupported is flagged ``verified: false`` and
listed in ``unverified_reasons`` so a human (or a stricter prompt) can react —
we never silently drop the claim, and we never block video generation.
"""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

# Total narration budget for a 3-5 minute video.
MIN_TARGET_SECONDS = 180
MAX_TARGET_SECONDS = 300
# Model output ceiling for one structured script (gateway default is too small).
_MAX_OUTPUT_TOKENS = 4000

SYSTEM_PROMPT = """你是资深纪录片编剧，专门为短视频平台（抖音/B站/YouTube）撰写**人物传记解说**。

# 硬性要求
1. 只依据给定的【事实材料】写作，不得虚构史料。材料没写的细节不要编造。
2. 语言：{language}。口语化解说，适合配音朗读，句子短，不用书面语长句。
3. 总时长 {target_seconds} 秒左右（约 {target_words} 字旁白），由多个分镜组成。
4. 叙事结构：hook（开场钩子，15字内，制造悬念或反差）→ timeline（按时间顺序的人生分镜）
   → closing（结尾升华，点出这个人物的历史意义，不要空洞赞美）。
5. 每个分镜必须给出 visual_type：
   - historical_photo：需要展示该人物本人或同时代真实照片的镜头（人物关键节点必须用这个）
   - generated_scene：抽象概念/内心世界/难以找到照片的场景
   - b_roll：过渡镜头（时代背景、地理环境、氛围画面）
6. visual_query 是给历史照片档案库用的**英文**检索词，1-4个词，
   例如 "Einstein patent office 1905"、"Albert Einstein young portrait"。
   必须包含人物英文名，便于档案库检索到本人照片。
7. 只在有把握时写具体年份/地点/人名，这些会逐条核对。

# 输出格式
只输出 JSON，不要 markdown 代码块，不要解释文字。结构：
{{"hook": "开场钩子文案",
 "timeline": [
   {{"narration": "该分镜旁白", "duration_hint": 12,
    "visual_type": "historical_photo", "visual_query": "Albert Einstein young portrait"}}
 ],
 "closing": "结尾升华文案"}}
分镜数量 {shot_count} 个左右，duration_hint 单位为秒，所有分镜 duration_hint 之和约等于 {target_seconds}。"""


def _target_words(target_seconds: int, language: str) -> int:
    # Chinese narration lands around 4.5 chars/second at 1.2x edge-tts speed.
    if language.lower().startswith("zh") or not language:
        return int(target_seconds * 4.5)
    return int(target_seconds * 2.4)  # English words per second


def build_fact_material(person: str, extra_context: str = "") -> str:
    """Assemble the grounding corpus: local library + Wikipedia summary.

    A Chinese Wikipedia stub can be only ~150 chars, which makes year checks
    meaningless. When the corpus is thin the local biography books (which the
    library pipeline already indexed) supply real dated passages.
    """
    parts: list[str] = []
    try:
        from app.services.archives import historical_archive

        grounding = historical_archive.person_grounding(person)
        block = grounding.get("library_block", "")
        if block:
            parts.append(block)
        extract = grounding.get("wikipedia_extract", "")
        if extract:
            parts.append(f"【维基百科摘要】\n{extract}")
        desc = grounding.get("wikipedia_description", "")
        if desc:
            parts.append(f"【人物身份】{desc}")
    except Exception as exc:
        logger.warning(f"biography grounding failed: {type(exc).__name__}")

    if extra_context:
        parts.append(f"【补充材料】\n{extra_context}")

    joined = "\n\n".join(parts)
    if len(joined) < _THIN_CORPUS_CHARS:
        book_facts = _facts_from_local_books(person)
        if book_facts:
            parts.append(book_facts)

    if not parts:
        parts.append(f"（未检索到 {person} 的外部资料，请只写广泛公认的史实）")
    return "\n\n".join(parts)


# Below this the corpus cannot support per-year fact checks.
_THIN_CORPUS_CHARS = 600


def _facts_from_local_books(person: str, max_passages: int = 6) -> str:
    """Pull dated passages about ``person`` from the indexed biography books.

    Passages are truncated hard: book bodies are third-party copyright, so they
    may inform the script's facts but must never be reproduced at length.
    """
    try:
        import re

        from app.services import library

        conn = library.get_conn()
        if conn is None:
            return ""
        rows = conn.execute(
            "SELECT b.title, c.body FROM books b JOIN chapters c ON c.book_id=b.book_id "
            "WHERE b.title LIKE ? ORDER BY c.idx LIMIT 40",
            (f"%{person}%",),
        ).fetchall()
        if not rows:
            return ""
        passages: list[str] = []
        year_re = re.compile(r"(1[0-9]{3}|20[0-2][0-9])\s*年|(公元)?(1[0-9]{3})\s*年")
        for title, body in rows:
            if len(passages) >= max_passages:
                break
            for para in re.split(r"\n+", body or ""):
                para = para.strip()
                if len(para) < 30 or not year_re.search(para):
                    continue
                # Cap each passage: facts only, never a bulk quotation.
                passages.append(f"《{title[:20]}》{para[:200]}")
                break
        if not passages:
            return ""
        return "【书目中的相关记载(仅作事实参考,不得原文引用)】\n" + "\n".join(passages)
    except Exception as exc:
        logger.warning(f"local book fact extraction failed: {type(exc).__name__}")
        return ""


def build_biography_prompt(
    person: str,
    *,
    language: str = "zh-CN",
    target_seconds: int = 240,
    shot_count: int = 14,
    extra_context: str = "",
) -> str:
    target_seconds = max(MIN_TARGET_SECONDS, min(MAX_TARGET_SECONDS, int(target_seconds)))
    shot_count = max(6, min(24, int(shot_count)))
    material = build_fact_material(person, extra_context)
    return (
        SYSTEM_PROMPT.format(
            language=language or "zh-CN",
            target_seconds=target_seconds,
            target_words=_target_words(target_seconds, language),
            shot_count=shot_count,
        )
        + f"\n\n# 事实材料\n{material}\n\n# 传主\n{person}\n\n请开始输出 JSON。"
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in response: {text[:120]!r}")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------- fact checks

_YEAR = re.compile(r"(公元)?(公元前)?(\d{2,4})\s*年")
_EN_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")
# 「人名+地名」rough extractors: 2-6 CJK chars before a role/place suffix.
_PLACE = re.compile(r"[\u4e00-\u9fff]{2,6}(市|省|县|城|国|州|岛|河|山)")
_NAME_HINT = re.compile(r"[\u4e00-\u9fff]{2,4}(?=[，。、；:：]?[^\u4e00-\u9fff]*)")


def _years_in(text: str) -> set[str]:
    years = {m.group(3) for m in _YEAR.finditer(text)}
    years |= {m.group(1) for m in _EN_YEAR.finditer(text)}
    return {y.lstrip("0") or "0" for y in years}


def _corpus_years(text: str) -> set[str]:
    years = {m.group(3) for m in _YEAR.finditer(text)}
    years |= {m.group(1) for m in _EN_YEAR.finditer(text)}
    return {y.lstrip("0") or "0" for y in years}


# A place token is only meaningful when it is a real geographic name; a naive
# 2-6 char prefix regex otherwise captures phrases like "一个不断更换国" or
# "斯坦出生在德国". Guards: the token must start on a name boundary (not
# mid-word), must not contain sentence verbs, and must not be a common
# noun+国 pattern (美国/德国 are real, 年美国/各国 are not).
_PLACE_STOPWORDS = (
    "整", "个", "这", "那", "哪", "们", "了", "的", "在", "他", "她", "它",
    "不断", "回头", "后来", "当时", "那时", "异", "各", "全", "本", "该",
    "出生", "去世", "生活", "出生在", "来自", "定居", "移民", "出生於",
)
# Suffix-only single chars that never form a place on their own.
_PLACE_TAIL_JUNK = ("年", "月", "日", "的", "了", "个", "们", "地", "时")
# Verbs/adverbs that indicate the match ran into the middle of a clause.
_PLACE_VERB_MARKERS = (
    "出生", "去世", "生活", "来自", "定居", "移民", "工作", "学习", "回到",
    "前往", "搬到", "移居", "在", "到", "去", "从", "他", "她", "它",
)


def _places_in(text: str) -> set[str]:
    """Extract plausible place names, rejecting mid-clause regex noise."""
    out = set()
    for m in _PLACE.finditer(text):
        token = m.group(0)
        prefix = token[:-1]
        # Order matters: reject junk tails and noise prefixes before the length
        # check, otherwise "年美国" (prefix len 1) slips through the <2 guard.
        if prefix and prefix[-1] in _PLACE_TAIL_JUNK:
            continue
        if len(prefix) < 2 or len(prefix) > 4:
            continue
        if any(prefix.startswith(s) for s in _PLACE_STOPWORDS):
            continue
        # A place prefix must not embed a verb/pronoun: 斯坦出生在德国 is noise,
        # while 德国/乌尔姆/普林斯顿 are real.
        if any(marker in prefix for marker in _PLACE_VERB_MARKERS):
            continue
        out.add(token)
    return out


def verify_shots(shots: list[dict[str, Any]], corpus: str) -> dict[str, Any]:
    """Flag years/places in each shot that the grounding corpus does not support.

    Deliberately conservative: only explicit 年份 and real 地点 tokens are
    checked. Names are skipped because alias forms make false positives far too
    likely. A year is counted as supported when it appears in the corpus, when
    it is a ±1 neighbour of a corpus year (sources disagree between 1904/1905
    for the same event), or when the corpus is too thin to judge — a short
    Wikipedia stub must not mark a correct birth year as unverified.
    """
    corpus_years = _corpus_years(corpus)
    corpus_places = {m.group(0) for m in _PLACE.finditer(corpus)}
    low_corpus = [int(y) for y in corpus_years if y.isdigit()]
    # Below this size the corpus cannot support per-year claims, so year checks
    # downgrade to "reported" rather than asserting the fact is unsupported.
    corpus_is_thin = len(corpus) < 600

    checked = unverified = 0
    for shot in shots:
        text = str(shot.get("narration") or "")
        reasons: list[str] = []
        for year in _years_in(text):
            checked += 1
            if year in corpus_years:
                continue
            if year.isdigit() and any(abs(int(year) - c) <= 1 for c in low_corpus):
                continue
            reasons.append(
                f"年份 {year} 无材料依据" if not corpus_is_thin
                else f"年份 {year} 需人工复核(材料有限)"
            )
        for place in _places_in(text):
            checked += 1
            if place in corpus_places:
                continue
            # Place names are frequently implied rather than stated (生在德国,
            # 19世纪的德国) so only flag when the corpus mentions places at all.
            if corpus_places:
                reasons.append(f"地点 {place} 无材料依据")
        shot["verified"] = not reasons
        shot["unverified_reasons"] = reasons
        if reasons:
            unverified += 1
    return {
        "checked": checked,
        "unverified": unverified,
        "corpus_chars": len(corpus),
        "corpus_thin": corpus_is_thin,
    }


# ------------------------------------------------------------- orchestrations


def generate_structured_script(
    person: str,
    *,
    language: str = "zh-CN",
    target_seconds: int = 240,
    shot_count: int = 14,
    extra_context: str = "",
    app_config=None,
) -> dict[str, Any] | None:
    """Generate + verify a structured biography script. None on total failure."""
    from app.services import llm

    prompt = build_biography_prompt(
        person,
        language=language,
        target_seconds=target_seconds,
        shot_count=shot_count,
        extra_context=extra_context,
    )
    corpus = build_fact_material(person, extra_context)
    logger.info(
        f"generating biography script: person={person}, "
        f"target_seconds={target_seconds}, corpus_chars={len(corpus)}"
    )

    for attempt in range(3):
        try:
            response = llm._generate_response(prompt, app_config=app_config)
            if not response or response.startswith("Error: "):
                logger.warning(f"biography script attempt {attempt + 1} failed: {response[:120]}")
                continue
            data = _extract_json(response)
            timeline = data.get("timeline") or []
            if not isinstance(timeline, list) or not timeline:
                logger.warning("biography script missing timeline, retrying")
                continue

            shots: list[dict[str, Any]] = []
            for raw in timeline:
                if not isinstance(raw, dict):
                    continue
                narration = str(raw.get("narration") or "").strip()
                if not narration:
                    continue
                visual_type = str(raw.get("visual_type") or "historical_photo").strip()
                if visual_type not in ("historical_photo", "generated_scene", "b_roll"):
                    visual_type = "historical_photo"
                try:
                    duration = int(raw.get("duration_hint") or 12)
                except (TypeError, ValueError):
                    duration = 12
                shots.append({
                    "narration": narration,
                    "duration_hint": max(4, min(30, duration)),
                    "visual_type": visual_type,
                    "visual_query": str(raw.get("visual_query") or "").strip(),
                })
            if not shots:
                continue

            check = verify_shots(shots, corpus)
            result = {
                "person": person,
                "hook": str(data.get("hook") or "").strip(),
                "timeline": shots,
                "closing": str(data.get("closing") or "").strip(),
                "facts_check": check,
                "language": language,
                "target_seconds": target_seconds,
            }
            logger.success(
                f"biography script ready: {len(shots)} shots, "
                f"facts checked={check['checked']} unverified={check['unverified']}"
            )
            return result
        except Exception as exc:
            logger.warning(f"biography script parse failed: {type(exc).__name__}: {exc}")

    logger.error(f"failed to generate biography script for {person}")
    return None


def script_to_narration(script: dict[str, Any]) -> str:
    """Flat narration text for TTS/subtitles (hook + timeline + closing)."""
    parts: list[str] = []
    hook = (script.get("hook") or "").strip()
    if hook:
        parts.append(hook)
    for shot in script.get("timeline") or []:
        text = (shot.get("narration") or "").strip()
        if text:
            parts.append(text)
    closing = (script.get("closing") or "").strip()
    if closing:
        parts.append(closing)
    return "\n\n".join(parts)


def script_to_search_terms(script: dict[str, Any]) -> list[str]:
    """Visual queries in shot order — becomes ``video_terms`` for material search."""
    terms: list[str] = []
    for shot in script.get("timeline") or []:
        query = (shot.get("visual_query") or "").strip()
        if query:
            terms.append(query)
    return terms


def script_shot_visual_types(script: dict[str, Any]) -> list[str]:
    """visual_type list aligned 1:1 with ``script_to_search_terms`` order."""
    types: list[str] = []
    for shot in script.get("timeline") or []:
        if (shot.get("visual_query") or "").strip():
            types.append(str(shot.get("visual_type") or "historical_photo"))
    return types
