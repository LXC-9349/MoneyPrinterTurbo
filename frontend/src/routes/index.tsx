// 人物管理页（首页 /，§二）：卡片网格 + 聚合统计 + 工具栏（搜索/筛选/排序/
// 卡片|表格/批量）+ 候选折叠区（史料库惰性物化）+ 新建人物弹窗（姓名联想）。
// 数据来自单次 /workbench/persons/dashboard（SQL 聚合，无 N+1）；筛选状态一律
// 放 URL query（顶栏全局搜索也 navigate 到这里），useSearch 驱动 query 重取。
import { Title } from "@solidjs/meta";
import { createAsync, useNavigate, useLocation } from "@solidjs/router";
import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  createResource,
  onCleanup,
  type Accessor,
} from "solid-js";
import { dashboardQuery, personSuggestQuery, invalidateWorkbench } from "~/lib/api/data";
import {
  createPerson,
  deletePerson,
  patchPerson,
  restorePerson,
  fetchWorkbenchCandidates,
} from "~/lib/api/client";
import type { DashboardResult, PersonCard, PersonSuggestCandidate } from "~/lib/api/types";
import { relTime } from "~/components/shell";
import { cn } from "~/lib/cn";
import { Button } from "~/components/ui/button";
import { Badge } from "~/components/ui/badge";
import { Select, type SelectOption } from "~/components/ui/select";
import { DialogRoot } from "~/components/ui/dialog";
import { notify } from "~/components/ui/toast";

// ── URL query → 后端筛选参数 ──────────────────────────────────────────────
interface Filter {
  keyword?: string;
  dynasty?: string;
  category?: string;
  status?: string;
  sort?: string;
}
interface SearchParams {
  q?: string;
  dynasty?: string;
  category?: string;
  status?: string;
  sort?: string;
  view?: string;
}

const SORT_LABELS: Record<string, string> = {
  "": "综合推荐",
  recent_generated: "最近生成",
  video_count: "视频数",
  article_count: "文章数",
  name: "名称",
  dynasty: "朝代",
  pinned: "置顶优先",
  active: "进行中优先",
  recent_edited: "最近编辑",
};
const STATUS_OPTIONS: SelectOption[] = [
  { value: "", label: "全部状态" },
  { value: "active", label: "有进行中" },
  { value: "videos", label: "有成片" },
  { value: "archived", label: "已归档" },
];

function toOptions(values: string[], allLabel: string): SelectOption[] {
  return [
    { value: "", label: allLabel },
    ...values.map((v) => ({ value: v, label: v })),
  ];
}

