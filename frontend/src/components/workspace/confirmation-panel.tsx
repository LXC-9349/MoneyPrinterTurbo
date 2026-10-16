// 分阶段确认屏（里程碑 4 #5）：任务处于 awaiting_confirmation 时展示。
//  · 脚本后暂停：展示已生成脚本，确认后从关键词阶段续跑。
//  · 素材后暂停：逐镜分镜卡片（旁白 / 已选画面 / 候选缩略图 / 相关性 / 来源授权 /
//    缺素材低相关警告），支持从候选选择、换图、上传、排除、删除镜头，确认后从成片
//    阶段续跑。编辑只作用于画面轨（复用已生成配音/字幕），删镜是移除该镜画面卡。
import { For, Show, createSignal, onMount } from "solid-js";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { notify } from "~/components/ui/toast";
import {
  ApiError,
  confirmTask,
  deleteConfirmationShot,
  excludeConfirmationShot,
  getConfirmation,
  resolveApiUrl,
  selectConfirmationShot,
  uploadConfirmationShot,
} from "~/lib/api/client";
import type { ConfirmationShotCard, ConfirmationView } from "~/lib/api/types";

// 已选素材本地路径 → 可服务的相对 URL（storage 下 tasks/… 或 cache_videos/…）。
function assetHref(path: string): string {
  const rel = (path || "").replace(/\\/g, "/");
  if (rel.startsWith("tasks/") || rel.startsWith("cache_videos/")) return resolveApiUrl(rel);
  const i = rel.lastIndexOf("/storage/");
  if (i >= 0) {
    const tail = rel.slice(i + "/storage/".length);
    if (tail.startsWith("tasks/") || tail.startsWith("cache_videos/")) return resolveApiUrl(tail);
  }
  return "";
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
}

