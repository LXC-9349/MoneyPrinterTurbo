// 任务管理列表页（/tasks，里程碑 3 #2）。在里程碑 1 的计数/筛选/搜索/实时进度
// 基础上补齐：顶部计数改用后端一次性全量统计、归档视图切换、行多选 + 批量
// 取消/重试/归档/删除/导出。阶段时间线、详情页调优等属后续里程碑项。
import { Title } from "@solidjs/meta";
import { createAsync, useNavigate, useLocation } from "@solidjs/router";
import { For, Show, createEffect, createMemo, createSignal, onMount, onCleanup } from "solid-js";
import { wbTasksQuery, revalidate } from "~/lib/api/data";
import {
  batchTasks,
  cancelTask,
  exportTaskUrl,
  retryTask,
  type TaskBatchAction,
} from "~/lib/api/client";
import type { WorkbenchTask, WorkbenchTaskStatus } from "~/lib/api/types";
import { relTime, stageLabel } from "~/components/shell";
import { cn } from "~/lib/cn";
import { Button } from "~/components/ui/button";
import { Badge, type BadgeTone } from "~/components/ui/badge";
import { Select, type SelectOption } from "~/components/ui/select";
import { notify } from "~/components/ui/toast";

const STATUS_LABEL: Record<WorkbenchTaskStatus, string> = {
  draft: "草稿",
  queued: "排队",
  processing: "进行中",
  awaiting_confirmation: "待确认",
  completed: "完成",
  failed: "失败",
  cancelled: "已取消",
};
const STATUS_TONE: Record<WorkbenchTaskStatus, BadgeTone> = {
  draft: "neutral",
  queued: "primary",
  processing: "primary",
  awaiting_confirmation: "warn",
  completed: "ok",
  failed: "err",
  cancelled: "neutral",
};
const STATUS_OPTIONS: SelectOption[] = [
  { value: "", label: "全部状态" },
  ...(["queued", "processing", "awaiting_confirmation", "completed", "failed", "cancelled", "draft"] as const).map(
    (v) => ({ value: v, label: STATUS_LABEL[v] }),
  ),
];

interface SearchParams {
  status?: string;
  person?: string;
  q?: string;
  archived?: string;
}

