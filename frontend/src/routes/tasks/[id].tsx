// 任务详情页（/tasks/:id，里程碑 3 #3）。
// 顶部：返回 + 标题 + 状态 + 操作（取消/重试/导出/复制为新任务）+ 阶段时间线。
// 标签页：概览（播放器 + 版本切换）/ 参数快照（只读 + 复制为新任务）/ 分镜与素材 /
// 配音与字幕 / 日志 / 提醒。读走客户端直连 getTaskDetail（活动任务每 3s 轮询刷新，
// 使进度/阶段/时间线实时更新）；写（取消/重试/导出）复用列表页同一批端点。
import { Title } from "@solidjs/meta";
import { useNavigate, useParams } from "@solidjs/router";
import { For, Show, createSignal, onCleanup, onMount } from "solid-js";
import {
  cancelTask,
  exportTaskUrl,
  getTaskDetail,
  resolveApiUrl,
  resumeTask,
  retryTask,
  taskVideoUrl,
} from "~/lib/api/client";
import type {
  TaskDetailResult,
  TaskTimelineStage,
  WorkbenchTaskStatus,
} from "~/lib/api/types";
import { relTime } from "~/components/shell";
import { cn } from "~/lib/cn";
import { Badge, type BadgeTone } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { TabPanel, TabsRoot } from "~/components/ui/tabs";
import { notify } from "~/components/ui/toast";

