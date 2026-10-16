"""批次 B 人物素材检索：离线单元测试（不联网、不建库）。

覆盖结构化相关性短路、Commons 分类裁剪与取材、Wikidata 解析、场景词兜底、
古代素材分类、确认屏/预览三数指标。网络与数据库全部以桩替换。
"""
from __future__ import annotations

from app.models.schema import MaterialInfo
from app.services import relevance_gate
from app.services import confirmation
from app.services.archives import commons, wikidata, historical_archive


def _mk(source_info: dict, provider: str = "wikimedia") -> MaterialInfo:
    item = MaterialInfo()
    item.provider = provider
    item.url = "https://example.com/img.jpg"
    item.source_info = source_info
    return item


# ── 相关性短路：结构化相关（分类/depicts/P18）不依赖文件名 ──────────────
def test_structured_basis_short_circuits_relevance():
    for basis in ("category", "depicts", "wikidata_image"):
        item = _mk({"relevance_basis": basis, "asset_id": "IMG_000123"})
        score, reason = relevance_gate.score_relevance(
            item, person_name="曹操", aliases=["Cao Cao"], era_keywords=["han"],
            provider="wikimedia",
        )
        assert score == 0.98, basis
        assert "结构化相关" in reason


def test_cao_cao_opaque_filename_now_relevant_via_basis():
    # 旧门槛：无名称子串的不透明文件名 → 0.1（0% 通过的根因）。
    plain = _mk({"asset_id": "File:9876543210.jpg"})
    score, _ = relevance_gate.score_relevance(
        plain, person_name="曹操", aliases=["Cao Cao"], era_keywords=[],
        provider="wikimedia",
    )
    assert score < relevance_gate.THRESHOLDS["standard"]
    # 同一素材若来自分类树则结构化判相关。
    in_cat = _mk({"asset_id": "File:9876543210.jpg", "relevance_basis": "category"})
    score2, _ = relevance_gate.score_relevance(
        in_cat, person_name="曹操", aliases=["Cao Cao"], era_keywords=[],
        provider="wikimedia",
    )
    assert score2 == 0.98


def test_description_alias_match():
    item = _mk({"asset_id": "battle-map", "description": "Portrait of Cao Cao 曹操"})
    score, reason = relevance_gate.score_relevance(
        item, person_name="曹操", aliases=["Cao Cao"], era_keywords=[],
        provider="wikimedia",
    )
    assert score >= 0.4 and "命中" in reason


# ── Commons 分类裁剪 + 取材（mock _get，无网络）────────────────────────
def test_is_pop_culture_prunes_fan_art():
    assert commons._is_pop_culture("Cao Cao in popular culture")
    assert commons._is_pop_culture("X Fan art")
    assert commons._is_pop_culture("曹操题材电子游戏")
    assert not commons._is_pop_culture("Portrait paintings of Cao Cao")
    assert not commons._is_pop_culture("Cao Cao")


def test_collect_qid_materials_dedupes_and_orders(monkeypatch):
    calls = {"n": 0}

    def fake_get(params, timeout=(15, 40)):
        action = params.get("action")
        gen = params.get("generator")
        # depicts search
        if gen == "search":
            return {"query": {"pages": {
                "1": {"title": "File:depicts1.jpg"},
                "2": {"title": "File:shared.jpg"},
            }}}
        # categorymembers: root has shared.jpg, subcat has sub1.jpg
        if action == "query" and params.get("list") == "categorymembers":
            calls["n"] += 1
            if params.get("cmtitle") == "Category:Cao Cao":
                return {"query": {"categorymembers": [
                    {"title": "File:shared.jpg", "ns": 6},
                    {"title": "Category:Cao Cao - Fan art", "ns": 14},
                    {"title": "Category:Paintings of Cao Cao", "ns": 14},
                ]}}
            if params.get("cmtitle") == "Category:Paintings of Cao Cao":
                return {"query": {"categorymembers": [
                    {"title": "File:sub1.jpg", "ns": 6},
                ]}}
            return {}
        # imageinfo
        if params.get("prop") == "imageinfo":
            titles = params.get("titles", "")
            out = {"query": {"pages": {}}}
            for t in titles.split("|"):
                out["query"]["pages"][t] = {"title": t, "imageinfo": [{
                    "width": 1200, "height": 1600,
                    "thumburl": "https://upload.example/" + t.replace(":", "_"),
                    "descriptionurl": "https://commons.example/" + t,
                    "extmetadata": {
                        "LicenseShortName": {"value": "Public domain"},
                        "Artist": {"value": "Anonymous"},
                        "Credit": {"value": "Commons"},
                        "ImageDescription": {"value": "portrait"},
                    },
                }]}
            return out
        return {}

    monkeypatch.setattr(commons, "_get", fake_get)
    # P18 image claim
    monkeypatch.setattr(wikidata, "entity_data",
                        lambda qid, props="labels|aliases|claims": {
                            "claims": {"P18": [{"mainsnak": {"datavalue": {"value": "File:p18.jpg"}}}],
                                       "P373": [{"mainsnak": {"datavalue": {"value": "Cao Cao"}}}]}
                        })
    # P18 resolution goes through materials_from_titles → imageinfo
    items = commons.collect_qid_materials("Q1", category="Cao Cao", limit=40)
    ids = {i.source_info["asset_id"] for i in items}
    # shared.jpg 只能出现一次（去重），pop-culture 子分类被裁剪。
    assert sum(1 for x in ids if x == "File:shared.jpg") == 1
    assert "File:sub1.jpg" in ids
    assert all(i.source_info.get("relevance_basis") in
               ("category", "depicts", "wikidata_image") for i in items)
    assert all(i.source_info.get("license") for i in items)


