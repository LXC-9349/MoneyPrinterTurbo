"""M4#1 评测基线脚本。

对 4 个人物，逐镜搜索候选素材并记录：来源/标题/作者/相关性得分/独立素材数/覆盖率。
结果摘要写入 docs（不提交图片）。把已知坏例作为回归测试。

运行：
    .venv\\Scripts\\python.exe -m scripts.eval_material_baseline
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.schema import MaterialInfo
from app.services.archives import search_loc_images, search_wikimedia_images
from app.services.relevance_gate import THRESHOLDS, score_relevance

# 4 人物及其手动定义的分镜查询（避免 LLM 依赖，专注评测检索质量）
PERSONS: dict[str, dict[str, Any]] = {
    "曹操": {
        "aliases": ["Cao Cao", "孟德", "Emperor Wu of Wei"],
        "era_keywords": ["Three Kingdoms", "Han Dynasty", "Chinese general"],
        "shots": [
            {"narration": "曹操出身宦官家族，少年机警任侠", "query": "Cao Cao portrait"},
            {"narration": "官渡之战以少胜多击败袁绍", "query": "Battle of Guandu"},
            {"narration": "赤壁之战曹操大败", "query": "Battle of Red Cliffs"},
            {"narration": "曹操统一北方，推行屯田", "query": "Cao Cao Wei kingdom"},
            {"narration": "曹操既是军事家也是诗人", "query": "Cao Cao poetry calligraphy"},
        ],
    },
    "爱因斯坦": {
        "aliases": ["Albert Einstein", "Einstein"],
        "era_keywords": ["physicist", "relativity", "Nobel Prize", "20th century"],
        "shots": [
            {"narration": "爱因斯坦1879年生于乌尔姆", "query": "Albert Einstein young portrait"},
            {"narration": "1905年奇迹年发表光电效应论文", "query": "Einstein 1905 patent office"},
            {"narration": "广义相对论改变宇宙观", "query": "Einstein general relativity blackboard"},
            {"narration": "1921年获诺贝尔物理学奖", "query": "Einstein Nobel Prize"},
            {"narration": "晚年致力于统一场论", "query": "Einstein Princeton elderly"},
        ],
    },
    "李世民": {
        "aliases": ["Li Shimin", "唐太宗", "Emperor Taizong of Tang"],
        "era_keywords": ["Tang Dynasty", "Chinese emperor", "7th century"],
        "shots": [
            {"narration": "李世民出身陇西李氏", "query": "Emperor Taizong portrait"},
            {"narration": "玄武门之变夺位", "query": "Xuanwu Gate"},
            {"narration": "贞观之治开创盛世", "query": "Tang Dynasty Chang'an"},
            {"narration": "击败东突厥被尊天可汗", "query": "Tang Dynasty Turkic"},
            {"narration": "凌烟阁二十四功臣", "query": "Lingyun Pavilion painters"},
        ],
    },
    "伊本·赫勒敦": {  # 冷门人物
        "aliases": ["Ibn Khaldun", "ابن خلدون"],
        "era_keywords": ["14th century", "historian", "North Africa", "Arab scholar"],
        "shots": [
            {"narration": "伊本·赫勒敦1332年生于突尼斯", "query": "Ibn Khaldun Tunis"},
            {"narration": "《历史绪论》开创历史哲学", "query": "Ibn Khaldun Muqaddimah manuscript"},
            {"narration": "周旋于北非各宫廷", "query": "14th century North Africa MADRASAH"},
            {"narration": "晚年移居开罗", "query": "Ibn Khaldun Cairo Al-Azhar"},
            {"narration": "被尊为社会学之父", "query": "Ibn Khaldun statue"},
        ],
    },
}


@dataclass
class ShotResult:
    narration: str
    query: str
    candidates: list[dict] = field(default_factory=list)
    relevant_count: int = 0
    independent_assets: int = 0
    sources: list[str] = field(default_factory=list)


@dataclass
class PersonResult:
    name: str
    shots: list[ShotResult] = field(default_factory=list)
    coverage: float = 0.0  # 有相关素材的镜头占比


def _compute_relevance(item: MaterialInfo, name: str, person: dict) -> float:
    """委托给真实门槛 relevance_gate.score_relevance，保证评测反映上线行为。

    返回 0-1 相关性得分（LoC 已关闭裸子串匹配）。
    """
    si = item.source_info or {}
    score, _reason = score_relevance(
        item,
        person_name=name,
        aliases=person.get("aliases", []),
        era_keywords=person.get("era_keywords", []),
        provider=str(si.get("provider") or item.provider or ""),
    )
    return score


def _near_duplicate_key(item: MaterialInfo) -> str:
    """近重复归一键：按 asset_id 去文件名扩展名/大小写，回退到 URL。

    同一历史照片在 Commons 常以不同尺寸/文件名多次命中，按素材身份而非
    URL 去重才能得到真实“独立素材数”。
    """
    si = item.source_info or {}
    asset = str(si.get("asset_id") or "").strip()
    if asset:
        base = os.path.splitext(asset)[0].lower().replace("file:", "").strip()
        if base:
            return base
    return (item.url or "").split("?")[0].rstrip("/").lower()


# 礼貌节流：Commons/LoC 会对突发请求限流（429/403），污染基线数字。
# 每次检索后固定等待，命中 0 结果再退避重试一次，拿到可复现的公平读数。
_THROTTLE_SEC = float(os.environ.get("M4_EVAL_THROTTLE", "2.0"))
_RETRY_BACKOFF_SEC = float(os.environ.get("M4_EVAL_BACKOFF", "6.0"))


def _search_one(fn, query: str, limit: int) -> list[MaterialInfo]:
    """带节流与一次退避重试的单源检索（不因限流把空结果误当真实覆盖）。"""
    try:
        out = list(fn(query, limit=limit) or [])
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] {fn.__name__} {query!r} failed: {exc}")
        out = []
    if not out:
        time.sleep(_RETRY_BACKOFF_SEC)
        try:
            out = list(fn(query, limit=limit) or [])
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] {fn.__name__} retry {query!r} failed: {exc}")
            out = []
    time.sleep(_THROTTLE_SEC)
    return out


def evaluate_person(name: str, data: dict) -> PersonResult:
    """逐镜搜索候选素材并评分。"""
    result = PersonResult(name=name)
    covered = 0

    standard_cutoff = THRESHOLDS["standard"]
    for shot in data["shots"]:
        sr = ShotResult(narration=shot["narration"], query=shot["query"])
        candidates = []

        # 逐镜独立检索；单个源限流（429/403）经节流重试后仍为空则如实记录。
        candidates.extend(_search_one(search_wikimedia_images, shot["query"], 8))
        candidates.extend(_search_one(search_loc_images, shot["query"], 4))

        for item in candidates:
            relevance = _compute_relevance(item, name, data)
            si = item.source_info or {}
            sr.candidates.append({
                "provider": item.provider,
                "title": str(si.get("asset_id") or "")[:100],
                "author": str((si.get("creator") or {}).get("name") or "")[:60],
                "source_page": str(si.get("source_page") or ""),
                "license": str(si.get("license") or ""),
                "relevance": relevance,
            })
            if relevance >= standard_cutoff:
                sr.relevant_count += 1

        # 独立素材数：按素材身份做近重复去重（而非原始 URL）。
        unique_assets = {_near_duplicate_key(item) for item in candidates}
        sr.independent_assets = len({k for k in unique_assets if k})
        sr.sources = sorted({item.provider for item in candidates})

        if sr.relevant_count > 0:
            covered += 1
        result.shots.append(sr)

    result.coverage = covered / len(data["shots"]) if data["shots"] else 0
    return result


def format_report(results: list[PersonResult]) -> str:
    lines = [
        "# M4 素材相关性评测基线",
        "",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
    ]
    for r in results:
        lines.append(f"## {r.name}")
        lines.append(f"- 覆盖率: {r.coverage:.0%}（{sum(1 for s in r.shots if s.relevant_count > 0)}/{len(r.shots)} 镜头有相关素材）")
        lines.append("")
        lines.append("| 镜头 | 查询 | 候选数 | 相关数 | 独立素材 | 来源 |")
        lines.append("|------|------|:--:|:--:|:--:|------|")
        for s in r.shots:
            srcs = ",".join(s.sources) or "无"
            lines.append(
                f"| {s.narration[:20]}… | {s.query[:25]} | {len(s.candidates)} | {s.relevant_count} | {s.independent_assets} | {srcs} |"
            )
        lines.append("")
    return "\n".join(lines)


def main():
    print("=== M4 评测基线（网络搜索模式）===\n")
    results = []
    for name, data in PERSONS.items():
        print(f"评估 {name}...")
        r = evaluate_person(name, data)
        results.append(r)
        print(f"  覆盖率: {r.coverage:.0%}, 镜头: {len(r.shots)}")

    report = format_report(results)
    out_path = os.path.join("docs", "M4_EVALUATION_BASELINE.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告已写入: {out_path}")

    # 导出 JSON 供回归测试使用
    json_data = []
    for r in results:
        json_data.append({
            "name": r.name,
            "coverage": r.coverage,
            "shots": [
                {
                    "query": s.query,
                    "relevant_count": s.relevant_count,
                    "independent_assets": s.independent_assets,
                    "top_candidates": s.candidates[:3],
                }
                for s in r.shots
            ],
        })
    json_path = os.path.join("docs", "M4_EVALUATION_DATA.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    print(f"JSON 数据: {json_path}")


if __name__ == "__main__":
    main()