export default function TasksPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const search = createMemo(() => location.query as unknown as SearchParams);
  const isArchivedView = createMemo(() => search().archived === "1");

  const tasks = createAsync(() =>
    wbTasksQuery({
      status: search().status || undefined,
      person: search().person || undefined,
      keyword: search().q || undefined,
      archived: isArchivedView() ? 1 : 0,
      limit: 500,
    }),
  );

  // 进行中行进度刷新：存在活动任务时每 4s 触发 wb-tasks 重取（与顶栏抽屉同节奏）。
  onMount(() => {
    const timer = setInterval(() => {
      const hasActive = tasks()?.tasks.some((t) => isActivity(t.status));
      if (hasActive) void revalidate("wb-tasks");
    }, 4000);
    onCleanup(() => clearInterval(timer));
  });

  const rows = createMemo(() => tasks()?.tasks ?? []);
  // 顶部计数优先用后端一次性全量统计（不随当前筛选变化），缺省回退本地扫描。
  const counts = createMemo<{
    active: number;
    queued: number;
    awaiting: number;
    today: number;
    failed: number;
    archived: number;
  }>(() => {
    const c = tasks()?.counts;
    if (c) {
      return {
        active: c.active,
        queued: c.queued,
        awaiting: c.awaiting,
        today: c.completed,
        failed: c.failed,
        archived: c.archived ?? 0,
      };
    }
    const acc = { active: 0, queued: 0, awaiting: 0, today: 0, failed: 0, archived: 0 };
    for (const t of rows()) {
      if (t.status === "processing") acc.active++;
      if (t.status === "queued") acc.queued++;
      if (t.status === "awaiting_confirmation") acc.awaiting++;
      if (t.status === "failed") acc.failed++;
      if (t.status === "completed") acc.today++;
    }
    return acc;
  });

  const patchQuery = (patch: Record<string, string | undefined>) => {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries({ ...search(), ...patch })) if (v) next[k] = v;
    const qs = new URLSearchParams(next).toString();
    navigate(qs ? `/tasks?${qs}` : "/tasks", { replace: true });
  };

  const [term, setTerm] = createSignal(search().q ?? "");
  createEffect(() => setTerm(search().q ?? ""));

  // ── 多选 ──────────────────────────────────────────────────────────────
  const [selected, setSelected] = createSignal<Set<string>>(new Set<string>());
  const clearSelection = () => setSelected(new Set<string>());
  // 切换筛选/刷新后，剔除已不在当前列表的 id，避免对不可见行误操作。
  createEffect(() => {
    const ids = new Set(rows().map((t) => t.task_id));
    setSelected((prev) => {
      const kept = new Set([...prev].filter((id) => ids.has(id)));
      return kept.size === prev.size ? prev : kept;
    });
  });
  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allChecked = createMemo(
    () => rows().length > 0 && rows().every((t) => selected().has(t.task_id)),
  );
  const toggleAll = () =>
    setSelected(allChecked() ? new Set<string>() : new Set(rows().map((t) => t.task_id)));

  const onCancel = async (t: WorkbenchTask) => {
    if (!window.confirm(`确认取消「${t.person_name || t.title || t.task_id}」？`)) return;
    try {
      await cancelTask(t.task_id);
      notify.success("已请求取消", t.person_name || t.title);
      void revalidate("wb-tasks");
    } catch (e) {
      notify.error("取消失败", e instanceof Error ? e.message : String(e));
    }
  };

  const runBatch = async (action: TaskBatchAction, label: string) => {
    const ids = [...selected()];
    if (!ids.length) return;
    if (action === "delete" && !window.confirm(`确认删除选中的 ${ids.length} 个任务？此操作不可撤销。`))
      return;
    try {
      const res = await batchTasks(ids, action);
      notify.success(`${label}完成`, `处理 ${res.count} 项`);
      clearSelection();
      void revalidate("wb-tasks");
    } catch (e) {
      notify.error(`${label}失败`, e instanceof Error ? e.message : String(e));
    }
  };

  const onRetry = async (t: WorkbenchTask) => {
    try {
      const res = await retryTask(t.task_id);
      notify.success("已重新提交", `新版本 v${res.version}`);
      clearSelection();
      void revalidate("wb-tasks");
    } catch (e) {
      notify.error("重试失败", e instanceof Error ? e.message : String(e));
    }
  };

  const exportOne = (t: WorkbenchTask) => {
    const a = document.createElement("a");
    a.href = exportTaskUrl(t.task_id);
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  return (
    <>
      <Title>任务 · 传记工作室</Title>
      <div class="mx-auto flex max-w-[1720px] flex-col gap-4 px-5 py-5">
        {/* 顶部计数（§五）：点击即筛选 */}
        <div class="flex flex-wrap gap-2 text-sm">
          <CountChip label="进行中" value={counts().active} tone="primary" onClick={() => patchQuery({ status: "processing", archived: undefined })} />
          <CountChip label="排队" value={counts().queued} tone="neutral" onClick={() => patchQuery({ status: "queued", archived: undefined })} />
          <CountChip label="待我确认" value={counts().awaiting} tone="warn" onClick={() => patchQuery({ status: "awaiting_confirmation", archived: undefined })} />
          <CountChip label="已完成" value={counts().today} tone="ok" onClick={() => patchQuery({ status: "completed", archived: undefined })} />
          <CountChip label="失败" value={counts().failed} tone="err" onClick={() => patchQuery({ status: "failed", archived: undefined })} />
          <CountChip label="已归档" value={counts().archived} tone="neutral" onClick={() => patchQuery({ archived: "1", status: undefined })} active={isArchivedView()} />
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <form
            class="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              patchQuery({ q: term().trim() || undefined });
            }}
          >
            <input
              type="search"
              value={term()}
              onInput={(e) => setTerm(e.currentTarget.value)}
              placeholder="搜索任务名 / 人物…"
              class="h-8 w-56 rounded-control border border-line bg-surface-2 px-3 text-sm text-ink outline-none placeholder:text-ink-dim/70 focus:border-line-strong focus:ring-2 focus:ring-primary/40"
            />
          </form>
          <Select
            class="w-32"
            value={search().status ?? ""}
            options={STATUS_OPTIONS}
            onChange={(v) => patchQuery({ status: v || undefined })}
          />
          <Show when={isArchivedView()}>
            <Badge tone="neutral">
              归档视图
              <button type="button" class="ml-1.5 hover:text-ink" onClick={() => patchQuery({ archived: undefined })}>
                ✕
              </button>
            </Badge>
          </Show>
          <Show when={search().person}>
            <Badge tone="primary">
              人物：{search().person}
              <button type="button" class="ml-1.5 hover:text-ink" onClick={() => patchQuery({ person: undefined })}>
                ✕
              </button>
            </Badge>
          </Show>
          <Show when={rows().length > 0}>
            <span class="ml-auto text-xs text-ink-dim">共 {rows().length} 条</span>
          </Show>
        </div>

        {/* 批量操作工具栏：仅在有选中项时出现 */}
        <Show when={selected().size > 0}>
          <div class="sticky top-2 z-10 flex flex-wrap items-center gap-2 rounded-card border border-primary/40 bg-surface-2 px-3 py-2 shadow-card">
            <label class="flex items-center gap-1.5 text-xs text-ink-dim">
              <input type="checkbox" checked={allChecked()} onChange={toggleAll} class="accent-[var(--color-primary)]" />
              已选 {selected().size} 项
            </label>
            <div class="ml-auto flex flex-wrap items-center gap-1.5">
              <Button size="sm" variant="ghost" onClick={() => void runBatch("cancel", "取消")}>
                批量取消
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void runBatch("archive", "归档")}>
                批量归档
              </Button>
              <Show when={isArchivedView()}>
                <Button size="sm" variant="ghost" onClick={() => void runBatch("unarchive", "取消归档")}>
                  取消归档
                </Button>
              </Show>
              <Button size="sm" variant="ghost" class="text-err hover:bg-err/10" onClick={() => void runBatch("delete", "删除")}>
                批量删除
              </Button>
              <Button size="sm" variant="ghost" onClick={clearSelection}>
                清除选择
              </Button>
            </div>
          </div>
        </Show>

        <main class="min-h-[50vh]">
          <Show when={tasks()} fallback={<p class="text-sm text-ink-dim">正在加载任务…</p>}>
            <Show
              when={rows().length > 0}
              fallback={
                <div class="flex flex-col items-center gap-3 rounded-card border border-dashed border-line bg-surface px-6 py-16 text-center">
                  <span class="text-3xl">▤</span>
                  <p class="text-sm text-ink-dim">
                    {isArchivedView() ? "暂无归档任务。" : "暂无任务。到人物页选择一位人物，新建生成任务即可开始。"}
                  </p>
                  <Show when={!isArchivedView()}>
                    <Button variant="primary" size="sm" onClick={() => navigate("/")}>
                      去人物页
                    </Button>
                  </Show>
                </div>
              }
            >
              <ul class="flex flex-col gap-2">
                <For each={rows()}>
                  {(t) => (
                    <li
                      class={cn(
                        "flex cursor-pointer items-center gap-3 rounded-card border bg-surface px-3 py-3 transition-colors hover:border-line-strong",
                        isActivity(t.status) ? "border-primary/40" : "border-line",
                        selected().has(t.task_id) && "ring-2 ring-primary/40",
                      )}
                      onClick={() => navigate(`/tasks/${encodeURIComponent(t.task_id)}`)}
                    >
                      <input
                        type="checkbox"
                        checked={selected().has(t.task_id)}
                        onClick={(e: MouseEvent) => e.stopPropagation()}
                        onChange={() => toggleSelect(t.task_id)}
                        class="h-4 w-4 shrink-0 accent-[var(--color-primary)]"
                        aria-label="选择任务"
                      />
                      <div class="min-w-0 flex-[2]">
                        <p class="truncate text-sm font-semibold text-ink">
                          {t.title || t.person_name || t.task_id}
                        </p>
                        <p class="mt-0.5 truncate text-xs text-ink-dim">
                          <Show when={t.person_name}>{t.person_name} · </Show>
                          {t.task_id.slice(0, 8)}
                          <Show when={t.version > 1}> · v{t.version}</Show>
                        </p>
                      </div>
                      <div class="w-24 shrink-0">
                        <Badge tone={STATUS_TONE[t.status]}>{STATUS_LABEL[t.status] ?? t.status}</Badge>
                      </div>
                      <div class="hidden min-w-0 flex-[2] md:block">
                        <Show
                          when={isActivity(t.status)}
                          fallback={
                            <p class="truncate text-xs text-ink-dim">
                              {t.status === "failed" ? t.error || "（见详情）" : "—"}
                            </p>
                          }
                        >
                          <div class="flex items-center gap-2">
                            <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2">
                              <div
                                class="h-full rounded-full bg-primary transition-[width] duration-500"
                                style={{ width: `${Math.min(100, Math.max(0, t.progress))}%` }}
                              />
                            </div>
                            <span class="w-9 shrink-0 text-right text-xs font-semibold text-primary">{t.progress}%</span>
                          </div>
                          <p class="mt-1 text-xs text-ink-dim">
                            当前阶段：{stageLabel(t.stage) || t.stage || "—"}
                          </p>
                        </Show>
                      </div>
                      <div class="w-24 shrink-0 text-right text-xs text-ink-dim" title={t.created_at}>
                        {relTime(t.created_at)}
                      </div>
                      <div class="w-20 shrink-0 text-right text-xs text-ink-dim">{duration(t)}</div>
                      <div class="flex shrink-0 items-center gap-1">
                        <Show when={isActivity(t.status)}>
                          <Button
                            size="sm"
                            variant="ghost"
                            class="text-err hover:bg-err/10"
                            onClick={(e: MouseEvent) => {
                              e.stopPropagation();
                              void onCancel(t);
                            }}
                          >
                            取消
                          </Button>
                        </Show>
                        <Show when={!isActivity(t.status) && !isArchivedView()}>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={(e: MouseEvent) => {
                              e.stopPropagation();
                              void onRetry(t);
                            }}
                          >
                            重试
                          </Button>
                        </Show>
                        <Show when={t.status === "completed" && !isArchivedView()}>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={(e: MouseEvent) => {
                              e.stopPropagation();
                              exportOne(t);
                            }}
                          >
                            导出
                          </Button>
                        </Show>
                        <span class="px-1 text-ink-dim">›</span>
                      </div>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </Show>
        </main>
      </div>
    </>
  );
}