# ── Wikidata 人类校验 + 解析（mock _get）──────────────────────────────
def test_is_human_requires_p31_q5():
    human = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]}}
    not_human = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q1234"}}}}]}}
    assert wikidata._is_human(human) is True
    assert wikidata._is_human(not_human) is False


def test_resolve_qid_prefers_human(monkeypatch):
    def fake_get(params, timeout=(15, 30)):
        if params.get("action") == "wbsearchentities":
            return {"search": [{"id": "Q9999"}, {"id": "Q100"}]}
        if params.get("action") == "wbgetentities":
            qid = params.get("ids")
            if qid == "Q100":
                return {"entities": {"Q100": {"claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]}}}}
            return {"entities": {qid: {"claims": {}}}}
        return {}

    monkeypatch.setattr(wikidata, "_get", fake_get)
    wikidata._QID_CACHE.clear()
    wikidata._ENTITY_CACHE.clear()
    qid = wikidata.resolve_qid("曹操", lang="zh", force=True)
    assert qid == "Q100"


# ── 场景词兜底：剔除人名 ──────────────────────────────────────────────
def test_scene_fallback_terms_strips_person_name(monkeypatch):
    monkeypatch.setattr(
        historical_archive, "person_gate_context",
        lambda person: {"name": person, "aliases": ["Cao Cao", "孟德"],
                        "era_keywords": ["three kingdoms"], "qid": ""},
    )
    terms = historical_archive.scene_fallback_terms(
        "曹操", ["曹操 battle", "Cao Cao portrait", "ancient china army"]
    )
    joined = " | ".join(terms).lower()
    assert "曹操" not in joined
    assert "cao cao" not in joined
    assert "孟德" not in joined
    # 仍保留场景/主题词
    assert any("battlefield" in t.lower() or "china" in t.lower() for t in terms)


# ── 古代素材分类 ─────────────────────────────────────────────────────
def test_classify_archive_subtype():
    assert historical_archive.classify_archive_subtype(
        _mk({"asset_id": "Cao Cao statue", "category": "Sculptures"})) == "sculpture"
    assert historical_archive.classify_archive_subtype(
        _mk({"description": "an ancient bronze 青铜器 relic"})) == "artifact"
    assert historical_archive.classify_archive_subtype(
        _mk({"asset_id": "map of western regions", "category": ""})) == "map"
    assert historical_archive.classify_archive_subtype(
        _mk({"asset_id": ""})) == ""


# ── 确认屏三数分类 ───────────────────────────────────────────────────
def test_classify_material_three_buckets():
    # 无画面 → missing
    assert confirmation._classify_material("", {}, "historical_photo")[0] == "missing"
    # 档案提供方 → relevant
    c, r = confirmation._classify_material(
        "/tmp/x.mp4", {"provider": "wikimedia"}, "historical_photo")
    assert c == "relevant" and r == ""
    # 结构化依据 → relevant
    assert confirmation._classify_material(
        "/tmp/x.mp4", {"provider": "pexels", "relevance_basis": "depicts"},
        "historical_photo")[0] == "relevant"
    # AI 插图 → fallback + 原因
    c, r = confirmation._classify_material(
        "/tmp/x.mp4", {"provider": "openai_image"}, "ai_illustration")
    assert c == "fallback" and "AI 插图" in r
    # b_roll → fallback 主题性
    c, r = confirmation._classify_material(
        "/tmp/x.mp4", {"provider": "pexels"}, "b_roll")
    assert c == "fallback" and "b-roll" in r


# ── Ken Burns 方向常量存在（复用限流依赖它）───────────────────────────
def test_kenburns_directions_present():
    from app.services import video
    for d in ("zoom_out", "pan_left", "pan_right", "pan_up", "pan_down"):
        assert d in video.KENBURNS_DIRECTIONS