const STATUS_TONE: Record<WorkbenchTaskStatus, BadgeTone> = {
  draft: "neutral",
  queued: "primary",
  processing: "primary",
  awaiting_confirmation: "warn",
  completed: "ok",
  failed: "err",
  cancelled: "neutral",
};
const STATUS_LABEL: Record<WorkbenchTaskStatus, string> = {
  draft: "草稿",
  queued: "排队中",
  processing: "进行中",
  awaiting_confirmation: "待确认",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const ACTIVE = new Set<WorkbenchTaskStatus>([
  "queued",
  "processing",
  "awaiting_confirmation",
]);

// 阶段耗时格式化：<1s 用毫秒，<60s 一位小数，其余分秒。
function fmtDur(s: number | null): string {
  if (s == null) return "—";
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}分${sec}秒`;
}

const STAGE_DOT: Record<TaskTimelineStage["status"], string> = {
  done: "bg-ok",
  running: "bg-primary animate-pulse",
  failed: "bg-err",
  pending: "bg-line",
};

// 素材本地文件相对 storage 根（tasks/…）时可直接拼 apiBase 访问，否则退回文件名。
function fileHref(path: string | undefined): string {
  const rel = (path || "").replace(/\\/g, "/");
  if (!rel.startsWith("tasks/") && !rel.startsWith("cache_videos/")) return "";
  return resolveApiUrl(rel);
}

function baseName(path: string | undefined): string {
  return (path || "").split(/[\\/]/).pop() || "—";
}

export default function TaskDetail() {
  const params = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = createSignal<TaskDetailResult | null>(null);
  const [loading, setLoading] = createSignal(true);
  const [busy, setBusy] = createSignal(false);
  const [tab, setTab] = createSignal("overview");

  let timer: ReturnType<typeof setInterval> | undefined;
  async function load() {
    try {
      const d = await getTaskDetail(params.id);
      setDetail(d);
    } catch (e) {
      notify.error("加载失败", e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }
  onMount(() => {
    void load();
    // 活动任务轮询：进度/阶段/时间线实时刷新；终态后自动停止。
    timer = setInterval(() => {
      const t = detail()?.task;
      if (t && ACTIVE.has(t.status)) void load();
    }, 3000);
  });
  onCleanup(() => {
    if (timer) clearInterval(timer);
  });

  const task = () => detail()?.task;
  const isActive = () => {
    const t = task();
    return !!t && ACTIVE.has(t.status);
  };
  const isTerminal = () => {
    const t = task();
    return !!t && (t.status === "completed" || t.status === "failed" || t.status === "cancelled");
  };

  const onCancel = async () => {
    const t = task();
    if (!t || !window.confirm("确认取消该任务？将在当前阶段结束后停下。")) return;
    setBusy(true);
    try {
      await cancelTask(t.task_id);
      notify.info("正在取消", `${t.title || t.person_name || t.task_id}：将在当前阶段结束后停下`);
      void load();
    } catch (e) {
      notify.error("取消失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // 重试 / 复制为新任务：以参数快照重新提交一整条新任务（版本 +1），跳到新任务。
  const onDuplicate = async () => {
    const t = task();
    if (!t) return;
    setBusy(true);
    try {
      const r = await retryTask(t.task_id);
      notify.success("已复制为新任务", `新版本 v${r.version}，正在生成…`);
      navigate(`/tasks/${r.task.task_id}`);
    } catch (e) {
      notify.error("复制失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // 从失败阶段继续：同一 task_id 重新派发，复用已生成的中间产物，只重做失败阶段。
  const onResume = async () => {
    const t = task();
    if (!t) return;
    setBusy(true);
    try {
      await resumeTask(t.task_id);
      notify.success("已从失败阶段继续", "复用已生成的中间产物，正在重跑失败阶段…");
      void load();
    } catch (e) {
      notify.error("续跑失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onExport = () => {
    const t = task();
    if (!t) return;
    const a = document.createElement("a");
    a.href = exportTaskUrl(t.task_id);
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  return (
    <div class="mx-auto flex max-w-4xl flex-col gap-4 px-5 py-6">
      <Title>任务详情 · 传记工作室</Title>

      <div class="flex items-center gap-3">
        <Button size="sm" variant="ghost" onClick={() => navigate("/tasks")}>
          ← 任务列表
        </Button>
        <Show when={isActive()}>
          <span class="text-xs text-ink-dim">每 3 秒自动刷新</span>
        </Show>
      </div>

      <Show
        when={!loading()}
        fallback={<div class="h-40 animate-pulse rounded-card border border-line bg-surface" />}
      >
        <Show
          when={detail()}
          fallback={
            <p class="rounded-card border border-line bg-surface p-4 text-sm text-ink-dim">
              未找到任务 <code class="text-ink">{params.id}</code>（可能已被清理）。
            </p>
          }
        >
          {(d) => {
            const t = () => d().task;
            const total = () => d().total_seconds || 0;
            return (
              <>
                {/* 头部 */}
                <div class="flex flex-col gap-4 rounded-card border border-line bg-surface p-5 shadow-card">
                  <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                      <div class="flex flex-wrap items-center gap-2">
                        <h1 class="text-lg font-bold text-ink">
                          {t().title || t().person_name || t().task_id}
                        </h1>
                        <Show when={t().version > 1}>
                          <Badge tone="neutral" class="!px-2 !text-[0.7rem]">v{t().version}</Badge>
                        </Show>
                        <Show when={t().archived}>
                          <Badge tone="neutral" class="!px-2 !text-[0.7rem]">已归档</Badge>
                        </Show>
                      </div>
                      <p class="mt-1 text-sm text-ink-dim">
                        <Show when={t().person_name}>{t().person_name} · </Show>
                        <code class="text-xs">{t().task_id}</code>
                      </p>
                    </div>
                    <div class="flex shrink-0 flex-col items-end gap-2">
                      <Badge tone={STATUS_TONE[t().status] ?? "neutral"}>
                        {STATUS_LABEL[t().status] ?? t().status}
                      </Badge>
                      <div class="flex gap-1">
                        <Show when={isActive()}>
                          <Button size="sm" variant="secondary" disabled={busy()} onClick={() => void onCancel()}>
                            取消
                          </Button>
                        </Show>
                        <Show when={isTerminal() && !t().archived}>
                          <Button size="sm" variant="secondary" disabled={busy()} onClick={() => void onDuplicate()}>
                            重试
                          </Button>
                        </Show>
                        <Show when={t().status === "completed" && d().has_video}>
                          <Button size="sm" variant="primary" onClick={onExport}>
                            导出
                          </Button>
                        </Show>
                      </div>
                    </div>
                  </div>

                  {/* 进度条（活动态） */}
                  <Show when={isActive()}>
                    <div class="flex flex-col gap-1">
                      <div class="flex justify-between text-xs text-ink-dim">
                        <span>当前阶段 · {t().stage || "—"}</span>
                        <span>{t().progress}%</span>
                      </div>
                      <div class="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
                        <div
                          class="h-full rounded-full bg-primary transition-[width] duration-500"
                          style={{ width: `${Math.max(0, Math.min(100, t().progress))}%` }}
                        />
                      </div>
                    </div>
                  </Show>

                  {/* 阶段时间线 */}
                  <div class="border-t border-line pt-3">
                    <div class="mb-2 flex items-center justify-between">
                      <h2 class="text-sm font-bold text-ink">阶段时间线</h2>
                      <span class="text-xs text-ink-dim">
                        总耗时 {fmtDur(total())} · 创建 {relTime(t().created_at)}
                      </span>
                    </div>
                    <Show
                      when={d().timeline.length > 0}
                      fallback={<p class="text-xs text-ink-dim">暂无阶段记录。</p>}
                    >
                      <ul class="flex flex-col gap-1.5">
                        <For each={d().timeline}>
                          {(seg) => {
                            const pct = () =>
                              total() > 0 && seg.seconds != null
                                ? Math.max(2, Math.round((seg.seconds / total()) * 100))
                                : 0;
                            return (
                              <li class="flex items-center gap-2 text-sm">
                                <span class={cn("h-2.5 w-2.5 shrink-0 rounded-full", STAGE_DOT[seg.status])} />
                                <span class="w-16 shrink-0 text-ink">{seg.label}</span>
                                <span class="w-20 shrink-0 tabular-nums text-ink-dim">{fmtDur(seg.seconds)}</span>
                                <div class="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-2">
                                  <div
                                    class={cn(
                                      "h-full rounded-full",
                                      seg.status === "failed" ? "bg-err" : seg.status === "running" ? "bg-primary" : "bg-ok",
                                    )}
                                    style={{ width: `${pct()}%` }}
                                  />
                                </div>
                                <Show when={seg.status === "running"}>
                                  <span class="shrink-0 text-xs text-primary">进行中…</span>
                                </Show>
                              </li>
                            );
                          }}
                        </For>
                      </ul>
                    </Show>
                  </div>
                </div>

                {/* 失败人话卡片（里程碑 3 #4）：仅失败态且有 failure 信息时醒目展示 */}
                <Show when={t().status === "failed" && d().failure}>
                  {(f) => (
                    <div class="mt-4 rounded-card border border-err/40 bg-err/10 p-4 shadow-card">
                      <div class="flex items-center gap-2">
                        <span class="h-2.5 w-2.5 shrink-0 rounded-full bg-err" />
                        <p class="text-sm font-bold text-ink">生成失败 · {f().stage_label || "某阶段"}</p>
                      </div>
                      <p class="mt-2 text-sm text-ink">{f().reason}</p>
                      <p class="mt-1 text-sm text-ink-dim">{f().hint}</p>
                      <Show when={f().detail}>
                        <details class="mt-2">
                          <summary class="cursor-pointer text-xs text-ink-dim">查看原始错误</summary>
                          <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-control border border-line bg-surface-2 p-2 text-xs text-ink-dim">
                            {f().detail}
                          </pre>
                        </details>
                      </Show>
                      <div class="mt-3 flex flex-wrap gap-2">
                        <Button size="sm" variant="primary" disabled={busy()} onClick={() => void onResume()}>
                          从失败阶段继续
                        </Button>
                        <Show when={!t().archived}>
                          <Button size="sm" variant="secondary" disabled={busy()} onClick={() => void onDuplicate()}>
                            复制为新任务
                          </Button>
                        </Show>
                        <Show when={f().fix === "settings"}>
                          <Button size="sm" variant="ghost" onClick={() => navigate("/settings")}>
                            去设置
                          </Button>
                        </Show>
                      </div>
                    </div>
                  )}
                </Show>

                {/* 标签页 */}
                <TabsRoot
                  class="mt-1"
                  value={tab()}
                  onChange={setTab}
                  items={[
                    { value: "overview", label: "概览" },
                    { value: "params", label: "参数快照" },
                    { value: "storyboard", label: "分镜与素材" },
                    { value: "audio", label: "配音与字幕" },
                    { value: "logs", label: `日志 ${d().logs.length || ""}` },
                    { value: "warnings", label: `提醒 ${d().warnings.length || ""}` },
                  ]}
                >
                  {/* 概览：播放器 + 版本切换 */}
                  <TabPanel value="overview" class="pt-4">
                    <div class="flex flex-col gap-4">
                      <Show
                        when={d().has_video}
                        fallback={
                          <div class="rounded-card border border-dashed border-line bg-surface px-4 py-10 text-center text-sm text-ink-dim">
                            {t().status === "completed" ? "未找到可播放的成片文件。" : "成片尚未生成。"}
                          </div>
                        }
                      >
                        <video
                          class="max-h-[60vh] w-full rounded-card border border-line bg-black"
                          controls
                          preload="metadata"
                          src={taskVideoUrl(t().task_id)}
                        />
                      </Show>

                      <Show when={d().versions.length > 1}>
                        <div class="rounded-card border border-line bg-surface p-3">
                          <p class="mb-2 text-xs font-semibold text-ink-dim">版本（{d().versions.length}）</p>
                          <div class="flex flex-wrap gap-2">
                            <For each={d().versions}>
                              {(v) => (
                                <button
                                  type="button"
                                  onClick={() => navigate(`/tasks/${v.task_id}`)}
                                  class={cn(
                                    "flex items-center gap-2 rounded-control border px-2.5 py-1.5 text-xs",
                                    v.task_id === t().task_id
                                      ? "border-primary bg-primary/10 text-ink"
                                      : "border-line bg-surface-2 text-ink-dim hover:text-ink",
                                  )}
                                >
                                  <span class="font-semibold">v{v.version}</span>
                                  <Badge tone={STATUS_TONE[v.status] ?? "neutral"} class="!px-1.5 !text-[0.65rem]">
                                    {STATUS_LABEL[v.status] ?? v.status}
                                  </Badge>
                                  <span>{relTime(v.created_at)}</span>
                                </button>
                              )}
                            </For>
                          </div>
                        </div>
                      </Show>

                      <dl class="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                        <MetaRow k="人物" v={t().person_name || "—"} />
                        <MetaRow k="终止于" v={t().stop_at || "video"} />
                        <MetaRow k="创建" v={relTime(t().created_at)} />
                        <MetaRow k="开始" v={t().started_at ? relTime(t().started_at) : "—"} />
                        <MetaRow k="完成" v={t().completed_at ? relTime(t().completed_at) : "—"} />
                        <MetaRow k="更新" v={relTime(t().updated_at)} />
                        <Show when={t().error}>
                          <div class="col-span-2">
                            <MetaRow k="错误" v={t().error} tone="err" />
                          </div>
                        </Show>
                      </dl>
                    </div>
                  </TabPanel>

                  {/* 参数快照：只读 + 复制为新任务 */}
                  <TabPanel value="params" class="pt-4">
                    <div class="flex flex-col gap-3">
                      <div class="flex items-center justify-between">
                        <p class="text-xs text-ink-dim">只读参数快照（重新提交时按此重建）。</p>
                        <Show when={isTerminal() && !t().archived}>
                          <Button size="sm" variant="secondary" disabled={busy()} onClick={() => void onDuplicate()}>
                            复制为新任务
                          </Button>
                        </Show>
                      </div>
                      <pre class="max-h-[60vh] overflow-auto rounded-card border border-line bg-surface-2 p-3 text-xs text-ink">
                        {JSON.stringify(d().params, null, 2)}
                      </pre>
                    </div>
                  </TabPanel>

                  {/* 分镜与素材 */}
                  <TabPanel value="storyboard" class="pt-4">
                    <div class="flex flex-col gap-4">
                      <Show when={typeof (d().script as { script?: unknown } | null)?.script === "string"}>
                        <div class="rounded-card border border-line bg-surface p-3">
                          <p class="mb-1 text-xs font-semibold text-ink-dim">旁白脚本</p>
                          <p class="whitespace-pre-wrap text-sm text-ink">
                            {String((d().script as { script: string }).script)}
                          </p>
                        </div>
                      </Show>
                      <MaterialsList script={d().script} />
                    </div>
                  </TabPanel>

                  {/* 配音与字幕 */}
                  <TabPanel value="audio" class="pt-4">
                    <div class="flex flex-col gap-3">
                      <ArtifactRow label="配音" path={d().artifacts.audio_file} kind="audio" />
                      <ArtifactRow label="字幕" path={d().artifacts.subtitle_path} kind="file" />
                    </div>
                  </TabPanel>

                  {/* 日志 */}
                  <TabPanel value="logs" class="pt-4">
                    <Show
                      when={d().logs.length > 0}
                      fallback={
                        <p class="py-6 text-center text-sm text-ink-dim">
                          暂无日志（历史任务或 API 提交的任务不保留进程内日志）。
                        </p>
                      }
                    >
                      <pre class="max-h-[60vh] overflow-auto rounded-card border border-line bg-surface-2 p-3 text-xs text-ink">
                        {d().logs.join("\n")}
                      </pre>
                    </Show>
                  </TabPanel>

                  {/* 提醒 */}
                  <TabPanel value="warnings" class="pt-4">
                    <Show
                      when={d().warnings.length > 0}
                      fallback={<p class="py-6 text-center text-sm text-ink-dim">没有提醒。</p>}
                    >
                      <ul class="flex flex-col gap-2">
                        <For each={d().warnings}>
                          {(w) => <WarningItem w={w} />}
                        </For>
                      </ul>
                    </Show>
                  </TabPanel>
                </TabsRoot>
              </>
            );
          }}
        </Show>
      </Show>
    </div>
  );
}

function MetaRow(props: { k: string; v: string; tone?: "err" }) {
  return (
    <>
      <dt class="text-ink-dim">{props.k}</dt>
      <dd class={cn("min-w-0 truncate", props.tone === "err" ? "text-err" : "text-ink")} title={props.v}>
        {props.v}
      </dd>
    </>
  );
}

// 素材列表：来自 script.json 的 material_sources（provider/授权/来源/检索词）。
function MaterialsList(props: { script: unknown }) {
  const sources = () => {
    const s = props.script as { material_sources?: unknown[] } | null;
    return Array.isArray(s?.material_sources) ? (s!.material_sources as Record<string, unknown>[]) : [];
  };
  return (
    <Show
      when={sources().length > 0}
      fallback={<p class="py-6 text-center text-sm text-ink-dim">没有素材记录。</p>}
    >
      <div class="flex flex-col gap-2">
        <p class="text-xs font-semibold text-ink-dim">素材（{sources().length}）</p>
        <For each={sources()}>
          {(m) => (
            <div class="rounded-card border border-line bg-surface px-3 py-2 text-sm">
              <div class="flex items-center justify-between gap-2">
                <span class="truncate text-ink" title={String(m.local_file ?? "")}>
                  {baseName(String(m.local_file ?? ""))}
                </span>
                <Show when={m.provider}>
                  <Badge tone="neutral" class="!px-1.5 !text-[0.65rem]">{String(m.provider)}</Badge>
                </Show>
              </div>
              <p class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-dim">
                <Show when={m.license}>授权 {String(m.license)}</Show>
                <Show when={m.visual_type}>类型 {String(m.visual_type)}</Show>
                <Show when={m.duration}>{String(m.duration)}s</Show>
                <Show when={m.search_term}>
                  <span class="truncate" title={String(m.search_term)}>· {String(m.search_term)}</span>
                </Show>
              </p>
              <Show when={m.credit}>
                <p class="mt-0.5 truncate text-xs text-ink-dim" title={String(m.credit)}>
                  来源 {String(m.credit)}
                </p>
              </Show>
            </div>
          )}
        </For>
      </div>
    </Show>
  );
}

// 配音/字幕产物行：存在则给内联播放或下载链接。
function ArtifactRow(props: { label: string; path?: string; kind: "audio" | "file" }) {
  const href = () => fileHref(props.path);
  return (
    <div class="rounded-card border border-line bg-surface p-3">
      <p class="mb-1 text-xs font-semibold text-ink-dim">{props.label}</p>
      <Show
        when={props.path}
        fallback={<p class="text-sm text-ink-dim">尚未生成。</p>}
      >
        <Show when={href()} fallback={<p class="text-sm text-ink">{baseName(props.path)}（本地路径不可直接访问）</p>}>
          {(h) => (
            <>
              <Show when={props.kind === "audio"}>
                <audio class="w-full" controls preload="none" src={h()} />
              </Show>
              <a class="mt-1 inline-block text-sm text-primary hover:underline" href={h()} target="_blank" rel="noreferrer">
                {baseName(props.path)}
              </a>
            </>
          )}
        </Show>
      </Show>
    </div>
  );
}

// 单条提醒：兼容字符串与 {code,message,...} 对象。
function WarningItem(props: { w: unknown }) {
  const w = () => props.w;
  const asObj = () => (typeof w() === "object" && w() !== null ? (w() as Record<string, unknown>) : null);
  const message = () => {
    const o = asObj();
    if (o) return String(o.message ?? o.msg ?? JSON.stringify(o));
    return String(w());
  };
  const code = () => {
    const o = asObj();
    return o?.code ? String(o.code) : "";
  };
  const isCompliance = () => {
    const c = code().toLowerCase();
    return c.includes("compliance") || c.includes("license") || c.includes("quality") || c.includes("ai_label");
  };
  return (
    <li
      class={cn(
        "rounded-card border px-3 py-2 text-sm",
        isCompliance() ? "border-warn/40 bg-warn/10" : "border-line bg-surface",
      )}
    >
      <Show when={code()}>
        <span class="mr-2 rounded bg-surface-2 px-1.5 py-0.5 text-[0.65rem] text-ink-dim">{code()}</span>
      </Show>
      <span class="text-ink">{message()}</span>
    </li>
  );
}
