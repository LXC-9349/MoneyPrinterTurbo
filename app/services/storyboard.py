"""普通视频分镜脚本(结构化),让"画面-解说"按镜对齐而非一组全局主题词。

动机(基于真实缺陷):普通视频上游只生成**一段扁平口播 + 5 个全局关键词**。
后果有三:
1. 画面与解说不同步 —— 关键词是全片通用的,后面的内容会提前出现在前面的画面里;
2. 分镜时长无差异 —— 模型收到"总时长≈目标"这种模糊指令时会填同一个数字;
3. 素材取用靠全局词,难以覆盖叙事推进。

传记模式已有结构化分镜(``biography.generate_structured_script``),本模块把
**同一套能力**带到普通视频:输出 ``{hook, timeline[{narration, duration_hint,
visual_query, visual_type}], closing}``,并复用 ``biography`` 的检索词/类型提取
函数与 ``shot_audio`` 的逐镜配音,使普通视频也有真实的逐镜时长与画面对齐。

与传记的差别:普通视频**不**做人物事实核查、不绑定档案库;``visual_type``
统一为 ``b_roll``(库存视频/生成图),检索词是通用英文短语而非"人物英文名+年份"。
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

# 每个分镜最多覆盖多少秒(与 video_clip_duration 的常规取值相称)。
_MIN_SHOT_SECONDS = 4
_MAX_SHOT_SECONDS = 18
_MAX_OUTPUT_TOKENS = 3000

SYSTEM_PROMPT = """你是资深短视频编剧,为{language}的短视频撰写**分镜脚本**。

# 硬性要求
1. 主题:{subject}。口语化、有画面感、适合配音朗读,句子短。
2. 全文由 {shot_count} 个分镜组成,总时长约 {target_seconds} 秒(约 {target_words} 字)。
3. **每个分镜讲一个不同的点**,不得重复同一句话的意思。
   叙事要有推进:开场抓人 → 分点展开 → 收尾落到具体感受或结论。
4. **duration_hint 必须逐镜真实估算**(内容多则长、少则短),总和≈{target_seconds}。
   **严禁**所有分镜填同一个数字。
5. 每个分镜给出 visual_query:给库存素材库用的**英文**检索词,2-5 个词,
   描述该镜画面(例:"busy city street morning"、"cup of coffee steam")。
   必须是可拍摄的真实画面,不要抽象概念。
6. 禁止空话套话("这非常美好"这类没有信息的句子)。

