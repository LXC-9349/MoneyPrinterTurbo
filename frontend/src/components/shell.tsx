// 应用外壳（§一）：左侧导航 + 顶栏（全局搜索 / 进行中任务抽屉 / 系统状态灯）。
// 挂在 app 根布局，所有业务页共享。进行中任务以客户端轮询驱动，任何页面都可见。
import { A, useNavigate, useLocation } from "@solidjs/router";
import { For, Show, createEffect, createSignal, onMount, onCleanup, type JSX } from "solid-js";
import { cn } from "~/lib/cn";
import { cancelTask, fetchActiveTasks } from "~/lib/api/client";
import type { WorkbenchTask } from "~/lib/api/types";
import { notify } from "~/components/ui/toast";

const NAV = [
  { href: "/", label: "人物", icon: "👤", end: true },
  { href: "/tasks", label: "任务", icon: "▤", end: false },
  { href: "/library", label: "史料库", icon: "🗂", end: false },
  { href: "/settings", label: "设置", icon: "⚙", end: false },
] as const;

// 相对时间（近 7 天内显示「N 分钟前」等，更久回退到日期）。悬停由 title 给绝对时间。
export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(t)) return "—";
  const diff = Date.now() - t;
  const min = Math.round(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.round(hr / 24);
  if (day <= 7) return `${day} 天前`;
  const d = new Date(t);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

// 运行耗时：从 started_at（缺省回退 created_at）到现在的秒数，格式 mm:ss。
// 抽屉每 4s 轮询会重新发布任务数组并触发重渲染，因此该值随轮询实时增长。
export function elapsedSince(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = Date.parse(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

const STAGE_LABEL: Record<string, string> = {
  preflight: "预检",
  script: "脚本",
  terms: "核验",
  audio: "配音",
  subtitle: "字幕",
  materials: "素材",
  video: "合成",
  pipeline: "流水线",
};
export function stageLabel(stage: string | undefined): string {
  if (!stage) return "";
  return STAGE_LABEL[stage] ?? stage;
}

function ProgressBar(props: { value: number }) {
  const pct = () => Math.max(0, Math.min(100, props.value));
  return (
    <div class="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
      <div
        class={cn(
          "h-full rounded-full transition-[width] duration-500",
          pct() >= 100 ? "bg-ok" : "bg-primary",
        )}
        style={{ width: `${pct()}%` }}
      />
    </div>
  );
}

// 进行中任务抽屉：轮询 /workbench/tasks/active，展示进度/阶段/取消。§一。
function ActiveDrawer(props: { open: boolean; onClose: () => void }) {
  return (
    <Show when={props.open}>
      <div
        class="fixed inset-0 z-40 bg-black/40"
        onClick={props.onClose}
        aria-hidden
      />
      <aside class="fixed right-0 top-0 z-50 flex h-full w-full max-w-sm flex-col border-l border-line bg-surface shadow-card">
        <header class="flex items-center justify-between border-b border-line px-4 py-3">
          <h2 class="text-sm font-bold text-ink">进行中任务</h2>
          <button
            type="button"
            class="rounded-control px-2 py-1 text-xs text-ink-dim hover:bg-surface-2 hover:text-ink"
            onClick={props.onClose}
          >
            关闭
          </button>
        </header>
        <div class="flex-1 overflow-y-auto p-3">
          <Show
            when={props.open}
          >
            <TaskList />
          </Show>
        </div>
      </aside>
    </Show>
  );
}

// 抽屉内的任务列表（自身持有轮询信号，由 shell 顶层的 store 提供数据）。
let storeTasks: WorkbenchTask[] = [];
const listeners = new Set<(t: WorkbenchTask[]) => void>();
function publish(tasks: WorkbenchTask[]) {
  storeTasks = tasks;
  for (const fn of listeners) fn(tasks);
}

function TaskList() {
  const [tasks, setTasks] = createSignal<WorkbenchTask[]>(storeTasks);
  onMount(() => {
    listeners.add(setTasks);
    onCleanup(() => listeners.delete(setTasks));
  });
  const navigate = useNavigate();

  // 取消乐观态：点击“取消”先本地标“取消中”，待流水线在阶段边界真正停下、
  // 该任务从活动列表消失后，短暂显示“已取消”再移除（§先修2）。
  const [cancelling, setCancelling] = createSignal<Record<string, string>>({});
  const [flash, setFlash] = createSignal<{ id: string; name: string }[]>([]);
  const timers = new Map<string, ReturnType<typeof setTimeout>>();

  createEffect(() => {
    const live = new Set(tasks().map((t) => t.task_id));
    const cur = cancelling();
    const done = Object.keys(cur).filter((id) => !live.has(id));
    if (!done.length) return;
    const next = { ...cur };
    for (const id of done) {
      delete next[id];
      setFlash((f) => (f.some((x) => x.id === id) ? f : [...f, { id, name: cur[id] }]));
      timers.set(
        id,
        setTimeout(() => {
          setFlash((f) => f.filter((x) => x.id !== id));
          timers.delete(id);
        }, 6000),
      );
    }
    setCancelling(next);
  });

  onCleanup(() => {
    for (const t of timers.values()) clearTimeout(t);
    timers.clear();
  });

  const onCancel = async (id: string, name: string) => {
    if (!window.confirm(`确认取消「${name || id}」？`)) return;
    setCancelling((c) => ({ ...c, [id]: name }));
    try {
      await cancelTask(id);
      notify.info("正在取消", `${name || id}：将在当前阶段结束后停下`);
    } catch (e) {
      setCancelling((c) => {
        const n = { ...c };
        delete n[id];
        return n;
      });
      notify.error("取消失败", e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <Show
        when={tasks().length > 0}
        fallback={
          <Show when={flash().length === 0}>
            <p class="px-1 py-6 text-center text-sm text-ink-dim">暂无进行中的任务。</p>
          </Show>
        }
      >
        <ul class="flex flex-col gap-3">
          <For each={tasks()}>
            {(t) => {
              const isCancelling = () => !!cancelling()[t.task_id];
              return (
                <li class="rounded-card border border-line bg-surface-2 p-3">
                  <div class="flex items-start justify-between gap-2">
                    <div class="min-w-0">
                      <p class="truncate text-sm font-semibold text-ink">
                        {t.person_name || t.title || t.task_id}
                      </p>
                      <p class="mt-0.5 text-xs text-ink-dim">
                        {isCancelling() ? (
                          <span class="font-semibold text-warn">取消中…</span>
                        ) : (
                          <>
                            {stageLabel(t.stage) || t.status}
                            <Show when={t.progress > 0}> · {t.progress}%</Show>
                            <Show when={elapsedSince(t.started_at || t.created_at)}>
                              {" "}· {elapsedSince(t.started_at || t.created_at)}
                            </Show>
                          </>
                        )}
                      </p>
                    </div>
                    <Show when={!isCancelling()}>
                      <span class="shrink-0 text-xs font-semibold text-primary">{t.progress}%</span>
                    </Show>
                  </div>
                  <div class="mt-2">
                    <ProgressBar value={t.progress} />
                  </div>
                  <div class="mt-2 flex justify-end gap-1">
                    <button
                      type="button"
                      class="rounded-control px-2 py-1 text-xs text-ink-dim hover:bg-surface hover:text-ink"
                      onClick={() => navigate(`/tasks/${t.task_id}`)}
                    >
                      详情
                    </button>
                    <Show when={!isCancelling()}>
                      <button
                        type="button"
                        class="rounded-control px-2 py-1 text-xs text-err hover:bg-err/10"
                        onClick={() => void onCancel(t.task_id, t.person_name || t.title)}
                      >
                        取消
                      </button>
                    </Show>
                  </div>
                </li>
              );
            }}
          </For>
        </ul>
      </Show>
      <Show when={flash().length > 0}>
        <ul class="mt-3 flex flex-col gap-2">
          <For each={flash()}>
            {(f) => (
              <li class="flex items-center justify-between rounded-card border border-line bg-surface-2/60 px-3 py-2 text-xs opacity-70">
                <span class="truncate text-ink-dim">{f.name || f.id}</span>
                <span class="font-semibold text-ink-dim">已取消</span>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </>
  );
}

export function AppShell(props: { children?: JSX.Element }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [q, setQ] = createSignal("");
  const [drawer, setDrawer] = createSignal(false);
  const [activeCount, setActiveCount] = createSignal(0);
  const [healthy, setHealthy] = createSignal<boolean | null>(null);

  // 顶栏进行中计数 + 抽屉数据：同一轮询驱动，任何页面都可见（§一）。
  let timer: ReturnType<typeof setInterval> | undefined;
  const poll = async () => {
    try {
      const res = await fetchActiveTasks();
      publish(res.tasks);
      setActiveCount(res.count);
      setHealthy(true);
    } catch {
      setHealthy(false);
    }
  };
  onMount(() => {
    void poll();
    timer = setInterval(() => void poll(), 4000);
    onCleanup(() => timer && clearInterval(timer));
  });

  const submitSearch = (e: SubmitEvent) => {
    e.preventDefault();
    const term = q().trim();
    navigate(term ? `/?q=${encodeURIComponent(term)}` : "/");
  };

  const lightTone = () =>
    healthy() === null ? "bg-warn" : healthy() ? "bg-ok" : "bg-err";

  const isActive = (href: string, end: boolean) =>
    end ? location.pathname === href : location.pathname.startsWith(href);

  return (
    <div class="flex min-h-screen bg-bg">
      {/* 左侧导航 */}
      <nav class="sticky top-0 z-20 flex h-screen w-16 shrink-0 flex-col items-stretch gap-1 border-r border-line bg-surface px-2 py-4 md:w-52">
        <div class="mb-3 flex items-center gap-2 px-1">
          <span class="text-lg">🎬</span>
          <span class="hidden text-sm font-bold text-ink md:inline">传记工作室</span>
        </div>
        <For each={NAV}>
          {(l) => (
            <A
              href={l.href}
              class={cn(
                "flex items-center gap-2.5 rounded-control px-2.5 py-2 text-sm font-medium transition-colors",
                isActive(l.href, l.end)
                  ? "bg-primary/15 text-ink"
                  : "text-ink-dim hover:bg-surface-2 hover:text-ink",
              )}
            >
              <span class="w-5 text-center">{l.icon}</span>
              <span class="hidden md:inline">{l.label}</span>
            </A>
          )}
        </For>
      </nav>

      {/* 右侧主体 */}
      <div class="flex min-w-0 flex-1 flex-col">
        <header class="sticky top-0 z-10 flex items-center gap-3 border-b border-line bg-surface/90 px-5 py-2.5 backdrop-blur">
          <form onSubmit={submitSearch} class="flex flex-1 items-center gap-2">
            <input
              type="search"
              value={q()}
              onInput={(e) => setQ(e.currentTarget.value)}
              placeholder="搜索人物、别名、拼音…"
              class="h-8 w-full max-w-md rounded-control border border-line bg-surface-2 px-3 text-sm text-ink outline-none placeholder:text-ink-dim/70 focus:border-line-strong focus:ring-2 focus:ring-primary/40"
            />
          </form>

          <button
            type="button"
            onClick={() => setDrawer((v) => !v)}
            class={cn(
              "flex items-center gap-1.5 rounded-control border px-2.5 py-1.5 text-sm font-medium transition-colors",
              activeCount() > 0
                ? "border-primary/50 bg-primary/10 text-ink"
                : "border-line bg-surface-2 text-ink-dim hover:text-ink",
            )}
          >
            进行中任务
            <span
              class={cn(
                "inline-flex min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-bold",
                activeCount() > 0 ? "bg-primary text-white animate-pulse" : "bg-surface text-ink-dim",
              )}
            >
              {activeCount()}
            </span>
          </button>

          <button
            type="button"
            title="系统状态"
            onClick={() => navigate("/settings")}
            class="flex items-center gap-1.5 rounded-control border border-line bg-surface-2 px-2.5 py-1.5 text-xs text-ink-dim hover:text-ink"
          >
            <span class={cn("h-2.5 w-2.5 rounded-full", lightTone())} />
            系统
          </button>
        </header>

        <main class="min-w-0 flex-1">{props.children}</main>
      </div>

      <ActiveDrawer open={drawer()} onClose={() => setDrawer(false)} />
    </div>
  );
}