export function ConfirmationPanel(props: { taskId: string; onChanged?: () => void }) {
  const [view, setView] = createSignal<ConfirmationView | null>(null);
  const [loading, setLoading] = createSignal(true);
  const [busy, setBusy] = createSignal(false);

  async function reload() {
    try {
      const v = await getConfirmation(props.taskId);
      setView(v);
    } catch (e) {
      notify.error("确认屏加载失败", errMsg(e));
    } finally {
      setLoading(false);
    }
  }
  onMount(() => void reload());

  const wrap = async (fn: () => Promise<unknown>, ok: string) => {
    if (busy()) return;
    setBusy(true);
    try {
      await fn();
      notify.success(ok);
      await reload();
      props.onChanged?.();
    } catch (e) {
      notify.error("操作失败", errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const onConfirm = () =>
    void wrap(async () => {
      const r = await confirmTask(props.taskId);
      notify.info("已确认并继续", `从「${r.resumed_from}」阶段续跑`);
    }, "已确认，任务继续运行");

  const pickCandidate = (idx: number, url: string) =>
    wrap(() => selectConfirmationShot(props.taskId, idx, { url }), "已选择该候选");

  const onUpload = (idx: number, file: File | undefined) => {
    if (!file) return;
    void wrap(() => uploadConfirmationShot(props.taskId, idx, file), "已上传并替换画面");
  };

  const onExclude = (idx: number) =>
    wrap(() => excludeConfirmationShot(props.taskId, idx), "已排除该素材并拉黑");

  const onDelete = (idx: number) => {
    if (!window.confirm("删除该镜头的画面卡？旁白与字幕在续跑时仍按原样复用。")) return;
    void wrap(() => deleteConfirmationShot(props.taskId, idx), "已删除该镜头画面");
  };

  const ShotCard = (sp: { shot: ConfirmationShotCard }) => {
    const href = () => assetHref(sp.shot.chosen);
    return (
      <div class="flex flex-col gap-2 rounded-card border border-line bg-surface-2 p-3">
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <p class="line-clamp-2 text-sm text-ink">{sp.shot.narration || "（无旁白）"}</p>
            <p class="truncate text-[11px] text-ink-dim">
              #{sp.shot.index + 1} · 检索：{sp.shot.query || "—"} · {sp.shot.visual_type}
            </p>
          </div>
          <Show when={sp.shot.relevant_count > 0}>
            <Badge tone="ok" class="!px-2 !text-[0.65rem]">相关 {sp.shot.relevant_count}</Badge>
          </Show>
        </div>

        <Show when={sp.shot.warning}>
          <p class="rounded-control bg-err/15 px-2 py-1 text-[11px] font-semibold text-err">
            ⚠ {sp.shot.warning}
          </p>
        </Show>

        {/* 已选画面 */}
        <div class="flex items-center gap-2">
          <Show
            when={href()}
            fallback={
              <div class="flex h-14 w-14 shrink-0 items-center justify-center rounded-control border border-dashed border-line text-[10px] text-ink-dim">
                无画面
              </div>
            }
          >
            <img src={href()} alt={sp.shot.chosen_name} class="h-14 w-14 shrink-0 rounded-control border border-line object-cover" loading="lazy" />
          </Show>
          <div class="min-w-0 text-[11px] text-ink-dim">
            <p class="truncate" title={sp.shot.chosen}>已选：{sp.shot.chosen_name || "—"}</p>
            <Show when={sp.shot.license}>
              <p class="truncate">
                授权：
                <Show
                  when={sp.shot.license_url}
                  fallback={<span>{sp.shot.license}</span>}
                >
                  <a href={sp.shot.license_url} target="_blank" rel="noreferrer" class="underline">
                    {sp.shot.license}
                  </a>
                </Show>
                <Show when={sp.shot.author}> · {sp.shot.author}</Show>
              </p>
            </Show>
          </div>
        </div>

        {/* 候选 */}
        <Show when={sp.shot.candidates.length > 0}>
          <div class="flex flex-wrap gap-1.5">
            <For each={sp.shot.candidates}>
              {(c) => (
                <button
                  type="button"
                  disabled={busy()}
                  title={`${c.title} · ${c.license || "未知授权"} · 相关性 ${c.relevance}`}
                  onClick={() => void pickCandidate(sp.shot.index, c.thumbnail)}
                  class={
                    "overflow-hidden rounded-control border " +
                    (c.passes_gate && !c.excluded ? "border-ok/40 hover:border-primary" : "border-line opacity-60")
                  }
                >
                  <img src={c.thumbnail} alt={c.title} class="h-12 w-12 object-cover" loading="lazy" />
                </button>
              )}
            </For>
          </div>
        </Show>

        {/* 操作 */}
        <div class="flex flex-wrap items-center gap-1.5 pt-0.5">
          <label class="cursor-pointer rounded-control border border-line bg-surface px-2 py-1 text-[11px] text-ink hover:border-line-strong">
            上传换图
            <input
              type="file"
              accept="image/*,video/*"
              class="hidden"
              disabled={busy()}
              onChange={(e) => onUpload(sp.shot.index, e.currentTarget.files?.[0])}
            />
          </label>
          <Show when={sp.shot.chosen}>
            <Button size="sm" variant="ghost" class="!py-1 !text-[11px]" disabled={busy()} onClick={() => void onExclude(sp.shot.index)}>
              排除
            </Button>
            <Button size="sm" variant="ghost" class="!py-1 !text-[11px] text-err" disabled={busy()} onClick={() => void onDelete(sp.shot.index)}>
              删除镜头
            </Button>
          </Show>
        </div>
      </div>
    );
  };

  return (
    <div class="mt-4 rounded-card border border-warn/40 bg-warn/5 p-4 shadow-card">
      <Show when={loading()} fallback={
        view() ? (
          <>
            <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div class="flex items-center gap-2">
                <span class="h-2.5 w-2.5 shrink-0 rounded-full bg-warn" />
                <h2 class="text-sm font-bold text-ink">
                  {view()!.paused_stage === "materials" ? "素材已就绪，待你确认" : "脚本已生成，待你确认"}
                </h2>
                <Show when={view()!.person}>
                  <span class="text-xs text-ink-dim">· {view()!.person}</span>
                </Show>
              </div>
              <Button size="sm" variant="primary" disabled={busy()} onClick={onConfirm}>
                {busy() ? "处理中…" : "确认并继续"}
              </Button>
            </div>

            {/* 脚本后暂停：展示脚本正文 */}
            <Show when={view()!.paused_stage !== "materials"}>
              <div class="rounded-card border border-line bg-surface p-3">
                <p class="mb-1 text-xs font-semibold text-ink-dim">旁白脚本（确认后将据此配音与检索）</p>
                <p class="whitespace-pre-wrap text-sm text-ink">{view()!.script || "（空）"}</p>
              </div>
            </Show>

            {/* 素材后暂停：逐镜卡片 */}
            <Show when={view()!.paused_stage === "materials"}>
              <div class="mb-3 flex flex-wrap items-center gap-2 text-xs text-ink-dim">
                <span>覆盖率 {Math.round(view()!.coverage * 100)}%</span>
                <span>·</span>
                <span>独立素材 {view()!.independent_assets}</span>
                <span>·</span>
                <span>{view()!.shot_count} 镜</span>
                <Badge tone={view()!.missing_shots.length ? "err" : "ok"}>
                  {view()!.missing_shots.length ? `缺/低相关 ${view()!.missing_shots.length}` : "素材齐备"}
                </Badge>
              </div>
              <div class="flex max-h-[60vh] flex-col gap-2 overflow-y-auto">
                <For each={view()!.shots}>{(shot) => <ShotCard shot={shot} />}</For>
              </div>
              <p class="mt-2 text-[11px] text-ink-dim">
                编辑只作用于画面轨，确认后复用已生成的配音与字幕续跑成片。
              </p>
            </Show>
          </>
        ) : (
          <p class="text-sm text-ink-dim">该任务当前没有可确认的检查点。</p>
        )
      }>
        <div class="h-24 animate-pulse rounded-card border border-line bg-surface" />
      </Show>
    </div>
  );
}
