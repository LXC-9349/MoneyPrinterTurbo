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
1. **只依据给定的【事实材料】写作**。材料里有的年份、地点、人物、事件必须准确使用;
   材料没写的细节不要编造。宁可少讲一件事，也不要编。
   **禁止原文照抄**：材料里的句子只能用来提取事实，你必须用自己的话重新组织表达；
   不要把材料中的整句直接搬进旁白（会触发侵权校验）。
   材料里的人物对话不要复述，改写成间接叙述。
2. 语言：{language}。口语化解说，适合配音朗读，句子短，不用书面语长句。
3. 充分挖掘材料：把材料中的具体年份、事件、数字、对话场景用起来，
   不要写成空泛的赞美（禁止"他是一位伟大的…"这类没有信息的句子）。
4. 总时长 {target_seconds} 秒左右（约 {target_words} 字旁白），由 {shot_count} 个分镜组成。
5. 叙事结构：hook（开场钩子，15字内，用材料里最有反差的细节制造悬念）
   → timeline（**严格按时间先后**的人生分镜）→ closing（结尾升华，落到具体的事实或影响上）。
6. **每个分镜必须讲一件不同的事**，不得重复同一主题、同一句话的意思。
   观众看完应该能说出这个人一生中 8 个以上具体节点。
7. 每个分镜必须给出 visual_type：
   - historical_photo：需要展示该人物本人或同时代真实照片的镜头（人物关键节点必须用这个）
   - generated_scene：抽象概念/内心世界/难以找到照片的场景
   - b_roll：过渡镜头（时代背景、地理环境、氛围画面）
8. visual_query 是给历史照片档案库用的**英文**检索词，1-4个词，
   例如 "Einstein patent office 1905"、"Albert Einstein young portrait"。
   必须包含人物英文名，便于档案库检索到本人照片。
9. duration_hint 要根据该分镜内容长度**真实估算**（内容多则长、少则短），
   不要所有分镜都填同一个数字；总和约等于 {target_seconds}。

