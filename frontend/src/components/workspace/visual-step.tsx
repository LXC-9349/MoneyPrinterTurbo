// 向导·视觉步：素材来源 / 画幅 / 拼接与转场 / 片段时长与数量 / 匹配开关。
// 传记模式来源固定为历史档案（不暴露来源选择）。来源选项来自 /options/video-sources。
import { For, Show, createSignal } from "solid-js";
import { createAsync } from "@solidjs/router";
import { Select } from "~/components/ui/select";
import { TextField } from "~/components/ui/text-field";
import { KSwitch } from "~/components/ui/switch";
import { Button } from "~/components/ui/button";
import { notify } from "~/components/ui/toast";
import { videoSourcesQuery } from "~/lib/api/data";
import { ApiError, uploadVideoMaterial } from "~/lib/api/client";
import type { VideoAspect, VideoConcatMode, VideoFitMode, VideoTransitionMode } from "~/lib/api/types";
import { useWorkspace } from "~/lib/stores/workspace";

const ASPECTS: { value: VideoAspect; label: string }[] = [
  { value: "9:16", label: "竖屏 9:16" },
  { value: "16:9", label: "横屏 16:9" },
  { value: "1:1", label: "方形 1:1" },
];
const CONCAT: { value: VideoConcatMode; label: string }[] = [
  { value: "random", label: "随机" },
  { value: "sequential", label: "顺序" },
];
const FIT: { value: VideoFitMode; label: string }[] = [
  { value: "cover", label: "裁切填满（cover）" },
  { value: "contain", label: "留黑适配（contain）" },
];
const TRANSITIONS: (VideoTransitionMode | "none")[] = [
  "none",
  "Shuffle",
  "FadeIn",
  "FadeOut",
  "SlideIn",
  "SlideOut",
  "ZoomIn",
  "ZoomOut",
];

export function VisualStep() {
  const ws = useWorkspace();
  const sources = createAsync(() => videoSourcesQuery());
  const [uploading, setUploading] = createSignal(false);
  const isLocal = () => ws.params.video_source === "local";

  const onUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const added: { provider: string; url: string; duration: number }[] = [];
      for (const file of Array.from(files)) {
        const r = await uploadVideoMaterial(file);
        added.push({ provider: "local", url: r.file, duration: 0 });
      }
      ws.setField("video_materials", [...(ws.params.video_materials ?? []), ...added]);
      notify.success(`已上传 ${added.length} 个素材`);
    } catch (e) {
      notify.error("上传失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div class="flex flex-col gap-4">
      <Show when={ws.mode() !== "biography"} fallback={
        <p class="text-sm text-ink-dim">
          传记项目使用历史档案素材源（{ws.params.video_source}），无需选择来源。
        </p>
      }>
        <div class="grid grid-cols-2 gap-3">
          <Select
            label="视频来源"
            value={ws.params.video_source}
            options={(sources()?.groups ?? []).flatMap((g) =>
              g.sources.map((s) => ({ value: s.value, label: `${g.label} · ${s.label}` })),
            )}
            onChange={(v) => v && ws.setField("video_source", v)}
          />
          <Select
            label="画幅"
            value={ws.params.video_aspect}
            options={ASPECTS}
            onChange={(v) => v && ws.setField("video_aspect", v as VideoAspect)}
          />
        </div>
      </Show>

      <div class="grid grid-cols-2 gap-3">
        <Select
          label="拼接模式"
          value={ws.params.video_concat_mode}
          options={CONCAT}
          onChange={(v) => v && ws.setField("video_concat_mode", v as VideoConcatMode)}
        />
        <Select
          label="填充模式"
          value={ws.params.video_fit_mode}
          options={FIT}
          onChange={(v) => v && ws.setField("video_fit_mode", v as VideoFitMode)}
        />
      </div>

      <Select
        label="镜头转场"
        value={ws.params.video_transition_mode ?? "none"}
        options={TRANSITIONS.map((t) => ({
          value: t,
          label: t === "none" ? "无转场" : t,
        }))}
        onChange={(v) =>
          ws.setField(
            "video_transition_mode",
            !v || v === "none" ? null : (v as VideoTransitionMode),
          )
        }
      />

      <div class="grid grid-cols-3 gap-3">
        <TextField
          label="生成数量"
          type="number"
          value={String(ws.params.video_count)}
          onInput={(v) => ws.setField("video_count", Math.max(1, Number(v) || 1))}
        />
        <TextField
          label="片段时长(秒)"
          type="number"
          value={String(ws.params.video_clip_duration)}
          onInput={(v) => ws.setField("video_clip_duration", Math.max(1, Number(v) || 1))}
        />
        <TextField
          label="片段速度"
          type="number"
          value={String(ws.params.video_clip_speed)}
          onInput={(v) => ws.setField("video_clip_speed", Number(v) || 1)}
        />
      </div>

      <KSwitch
        label="素材按文案顺序匹配"
        checked={ws.params.match_materials_to_script}
        onChange={(v) => ws.setField("match_materials_to_script", v)}
      />

      <Show when={isLocal()}>
        <div class="flex flex-col gap-2 rounded-card border border-line bg-surface-2 p-3">
          <p class="text-sm font-semibold text-ink">本地素材（{ws.params.video_materials?.length ?? 0}）</p>
          <input
            type="file"
            accept="video/*"
            multiple
            disabled={uploading()}
            class="text-sm text-ink-dim file:mr-2 file:rounded-control file:border-0 file:bg-primary file:px-3 file:py-1 file:text-white"
            onChange={(e) => void onUpload(e.currentTarget.files)}
          />
          <Show when={(ws.params.video_materials ?? []).length > 0}>
            <ul class="flex max-h-40 flex-col gap-1 overflow-y-auto text-xs text-ink-dim">
              <For each={ws.params.video_materials ?? []}>
                {(m) => <li class="truncate">{m.url}</li>}
              </For>
            </ul>
          </Show>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => ws.setField("video_materials", [])}
          >
            清空素材
          </Button>
        </div>
      </Show>
    </div>
  );
}
