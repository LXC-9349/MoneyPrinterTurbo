"""人物生成总览页(概览)。

以「人物列表」为主览:一眼看出哪些人物已经生成了视频、哪些还没生成,
并可从任意人物一键直达传记生成向导(已预选该人物)。

数据来源(全部只读,经既有服务):
  * 人物主数据:``app.services.person_store.list_persons()``
    (library.sqlite 人物 + personal.sqlite 用户叠加层)
  * 生成归属与状态:复用 ``mpt_admin._task_rows()``(权威任务扫描器),
    从每个任务的 ``script.json.params.biography_person`` 反推人物归属,
    成片看 ``final-*.mp4`` 是否存在。

沿用 projects.py 的自包含渲染模式(不 import webui.Main,避免循环依赖)。
"""
from __future__ import annotations

import html
import math
from typing import Any

import streamlit as st

from app.models import const
from app.services import person_store
from webui import base
from webui import mpt_admin

__all__ = ["render_overview_page"]


def _escape(text: Any) -> str:
    """HTML 转义,供 unsafe_allow_html 片段安全插入。"""
    return html.escape(str(text))


def t(key: str) -> str:
    """翻译:当前语言 → 英文 → 原始 key(mpt_admin 的同名实现)。"""
    return mpt_admin.t(key)


# ---------------------------------------------------------------------------
# 数据聚合
# ---------------------------------------------------------------------------
def _person_generation_index() -> dict[str, dict[str, Any]]:
    """扫描任务,按人物聚合生成情况。

    返回 ``{person: {count, done, running, failed, last}}``。生成归属只能从
    任务目录反推:projects 索引的 person 字段对普通视频为空、对传记也不可靠,
    因此这里以 ``script.json.params.biography_person`` 为唯一事实来源。
    """
    index: dict[str, dict[str, Any]] = {}
    try:
        rows = mpt_admin._task_rows()
    except Exception:
        return index

    for row in rows:
        params = (row.get("script") or {}).get("params") or {}
        person = str(params.get("biography_person") or "").strip()
        if not person:
            continue
        rec = index.setdefault(
            person,
            {"count": 0, "done": 0, "running": 0, "failed": 0, "last": 0.0},
        )
        rec["count"] += 1
        state = row.get("state")
        has_video = bool(row.get("video_file"))
        if state == const.TASK_STATE_PROCESSING:
            rec["running"] += 1
        elif state == const.TASK_STATE_FAILED and not has_video:
            rec["failed"] += 1
        else:
            rec["done"] += 1
        try:
            rec["last"] = max(rec["last"], float(row.get("mtime") or 0))
        except (TypeError, ValueError):
            pass
    return index


def _status_key(rec: dict[str, Any] | None) -> str:
    if not rec or not rec.get("count"):
        return "missing"
    if rec.get("running"):
        return "running"
    if rec.get("done"):
        return "done"
    if rec.get("failed"):
        return "failed"
    return "missing"


def _status_badge(rec: dict[str, Any] | None) -> str:
    key = _status_key(rec)
    if key == "done":
        return t("ov_badge_done").format(count=int((rec or {}).get("done", 0)))
    if key == "running":
        return t("ov_badge_running")
    if key == "failed":
        return t("ov_badge_failed")
    return t("ov_badge_missing")


# ---------------------------------------------------------------------------
# 跳转:预选人物并切到传记生成向导
# ---------------------------------------------------------------------------
def _goto_generation(person: str) -> None:
    """把该人物写入生成向导的预选状态,并切回生成页。

    镜像任务恢复流(tasks.py 的传记人物恢复):必须先于控件实例化写入会话态,
    否则前端上报的旧值会覆盖。本页在 admin_page != generate 时渲染,生成模式
    下拉此刻未实例化,因此可直接写其 widget key。
    """
    st.session_state["generation_mode"] = "biography"
    st.session_state["biography_person_input"] = person
    st.session_state["library_person_search"] = person
    st.session_state.pop("library_person_select", None)
    for fk in (
        "library_dynasty_filter",
        "library_category_filter",
        "library_group_filter",
    ):
        st.session_state.pop(fk, None)
    # 强制生成模式下拉显示为传记(按当前语言键)。
    try:
        st.session_state[base.localized_widget_key("generation_mode_select")] = "biography"
    except Exception:
        pass
    base._set_runtime_config("app", "generation_mode", "biography")
    base._set_runtime_config("app", "biography_person", person)
    base._set_runtime_config("app", "video_source", base._BIOGRAPHY_SOURCE)
    # 中转键在下一轮 rerun、radio 实例化前被消费,安全切页。
    st.session_state["admin_page_back"] = "generate"