# 输出格式
只输出 JSON,不要 markdown 代码块,不要多余文字:
{{"hook": "开场文案",
 "timeline": [
   {{"narration": "该镜解说", "duration_hint": 8,
    "visual_query": "english stock footage phrase", "visual_type": "b_roll"}}
 ],
 "closing": "收尾文案"}}"""


def _target_words(target_seconds: int, language: str) -> int:
    if language.lower().startswith("zh") or not language:
        return int(target_seconds * 4.5)
    return int(target_seconds * 2.4)


def _extract_json(text: str) -> dict[str, Any]:
    """容错解析:剥掉 markdown 围栏,截取最外层 JSON 对象。"""
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except Exception:
            return {}
    return {}


def _coerce_shots(timeline: list, target_seconds: int) -> list[dict[str, Any]]:
    """校验/归一化分镜:去空、修时长、抑制"全同"时长。"""
    shots: list[dict[str, Any]] = []
    for raw in timeline:
        if not isinstance(raw, dict):
            continue
        narration = str(raw.get("narration") or "").strip()
        query = str(raw.get("visual_query") or "").strip()
        if not narration or not query:
            continue
        try:
            hint = float(raw.get("duration_hint") or 0)
        except (TypeError, ValueError):
            hint = 0.0
        if hint < _MIN_SHOT_SECONDS or hint > _MAX_SHOT_SECONDS:
            hint = (target_seconds / max(1, len(timeline)))
        shots.append(
            {
                "narration": narration,
                "duration_hint": round(hint, 1),
                "visual_query": query,
                "visual_type": "b_roll",
            }
        )
    if not shots:
        return []

    # 抑制"每个分镜时长完全相同"——质量塌陷的典型信号。按解说字数重排时长,
    # 保持总和不变,使节奏有真实起伏。
    hints = [s["duration_hint"] for s in shots]
    if len(set(hints)) == 1 and len(shots) > 2:
        weights = [max(1, len(s["narration"])) for s in shots]
        total_w = sum(weights)
        total_t = hints[0] * len(shots)
        for s, w in zip(shots, weights):
            s["duration_hint"] = round(max(_MIN_SHOT_SECONDS, total_t * w / total_w), 1)
    return shots


def generate_storyboard(
    subject: str,
    *,
    language: str = "zh-CN",
    target_seconds: int = 45,
    shot_count: int = 6,
    style: str = "",
    custom_system_prompt: str = "",
    app_config=None,
) -> dict[str, Any] | None:
    """生成普通视频的分镜脚本;失败返回 None(调用方回退到扁平脚本)。"""
    from app.services import llm

    shot_count = max(3, min(int(shot_count or 6), 12))
    target_seconds = max(15, min(int(target_seconds or 45), 300))
    target_words = _target_words(target_seconds, language)

    system = SYSTEM_PROMPT.format(
        language=language or "中文",
        subject=subject,
        shot_count=shot_count,
        target_seconds=target_seconds,
        target_words=target_words,
    )
    user_bits = [f"主题:{subject}", f"分镜数:{shot_count}", f"目标总时长:{target_seconds}秒"]
    if style:
        user_bits.append(f"风格要求:{style}")
    prompt = system + "\n\n" + "\n".join(user_bits)

    logger.info(
        f"generating storyboard: subject={subject}, shot_count={shot_count}, "
        f"target_seconds={target_seconds}"
    )
    for attempt in range(3):
        try:
            response = llm._generate_response(prompt, app_config=app_config)
            if not response or str(response).startswith("Error: "):
                logger.warning(f"storyboard attempt {attempt + 1} failed: {str(response)[:120]}")
                continue
            data = _extract_json(response)
            shots = _coerce_shots(data.get("timeline") or [], target_seconds)
            if not shots:
                logger.warning("storyboard missing usable timeline, retrying")
                continue
            return {
                "hook": str(data.get("hook") or "").strip(),
                "timeline": shots,
                "closing": str(data.get("closing") or "").strip(),
            }
        except Exception as exc:
            logger.warning(f"storyboard attempt {attempt + 1} raised: {type(exc).__name__}: {exc}")
    return None


def storyboard_to_script(storyboard: dict[str, Any]) -> str:
    """把分镜脚本拼成一段连续旁白(供配音/字幕整体使用)。

    模型常把 hook 同时写进第 1 个分镜、closing 写进最后 1 个分镜,导致开头/结尾
    重复。这里做去重:若 hook 与首个分镜旁白实质相同则不重复追加,closing 同理。
    """
    def _norm(text: str) -> str:
        # 去空白 + 中英文常见标点,仅用于"是否实质相同"的比较。
        return re.sub(
            r"[\s,\u3002\uff0c!\uff01?\uff1f;\uff1b:\uff1a\"'\u201c\u201d\u2018\u2019]",
            "",
            str(text or ""),
        ).strip()

    timeline_texts = [
        str(shot.get("narration") or "").strip()
        for shot in (storyboard.get("timeline") or [])
        if str(shot.get("narration") or "").strip()
    ]
    first_norm = _norm(timeline_texts[0]) if timeline_texts else ""
    last_norm = _norm(timeline_texts[-1]) if timeline_texts else ""

    parts: list[str] = []
    hook = str(storyboard.get("hook") or "").strip()
    if hook and _norm(hook) != first_norm:
        parts.append(hook)
    parts.extend(timeline_texts)
    closing = str(storyboard.get("closing") or "").strip()
    if closing and _norm(closing) != last_norm:
        parts.append(closing)
    return "\n\n".join(parts)


def storyboard_search_terms(storyboard: dict[str, Any]) -> list[str]:
    """按分镜顺序提取画面检索词,与逐镜配音顺序 1:1 对齐。"""
    terms: list[str] = []
    for shot in storyboard.get("timeline") or []:
        query = str(shot.get("visual_query") or "").strip()
        if query:
            terms.append(query)
    return terms


def storyboard_shot_count(storyboard: dict[str, Any]) -> int:
    return len(storyboard.get("timeline") or [])