function isActivity(status: WorkbenchTaskStatus): boolean {
  return status === "queued" || status === "processing" || status === "awaiting_confirmation";
}

function duration(t: WorkbenchTask): string {
  const parse = (iso: string | null) => (iso ? Date.parse(iso.endsWith("Z") ? iso : iso + "Z") : NaN);
  const start = parse(t.started_at) || parse(t.created_at);
  if (Number.isNaN(start)) return "—";
  const end = t.completed_at ? parse(t.completed_at) : Date.now();
  const sec = Math.max(0, Math.round((end - start) / 1000));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m < 60 ? `${m} 分 ${s} 秒` : `${Math.floor(m / 60)} 时 ${m % 60} 分`;
}

function CountChip(props: {
  label: string;
  value: number;
  tone: BadgeTone;
  onClick?: () => void;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      class={cn(
        "flex items-center gap-2 rounded-card border bg-surface px-3.5 py-2 shadow-card transition-colors",
        props.active ? "border-line-strong ring-2 ring-primary/40" : "border-line",
        props.onClick ? "hover:border-line-strong" : "cursor-default",
      )}
      onClick={props.onClick}
    >
      <span
        class={cn(
          "text-lg font-bold",
          props.tone === "primary" && "text-primary",
          props.tone === "ok" && "text-ok",
          props.tone === "warn" && "text-warn",
          props.tone === "err" && "text-err",
          props.tone === "neutral" && "text-ink",
        )}
      >
        {props.value}
      </span>
      <span class="text-xs text-ink-dim">{props.label}</span>
    </button>
  );
}