# 输出格式
只输出 JSON，不要 markdown 代码块，不要解释文字。结构：
{{"hook": "开场钩子文案",
 "timeline": [
   {{"narration": "该分镜旁白", "duration_hint": 16,
    "visual_type": "historical_photo", "visual_query": "Albert Einstein young portrait"}}
 ],
 "closing": "结尾升华文案"}}"""


def _target_words(target_seconds: int, language: str) -> int:
    # Chinese narration lands around 4.5 chars/second at 1.2x edge-tts speed.
    if language.lower().startswith("zh") or not language:
        return int(target_seconds * 4.5)
    return int(target_seconds * 2.4)  # English words per second


def build_fact_material(person: str, extra_context: str = "", base_doc_ids: list[str] | None = None) -> str:
    """Assemble the grounding corpus.

    Base-script mode (``base_doc_ids`` non-empty): the user has explicitly
    selected which biography articles the narration must come from. Those
    articles become the *only* source (the automatic mined fact pack is skipped
    entirely, so the user's selection is authoritative and never contaminated
    by auto-mining). Wikipedia is reduced to the ``人物身份`` identity line
    only — that text carries no facts the narration can be verified against,
    but it helps the model pick an ``visual_query`` (e.g. an English name).
    If the selected ids resolve to nothing, we fall back to the automatic
    path rather than failing the task.

    Auto mode (``base_doc_ids`` empty/None): mined fact pack + Wikipedia summary,
    order of authority:
      1. local corpus fact pack (article fact sentences + book excerpts,
         chapter-ordered) — richest and dated,
      2. Wikipedia summary — authoritative but often only a 150-char stub,
      3. thin-corpus backfill (kept for subjects with no local match at all).
    """
    base_ids = [x for x in (base_doc_ids or []) if x is not None]
    if base_ids:
        grounding = ""
        try:
            from app.services.archives import historical_archive

            grounding = historical_archive.person_grounding(person)
        except Exception:
            grounding = {}
        base_material = base_script_material(person, base_ids)
        if base_material:
            parts: list[str] = [base_material]
            desc = (grounding or {}).get("wikipedia_description", "") or ""
            if desc:
                parts.append(f"【人物身份】{desc}")
            if extra_context:
                parts.append(f"【补充材料】\n{extra_context}")
            return "\n\n".join(parts)
        # base ids resolved to nothing: fall through to the automatic path below

    parts: list[str] = []
    try:
        from app.services import person_facts

        pack = person_facts.build_fact_pack(person)
        if pack is not None:
            rendered = pack.render()
            if rendered:
                parts.append(f"【本地史料库 — {person}】\n{rendered}")
    except Exception as exc:
        logger.warning(f"fact pack build failed: {type(exc).__name__}")

    try:
        from app.services.archives import historical_archive

        grounding = historical_archive.person_grounding(person)
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
# 单篇基础脚本正文上限:事实来源允许较完整保留,但仍须控制 prompt 体积。
_BASE_SCRIPT_DOC_CHARS = 4000


def base_script_material(person: str, base_doc_ids: list[str]) -> str:
    """渲染用户勾选的基础脚本(传记文章)作为唯一事实来源。

    逐篇读取并拼接;每篇(标题行 + 正文)整体截断到 _BASE_SCRIPT_DOC_CHARS,
    保证 ``material.count(某字)`` 不超上限(标题行本身也会占字符位)。
    无文章、正文为空或 doc_id 全部解析不到时返回空串,由调用方决定降级。
    """
    from app.services import library

    docs = library.get_docs(base_doc_ids or [])
    parts: list[str] = []
    for d in docs:
        body = (d.get("body") or "").strip()
        if not body:
            continue
        title = str(d.get("title") or "").strip() or f"{d.get('doc_id')}"
        block = f"《{title}》\n{body}"
        parts.append(block[:_BASE_SCRIPT_DOC_CHARS])
    if not parts:
        return ""
    return f"【基础脚本 — {person}】\n" + "\n\n".join(parts)


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
    base_doc_ids: list[str] | None = None,
) -> str:
    target_seconds = max(MIN_TARGET_SECONDS, min(MAX_TARGET_SECONDS, int(target_seconds)))
    shot_count = max(6, min(24, int(shot_count)))
    material = build_fact_material(person, extra_context, base_doc_ids or None)
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
# 2-6 char prefix regex otherwise captures phrases like "一个不断更换国",
# "斯坦出生在德国" or a verb+place ("选择九嵕山"). The reliable signal is a
# locative preposition: facts state places as "在/于/至/到 + PLACE".
_PLACE = re.compile(r"(?:在|于|至|到|自|前往|定居于)\s*([\u4e00-\u9fff]{2,6}(?:市|省|县|城|国|州|岛|河|山|关|宫|殿|陵|寺|府|镇|村))")
_PLACE_TAIL_JUNK = ("年", "月", "日", "的", "了", "个", "们", "地", "时")
_PLACE_VERB_MARKERS = (
    "出生", "去世", "生活", "来自", "定居", "移民", "工作", "学习", "回到",
    "前往", "搬到", "移居", "选择", "成为", "称为", "坐落", "葬于", "死后",
)


def _places_in(text: str) -> set[str]:
    """Extract place names that follow a locative preposition."""
    out = set()
    for m in _PLACE.finditer(text):
        token = m.group(1)
        prefix = token[:-1]
        if not prefix or prefix[-1] in _PLACE_TAIL_JUNK:
            continue
        if 1 <= len(prefix) <= 4 and any(marker in prefix for marker in _PLACE_VERB_MARKERS):
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
    corpus_places = _places_in(corpus)
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
    base_doc_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """Generate + verify a structured biography script. None on total failure.

    ``base_doc_ids`` (the user's selected biography articles) switches the
    corpus to base-script mode: the selected articles are the sole source,
    the automatic mined fact pack is skipped, the verbatim guard compares
    against the base corpus, and the result records provenance via
    ``base_script_doc_ids``. Empty/None preserves the existing auto-mined path.
    """
    base_doc_ids = [d for d in (base_doc_ids or []) if d]
    base_mode = bool(base_doc_ids)
    from app.services import llm
    from app.services import person_facts

    prompt = build_biography_prompt(
        person,
        language=language,
        target_seconds=target_seconds,
        shot_count=shot_count,
        extra_context=extra_context,
        base_doc_ids=base_doc_ids if base_mode else None,
    )
    corpus = build_fact_material(person, extra_context, base_doc_ids if base_mode else None)
    logger.info(
        f"generating biography script: person={person}, mode={'base_script' if base_mode else 'auto'}, "
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

            # Copyright guard: narration must not echo a source passage. Facts
            # are free, expression is not, so any shot overlapping the source
            # corpus is flagged for review before it ships. In base mode the
            # corpus is the user's selected articles (checked against that);
            # in auto mode it is the mined fact pack.
            verbatim = []
            try:
                if base_mode:
                    verbatim = person_facts.check_verbatim_against(shots, [corpus])
                else:
                    pack = person_facts.build_fact_pack(person)
                    verbatim = person_facts.check_verbatim(shots, pack)
                    if verbatim:
                        logger.warning(
                            f"verbatim risk in {len(verbatim)} shot(s): "
                            + ", ".join(f"#{v['shot']}({v['overlap']})" for v in verbatim[:5])
                        )
            except Exception as exc:
                logger.warning(f"verbatim check failed: {type(exc).__name__}: {exc}")

            # Quoted speech lifted from a source is the riskiest overlap; retry
            # once with the same material rather than shipping a near-quotation.
            # In auto mode a verbatim hit triggers one retry; in base mode we
            # do NOT re-mine (the user's selection is authoritative), we only
            # record the flag on the result.
            if not base_mode and verbatim and attempt == 0:
                logger.warning("retrying script generation to remove verbatim overlap")
                continue

            result = {
                "person": person,
                "hook": str(data.get("hook") or "").strip(),
                "timeline": shots,
                "closing": str(data.get("closing") or "").strip(),
                "facts_check": {**check, "verbatim_flagged": len(verbatim)},
                "language": language,
                "target_seconds": target_seconds,
            }
            if base_mode:
                result["base_script_doc_ids"] = base_doc_ids
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