# ---------------------------------------------------------------------------
# 列表渲染
# ---------------------------------------------------------------------------
# 列宽定义:状态 | 姓名 | 朝代/类别 | 文章数 | 最近 | 操作
_ROW_COLS = [1.1, 3.0, 2.4, 1.0, 1.6, 1.3]


def _render_table_header() -> None:
    with st.container(key="ov_table_header"):
        cols = st.columns(_ROW_COLS, vertical_alignment="center")
        cols[0].markdown(t("ov_col_status"))
        cols[1].markdown(t("ov_col_name"))
        cols[2].markdown(t("ov_col_meta"))
        cols[3].markdown(t("ov_col_articles"))
        cols[4].markdown(t("ov_col_last"))
        cols[5].markdown(t("ov_col_action"))


def _render_row(person_row: dict[str, Any], rec: dict[str, Any] | None) -> None:
    name = person_row.get("person", "")
    status_key = _status_key(rec)
    safe = "".join(ch if ch.isalnum() else "_" for ch in name)[:40]
    badge_cls = {
        "done": "mpt-badge-ok",
        "running": "mpt-badge-warn",
        "failed": "mpt-badge-err",
        "missing": "",
    }.get(status_key, "")

    with st.container(key=f"ov_row_{safe}", border=True):
        c0, c1, c2, c3, c4, c5 = st.columns(_ROW_COLS, vertical_alignment="center")
        with c0:
            badge = _escape(_status_badge(rec))
            st.markdown(
                f"<span class='{badge_cls}'>{badge}</span>" if badge_cls else badge,
                unsafe_allow_html=True,
            )
        with c1:
            title = ("★ " if person_row.get("pinned") else "") + name
            # 只对姓名做转义，未收录徽标的 <span> 需作为 HTML 保留；
            # 若连同徽标一起 _escape，尖括号会被转义成纯文本泄漏到界面。
            unregistered_badge = (
                "  <span class='mpt-badge-warn'>· "
                + _escape(t("ov_unregistered"))
                + "</span>"
                if person_row.get("unregistered")
                else ""
            )
            st.markdown(
                f"**{_escape(title)}**{unregistered_badge}",
                unsafe_allow_html=True,
            )
        with c2:
            meta = person_row.get("dynasty") or "—"
            if person_row.get("category"):
                meta += " · " + str(person_row["category"])
            if person_row.get("groups"):
                meta += " [" + "、".join(person_row["groups"]) + "]"
            st.markdown(
                f"<span style='color:var(--mpt-text-dim)'>{_escape(meta)}</span>",
                unsafe_allow_html=True,
            )
        with c3:
            st.markdown(f"{int(person_row.get('doc_count') or 0)}")
        with c4:
            last = mpt_admin._human_mtime(rec["last"]) if rec and rec.get("last") else "—"
            st.markdown(
                f"<span style='color:var(--mpt-text-dim)'>{_escape(last)}</span>",
                unsafe_allow_html=True,
            )
        with c5:
            label = (
                t("ov_action_regenerate")
                if status_key in ("done", "failed", "running")
                else t("ov_action_generate")
            )
            if st.button(
                label,
                key=f"ov_go_{safe}",
                icon=":material/movie:",
                width="stretch",
                type="primary" if status_key == "missing" else "secondary",
            ):
                _goto_generation(name)
                st.rerun()
                return


