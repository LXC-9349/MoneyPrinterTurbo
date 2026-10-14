// 存储 / 缓存管理面板：各托管子目录占用、视频缓存统计、预览与清理。
// 数据只在客户端读取（storage 属运维信息，无需 SSR），清理前后重新拉取占用。
import { createSignal, onMount, Show, For } from "solid-js";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "~/components/ui/card";
import { Select } from "~/components/ui/select";
import { notify } from "~/components/ui/toast";
import {
  ApiError,
  cleanVideoCache,
  fetchStorageUsage,
  previewVideoCache,
} from "~/lib/api/client";
import type { CachePreview, StorageUsage } from "~/lib/api/types";

// 目录 name → 中文标签（与后端 _STORAGE_DIRS 对齐）。
const DIR_LABELS: Record<string, string> = {
  tasks: "任务产物",
  cache_videos: "视频缓存",
  local_videos: "本地素材",
  cache_material_search: "素材检索缓存",
  bgm: "背景音乐",
  library: "语料库",
};

function formatBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / Math.pow(1024, i)).toFixed(i ? 2 : 0)} ${units[i]}`;
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

const AGE_OPTIONS = [
  { value: "1", label: "仅 1 天前" },
  { value: "7", label: "仅 7 天前" },
  { value: "30", label: "仅 30 天前" },
  { value: "all", label: "全部缓存" },
];

export function StoragePanel() {
  const [usage, setUsage] = createSignal<StorageUsage | null>(null);
  const [loading, setLoading] = createSignal(true);
  const [maxAge, setMaxAge] = createSignal<string>("all");
  const [preview, setPreview] = createSignal<CachePreview | null>(null);
  const [busy, setBusy] = createSignal(false);

  async function load() {
    setLoading(true);
    try {
      setUsage(await fetchStorageUsage());
    } catch (e) {
      notify.error("读取存储占用失败", errMsg(e));
    } finally {
      setLoading(false);
    }
  }
  onMount(load);

  const ageDays = () => (maxAge() === "all" ? undefined : Number(maxAge()));

  async function doPreview() {
    try {
      setPreview(await previewVideoCache(ageDays()));
    } catch (e) {
      notify.error("预览失败", errMsg(e));
    }
  }

  async function doClean() {
    setBusy(true);
    try {
      const r = await cleanVideoCache(ageDays());
      notify.success(
        `已清理 ${r.deleted_count} 个文件（${formatBytes(r.deleted_size)}）`,
        r.failed_count ? `${r.failed_count} 个文件删除失败` : undefined,
      );
      setPreview(null);
      setUsage(await fetchStorageUsage());
    } catch (e) {
      notify.error("清理失败", errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Show
      when={!loading() && usage()}
      fallback={<p class="py-8 text-center text-sm text-ink-dim">统计存储占用中…</p>}
    >
      {(u) => (
        <div class="flex flex-col gap-5">
          <Card>
            <CardHeader>
              <CardTitle>目录占用 · 合计 {formatBytes(u().total_bytes)}</CardTitle>
            </CardHeader>
            <CardContent class="flex flex-col gap-2">
              <For each={u().dirs}>
                {(d) => (
                  <div class="flex items-center justify-between border-b border-line/60 py-1.5 text-sm last:border-0">
                    <span class="text-ink">{DIR_LABELS[d.name] ?? d.name}</span>
                    <span class="text-ink-dim">
                      {d.exists ? `${d.count} 个文件 · ${formatBytes(d.size)}` : "—"}
                    </span>
                  </div>
                )}
              </For>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>视频缓存</CardTitle>
            </CardHeader>
            <CardContent class="flex flex-col gap-3">
              <p class="text-sm text-ink">
                共 {u().video_cache.file_count} 个缓存文件 ·{" "}
                {formatBytes(u().video_cache.total_size)}
              </p>
              <p class="break-all text-xs text-ink-dim">{u().video_cache_dir}</p>
              <div class="flex flex-wrap items-end gap-3">
                <Select
                  label="清理范围"
                  value={maxAge()}
                  options={AGE_OPTIONS}
                  onChange={(v) => {
                    setMaxAge(v ?? "all");
                    setPreview(null);
                  }}
                  class="w-44"
                />
                <Button variant="secondary" size="sm" onClick={doPreview}>
                  预览
                </Button>
                <Button variant="danger" size="sm" onClick={doClean} disabled={busy()}>
                  {busy() ? "清理中…" : "执行清理"}
                </Button>
              </div>
              <Show when={preview()}>
                {(p) => (
                  <p class="text-xs text-ink-dim">
                    符合条件：{p().file_count} 个文件 · {formatBytes(p().total_size)}
                  </p>
                )}
              </Show>
            </CardContent>
          </Card>
        </div>
      )}
    </Show>
  );
}
