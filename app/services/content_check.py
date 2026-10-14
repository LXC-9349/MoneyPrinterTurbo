"""内容质量预检(生成前 / 生成后均可用的确定性检查)。

定位:把"内容质量"从主观评价变成**可复现的客观信号**,并在管线里留痕,
让 UI 能提示用户"这条脚本可能偏短/偏长/重复"。所有检查都是确定性的,
不调用大模型,失败也不阻断交付 —— 只写结论,由调用方决定是否提示。

结论写入 script.json 的 ``content_report``:
    {
      "passed": bool,
      "issues": ["too_short", "repeated_sentences", ...],
      "metrics": {"chars": int, "sentences": int, "est_seconds": float,
                  "target_seconds": int, "unique_ratio": float}
    }
"""
from __future__ import annotations

import re
from typing import Any

# 中文按字计,英文按词计,统一折算为"口播秒数"的经验系数。
_ZH_CHARS_PER_SEC = 5.0   # 中文播音约 5 字/秒
_EN_WORDS_PER_SEC = 2.6   # 英文播音约 2.6 词/秒
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。!?!?\n…]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def estimate_seconds(text: str) -> float:
    """估算口播时长(秒):中文字数/英文词数分别折算后取和。"""
    text = text or ""
    cjk = len(_CJK.findall(text))
    non_cjk = _CJK.sub(" ", text)
    words = len([w for w in re.split(r"\s+", non_cjk) if re.search(r"[A-Za-z0-9]", w)])
    return round(cjk / _ZH_CHARS_PER_SEC + words / _EN_WORDS_PER_SEC, 1)


def check_script(
    text: str,
    target_seconds: float = 0,
    min_seconds: float = 0,
    min_chars: int = 0,
) -> dict[str, Any]:
    """对生成的脚本文本做确定性内容检查。

    - 无文本 → ``empty_script``(硬问题)。
    - 估算时长与目标偏差过大 → ``too_short`` / ``too_long``(仅在给了目标时)。
    - 句子高度重复(独特句占比过低)→ ``repeated_sentences``。
    - 含未替换的占位词(如"仔细观察"式模板痕迹)→ ``template_placeholder``。
    无法判定的情况不计入 issues,避免误报。
    """
    issues: list[str] = []
    text = (text or "").strip()
    if not text:
        return {
            "passed": False,
            "issues": ["empty_script"],
            "metrics": {"chars": 0, "sentences": 0, "est_seconds": 0.0},
        }

    sentences = _split_sentences(text)
    est = estimate_seconds(text)
    chars = len(text)

    # 句子重复度(独特句占比)。短脚本(<3 句)不判,避免误报。
    unique_ratio = 1.0
    if len(sentences) >= 3:
        unique_ratio = round(len(set(sentences)) / len(sentences), 3)
        if unique_ratio < 0.6:
            issues.append("repeated_sentences")

    # 模板占位痕迹(项目历史上大量出现的填充词)。
    if re.search(r"仔细观察|待补充|TODO|xxx|占位|placeholder", text, re.IGNORECASE):
        issues.append("template_placeholder")

    if min_chars and chars < min_chars:
        issues.append("too_short")

    if target_seconds and target_seconds > 0:
        if est < target_seconds * 0.6:
            issues.append("too_short")
        elif est > target_seconds * 1.8:
            issues.append("too_long")

    return {
        "passed": not issues,
        "issues": issues,
        "metrics": {
            "chars": chars,
            "sentences": len(sentences),
            "est_seconds": est,
            "target_seconds": int(target_seconds or 0),
            "unique_ratio": unique_ratio,
        },
    }