def render_overview_page() -> None:
    st.markdown(t("ov_hint"))

    index = _person_generation_index()

    # ---- 总览指标(以「人物主数据 ∪ 有生成记录者」为全集)----
    try:
        all_persons = person_store.list_persons(include_hidden=False)
    except Exception:
        st.warning(t("admin_library_unavailable"))
        return

    known_names = {p["person"] for p in all_persons}
    # 有生成记录、但不在人物主数据里的人(例如语料库未收录的传主)。
    # 必须纳入总览,否则「生成了却看不到」正是本页要解决的问题。
    extra_names = [
        name for name in index.keys() if name and name not in known_names
    ]

    total = len(all_persons) + len(extra_names)
    done_persons = sum(
        1 for p in all_persons if (index.get(p["person"]) or {}).get("done")
    ) + sum(1 for n in extra_names if (index.get(n) or {}).get("done"))
    running_persons = sum(
        1 for p in all_persons if (index.get(p["person"]) or {}).get("running")
    ) + sum(1 for n in extra_names if (index.get(n) or {}).get("running"))
    attempted = sum(
        1 for p in all_persons if (index.get(p["person"]) or {}).get("count")
    ) + len(extra_names)
    missing_persons = total - attempted
    coverage = (done_persons / total * 100.0) if total else 0.0

    m1, m2, m3, m4, m5 = st.columns(5, vertical_alignment="center", gap="small")
    m1.metric(t("ov_metric_total"), total)
    m2.metric(t("ov_metric_done"), done_persons)
    m3.metric(t("ov_metric_running"), running_persons)
    m4.metric(t("ov_metric_missing"), missing_persons)
    m5.metric(t("ov_coverage"), f"{coverage:.0f}%")

    st.divider()

    # ---- 筛选 ----
    f1, f2, f3, f4, f5 = st.columns(
        [1.3, 1.2, 1.2, 1.8, 0.7], vertical_alignment="center"
    )
    with f1:
        status_filter = st.selectbox(
            t("ov_filter_status"),
            options=["all", "done", "missing", "running", "failed"],
            format_func=lambda v: {
                "all": t("ov_status_all"),
                "done": t("ov_status_done"),
                "missing": t("ov_status_missing"),
                "running": t("ov_status_running"),
                "failed": t("ov_status_failed"),
            }[v],
            key="ov_filter_status",
        )
    with f2:
        dyn = st.selectbox(
            t("ov_filter_dynasty"),
            options=[""] + [str(x) for x in person_store.dynasty_options()],
            key="ov_filter_dynasty",
        )
    with f3:
        cat = st.selectbox(
            t("ov_filter_category"),
            options=[""] + [str(x) for x in person_store.category_options()],
            key="ov_filter_category",
        )
    with f4:
        kw = st.text_input(
            t("ov_search"), placeholder=t("ov_search_ph"), key="ov_search"
        )
    with f5:
        if st.button(t("ov_refresh"), key="ov_refresh", icon=":material/refresh:",
                     width="stretch"):
            st.rerun()

    # 服务端筛选(朝代/类别/关键词走 person_store)。
    try:
        persons = person_store.list_persons(
            dynasty=dyn or "",
            category=cat or "",
            keyword=(kw or "").strip(),
            include_hidden=False,
        )
    except Exception:
        persons = []

    # 未登记人物(有生成记录但不在主数据)只在无「朝代/类别」筛选、且命中关键词时纳入。
    kw_l = (kw or "").strip().lower()
    rows: list[dict[str, Any]] = list(persons)
    if not dyn and not cat:
        for name in extra_names:
            if kw_l and kw_l not in name.lower():
                continue
            rows.append(
                {
                    "person": name,
                    "dynasty": "",
                    "category": "",
                    "doc_count": 0,
                    "source": "generated",
                    "pinned": False,
                    "hidden": False,
                    "note": "",
                    "groups": [],
                    "unregistered": True,
                }
            )

    # 排序:置顶 > 已生成 > 文章数。
    def _sort_key(p: dict[str, Any]):
        rec = index.get(p["person"]) or {}
        return (
            not p.get("pinned"),
            not rec.get("count"),
            -int(p.get("doc_count") or 0),
            p.get("person", ""),
        )

    rows = sorted(rows, key=_sort_key)

    if status_filter != "all":
        rows = [
            p for p in rows if _status_key(index.get(p["person"])) == status_filter
        ]

    if not rows:
        st.info(t("ov_empty"))
        return

    # ---- 分页 ----
    pcol1, pcol2, _ = st.columns([1, 1, 5], vertical_alignment="center")
    with pcol1:
        page_size = st.selectbox(
            t("ov_per_page"), options=[20, 50, 100, 200], index=1, key="ov_page_size"
        )
    total_rows = len(rows)
    pages = max(1, math.ceil(total_rows / page_size))
    with pcol2:
        page = st.number_input(
            t("ov_page"), min_value=1, max_value=pages, value=1, step=1, key="ov_page"
        )
    start = (int(page) - 1) * page_size
    end = min(start + page_size, total_rows)
    st.caption(
        t("ov_showing").format(start=start + 1, end=end, total=total_rows)
    )

    _render_table_header()
    for p in rows[start:end]:
        _render_row(p, index.get(p["person"]))