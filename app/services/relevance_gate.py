"""M4#2 相关性门槛模块。

检索结果必须与人物姓名、别名、朝代/时代关键词匹配后才可用。
低于阈值的丢弃并记提醒。阈值提供严格/标准/宽松三档。
LoC 关闭裸子串匹配。
"""
from __future__ import annotations

import re
from typing import Any

from app.models.schema import MaterialInfo

# 三档阈值
THRESHOLDS = {
    "strict": 0.7,    # 只保留 asset_id/标题中直接包含人物名的结果
    "standard": 0.4,  # 允许时代关键词命中
    "loose": 0.2,     # 仅排除完全无关的噪声
}


def score_relevance(
    item: MaterialInfo,
    *,
    person_name: str,
    aliases: list[str],
    era_keywords: list[str],
    provider: str = "",
) -> tuple[float, str]:
    """对单条候选打相关性分（0-1），返回 (score, reason)。

    评分规则（从高到低短路）：
    - 0.95: asset_id/title 中包含人名/别名（强匹配）
    - 0.7:  source_page 的 collection 名包含人名
    - 0.5:  asset_id/title 包含时代关键词
    - 0.3:  search_term 本身包含人名但结果不相关（弱关联）
    - 0.1:  无任何关键词命中（噪声，应被丢弃）

    LoC 特殊处理：关闭裸子串匹配（loc.gov 全文搜索极宽泛，
    子串命中不代表相关性），仅检查 asset_id 和 title 字段。
    """
    si = item.source_info or {}
    # 结构化相关性（批次 B）：来自人物 Commons 分类树、depicts 该 QID、或
    # Wikidata P18 配图的候选，其相关性由结构而非文件名子串证明，直接判相关。
    basis = str(si.get("relevance_basis") or "").strip().lower()
    if basis in ("category", "depicts", "wikidata_image"):
        return 0.98, f"结构化相关：{basis}"
    all_names = [person_name] + (aliases or [])
    # 归一化：文件名用下划线，人名用空格；统一成小写+空格再比较。
    def _norm(s: str) -> str:
        return s.lower().replace("_", " ").replace("-", " ")

    match_fields = {
        "asset_id": _norm(str(si.get("asset_id") or "")),
        "source_page": _norm(str(si.get("source_page") or "")),
        "credit": _norm(str(si.get("credit") or "")),
        "description": _norm(str(si.get("description") or "")),
    }
    # LoC 关闭裸子串匹配：只检查 asset_id（标题/id），不检查 source_page 全文
    if provider == "loc" or si.get("provider") == "loc":
        check_fields = ["asset_id"]
    else:
        check_fields = ["asset_id", "source_page", "credit", "description"]

    # 强匹配：名称在 asset_id 中
    asset_lower = match_fields["asset_id"]
    for name in all_names:
        name_lower = _norm(name)
        if not name_lower:
            continue
        if name_lower in asset_lower:
            return 0.95, f"名称 '{name}' 命中 asset_id"

    # 中等匹配：名称在 source_page 或 credit 中
    for field in check_fields:
        if field == "asset_id":
            continue
        text = match_fields[field]
        for name in all_names:
            name_lower = _norm(name)
            if name_lower and name_lower in text:
                return 0.7, f"名称 '{name}' 命中 {field}"

    # 时代关键词匹配（只查 asset_id 和 credit）
    for kw in (era_keywords or []):
        kw_lower = _norm(kw)
        if not kw_lower:
            continue
        for field in check_fields:
            if kw_lower in match_fields[field]:
                return 0.5, f"时代关键词 '{kw}' 命中 {field}"

    # 搜索词命中但结果本身无名称（弱关联）
    search_term = _norm(str(si.get("search_term") or ""))
    for name in all_names:
        name_lower = _norm(name)
        if name_lower and name_lower in search_term:
            return 0.3, "搜索词包含人名但结果标题无关联"

    return 0.1, "未命中任何关键词"


def filter_candidates(
    items: list[MaterialInfo],
    *,
    person_name: str,
    aliases: list[str],
    era_keywords: list[str],
    threshold: str = "standard",
) -> tuple[list[MaterialInfo], list[dict]]:
    """过滤候选列表，返回 (通过列表, 被丢弃记录)。

    threshold 可选 strict/standard/loose，对应不同分数门槛。
    被丢弃记录包含 {provider, title, score, reason} 供提醒使用。
    """
    cutoff = THRESHOLDS.get(threshold, THRESHOLDS["standard"])
    passed: list[MaterialInfo] = []
    discarded: list[dict] = []

    for item in items:
        si = item.source_info or {}
        score, reason = score_relevance(
            item,
            person_name=person_name,
            aliases=aliases,
            era_keywords=era_keywords,
            provider=str(si.get("provider") or item.provider or ""),
        )
        if score >= cutoff:
            passed.append(item)
        else:
            discarded.append({
                "provider": item.provider,
                "title": str(si.get("asset_id") or "")[:120],
                "source_page": str(si.get("source_page") or ""),
                "score": score,
                "reason": reason,
                "url": item.url[:100] if item.url else "",
            })

    return passed, discarded
