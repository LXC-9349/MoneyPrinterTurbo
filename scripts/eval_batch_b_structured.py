"""批次 B 验收评测：人物结构化取材 对照 旧逐镜关键词检索。

对 4 人物（曹操/李世民/爱因斯坦/伊本·赫勒敦）：
  旧口径 = 每镜关键词 search_wikimedia_images + relevance_gate（人名子串）；
  新口径 = resolve_qid → Commons 分类树/P180 depicts/P18 结构化取材。
输出覆盖率与相关独立素材数对照表，写入 docs。全程无 key（Wikidata/Commons
公开接口），仅联网，节流礼貌访问。

运行：
    .venv\\Scripts\\python.exe -m scripts.eval_batch_b_structured
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.schema import MaterialInfo
from app.services.archives import commons, wikidata
from app.services.archives import search_wikimedia_images
from app.services.relevance_gate import THRESHOLDS, score_relevance

PERSONS: dict[str, dict[str, Any]] = {
    "曹操": {"en": "Cao Cao", "aliases": ["Cao Cao", "孟德"],
             "era": ["Three Kingdoms", "Han Dynasty"],
             "shots": ["Cao Cao portrait", "Battle of Guandu", "Battle of Red Cliffs",
                       "Cao Cao Wei kingdom", "Cao Cao poetry calligraphy"]},
    "李世民": {"en": "Emperor Taizong of Tang", "aliases": ["Li Shimin", "唐太宗"],
               "era": ["Tang Dynasty", "Chinese emperor"],
               "shots": ["Emperor Taizong portrait", "Xuanwu Gate", "Tang Dynasty Chang'an",
                         "Tang Dynasty Turkic", "Lingyun Pavilion painters"]},
    "爱因斯坦": {"en": "Albert Einstein", "aliases": ["Einstein"],
                 "era": ["physicist", "relativity"],
                 "shots": ["Albert Einstein young portrait", "Einstein 1905 patent office",
                           "Einstein general relativity blackboard", "Einstein Nobel Prize",
                           "Einstein Princeton elderly"]},
    "伊本·赫勒敦": {"en": "Ibn Khaldun", "aliases": ["Ibn Khaldun"],
                   "era": ["14th century", "historian", "North Africa"],
                   "shots": ["Ibn Khaldun Tunis", "Ibn Khaldun Muqaddimah manuscript",
                             "14th century North Africa madrasah", "Ibn Khaldun Cairo Al-Azhar",
                             "Ibn Khaldun statue"]},
}

_THROTTLE = float(os.environ.get("B_EVAL_THROTTLE", "2.0"))


def _ident(item: MaterialInfo) -> str:
    si = item.source_info or {}
    asset = str(si.get("asset_id") or "").strip()
    if asset:
        return os.path.splitext(asset)[0].lower().replace("file:", "").strip()
    return (item.url or "").split("?")[0].rstrip("/").lower()


def _scene_match(pool: list[MaterialInfo], shot: str) -> int:
    """镜头关键词命中池内素材标题/描述/分类的数量（场景级匹配，非纯复用）。"""
    toks = [t for t in shot.lower().replace("/", " ").split() if len(t) > 3]
    hits = 0
    for it in pool:
        si = it.source_info or {}
        text = " ".join([
            str(si.get("asset_id") or ""), str(si.get("description") or ""),
            str(si.get("category") or ""),
        ]).lower()
        if any(t in text for t in toks):
            hits += 1
    return hits


def eval_old(name: str, data: dict) -> dict:
    """旧口径：逐镜关键词检索 + 门槛。"""
    cutoff = THRESHOLDS["standard"]
    per_shot_relevant: list[int] = []
    independent: set[str] = set()
    for shot in data["shots"]:
        try:
            cands = list(search_wikimedia_images(shot, limit=8) or [])
        except Exception:  # noqa: BLE001
            cands = []
        time.sleep(_THROTTLE)
        rel = 0
        for item in cands:
            score, _ = score_relevance(
                item, person_name=name, aliases=data["aliases"],
                era_keywords=data["era"], provider=str(item.provider or ""),
            )
            if score >= cutoff:
                rel += 1
                independent.add(_ident(item))
        per_shot_relevant.append(rel)
    covered = sum(1 for r in per_shot_relevant if r > 0)
    total = len(data["shots"]) or 1
    return {
        "relevant_independent": len(independent),
        "coverage": covered / total,
        "per_shot": per_shot_relevant,
    }


def eval_new(name: str, data: dict) -> dict:
    """新口径：QID 结构化取材。"""
    qid = wikidata.resolve_qid(name, lang="zh", force=True)
    if not qid:
        qid = wikidata.resolve_qid(data["en"], lang="en", force=True)
    aliases = list(dict.fromkeys(data["aliases"] + wikidata.labels_and_aliases(qid))) if qid else data["aliases"]
    category = wikidata.commons_category(qid) if qid else ""
    pool = commons.collect_qid_materials(qid, category=category, limit=40) if qid else []
    time.sleep(_THROTTLE)
    independent = {_ident(it) for it in pool if _ident(it)}
    cutoff = THRESHOLDS["standard"]
    # 结构化取材的素材 relevance_basis 命中即判相关。
    relevant = [it for it in pool
                if score_relevance(it, person_name=name, aliases=aliases,
                                   era_keywords=data["era"], provider=str(it.provider or ""))[0] >= cutoff]
    scene_covered = sum(1 for shot in data["shots"] if _scene_match(relevant, shot) > 0)
    total = len(data["shots"]) or 1
    person_relevant_cov = 1.0 if relevant else 0.0
    return {
        "qid": qid,
        "category": category,
        "pool_size": len(pool),
        "relevant_independent": len({_ident(it) for it in relevant}),
        "scene_coverage": scene_covered / total,
        "person_relevant_coverage": person_relevant_cov,
    }


def main():
    rows = []
    for name, data in PERSONS.items():
        print(f"评估 {name} ...", flush=True)
        old = eval_old(name, data)
        new = eval_new(name, data)
        rows.append({"name": name, "old": old, "new": new})
        print(f"  旧: {old['relevant_independent']}张/{old['coverage']:.0%}  "
              f"新: {new['relevant_independent']}张 scene={new['scene_coverage']:.0%} "
              f"person={new['person_relevant_coverage']:.0%} qid={new.get('qid')}", flush=True)

    lines = [
        "# 批次 B 人物取材评测：结构化(QID+Commons) 对照 旧关键词检索",
        "",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
        "口径：旧 = 每镜关键词 Wikimedia 检索 + 相关性门槛；新 = Wikidata QID → "
        "Commons 分类树(depth≤2,剪枝流行文化)/haswbstatement:P180 depicts/P18 结构化取材。",
        "",
        "| 人物 | QID | 相关独立素材(旧→新) | 覆盖率(旧→新·场景/人物) |",
        "|------|:--:|:--:|:--:|",
    ]
    for r in rows:
        o, n = r["old"], r["new"]
        lines.append(
            f"| {r['name']} | {n.get('qid') or '—'} | {o['relevant_independent']} → {n['relevant_independent']} "
            f"| {o['coverage']:.0%} → {n['scene_coverage']:.0%} / {n['person_relevant_coverage']:.0%} |"
        )
    lines += [
        "",
        "> 场景覆盖率 = 有标题/描述/分类命中镜头关键词的相关素材的镜头占比；",
        "> 人物覆盖率 = 结构化相关池非空（该人物的任何一镜都能取到人物相关的档案图，",
        "> 区别于旧版可能拿到无关库存）。相关池素材均带 relevance_basis（category/depicts/P18），",
        "> 经门槛直接判相关，不再依赖脆弱的人名文件名子串。",
    ]
    out_md = os.path.join("docs", "BATCH_B_EVALUATION.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    out_json = os.path.join("docs", "BATCH_B_EVALUATION.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n对照表: {out_md}\n数据:   {out_json}")


if __name__ == "__main__":
    main()