export default function PersonsPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const search = createMemo(() => location.query as unknown as SearchParams);

  const filter = createMemo<Filter>(() => ({
    keyword: search().q || undefined,
    dynasty: search().dynasty || undefined,
    category: search().category || undefined,
    status: search().status || undefined,
    sort: search().sort || undefined,
  }));
  const dash = createAsync(() => dashboardQuery(filter()));

  // 工具栏变更写回 URL（保留其余 query 键），由 location.query 响应式驱动重取。
  const patchQuery = (patch: Record<string, string | undefined>) => {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries({ ...search(), ...patch })) {
      if (v) next[k] = v;
    }
    const qs = new URLSearchParams(next).toString();
    navigate(qs ? `/?${qs}` : "/", { replace: true });
  };

  const [dialogOpen, setDialogOpen] = createSignal(false);
  const [menuFor, setMenuFor] = createSignal<string | null>(null);
  const [selected, setSelected] = createSignal<Set<string>>(new Set());
  const [candidatesOpen, setCandidatesOpen] = createSignal(false);

  const persons = createMemo(() => dash()?.persons ?? []);
  const anySelected = createMemo(() => selected().size > 0);

  const toggleSelect = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  // ── 卡片操作（⋯ 菜单，§二）─────────────────────────────────────────────
  const act = async (name: string, kind: "pin" | "unpin" | "archive" | "restore" | "export") => {
    setMenuFor(null);
    try {
      if (kind === "pin" || kind === "unpin") {
        await patchPerson(name, { pinned: kind === "pin" });
        notify.success(kind === "pin" ? "已置顶" : "已取消置顶", name);
      } else if (kind === "archive") {
        await deletePerson(name);
        notify.success("已归档", `${name}（可从「已归档」筛选恢复）`);
      } else if (kind === "restore") {
        await restorePerson(name);
        notify.success("已恢复", name);
      } else if (kind === "export") {
        exportPersons(persons().filter((p) => p.person === name));
        return;
      }
      invalidateWorkbench();
    } catch (e) {
      notify.error("操作失败", e instanceof Error ? e.message : String(e));
    }
  };

  const hardDelete = async (name: string) => {
    setMenuFor(null);
    // 删除二次确认（§二）：归档不影响任务/文章历史，删除需明确键入姓名。
    const typed = window.prompt(`删除「${name}」将从工作台移除其自定义记录。\n输入姓名以确认删除：`);
    if (typed === null) return;
    if (typed.trim() !== name) {
      notify.info("未确认", "输入的姓名与目标不一致，已取消删除。");
      return;
    }
    if (!window.confirm(`再次确认：删除「${name}」？任务与文章历史仍会保留在数据库。`)) return;
    try {
      const r = await deletePerson(name);
      notify.success("已删除", `${name}（outcome: ${r.outcome}）`);
      invalidateWorkbench();
    } catch (e) {
      notify.error("删除失败", e instanceof Error ? e.message : String(e));
    }
  };

  const exportPersons = (list: PersonCard[]) => {
    const blob = new Blob([JSON.stringify(list, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `persons-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const batch = async (kind: "pin" | "archive" | "export") => {
    const names = [...selected()];
    if (kind === "export") {
      exportPersons(persons().filter((p) => names.includes(p.person)));
      return;
    }
    for (const n of names) {
      try {
        if (kind === "pin") await patchPerson(n, { pinned: true });
        else await deletePerson(n);
      } catch {
        /* 单项失败不阻断批量 */
      }
    }
    notify.success(kind === "pin" ? `已置顶 ${names.length} 位` : `已归档 ${names.length} 位`);
    setSelected(new Set<string>());
    invalidateWorkbench();
  };

  const adopt = async (c: PersonSuggestCandidate | PersonCard) => {
    try {
      await createPerson({
        person: c.person,
        dynasty: c.dynasty || undefined,
        category: c.category || undefined,
      });
      notify.success("已采纳到工作台", c.person);
      invalidateWorkbench();
    } catch (e) {
      notify.error("采纳失败", e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <Title>人物 · 传记工作室</Title>
      <div class="mx-auto flex max-w-[1720px] flex-col gap-4 px-5 py-5" onClick={() => setMenuFor(null)}>
        <PersonsToolbar
          dash={dash}
          search={search}
          patchQuery={patchQuery}
          onNew={() => setDialogOpen(true)}
        />

        <Show when={anySelected()}>
          <div class="flex items-center gap-3 rounded-card border border-primary/40 bg-primary/10 px-4 py-2 text-sm">
            <span class="text-ink">已选 {selected().size} 位人物</span>
            <Button size="sm" variant="secondary" onClick={() => void batch("pin")}>
              批量置顶
            </Button>
            <Button size="sm" variant="secondary" onClick={() => void batch("archive")}>
              批量归档
            </Button>
            <Button size="sm" variant="secondary" onClick={() => void batch("export")}>
              导出 JSON
            </Button>
            <button
              type="button"
              class="ml-auto text-xs text-ink-dim hover:text-ink"
              onClick={() => setSelected(new Set<string>())}
            >
              清除选择
            </button>
          </div>
        </Show>

        <main class="min-h-[50vh]">
          <Show when={dash()} fallback={<GridSkeleton />}>
            {(data) => (
              <>
                <Show
                  when={data().persons.length > 0}
                  fallback={<EmptyGuide keyword={search().q} onNew={() => setDialogOpen(true)} />}
                >
                  <Show
                    when={search().view === "table"}
                    fallback={
                      <div class="grid grid-cols-1 gap-4 md:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
                        <For each={data().persons}>
                          {(p) => (
                            <PersonCardView
                              card={p}
                              menuOpen={menuFor() === p.person}
                              checked={selected().has(p.person)}
                              onToggleCheck={() => toggleSelect(p.person)}
                              onMenu={(e) => {
                                e.stopPropagation();
                                setMenuFor((m) => (m === p.person ? null : p.person));
                              }}
                              onOpen={() => navigate(`/persons/${encodeURIComponent(p.person)}`)}
                              onNewTask={() =>
                                navigate(`/persons/${encodeURIComponent(p.person)}?new=1`)
                              }
                              onActiveClick={() =>
                                navigate(`/tasks?person=${encodeURIComponent(p.person)}`)
                              }
                              onAct={(kind) => void act(p.person, kind)}
                              onDelete={() => void hardDelete(p.person)}
                            />
                          )}
                        </For>
                      </div>
                    }
                  >
                    <PersonsTable rows={data().persons} onOpen={(p) => navigate(`/persons/${encodeURIComponent(p)}`)} />
                  </Show>
                </Show>

                <Show when={data().candidate_count > 0 && !search().status}>
                  <CandidatesSection
                    count={data().candidate_count}
                    open={candidatesOpen()}
                    search={search}
                    onToggle={() => setCandidatesOpen((v) => !v)}
                    onAdopt={(c) => void adopt(c)}
                  />
                </Show>
              </>
            )}
          </Show>
        </main>
      </div>

      <NewPersonDialog open={dialogOpen()} onClose={() => setDialogOpen(false)} onAdopted={() => invalidateWorkbench()} />
    </>
  );
}

// ── 工具栏 ────────────────────────────────────────────────────────────────
function PersonsToolbar(props: {
  dash: Accessor<DashboardResult | undefined>;
  search: Accessor<SearchParams>;
  patchQuery: (patch: Record<string, string | undefined>) => void;
  onNew: () => void;
}) {
  const [term, createSignalTerm] = createSignal(props.search().q ?? "");
  // URL 变化（顶栏搜索跳转等）同步回输入框。
  createEffect(() => createSignalTerm(props.search().q ?? ""));
  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    const t = term().trim();
    props.patchQuery({ q: t || undefined });
  };
  return (
    <div class="flex flex-wrap items-center gap-2">
      <form onSubmit={submit} class="flex items-center gap-2">
        <input
          type="search"
          value={term()}
          onInput={(e) => createSignalTerm(e.currentTarget.value)}
          placeholder="搜索姓名、别名、拼音…"
          class="h-8 w-52 rounded-control border border-line bg-surface-2 px-3 text-sm text-ink outline-none placeholder:text-ink-dim/70 focus:border-line-strong focus:ring-2 focus:ring-primary/40"
        />
      </form>
      <Select
        class="w-32"
        value={props.search().dynasty ?? ""}
        options={toOptions(props.dash()?.dynasties ?? [], "全部朝代")}
        onChange={(v) => props.patchQuery({ dynasty: v || undefined })}
      />
      <Select
        class="w-32"
        value={props.search().category ?? ""}
        options={toOptions(props.dash()?.categories ?? [], "全部类别")}
        onChange={(v) => props.patchQuery({ category: v || undefined })}
      />
      <Select
        class="w-32"
        value={props.search().status ?? ""}
        options={STATUS_OPTIONS}
        onChange={(v) => props.patchQuery({ status: v || undefined })}
      />
      <Select
        class="w-36"
        value={props.search().sort ?? ""}
        options={(props.dash()?.sort_options ?? [""]).map((s) => ({
          value: s,
          label: SORT_LABELS[s] ?? s,
        }))}
        onChange={(v) => props.patchQuery({ sort: v || undefined })}
      />
      <div class="ml-auto flex items-center gap-1.5">
        <div class="flex overflow-hidden rounded-control border border-line">
          <button
            type="button"
            title="卡片视图"
            class={cn("px-2.5 py-1.5 text-sm", (props.search().view ?? "grid") === "grid" ? "bg-primary/20 text-ink" : "bg-surface-2 text-ink-dim hover:text-ink")}
            onClick={() => props.patchQuery({ view: undefined })}
          >
            ▦
          </button>
          <button
            type="button"
            title="表格视图"
            class={cn("px-2.5 py-1.5 text-sm", props.search().view === "table" ? "bg-primary/20 text-ink" : "bg-surface-2 text-ink-dim hover:text-ink")}
            onClick={() => props.patchQuery({ view: "table" })}
          >
            ▤
          </button>
        </div>
        <Button variant="primary" size="sm" onClick={props.onNew}>
          ＋ 新建人物
        </Button>
      </div>
    </div>
  );
}

// ── 人物卡片（§二）────────────────────────────────────────────────────────
function PersonCardView(props: {
  card: PersonCard;
  menuOpen: boolean;
  checked: boolean;
  onToggleCheck: () => void;
  onMenu: (e: MouseEvent) => void;
  onOpen: () => void;
  onNewTask: () => void;
  onActiveClick: () => void;
  onAct: (kind: "pin" | "unpin" | "archive" | "restore" | "export") => void;
  onDelete: () => void;
}) {
  const c = () => props.card;
  const insufficient = () => c().used && c().doc_count === 0 && c().article_count === 0;
  return (
    <article
      class={cn(
        "group relative flex flex-col rounded-card border bg-surface shadow-card transition-colors",
        c().hidden ? "border-line opacity-70" : "border-line hover:border-line-strong",
      )}
    >
      {/* 卡片主体：点击进入详情 */}
      <button type="button" class="flex flex-1 flex-col text-left" onClick={props.onOpen}>
        <div class="flex items-center gap-3 px-4 pt-4">
          <span class="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-primary/20 text-lg font-bold text-ink">
            {c().person.slice(0, 1)}
          </span>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5">
              <Show when={c().pinned}>
                <span title="已置顶" class="text-xs text-warn">
                  📌
                </span>
              </Show>
              <h3 class="truncate text-[0.95rem] font-bold text-ink">{c().person}</h3>
            </div>
            <p class="mt-0.5 truncate text-xs text-ink-dim">
              {[c().dynasty || "朝代不详", c().category || "未分类"].join(" · ")}
              <Show when={c().source === "custom"}>{" · 自建"}</Show>
            </p>
          </div>
          {/* 角标：素材不足 / 待确认（§二） */}
          <div class="flex shrink-0 flex-col items-end gap-1">
            <Show when={insufficient()}>
              <Badge tone="warn" class="!px-2 !text-[0.7rem]">
                素材不足
              </Badge>
            </Show>
            <Show when={c().awaiting_count > 0}>
              <Badge tone="err" class="!px-2 !text-[0.7rem]">
                待确认 {c().awaiting_count}
              </Badge>
            </Show>
          </div>
        </div>

        <dl class="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 px-4 text-xs">
          <div class="flex justify-between">
            <dt class="text-ink-dim">▶ 视频</dt>
            <dd class="font-semibold text-ink">{c().video_count}</dd>
          </div>
          <div class="flex justify-between">
            <dt class="text-ink-dim">▤ 文章</dt>
            <dd class="font-semibold text-ink" title={`启用 ${c().article_enabled_count} 篇${c().library_article_count ? ` · 含史料库软引用 ${c().library_article_count} 篇` : ""}`}>
              {c().article_count}
            </dd>
          </div>
          <div class="flex justify-between">
            <dt class="text-ink-dim">⟳ 进行中</dt>
            <dd>
              <Show
                when={c().active_task_count > 0}
                fallback={<span class="font-semibold text-ink">{c().active_task_count}</span>}
              >
                {/* >0 高亮脉动，点击跳转任务页并按该人物筛选（§二） */}
                <span
                  role="button"
                  tabindex={0}
                  class="animate-pulse rounded-full bg-primary/25 px-2 font-bold text-primary"
                  onClick={(e) => {
                    e.stopPropagation();
                    props.onActiveClick();
                  }}
                  onKeyDown={(e) => e.key === "Enter" && props.onActiveClick()}
                >
                  {c().active_task_count}
                </span>
              </Show>
            </dd>
          </div>
          <div class="flex justify-between">
            <dt class="text-ink-dim">更新</dt>
            <dd class="font-medium text-ink" title={c().last_updated ?? undefined}>
              {relTime(c().last_updated)}
            </dd>
          </div>
        </dl>
      </button>

      {/* 底部操作 */}
      <div class="mt-3 flex items-center gap-2 border-t border-line px-3 py-2">
        <label class="flex cursor-pointer items-center gap-1.5 text-xs text-ink-dim" onClick={(e) => e.stopPropagation()}>
          <input
            type="checkbox"
            class="accent-[#4682b4]"
            checked={props.checked}
            onChange={props.onToggleCheck}
          />
        </label>
        <Button size="sm" variant="secondary" class="ml-auto" onClick={(e: MouseEvent) => { e.stopPropagation(); props.onNewTask(); }}>
          新建任务
        </Button>
        <div class="relative">
          <button
            type="button"
            aria-label="更多操作"
            class="rounded-control px-2 py-1 text-sm text-ink-dim hover:bg-surface-2 hover:text-ink"
            onClick={props.onMenu}
          >
            ⋯
          </button>
          <Show when={props.menuOpen}>
            <div
              class="absolute right-0 bottom-9 z-30 w-36 overflow-hidden rounded-card border border-line bg-surface py-1 shadow-card"
              onClick={(e) => e.stopPropagation()}
            >
              <Show
                when={!c().hidden}
                fallback={
                  <MenuBtn label="恢复归档" onClick={() => props.onAct("restore")} />
                }
              >
                <MenuBtn label={c().pinned ? "取消置顶" : "置顶"} onClick={() => props.onAct(c().pinned ? "unpin" : "pin")} />
                <MenuBtn label="归档" onClick={() => props.onAct("archive")} />
              </Show>
              <MenuBtn label="导出 JSON" onClick={() => props.onAct("export")} />
              <MenuBtn label="删除…" tone="err" onClick={props.onDelete} />
            </div>
          </Show>
        </div>
      </div>
    </article>
  );
}

function MenuBtn(props: { label: string; tone?: "err"; onClick: () => void }) {
  return (
    <button
      type="button"
      class={cn(
        "block w-full px-3 py-1.5 text-left text-xs hover:bg-surface-2",
        props.tone === "err" ? "text-err" : "text-ink",
      )}
      onClick={props.onClick}
    >
      {props.label}
    </button>
  );
}

// ── 表格视图（§二 卡片|表格切换）──────────────────────────────────────────
function PersonsTable(props: { rows: PersonCard[]; onOpen: (name: string) => void }) {
  return (
    <div class="overflow-x-auto rounded-card border border-line bg-surface shadow-card">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-line text-left text-xs text-ink-dim">
            <th class="px-3 py-2 font-medium">人物</th>
            <th class="px-3 py-2 font-medium">朝代</th>
            <th class="px-3 py-2 font-medium">类别</th>
            <th class="px-3 py-2 text-right font-medium">视频</th>
            <th class="px-3 py-2 text-right font-medium">文章</th>
            <th class="px-3 py-2 text-right font-medium">进行中</th>
            <th class="px-3 py-2 font-medium">最近更新</th>
          </tr>
        </thead>
        <tbody>
          <For each={props.rows}>
            {(p) => (
              <tr class="cursor-pointer border-b border-line/60 last:border-0 hover:bg-surface-2" onClick={() => props.onOpen(p.person)}>
                <td class="px-3 py-2 font-semibold text-ink">
                  <Show when={p.pinned}>
                    <span class="mr-1 text-xs text-warn">📌</span>
                  </Show>
                  {p.person}
                </td>
                <td class="px-3 py-2 text-ink-dim">{p.dynasty || "—"}</td>
                <td class="px-3 py-2 text-ink-dim">{p.category || "—"}</td>
                <td class="px-3 py-2 text-right text-ink">{p.video_count}</td>
                <td class="px-3 py-2 text-right text-ink" title={`启用 ${p.article_enabled_count} 篇${p.library_article_count ? ` · 含史料库软引用 ${p.library_article_count} 篇` : ""}`}>
                  {p.article_count}
                </td>
                <td class={cn("px-3 py-2 text-right", p.active_task_count > 0 ? "font-bold animate-pulse text-primary" : "text-ink")}>
                  {p.active_task_count}
                </td>
                <td class="px-3 py-2 text-ink-dim" title={p.last_updated ?? undefined}>
                  {relTime(p.last_updated)}
                </td>
              </tr>
            )}
          </For>
        </tbody>
      </table>
    </div>
  );
}

// ── 候选折叠区（史料库尚未使用的人物，惰性物化，§二）─────────────────────
function CandidatesSection(props: {
  count: number;
  open: boolean;
  search: Accessor<SearchParams>;
  onToggle: () => void;
  onAdopt: (c: PersonCard) => void;
}) {
  const PAGE = 50;
  const [kw, setKw] = createSignal("");
  const [list, setList] = createSignal<PersonCard[]>([]);
  const [total, setTotal] = createSignal(props.count);
  const [offset, setOffset] = createSignal(0);
  const [hasMore, setHasMore] = createSignal(false);
  const [loading, setLoading] = createSignal(false);
  const [loaded, setLoaded] = createSignal(false);
  const [err, setErr] = createSignal("");

  const load = async (reset: boolean) => {
    setLoading(true);
    setErr("");
    const base = reset ? 0 : offset();
    try {
      const r = await fetchWorkbenchCandidates({
        keyword: kw().trim() || undefined,
        dynasty: props.search().dynasty || undefined,
        category: props.search().category || undefined,
        offset: base,
        limit: PAGE,
      });
      setList((prev) => (reset ? r.candidates : [...prev, ...r.candidates]));
      setTotal(r.total);
      setHasMore(r.has_more);
      setOffset(base + r.candidates.length);
      setLoaded(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  // 展开时惰性加载首页；展开状态下朝代/类别变化则回到第一页重取（服务端搜索）。
  createEffect(() => {
    const open = props.open;
    const d = props.search().dynasty ?? "";
    const c = props.search().category ?? "";
    if (open) void load(true);
    void d;
    void c;
  });

  const submitSearch = (e: SubmitEvent) => {
    e.preventDefault();
    void load(true);
  };

  const adopt = async (c: PersonCard) => {
    props.onAdopt(c);
    setList((prev) => prev.filter((x) => x.person !== c.person));
    setTotal((t) => Math.max(0, t - 1));
  };

  return (
    <section class="rounded-card border border-line bg-surface shadow-card">
      <button type="button" class="flex w-full items-center justify-between px-4 py-3 text-sm" onClick={props.onToggle}>
        <span class="font-semibold text-ink">
          史料库候选人物
          <span class="ml-2 text-xs font-normal text-ink-dim">
            {total()} 位尚未使用 · 采纳后出现在卡片网格
          </span>
        </span>
        <span class="text-ink-dim">{props.open ? "▴ 收起" : "▾ 展开"}</span>
      </button>
      <Show when={props.open}>
        <div class="border-t border-line px-4 py-3">
          <form onSubmit={submitSearch} class="mb-3 flex items-center gap-2">
            <input
              type="search"
              value={kw()}
              onInput={(e) => setKw(e.currentTarget.value)}
              placeholder="在候选中搜索姓名…（服务端检索，每次最多 50 条）"
              class="h-8 w-72 rounded-control border border-line bg-surface-2 px-3 text-sm text-ink outline-none placeholder:text-ink-dim/70 focus:border-line-strong focus:ring-2 focus:ring-primary/40"
            />
            <Button type="submit" size="sm" variant="secondary">搜索</Button>
          </form>
          <Show when={err()}>
            <p class="mb-2 text-xs text-err">{err()}</p>
          </Show>
          <div class="flex flex-wrap gap-2">
            <Show when={list().length > 0}>
              <For each={list()}>
                {(c) => (
                  <span class="flex items-center gap-1.5 rounded-full border border-line bg-surface-2 py-1 pl-3 pr-1 text-xs text-ink">
                    {c.person}
                    <span class="text-ink-dim">{c.doc_count} 篇</span>
                    <button
                      type="button"
                      class="rounded-full bg-primary/20 px-2 py-0.5 font-semibold text-primary hover:bg-primary/30"
                      onClick={() => void adopt(c)}
                    >
                      采纳
                    </button>
                  </span>
                )}
              </For>
            </Show>
            <Show when={!loading() && loaded() && list().length === 0}>
              <span class="text-xs text-ink-dim">没有匹配的候选。</span>
            </Show>
          </div>
          <div class="mt-3 flex items-center gap-3">
            <Show when={loading()}>
              <span class="text-xs text-ink-dim">加载中…</span>
            </Show>
            <Show when={!loading() && hasMore()}>
              <Button size="sm" variant="ghost" onClick={() => void load(false)}>
                加载更多（已显 {list().length}/{total()}）
              </Button>
            </Show>
          </div>
        </div>
      </Show>
    </section>
  );
}

// ── 空状态引导（§二：不给空白页）──────────────────────────────────────────
function EmptyGuide(props: { keyword: string | undefined; onNew: () => void }) {
  return (
    <div class="flex flex-col items-center gap-3 rounded-card border border-dashed border-line bg-surface px-6 py-16 text-center">
      <span class="text-3xl">👤</span>
      <Show
        when={props.keyword}
        fallback={
          <p class="text-sm text-ink-dim">
            工作台还没有人物。从<span class="text-ink">史料库</span>检索一位人物开始，或新建一个自定义人物。
          </p>
        }
      >
        <p class="text-sm text-ink-dim">
          没有匹配「<span class="text-ink">{props.keyword}</span>」的人物。可尝试新建并采纳史料库候选。
        </p>
      </Show>
      <div class="flex gap-2">
        <Button variant="primary" size="sm" onClick={props.onNew}>
          ＋ 新建人物
        </Button>
        <Button variant="secondary" size="sm" onClick={() => (window.location.href = "/library")}>
          浏览史料库
        </Button>
      </div>
    </div>
  );
}

function GridSkeleton() {
  return (
    <div class="grid grid-cols-1 gap-4 md:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
      <For each={Array.from({ length: 10 })}>
        {() => <div class="h-44 animate-pulse rounded-card border border-line bg-surface" />}
      </For>
    </div>
  );
}

// ── 新建人物弹窗（§二：只输入姓名，联想史料库与候选，一键采纳）────────────
function NewPersonDialog(props: { open: boolean; onClose: () => void; onAdopted: () => void }) {
  const [name, setName] = createSignal("");
  const [debounced, setDebounced] = createSignal("");
  let timer: ReturnType<typeof setTimeout> | undefined;
  createEffect(() => {
    const v = name().trim();
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => setDebounced(v), 280);
  });
  onCleanup(() => {
    if (timer) clearTimeout(timer);
  });

  const [sugg] = createResource(debounced, (n) => (n && props.open ? personSuggestQuery(n) : undefined));
  const candidates = createMemo<PersonSuggestCandidate[]>(() => sugg()?.candidates ?? []);

  const create = async () => {
    const v = name().trim();
    if (!v) return;
    try {
      await createPerson({ person: v });
      notify.success("已新建人物", v);
      setName("");
      props.onAdopted();
      props.onClose();
    } catch (e) {
      notify.error("新建失败", e instanceof Error ? e.message : String(e));
    }
  };

  const adopt = async (c: PersonSuggestCandidate) => {
    try {
      await createPerson({
        person: c.person,
        dynasty: c.dynasty || undefined,
        category: c.category || undefined,
      });
      notify.success("已采纳", `${c.person}（史料库 ${c.doc_count} 篇已关联）`);
      setName("");
      props.onAdopted();
      props.onClose();
    } catch (e) {
      notify.error("采纳失败", e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <DialogRoot open={props.open} onOpenChange={props.onClose} title="新建人物">
      <div class="flex flex-col gap-3">
        <p class="text-xs text-ink-dim">只需输入姓名；若史料库中存在同名人物，可直接采纳并关联全部语料。</p>
        <input
          autofocus
          type="text"
          value={name()}
          onInput={(e) => setName(e.currentTarget.value)}
          onKeyDown={(e) => e.key === "Enter" && void create()}
          placeholder="姓名，如：曹操"
          class="h-9 w-full rounded-control border border-line bg-surface-2 px-3 text-sm text-ink outline-none focus:border-line-strong focus:ring-2 focus:ring-primary/40"
        />
        <Show when={debounced()}>
          <div class="flex max-h-56 flex-col gap-1 overflow-y-auto">
            <Show
              when={candidates().length > 0}
              fallback={<p class="py-2 text-xs text-ink-dim">史料库中没有匹配的人物，可直接创建自定义人物。</p>}
            >
              <For each={candidates()}>
                {(c) => (
                  <div class="flex items-center justify-between gap-2 rounded-control border border-line bg-surface-2 px-3 py-2 text-sm">
                    <span class="min-w-0 truncate text-ink">
                      {c.person}
                      <span class="ml-2 text-xs text-ink-dim">
                        {[c.dynasty, c.category].filter(Boolean).join(" · ")}
                        {c.in_library ? ` · 语料 ${c.doc_count} 篇` : " · 自建"}
                      </span>
                    </span>
                    <Show
                      when={c.in_library}
                      fallback={
                        <Button size="sm" variant="ghost" onClick={() => void create()}>
                          创建
                        </Button>
                      }
                    >
                      <Button size="sm" variant="secondary" onClick={() => void adopt(c)}>
                        采纳
                      </Button>
                    </Show>
                  </div>
                )}
              </For>
            </Show>
          </div>
        </Show>
        <div class="flex justify-end gap-2 pt-1">
          <Button variant="ghost" size="sm" onClick={props.onClose}>
            取消
          </Button>
          <Button variant="primary" size="sm" disabled={!name().trim()} onClick={() => void create()}>
            创建自定义人物
          </Button>
        </div>
      </div>
    </DialogRoot>
  );
}
