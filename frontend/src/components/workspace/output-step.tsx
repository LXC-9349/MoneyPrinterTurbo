// 向导·字幕与生成步：字幕开关/位置/显示模式/动画/字体/颜色/描边，摘要，以及
// 提交生成 + 轮询进度。生成走客户端直连 POST /videos，进度来自 workspace 的定点
// 轮询（store.task()）。完成展示产物链接，失败展示 error/failed_stage。
import { For, Show } from "solid-js";
import { createAsync } from "@solidjs/router";
import { Select } from "~/components/ui/select";
import { TextField } from "~/components/ui/text-field";
import { KSwitch } from "~/components/ui/switch";
import { Button } from "~/components/ui/button";
import { Badge } from "~/components/ui/badge";
import { notify } from "~/components/ui/toast";
import { fontsQuery } from "~/lib/api/data";
import { ApiError, resolveApiUrl } from "~/lib/api/client";
import { TaskState, type SubtitleAnimation, type SubtitleDisplayMode } from "~/lib/api/types";
import { useWorkspace } from "~/lib/stores/workspace";

const POSITIONS = [
  { value: "bottom", label: "底部" },
  { value: "top", label: "顶部" },
  { value: "center", label: "居中" },
  { value: "two_thirds_bottom", label: "下三分之二" },
  { value: "custom", label: "自定义" },
];
const DISPLAY: { value: SubtitleDisplayMode; label: string }[] = [
  { value: "sentence", label: "整句" },
  { value: "word_by_word", label: "逐词" },
];
const ANIMATION: { value: SubtitleAnimation; label: string }[] = [
  { value: "none", label: "无" },
  { value: "pop_spring", label: "弹跳" },
];

export function OutputStep() {
  const ws = useWorkspace();
  const fonts = createAsync(() => fontsQuery());
  const task = ws.task;
  const videos = () => task()?.videos ?? task()?.combined_videos ?? [];

  const onSubmit = async () => {
    if (!ws.canProceed("content")) {
      notify.error("内容未完成", ws.stepError("content"));
      return;
    }
    try {
      await ws.submitGenerate();
      notify.success("已提交生成任务");
    } catch (e) {
      notify.error("提交失败", e instanceof ApiError ? e.message : String(e));
    }
  };

  const state = () => {
    const t = task();
    if (!t) return "idle";
    if (t.state === TaskState.COMPLETE) return "done";
    if (t.state === TaskState.FAILED) return "failed";
    return "processing";
  };

  return (
    <div class="flex flex-col gap-4">
      <KSwitch
        label="启用字幕"
        checked={ws.params.subtitle_enabled}
        onChange={(v) => ws.setField("subtitle_enabled", v)}
      />

      <Show when={ws.params.subtitle_enabled}>
        <div class="grid grid-cols-2 gap-3">
          <Select
            label="字幕位置"
            value={ws.params.subtitle_position}
            options={POSITIONS}
            onChange={(v) => v && ws.setField("subtitle_position", v)}
          />
          <Select
            label="字体"
            value={ws.params.font_name}
            options={(fonts()?.fonts ?? []).map((f) => ({ value: f.value, label: f.label }))}
            onChange={(v) => v && ws.setField("font_name", v)}
          />
        </div>
        <div class="grid grid-cols-2 gap-3">
          <Select
            label="显示模式"
            value={ws.params.subtitle_display_mode}
            options={DISPLAY}
            onChange={(v) => v && ws.setField("subtitle_display_mode", v as SubtitleDisplayMode)}
          />
          <Select
            label="字幕动画"
            value={ws.params.subtitle_animation}
            options={ANIMATION}
            onChange={(v) => v && ws.setField("subtitle_animation", v as SubtitleAnimation)}
          />
        </div>
        <div class="grid grid-cols-3 gap-3">
          <TextField
            label="字号"
            type="number"
            value={String(ws.params.font_size)}
            onInput={(v) => ws.setField("font_size", Math.max(1, Number(v) || 1))}
          />
          <TextField
            label="文字颜色"
            value={ws.params.text_fore_color}
            onInput={(v) => ws.setField("text_fore_color", v)}
          />
          <TextField
            label="描边宽度"
            type="number"
            value={String(ws.params.stroke_width)}
            onInput={(v) => ws.setField("stroke_width", Number(v) || 0)}
          />
        </div>
        <Show when={ws.params.subtitle_position === "custom"}>
          <TextField
            label="自定义位置(0-100)"
            type="number"
            value={String(ws.params.custom_position)}
            onInput={(v) => ws.setField("custom_position", Number(v) || 0)}
          />
        </Show>
      </Show>

      <div class="my-1 h-px bg-line" />

      <div class="flex items-center gap-3">
        <Button variant="primary" onClick={onSubmit} disabled={ws.generating()}>
          {ws.generating() ? "生成中…" : "开始生成"}
        </Button>
        <Show when={state() !== "idle"}>
          <Badge
            tone={
              state() === "done" ? "ok" : state() === "failed" ? "err" : "primary"
            }
          >
            {state() === "done" ? "已完成" : state() === "failed" ? "失败" : "进行中"}
          </Badge>
        </Show>
      </div>

      <Show when={state() === "processing"}>
        <div class="flex flex-col gap-1">
          <div class="h-2 w-full overflow-hidden rounded-full bg-surface-2">
            <div
              class="h-full bg-primary transition-all duration-300"
              style={{ width: `${Math.min(100, Math.max(0, task()?.progress ?? 0))}%` }}
            />
          </div>
          <span class="text-xs text-ink-dim">{task()?.progress ?? 0}%</span>
        </div>
      </Show>

      <Show when={state() === "failed"}>
        <p class="rounded-card border border-err/40 bg-err/10 p-3 text-sm text-err">
          生成失败{task()?.failed_stage ? `（阶段：${task()?.failed_stage}）` : ""}：
          {task()?.error ?? "未知错误"}
        </p>
      </Show>

      <Show when={state() === "done" && videos().length > 0}>
        <ul class="flex flex-col gap-2">
          <For each={videos()}>
            {(v) => (
              <li>
                <a
                  href={resolveApiUrl(v)}
                  target="_blank"
                  rel="noreferrer"
                  class="text-sm text-primary underline"
                >
                  查看成片：{v.split("/").pop()}
                </a>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </div>
  );
}
